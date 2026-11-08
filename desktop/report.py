"""Quick text report of one day's activity.

    python desktop/report.py              # today
    python desktop/report.py 2026-11-07   # a specific day

Desktop sessions are the time total. Browser sessions are a breakdown of the
time spent inside the browser, so they're shown separately (never added on top).
"""

import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import db


def fmt(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60:>2}h {m % 60:02d}m"


def totals(rows, day_start, day_end):
    by_app, by_title, total = defaultdict(float), defaultdict(float), 0.0
    for start, end, app, title in rows:
        dur = min(end, day_end) - max(start, day_start)  # clip sessions crossing midnight
        if dur <= 0:
            continue
        by_app[app] += dur
        by_title[(app, title)] += dur
        total += dur
    return by_app, by_title, total


def print_ranked(header, items, total, limit, width=20):
    print(header)
    for key, secs in sorted(items.items(), key=lambda kv: -kv[1])[:limit]:
        if isinstance(key, tuple):
            app, title = key
            print(f"  {fmt(secs)}  {app[:width]:<{width}} {title[:70]}")
        else:
            print(f"  {fmt(secs)}  {secs / total:5.1%}  {key}")
    print()


def print_right_now():
    """Live view: what each source saw most recently (desktop updates every 5s,
    the browser extension every 30s or on tab switch)."""
    conn = db.connect()
    now = datetime.now().timestamp()
    print("\nRight now")
    for source, fresh in (("desktop", 15), ("browser", 45)):
        row = conn.execute(
            "SELECT start_ts, end_ts, app, title FROM sessions WHERE source = ?"
            " ORDER BY end_ts DESC LIMIT 1", (source,)).fetchone()
        if row and now - row[1] <= fresh:
            start, end, app, title = row
            print(f"  {source:<8} {app:<20} {title[:60]}  (for {fmt(end - start).strip()})")
        else:
            print(f"  {source:<8} -")
    conn.close()


def main():
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    day_start = datetime.combine(day, datetime.min.time()).timestamp()
    day_end = (datetime.combine(day, datetime.min.time()) + timedelta(days=1)).timestamp()

    conn = db.connect()
    query = ("SELECT start_ts, end_ts, app, title FROM sessions"
             " WHERE source = ? AND end_ts > ? AND start_ts < ? ORDER BY start_ts")
    desktop = conn.execute(query, ("desktop", day_start, day_end)).fetchall()
    browser = conn.execute(query, ("browser", day_start, day_end)).fetchall()
    pages = conn.execute(
        "SELECT kind, domain, title, author FROM pages WHERE scanned_ts >= ? AND scanned_ts < ?"
        " ORDER BY scanned_ts DESC",
        (day_start, day_end),
    ).fetchall()
    conn.close()

    if not desktop and not browser:
        print(f"No activity recorded for {day}.")
        return

    if day == date.today():
        print_right_now()

    if desktop:
        by_app, by_title, total = totals(desktop, day_start, day_end)
        first = datetime.fromtimestamp(max(desktop[0][0], day_start))
        last = datetime.fromtimestamp(min(desktop[-1][1], day_end))
        print(f"\n{day}   active {fmt(total).strip()}   {first:%H:%M} - {last:%H:%M}"
              f"   {len(desktop)} sessions / window switches\n")
        print_ranked("By app", by_app, total, 50)
        print_ranked("Top 15 windows", by_title, total, 15)

    if browser:
        by_site, by_page, btotal = totals(browser, day_start, day_end)
        print(f"Browser (from extension)   {fmt(btotal).strip()} across {len(by_site)} sites\n")
        print_ranked("By site", by_site, btotal, 15)
        print_ranked("Top 10 pages", by_page, btotal, 10, width=16)

    if pages:
        print(f"Scanned pages ({len(pages)})")
        for kind, domain, title, author in pages[:15]:
            extra = f"  [{author}]" if author else ""
            print(f"  {kind:<8} {domain:<14} {(title or '')[:60]}{extra}")
        print()


if __name__ == "__main__":
    main()
