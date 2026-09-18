# optcg-sj-watch

Watches ONE PIECE CARD GAME events at two South Bay stores and emails when new ones post.

| Store | `organizer_id` | Address |
|---|---|---|
| ONE PIECE CARD GAME Official Shop San Jose | 9297 | 675 Saratoga Ave, San Jose |
| CardArt | 1339 | 781 E El Camino Real Suite 100, Sunnyvale |

## How it works

`.github/workflows/fetch.yml` runs four times a day, one job:

1. Fetch both stores from the list endpoint. Both must succeed — a partial fetch looks
   like deletions and would silently stop alerting for that store.
2. Fetch `count_applicants`, `entry_type` and `apply_end_datetime` per event. These exist
   only on the per-event endpoint, so this is one request per event. A failed lookup
   yields `null` and renders as "unknown" rather than a wrong number.
3. Write `events.json`.
4. Run `alert.py` — render and send anything whose id is absent from `alerted.json`.
5. Commit `events.json` and `alerted.json`.

Manual run: Actions tab → Run workflow. The `force_all` input alerts on every upcoming
event, ignoring the ledger.

## Files

| File | Purpose |
|---|---|
| `.github/workflows/fetch.yml` | The entire pipeline |
| `alert.py` | Renders and sends. Stdlib only — the runner gets no `pip install` |
| `alerted.json` | Ids already alerted on; appended after a successful send, never pruned |
| `events.json` | Latest snapshot |
| `index.html` | Local viewer, unpublished |

## Dedup

An event alerts once, when its id is absent from `alerted.json`. Ids are added only after
the send returns: a failed send retries next run, and a successful send whose commit fails
costs one duplicate. A duplicate is an annoyance, a miss costs a seat.

## Email

Open seats are split into one table per store, San Jose first, then lottery events,
waitlist only, and seat count unknown when non-empty. Sorted by event date within each.

Subject is `3 new OPCG · 2 open`, or `· all full` when nothing is enterable, or
`· Sep 18 - Dec 30` when more than ten land at once and it is a season drop rather than
something urgent.

`EXCLUDE_TITLES` in `alert.py` drops event types nobody here can attend — currently
Kid's Cup. Matched as a lowercased substring of the title, so prefixes and season markers
do not matter. Excluded events are never alerted and never recorded in the ledger, so
deleting an entry makes its events alert again on the next run.

`entry_type` is `1` for first-come-first-served and `2` for a lottery draw. Lotteries run
far past capacity by design — one has 242 applicants for 32 seats — so they are never
rendered as a waitlist, which would read as hopeless when the draw gives even odds.

Preview without sending:

```
python3 alert.py --force-all --dry-run preview/
```

`--dry-run` writes `body.html` and `body.txt` and touches nothing else. It cannot show
Gmail's CSS stripping; for that, point `ALERT_TO` at yourself and dispatch with `force_all`.

## Secrets

Actions secrets, never repo contents:

| Secret | Value |
|---|---|
| `SMTP_USER` | Sending Gmail address |
| `SMTP_PASSWORD` | Gmail app password (requires 2-Step Verification) |
| `ALERT_TO` | Comma-separated recipients |

## The page

`index.html` is not published. Serve it locally:

```
python3 -m http.server
```

**Refresh from Bandai** re-queries the live API from the browser — the API reflects the
caller's origin in `Access-Control-Allow-Origin`, so no proxy is needed. Counts are one
request per event, so a refresh takes a few seconds. Opening the file over `file://` may
fail CORS.

## API

No auth, no key, no cookie:

```
https://api.bandai-tcg-plus.com/api/user/event/list?game_title_id=4&country_code[]=US&organizer_id=9297&limit=100&offset=0&order=1
https://api.bandai-tcg-plus.com/api/user/event/<id>
```

- 403s without a browser `User-Agent`.
- `game_title_id=4` is ONE PIECE CARD GAME (English).
- The two endpoints name the same data differently (`entryFee` vs `entry_fee`), and only
  the per-event one carries `entry_type`.
- `start_datetime` is wall-clock local to the store and carries no offset — format it,
  never convert it.
- `apply_end_datetime` carries a real UTC offset and appears only on lottery events.
- The API is anonymous, so it sees events but never your registration. It cannot tell you
  that you have cleared a waitlist.
