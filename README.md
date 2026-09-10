# optcg-sj-watch

Daily snapshot of ONE PIECE CARD GAME events at **ONE PIECE CARD GAME Official Shop San Jose**,
so a scheduled Claude Code routine can email when a new one is posted.

## Why this repo exists

The Bandai TCG+ event API is public, but the Anthropic cloud sandbox that runs the routine has a
fixed network allowlist that does not include `api.bandai-tcg-plus.com`. It *does* allow GitHub.
So GitHub Actions does the fetching, and the routine reads the result from
`raw.githubusercontent.com`.

## How it works

1. `.github/workflows/fetch.yml` runs at 07:00 UTC (12:00am PDT), calls the API, and commits
   `events.json` — but only when the content actually changed.
2. A Claude Code routine runs at 07:05 UTC, reads `events.json`, keeps events whose
   `event_created_at` is within the last 26 hours, and emails them. No new events, no email.

## The API

No auth, no key, no cookie:

```
https://api.bandai-tcg-plus.com/api/user/event/list?game_title_id=4&country_code[]=US&organizer_id=9297&limit=100&offset=0&order=1
```

- `game_title_id=4` — ONE PIECE CARD GAME (English)
- `organizer_id=9297` — the San Jose shop

`apply_start_datetime` is the field that matters: it's when signups open.

Run it by hand any time from the Actions tab (`workflow_dispatch`).
