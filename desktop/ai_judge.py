"""AI judge (Groq): the last step of categorising, for what the rules, keywords and Voyage
couldn't decide.

Two jobs, both run in the background (tracker.py) right after the Voyage step:

  Pages   Pages on mixed sites (rule "content": YouTube, claude.ai, Google, ...) that
          ended up "Unsure". Groq picks a category; if it's confident (>= MIN_CONFIDENCE)
          the page gets it automatically (dashboard shows an "AI" badge). Your own rules
          always win, so picking a category by hand overrides it.
  Names   Sites and apps with no rule at all. Groq's answer is only a SUGGESTION on the
          dashboard's Fix categories card; a rule is written when you click Accept.

Each page/name is asked about once and the answer is stored (page_judge, name_judge).
It's asked again only if the page's text changes or the category list changes.

What leaves the PC (to api.groq.com):
  pages: site, tab title, page title/channel/description, the search words in the URL,
         and the first EXCERPT_CHARS characters of the page text.
  names: the site/exe name and up to 3 of its page/window titles.
Never sent: anything on the extension's never-read list (WhatsApp, banks, password
managers, see extension/blocklist.js), private-window time (never recorded) and adult
sites (categorised locally).
"""

import hashlib
import json
import logging
import time
from urllib.parse import parse_qs, urlsplit

import categorize
import keys
import sitelists
import weekly

log = logging.getLogger("tracker")

MIN_CONFIDENCE = 0.7     # below this a page stays Unsure / no suggestion is shown
BATCH = 20               # items per Groq request
MAX_REQUESTS = 3         # per background run (every 2 minutes): stays far below Groq's free limits
LOOKBACK_DAYS = 30       # only judge things you used recently ...
MIN_SECONDS = 10         # ... for at least this long
EXCERPT_CHARS = 500
BACKOFF = 600            # after a rate limit / error, wait this long before asking again

status = {"last_run": None, "last_error": None, "model": None, "paused_until": 0.0}


class JudgeError(Exception):
    pass


