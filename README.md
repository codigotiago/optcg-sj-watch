# optcg-sj-watch

Watches ONE PIECE CARD GAME events at two South Bay stores and emails when a new one is
posted. Seats are first-come-first-served, so the only question that matters is whether
there is still an actual seat — every alert leads with that.

| Store | `organizer_id` | Where | Typical night |
|---|---|---|---|
| ONE PIECE CARD GAME Official Shop San Jose | 9297 | 675 Saratoga Ave, San Jose | Thu/Fri/Sat/Sun |
| CardArt | 1339 | 781 E El Camino Real Suite 100, Sunnyvale | Tuesday |

## How it works

One GitHub Actions job, four times a day, does everything:

1. **Fetch** both stores from the Bandai TCG+ API. Both must succeed — a partial fetch
   would look like that store's events had been deleted and would silently stop alerting
   for it.
2. **Fetch applicant counts**, one request per event, because `count_applicants` only
   exists on the per-event endpoint. A miss leaves the count `null`, which renders as
   "unknown" rather than a wrong number.
3. **Stamp `first_seen_at`** — the UTC time this workflow first observed each id. It never
   changes afterwards. This is metadata now; dedup is `alerted.json`'s job.
4. **Render and send** the alert with `alert.py`.
5. **Commit** `events.json` and `alerted.json`.

Run it by hand any time from the Actions tab. The `force_all` dispatch input alerts on
every upcoming event, ignoring the ledger — useful for checking rendering without waiting
for a store to post something.

### Why Actions does all of it

An earlier version split the work: Actions fetched, and a scheduled Claude routine read
`events.json` over `raw.githubusercontent.com` and wrote the email by hand. That existed
because the sandbox running the routine has a network allowlist that excludes
`api.bandai-tcg-plus.com` but includes GitHub.

It had two costs. Nothing rendered the email — a model improvised it from scratch every
run, so the formatting was different each time. And the anonymous raw fetch only works
while the repo is public. Folding render and send into the same job that already does the
fetching removed both.

### Alerting and dedup

`alerted.json` is the ledger: `{"alerted_ids": [...], "updated_at": "..."}`. An event is
alerted once, when its id first shows up missing from that list, and the id is added
**only after the send succeeds**. If the send works but the commit fails you get one
duplicate on the next run — a duplicate is an annoyance, a miss costs a seat.

Ids accumulate and are never pruned. A few hundred integers is nothing, and pruning would
risk re-alerting an event that briefly vanished from the API.

New events do not trickle in one at a time. A whole season lands at once — one run in
September 2026 saw 37 events spanning October to December. The email leads with the date
span rather than a seat count when that happens, because a season drop is a schedule, not
an emergency.

**What this cannot do.** The API is anonymous, so it only ever sees events, never your
registration. It cannot tell you that you have cleared a waitlist.

## The email

`alert.py` renders it — stdlib only, since the runner gets no `pip install`. Two sections,
"Seats still open" and "Waitlist only", sorted by event date, plus a third for unknown
counts when there are any.

Event titles get no column: most events share one title that carries no information. A
title appears on a row only when it differs from that store's usual one, which is what
surfaces the genuinely different events.

Check rendering without sending:

```
python3 alert.py --force-all --dry-run /tmp/preview
```

That writes `body.html` and `body.txt` and touches nothing else. Local rendering cannot
show you Gmail's actual CSS stripping though — for that, point `ALERT_TO` at yourself and
dispatch with `force_all`.

Recipients and credentials are Actions secrets, not repo contents: `SMTP_USER`,
`SMTP_PASSWORD` (a Gmail app password, which needs 2-Step Verification enabled on the
sending account) and `ALERT_TO` (comma-separated).

## The page

`index.html` is a local viewer — it is not published anywhere. Serve the repo and open it:

```
python3 -m http.server
```

It loads `events.json` instantly, and **Refresh from Bandai** re-queries the live API from
the browser; the API returns `Access-Control-Allow-Origin` reflecting the caller, so no
proxy is needed. Applicant counts are one request per event, so a refresh takes a few
seconds. (Opening the file directly over `file://` may fail CORS — hence the tiny server.)

## The API

No auth, no key, no cookie:

```
https://api.bandai-tcg-plus.com/api/user/event/list?game_title_id=4&country_code[]=US&organizer_id=9297&limit=100&offset=0&order=1
```

- `game_title_id=4` — ONE PIECE CARD GAME (English)
- `organizer_id` — 9297 (San Jose) or 1339 (CardArt); fetched separately and merged
- `apply_start_datetime` is when signups open
- `start_datetime` is wall-clock local to the store and carries no offset, so it is
  formatted, never converted
