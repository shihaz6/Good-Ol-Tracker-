"""Focus mode: timed sessions during which the browser extension blocks distracting sites.

The desktop app is the source of truth. Sessions start/stop from the dashboard or the
extension popup; the extension learns the current state from every /ingest reply (within
~30 s) or immediately when it started/stopped it itself. Each blocked attempt is reported
back, and each session is scored afterwards by how much of it went to productive categories.
"""

import json
import re
import time

import categorize
import db

MIN_MINUTES, MAX_MINUTES = 5, 240
DONE_GRACE = 60  # stopping within a minute of the end still counts as finished
DEFAULTS = {"minutes": 50, "block": [], "adult": "focus"}  # block filled from categories.json on first use
ADULT_MODES = ("focus", "always", "off")  # adult-site filter (list lives in the extension: adult.js)
DEFAULT_BLOCK_CATEGORIES = ("Entertainment", "Social")
EXTRA_DEFAULT_BLOCK = ["reddit.com", "9gag.com"]
_DOMAIN = re.compile(r"^(?=.{1,253}$)[a-z0-9-]+(\.[a-z0-9-]+)+$")


class FocusError(ValueError):
    pass


def _settings_path():
    return db.DATA_DIR / "focus.json"


def default_block(rules=None) -> list:
    rules = rules or categorize.Rules()
    sites = [d for d, c in rules.sites.items() if c in DEFAULT_BLOCK_CATEGORIES]
    return sorted(set(sites + EXTRA_DEFAULT_BLOCK))


