#!/usr/bin/env python3
"""Render and send the new-event alert for the South Bay OPCG watch.

Runs in the same GitHub Actions job that fetched events.json, so it needs no
network of its own beyond SMTP. Stdlib only: the runner gets no pip install.

Deliberately has no broad try/except. If rendering breaks, the traceback and a
nonzero exit are the right outcome -- a blank or half-rendered alert is worse
than no alert at all.
"""

import argparse
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

# Both stores, keyed by organizer_id. Resolved by id and never by the `store`
# string in events.json, which is stamped by a jq branch that mislabels any
# organizer it does not know about.
STORES = {
    9297: {
        "name": "ONE PIECE CARD GAME Official Shop San Jose",
        "short": "San Jose",
        "addr": "675 Saratoga Ave, San Jose",
        "travel": "5 min walk",
    },
    1339: {
        "name": "CardArt",
        "short": "CardArt",
        "addr": "781 E El Camino Real Suite 100, Sunnyvale",
        "travel": "10-15 min drive",
    },
}

STORE_TZ = ZoneInfo("America/Los_Angeles")

# Lifted from index.html so the email and the page agree.
INK = "#1a1a1a"
MUTED = "#6b6b6b"
LINE = "#e2e0da"
ACCENT = "#b3261e"
OK_BG = "#e6f4ea"
OK_INK = "#14532d"
FULL_INK = "#7f1d1d"
PAGE_BG = "#f7f6f3"
PANEL = "#ffffff"

# entry_type on the per-event endpoint: 1 is first-come-first-served, 2 is a
# lottery draw. The two disagree about what count_applicants means -- 242
# applicants against 32 seats is a hopeless queue under one and even odds
# under the other -- so they are never rendered the same way.
ENTRY_TYPE_LOTTERY = 2


# --------------------------------------------------------------------------
# formatting helpers (ports of the functions index.html already uses)
# --------------------------------------------------------------------------

def store_of(event):
    return STORES.get(event.get("organizer_id"), {
        "name": event.get("organizer_name") or "Unknown store",
        "short": event.get("organizer_name") or "Unknown",
        "addr": "",
        "travel": "",
    })


def pacific_now_key():
    """'now' as a store-local wall-clock string, for comparing against
    start_datetime (which is naive and local to the store)."""
    return datetime.now(STORE_TZ).strftime("%Y-%m-%dT%H:%M")


def is_upcoming(event, now_key):
    return str(event.get("start_datetime") or "")[:16] >= now_key


