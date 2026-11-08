"""Turns raw sessions into categorised time.

Rules live in categories.json (project root). Categories are computed when data is
read, never stored, so editing the rules instantly re-categorises all history.

Desktop sessions are the time truth. When the browser is in front, that time is
split using the extension's visits (site + scanned page), so an hour of brave.exe
becomes e.g. 40 min Study (YouTube lecture) + 20 min Social (LinkedIn).
"""

import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path

import sitelists

RULES_PATH = Path(__file__).resolve().parent.parent / "categories.json"
BROWSERS = {"brave.exe", "chrome.exe", "msedge.exe"}
CONTENT = "content"
ADULT_CATEGORY = "Entertainment"  # adult sites (extension/adult.js) without a rule of their own

# How much a keyword hit in each page field counts. The title says the most.
FIELD_WEIGHTS = (("title", 3), ("author", 2), ("description", 1), ("headings", 1), ("body", 0.5))
MAX_HITS_PER_FIELD = 5  # stop one long comment thread from outvoting the title
MIN_SCORE = 2           # one title hit (3) is enough; one description hit (1) is not
MARGIN = 1.5            # winner must beat the runner-up by this factor, else "unsure"


@dataclass
class Item:
    start: float
    end: float
    category: str
    app: str        # exe name, or site domain for browser time
    title: str
    source: str     # 'desktop' | 'browser'
    matched: bool   # False = no rule matched; shown in "Not categorised yet"
    url: str | None = None  # browser time only
    via: str = ""   # browser time: what decided it ("groq" = the AI judge; shown as a badge)

    @property
    def seconds(self) -> float:
        return self.end - self.start


class Rules:
    def __init__(self, path: Path = RULES_PATH):
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.categories = list(raw["categories"])
        self.productive = set(raw.get("productive", ["Coding", "Study", "Work"]))
        self.fallback = {"app": "Other", "site": "Browsing", "content": "Unsure",
                         **raw.get("fallback", {})}
        self.apps = {k.lower(): v for k, v in raw.get("apps", {}).items()}
        self.sites = {k.lower(): v for k, v in raw.get("sites", {}).items()}
        self.channels = {k.lower(): v for k, v in raw.get("channels", {}).items()}
        self.pages = dict(raw.get("pages", {}))  # exact page URL -> category (set from the dashboard)
        self.phone_apps = {k.lower(): v for k, v in raw.get("phone_apps", {}).items()}  # Android package -> category
        self.descriptions = {k: [str(t) for t in v] for k, v in raw.get("descriptions", {}).items() if v}
        self.keywords = {
            cat: re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.I)
            for cat, words in raw.get("keywords", {}).items() if words
        }
        used = [*self.apps.values(), *self.sites.values(), *self.channels.values(), *self.pages.values(),
                *self.phone_apps.values(),
                *self.keywords, *self.fallback.values(), *self.productive, *self.descriptions]
        unknown = sorted({c for c in used if c not in self.categories and c != CONTENT})
        if unknown:
            raise ValueError(f"categories.json uses categories not in the list: {unknown}")

    # --- matching -----------------------------------------------------------
    def site_rule(self, domain: str):
        """Most specific rule wins: music.youtube.com before youtube.com."""
        parts = domain.lower().split(".")
        for i in range(len(parts)):
            rule = self.sites.get(".".join(parts[i:]))
            if rule:
                return rule
        return None

    def classify_text(self, fields):
        """fields: [(text, weight)]. Returns the winning category or None."""
        scores = {}
        for text, weight in fields:
            if not text:
                continue
            for cat, rx in self.keywords.items():
                hits = min(len(rx.findall(text)), MAX_HITS_PER_FIELD)
                if hits:
                    scores[cat] = scores.get(cat, 0) + hits * weight
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        best, top = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        return best if top >= MIN_SCORE and top >= runner_up * MARGIN else None

    def classify_desktop(self, app: str, title: str):
        rule = self.apps.get(app.lower())
        if rule:
            return rule, True
        cat = self.classify_text([(title, 3)])
        return (cat, True) if cat else (self.fallback["app"], False)

    def classify_phone(self, package: str, label: str):
        """Phone apps: a phone_apps rule (by package name), else keywords in the app's name."""
        rule = self.phone_apps.get(package.lower())
        if rule in self.categories:
            return rule, True
        cat = self.classify_text([(label, 3)])
        return (cat, True) if cat else (self.fallback["app"], False)

    def _page_fields(self, title, page):
        fields = [((page or {}).get(f), w) for f, w in FIELD_WEIGHTS]
        if not (page or {}).get("title"):
            fields.append((title, 3))  # no scan (yet): judge by the tab title
        return fields

    def classify_browser(self, domain: str, title: str, page: dict | None, url: str | None = None):
        cat, matched, _ = self.classify_browser_ex(domain, title, page, url)
        return cat, matched

    def classify_browser_ex(self, domain: str, title: str, page: dict | None, url: str | None = None):
        """-> (category, matched, via). Order: page rule > site rule > adult site > channel
        rule > keywords > AI embeddings (Voyage) > AI judge (Groq) > fallback."""
        if url and self.pages.get(url) in self.categories:
            return self.pages[url], True, "rule"
        rule = self.site_rule(domain)
        page = page or {}
        ai = page.get("ai_category") if page.get("ai_category") in self.categories else None
        judge = page.get("judge_category") if page.get("judge_category") in self.categories else None
        if rule == CONTENT:
            author = page.get("author") or ""
            if author.lower() in self.channels:
                return self.channels[author.lower()], True, "rule"
            cat = self.classify_text(self._page_fields(title, page))
            if cat:
                return cat, True, "keywords"
            if ai:
                return ai, True, "voyage"
            if judge:
                return judge, True, "groq"
            return self.fallback["content"], True, "fallback"
        if rule:
            return rule, True, "rule"
        if ADULT_CATEGORY in self.categories and sitelists.is_adult(domain):
            return ADULT_CATEGORY, True, "adult"
        # Unknown site: let the page content (if scanned) or the tab title decide.
        cat = self.classify_text(self._page_fields(title, page))
        if cat:
            return cat, True, "keywords"
        if ai:
            return ai, True, "voyage"
        return self.fallback["site"], False, "fallback"


