"""SQLite storage.

sessions: one row per continuous stretch of activity.
    source='desktop' -> foreground app + window title (whole PC, the time "truth")
    source='browser' -> active tab from the extension (app = site domain, url set)
pages:    one row per scanned page (only allowlisted sites), keyed by canonical URL.
"""

import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts  REAL NOT NULL,          -- unix seconds
    end_ts    REAL NOT NULL,          -- unix seconds
    app       TEXT NOT NULL,          -- desktop: exe name; browser: site domain
    title     TEXT NOT NULL,          -- window / tab title
    source    TEXT NOT NULL DEFAULT 'desktop',  -- 'desktop' | 'browser'
    url       TEXT,                   -- browser only (canonical)
    category  TEXT                    -- filled by the categoriser (step 3)
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_end   ON sessions(end_ts);

CREATE TABLE IF NOT EXISTS pages (
    url         TEXT PRIMARY KEY,     -- canonical URL (matches sessions.url)
    domain      TEXT NOT NULL,
    kind        TEXT NOT NULL,        -- 'youtube' | 'article' | 'page' (any site)
    scanned_ts  REAL NOT NULL,
    title       TEXT,
    author      TEXT,                 -- channel / author when available
    description TEXT,
    headings    TEXT,                 -- newline separated
    body        TEXT                  -- capped main text
);

CREATE TABLE IF NOT EXISTS searches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,         -- unix seconds
    engine     TEXT NOT NULL,         -- 'Brave Search', 'YouTube', or the site's domain
    domain     TEXT NOT NULL,         -- site the search was made on
    query      TEXT NOT NULL,
    url        TEXT NOT NULL,
    client_id  TEXT UNIQUE            -- dedupes re-sends from the extension
);
CREATE INDEX IF NOT EXISTS idx_searches_ts ON searches(ts);

-- AI categoriser (Voyage embeddings). Vectors are float32 blobs.
CREATE TABLE IF NOT EXISTS page_ai (
    url         TEXT PRIMARY KEY,     -- matches pages.url
    model       TEXT NOT NULL,
    text_hash   TEXT NOT NULL,        -- what was embedded (re-embed when the page changes)
    vector      BLOB NOT NULL,
    category    TEXT,                 -- NULL = no confident match
    score       REAL,
    rules_hash  TEXT,                 -- category descriptions it was compared against
    updated_ts  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS query_ai (
    query_key   TEXT PRIMARY KEY,     -- lower(trim(search query))
    model       TEXT NOT NULL,
    vector      BLOB NOT NULL,
    category    TEXT,
    score       REAL,
    rules_hash  TEXT,
    updated_ts  REAL NOT NULL
);

-- Time away from the PC that you log yourself (lectures, reading, ...).
CREATE TABLE IF NOT EXISTS manual_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts    REAL NOT NULL,
    end_ts      REAL NOT NULL,
    category    TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_start ON manual_entries(start_ts);

CREATE TABLE IF NOT EXISTS prototype_vectors (
    text_hash   TEXT NOT NULL,
    model       TEXT NOT NULL,
    vector      BLOB NOT NULL,
    PRIMARY KEY (text_hash, model)
);

-- Small key/value store: last backup, extension last seen, ...
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Full-text index over pages (search what you've read). Kept in sync by triggers, so the
-- 60-day purge of page text also removes it from the index.
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    title, author, description, headings, body,
    content='pages', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS pages_fts_insert AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, title, author, description, headings, body)
    VALUES (new.rowid, new.title, new.author, new.description, new.headings, new.body);
END;
CREATE TRIGGER IF NOT EXISTS pages_fts_delete AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, author, description, headings, body)
    VALUES ('delete', old.rowid, old.title, old.author, old.description, old.headings, old.body);
END;
CREATE TRIGGER IF NOT EXISTS pages_fts_update AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, author, description, headings, body)
    VALUES ('delete', old.rowid, old.title, old.author, old.description, old.headings, old.body);
    INSERT INTO pages_fts(rowid, title, author, description, headings, body)
    VALUES (new.rowid, new.title, new.author, new.description, new.headings, new.body);
END;

-- Focus mode: one row per session. end_ts/result stay NULL while it runs.
CREATE TABLE IF NOT EXISTS focus_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts        REAL NOT NULL,
    planned_end_ts  REAL NOT NULL,
    end_ts          REAL,
    result          TEXT,               -- 'completed' | 'stopped'
    label           TEXT NOT NULL DEFAULT ''
);
-- Each time a blocked site was opened during a focus session.
CREATE TABLE IF NOT EXISTS focus_blocks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    focus_id   INTEGER NOT NULL,
    ts         REAL NOT NULL,
    domain     TEXT NOT NULL,
    client_id  TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_focus_blocks ON focus_blocks(focus_id);

