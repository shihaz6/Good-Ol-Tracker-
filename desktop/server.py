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
  GET  /api/weekly/preview  -> exactly what a weekly summary would send to Groq
  POST /api/weekly          -> generate a summary of the last 7 days (dashboard only)
       Same-origin only: other websites can't read it (no CORS, Sec-Fetch-Site and
       Origin checks, Host check against DNS rebinding).
"""

import json
import logging
import secrets
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import ai_categorize
import analytics
import db
import keys
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
    """Data the server hands to the tracker thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._media_until = 0.0

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

        if path in ("/api/summary", "/api/ai", "/api/weekly/preview"):
            if not self._dashboard_ok(api=True):
                return self._send(403, {"error": "forbidden"})
            if path == "/api/summary":
                return self._summary(parse_qs(query))
            if path == "/api/ai":
                return self._ai_status()
            return self._weekly_preview()

        self._send(404, {"error": "not found"})

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
        # Triggers an outside API call, so only the dashboard page itself may do it:
        # an Origin header is required (browsers always send one on POST) and must be ours.
        if (self.headers.get("Host") not in ALLOWED_HOSTS
                or self.headers.get("Origin") not in SAME_ORIGINS
                or self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"):
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
    if not all(isinstance(x, list) for x in (visits, pages, searches)):
        raise ValueError("visits/pages/searches must be lists")
    conn = db.connect()
    try:
        n_visits = db.insert_browser_visits(conn, visits)
        n_pages = db.upsert_pages(conn, pages)
        n_searches = db.insert_searches(conn, searches)
    finally:
        conn.close()
    if "media" in payload:
        state.media_heartbeat(bool(payload["media"]))
    return {"visits": n_visits, "pages": n_pages, "searches": n_searches}


def start(token: str, state: SharedState) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    server.token = token
    server.state = state
    threading.Thread(target=server.serve_forever, name="ingest-server", daemon=True).start()
    return server
