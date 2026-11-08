"""Phone (Android companion app, android/): pairing, discovery and sync.

The phone reads Android's own app-usage history and sends it here every ~15 minutes
whenever it can reach the PC (your phone's hotspot or any Wi-Fi you share). Android keeps
about a week of history, so nothing is lost while the PC is off; the app catches up.

Network (the only part of the tracker reachable from other devices):
  UDP  DISCOVERY_PORT  "where is the tracker?" -> address + port. Once a phone is paired,
                       the answer proves it knows the phone's token (HMAC of the phone's
                       random nonce), so another PC on the same network can't pose as yours.
  TCP  PHONE_PORT      /phone/pair, /phone/pair/status, /phone/sync. Nothing else (the
                       dashboard and extension API stay on 127.0.0.1 only).
  Requests are refused unless they come from a private address (192.168.x, 10.x, ...).
  Windows only lets them in on networks set to "Private" (phone_firewall.ps1).

Pairing: the phone asks with a 4-digit code it shows on its screen; the dashboard shows
the same code with Allow / Deny. Allowing hands the phone its own random token.
What the phone sends: which app was in front from when to when, its name, and unlock times.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from datetime import date, datetime

import categorize
import db
import focus
import nudge
import wellbeing

log =logging.getLogger("tracker")

PHONE_PORT = 47323
DISCOVERY_PORT = 47324
PAIR_TTL = 300            # a pairing request waits this long for Allow/Deny
MAX_PENDING = 3
MAX_BODY = 4_000_000
MAX_SESSIONS = 2000       # per sync request
MAX_SESSION_SECONDS = 12 * 3600

_lock = threading.Lock()
_pending = {}             # request id -> {device, name, code, ip, ts, status, token}


class PhoneError(ValueError):
    pass


# --------------------------------------------------------------------- devices

def _path():
    return db.DATA_DIR / "phone_devices.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("pc_id", secrets.token_hex(8))
    data.setdefault("devices", {})
    return data


def _save(data) -> None:
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_path())


def _device_for_token(token: str):
    if not token:
        return None, None
    data = _load()
    for dev_id, d in data["devices"].items():
        if secrets.compare_digest(d.get("token", ""), token):
            return dev_id, d
    return None, None


def _touch(dev_id, **fields) -> None:
    with _lock:
        data = _load()
        if dev_id in data["devices"]:
            data["devices"][dev_id].update(fields)
            _save(data)


def devices() -> list:
    return [{"id": k, "name": d.get("name"), "paired": d.get("paired"), "last_sync": d.get("last_sync"),
             "last_ip": d.get("last_ip"), "version": d.get("version"), "behind": d.get("behind"),
             "adult_filter": d.get("adult_filter"), "blocking": d.get("blocking")}
            for k, d in _load()["devices"].items()]


def unpair(dev_id: str) -> None:
    with _lock:
        data = _load()
        if data["devices"].pop(dev_id, None) is None:
            raise PhoneError("no such phone")
        _save(data)


# --------------------------------------------------------------------- pairing

def _clean_device_id(value) -> str:
    v = str(value or "")
    if not 8 <= len(v) <= 64 or not all(c.isalnum() or c in "-_" for c in v):
        raise PhoneError("bad device id")
    return v


def pair_request(body: dict, ip: str, now=None) -> dict:
    now = now or time.time()
    device = _clean_device_id(body.get("device"))
    name = str(body.get("name") or "Android phone")[:60]
    code = str(body.get("code") or "")
    if not (len(code) == 4 and code.isdigit()):
        raise PhoneError("code must be 4 digits")
    with _lock:
        for rid in [r for r, p in _pending.items() if now - p["ts"] > PAIR_TTL]:
            del _pending[rid]
        if sum(1 for p in _pending.values() if p["status"] == "waiting") >= MAX_PENDING:
            raise PhoneError("too many pairing requests; allow or deny them on the dashboard first")
        rid = secrets.token_urlsafe(12)
        _pending[rid] = {"device": device, "name": name, "code": code, "ip": ip, "ts": now,
                         "status": "waiting", "token": None}
    log.info("phone pairing requested by %s (%s)", name, ip)
    return {"request": rid, "expires_in": PAIR_TTL}


def pair_status(rid: str, device: str) -> dict:
    with _lock:
        p = _pending.get(rid)
        if not p or p["device"] != device or time.time() - p["ts"] > PAIR_TTL:
            return {"status": "expired"}
        if p["status"] == "allowed":
            del _pending[rid]  # the token is handed over exactly once
            return {"status": "allowed", "token": p["token"], "pc_id": _load()["pc_id"]}
        if p["status"] == "denied":
            del _pending[rid]
        return {"status": p["status"]}


def pending() -> list:
    now = time.time()
    with _lock:
        return [{"id": r, "name": p["name"], "code": p["code"], "ip": p["ip"], "age": round(now - p["ts"])}
                for r, p in _pending.items() if p["status"] == "waiting" and now - p["ts"] <= PAIR_TTL]


def decide(rid: str, allow: bool, now=None) -> dict:
    now = now or time.time()
    with _lock:
        p = _pending.get(rid)
        if not p or p["status"] != "waiting" or now - p["ts"] > PAIR_TTL:
            raise PhoneError("that pairing request has expired; start again on the phone")
        if not allow:
            p["status"] = "denied"
            return {"ok": True}
        token = secrets.token_urlsafe(32)
        data = _load()
        data["devices"][p["device"]] = {"name": p["name"], "token": token, "paired": now,
                                        "last_sync": None, "last_ip": p["ip"]}
        _save(data)
        p["status"], p["token"] = "allowed", token
    log.info("phone paired: %s", p["name"])
    return {"ok": True, "device": p["device"]}


# --------------------------------------------------------------------- sync

def _num(v, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise PhoneError(f"{name} must be a number") from None
    if f != f or f in (float("inf"), float("-inf")):
        raise PhoneError(f"{name} must be a number")
    return f


def sync(conn, dev_id: str, body: dict, now=None) -> dict:
    """body: {sessions: [{pkg, label, start, end}] (ms), unlocks: [ms], client: {...}}"""
    now = now or time.time()
    sessions = body.get("sessions") or []
    unlocks = body.get("unlocks") or []
    if not isinstance(sessions, list) or not isinstance(unlocks, list):
        raise PhoneError("sessions/unlocks must be lists")
    if len(sessions) > MAX_SESSIONS or len(unlocks) > 5 * MAX_SESSIONS:
        raise PhoneError("too much in one request")
    rows = []
    for s in sessions:
        if not isinstance(s, dict):
            raise PhoneError("each session must be an object")
        pkg = str(s.get("pkg") or "")[:150]
        if not pkg or any(ord(c) < 33 for c in pkg):
            raise PhoneError("bad package name")
        label = str(s.get("label") or pkg)[:100]
        start, end = _num(s.get("start"), "start") / 1000, _num(s.get("end"), "end") / 1000
        if not (0 < start < end <= now + 120) or end - start > MAX_SESSION_SECONDS:
            continue  # clock oddities: skip that one row, keep the rest
        rows.append((start, end, pkg, label, f"phone:{dev_id}:{pkg}@{int(round(start * 1000))}"))
    # The phone re-sends the session that was still running last time: keep the longest end.
    conn.executemany(
        "INSERT INTO sessions (start_ts, end_ts, source, app, title, client_id) VALUES (?, ?, 'phone', ?, ?, ?)"
        " ON CONFLICT(client_id) DO UPDATE SET end_ts = MAX(end_ts, excluded.end_ts), title = excluded.title",
        rows)
    u_rows = []
    for u in unlocks:
        ts = _num(u, "unlock") / 1000
        if 0 < ts <= now + 120:
            u_rows.append((dev_id, ts))
    conn.executemany("INSERT OR IGNORE INTO phone_unlocks (device, ts) VALUES (?, ?)", u_rows)
    conn.commit()
    focus.record_phone_blocks(conn, body.get("focus_blocks") or [])
    client = body.get("client") if isinstance(body.get("client"), dict) else {}
    _touch(dev_id, last_sync=now, version=str(client.get("version", ""))[:20],
           behind=client.get("behind") if isinstance(client.get("behind"), (int, float)) else None,
           adult_filter=str(client.get("private_dns") or "")[:100] or None,
           blocking=bool(client.get("blocking")))
    return {"ok": True, "sessions": len(rows), "unlocks": len(u_rows), **state_for_phone(conn, now)}


def state_for_phone(conn, now=None) -> dict:
    """What the phone needs back on every sync: the running focus session (if any), which
    apps to block during it, and a distraction alert to show (if it's for the phone)."""
    now = now or time.time()
    cur = focus.current(conn, now)
    return {"focus": {k: cur[k] for k in ("id", "start", "end", "label")} if cur else None,
            "phone_block": focus.phone_block(conn, now=now),
            "nudge": nudge.take("phone"),
            "wellbeing": wellbeing.for_phone(),   # app limits, bedtime, scheduled focus
            "limit_pc_used": wellbeing.pc_used_for_phone(conn, now),  # shared budget: PC site time
            "today": _today(conn, now)}           # for the app's screen + home-screen widget


_today_cache = {"ts": 0.0, "value": None}


def _today(conn, now):
    """Today's productive time + goals (cached for a minute: the phone asks every minute)."""
    if now - _today_cache["ts"] < 60 and _today_cache["value"] is not None:
        return _today_cache["value"]
    g = wellbeing.goals(conn, now=now)
    value = {"productive": next((x["today"] for x in g if x["kind"] == "productive_min"), None),
             "goals": [{k: x[k] for k in ("label", "today", "target", "met_today", "over", "streak", "kind")} for x in g]}
    if value["productive"] is None:
        rules = categorize.Rules()
        d0 = datetime.combine(date.fromtimestamp(now), datetime.min.time()).timestamp()
        value["productive"] = round(sum(it.seconds for it in categorize.categorise(conn, d0, now, rules)
                                        if it.category in rules.productive))
    _today_cache.update(ts=now, value=value)
    return value


def focus_action(conn, body: dict, now=None) -> dict:
    """The phone's Start focus / End early buttons (and the alert's Start focus button)."""
    action = body.get("action")
    if action == "start":
        focus.start(conn, body.get("minutes") or 25, str(body.get("label") or "")[:100], now=now)
        log.info("focus session started from the phone")
    elif action == "stop":
        result = focus.stop(conn, now=now)
        log.info("focus session ended from the phone: %s", result)
    elif action == "snooze":  # distraction alert's Snooze button
        nudge.snooze(body.get("minutes"), now=now)
    elif action != "status":
        raise PhoneError("action must be start, stop, snooze or status")
    return {"ok": True, **state_for_phone(conn, now)}


# --------------------------------------------------------------------- network

def _private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return (a.is_private or a.is_link_local) and not a.is_loopback and not a.is_unspecified


class PhoneHandler(BaseHTTPRequestHandler):
    server_version = "GoodOlTracker-Phone/1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ip(self):
        return self.client_address[0]

    def _allowed(self):
        if self.server.allow_loopback_for_tests and self._ip() == "127.0.0.1":
            return True
        return _private(self._ip())

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= MAX_BODY:
            raise PhoneError("bad body size")
        data = json.loads(self.rfile.read(n))
        if not isinstance(data, dict):
            raise PhoneError("expected a JSON object")
        return data

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        url = urlsplit(self.path)
        if url.path == "/phone/pair/status":
            q = parse_qs(url.query)
            return self._send(200, pair_status(q.get("request", [""])[0], q.get("device", [""])[0]))
        if url.path == "/phone/hello":  # the app's "is my token still good?" check
            dev_id, d = _device_for_token(self.headers.get("X-Phone-Token", ""))
            if not dev_id:
                return self._send(401, {"error": "not paired"})
            return self._send(200, {"ok": True, "name": d.get("name"), "pc_id": _load()["pc_id"]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        try:
            if self.path == "/phone/pair":
                return self._send(200, pair_request(self._body(), self._ip()))
            if self.path == "/phone/sync":
                dev_id, _ = _device_for_token(self.headers.get("X-Phone-Token", ""))
                if not dev_id:
                    return self._send(401, {"error": "not paired"})
                body = self._body()
                conn = db.connect()
                try:
                    result = sync(conn, dev_id, body)
                finally:
                    conn.close()
                _touch(dev_id, last_ip=self._ip())
                return self._send(200, result)
            if self.path == "/phone/focus":
                dev_id, _ = _device_for_token(self.headers.get("X-Phone-Token", ""))
                if not dev_id:
                    return self._send(401, {"error": "not paired"})
                body = self._body()
                conn = db.connect()
                try:
                    return self._send(200, focus_action(conn, body))
                finally:
                    conn.close()
        except (PhoneError, ValueError, TypeError) as exc:
            return self._send(400, {"error": str(exc) or "bad request"})
        self._send(404, {"error": "not found"})


def discovery_reply(msg: bytes):
    """UDP: {"t":"got-discover","nonce":..,"device":..} -> reply bytes (or None to ignore)."""
    try:
        req = json.loads(msg.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(req, dict) or req.get("t") != "got-discover":
        return None
    nonce = str(req.get("nonce") or "")[:64]
    device = str(req.get("device") or "")[:64]
    data = _load()
    d = data["devices"].get(device)
    proof = hmac.new(d["token"].encode(), nonce.encode(), hashlib.sha256).hexdigest() if d and nonce else None
    return json.dumps({"t": "got-here", "port": PHONE_PORT, "pc": data["pc_id"],
                       "name": socket.gethostname()[:60], "proof": proof}).encode()


def _udp_loop(sock):
    while True:
        try:
            msg, addr = sock.recvfrom(2048)
            if not _private(addr[0]):
                continue
            reply = discovery_reply(msg)
            if reply:
                sock.sendto(reply, addr)
        except OSError:
            time.sleep(1)
        except Exception:
            log.exception("phone discovery failed")


def start(allow_loopback_for_tests=False):
    """Start the phone server + discovery listener (tracker.py). Never fatal."""
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", PHONE_PORT), PhoneHandler)
        httpd.daemon_threads = True
        httpd.allow_loopback_for_tests = allow_loopback_for_tests
        threading.Thread(target=httpd.serve_forever, name="phone-server", daemon=True).start()
    except OSError as exc:
        log.warning("phone server not started: %s", exc)
        return None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
        threading.Thread(target=_udp_loop, args=(sock,), name="phone-discovery", daemon=True).start()
    except OSError as exc:
        log.warning("phone discovery not started: %s", exc)
    log.info("phone sync listening on port %d (discovery %d)", PHONE_PORT, DISCOVERY_PORT)
    return httpd


def status() -> dict:
    return {"devices": devices(), "pending": pending(), "port": PHONE_PORT}
