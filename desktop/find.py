"""Search what you've read: pages (title, channel, description, headings, text) and your
past searches.

Two ways to match, combined:
  * words   - SQLite full-text index (pages_fts), works offline
  * meaning - the query is embedded with Voyage and compared with the page vectors the AI
              categoriser already stored (page_ai), so "how to split a table in a database"
              finds a page titled "Database normalization explained".
What leaves the PC: only the search text you type, sent to Voyage (if "meaning" is on).
Page text is kept 60 days; after that a page is still found by its title and channel.
"""

import math
import operator
import re
import threading
from collections import OrderedDict

import ai_categorize
import keys

MAX_RESULTS = 20
# voyage-3.5-lite, checked on real data (2026-11-08): unrelated pages score 0.33-0.38 against a
# query; related ones 0.45+. A match must clear the floor and be near the best match.
MIN_MEANING = 0.42
MEANING_WINDOW = 0.10
RRF_K = 60           # reciprocal-rank fusion constant
HL_START, HL_END = "\u0002", "\u0003"  # highlight markers in snippets (rendered by the dashboard)

_vec_lock = threading.Lock()
_page_vecs = {}            # url -> (updated_ts, normalised vector)
_query_vecs = OrderedDict()  # (model, query) -> normalised vector (small LRU)


STOP_WORDS = set("""a an and are as at be by do does for from how i in is it me my of on or so that the
this to was what when where which who why with you your about into vs""".split())


def _tokens(q: str):
    words = re.findall(r"\w+", q.lower())
    kept = [w for w in words if w not in STOP_WORDS]
    return (kept or words)[:12]  # a query of only filler words still searches for them


def _fts_query(tokens, any_word=False):
    # Quote every token (no FTS syntax injection) and allow prefix matches on longer ones.
    parts = [f'"{t}"' + ("*" if len(t) >= 3 else "") for t in tokens]
    return (" OR " if any_word else " ").join(parts)


def _word_matches(conn, tokens, limit=50):
    if not tokens:
        return []
    sql = (f"SELECT p.url, snippet(pages_fts, -1, '{HL_START}', '{HL_END}', '…', 14),"
           f" bm25(pages_fts, 10.0, 4.0, 2.0, 3.0, 1.0)"
           f" FROM pages_fts JOIN pages p ON p.rowid = pages_fts.rowid"
           f" WHERE pages_fts MATCH ? ORDER BY 3 LIMIT ?")
    rows = conn.execute(sql, (_fts_query(tokens), limit)).fetchall()
    if not rows and len(tokens) > 1:  # nothing has every word: settle for any of them
        rows = conn.execute(sql, (_fts_query(tokens, any_word=True), limit)).fetchall()
    return [(url, snip) for url, snip, _ in rows]


def _normalise(vec):
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def _page_vectors(conn, model):
    rows = conn.execute(
        "SELECT url, updated_ts, vector FROM page_ai WHERE model = ? AND length(vector) > 0", (model,)).fetchall()
    with _vec_lock:
        live = set()
        for url, ts, blob in rows:
            live.add(url)
            cached = _page_vecs.get(url)
            if not cached or cached[0] != ts:
                _page_vecs[url] = (ts, _normalise(ai_categorize.unpack(blob)))
        for url in list(_page_vecs):
            if url not in live:
                del _page_vecs[url]
        return {u: v for u, (_, v) in _page_vecs.items()}


def _query_vector(query, key, model, embed_fn):
    k = (model, query.lower().strip())
    with _vec_lock:
        if k in _query_vecs:
            _query_vecs.move_to_end(k)
            return _query_vecs[k]
    _, vecs = embed_fn([query], key, model)
    vec = _normalise(vecs[0])
    with _vec_lock:
        _query_vecs[k] = vec
        while len(_query_vecs) > 100:
            _query_vecs.popitem(last=False)
    return vec


def _meaning_matches(conn, query, embed_fn, limit=50):
    """-> (matches [(url, score)], note). note explains why meaning search was skipped."""
    key = keys.load_key("voyage")
    if not key:
        return [], "meaning search is off: no Voyage key"
    row = conn.execute("SELECT model, COUNT(*) FROM page_ai WHERE length(vector) > 0"
                       " GROUP BY model ORDER BY 2 DESC LIMIT 1").fetchone()
    if not row:
        return [], "no pages have been processed by the AI yet"
    model = row[0]
    try:
        qv = _query_vector(query, key, model, embed_fn)
    except ai_categorize.AIError as exc:
        return [], f"meaning search unavailable: {exc}"
    scored = [(url, sum(map(operator.mul, qv, v))) for url, v in _page_vectors(conn, model).items()]
    best = max((s for _, s in scored), default=0)
    scored = [s for s in scored if s[1] >= max(MIN_MEANING, best - MEANING_WINDOW)]
    scored.sort(key=lambda s: -s[1])
    return scored[:limit], None


