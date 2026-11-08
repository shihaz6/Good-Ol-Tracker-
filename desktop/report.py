"""Quick text report of one day's activity.

    python desktop/report.py              # today
    python desktop/report.py 2026-11-07   # a specific day

Desktop sessions are the time total. Browser sessions are a breakdown of the
time spent inside the browser, so they're shown separately (never added on top).
Categories come from categories.json (see desktop/categorize.py).
"""

import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import categorize
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


def print_right_now(conn, rules):
    """Live view: what each source saw most recently (desktop updates every 5s,
    the browser extension every 30s or on tab switch)."""
    now = datetime.now().timestamp()
    print("\nRight now")
    for source, fresh in (("desktop", 15), ("browser", 45)):
        row = conn.execute(
            "SELECT start_ts, end_ts, app, title, url FROM sessions WHERE source = ?"
            " ORDER BY end_ts DESC LIMIT 1", (source,)).fetchone()
        if not row or now - row[1] > fresh:
            print(f"  {source:<8} -")
            continue
        start, end, app, title, url = row
        cat = ""
        if rules:
            if source == "browser":
                page = categorize._load_pages(conn, [url]).get(url)
                cat = rules.classify_browser(app, title, page, url)[0]
            else:
                cat = rules.classify_desktop(app, title)[0]
        print(f"  {source:<8} {cat:<13} {app[:20]:<20} {title[:50]}  (for {fmt(end - start).strip()})")


def print_categories(items, rules):
    total = sum(it.seconds for it in items)
    if not total:
        return
    by_cat = defaultdict(float)
    unmatched = defaultdict(float)   # (kind, name) -> seconds
    unsure = defaultdict(float)      # (site, title) -> seconds
    for it in items:
        by_cat[it.category] += it.seconds
        if not it.matched:
            unmatched[("site" if it.source == "browser" else "app", it.app)] += it.seconds
        elif it.category == rules.fallback["content"]:
            unsure[(it.app, it.title)] += it.seconds

    print("By category")
    for cat, secs in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        share = secs / total
        print(f"  {fmt(secs)}  {share:5.1%}  {cat:<14} {'█' * max(1, round(share * 30))}")
    print()

    if unmatched:
        print("Not categorised yet  (add these to \"apps\" / \"sites\" in categories.json)")
        for (kind, name), secs in sorted(unmatched.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {fmt(secs)}  {kind:<4}  {name}")
        print()
    if unsure:
        print("Unsure  (add a channel rule or keywords in categories.json)")
        for (site, title), secs in sorted(unsure.items(), key=lambda kv: -kv[1])[:5]:
            print(f"  {fmt(secs)}  {site[:16]:<16} {title[:60]}")
        print()


def main():
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    day_start = datetime.combine(day, datetime.min.time()).timestamp()
    day_end = (datetime.combine(day, datetime.min.time()) + timedelta(days=1)).timestamp()

    conn = db.connect()
    try:
        rules = categorize.Rules()
    except (OSError, ValueError) as exc:  # ValueError includes JSON syntax errors
        print(f"\n!! categories.json problem, skipping categories: {exc}")
        rules = None

    query = ("SELECT start_ts, end_ts, app, title FROM sessions"
             " WHERE source = ? AND end_ts > ? AND start_ts < ? ORDER BY start_ts")
    desktop = conn.execute(query, ("desktop", day_start, day_end)).fetchall()
    browser = conn.execute(query, ("browser", day_start, day_end)).fetchall()
    pages = conn.execute(
        "SELECT kind, domain, title, author, length(COALESCE(body, '')) FROM pages"
        " WHERE scanned_ts >= ? AND scanned_ts < ? ORDER BY scanned_ts DESC",
        (day_start, day_end),
    ).fetchall()
    scroll = {
        (app, title): pct for app, title, pct in conn.execute(
            "SELECT app, title, MAX(scroll_pct) FROM sessions WHERE source = 'browser'"
            " AND end_ts > ? AND start_ts < ? AND scroll_pct IS NOT NULL GROUP BY app, title",
            (day_start, day_end))
    }
    searches = conn.execute(
        "SELECT ts, engine, query FROM searches WHERE ts >= ? AND ts < ? ORDER BY ts DESC",
        (day_start, day_end),
    ).fetchall()
    items = categorize.categorise(conn, day_start, day_end, rules) if rules else []

    if day == date.today():
        print_right_now(conn, rules)
    conn.close()

    if not desktop and not browser:
        print(f"No activity recorded for {day}.")
        return

    if desktop:
        by_app, by_title, total = totals(desktop, day_start, day_end)
        first = datetime.fromtimestamp(max(desktop[0][0], day_start))
        last = datetime.fromtimestamp(min(desktop[-1][1], day_end))
        print(f"\n{day}   active {fmt(total).strip()}   {first:%H:%M} - {last:%H:%M}"
              f"   {len(desktop)} sessions / window switches\n")
        if rules:
            print_categories(items, rules)
        print_ranked("By app", by_app, total, 50)
        print_ranked("Top 15 windows", by_title, total, 15)

    if browser:
        by_site, by_page, btotal = totals(browser, day_start, day_end)
        print(f"Browser (from extension)   {fmt(btotal).strip()} across {len(by_site)} sites\n")
        print_ranked("By site", by_site, btotal, 15)
        print("Top 10 pages                         (read = deepest scroll)")
        for (site, title), secs in sorted(by_page.items(), key=lambda kv: -kv[1])[:10]:
            pct = scroll.get((site, title))
            read = f"{pct:3.0f}%" if pct is not None else "   -"
            print(f"  {fmt(secs)}  {read}  {site[:16]:<16} {title[:60]}")
        print()

    if searches:
        print(f"Searches ({len(searches)})")
        for ts, engine, query in searches[:30]:
            cat = (rules.classify_text([(query, 3)]) if rules else None) or "-"
            print(f"  {datetime.fromtimestamp(ts):%H:%M}  {cat:<13} {engine[:16]:<16} {query[:60]}")
        if len(searches) > 30:
            print(f"  ... and {len(searches) - 30} more")
        print()

    if pages:
        with_text = sum(1 for p in pages if p[4])
        print(f"Pages read ({len(pages)}, {with_text} with full text)")
        for kind, domain, title, author, chars in pages[:15]:
            extra = f"  [{author}]" if author else ""
            print(f"  {kind:<8} {domain[:18]:<18} {chars:>5} ch  {(title or '')[:50]}{extra}")
        if len(pages) > 15:
            print(f"  ... and {len(pages) - 15} more")
        print()


if __name__ == "__main__":
    main()
