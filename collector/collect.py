"""Roblox Radar collector.

Runs on GitHub Actions every 30 minutes. Uses only public, unauthenticated
Roblox endpoints (no cookies, no account) and writes JSON + images into the repo:

  data/latest.json        current metrics for every tracked game
  data/new.json           games created in the last 14 days, by players online
  data/charts.json        Roblox chart lists (Top Trending, Up-and-Coming, ...) with ranks
  data/ccu/<id>.json      players-online history per game (last 14 days, 30-min steps)
  data/groups.json        member counts of developer groups, one point per day
  data/media.json         icon / thumbnail versions and change history
  media/<id>-icon-<v>.webp, media/<id>-thumb-<v>.webp
"""
import json, os, sys, time, uuid, urllib.request, urllib.error, urllib.parse, datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
MEDIA = os.path.join(ROOT, "media")
UA = {"User-Agent": "roblox-radar/1.0 (+github actions; public stats)", "Accept": "application/json"}
NOW = dt.datetime.now(dt.timezone.utc)
TS = int(NOW.timestamp())
TODAY = NOW.strftime("%Y-%m-%d")
KEEP_DAYS = 14
TRACK_DAYS = 30          # keep tracking a game this long after it last showed up in a chart
MAX_TRACKED = 800
MAX_IMAGES_PER_RUN = 240   # icons+thumbnails downloaded per run; the rest follow in later runs
from concurrent.futures import ThreadPoolExecutor

def log(*a):
    print(*a, flush=True)


def get(url, tries=3, raw=False):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                b = r.read()
                return b if raw else json.loads(b)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (i + 1)); continue
            if e.code >= 500:
                time.sleep(2 * (i + 1)); continue
            print("HTTP", e.code, url); return None
        except Exception as e:  # network hiccup
            print("ERR", e, url); time.sleep(2 * (i + 1))
    return None


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


# 1. Roblox charts ---------------------------------------------------------
def fetch_charts():
    sid = str(uuid.uuid4())
    charts, token = [], ""
    for _ in range(12):
        url = f"https://apis.roblox.com/explore-api/v1/get-sorts?sessionId={sid}&device=computer&country=all"
        if token:
            url += "&sortsPageToken=" + urllib.parse.quote(token)
        j = get(url)
        if not j:
            break
        for s in j.get("sorts", []):
            games = s.get("games") or []
            if not games:
                continue
            charts.append({
                "id": s.get("sortId"),
                "name": s.get("sortDisplayName") or s.get("sortId"),
                "games": [{"universeId": g.get("universeId"), "rank": i + 1,
                           "name": g.get("name"), "playing": g.get("playerCount")}
                          for i, g in enumerate(games) if g.get("universeId")],
            })
        token = j.get("nextSortsPageToken") or ""
        if not token:
            break
    return charts