def _title_matches(conn, tokens, limit=50):
    """Pages that were visited but never scanned (blocked sites, before scanning was on):
    match on the tab title."""
    if not tokens:
        return []
    where = " AND ".join("s.title LIKE ?" for _ in tokens)
    rows = conn.execute(
        f"SELECT s.url, s.title, MAX(s.end_ts) FROM sessions s LEFT JOIN pages p ON p.url = s.url"
        f" WHERE s.source = 'browser' AND p.url IS NULL AND {where}"
        f" GROUP BY s.url ORDER BY 3 DESC LIMIT 500", [f"%{t}%" for t in tokens]).fetchall()
    return [url for url, title, _ in rows if all(t in (title or "").lower() for t in tokens)][:limit]


def _page_details(conn, urls):
    out = {}
    urls = list(urls)
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        marks = ",".join("?" * len(chunk))
        # Time spent + the latest tab title (SQLite takes bare columns from the MAX() row).
        for url, domain, title, secs, last, scroll in conn.execute(
                f"SELECT url, app, title, SUM(end_ts - start_ts), MAX(end_ts), MAX(scroll_pct) FROM sessions"
                f" WHERE source = 'browser' AND url IN ({marks}) GROUP BY url", chunk):
            out[url] = {"url": url, "domain": domain, "kind": "page", "title": title or url, "author": "",
                        "description": "", "seconds": round(secs or 0), "last_seen": last, "scroll": scroll}
        for url, domain, kind, title, author, desc in conn.execute(
                f"SELECT url, domain, kind, title, author, description FROM pages WHERE url IN ({marks})", chunk):
            d = out.setdefault(url, {"url": url, "seconds": 0, "last_seen": None, "scroll": None, "title": url})
            d.update(domain=domain, kind=kind, title=title or d["title"], author=author or "", description=desc or "")
    return out


def _search_matches(conn, tokens, limit=20):
    if not tokens:
        return []
    # SQLite LIKE is ASCII-case-insensitive only, so filter again in Python.
    where = " AND ".join("query LIKE ?" for _ in tokens)
    rows = conn.execute(f"SELECT ts, engine, query FROM searches WHERE {where} ORDER BY ts DESC LIMIT 500",
                        [f"%{t}%" for t in tokens]).fetchall()
    rows = [r for r in rows if all(t in r[2].lower() for t in tokens)]
    seen, out = set(), []
    for ts, engine, query in rows:
        k = (engine, query.lower())
        if k in seen:
            continue
        seen.add(k)
        out.append({"ts": ts, "engine": engine, "query": query})
        if len(out) >= limit:
            break
    return out


def search(conn, query: str, meaning: bool = True, embed_fn=ai_categorize.embed) -> dict:
    query = (query or "").strip()[:300]
    tokens = _tokens(query)
    if not tokens:
        return {"query": query, "pages": [], "searches": [], "note": None}

    words = _word_matches(conn, tokens) + [(u, None) for u in _title_matches(conn, tokens)]
    meanings, note = _meaning_matches(conn, query, embed_fn) if meaning else ([], None)

    # Reciprocal-rank fusion: good in either list ranks high, good in both ranks highest.
    score, how, snippets, sims = {}, {}, {}, {}
    for rank, (url, snip) in enumerate(words):
        score[url] = score.get(url, 0) + 1 / (RRF_K + rank)
        how.setdefault(url, set()).add("words")
        snippets[url] = snip
    for rank, (url, sim) in enumerate(meanings):
        score[url] = score.get(url, 0) + 1 / (RRF_K + rank)
        how.setdefault(url, set()).add("meaning")
        sims[url] = sim

    ranked = sorted(score, key=lambda u: -score[u])
    details = _page_details(conn, ranked[:MAX_RESULTS * 3])
    pages, seen = [], {}
    for url in ranked:
        d = details.get(url)
        if not d:
            continue
        key = (d["domain"], d["title"])  # same page via different URLs: show once
        if key in seen:
            s = seen[key]
            s["seconds"] += d["seconds"]
            s["last_seen"] = max(filter(None, (s["last_seen"], d["last_seen"])), default=None)
            continue
        snippet = snippets.get(url)
        if not snippet or HL_START not in snippet:  # matched on the title only, or by meaning
            snippet = d["description"][:220]
        pages.append(seen.setdefault(key, {
            **{k: d[k] for k in ("url", "domain", "kind", "title", "author", "seconds", "last_seen", "scroll")},
            "snippet": snippet, "match": sorted(how[url]),
            "similarity": round(sims[url], 3) if url in sims else None,
        }))
        if len(pages) >= MAX_RESULTS:
            break
    return {"query": query, "pages": pages, "searches": _search_matches(conn, tokens), "note": note}