def normalize_domain(value) -> str:
    d = str(value or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    if not _DOMAIN.match(d):
        raise FocusError(f"not a site name: {value!r} (use e.g. youtube.com)")
    return d


def settings() -> dict:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    if "block" not in data:
        out["block"] = default_block()
    return out


def save_settings(body: dict) -> dict:
    current = settings()
    if "minutes" in body:
        current["minutes"] = _minutes(body["minutes"])
    if "adult" in body:
        if body["adult"] not in ADULT_MODES:
            raise FocusError("adult must be focus, always or off")
        current["adult"] = body["adult"]
    if "block" in body:
        items = body["block"]
        if isinstance(items, str):
            items = re.split(r"[\s,]+", items)
        if not isinstance(items, list) or len(items) > 300:
            raise FocusError("block list must be a list of up to 300 sites")
        current["block"] = sorted({normalize_domain(d) for d in items if str(d).strip()})
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _settings_path().write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def _minutes(value) -> int:
    try:
        m = int(value)
    except (TypeError, ValueError):
        raise FocusError("minutes must be a number") from None
    if not MIN_MINUTES <= m <= MAX_MINUTES:
        raise FocusError(f"a focus session is {MIN_MINUTES}-{MAX_MINUTES} minutes")
    return m


# ------------------------------------------------------------------ sessions

def _finish_expired(conn, now):
    conn.execute("UPDATE focus_sessions SET end_ts = planned_end_ts, result = 'completed'"
                 " WHERE end_ts IS NULL AND planned_end_ts <= ?", (now,))
    conn.commit()


def current(conn, now=None):
    """The running session as the extension needs it, or None."""
    now = now or time.time()
    _finish_expired(conn, now)
    row = conn.execute("SELECT id, start_ts, planned_end_ts, label FROM focus_sessions"
                       " WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    s = settings()
    return {"id": row[0], "start": row[1], "end": row[2], "label": row[3], "block": s["block"],
            "adult": s["adult"] != "off"}


def start(conn, minutes=None, label="", now=None) -> dict:
    now = now or time.time()
    running = current(conn, now)
    if running:
        return running  # one at a time; starting again just returns the running one
    m = _minutes(minutes if minutes is not None else settings()["minutes"])
    conn.execute("INSERT INTO focus_sessions (start_ts, planned_end_ts, label) VALUES (?, ?, ?)",
                 (now, now + m * 60, str(label or "").strip()[:100]))
    conn.commit()
    return current(conn, now)


def stop(conn, now=None):
    """End the running session early. Returns the ended session's result, or None."""
    now = now or time.time()
    running = current(conn, now)
    if not running:
        return None
    result = "completed" if now >= running["end"] - DONE_GRACE else "stopped"
    conn.execute("UPDATE focus_sessions SET end_ts = ?, result = ? WHERE id = ?", (now, result, running["id"]))
    conn.commit()
    return result


def record_blocks(conn, items) -> int:
    """items from the extension: [{id, ts, domain}] = a blocked site was opened."""
    rows = []
    for it in items if isinstance(items, list) else []:
        try:
            ts = float(it["ts"])
            domain = normalize_domain(it["domain"])
            row = conn.execute(
                "SELECT id FROM focus_sessions WHERE start_ts <= ? AND COALESCE(end_ts, planned_end_ts) >= ?"
                " ORDER BY id DESC LIMIT 1", (ts + 5, ts - 5)).fetchone()
            if row:
                rows.append((row[0], ts, domain, str(it["id"])[:64]))
        except (KeyError, TypeError, ValueError):
            continue
    conn.executemany("INSERT OR IGNORE INTO focus_blocks (focus_id, ts, domain, client_id) VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def phone_block(conn, rules=None, now=None) -> list:
    """Android packages the phone app blocks during focus: every phone app your rules (or
    its name's keywords) put in the blocked categories, Entertainment and Social by default."""
    rules = rules or categorize.Rules()
    now = now or time.time()
    pkgs = {p for p, c in rules.phone_apps.items() if c in DEFAULT_BLOCK_CATEGORIES}
    for pkg, label in conn.execute(
            "SELECT lower(app), MAX(title) FROM sessions WHERE source = 'phone' AND end_ts > ? GROUP BY lower(app)",
            (now - 60 * 86400,)):
        if pkg not in rules.phone_apps and rules.classify_phone(pkg, label or "")[0] in DEFAULT_BLOCK_CATEGORIES:
            pkgs.add(pkg)
    return sorted(pkgs - set(categorize.IGNORED_PHONE_APPS))


def record_phone_sessions(conn, items, device: str, now=None) -> int:
    """Finished focus sessions the phone ran on its own while it couldn't reach the PC:
    [{id, start, planned_end, end, label}] (ms). Saved so they count in the focus stats and
    the apps blocked during them (record_phone_blocks) have a session to belong to.
    Skipped if a PC session already covers that time (its blocks go to that one)."""
    now = now or time.time()
    added = 0
    for it in (items if isinstance(items, list) else [])[:50]:
        try:
            cid = f"phone:{device}:{str(it['id'])[:60]}"
            start = float(it["start"]) / 1000
            planned = float(it["planned_end"]) / 1000
            end = min(float(it.get("end") or it["planned_end"]) / 1000, planned, now)
            label = str(it.get("label") or "")[:100].strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 < start < planned <= start + 12 * 3600) or end <= start:
            continue
        overlap = conn.execute(
            "SELECT 1 FROM focus_sessions WHERE start_ts < ? AND COALESCE(end_ts, planned_end_ts) > ?"
            " AND (client_id IS NULL OR client_id != ?)", (end, start, cid)).fetchone()
        if overlap:
            continue
        result = "completed" if end >= planned - DONE_GRACE else "stopped"
        cur = conn.execute(
            "INSERT OR IGNORE INTO focus_sessions (start_ts, planned_end_ts, end_ts, result, label, client_id)"
            " VALUES (?, ?, ?, ?, ?, ?)", (start, planned, end, result, label, cid))
        added += cur.rowcount
    conn.commit()
    return added


def record_phone_blocks(conn, items) -> int:
    """items from the phone app: [{id, ts, app}] = a blocked app was opened during focus."""
    rows = []
    for it in items if isinstance(items, list) else []:
        try:
            ts = float(it["ts"]) / 1000
            app = str(it["app"])[:60].strip()
            if not app:
                continue
            row = conn.execute(
                "SELECT id FROM focus_sessions WHERE start_ts <= ? AND COALESCE(end_ts, planned_end_ts) >= ?"
                " ORDER BY id DESC LIMIT 1", (ts + 5, ts - 5)).fetchone()
            if row:
                rows.append((row[0], ts, f"{app} (phone)", "phone:" + str(it["id"])[:60]))
        except (KeyError, TypeError, ValueError):
            continue
    conn.executemany("INSERT OR IGNORE INTO focus_blocks (focus_id, ts, domain, client_id) VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def history(conn, start_ts: float, end_ts: float, rules=None, now=None) -> dict:
    """Sessions that started in [start_ts, end_ts) with how they went."""
    now = now or time.time()
    _finish_expired(conn, now)
    rows = conn.execute(
        "SELECT id, start_ts, planned_end_ts, end_ts, result, label FROM focus_sessions"
        " WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts DESC", (start_ts, end_ts)).fetchall()
    blocks = {}
    for fid, domain, n in conn.execute(
            "SELECT focus_id, domain, COUNT(*) FROM focus_blocks WHERE focus_id IN"
            f" ({','.join(str(r[0]) for r in rows) or 'NULL'}) GROUP BY focus_id, domain ORDER BY 3 DESC"):
        blocks.setdefault(fid, []).append({"domain": domain, "count": n})

    rules = rules or categorize.Rules()
    items = categorize.categorise(conn, rows[-1][1], max(r[3] or now for r in rows), rules) if rows else []
    sessions = []
    for fid, s, planned, e, result, label in rows:
        end = e or now
        prod = active = 0.0
        for it in items:
            o = min(it.end, end) - max(it.start, s)
            if o > 0 and it.source != "manual":
                active += o
                if it.category in rules.productive:
                    prod += o
        sessions.append({
            "id": fid, "start": s, "planned_end": planned, "end": e, "label": label,
            "result": result or "running", "planned_seconds": round(planned - s), "seconds": round(end - s),
            "productive_seconds": round(prod), "active_seconds": round(active),
            "blocked": sum(b["count"] for b in blocks.get(fid, [])), "blocked_sites": blocks.get(fid, [])[:5],
        })
    done = [x for x in sessions if x["result"] != "running"]
    return {
        "sessions": sessions,
        "completed": sum(1 for x in done if x["result"] == "completed"),
        "stopped": sum(1 for x in done if x["result"] == "stopped"),
        "focused_seconds": sum(x["seconds"] for x in done),
        "productive_seconds": sum(x["productive_seconds"] for x in done),
        "blocked": sum(x["blocked"] for x in sessions),
    }