def _sha(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def rules_hash(rules) -> str:
    """Answers are kept until the category list or its descriptions change."""
    return _sha([rules.categories, rules.descriptions])


def _clip(text, n) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _search_words(url: str) -> str:
    try:
        qs = parse_qs(urlsplit(url).query)
    except ValueError:
        return ""
    for key in ("q", "query", "search_query", "p"):
        if qs.get(key):
            return _clip(qs[key][0], 200)
    return ""


# --------------------------------------------------------------------- candidates

def page_candidates(conn, rules, now=None) -> list:
    """Unsure pages worth asking about, most-used first: [(url, payload, input_hash)]."""
    now = now or time.time()
    rows = conn.execute(
        "SELECT url, app, MAX(title), SUM(end_ts - start_ts) AS secs FROM sessions"
        " WHERE source = 'browser' AND url IS NOT NULL AND end_ts > ?"
        " GROUP BY url HAVING secs >= ? ORDER BY secs DESC LIMIT 2000",
        (now - LOOKBACK_DAYS * 86400, MIN_SECONDS)).fetchall()
    if not rows:
        return []
    pages = categorize._load_pages(conn, [r[0] for r in rows])
    done = {}
    urls = [r[0] for r in rows]
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        done.update((u, (h, rh)) for u, h, rh in conn.execute(
            f"SELECT url, input_hash, rules_hash FROM page_judge WHERE url IN ({','.join('?' * len(chunk))})", chunk))
    rh = rules_hash(rules)
    out = []
    for url, domain, title, _ in rows:
        if sitelists.never_send(domain) or sitelists.is_adult(domain):
            continue
        if rules.site_rule(domain) != categorize.CONTENT:
            continue  # sites without a rule get a whole-site suggestion instead
        page = {k: v for k, v in (pages.get(url) or {}).items() if k != "judge_category"}
        if rules.classify_browser_ex(domain, title, page, url)[2] != "fallback":
            continue  # rules / keywords / Voyage already decided it
        payload = {"site": domain, "tab_title": _clip(title, 200)}
        for key, n in (("title", 200), ("author", 100), ("description", 300)):
            if page.get(key):
                payload["page_" + key if key == "title" else key] = _clip(page[key], n)
        if search := _search_words(url):
            payload["searched_for"] = search
        if page.get("body"):
            payload["text_start"] = _clip(page["body"], EXCERPT_CHARS)
        h = _sha(payload)
        if done.get(url) == (h, rh):
            continue
        out.append((url, payload, h))
    return out


def name_candidates(conn, rules, now=None) -> list:
    """Sites/apps with no rule, most-used first: [(kind, name, payload)]."""
    now = now or time.time()
    since = now - LOOKBACK_DAYS * 86400
    dismissed_or_done = {(k, n) for k, n, rh in conn.execute(
        "SELECT kind, name, rules_hash FROM name_judge") if rh == rules_hash(rules)}
    out = []
    for source, kind in (("browser", "site"), ("desktop", "app")):
        rows = conn.execute(
            "SELECT app, SUM(end_ts - start_ts) AS secs FROM sessions WHERE source = ? AND end_ts > ?"
            " GROUP BY lower(app) HAVING secs >= ? ORDER BY secs DESC LIMIT 200",
            (source, since, MIN_SECONDS)).fetchall()
        for name, _ in rows:
            key = name.lower()
            if (kind, key) in dismissed_or_done:
                continue
            if kind == "site":
                if rules.site_rule(key) or sitelists.never_send(key) or sitelists.is_adult(key):
                    continue
            elif key in rules.apps or key in categorize.BROWSERS or sitelists.never_send_app(key):
                continue
            titles = [t for (t,) in conn.execute(
                "SELECT title FROM sessions WHERE source = ? AND lower(app) = ? AND end_ts > ? AND title != ''"
                " GROUP BY title ORDER BY SUM(end_ts - start_ts) DESC LIMIT 3", (source, key, since))]
            out.append((kind, key, {"type": "website" if kind == "site" else "Windows program",
                                    "name": name, "example_titles": [_clip(t, 100) for t in titles]}))
    # Phone apps: app = package, title = the app's name. Only names go out (no titles exist).
    for pkg, label, _ in conn.execute(
            "SELECT lower(app), MAX(title), SUM(end_ts - start_ts) AS secs FROM sessions"
            " WHERE source = 'phone' AND end_ts > ? GROUP BY lower(app) HAVING secs >= ?"
            " ORDER BY secs DESC LIMIT 200", (since, MIN_SECONDS)):
        if ("phone", pkg) in dismissed_or_done or pkg in rules.phone_apps or pkg in categorize.IGNORED_PHONE_APPS:
            continue
        if sitelists.never_send_app(pkg) or sitelists.never_send_app(label or ""):
            continue
        out.append(("phone", pkg, {"type": "Android phone app", "name": label or pkg, "package": pkg}))
    return out


# --------------------------------------------------------------------- asking Groq

def _system_prompt(rules, allow, extra="") -> str:
    lines = []
    for cat in allow:
        hints = rules.descriptions.get(cat)
        lines.append(f"- {cat}" + (f": e.g. {'; '.join(hints)}" if hints else ""))
    return (
        "You categorise one person's computer activity (a university computing student) for a "
        "time tracker. Choose exactly one category per item from this list:\n"
        + "\n".join(lines) + "\n" + extra +
        "\nRules: judge only from the given information, never guess wildly. "
        "confidence is 0 to 1: how sure you are a careful human would pick the same category. "
        "If the information is too thin, give your best category with a low confidence. "
        "reason: at most 12 words.\n"
        'Reply with JSON only: {"results": [{"id": <id>, "category": "<category>", '
        '"confidence": <0-1>, "reason": "<why>"}]}'
    )


def _ask(key, model, system, items, request=None) -> dict:
    """items: [(id, payload)] -> {id: (category, confidence, reason)}."""
    request = request or weekly._request
    body = {
        "model": model, "temperature": 0, "max_tokens": 120 * len(items) + 200,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(
                {"items": [{"id": i, **p} for i, p in items]}, ensure_ascii=False)},
        ],
    }
    try:
        reply = request(key, "/chat/completions", body)
        data = json.loads(reply["choices"][0]["message"]["content"])
    except weekly.SummaryError as exc:
        raise JudgeError(str(exc)) from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise JudgeError("Groq returned an unexpected answer") from exc
    out = {}
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        try:
            conf = max(0.0, min(1.0, float(r.get("confidence", 0))))
        except (TypeError, ValueError):
            conf = 0.0
        out[r.get("id")] = (str(r.get("category") or ""), conf, _clip(r.get("reason"), 160))
    return out