# --- reading + splitting -----------------------------------------------------

def _load_pages(conn, urls):
    pages, urls = {}, list({u for u in urls if u})
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        rows = conn.execute(
            f"SELECT p.url, p.title, p.author, p.description, p.headings, p.body, a.category"
            f" FROM pages p LEFT JOIN page_ai a ON a.url = p.url"
            f" WHERE p.url IN ({','.join('?' * len(chunk))})", chunk)
        for url, *vals in rows:
            pages[url] = dict(zip(("title", "author", "description", "headings", "body", "ai_category"), vals))
        # The Groq judge also decides pages that were never scanned (by tab title), so it
        # can add an entry here for a URL that isn't in `pages`.
        for url, cat in conn.execute(
                f"SELECT url, category FROM page_judge WHERE category IS NOT NULL"
                f" AND url IN ({','.join('?' * len(chunk))})", chunk):
            pages.setdefault(url, {})["judge_category"] = cat
    return pages


def _manual_items(conn, start, end, desktop_spans) -> list[Item]:
    """Offline time you logged yourself. Only the parts when the PC wasn't in use count,
    so a lecture logged 9-11 while your laptop was open for 30 min isn't double counted."""
    rows = conn.execute(
        "SELECT start_ts, end_ts, category, note FROM manual_entries WHERE end_ts > ? AND start_ts < ?"
        " ORDER BY start_ts", (start, end)).fetchall()
    items = []
    for s, e, cat, note in rows:
        s, e = max(s, start), min(e, end)
        cursor = s
        for ds, de in desktop_spans:
            if de <= cursor or ds >= e:
                continue
            if ds > cursor:
                items.append(Item(cursor, ds, cat, "Offline", note or "Offline time", "manual", True))
            cursor = max(cursor, de)
            if cursor >= e:
                break
        if cursor < e:
            items.append(Item(cursor, e, cat, "Offline", note or "Offline time", "manual", True))
    return items


def classify_search(rules: Rules, query: str, ai_category: str | None) -> str:
    """Keywords first, then the AI's category for the query, else ''."""
    cat = rules.classify_text([(query, 3)])
    if cat:
        return cat
    return ai_category if ai_category in rules.categories else ""


