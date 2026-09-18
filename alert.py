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
from collections import Counter
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

# Every San Jose tournament repeats this; it is not per-event information.
# It appears once in the footer instead of under all 31 rows.
FOOTER_NOTE = (
    "Bring your own deck. If you are not at the shop by the start time your "
    "seat goes to someone else."
)


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


def seat_state(event):
    """-> (kind, headline, detail). kind is 'open', 'waitlist' or 'unknown'.

    Derived from max_join_count - count_applicants the way index.html does.
    seats_left in the snapshot goes as low as -209, so it is never dropped
    into a sentence as-is.
    """
    cap = event.get("max_join_count")
    applied = event.get("count_applicants")
    if applied is None or cap is None:
        return ("unknown", "Seats unknown", "count unavailable")
    left = cap - applied
    if left > 0:
        return (
            "open",
            "%d seat%s left" % (left, "" if left == 1 else "s"),
            "%d of %d taken" % (applied, cap),
        )
    deep = applied - cap
    return (
        "waitlist",
        "Waitlist" + (" ~%d deep" % deep if deep > 0 else ""),
        "%d applied / %d seats" % (applied, cap),
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


def common_titles(all_events):
    """The most-repeated title per store. 37 of 55 events share one title that
    carries no information, so a row shows its title only when it differs from
    its store's usual one -- which is exactly what surfaces the odd events."""
    by_store = {}
    for e in all_events:
        by_store.setdefault(e.get("organizer_id"), []).append(
            e.get("event_series_title") or ""
        )
    return {
        oid: Counter(titles).most_common(1)[0][0]
        for oid, titles in by_store.items() if titles
    }


def title_for(event, usual):
    title = event.get("event_series_title") or ""
    if title and title != usual.get(event.get("organizer_id")):
        return title
    return ""


def date_span(events):
    """'Oct 4 - Dec 30' across a set of events."""
    keys = sorted(str(e.get("start_datetime") or "") for e in events)
    first = fmt_when(keys[0])[1]
    last = fmt_when(keys[-1])[1]
    return first if first == last else "%s - %s" % (first, last)


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def select_new(snapshot, alerted_ids, force_all):
    now_key = pacific_now_key()
    rows = [
        e for e in snapshot.get("events", [])
        if is_upcoming(e, now_key)
        and not e.get("is_canceled")
        and (force_all or e.get("id") not in alerted_ids)
    ]
    rows.sort(key=lambda e: str(e.get("start_datetime") or ""))
    return rows


def bucket(rows):
    out = {"open": [], "waitlist": [], "unknown": []}
    for e in rows:
        out[seat_state(e)[0]].append(e)
    return out


def subject_for(rows, buckets):
    n = len(rows)
    noun = "event" if n == 1 else "events"
    # A whole season drops at once -- 37 events spanning three months landed in
    # a single run. Leading with the span says "schedule", which is what it is.
    # A handful of events is the case where the seat count is the headline.
    if n > 10:
        return "%d new OPCG %s (%s)" % (n, noun, date_span(rows))
    open_n = len(buckets["open"])
    if open_n == 0:
        return "%d new OPCG %s - waitlist only" % (n, noun)
    return "%d new OPCG %s, %d with seats open" % (n, noun, open_n)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def _cell(content, extra=""):
    return (
        '<td style="padding:9px 10px;border-bottom:1px solid %s;'
        'vertical-align:top;font-size:14px;color:%s;%s">%s</td>'
        % (LINE, INK, extra, content)
    )


def html_rows(rows, usual):
    out = []
    for e in rows:
        day, date, time = fmt_when(e.get("start_datetime"))
        price, free = fmt_price(e)
        kind, headline, detail = seat_state(e)
        seat_colour = {"open": OK_INK, "waitlist": FULL_INK}.get(kind, MUTED)
        title = title_for(e, usual)

        when = (
            '<span style="color:%s">%s</span> %s<br>'
            '<span style="color:%s;font-size:13px">%s</span>'
            % (MUTED, esc(day), esc(date), MUTED, esc(time))
        )
        where = esc(store_of(e)["short"])
        if title:
            where += (
                '<br><span style="color:%s;font-size:12px">%s</span>'
                % (MUTED, esc(title))
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
        link = (
            '<a href="%s" style="color:%s;font-weight:700;'
            'text-decoration:none">Sign up &rarr;</a>'
            % (esc(e.get("url") or ""), ACCENT)
        )

        out.append(
            "<tr>"
            + _cell(when, "white-space:nowrap;")
            + _cell(where)
            + _cell(seats, "white-space:nowrap;")
            + _cell(price_html, "white-space:nowrap;")
            + _cell(link, "white-space:nowrap;")
            + "</tr>"
        )
    return "".join(out)


# Pinned so the columns line up across sections; without this each table
# sizes itself and Seats lands somewhere different in each one.
COLUMNS = (("When", "17%"), ("Store", "30%"), ("Seats", "26%"),
           ("Price", "11%"), ("", "16%"))


def html_section(heading, rows, usual, tint):
    if not rows:
        return ""
    head_cells = "".join(
        '<th width="%s" style="text-align:left;padding:7px 10px;font-size:11px;'
        'text-transform:uppercase;letter-spacing:.05em;color:%s;'
        'border-bottom:1px solid %s">%s</th>' % (width, MUTED, LINE, label)
        for label, width in COLUMNS
    )
    return (
        '<tr><td style="padding:22px 0 8px 0">'
        '<div style="font-size:16px;font-weight:700;color:%s">%s (%d)</div>'
        "</td></tr>"
        '<tr><td bgcolor="%s" style="background:%s;border:1px solid %s;'
        'border-radius:8px">'
        '<table role="presentation" width="100%%" cellpadding="0" '
        'cellspacing="0" border="0" style="border-collapse:collapse">'
        "<tr>%s</tr>%s</table></td></tr>"
        % (INK, esc(heading), len(rows), tint, tint, LINE,
           head_cells, html_rows(rows, usual))
    )


def render_html(rows, buckets, usual, subject):
    preheader = (
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        'mso-hide:all">%s</div>' % esc(subject)
    )
    sections = (
        html_section("Seats still open", buckets["open"], usual, OK_BG)
        + html_section("Waitlist only", buckets["waitlist"], usual, PANEL)
        + html_section("Seat count unknown", buckets["unknown"], usual, PANEL)
    )
    footer = (
        '<tr><td style="padding:26px 0 0 0;font-size:12px;line-height:1.6;'
        'color:%s;border-top:1px solid %s">'
        "New means this watcher had not seen the event before. "
        "Seats are first-come-first-served, and the counts above move fast - "
        "check the signup page before counting on one.<br>%s"
        "</td></tr>" % (MUTED, LINE, esc(FOOTER_NOTE))
    )
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:%s">'
        "%s"
        '<table role="presentation" width="100%%" cellpadding="0" '
        'cellspacing="0" border="0" bgcolor="%s" style="background:%s">'
        '<tr><td align="center" style="padding:20px 12px">'
        '<table role="presentation" width="600" cellpadding="0" '
        'cellspacing="0" border="0" style="width:600px;max-width:100%%;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,'
        "Helvetica,Arial,sans-serif\">"
        '<tr><td style="font-size:20px;font-weight:700;color:%s;'
        'padding-bottom:2px">%s</td></tr>'
        "%s%s"
        "</table></td></tr></table></body></html>"
        % (PAGE_BG, preheader, PAGE_BG, PAGE_BG, INK, esc(subject),
           sections, footer)
    )


# --------------------------------------------------------------------------
# plain text
# --------------------------------------------------------------------------

def text_section(heading, rows, usual):
    if not rows:
        return []
    out = ["", "%s (%d)" % (heading.upper(), len(rows)), "-" * 62]
    for e in rows:
        day, date, time = fmt_when(e.get("start_datetime"))
        price, _ = fmt_price(e)
        _, headline, detail = seat_state(e)
        title = title_for(e, usual)
        out.append(
            "%s  %s  %s"
            % (
                ("%s %s %s" % (day, date, time)).ljust(22),
                store_of(e)["short"].ljust(10),
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


def render_text(rows, buckets, usual, subject):
    lines = [subject, "=" * 62]
    for heading, key in (
        ("Seats still open", "open"),
        ("Waitlist only", "waitlist"),
        ("Seat count unknown", "unknown"),
    ):
        lines += text_section(heading, buckets[key], usual)
    lines += [
        "-" * 62,
        "New means this watcher had not seen the event before.",
        "Seats are first-come-first-served and the counts above move fast;",
        "check the signup page before counting on one.",
        FOOTER_NOTE,
    ]
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

    usual = common_titles(snapshot.get("events", []))
    buckets = bucket(rows)
    subject = subject_for(rows, buckets)
    html = render_html(rows, buckets, usual, subject)
    text = render_text(rows, buckets, usual, subject)

    print("%s  [%d open / %d waitlist / %d unknown]  html %.1fkB"
          % (subject, len(buckets["open"]), len(buckets["waitlist"]),
             len(buckets["unknown"]), len(html.encode("utf-8")) / 1024.0))

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