-- Groq judge (ai_judge.py): decides pages the rules, keywords and Voyage left "Unsure".
CREATE TABLE IF NOT EXISTS page_judge (
    url         TEXT PRIMARY KEY,     -- sessions.url
    category    TEXT,                 -- NULL = Groq wasn't confident either
    confidence  REAL,
    reason      TEXT,
    model       TEXT NOT NULL,
    input_hash  TEXT NOT NULL,        -- what was sent (ask again when the page changes)
    rules_hash  TEXT NOT NULL,        -- category list it chose from
    updated_ts  REAL NOT NULL
);
-- Groq suggestions for whole sites/apps without a rule. Shown on the dashboard; a rule
-- is only written when you accept one. dismissed = 1 after "Reject".
CREATE TABLE IF NOT EXISTS name_judge (
    kind        TEXT NOT NULL,        -- 'site' | 'app'
    name        TEXT NOT NULL,        -- domain or exe name (lower case)
    category    TEXT,
    confidence  REAL,
    reason      TEXT,
    model       TEXT NOT NULL,
    rules_hash  TEXT NOT NULL,
    updated_ts  REAL NOT NULL,
    dismissed   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, name)
);

-- Phone (Android companion app). App time goes into sessions (source 'phone',
-- app = package name, title = app name, client_id = 'phone:<device>:<package>@<start ms>').
CREATE TABLE IF NOT EXISTS phone_unlocks (
    device  TEXT NOT NULL,
    ts      REAL NOT NULL,
    PRIMARY KEY (device, ts)
);
CREATE INDEX IF NOT EXISTS idx_phone_unlocks_ts ON phone_unlocks(ts);

-- Weekly AI summaries (Groq). payload = exactly what was sent.
CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts  REAL NOT NULL,
    start_day   TEXT NOT NULL,
    end_day     TEXT NOT NULL,
    model       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    text        TEXT NOT NULL
);
"""

PAGE_KINDS = ("youtube", "article", "page", "search")

# Hard caps so a misbehaving client can't balloon the database.
LIMITS = {"title": 300, "author": 200, "description": 2000, "headings": 3000, "body": 3000}
MAX_VISIT_SECONDS = 24 * 3600


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")  # readers + tracker + server can work concurrently
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "client_id" not in cols:  # added in step 2: dedupes visits the extension re-sends
        conn.execute("ALTER TABLE sessions ADD COLUMN client_id TEXT")
    if "scroll_pct" not in cols:  # browser only: deepest scroll position reached, 0-100
        conn.execute("ALTER TABLE sessions ADD COLUMN scroll_pct REAL")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_client ON sessions(client_id)"
    )
    # One-off repair (2026-11-08): a bug stored "no scroll data" as 0%. A real reading is
    # never 0 (the first screen already counts), so 0 means unknown.
    if get_meta(conn, "fix_scroll_zero") is None:
        conn.execute("UPDATE sessions SET scroll_pct = NULL WHERE scroll_pct = 0")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('fix_scroll_zero', '1')")
    # Index pages that were stored before the full-text index existed.
    if get_meta(conn, "fts_built") is None:
        conn.execute("INSERT INTO pages_fts(pages_fts) VALUES ('rebuild')")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('fts_built', '1')")
    conn.commit()


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


# --- desktop sessions ---------------------------------------------------------

def start_session(conn, start_ts, end_ts, app, title) -> int:
    cur = conn.execute(
        "INSERT INTO sessions (start_ts, end_ts, app, title) VALUES (?, ?, ?, ?)",
        (start_ts, end_ts, app, title),
    )
    conn.commit()
    return cur.lastrowid


def extend_session(conn, session_id, end_ts) -> None:
    conn.execute("UPDATE sessions SET end_ts = ? WHERE id = ?", (end_ts, session_id))
    conn.commit()


# --- browser data -------------------------------------------------------------

def canonical_url(url: str) -> str:
    """Strip noise so the same video/search/article always maps to one URL."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    q = parse_qs(p.query)
    if host.endswith("youtube.com") and p.path == "/watch" and "v" in q:
        query = urlencode({"v": q["v"][0]})
    elif host.endswith("google.com") and p.path == "/search" and "q" in q:
        query = urlencode({"q": q["q"][0]})
    else:
        query = p.query
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, query, ""))


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _http_url(value) -> str:
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        raise ValueError("url must be http(s)")
    return value[:2000]