def run_batch(conn, rules, request=None, now=None) -> dict:
    """One background step. Returns counts; raises JudgeError on API trouble."""
    now = now or time.time()
    key = keys.load_key("groq")
    if not key:
        return {"status": "no_key"}
    if now < status["paused_until"]:
        return {"status": "waiting"}
    rh = rules_hash(rules)
    try:
        model = status["model"] or ("test-model" if request else weekly.choose_model(key))
    except weekly.SummaryError as exc:
        status["paused_until"] = now + BACKOFF
        raise JudgeError(str(exc)) from exc
    status["model"] = model
    page_cats = [c for c in rules.categories if c != rules.fallback["content"]]
    name_cats = page_cats  # "Other" is fine for system tools

    pages_done = names_done = requests = 0
    try:
        todo = page_candidates(conn, rules, now)
        system = _system_prompt(rules, page_cats, "Items are web pages; the site, title and start of the text describe them.\n")
        while todo and requests < MAX_REQUESTS:
            chunk, todo = todo[:BATCH], todo[BATCH:]
            answers = _ask(key, model, system, [(i, p) for i, (_, p, _) in enumerate(chunk)], request)
            requests += 1
            rows = []
            for i, (url, _, h) in enumerate(chunk):
                cat, conf, reason = answers.get(i, ("", 0.0, ""))
                ok = cat in page_cats and conf >= MIN_CONFIDENCE
                rows.append((url, cat if ok else None, conf, reason, model, h, rh, now))
            conn.executemany("INSERT OR REPLACE INTO page_judge VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
            pages_done += len(rows)

        names = name_candidates(conn, rules, now)
        if names and requests < MAX_REQUESTS:
            chunk = names[:BATCH]
            system = _system_prompt(rules, name_cats + [categorize.CONTENT],
                                    'Items are whole websites, Windows programs or Android phone apps. '
                                    'Use "content" (websites only) for a '
                                    "website whose pages differ a lot in category (video sites, AI chats, "
                                    "search engines, forums), so each page is judged on its own. Use Other for "
                                    "Windows system tools and pop-ups.\n")
            answers = _ask(key, model, system, [(i, p) for i, (_, _, p) in enumerate(chunk)], request)
            requests += 1
            rows = []
            for i, (kind, name, _) in enumerate(chunk):
                cat, conf, reason = answers.get(i, ("", 0.0, ""))
                valid = cat in name_cats or (cat == categorize.CONTENT and kind == "site")
                rows.append((kind, name, cat if valid else None, conf, reason, model, rh, now))
            conn.executemany(
                "INSERT INTO name_judge (kind, name, category, confidence, reason, model, rules_hash, updated_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(kind, name) DO UPDATE SET"
                " category = excluded.category, confidence = excluded.confidence, reason = excluded.reason,"
                " model = excluded.model, rules_hash = excluded.rules_hash, updated_ts = excluded.updated_ts",
                rows)
            conn.commit()
            names_done = len(rows)
    except JudgeError:
        status["paused_until"] = now + BACKOFF
        raise
    return {"status": "ok", "pages": pages_done, "names": names_done, "requests": requests, "model": model}


# --------------------------------------------------------------------- reading results

def suggestions(conn, rules) -> dict:
    """{(kind, name): {category, confidence, reason}} for confident, not-dismissed answers
    about sites/apps that still have no rule."""
    out = {}
    for kind, name, cat, conf, reason in conn.execute(
            "SELECT kind, name, category, confidence, reason FROM name_judge"
            " WHERE dismissed = 0 AND category IS NOT NULL AND confidence >= ?", (MIN_CONFIDENCE,)):
        if cat != categorize.CONTENT and cat not in rules.categories:
            continue
        if kind == "site" and rules.site_rule(name):
            continue
        if kind == "app" and name in rules.apps:
            continue
        if kind == "phone" and (name in rules.phone_apps or cat == categorize.CONTENT):
            continue
        out[(kind, name)] = {"category": cat, "confidence": round(conf, 2), "reason": reason}
    return out


def page_reasons(conn, urls) -> dict:
    """{url: (confidence, reason)} for pages the judge decided."""
    urls, out = list({u for u in urls if u}), {}
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        for u, conf, reason in conn.execute(
                f"SELECT url, confidence, reason FROM page_judge WHERE category IS NOT NULL"
                f" AND url IN ({','.join('?' * len(chunk))})", chunk):
            out[u] = (conf, reason)
    return out


def dismiss(conn, kind: str, name: str) -> None:
    if kind not in ("site", "app", "phone") or not name:
        raise ValueError("kind must be site, app or phone, with a name")
    cur = conn.execute("UPDATE name_judge SET dismissed = 1 WHERE kind = ? AND name = ?", (kind, name.lower()))
    conn.commit()
    if not cur.rowcount:
        raise ValueError("no such suggestion")


def counts(conn) -> dict:
    decided, asked = conn.execute("SELECT COUNT(category), COUNT(*) FROM page_judge").fetchone()
    return {"pages_asked": asked, "pages_decided": decided,
            "suggestions": conn.execute(
                "SELECT COUNT(*) FROM name_judge WHERE dismissed = 0 AND category IS NOT NULL AND confidence >= ?",
                (MIN_CONFIDENCE,)).fetchone()[0]}
