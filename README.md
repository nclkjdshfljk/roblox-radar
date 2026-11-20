# roblox-radar

Every 30 minutes a GitHub Action collects public Roblox game statistics: charts (Top Trending, Up-and-Coming and others), players online, visits, votes, developer group sizes, game icons and thumbnails.

It uses only public, unauthenticated Roblox endpoints. No account or cookies are involved.

- `data/latest.json`: current metrics for every tracked game
- `data/new.json`: games created in the last 14 days
- `data/charts.json`: Roblox chart lists with ranks
- `data/ccu/<universeId>.json`: players-online history (14 days)
- `data/groups.json`: developer group member counts per day
- `data/media.json` and `media/`: icons, thumbnails and their change history

Run manually: Actions → collect → Run workflow.
