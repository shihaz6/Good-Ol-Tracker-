"""Local HTTP server the browser extension posts to.

Security:
  * bound to 127.0.0.1 only (not reachable from the network)
  * every request needs the token in data/token.txt (X-Tracker-Token header)
  * requests from web pages are refused (Origin must be a browser extension)
  * Host header must be 127.0.0.1/localhost (blocks DNS-rebinding tricks)

Endpoints:
  GET  /health       -> {"ok": true}            (extension options page; needs token)
  POST /ingest       -> {"visits": [...], "pages": [...], "searches": [...], "media": bool}
  GET  /             -> dashboard page (desktop/dashboard/)
  GET  /api/summary?start=YYYY-MM-DD&end=YYYY-MM-DD -> dashboard data
  GET  /api/ai              -> AI status + latest weekly summary
  GET  /api/health          -> tracker / extension / AI / backup status
  GET  /api/weekly/preview  -> exactly what a weekly summary would send to Groq
  POST /api/weekly          -> generate a summary of the last 7 days (dashboard only)
  GET  /api/trends?end=&days=          -> per-day series + this week vs last week
  GET  /api/manual?start=&end=         -> offline time entries
  POST /api/manual {date,start,end,category,note} / POST /api/manual/delete {id}
  GET  /api/export?kind=activity|daily|searches&start=&end=  -> CSV download
  GET  /api/email                      -> email settings (never the password)
  POST /api/email/settings {...,password?} / POST /api/email/test
  POST /api/rules {changes:[{section,key,category}]} / POST /api/rules/undo / GET /api/rules/recent
  GET  /api/find?q=&meaning=1          -> search pages you've read + past searches
  GET  /api/focus / POST /api/focus/start {minutes,label} / /api/focus/stop / /api/focus/settings
  POST /focus {action:start|stop, minutes, label}  (extension popup; needs token)
       Same-origin only: other websites can't read it (no CORS, Sec-Fetch-Site and
       Origin checks, Host check against DNS rebinding).
"""

import csv
import io
import json
import logging
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import ai_categorize
import analytics
import backup
import categorize
import db
import find
import focus
import keys
import mailer
import rules_edit
import weekly

PORT = 47321
MEDIA_GRACE = 75          # a "media playing" heartbeat counts for this many seconds
MAX_BODY = 2_000_000      # bytes
MAX_RANGE_DAYS = 366
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
SAME_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
EXTENSION_ORIGINS = ("chrome-extension://",)

DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
}
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}

log = logging.getLogger("tracker")


