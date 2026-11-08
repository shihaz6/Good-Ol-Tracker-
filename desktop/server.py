"""Local HTTP server the browser extension posts to.

Security:
  * bound to 127.0.0.1 only (not reachable from the network)
  * every request needs the token in data/token.txt (X-Tracker-Token header)
  * requests from web pages are refused (Origin must be a browser extension)
  * Host header must be 127.0.0.1/localhost (blocks DNS-rebinding tricks)

Endpoints:
  GET  /health  -> {"ok": true}            (used by the extension's options page)
  POST /ingest  -> {"visits": [...], "pages": [...], "media": bool}
"""

import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db

PORT = 47321
MEDIA_GRACE = 75          # a "media playing" heartbeat counts for this many seconds
MAX_BODY = 2_000_000      # bytes
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
EXTENSION_ORIGINS = ("chrome-extension://",)

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

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        self._send(200, {"ok": True})

    def do_POST(self):
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
    if not isinstance(visits, list) or not isinstance(pages, list):
        raise ValueError("visits/pages must be lists")
    conn = db.connect()
    try:
        n_visits = db.insert_browser_visits(conn, visits)
        n_pages = db.upsert_pages(conn, pages)
    finally:
        conn.close()
    if "media" in payload:
        state.media_heartbeat(bool(payload["media"]))
    return {"visits": n_visits, "pages": n_pages}


def start(token: str, state: SharedState) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    server.token = token
    server.state = state
    threading.Thread(target=server.serve_forever, name="ingest-server", daemon=True).start()
    return server