def _text(value, limit) -> str:
    return value[:limit] if isinstance(value, str) else ""


def insert_browser_visits(conn, visits) -> int:
    rows = []
    for v in visits:
        try:  # skip a malformed item instead of rejecting the whole batch
            start, end = float(v["start"]), float(v["end"])
            if not (0 <= end - start <= MAX_VISIT_SECONDS) or end > time.time() + 300:
                continue  # nonsense timestamps; drop rather than corrupt totals
            url = canonical_url(_http_url(v["url"]))
            scroll = v.get("scroll")
            scroll = None if scroll is None else max(0.0, min(100.0, float(scroll)))
            rows.append((start, end, domain_of(url), _text(v.get("title"), LIMITS["title"]),
                         url, str(v["id"])[:64], scroll))
        except (KeyError, TypeError, ValueError):
            continue
    # The extension re-sends the in-progress visit every heartbeat with the same id,
    # so this updates one live row in place instead of adding duplicates.
    conn.executemany(
        """INSERT INTO sessions (start_ts, end_ts, app, title, source, url, client_id, scroll_pct)
           VALUES (?, ?, ?, ?, 'browser', ?, ?, ?)
           ON CONFLICT(client_id) DO UPDATE SET
             end_ts     = MAX(sessions.end_ts, excluded.end_ts),
             title      = excluded.title,
             scroll_pct = CASE  -- keep the deepest scroll; "unknown" (NULL) never becomes 0
               WHEN excluded.scroll_pct IS NULL THEN sessions.scroll_pct
               WHEN sessions.scroll_pct IS NULL THEN excluded.scroll_pct
               ELSE MAX(sessions.scroll_pct, excluded.scroll_pct) END""",
        rows,
    )
    conn.commit()
    return len(rows)


def insert_searches(conn, searches) -> int:
    rows = []
    for s in searches:
        try:
            url = _http_url(s["url"]).split("#")[0]
            query = s["query"].strip()
            ts = float(s["ts"])
            if not query or ts > time.time() + 300:
                continue
            domain = domain_of(url)
            rows.append((ts, _text(s.get("engine"), 100) or domain, domain, query[:500], url,
                         str(s["id"])[:64]))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    conn.executemany(
        "INSERT OR IGNORE INTO searches (ts, engine, domain, query, url, client_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


MAX_MANUAL_SECONDS = 16 * 3600


def add_manual_entry(conn, start_ts: float, end_ts: float, category: str, note: str, categories) -> int:
    start_ts, end_ts = float(start_ts), float(end_ts)
    if not 0 < end_ts - start_ts <= MAX_MANUAL_SECONDS:
        raise ValueError("an entry must be longer than 0 and at most 16 hours")
    if category not in categories:
        raise ValueError(f"unknown category: {category}")
    cur = conn.execute(
        "INSERT INTO manual_entries (start_ts, end_ts, category, note, created_ts) VALUES (?, ?, ?, ?, ?)",
        (start_ts, end_ts, category, str(note or "").strip()[:200], time.time()))
    conn.commit()
    return cur.lastrowid


def delete_manual_entry(conn, entry_id: int) -> bool:
    cur = conn.execute("DELETE FROM manual_entries WHERE id = ?", (int(entry_id),))
    conn.commit()
    return cur.rowcount > 0


def purge_page_text(conn, days: float) -> int:
    """Retention: drop full page text older than `days`. Title/author/URL are kept,
    so time and categories for old visits still work."""
    cutoff = time.time() - days * 86400
    cur = conn.execute(
        "UPDATE pages SET description = NULL, headings = NULL, body = NULL"
        " WHERE scanned_ts < ? AND (description IS NOT NULL OR headings IS NOT NULL OR body IS NOT NULL)",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def upsert_pages(conn, pages) -> int:
    rows = []
    for p in pages:
        try:
            url = canonical_url(_http_url(p["url"]))
            kind = p.get("kind")
            if kind not in PAGE_KINDS:
                continue
            rows.append((
                url, domain_of(url), kind, float(p.get("ts") or time.time()),
                *(_text(p.get(field), LIMITS[field]) for field in
                  ("title", "author", "description", "headings", "body")),
            ))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    conn.executemany(
        """INSERT INTO pages (url, domain, kind, scanned_ts, title, author, description, headings, body)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET
             scanned_ts=excluded.scanned_ts, title=excluded.title, author=excluded.author,
             description=excluded.description, headings=excluded.headings, body=excluded.body""",
        rows,
    )
    conn.commit()
    return len(rows)