def main():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(MEDIA, exist_ok=True)
    tracked = load(os.path.join(DATA, "tracked.json"), {})      # id -> {firstSeen, lastSeen}
    seeds = load(os.path.join(ROOT, "collector", "seeds.json"), [])
    for s in seeds:
        tracked.setdefault(str(s), {"firstSeen": TODAY, "lastSeen": TODAY})

    charts = fetch_charts()
    log("charts:", len(charts), [c["id"] for c in charts])
    in_chart = {}
    for c in charts:
        for g in c["games"]:
            uid = str(g["universeId"])
            in_chart.setdefault(uid, []).append({"id": c["id"], "name": c["name"], "rank": g["rank"]})
            t = tracked.setdefault(uid, {"firstSeen": TODAY})
            t["lastSeen"] = TODAY

    # drop games not seen in charts for TRACK_DAYS (seeds stay)
    cutoff = (NOW - dt.timedelta(days=TRACK_DAYS)).strftime("%Y-%m-%d")
    seedset = {str(s) for s in seeds}
    tracked = {k: v for k, v in tracked.items() if k in seedset or v.get("lastSeen", TODAY) >= cutoff}
    ids = sorted(tracked, key=lambda k: tracked[k].get("lastSeen", ""), reverse=True)[:MAX_TRACKED]

    # 2. game details + votes ------------------------------------------------
    details, votes = {}, {}
    parts = list(chunks(ids, 50))
    with ThreadPoolExecutor(6) as ex:
        dj = list(ex.map(lambda p: get("https://games.roblox.com/v1/games?universeIds=" + ",".join(p)), parts))
        vj = list(ex.map(lambda p: get("https://games.roblox.com/v1/games/votes?universeIds=" + ",".join(p)), parts))
    for j in dj:
        for g in (j or {}).get("data", []):
            details[str(g["id"])] = g
    for v in vj:
        for g in (v or {}).get("data", []):
            votes[str(g["id"])] = g
    log("details:", len(details))

    # 3. players-online history ---------------------------------------------
    keep_from = TS - KEEP_DAYS * 86400
    for uid, g in details.items():
        p = os.path.join(DATA, "ccu", uid + ".json")
        h = [x for x in load(p, []) if x[0] >= keep_from]
        h.append([TS, g.get("playing") or 0, g.get("visits") or 0])
        save(p, h)

    # 4. developer groups (once a day is enough) -----------------------------
    groups = load(os.path.join(DATA, "groups.json"), {})
    gids = {str(g["creator"]["id"]) for g in details.values()
            if (g.get("creator") or {}).get("type") == "Group"}
    todo = [g for g in sorted(gids) if not (groups.get(g, {}).get("history") or [[None]])[-1][0] == TODAY][:300]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda gid: (gid, get(f"https://groups.roblox.com/v1/groups/{gid}", tries=2)), todo))
    for gid, j in res:
        hist = groups.setdefault(gid, {"name": "", "history": []})
        if j and "memberCount" in j:
            hist["name"] = j.get("name", hist["name"])
            hist["history"] = (hist["history"] + [[TODAY, j["memberCount"]]])[-90:]
    log("groups updated:", len(res))

    # 5. icons and thumbnails ------------------------------------------------
    media = load(os.path.join(DATA, "media.json"), {})
    icons, thumbs = {}, {}
    for part in chunks(ids, 100):
        j = get("https://thumbnails.roblox.com/v1/games/icons?universeIds=" + ",".join(part)
                + "&size=256x256&format=Webp&returnPolicy=PlaceHolder")
        for d in (j or {}).get("data", []):
            if d.get("state") == "Completed":
                icons[str(d["targetId"])] = d["imageUrl"]
    for part in chunks(ids, 50):
        j = get("https://thumbnails.roblox.com/v1/games/multiget/thumbnails?universeIds=" + ",".join(part)
                + "&countPerUniverse=3&size=768x432&format=Webp")
        for u in (j or {}).get("data", []):
            ok = [t for t in u.get("thumbnails", []) if t.get("state") == "Completed"]
            if ok:
                thumbs[str(u["universeId"])] = ok
    jobs = []  # (uid, kind, url, name, ver/ids)
    for uid in ids:   # ids are ordered by chart recency, so the hottest games get images first
        m = media.setdefault(uid, {"icon": "", "iconVer": "", "thumb": "", "thumbIds": [], "changes": []})
        iurl = icons.get(uid)
        if iurl:
            parts_ = iurl.split("/")
            ver = parts_[3][-12:] if len(parts_) > 3 else str(TS)
            if ver != m["iconVer"]:
                jobs.append((uid, "icon", iurl, f"media/{uid}-icon-{ver}.webp", ver))
        th = thumbs.get(uid)
        if th:
            tids = [str(t["targetId"]) for t in th]
            if tids != m["thumbIds"]:
                jobs.append((uid, "thumbnail", th[0]["imageUrl"], f"media/{uid}-thumb-{tids[0]}.webp", tids))
    jobs = jobs[:MAX_IMAGES_PER_RUN]
    with ThreadPoolExecutor(12) as ex:
        blobs = list(ex.map(lambda jb: get(jb[2], tries=2, raw=True), jobs))
    for (uid, kind, url, name, ver), b in zip(jobs, blobs):
        if not b:
            continue
        open(os.path.join(ROOT, name), "wb").write(b)
        m = media[uid]
        if kind == "icon":
            if m["iconVer"]:
                m["changes"].append({"d": TODAY, "what": "icon", "old": m["icon"], "new": name})
            m["icon"], m["iconVer"] = name, ver
        else:
            if m["thumbIds"]:
                m["changes"].append({"d": TODAY, "what": "thumbnail", "old": m["thumb"], "new": name})
            m["thumb"], m["thumbIds"] = name, ver
        m["changes"] = m["changes"][-30:]
    log("images downloaded:", sum(1 for b in blobs if b), "of", len(jobs))

    # 6. latest + new --------------------------------------------------------
    latest = []
    for uid in ids:
        g = details.get(uid)
        if not g:
            continue
        v = votes.get(uid, {})
        cr = g.get("creator") or {}
        grp = groups.get(str(cr.get("id"))) if cr.get("type") == "Group" else None
        hist = load(os.path.join(DATA, "ccu", uid + ".json"), [])
        day_ago = [x for x in hist if x[0] <= TS - 86400]
        week_ago = [x for x in hist if x[0] <= TS - 7 * 86400]
        def pct(old):
            return round(((g.get("playing") or 0) - old[-1][1]) / old[-1][1] * 100, 1) if old and old[-1][1] else None
        gm = grp["history"] if grp else []
        latest.append({
            "universeId": uid, "placeId": g.get("rootPlaceId"), "name": g.get("name"),
            "creator": cr.get("name"), "creatorType": cr.get("type"), "creatorId": cr.get("id"),
            "created": (g.get("created") or "")[:10], "updated": (g.get("updated") or "")[:10],
            "genre": g.get("genre_l1") or g.get("genre"), "subgenre": g.get("genre_l2"),
            "playing": g.get("playing"), "visits": g.get("visits"), "favorites": g.get("favoritedCount"),
            "up": v.get("upVotes"), "down": v.get("downVotes"),
            "ccuChange24h": pct(day_ago), "ccuChange7d": pct(week_ago),
            "peak14d": max([x[1] for x in hist] or [0]),
            "groupMembers": gm[-1][1] if gm else None,
            "groupChange7d": (gm[-1][1] - gm[-8][1]) if len(gm) >= 8 else None,
            "charts": in_chart.get(uid, []),
            "icon": media.get(uid, {}).get("icon"), "thumb": media.get(uid, {}).get("thumb"),
            "firstSeen": tracked[uid].get("firstSeen"),
        })
    latest.sort(key=lambda x: x.get("playing") or 0, reverse=True)
    new_from = (NOW - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    new = [x for x in latest if x["created"] and x["created"] >= new_from]

    save(os.path.join(DATA, "latest.json"), {"updatedAt": NOW.isoformat(), "games": latest})
    save(os.path.join(DATA, "new.json"), {"updatedAt": NOW.isoformat(), "games": new})
    save(os.path.join(DATA, "charts.json"), {"updatedAt": NOW.isoformat(), "charts": charts})
    save(os.path.join(DATA, "groups.json"), groups)
    save(os.path.join(DATA, "media.json"), media)
    save(os.path.join(DATA, "tracked.json"), tracked)
    log(f"done: {len(latest)} games, {len(new)} new in 14 days")


if __name__ == "__main__":
    main()
