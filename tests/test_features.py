"""Offline time, search AI, trends, export, email (fake SMTP). No network.
Run:  python -m unittest discover -s tests -v"""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import ai_categorize  # noqa: E402
import analytics  # noqa: E402
import categorize  # noqa: E402
import db  # noqa: E402
import keys  # noqa: E402
import mailer  # noqa: E402

DAY = date(2026, 11, 2)  # a Monday in the past (future timestamps are rejected on insert)
T0 = datetime.combine(DAY, datetime.min.time()).timestamp()
CATS = categorize.Rules().categories


def at(hour, minute=0, day=0):
    return T0 + day * 86400 + hour * 3600 + minute * 60


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


def desktop(conn, start, end, app, title="x"):
    conn.execute("INSERT INTO sessions (start_ts, end_ts, app, title) VALUES (?,?,?,?)", (start, end, app, title))


class OfflineTimeTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()

    def test_only_counts_when_pc_not_in_use(self):
        desktop(self.conn, at(10), at(10, 30), "Code.exe")  # laptop open for 30 min during the lecture
        db.add_manual_entry(self.conn, at(9), at(11), "Study", "DBMS lecture", CATS)
        s = analytics.summary(self.conn, DAY, DAY)
        self.assertEqual(s["totals"]["by_category"], {"Study": 5400, "Coding": 1800})  # 1h30 offline + 30m PC
        self.assertEqual(s["offline_seconds"], 5400)
        self.assertEqual(s["totals"]["active"], 7200)  # never more than the 2h that passed

    def test_offline_time_is_not_pc_focus_or_switching(self):
        db.add_manual_entry(self.conn, at(9), at(11), "Study", "lecture", CATS)
        s = analytics.summary(self.conn, DAY, DAY)
        self.assertIsNone(s["focus"]["longest"])
        self.assertEqual(s["switches"]["count"], 0)
        self.assertEqual(s["top_apps"][0]["kind"], "offline")

    def test_validation_and_delete(self):
        with self.assertRaises(ValueError):
            db.add_manual_entry(self.conn, at(9), at(9), "Study", "", CATS)        # zero length
        with self.assertRaises(ValueError):
            db.add_manual_entry(self.conn, at(0), at(17), "Study", "", CATS)       # > 16h
        with self.assertRaises(ValueError):
            db.add_manual_entry(self.conn, at(9), at(10), "Napping", "", CATS)     # unknown category
        i = db.add_manual_entry(self.conn, at(9), at(10), "Study", "x" * 500, CATS)
        self.assertEqual(len(self.conn.execute("SELECT note FROM manual_entries").fetchone()[0]), 200)
        self.assertTrue(db.delete_manual_entry(self.conn, i))
        self.assertFalse(db.delete_manual_entry(self.conn, i))


class SearchAITest(unittest.TestCase):
    def setUp(self):
        self._load_key = keys.load_key
        keys.load_key = lambda name: "k"
        ai_categorize.status.update(model=None)
        self.conn = memory_db()
        self.rules = categorize.Rules()
        self.rules.descriptions = {"Study": ["a lecture or tutorial"], "Entertainment": ["funny gaming clips"]}
        self.calls = []

        def fake(texts, key, model):
            self.calls.append(list(texts))
            vec = lambda t: [sum(w in t.lower() for w in ("lecture", "tutorial", "normal")),
                             sum(w in t.lower() for w in ("gaming", "funny")), 0.05]
            return (model or "m"), [vec(t) for t in texts]
        self.fake = fake

    def tearDown(self):
        keys.load_key = self._load_key

    def add_search(self, i, query):
        db.insert_searches(self.conn, [{"id": str(i), "ts": at(9, i), "engine": "Google", "query": query,
                                        "url": "https://www.google.com/search?q=x"}])

    def test_searches_get_ai_categories_once_per_distinct_query(self):
        self.add_search(1, "Normal forms tutorial")
        self.add_search(2, "normal   forms TUTORIAL")   # same query, different case/spacing
        self.add_search(3, "funny gaming")
        res = ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)
        self.assertEqual(res["queries_embedded"], 2)
        cats = ai_categorize.query_categories(self.conn, ["NORMAL FORMS tutorial", "funny gaming"])
        self.assertEqual(cats, {"normal forms tutorial": "Study", "funny gaming": "Entertainment"})
        n = len(self.calls)
        res = ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)
        self.assertEqual((res["queries_embedded"], res["queries_reclassified"]), (0, 0))
        self.assertEqual(len(self.calls), n)

    def test_unicode_queries_are_not_re_embedded_forever(self):
        self.add_search(1, "ÉCOLE normal tutorial")  # SQLite's lower() wouldn't fold É
        ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)
        res = ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)
        self.assertEqual(res["queries_embedded"], 0)

    def test_keywords_first_then_ai(self):
        r = categorize.Rules()
        self.assertEqual(categorize.classify_search(r, "sql normalization", "Entertainment"), "Study")
        self.assertEqual(categorize.classify_search(r, "pc activity tracker", "Coding"), "Coding")
        self.assertEqual(categorize.classify_search(r, "pc activity tracker", None), "")
        self.assertEqual(categorize.classify_search(r, "pc activity tracker", "Gone"), "")


class TrendsAndExportTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        desktop(self.conn, at(9, day=-7), at(10, day=-7), "Code.exe")      # last week: 1h coding
        desktop(self.conn, at(9), at(11), "Code.exe")                       # this week: 2h coding
        desktop(self.conn, at(11), at(11, 30), "Spotify.exe")

    def test_trends_series_and_week_compare(self):
        t = analytics.trends(self.conn, DAY, days=30)
        self.assertEqual(len(t["days"]), 30)
        self.assertEqual(t["days"][-1]["date"], DAY.isoformat())
        self.assertEqual(t["days"][-1]["productive"], 7200)
        self.assertEqual(t["days"][-1]["longest_focus"], 7200)
        self.assertEqual((t["this_week"]["productive"], t["last_week"]["productive"]), (7200, 3600))
        self.assertEqual(t["this_week"]["start"], (DAY - timedelta(days=6)).isoformat())
        self.assertIsNone(t["days"][0]["switches_per_hour"])  # no data that day

    def test_exports(self):
        header, rows = analytics.export_rows(self.conn, "activity", DAY, DAY)
        self.assertEqual(header[:4], ["start", "end", "minutes", "category"])
        self.assertEqual([(r[2], r[3]) for r in rows], [(120.0, "Coding"), (30.0, "Entertainment")])
        header, rows = analytics.export_rows(self.conn, "daily", DAY, DAY)
        self.assertEqual(rows[0][:3], [DAY.isoformat(), 2.5, 2.0])
        self.assertEqual(analytics.export_rows(self.conn, "searches", DAY, DAY)[1], [])
        with self.assertRaises(ValueError):
            analytics.export_rows(self.conn, "passwords", DAY, DAY)


class FakeSMTP:
    sent = []
    login_ok = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        import smtplib
        if not FakeSMTP.login_ok:
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.user, self.pw = user, pw

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class MailerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._data, self._settings = db.DATA_DIR, mailer.SETTINGS_PATH
        db.DATA_DIR = Path(self.tmp.name)
        mailer.SETTINGS_PATH = db.DATA_DIR / "email.json"
        self._load_key = keys.load_key
        keys.load_key = lambda name: "abcdabcdabcdabcd" if name == "gmail" else None
        FakeSMTP.sent, FakeSMTP.login_ok = [], True
        self.conn = memory_db()
        desktop(self.conn, at(9), at(11), "Code.exe", "secret_project.py")
        db.insert_searches(self.conn, [{"id": "s", "ts": at(10), "engine": "Google",
                                        "query": "very private search", "url": "https://www.google.com/search?q=x"}])

    def tearDown(self):
        db.DATA_DIR, mailer.SETTINGS_PATH = self._data, self._settings
        keys.load_key = self._load_key
        self.tmp.cleanup()

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            mailer.save_settings({"address": "not-an-email"})
        with self.assertRaises(ValueError):
            mailer.save_settings({"address": "a@gmail.com", "daily_time": "25:00"})
        s = mailer.save_settings({"address": "a@gmail.com", "daily_enabled": True, "daily_time": "21:30"})
        self.assertEqual((s["to"], s["daily_time"]), ("a@gmail.com", "21:30"))  # "to" defaults to self
        with self.assertRaises(ValueError):
            mailer.save_password("short")
        mailer.save_password("abcd efgh ijkl mnop")
        self.assertEqual((db.DATA_DIR / "gmail_key.txt").read_text(), "abcdefghijklmnop")

    def test_daily_email_has_totals_not_private_text(self):
        mailer.save_settings({"address": "a@gmail.com"})
        subject, text, body = mailer.build_daily(self.conn, DAY)
        self.assertIn("2h 00m productive (100%)", subject)
        for blob in (text, body):
            self.assertNotIn("very private search", blob)
            self.assertNotIn("secret_project", blob)
        mailer.send(subject, text, body, smtp_factory=FakeSMTP)
        msg = FakeSMTP.sent[0]
        self.assertEqual(msg["To"], "a@gmail.com")
        self.assertTrue(msg.is_multipart())

    def test_bad_login_is_a_clear_error(self):
        mailer.save_settings({"address": "a@gmail.com"})
        FakeSMTP.login_ok = False
        with self.assertRaises(mailer.MailError) as ctx:
            mailer.send("s", "t", "<p>h</p>", smtp_factory=FakeSMTP)
        self.assertIn("App Password", str(ctx.exception))

    def test_daily_schedule_once_per_day(self):
        mailer.save_settings({"address": "a@gmail.com", "daily_enabled": True, "daily_time": "21:00",
                              "weekly_enabled": False})
        original = mailer.send
        mailer.send = lambda *a, **k: FakeSMTP.sent.append(a[0])
        try:
            self.assertEqual(mailer.maybe_send(self.conn, datetime.combine(DAY, datetime.min.time()).replace(hour=20)), [])
            self.assertEqual(mailer.maybe_send(self.conn, datetime.combine(DAY, datetime.min.time()).replace(hour=21, minute=5)), ["daily"])
            self.assertEqual(mailer.maybe_send(self.conn, datetime.combine(DAY, datetime.min.time()).replace(hour=22)), [])
        finally:
            mailer.send = original

    def test_weekly_email_sent_once_for_a_full_week_summary(self):
        mailer.save_settings({"address": "a@gmail.com", "daily_enabled": False, "weekly_enabled": True})
        self.conn.execute("INSERT INTO summaries (created_ts, start_day, end_day, model, payload, text)"
                          " VALUES (0, '2026-11-09', '2026-11-15', 'm', '{}', '## Overview\nGreat week.')")
        original = mailer.send
        mailer.send = lambda *a, **k: FakeSMTP.sent.append(a)
        try:
            self.assertEqual(mailer.maybe_send(self.conn, datetime(2026, 11, 16, 9)), ["weekly"])
            self.assertEqual(mailer.maybe_send(self.conn, datetime(2026, 11, 16, 10)), [])
            subject, text, body = FakeSMTP.sent[0]
            self.assertIn("Great week.", text)
            self.assertIn("This week vs last week", text)
        finally:
            mailer.send = original

    def test_not_configured_sends_nothing(self):
        keys.load_key = lambda name: None
        self.assertEqual(mailer.maybe_send(self.conn), [])


if __name__ == "__main__":
    unittest.main()
