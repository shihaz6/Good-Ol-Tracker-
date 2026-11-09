"""Phone sync (phone.py) + phone time in categories/analytics. Uses a real local socket
for the HTTP part (loopback allowed only in test mode).
Run:  python -m unittest discover -s tests -v"""

import hashlib
import hmac
import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import analytics  # noqa: E402
import categorize  # noqa: E402
import db  # noqa: E402
import phone  # noqa: E402
import rules_edit  # noqa: E402

DAY = date(2026, 11, 2)
T0 = datetime.combine(DAY, datetime.min.time()).timestamp()


def at(hour, minute=0):
    return T0 + hour * 3600 + minute * 60


def ms(ts):
    return int(ts * 1000)


class PhoneBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._data = db.DATA_DIR
        db.DATA_DIR = Path(self.tmp.name)
        phone._pending.clear()
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)  # HttpTest uses it from the server thread
        self.conn.executescript(db.SCHEMA)
        db._migrate(self.conn)

    def tearDown(self):
        db.DATA_DIR = self._data
        self.tmp.cleanup()

    def pair(self, device="redmi15c-abcdef12"):
        r = phone.pair_request({"device": device, "name": "Redmi 15C", "code": "4821"}, "192.168.43.1")
        self.assertEqual(phone.pending()[0]["code"], "4821")
        phone.decide(r["request"], True)
        got = phone.pair_status(r["request"], device)
        self.assertEqual(got["status"], "allowed")
        return device, got["token"]


class PairingTest(PhoneBase):
    def test_allow_hands_out_the_token_once(self):
        r = phone.pair_request({"device": "dev-12345678", "name": "Redmi", "code": "1234"}, "192.168.43.1")
        self.assertEqual(phone.pair_status(r["request"], "dev-12345678"), {"status": "waiting"})
        self.assertEqual(phone.pair_status(r["request"], "someone-else"), {"status": "expired"})
        phone.decide(r["request"], True)
        first = phone.pair_status(r["request"], "dev-12345678")
        self.assertEqual(first["status"], "allowed")
        self.assertTrue(len(first["token"]) > 30)
        self.assertEqual(phone.pair_status(r["request"], "dev-12345678")["status"], "expired")
        self.assertEqual(phone.devices()[0]["name"], "Redmi")

    def test_deny_and_limits(self):
        r = phone.pair_request({"device": "dev-12345678", "code": "1234"}, "10.0.0.2")
        phone.decide(r["request"], False)
        self.assertEqual(phone.pair_status(r["request"], "dev-12345678")["status"], "denied")
        with self.assertRaises(ValueError):
            phone.decide(r["request"], True)
        for bad in ({"device": "x", "code": "1234"}, {"device": "dev-12345678", "code": "12a4"}):
            with self.assertRaises(ValueError):
                phone.pair_request(bad, "10.0.0.2")
        for i in range(phone.MAX_PENDING):
            phone.pair_request({"device": f"dev-1234567{i}", "code": "1111"}, "10.0.0.2")
        with self.assertRaises(ValueError):
            phone.pair_request({"device": "dev-99999999", "code": "1111"}, "10.0.0.2")

    def test_discovery_proves_it_knows_the_token(self):
        device, token = self.pair()
        reply = json.loads(phone.discovery_reply(json.dumps(
            {"t": "got-discover", "nonce": "n0nce", "device": device}).encode()))
        self.assertEqual(reply["port"], phone.PHONE_PORT)
        self.assertEqual(reply["proof"], hmac.new(token.encode(), b"n0nce", hashlib.sha256).hexdigest())
        unknown = json.loads(phone.discovery_reply(b'{"t":"got-discover","nonce":"x","device":"nobody"}'))
        self.assertIsNone(unknown["proof"])
        self.assertIsNone(phone.discovery_reply(b"garbage"))

    def test_private_addresses_only(self):
        for ip in ("192.168.43.12", "10.1.2.3", "172.16.0.5", "fe80::1"):
            self.assertTrue(phone._private(ip), ip)
        for ip in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "nonsense"):
            self.assertFalse(phone._private(ip), ip)


