#!/usr/bin/env python3
"""One-off: list every store near San Jose with upcoming OPCG events.

Pages through Bandai's event list for the whole US with no organizer_id,
keeps events in South Bay cities, and groups them by organizer. Then asks the
per-event endpoint about one event per store and prints any field that looks
like a website or social link. Stdlib only, like alert.py.
"""

import json
import sys
import time
import urllib.error
import urllib.request

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.bandai-tcg-plus.com/",
}
API = "https://api.bandai-tcg-plus.com/api/user"
LIST = API + "/event/list?game_title_id=4&country_code[]=US&limit=100&offset=%d&order=1"

CITIES = {
    "san jose", "santa clara", "sunnyvale", "cupertino", "campbell", "saratoga",
    "los gatos", "monte sereno", "milpitas", "mountain view", "los altos",
    "palo alto", "fremont", "newark", "union city", "morgan hill", "gilroy",
    "menlo park", "redwood city", "east palo alto",
}
LINK_HINTS = ("url", "sns", "discord", "twitter", "insta", "web", "site",
              "facebook", "link", "homepage", "x_")


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, "%s.%s" % (path, k) if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, "%s[%d]" % (path, i))
    else:
        yield path, obj


def fetch_pages(first, count):
    """Pages [first, first+count) of the US list, South Bay events only.

    The full list is ~47k events at ~12s a page, so one job cannot read it
    before timing out; the workflow runs this as parallel shards instead."""
    near, cities = [], {}
    for n in range(first, first + count):
        page = get(LIST % (n * 100))["success"]
        batch = page.get("event_list") or []
        print("page %d: %d events, total %s, first start %s"
              % (n, len(batch), page.get("total"),
                 batch[0].get("start_datetime") if batch else "-"), flush=True)
        for e in batch:
            city = norm_city(e.get("city_code"))
            cities[city] = cities.get(city, 0) + 1
            if city in CITIES:
                near.append(e)
        if len(batch) < 100:
            break
        time.sleep(0.3)
    return near, cities


def norm_city(value):
    return str(value or "").strip().lower().replace("\u00e9", "e")


def combine(paths):
    events, seen = [], set()
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for e in json.load(fh):
                if e.get("id") not in seen:
                    seen.add(e.get("id"))
                    events.append(e)
    print("%d South Bay events across %d shards" % (len(events), len(paths)))

    stores = {}
    for e in events:
        s = stores.setdefault(e.get("organizer_id"), {
            "organizer_id": e.get("organizer_id"),
            "name": e.get("organizer_name"),
            "address": "%s, %s" % (e.get("street_address") or "",
                                   e.get("city_code") or ""),
            "events": 0, "titles": {}, "next": None, "sample_event": e.get("id"),
        })
        s["events"] += 1
        t = e.get("event_series_title") or ""
        s["titles"][t] = s["titles"].get(t, 0) + 1
        start = str(e.get("start_datetime") or "")
        if s["next"] is None or start < s["next"]:
            s["next"] = start

    for s in stores.values():
        try:
            detail = get("%s/event/%s" % (API, s["sample_event"]))
        except urllib.error.URLError as exc:
            s["links"] = {"error": str(exc)}
            continue
        s["links"] = {
            p: v for p, v in walk(detail)
            if v not in (None, "", [], {})
            and any(h in p.lower().rsplit(".", 1)[-1] for h in LINK_HINTS)
        }
        s["organizer_detail"] = {
            p: v for p, v in walk(detail) if "organizer" in p.lower()
        }
        time.sleep(0.3)

    rows = sorted(stores.values(), key=lambda s: (-s["events"], s["name"] or ""))
    print("\n%d South Bay stores with upcoming OPCG events\n" % len(rows))
    for s in rows:
        print("=" * 70)
        print("%s  (organizer_id %s)" % (s["name"], s["organizer_id"]))
        print("  %s" % s["address"])
        print("  %d upcoming events, next %s" % (s["events"], s["next"]))
        for t, n in sorted(s["titles"].items(), key=lambda kv: -kv[1])[:5]:
            print("    %3d x %s" % (n, t))
        for p, v in s.get("links", {}).items():
            print("  link %s = %s" % (p, v))
        for p, v in s.get("organizer_detail", {}).items():
            print("  org  %s = %s" % (p, v))
    with open("stores.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)


def main(argv):
    if argv[:1] == ["shard"]:
        first, count, out = int(argv[1]), int(argv[2]), argv[3]
        near, cities = fetch_pages(first, count)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(near, fh)
        print("%d South Bay events in this shard" % len(near))
        # Any spelling of a nearby city the CITIES set misses shows up here.
        bay = sorted(c for c in cities
                     if any(w in c for w in ("jose", "clara", "sunny", "cuper",
                                             "campb", "gatos", "milp", "alto",
                                             "mountain", "fremont")))
        print("bay-ish city spellings seen:", bay)
        return 0
    if argv[:1] == ["combine"]:
        combine(argv[1:])
        return 0
    sys.exit("usage: find_stores.py shard FIRST COUNT OUT | combine SHARD...")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
