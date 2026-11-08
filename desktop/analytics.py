"""The numbers behind the dashboard: one call, one JSON-ready dict.

Everything is derived from categorize.categorise(), so the dashboard always agrees
with the report and with the current rules in categories.json.
"""

import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import categorize

FOCUS_GAP = 120          # a focus streak survives interruptions up to this long (seconds)
FOCUS_MIN = 5 * 60       # shorter productive stretches aren't counted as streaks
DEEP_FOCUS = 25 * 60     # "deep focus" = a streak at least this long
BLOCK_MERGE_GAP = 60     # timeline: same-category blocks closer than this are drawn as one
TOPIC_MIN_PAGE_SECONDS = 30  # pages shorter than this don't feed "top topics"
LIVE = {"desktop": 15, "browser": 45}  # how fresh a row must be to count as "right now"

# Words that say nothing about a topic (plus site names that appear in every title).
STOPWORDS = set("""
a an and are as at be been but by can could did do does for from get got had has have how i if in
into is it its just me more most my new no not now of off on one or our out so than that the their
them then there these they this to too up us vs was we what when where which who why will with you
your yours about after all also any best both each every here only over own same some such very
made make makes making thing things mostly like using use used way want need really much many
first last next back still even well
youtube google brave search gmail reddit medium github linkedin com www http https html official
video videos watch home page inbox untitled new tab
""".split())
SHORT_TERMS = {"c#", "c++", "ai", "ml", "ui", "ux", "os", "go", "r"}
TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]")


def _midnight(d: date) -> float:
    return datetime.combine(d, datetime.min.time()).timestamp()


def _pieces(s: float, e: float, step: str):
    """Split [s, e) at hour or day boundaries -> (datetime_of_piece_start, seconds)."""
    t = s
    while t < e:
        dt = datetime.fromtimestamp(t)
        if step == "hour":
            nxt = dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            nxt = datetime.combine(dt.date() + timedelta(days=1), datetime.min.time())
        piece_end = min(e, nxt.timestamp())
        yield dt, piece_end - t
        t = piece_end


def _terms(text: str):
    for tok in TOKEN.findall((text or "").lower()):
        tok = tok.strip(".")
        if tok in STOPWORDS or tok.isdigit():
            continue
        if len(tok) < 3 and tok not in SHORT_TERMS:
            continue
        yield tok


def _focus(items, productive):
    """Productive streaks, allowing short interruptions (<= FOCUS_GAP)."""
    streaks, cur = [], None
    for it in items:
        if it.category not in productive:
            continue  # interruptions only matter through the gap they leave
        if cur and it.start - cur["end"] <= FOCUS_GAP:
            cur["end"] = max(cur["end"], it.end)
        else:
            if cur:
                streaks.append(cur)
            cur = {"start": it.start, "end": it.end, "cats": Counter()}
        cur["cats"][it.category] += it.seconds
    if cur:
        streaks.append(cur)
    out = [
        {"start": s["start"], "end": s["end"], "seconds": round(s["end"] - s["start"]),
         "category": s["cats"].most_common(1)[0][0]}
        for s in streaks if s["end"] - s["start"] >= FOCUS_MIN
    ]
    deep = [s for s in out if s["seconds"] >= DEEP_FOCUS]
    return {
        "longest": max(out, key=lambda s: s["seconds"], default=None),
        "streaks": sorted(out, key=lambda s: -s["seconds"])[:10],
        "count": len(out),
        "deep_count": len(deep),
        "deep_seconds": sum(s["seconds"] for s in deep),
        "min_seconds": FOCUS_MIN,
        "deep_threshold": DEEP_FOCUS,
    }


SWITCH_MIN_SECONDS = 5  # flicking past a window (alt-tab) isn't a context switch


def _switches(items):
    """App/site changes between consecutive stretches of activity (not across breaks)."""
    count, prev, prev_end = 0, None, None
    for it in items:
        if it.seconds < SWITCH_MIN_SECONDS:
            continue
        key = it.app.lower()
        if prev is not None and key != prev and it.start - prev_end <= FOCUS_GAP:
            count += 1
        prev, prev_end = key, it.end
    return count