def fmt_when(value):
    """-> ('Thu', 'Oct 4', '5:30 PM'). start_datetime carries no offset and is
    local to the store, so it is formatted, never converted."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", str(value or ""))
    if not m:
        return ("", str(value or "?"), "")
    year, month, day, hour, minute = (int(m.group(i)) for i in range(1, 6))
    d = datetime(year, month, day)
    suffix = "PM" if hour >= 12 else "AM"
    hour12 = hour % 12 or 12
    return (
        d.strftime("%a"),
        "%s %d" % (d.strftime("%b"), day),
        "%d:%02d %s" % (hour12, minute, suffix),
    )


def fmt_price(event):
    """-> (text, is_free). The API spells one currency three ways."""
    try:
        n = float(event.get("entryFee"))
    except (TypeError, ValueError):
        return ("--", False)
    if n == 0:
        return ("Free", True)
    raw = str(event.get("entry_fee_currency_code") or "").strip()
    amount = ("%.2f" % n).replace(".00", "")
    if re.match(r"^(usd|\$)$", raw, re.I):
        return ("$" + amount, False)
    return ((raw + " " if raw else "") + amount, False)


def lottery_detail(event, applied, cap):
    """'closes Oct 2', or 'entry closed' once the deadline has passed. A drawn
    lottery and an open one look identical otherwise, and only one of them is
    worth acting on -- which is why the deadline gets the line rather than the
    entrant count. Entrant count is the fallback when there is no deadline, so
    the cell is never blank."""
    raw = event.get("apply_end_datetime")
    if not raw:
        return "%s entered / %d seats" % (
            applied if applied is not None else "?", cap or 0)
    # apply_end_datetime carries a real UTC offset, unlike start_datetime.
    closes = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if closes <= datetime.now(timezone.utc):
        return "entry closed"
    return "closes %s" % fmt_when(closes.astimezone(STORE_TZ)
                                  .strftime("%Y-%m-%dT%H:%M"))[1]


def seat_state(event):
    """-> (kind, headline, detail). kind is 'open', 'lottery', 'waitlist' or
    'unknown'.

    Derived from max_join_count - count_applicants the way index.html does.
    seats_left in the snapshot goes as low as -209, so it is never dropped
    into a sentence as-is.
    """
    cap = event.get("max_join_count")
    applied = event.get("count_applicants")

    # Checked first. A lottery routinely runs far past capacity by design, so
    # falling through to the waitlist branch would report 242 applicants for
    # 32 seats as "210 on waitlist" -- reading as hopeless when the draw in
    # fact gives everyone the same odds.
    if event.get("entry_type") == ENTRY_TYPE_LOTTERY:
        return ("lottery", "Lottery", lottery_detail(event, applied, cap))

    # Strings stay short because Gmail's mobile app overrides white-space:nowrap
    # when it squeezes the table onto a phone, and a long cell wraps to three
    # lines. Capacity is the bare number: "9 left / 16 total" distinguishes a
    # CardArt 16-seater from a San Jose 32-seater without spelling it out.
    if applied is None or cap is None:
        return ("unknown", "Seats unknown", "no count")
    left = cap - applied
    if left > 0:
        return ("open", "%d left" % left, "%d total" % cap)
    deep = applied - cap
    return (
        "waitlist",
        "%d waiting" % deep if deep > 0 else "Full",
        "%d total" % cap,
    )


def esc(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def title_for(event):
    # Shown on every row. An earlier version hid titles matching the store's
    # most common one, to cut noise from a 37-row season drop -- but that also
    # hid the only thing saying what you are signing up for.
    title = (event.get("event_series_title") or "").strip()
    # Every event here is ONE PIECE, so the prefix is dead weight, and a
    # leading "[Oct-Dec 2026]" or "[Official Shop]" is a catalogue marker
    # rather than a name. Stripping both gets every title but one under 30
    # characters, which is what stops the store column wrapping to four lines.
    if title.startswith("ONE PIECE CARD GAME "):
        title = title[len("ONE PIECE CARD GAME "):]
    title = re.sub(r"^\[[^\]]*\]\s*", "", title)
    # Never truncated past this. A clipped name is worse than a tall row --
    # knowing which event it is, is the whole reason the title is here.
    return title


def date_span(events):
    """'Oct 4 - Dec 30' across a set of events."""
    keys = sorted(str(e.get("start_datetime") or "") for e in events)
    first = fmt_when(keys[0])[1]
    last = fmt_when(keys[-1])[1]
    return first if first == last else "%s - %s" % (first, last)


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

# Event types nobody here can attend. Matched as a substring against the
# lowercased title, so the "[Official Shop] " prefix and any season marker do
# not matter. Excluded events are never alerted and never recorded, so
# removing an entry here makes its events alert again.
EXCLUDE_TITLES = ("kid's cup",)


def is_excluded(event):
    # Bandai mixes the straight and curly apostrophe between fields -- the
    # title uses U+0027 while the excerpt uses U+2019 -- so normalise before
    # matching rather than trusting one of them.
    title = (event.get("event_series_title") or "").lower().replace("’", "'")
    return any(bad in title for bad in EXCLUDE_TITLES)


def select_new(snapshot, alerted_ids, force_all):
    now_key = pacific_now_key()
    rows = [
        e for e in snapshot.get("events", [])
        if is_upcoming(e, now_key)
        and not e.get("is_canceled")
        and not is_excluded(e)
        and (force_all or e.get("id") not in alerted_ids)
    ]
    rows.sort(key=lambda e: str(e.get("start_datetime") or ""))
    return rows


# San Jose leads: it is the five-minute walk, so its open seats are the ones
# worth seeing first. Only the open-seats section splits this way -- a lottery
# or a waitlist is not something you act on by store.
STORE_PRIORITY = (9297, 1339)


def by_store(rows):
    """-> [(store label, rows)], priority stores first, then anything else."""
    groups = {}
    for e in rows:
        groups.setdefault(e.get("organizer_id"), []).append(e)
    ordered = [oid for oid in STORE_PRIORITY if oid in groups]
    ordered += [oid for oid in groups if oid not in STORE_PRIORITY]
    return [(store_of(groups[oid][0])["short"], groups[oid])
            for oid in ordered]


def bucket(rows):
    out = {"open": [], "lottery": [], "waitlist": [], "unknown": []}
    for e in rows:
        out[seat_state(e)[0]].append(e)
    return out


def subject_for(rows, buckets):
    """Short enough to survive a phone inbox's truncation, and different
    enough between cases that the inbox line alone says whether anything is
    gettable."""
    n = len(rows)
    # A whole season drops at once -- 37 events spanning three months landed in
    # a single run. Leading with the span says "schedule", which is what it is.
    # A handful of events is the case where the seat count is the headline.
    if n > 10:
        return "%d new OPCG · %s" % (n, date_span(rows))
    # A lottery you can still enter is just as actionable as an open seat.
    actionable = len(buckets["open"]) + len(buckets["lottery"])
    if actionable == 0:
        return "%d new OPCG · all full" % n
    return "%d new OPCG · %d open" % (n, actionable)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def _cell(content, extra=""):
    return (
        '<td style="padding:9px 7px;border-bottom:1px solid %s;'
        'vertical-align:top;font-size:14px;color:%s;%s">%s</td>'
        % (LINE, INK, extra, content)
    )


def html_rows(rows, show_store=True):
    out = []
    for e in rows:
        day, date, time = fmt_when(e.get("start_datetime"))
        price, free = fmt_price(e)
        kind, headline, detail = seat_state(e)
        seat_colour = {"open": OK_INK, "waitlist": FULL_INK}.get(kind, MUTED)
        title = title_for(e)

        when = (
            '<span style="color:%s">%s</span> %s<br>'
            '<span style="color:%s;font-size:13px">%s</span>'
            % (MUTED, esc(day), esc(date), MUTED, esc(time))
        )

        if show_store:
            # Store name over the title, the title carrying the accent so the
            # row reads as tappable.
            where = '<span style="color:%s">%s</span>' % (
                ACCENT if not title else INK, esc(store_of(e)["short"]))
            if title:
                where += (
                    '<br><span style="color:%s;font-size:12px">%s</span>'
                    % (ACCENT, esc(title))
                )
        else:
            # The section heading already names the store, so the cell is just
            # the event. INK rather than a literal white: Gmail's dark mode
            # inverts it to white, and it stays legible on the light background
            # everyone else sees. The trailing arrow carries the "this is a
            # link" signal that the accent colour used to.
            where = (
                '<span style="color:%s">%s&nbsp;&rarr;</span>'
                % (INK, esc(title or store_of(e)["short"]))
            )
        # The whole cell is the link, not just the title -- a single line of
        # 12px text is a poor tap target on a phone.
        where = (
            '<a href="%s" style="text-decoration:none;font-weight:600">%s</a>'
            % (esc(e.get("url") or ""), where)
        )
        seats = (
            '<span style="color:%s;font-weight:700">%s</span><br>'
            '<span style="color:%s;font-size:12px">%s</span>'
            % (seat_colour, esc(headline), MUTED, esc(detail))
        )
        price_html = (
            '<span style="color:%s;font-weight:700">Free</span>' % OK_INK
            if free else esc(price)
        )
        # No white-space:nowrap on any of these. A cell that cannot wrap
        # overflows into its neighbour instead, which is what put "Tue 6:30 PM"
        # on top of the event title on a phone. Wrapping is the graceful
        # failure; overlapping is not.
        out.append(
            "<tr>"
            + _cell(when)
            + _cell(where)
            + _cell(seats)
            + _cell(price_html)
            + "</tr>"
        )
    return "".join(out)


# Pinned so the columns line up across sections; without this each table
# sizes itself and Seats lands somewhere different in each one. Four columns,
# not five: a separate "Sign up" column wrapped to three lines on a phone, so
# the store/event cell carries the link instead.
# Sized so "Tue Sep 22", "23 waiting" and the "PRICE" header each hold one
# line at 360px wide. The store column takes what is left and wraps, since a
# long event name is the one thing here allowed to run to several lines.
COLUMNS = (("When", "23%"), ("Store", "41%"), ("Seats", "22%"),
           ("Price", "14%"))


def html_section(heading, rows, tint, show_store=True):
    if not rows:
        return ""
    # nowrap plus no letter-spacing so "PRICE" stays on one line in a narrow
    # column -- a header is short enough that overflowing beats wrapping.
    head_cells = "".join(
        '<th width="%s" style="text-align:left;padding:7px 7px;font-size:11px;'
        'text-transform:uppercase;color:%s;white-space:nowrap;'
        'border-bottom:1px solid %s">%s</th>'
        % (width, MUTED, LINE, label if show_store or i != 1 else "Event")
        for i, (label, width) in enumerate(COLUMNS)
    )
    return (
        '<tr><td style="padding:22px 0 8px 0">'
        '<div style="font-size:16px;font-weight:700;color:%s">%s (%d)</div>'
        "</td></tr>"
        '<tr><td bgcolor="%s" style="background:%s;border:1px solid %s;'
        'border-radius:8px">'
        '<table role="presentation" width="100%%" cellpadding="0" '
        'cellspacing="0" border="0" style="border-collapse:collapse;'
        'table-layout:fixed">'
        "<tr>%s</tr>%s</table></td></tr>"
        % (INK, esc(heading), len(rows), tint, tint, LINE,
           head_cells, html_rows(rows, show_store))
    )


def render_html(rows, buckets, subject):
    preheader = (
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        'mso-hide:all">%s</div>' % esc(subject)
    )
    sections = "".join(
        html_section("%s - seats open" % label, store_rows, OK_BG,
                     show_store=False)
        for label, store_rows in by_store(buckets["open"])
    ) + (
        html_section("Lottery events", buckets["lottery"], OK_BG)
        + html_section("Waitlist only", buckets["waitlist"], PANEL)
        + html_section("Seat count unknown", buckets["unknown"], PANEL)
    )
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:%s">'
        "%s"
        '<table role="presentation" width="100%%" cellpadding="0" '
        'cellspacing="0" border="0" bgcolor="%s" style="background:%s">'
        '<tr><td align="center" style="padding:20px 12px">'
        # width:100% capped by max-width, NOT width:600px with max-width:100%.
        # The latter resolves its percentage against a containing block whose
        # width depends on this table, so browsers drop the max-width and the
        # right-hand columns fall off a phone screen.
        '<table role="presentation" width="100%%" cellpadding="0" '
        'cellspacing="0" border="0" style="width:100%%;max-width:600px;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,'
        "Helvetica,Arial,sans-serif\">"
        '<tr><td style="font-size:20px;font-weight:700;color:%s;'
        'padding-bottom:2px">%s</td></tr>'
        "%s"
        "</table></td></tr></table></body></html>"
        % (PAGE_BG, preheader, PAGE_BG, PAGE_BG, INK, esc(subject), sections)
    )


# --------------------------------------------------------------------------
# plain text
# --------------------------------------------------------------------------

def text_section(heading, rows, show_store=True):
    if not rows:
        return []
    out = ["", "%s (%d)" % (heading.upper(), len(rows)), "-" * 62]
    for e in rows:
        day, date, time = fmt_when(e.get("start_datetime"))
        price, _ = fmt_price(e)
        _, headline, detail = seat_state(e)
        title = title_for(e)
        out.append(
            "%s  %s  %s"
            % (
                ("%s %s %s" % (day, date, time)).ljust(22),
                (store_of(e)["short"] if show_store else "").ljust(10),
                "%s (%s)" % (headline, detail),
            )
        )
        tail = "    %s" % price
        if title:
            tail += "  -  %s" % title
        out.append(tail)
        # On its own line so mail clients turn it into a link.
        out.append("    %s" % (e.get("url") or ""))
        out.append("")
    return out


def render_text(rows, buckets, subject):
    lines = [subject, "=" * 62]
    for label, store_rows in by_store(buckets["open"]):
        lines += text_section("%s - seats open" % label, store_rows,
                              show_store=False)
    for heading, key in (
        ("Lottery events", "lottery"),
        ("Waitlist only", "waitlist"),
        ("Seat count unknown", "unknown"),
    ):
        lines += text_section(heading, buckets[key])
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# io
# --------------------------------------------------------------------------

def load_json(path, default=None):
    if not os.path.exists(path):
        if default is None:
            sys.exit("missing required file: %s" % path)
        return default
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            # Names the file, because the bare decoder error does not and this
            # surfaces at 3am as a failed workflow run.
            sys.exit("%s is not valid JSON: %s" % (path, exc))


def send_email(subject, html, text):
    user = os.environ.get("SMTP_USER", "").strip()
    # Google shows app passwords as "abcd efgh ijkl mnop". The spaces are
    # presentation only, but smtplib would send them literally and the login
    # would fail with a misleading "username and password not accepted".
    password = re.sub(r"\s+", "", os.environ.get("SMTP_PASSWORD", ""))
    recipients = [
        a.strip() for a in os.environ.get("ALERT_TO", "").split(",") if a.strip()
    ]
    missing = [
        name for name, value in
        (("SMTP_USER", user), ("SMTP_PASSWORD", password), ("ALERT_TO", recipients))
        if not value
    ]
    if missing:
        sys.exit("cannot send, missing secrets: %s" % ", ".join(missing))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.send_message(msg)
    print("sent to %d recipient(s)" % len(recipients))


def save_ledger(path, alerted_ids, new_ids):
    merged = sorted(set(alerted_ids) | set(new_ids))
    payload = {
        "alerted_ids": merged,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
        fh.write("\n")
    print("ledger now holds %d ids" % len(merged))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", default="events.json")
    ap.add_argument("--alerted", default="alerted.json")
    ap.add_argument("--dry-run", metavar="DIR",
                    help="write body.html and body.txt there; send nothing "
                         "and leave the ledger alone")
    ap.add_argument("--force-all", action="store_true",
                    help="ignore the ledger and treat every upcoming event "
                         "as new")
    args = ap.parse_args(argv)

    snapshot = load_json(args.events)
    ledger = load_json(args.alerted, {"alerted_ids": []})
    alerted_ids = set(ledger.get("alerted_ids") or [])

    rows = select_new(snapshot, alerted_ids, args.force_all)
    if not rows:
        print("nothing new to alert (%d ids in ledger)" % len(alerted_ids))
        return 0

    buckets = bucket(rows)
    subject = subject_for(rows, buckets)
    html = render_html(rows, buckets, subject)
    text = render_text(rows, buckets, subject)

    print("%s  [%d open / %d lottery / %d waitlist / %d unknown]  html %.1fkB"
          % (subject, len(buckets["open"]), len(buckets["lottery"]),
             len(buckets["waitlist"]), len(buckets["unknown"]),
             len(html.encode("utf-8")) / 1024.0))

    if args.dry_run:
        os.makedirs(args.dry_run, exist_ok=True)
        for name, body in (("body.html", html), ("body.txt", text)):
            with open(os.path.join(args.dry_run, name), "w",
                      encoding="utf-8") as fh:
                fh.write(body)
        print("wrote %s/body.html and %s/body.txt" % (args.dry_run, args.dry_run))
        return 0

    send_email(subject, html, text)
    # Only after the send returns. A send that succeeds but whose commit fails
    # costs one duplicate next run; the reverse would cost a seat.
    save_ledger(args.alerted, alerted_ids, [e.get("id") for e in rows])
    return 0


if __name__ == "__main__":
    sys.exit(main())