class SharedState:
    """Data shared between the tracker loop and the server threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._media_until = 0.0
        self.started = time.time()
        self.last_tick = 0.0  # set by the desktop sampling loop every INTERVAL seconds

    def media_heartbeat(self, playing: bool) -> None:
        with self._lock:
            self._media_until = time.time() + MEDIA_GRACE if playing else 0.0

    def media_playing(self) -> bool:
        with self._lock:
            return time.time() < self._media_until


def load_token() -> str:
    path = db.DATA_DIR / "token.txt"
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="utf-8")
    return token


class Handler(BaseHTTPRequestHandler):
    server_version = "GoodOlTracker/0.2"

    def log_message(self, fmt, *args):  # silence default stderr logging
        pass

    # --- helpers ---
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        # No Origin = a local non-browser client (e.g. a test script); still needs the token.
        return origin is None or origin.startswith(EXTENSION_ORIGINS)

    def _authorized(self) -> bool:
        if self.headers.get("Host") not in ALLOWED_HOSTS or not self._origin_ok():
            return False
        return secrets.compare_digest(self.headers.get("X-Tracker-Token", ""), self.server.token)

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        origin = self.headers.get("Origin")
        if origin and origin.startswith(EXTENSION_ORIGINS):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    # --- verbs ---
    def do_OPTIONS(self):  # CORS preflight, only ever approved for extensions
        origin = self.headers.get("Origin", "")
        if not origin.startswith(EXTENSION_ORIGINS):
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Tracker-Token")
        self.end_headers()

    def _send_bytes(self, code: int, body: bytes, content_type: str, extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _dashboard_ok(self, api: bool) -> bool:
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in SAME_ORIGINS:
            return False
        # Data requests must come from the dashboard itself (or be typed in directly).
        return not api or self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"ok": True})

        if path in STATIC:
            if not self._dashboard_ok(api=False):
                return self._send(403, {"error": "forbidden"})
            name, ctype = STATIC[path]
            try:
                body = (DASHBOARD_DIR / name).read_bytes()
            except OSError:
                return self._send(404, {"error": "dashboard files missing"})
            return self._send_bytes(200, body, ctype, PAGE_HEADERS if name.endswith(".html") else None)

        routes = {
            "/api/summary": lambda: self._summary(parse_qs(query)),
            "/api/ai": self._ai_status,
            "/api/health": self._health,
            "/api/weekly/preview": self._weekly_preview,
            "/api/trends": lambda: self._trends(parse_qs(query)),
            "/api/manual": lambda: self._manual_list(parse_qs(query)),
            "/api/export": lambda: self._export(parse_qs(query)),
            "/api/email": lambda: self._json(200, mailer.status()),
            "/api/rules/recent": lambda: self._json(200, {"recent": rules_edit.recent()}),
            "/api/find": lambda: self._find(parse_qs(query)),
            "/api/focus": self._focus_status,
        }
        if path in routes:
            if not self._dashboard_ok(api=True):
                return self._send(403, {"error": "forbidden"})
            return routes[path]()

        self._send(404, {"error": "not found"})

    # --- helpers for dashboard API -------------------------------------------------

    def _json(self, code: int, obj) -> None:
        self._send_bytes(code, json.dumps(obj).encode(), "application/json")

    def _strict_same_origin(self) -> bool:
        """For anything that changes data or calls out (POST): only the dashboard page.
        Browsers always send Origin on POST, so it's required and must be ours."""
        return (self.headers.get("Host") in ALLOWED_HOSTS
                and self.headers.get("Origin") in SAME_ORIGINS
                and self.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 100_000:
            raise ValueError("request too large")
        data = json.loads(self.rfile.read(length) or b"{}") if length else {}
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    @staticmethod
    def _range(params, default_days=1):
        today = date.today()
        end = date.fromisoformat(params.get("end", [today.isoformat()])[0])
        start = date.fromisoformat(params.get("start", [(end - timedelta(days=default_days - 1)).isoformat()])[0])
        if end < start:
            start, end = end, start
        if (end - start).days >= MAX_RANGE_DAYS:
            start = end - timedelta(days=MAX_RANGE_DAYS - 1)
        return start, end

    # --- trends / offline time / export / email -------------------------------------

    def _trends(self, params):
        try:
            end = date.fromisoformat(params.get("end", [date.today().isoformat()])[0])
            days = max(7, min(90, int(params.get("days", ["30"])[0])))
        except ValueError:
            return self._send(400, {"error": "bad end/days"})
        conn = db.connect()
        try:
            return self._json(200, analytics.trends(conn, end, days))
        except ValueError as exc:
            return self._send(500, {"error": f"categories.json: {exc}"})
        finally:
            conn.close()

    def _manual_list(self, params):
        try:
            start, end = self._range(params, default_days=30)
        except ValueError:
            return self._send(400, {"error": "start/end must be YYYY-MM-DD"})
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT id, start_ts, end_ts, category, note FROM manual_entries WHERE end_ts > ? AND start_ts < ?"
                " ORDER BY start_ts DESC LIMIT 200",
                (analytics._midnight(start), analytics._midnight(end + timedelta(days=1)))).fetchall()
        finally:
            conn.close()
        return self._json(200, {"entries": [dict(zip(("id", "start", "end", "category", "note"), r)) for r in rows]})

    def _manual_add(self, body):
        day = date.fromisoformat(str(body.get("date", "")))
        t0 = datetime.strptime(str(body.get("start", "")), "%H:%M").time()
        t1 = datetime.strptime(str(body.get("end", "")), "%H:%M").time()
        start = datetime.combine(day, t0)
        end = datetime.combine(day, t1)
        if end <= start:
            end += timedelta(days=1)  # e.g. 23:00 -> 01:00
        conn = db.connect()
        try:
            rules = categorize.Rules()
            entry_id = db.add_manual_entry(conn, start.timestamp(), end.timestamp(),
                                           str(body.get("category", "")), body.get("note", ""), rules.categories)
        finally:
            conn.close()
        return self._json(200, {"ok": True, "id": entry_id})

    def _manual_delete(self, body):
        conn = db.connect()
        try:
            ok = db.delete_manual_entry(conn, int(body.get("id")))
        finally:
            conn.close()
        return self._json(200 if ok else 404, {"ok": ok})

    def _export(self, params):
        kind = params.get("kind", ["activity"])[0]
        try:
            start, end = self._range(params)
        except ValueError:
            return self._send(400, {"error": "start/end must be YYYY-MM-DD"})
        conn = db.connect()
        try:
            header, rows = analytics.export_rows(conn, kind, start, end)
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        finally:
            conn.close()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        writer.writerows(rows)
        name = f"good-ol-tracker-{kind}-{start.isoformat()}" + ("" if start == end else f"-to-{end.isoformat()}")
        return self._send_bytes(200, buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                                {"Content-Disposition": f'attachment; filename="{name}.csv"'})

    def _email_settings(self, body):
        password = body.pop("password", "")
        settings = mailer.save_settings(body)
        if password:
            mailer.save_password(password)
        return self._json(200, {"ok": True, **mailer.status(), **settings})

    def _email_test(self, _body):
        conn = db.connect()
        try:
            to = mailer.send_test(conn)
        finally:
            conn.close()
        return self._json(200, {"ok": True, "to": to})

    # --- rules / search / focus ----------------------------------------------------

    def _rules_set(self, body):
        return self._json(200, rules_edit.set_rules(body.get("changes")))

    def _rules_undo(self, _body):
        return self._json(200, rules_edit.undo())

    def _find(self, params):
        q = params.get("q", [""])[0]
        meaning = params.get("meaning", ["1"])[0] != "0"
        conn = db.connect()
        try:
            return self._json(200, find.search(conn, q, meaning))
        finally:
            conn.close()

    def _focus_status(self):
        conn = db.connect()
        try:
            now = time.time()
            data = {"now": now, "current": focus.current(conn, now), "settings": focus.settings(),
                    "history": focus.history(conn, now - 14 * 86400, now + 1, now=now)}
        except ValueError as exc:
            return self._send(500, {"error": f"categories.json: {exc}"})
        finally:
            conn.close()
        return self._json(200, data)

    @staticmethod
    def _focus_do(body) -> dict:
        """Shared by the dashboard (POST /api/focus/start|stop) and the extension (POST /focus)."""
        action = body.get("action")
        conn = db.connect()
        try:
            if action == "status":
                return {"ok": True, "focus": focus.current(conn)}
            if action == "start":
                cur = focus.start(conn, body.get("minutes"), body.get("label", ""))
                log.info("focus session started: %s min", round((cur["end"] - cur["start"]) / 60))
                return {"ok": True, "focus": cur}
            if action == "stop":
                result = focus.stop(conn)
                log.info("focus session ended: %s", result)
                return {"ok": True, "result": result, "focus": None}
            raise ValueError("action must be start, stop or status")
        finally:
            conn.close()

    def _focus_settings(self, body):
        return self._json(200, {"ok": True, "settings": focus.save_settings(body)})

    def _ai_status(self):
        conn = db.connect()
        try:
            data = {
                "voyage_key": bool(keys.load_key("voyage")),
                "groq_key": bool(keys.load_key("groq")),
                "categoriser": {**ai_categorize.counts(conn), **ai_categorize.status},
                "summary": weekly.latest(conn),
                "default_range": [d.isoformat() for d in weekly.last_7_days()],
            }
        finally:
            conn.close()
        self._send_bytes(200, json.dumps(data).encode(), "application/json")

    def _health(self):
        state = self.server.state
        conn = db.connect()
        try:
            client = db.get_meta(conn, "extension_client")
            seen = db.get_meta(conn, "extension_last_seen")
            data = {
                "now": time.time(),
                "tracker": {"started": state.started, "last_tick": state.last_tick or None},
                "extension": {"last_seen": float(seen) if seen else None,
                              **(json.loads(client) if client else {})},
                "ai": {"voyage_key": bool(keys.load_key("voyage")), "groq_key": bool(keys.load_key("groq")),
                       "last_run": ai_categorize.status["last_run"],
                       "last_error": ai_categorize.status["last_error"]},
                "backups": backup.status(conn),
                "db_mb": round(db.DB_PATH.stat().st_size / 1e6, 1) if db.DB_PATH.exists() else 0,
            }
        finally:
            conn.close()
        self._send_bytes(200, json.dumps(data).encode(), "application/json")

    def _weekly_preview(self):
        start, end = weekly.last_7_days()
        conn = db.connect()
        try:
            payload = weekly.build_payload(conn, start, end)
        except ValueError as exc:
            return self._send(500, {"error": f"categories.json: {exc}"})
        finally:
            conn.close()
        self._send_bytes(200, json.dumps(payload).encode(), "application/json")

    def _weekly_generate(self):
        # Triggers an outside API call, so only the dashboard page itself may do it.
        if not self._strict_same_origin():
            return self._send(403, {"error": "forbidden"})
        start, end = weekly.last_7_days()
        conn = db.connect()
        try:
            summary = weekly.generate(conn, start, end)
        except weekly.SummaryError as exc:
            return self._send(400, {"error": str(exc)})
        except ValueError as exc:
            return self._send(500, {"error": f"categories.json: {exc}"})
        finally:
            conn.close()
        self._send_bytes(200, json.dumps(summary).encode(), "application/json")

    def _summary(self, params):
        try:
            today = date.today()
            start = date.fromisoformat(params.get("start", [today.isoformat()])[0])
            end = date.fromisoformat(params.get("end", [start.isoformat()])[0])
            if end < start:
                start, end = end, start
            if (end - start).days >= MAX_RANGE_DAYS:
                start = end - timedelta(days=MAX_RANGE_DAYS - 1)
        except ValueError:
            return self._send(400, {"error": "start/end must be YYYY-MM-DD"})
        conn = db.connect()
        try:
            data = analytics.summary(conn, start, end)
        except ValueError as exc:  # e.g. a typo in categories.json
            log.warning("summary failed: %r", exc)
            return self._send(500, {"error": f"categories.json: {exc}"})
        finally:
            conn.close()
        self._send_bytes(200, json.dumps(data).encode(), "application/json")

    def do_POST(self):
        if self.path == "/api/weekly":
            return self._weekly_generate()
        posts = {
            "/api/manual": self._manual_add,
            "/api/manual/delete": self._manual_delete,
            "/api/email/settings": self._email_settings,
            "/api/email/test": self._email_test,
            "/api/rules": self._rules_set,
            "/api/rules/undo": self._rules_undo,
            "/api/focus/start": lambda b: self._json(200, self._focus_do({**b, "action": "start"})),
            "/api/focus/stop": lambda b: self._json(200, self._focus_do({"action": "stop"})),
            "/api/focus/settings": self._focus_settings,
        }
        if self.path in posts:
            if not self._strict_same_origin():
                return self._send(403, {"error": "forbidden"})
            try:
                return posts[self.path](self._read_json())
            except mailer.MailError as exc:
                return self._send(400, {"error": str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                return self._send(400, {"error": str(exc) or "bad request"})
        if self.path == "/focus":  # extension popup / blocked page
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            try:
                return self._send(200, self._focus_do(self._read_json()))
            except (ValueError, TypeError, KeyError) as exc:
                return self._send(400, {"error": str(exc) or "bad request"})
        if self.path != "/ingest":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": "bad body size"})
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            counts = ingest(payload, self.server.state)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("rejected ingest: %r", exc)
            return self._send(400, {"error": str(exc)})
        self._send(200, {"ok": True, **counts})


def ingest(payload: dict, state: SharedState) -> dict:
    visits = payload.get("visits") or []
    pages = payload.get("pages") or []
    searches = payload.get("searches") or []
    blocks = payload.get("focus_blocks") or []
    if not all(isinstance(x, list) for x in (visits, pages, searches, blocks)):
        raise ValueError("visits/pages/searches/focus_blocks must be lists")
    client = payload.get("client")
    conn = db.connect()
    try:
        n_visits = db.insert_browser_visits(conn, visits)
        n_pages = db.upsert_pages(conn, pages)
        n_searches = db.insert_searches(conn, searches)
        focus.record_blocks(conn, blocks)
        current_focus = focus.current(conn)  # the reply tells the extension what to block
        # Health check: remember when the extension last checked in and its settings.
        db.set_meta(conn, "extension_last_seen", time.time())
        if isinstance(client, dict):
            info = {k: client.get(k) for k in ("version", "all_sites", "paused", "queued")}
            db.set_meta(conn, "extension_client", json.dumps(info)[:500])
    finally:
        conn.close()
    if "media" in payload:
        state.media_heartbeat(bool(payload["media"]))
    return {"visits": n_visits, "pages": n_pages, "searches": n_searches, "focus": current_focus}


def start(token: str, state: SharedState) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    server.token = token
    server.state = state
    threading.Thread(target=server.serve_forever, name="ingest-server", daemon=True).start()
    return server