def _timeline(items, days):
    """Per day: category blocks for a ribbon chart, with the main app/site of each block."""
    per_day = {d.isoformat(): [] for d in days}
    for it in items:
        for dt, secs in _pieces(it.start, it.end, "day"):
            key = dt.date().isoformat()
            if key not in per_day:
                continue
            s = dt.timestamp()
            e = s + secs
            blocks = per_day[key]
            last = blocks[-1] if blocks else None
            if last and last["category"] == it.category and s - last["end"] <= BLOCK_MERGE_GAP:
                last["end"] = e
            else:
                last = {"start": s, "end": e, "category": it.category, "apps": Counter()}
                blocks.append(last)
            last["apps"][it.app] += secs
    return [
        {"date": d, "blocks": [
            {"start": round(b["start"]), "end": round(b["end"]), "category": b["category"],
             "label": b["apps"].most_common(1)[0][0]}
            for b in blocks]}
        for d, blocks in per_day.items()
    ]


def _right_now(conn, rules):
    now = time.time()
    out = {}
    for source, fresh in LIVE.items():
        row = conn.execute(
            "SELECT start_ts, end_ts, app, title, url FROM sessions WHERE source = ?"
            " ORDER BY end_ts DESC LIMIT 1", (source,)).fetchone()
        if not row or now - row[1] > fresh:
            out[source] = None
            continue
        start, end, app, title, url = row
        if source == "browser":
            page = categorize._load_pages(conn, [url]).get(url)
            cat = rules.classify_browser(app, title, page)[0]
        else:
            cat = rules.classify_desktop(app, title)[0]
        out[source] = {"app": app, "title": title, "category": cat, "seconds": round(end - start)}
    return out


