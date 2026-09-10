# optcg-sj-watch

Snapshot of ONE PIECE CARD GAME events at two South Bay stores, so a scheduled Claude
Code routine can email when a new one is posted.

| Store | `organizer_id` | Where | Typical night |
|---|---|---|---|
| ONE PIECE CARD GAME Official Shop San Jose | 9297 | 675 Saratoga Ave, San Jose | Thu/Fri/Sat/Sun |
| CardArt | 1339 | 781 E El Camino Real Suite 100, Sunnyvale | Tuesday |

## Why this repo exists

The Bandai TCG+ event API is public, but the Anthropic cloud sandbox that runs the routine has a
fixed network allowlist that does not include `api.bandai-tcg-plus.com`. It *does* allow GitHub.
So GitHub Actions does the fetching, and the routine reads the result from
`raw.githubusercontent.com`.

## How it works

## The site

**https://codigotiago.github.io/optcg-sj-watch/**

Upcoming events at both stores, per store, with seats/applicants, price and a signup
link. Loads `events.json` instantly, and **Refresh from Bandai** re-queries the live API
from the browser — the API returns `Access-Control-Allow-Origin` reflecting the caller,
so no proxy is needed. Applicant counts are one request per event, so a refresh takes a
few seconds.

## How the watcher works

Four times a day:

1. `.github/workflows/fetch.yml` runs at 23:45, 02:45, 06:45 and 15:45 UTC (4:45pm,
   7:45pm, 11:45pm and 8:45am PDT),
   calls the API, and commits `events.json`. Every event carries `first_seen_at` — the
   time this workflow first observed that id. It never changes afterwards.
2. A Claude Code routine runs at 00:05, 03:05, 07:05 and 16:05 UTC (5:05pm, 8:05pm,
   12:05am and 9:05am PDT), reads `events.json`, and emails any event whose
   `first_seen_at` is within 20 hours. No new events, no email.

   The window is 20h rather than 26h: the longest gap between runs is 9 hours, so 20h
   still survives a completely missed run while cutting repeat alerts. Event alerts go
   to Santiago and Christina; failure notices go to Santiago only.

The 20-minute gap absorbs raw.githubusercontent.com's ~5 minute CDN TTL (which ignores
cache-busting) and GitHub's habit of running scheduled workflows late.

**Why a stamp and not a diff.** Diffing each fetch against the previous snapshot gives
exact once-only alerts, but it is lossy: a late scheduled run marks the event new on a
run nobody reads, then the next run clears it, and the alert is gone. `first_seen_at` is
stable, so a late fetch only delays an alert. The cost is that a 26-hour window against
two runs a day can email the same event twice. That is the right way to be wrong — a
duplicate is an annoyance, a miss costs a seat.

**What this cannot do.** The API is anonymous, so it only sees events, never your
registration. It cannot tell you that you have cleared a waitlist.

## The API

No auth, no key, no cookie:

```
https://api.bandai-tcg-plus.com/api/user/event/list?game_title_id=4&country_code[]=US&organizer_id=9297&limit=100&offset=0&order=1
```

- `game_title_id=4` — ONE PIECE CARD GAME (English)
- `organizer_id` — 9297 (San Jose) or 1339 (CardArt); fetched separately and merged

Both fetches must succeed or the workflow fails. A partial fetch would look like that
store's events had been deleted, and would silently stop alerting for it.

`apply_start_datetime` is the field that matters: it's when signups open.

Run it by hand any time from the Actions tab (`workflow_dispatch`).

## The routine

Claude Code routine `trig_01QbduLhEoisvbxcet4DgwcT` — "One Piece San Jose event watch",
cron `5 7 * * *` (UTC). Manage at https://claude.ai/code/routines

Alerts go to santiagoblair@gmail.com and zoo.christina@gmail.com via the Gmail
connector. No new events, no email.