class SyncTest(PhoneBase):
    def test_sessions_are_upserted_and_unlocks_deduped(self):
        device, _ = self.pair()
        s = {"pkg": "com.instagram.android", "label": "Instagram", "start": ms(at(9)), "end": ms(at(9, 10))}
        r = phone.sync(self.conn, device, {"sessions": [s], "unlocks": [ms(at(9))]}, now=at(12))
        self.assertEqual({k: r[k] for k in ("ok", "sessions", "unlocks")}, {"ok": True, "sessions": 1, "unlocks": 1})
        self.assertIn("phone_block", r)  # + focus state and alert for the phone
        # the same session again, now longer (it was still running) + the same unlock
        phone.sync(self.conn, device, {"sessions": [{**s, "end": ms(at(9, 25))}], "unlocks": [ms(at(9))]}, now=at(12))
        rows = self.conn.execute("SELECT start_ts, end_ts, app, title FROM sessions WHERE source = 'phone'").fetchall()
        self.assertEqual(rows, [(at(9), at(9, 25), "com.instagram.android", "Instagram")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM phone_unlocks").fetchone()[0], 1)
        # a shorter re-send never shrinks it
        phone.sync(self.conn, device, {"sessions": [s]}, now=at(12))
        self.assertEqual(self.conn.execute("SELECT end_ts FROM sessions WHERE source='phone'").fetchone()[0], at(9, 25))

    def test_phone_only_focus_session_keeps_its_blocks(self):
        device, _ = self.pair()
        local = {"id": "local-1", "start": ms(at(9)), "planned_end": ms(at(9, 25)), "end": ms(at(9, 20)), "label": "Maths"}
        block = {"id": "com.instagram.android@1", "ts": ms(at(9, 5)), "app": "Instagram"}
        phone.sync(self.conn, device, {"local_focus": [local], "focus_blocks": [block]}, now=at(12))
        phone.sync(self.conn, device, {"local_focus": [local], "focus_blocks": [block]}, now=at(12))  # re-sent
        rows = self.conn.execute("SELECT start_ts, end_ts, result, label FROM focus_sessions").fetchall()
        self.assertEqual(rows, [(at(9), at(9, 20), "stopped", "Maths")])
        self.assertEqual(self.conn.execute("SELECT domain FROM focus_blocks").fetchall(), [("Instagram (phone)",)])
        # one that overlaps a session the PC already has isn't added twice
        phone.sync(self.conn, device, {"local_focus": [{**local, "id": "local-2"}]}, now=at(12))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM focus_sessions").fetchone()[0], 1)

    def test_bad_rows(self):
        device, _ = self.pair()
        future = {"pkg": "a.b", "start": ms(at(13)), "end": ms(at(14))}
        backwards = {"pkg": "a.b", "start": ms(at(10)), "end": ms(at(9))}
        self.assertEqual(phone.sync(self.conn, device, {"sessions": [future, backwards]}, now=at(12))["sessions"], 0)
        with self.assertRaises(ValueError):
            phone.sync(self.conn, device, {"sessions": [{"pkg": "", "start": 1, "end": 2}]})
        with self.assertRaises(ValueError):
            phone.sync(self.conn, device, {"sessions": "nope"})


class HttpTest(PhoneBase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = phone.ThreadingHTTPServer(("127.0.0.1", 0), phone.PhoneHandler)
        cls.httpd.allow_loopback_for_tests = True
        cls.httpd.daemon_threads = True
        import threading
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, path, body=None, token=None):
        r = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json", **({"X-Phone-Token": token} if token else {})})
        try:
            with urllib.request.urlopen(r, timeout=5) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        code, r = self.req("/phone/pair", {"device": "dev-12345678", "name": "Redmi", "code": "4821"})
        self.assertEqual(code, 200)
        phone.decide(r["request"], True)
        code, st = self.req(f"/phone/pair/status?request={r['request']}&device=dev-12345678")
        token = st["token"]
        self.assertEqual(self.req("/phone/hello", token=token)[0], 200)
        self.assertEqual(self.req("/phone/hello", token="wrong")[0], 401)
        self.assertEqual(self.req("/phone/sync", {"sessions": []}, token="wrong")[0], 401)
        orig = db.connect
        db.connect = lambda: Wrap(self.conn)
        try:
            code, res = self.req("/phone/sync", {"sessions": [
                {"pkg": "com.whatsapp", "label": "WhatsApp", "start": ms(at(9)), "end": ms(at(9, 1))}]}, token=token)
        finally:
            db.connect = orig
        self.assertEqual((code, res["sessions"]), (200, 1))
        self.assertEqual(self.req("/api/summary")[0], 404)  # nothing else is reachable here


class Wrap:
    def __init__(self, c): self.c = c
    def __getattr__(self, n): return getattr(self.c, n)
    def close(self): pass


class PhoneTimeTest(PhoneBase):
    def setUp(self):
        super().setUp()
        c = self.conn
        # PC: VS Code 9:00-10:00. Phone: Instagram 9:20-9:30 (PC sat untouched) + an unknown app.
        c.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title) VALUES (?, ?, 'desktop', 'Code.exe', 'x')",
                  (at(9), at(10)))
        c.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, client_id) VALUES (?, ?, 'phone',"
                  " 'com.instagram.android', 'Instagram', 'p1')", (at(9, 20), at(9, 30)))
        c.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, client_id) VALUES (?, ?, 'phone',"
                  " 'com.example.weird', 'Weird App', 'p2')", (at(11), at(11, 5)))
        c.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, client_id) VALUES (?, ?, 'phone',"
                  " 'com.android.deskclock', 'Clock', 'p3')", (at(1), at(7)))  # alarm screen: ignored
        c.execute("INSERT INTO phone_unlocks VALUES ('d', ?)", (at(9, 20),))
        c.commit()
        self.rules = categorize.Rules()

    def test_overlap_counts_once_as_phone(self):
        items = categorize.categorise(self.conn, at(0), at(24), self.rules)
        coding = sum(it.seconds for it in items if it.category == "Coding")
        social = sum(it.seconds for it in items if it.category == "Social")
        self.assertEqual((coding, social), (50 * 60, 10 * 60))
        spans = sorted((it.start, it.end) for it in items)
        self.assertTrue(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])))  # no overlaps left

    def test_summary_device_filter_and_tile(self):
        s = analytics.summary(self.conn, DAY, DAY, self.rules)
        self.assertEqual(s["phone"], {"seconds": 15 * 60, "unlocks": 1, "paired": True})
        self.assertEqual(s["totals"]["active"], 65 * 60)
        um = {u["name"]: u for u in s["unmatched"]}
        self.assertEqual((um["com.example.weird"]["kind"], um["com.example.weird"]["label"]), ("phone", "Weird App"))
        top = {a["name"]: a for a in s["top_apps"]}
        self.assertEqual((top["com.instagram.android"]["label"], top["com.instagram.android"]["rule_section"]),
                         ("Instagram", "phone_apps"))
        only_phone = analytics.summary(self.conn, DAY, DAY, self.rules, device="phone")
        self.assertEqual(only_phone["totals"]["active"], 15 * 60)
        only_pc = analytics.summary(self.conn, DAY, DAY, self.rules, device="pc")
        self.assertEqual(only_pc["totals"]["active"], 50 * 60)

    def test_phone_app_rule_from_dashboard(self):
        path = Path(self.tmp.name) / "categories.json"
        path.write_text(categorize.RULES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        r = rules_edit.set_rules([{"section": "phone_apps", "key": "COM.Example.Weird", "category": "Study"}], path)
        self.assertEqual(r["changed"], ["Phone app com.example.weird → Study"])
        self.assertEqual(categorize.Rules(path).classify_phone("com.example.weird", "Weird App"), ("Study", True))
        with self.assertRaises(ValueError):
            rules_edit.set_rules([{"section": "phone_apps", "key": "not a package", "category": "Study"}], path)


if __name__ == "__main__":
    unittest.main()
