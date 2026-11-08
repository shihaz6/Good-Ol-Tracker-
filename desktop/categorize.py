"""Turns raw sessions into categorised time.

Rules live in categories.json (project root). Categories are computed when data is
read, never stored, so editing the rules instantly re-categorises all history.

Desktop sessions are the time truth. When the browser is in front, that time is
split using the extension's visits (site + scanned page), so an hour of brave.exe
becomes e.g. 40 min Study (YouTube lecture) + 20 min Social (LinkedIn).
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent.parent / "categories.json"
BROWSERS = {"brave.exe", "chrome.exe", "msedge.exe"}
CONTENT = "content"

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

    @property
    def seconds(self) -> float:
        return self.end - self.start


class Rules:
    def __init__(self, path: Path = RULES_PATH):
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.categories = list(raw["categories"])
        self.fallback = {"app": "Other", "site": "Browsing", "content": "Unsure",
                         **raw.get("fallback", {})}
        self.apps = {k.lower(): v for k, v in raw.get("apps", {}).items()}
        self.sites = {k.lower(): v for k, v in raw.get("sites", {}).items()}
        self.channels = {k.lower(): v for k, v in raw.get("channels", {}).items()}
        self.keywords = {
            cat: re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.I)
            for cat, words in raw.get("keywords", {}).items() if words
        }
        used = [*self.apps.values(), *self.sites.values(), *self.channels.values(),
                *self.keywords, *self.fallback.values()]
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

    def _page_fields(self, title, page):
        fields = [((page or {}).get(f), w) for f, w in FIELD_WEIGHTS]
        if not (page or {}).get("title"):
            fields.append((title, 3))  # no scan (yet): judge by the tab title
        return fields

    def classify_browser(self, domain: str, title: str, page: dict | None):
        rule = self.site_rule(domain)
        if rule == CONTENT:
            author = (page or {}).get("author") or ""
            if author.lower() in self.channels:
                return self.channels[author.lower()], True
            return self.classify_text(self._page_fields(title, page)) or self.fallback["content"], True
        if rule:
            return rule, True
        # Unknown site: let the page content (if scanned) or the tab title decide.
        cat = self.classify_text(self._page_fields(title, page))
        return (cat, True) if cat else (self.fallback["site"], False)


# --- reading + splitting -----------------------------------------------------

def _load_pages(conn, urls):
    pages, urls = {}, list({u for u in urls if u})
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        rows = conn.execute(
            f"SELECT url, title, author, description, headings, body FROM pages"
            f" WHERE url IN ({','.join('?' * len(chunk))})", chunk)
        for url, *vals in rows:
            pages[url] = dict(zip(("title", "author", "description", "headings", "body"), vals))
    return pages


def categorise(conn, start: float, end: float, rules: Rules | None = None) -> list[Item]:
    """All activity between start and end as categorised, non-overlapping Items."""
    rules = rules or Rules()
    q = ("SELECT start_ts, end_ts, app, title, url FROM sessions"
         " WHERE source = ? AND end_ts > ? AND start_ts < ? ORDER BY start_ts")
    desktop = conn.execute(q, ("desktop", start, end)).fetchall()
    browser_rows = conn.execute(q, ("browser", start, end)).fetchall()
    pages = _load_pages(conn, (r[4] for r in browser_rows))

    visits = []
    for s, e, domain, title, url in browser_rows:
        cat, matched = rules.classify_browser(domain, title, pages.get(url))
        visits.append(Item(s, e, cat, domain, title, "browser", matched))

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
            items.append(Item(os_, oe, v.category, v.app, v.title, "browser", v.matched))
            cursor = oe
        if cursor < e:
            # No extension data (new tab page, settings, incognito, extension off).
            items.append(_browser_gap(rules, cursor, e, app, title))
    return items


def _browser_gap(rules, s, e, app, title) -> Item:
    cat = rules.classify_text([(title, 3)]) or rules.fallback["site"]
    return Item(s, e, cat, app, title, "desktop", True)
