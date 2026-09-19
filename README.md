# optcg-sj-watch

Checks two South Bay card shops for new One Piece Card Game events and emails me when
any show up.

| Store | `organizer_id` | Address |
|---|---|---|
| ONE PIECE CARD GAME Official Shop San Jose | 9297 | 675 Saratoga Ave, San Jose |
| CardArt | 1339 | 781 E El Camino Real Suite 100, Sunnyvale |

## How it works

A GitHub Actions workflow (`.github/workflows/fetch.yml`) runs four times a day, and each
run does the whole job:

1. Ask Bandai's API for the event list at both stores. If either store fails, the run
   stops. A half-finished fetch looks exactly like a store deleting all its events, and
   the rest of the run would quietly stop emailing about that store.
2. Ask again about each event one at a time, to find out how many people have applied,
   whether it's a lottery, and when applications close. The list doesn't include any of
   that, so it costs one extra request per event. If a request fails, the field is left
   empty and shows up as "unknown" rather than as a wrong number.
3. Save it all to `events.json`.
4. Run `alert.py`, which emails about any event it hasn't emailed about before. It sticks
   to Python's standard library, because nothing installs packages on the runner.
5. Commit `events.json` and `alerted.json` back to the repo.

You can also start a run by hand from the Actions tab. Tick `force_all` and it emails
about every upcoming event, including ones it has already sent.

## How it avoids repeat emails

`alerted.json` holds the ids of events it has already emailed about. Anything missing
from that list gets an email, and then its id is added.

Ids are only added once the email has actually sent, and nothing is ever taken off the
list. So if sending fails, the next run tries again. If the email sends but the commit
afterwards fails, you get one duplicate.

## The email

The subject line says how many events are new and how many you can still get into, like
`3 new OPCG · 2 open`. That second number counts open seats plus lotteries you can still
enter. If there's nothing you can enter it says `· all full` instead. And if more than
ten events arrive at once, which is what happens when a whole season posts, it shows the
date range instead: `· Sep 18 - Dec 30`.

Open seats are listed one table per store, San Jose first. That order comes from
`STORE_PRIORITY` in `alert.py`. Lottery events are never shown as a waitlist.

`EXCLUDE_TITLES` in `alert.py` is the list of event types to skip, which right now is
just Kid's Cup. It matches against the lowercased title, so whatever prefix or season
marker the name carries doesn't matter. Skipped events are never emailed about and never
recorded, so taking something off that list makes its events email on the next run.

Worth knowing if you change how the email looks: `--dry-run` writes it out to a file, but
that file can't show you what Gmail will do to it, since Gmail strips a lot of CSS. To
really check, point `ALERT_TO` at your own address and run it with `force_all`.

## Secrets

These live in the repo's Actions secrets, never in a file here.

| Secret | Value |
|---|---|
| `SMTP_USER` | The Gmail address it sends from |
| `SMTP_PASSWORD` | A Gmail app password, which needs 2-Step Verification turned on |
| `ALERT_TO` | Who to email, comma-separated |

## The local page

`index.html` isn't published anywhere. To look at it, run `python3 -m http.server` and
open it through that — opening the file directly tends to fail on CORS.

The **Refresh from Bandai** button re-fetches from the live API right in the browser.
That works because the API echoes whoever is asking back in its
`Access-Control-Allow-Origin` header, so there's no need for a proxy.

## Notes on Bandai's API

No key, no login, no cookie:

```
https://api.bandai-tcg-plus.com/api/user/event/list?game_title_id=4&country_code[]=US&organizer_id=9297&limit=100&offset=0&order=1
https://api.bandai-tcg-plus.com/api/user/event/<id>
```

- It returns 403 unless you send a browser-like `User-Agent`.
- `game_title_id=4` means One Piece Card Game, English.
- The two endpoints spell the same things differently — `entryFee` in one, `entry_fee` in
  the other. Only the per-event one has `entry_type`, where `1` means first-come
  first-served and `2` means a lottery.
- `start_datetime` is the local time at the store, with no timezone attached to it. Print
  it as it comes; don't convert it.
- `apply_end_datetime` does carry a real UTC offset, and only lottery events have it.
- Because there's no login, it only ever knows about events, never about your own
  registration. It can't tell you whether you got off a waitlist.