def categorise(conn, start: float, end: float, rules: Rules | None = None) -> list[Item]:
    """All activity between start and end as categorised, non-overlapping Items
    (PC activity + offline time you logged), sorted by start."""
    rules = rules or Rules()
    phone = _phone_items(conn, start, end, rules)
    items = _minus(_categorise_pc(conn, start, end, rules), phone) + phone
    spans = sorted((it.start, it.end) for it in items)
    items += _manual_items(conn, start, end, spans)
    return sorted(items, key=lambda it: it.start)


def _phone_items(conn, start, end, rules) -> list[Item]:
    """Phone app time from the Android companion app (sessions with source 'phone';
    app = package name, title = the app's name)."""
    rows = conn.execute(
        "SELECT start_ts, end_ts, app, title FROM sessions WHERE source = 'phone' AND end_ts > ? AND start_ts < ?"
        " ORDER BY start_ts", (start, end)).fetchall()
    items = []
    for s, e, pkg, label in rows:
        s, e = max(s, start), min(e, end)
        if e > s:
            cat, matched = rules.classify_phone(pkg, label)
            items.append(Item(s, e, cat, label or pkg, pkg, "phone", matched))
    return items


def _minus(items, phone) -> list[Item]:
    """PC time with the phone's time cut out. When both overlap you were on the phone (the
    PC only stays "active" for up to 2 min without input, or while a video plays), so each
    second is counted once, as phone time."""
    if not phone:
        return items
    spans = sorted((p.start, p.end) for p in phone)
    out, j = [], 0
    for it in sorted(items, key=lambda it: it.start):
        while j < len(spans) and spans[j][1] <= it.start:
            j += 1
        cursor, k = it.start, j
        while k < len(spans) and spans[k][0] < it.end:
            s, e = spans[k]
            if s > cursor:
                out.append(dataclasses.replace(it, start=cursor, end=s))
            cursor = max(cursor, e)
            k += 1
        if cursor < it.end:
            out.append(dataclasses.replace(it, start=cursor, end=it.end) if cursor != it.start else it)
    return out


def _categorise_pc(conn, start: float, end: float, rules: Rules) -> list[Item]:
    """PC activity only (desktop sessions, split by browser visits)."""
    q = ("SELECT start_ts, end_ts, app, title, url FROM sessions"
         " WHERE source = ? AND end_ts > ? AND start_ts < ? ORDER BY start_ts")
    desktop = conn.execute(q, ("desktop", start, end)).fetchall()
    browser_rows = conn.execute(q, ("browser", start, end)).fetchall()
    pages = _load_pages(conn, (r[4] for r in browser_rows))

    visits = []
    for s, e, domain, title, url in browser_rows:
        cat, matched, via = rules.classify_browser_ex(domain, title, pages.get(url), url)
        visits.append(Item(s, e, cat, domain, title, "browser", matched, url, via))

    items, i = [], 0
    for s, e, app, title, _ in desktop:
        s, e = max(s, start), min(e, end)
        if e <= s:
            continue
        if app.lower() not in BROWSERS:
            cat, matched = rules.classify_desktop(app, title)
            items.append(Item(s, e, cat, app, title, "desktop", matched))
            continue

        # Browser in front: hand the time to whatever tab the extension saw.
        while i < len(visits) and visits[i].end <= s:
            i += 1
        cursor = s
        k = i
        while k < len(visits) and visits[k].start < e:
            v = visits[k]
            k += 1
            os_, oe = max(cursor, v.start), min(e, v.end)
            if oe <= os_:
                continue
            if os_ > cursor:
                items.append(_browser_gap(rules, cursor, os_, app, title))
            items.append(Item(os_, oe, v.category, v.app, v.title, "browser", v.matched, v.url, v.via))
            cursor = oe
        if cursor < e:
            # No extension data (new tab page, settings, incognito, extension off).
            items.append(_browser_gap(rules, cursor, e, app, title))
    return items


def _browser_gap(rules, s, e, app, title) -> Item:
    cat = rules.classify_text([(title, 3)]) or rules.fallback["site"]
    return Item(s, e, cat, app, title, "desktop", True)