def summary(conn, start_day: date, end_day: date, rules=None) -> dict:
    rules = rules or categorize.Rules()
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    days = [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]
    start, end = _midnight(start_day), _midnight(end_day + timedelta(days=1))
    items = sorted(categorize.categorise(conn, start, end, rules), key=lambda it: it.start)
    productive = rules.productive

    by_cat = defaultdict(float)
    per_day = {d.isoformat(): defaultdict(float) for d in days}
    by_hour = [defaultdict(float) for _ in range(24)]  # hour -> category -> seconds
    apps = defaultdict(lambda: {"seconds": 0.0, "cats": Counter(), "kind": "app"})
    pages = defaultdict(lambda: {"seconds": 0.0, "title": "", "domain": "", "category": ""})
    unmatched, unsure = defaultdict(float), defaultdict(float)

    for it in items:
        secs = it.seconds
        by_cat[it.category] += secs
        for dt, part in _pieces(it.start, it.end, "hour"):
            key = dt.date().isoformat()
            if key in per_day:
                per_day[key][it.category] += part
            by_hour[dt.hour][it.category] += part
        a = apps[(it.app, it.source)]
        a["seconds"] += secs
        a["cats"][it.category] += secs
        a["kind"] = "site" if it.source == "browser" else "app"
        if it.url:
            p = pages[it.url]
            p["seconds"] += secs
            p["title"], p["domain"], p["category"] = it.title, it.app, it.category
        if not it.matched:
            unmatched[("site" if it.source == "browser" else "app", it.app)] += secs
        elif it.category == rules.fallback["content"]:
            unsure[(it.app, it.title)] += secs

    active = sum(by_cat.values())
    prod_secs = sum(v for k, v in by_cat.items() if k in productive)
    switches = _switches(items)

    # Pages: the same page reached via slightly different URLs (tracking/query params)
    # becomes one row, linked to the URL you spent most time on.
    merged = {}
    for url, p in pages.items():
        m = merged.setdefault((p["domain"], p["title"]), {**p, "seconds": 0.0, "url": url, "best": -1.0, "urls": []})
        m["seconds"] += p["seconds"]
        m["urls"].append(url)
        if p["seconds"] > m["best"]:
            m["url"], m["best"] = url, p["seconds"]
    top_pages = sorted(merged.values(), key=lambda m: -m["seconds"])[:25]
    urls = [u for m in top_pages for u in m["urls"]]
    scanned = categorize._load_pages(conn, urls)
    scroll = {}
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        scroll.update(conn.execute(
            f"SELECT url, MAX(scroll_pct) FROM sessions WHERE source = 'browser' AND url IN"
            f" ({','.join('?' * len(chunk))}) AND end_ts > ? AND start_ts < ? GROUP BY url",
            (*chunk, start, end)).fetchall())

    # Searches + topics.
    search_rows = conn.execute(
        "SELECT ts, engine, domain, query FROM searches WHERE ts >= ? AND ts < ? ORDER BY ts DESC",
        (start, end)).fetchall()
    topic_score, topic_searches, topic_pages = Counter(), Counter(), Counter()
    for _, _, _, query in search_rows:
        for term in set(_terms(query)):
            topic_score[term] += 3
            topic_searches[term] += 1
    for url, p in pages.items():
        if p["seconds"] < TOPIC_MIN_PAGE_SECONDS:
            continue  # flicked past it; not something you actually read
        title = (scanned.get(url) or {}).get("title") or p["title"]
        weight = 2 if p["seconds"] >= 300 else 1
        for term in set(_terms(title)):
            topic_score[term] += weight
            topic_pages[term] += 1

    return {
        "range": {"start": start_day.isoformat(), "end": end_day.isoformat(), "days": len(days),
                  "includes_today": start_day <= date.today() <= end_day},
        "generated_at": time.time(),
        "categories": rules.categories,
        "productive": [c for c in rules.categories if c in productive],
        "totals": {
            "active": round(active),
            "productive": round(prod_secs),
            "by_category": {k: round(v) for k, v in by_cat.items()},
        },
        "per_day": [
            {"date": d, "active": round(sum(c.values())), "by_category": {k: round(v) for k, v in c.items()}}
            for d, c in per_day.items()
        ],
        "by_hour": [{"hour": h, "by_category": {k: round(v) for k, v in cats.items()}}
                    for h, cats in enumerate(by_hour)],
        "focus": _focus(items, productive),
        "switches": {"count": switches,
                     "per_hour": round(switches / (active / 3600), 1) if active >= 600 else None},
        "timeline": _timeline(items, days),
        "top_apps": [
            {"name": name, "kind": a["kind"], "seconds": round(a["seconds"]),
             "category": a["cats"].most_common(1)[0][0]}
            for (name, _), a in sorted(apps.items(), key=lambda kv: -kv[1]["seconds"])[:15]
        ],
        "pages": [
            {"url": m["url"], "domain": m["domain"], "seconds": round(m["seconds"]), "category": m["category"],
             "title": next(((scanned.get(u) or {}).get("title") for u in m["urls"] if (scanned.get(u) or {}).get("title")),
                           m["title"]),
             "scroll": max((scroll[u] for u in m["urls"] if scroll.get(u) is not None), default=None),
             "has_text": any((scanned.get(u) or {}).get("body") for u in m["urls"])}
            for m in top_pages
        ],
        "topics": [
            {"term": t, "score": s, "searches": topic_searches[t], "pages": topic_pages[t]}
            for t, s in topic_score.most_common(20)
        ],
        "searches": [
            {"ts": ts, "engine": engine, "domain": domain, "query": query,
             "category": rules.classify_text([(query, 3)]) or ""}
            for ts, engine, domain, query in search_rows[:200]
        ],
        "search_count": len(search_rows),
        "unmatched": [{"kind": k, "name": n, "seconds": round(s)}
                      for (k, n), s in sorted(unmatched.items(), key=lambda kv: -kv[1])[:10]],
        "unsure": [{"site": site, "title": title, "seconds": round(s)}
                   for (site, title), s in sorted(unsure.items(), key=lambda kv: -kv[1])[:10]],
        "now": _right_now(conn, rules) if start_day <= date.today() <= end_day else None,
    }
