"""Distraction alerts (nudge.py). No network, no real notifications.
Run:  python -m unittest discover -s tests -v"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import categorize  # noqa: E402
import db  # noqa: E402
import focus  # noqa: E402
import nudge  # noqa: E402
import server  # noqa: E402

T0 = datetime.combine(date(2026, 11, 2), datetime.min.time()).timestamp()


def at(hour, minute=0, sec=0):
    return T0 + hour * 3600 + minute * 60 + sec


def item(start, end, cat, app="youtube.com"):
    return categorize.Item(start, end, cat, app, "t", "browser", True)


CATS = ["Entertainment", "Social"]


class StreakTest(unittest.TestCase):
    def test_simple_streak(self):
        items = [item(at(9), at(9, 10), "Study", "code.exe"),
                 item(at(9, 10), at(9, 30), "Entertainment"),
                 item(at(9, 30), at(9, 35), "Social", "instagram.com")]
        s = nudge.streak(items, CATS, at(9, 35, 30))
        self.assertEqual(s["seconds"], 25 * 60)
        self.assertEqual(s["start"], at(9, 10))
        self.assertEqual(s["top"][0], ("youtube.com", 20 * 60))

    def test_short_detour_and_pause_do_not_break_it(self):
        items = [item(at(9), at(9, 10), "Entertainment"),
                 item(at(9, 10), at(9, 11), "Communication", "web.whatsapp.com"),  # 60 s detour
                 item(at(9, 13), at(9, 20), "Entertainment")]                      # after a 2 min pause
        self.assertEqual(nudge.streak(items, CATS, at(9, 20))["seconds"], 17 * 60)

    def test_real_work_breaks_it(self):
        items = [item(at(9), at(9, 30), "Entertainment"),
                 item(at(9, 30), at(9, 35), "Study", "code.exe"),
                 item(at(9, 35), at(9, 40), "Entertainment")]
        self.assertEqual(nudge.streak(items, CATS, at(9, 40))["seconds"], 5 * 60)

    def test_long_pause_breaks_it(self):
        items = [item(at(9), at(9, 30), "Entertainment"), item(at(9, 40), at(9, 45), "Entertainment")]
        self.assertEqual(nudge.streak(items, CATS, at(9, 45))["seconds"], 5 * 60)

    def test_must_be_happening_now(self):
        items = [item(at(9), at(9, 30), "Entertainment")]
        self.assertIsNone(nudge.streak(items, CATS, at(9, 35)))
        items.append(item(at(9, 30), at(9, 35), "Study", "code.exe"))
        self.assertIsNone(nudge.streak(items, CATS, at(9, 35)))

    def test_message(self):
        m = nudge.message_for({"seconds": 1500, "top": [("youtube.com", 900), ("Spotify.exe", 600)]}, CATS)
        self.assertEqual(m["title"], "25 min on youtube.com and Spotify")


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._data = db.DATA_DIR
        db.DATA_DIR = Path(self.tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(db.SCHEMA)
        db._migrate(self.conn)
        nudge.state.update(pending=None, last_alert=0.0, snooze_until=0.0, history=[])
        self.rules = categorize.Rules()
        # 09:00-10:00 Brave in front, on netflix.com (Entertainment). Checks happen at
        # earlier "now"s, so categorise only sees what had happened by then.
        self.conn.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title) VALUES (?, ?, 'desktop', 'brave.exe', 'b')",
                          (at(9), at(10)))
        self.conn.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, url) VALUES (?, ?, 'browser', 'netflix.com', 'Show', 'https://netflix.com/w')",
                          (at(9), at(10)))
        self.conn.commit()

    def tearDown(self):
        db.DATA_DIR = self._data
        self.tmp.cleanup()

    def check(self, t):
        return nudge.check(self.conn, now=t, rules=self.rules)

    def test_alerts_once_then_repeats(self):
        self.assertIsNone(self.check(at(9, 15)))          # 15 min: not yet
        a = self.check(at(9, 21))
        self.assertIsNotNone(a)
        self.assertIn("netflix.com", a["title"])
        self.assertIsNone(self.check(at(9, 30)))          # same streak, within repeat
        self.assertIsNotNone(self.check(at(9, 42)))       # 20+ min later, still going
        self.assertEqual(nudge.take()["id"], a["id"] + 1)
        self.assertIsNone(nudge.take())                   # collected once

    def test_snooze_off_and_focus(self):
        nudge.snooze(30, now=at(9))
        self.assertIsNone(self.check(at(9, 21)))
        nudge.state["snooze_until"] = 0
        nudge.save_settings({"enabled": False}, self.rules)
        self.assertIsNone(self.check(at(9, 21)))
        nudge.save_settings({"enabled": True}, self.rules)
        focus.start(self.conn, 50, now=at(9, 1))
        self.assertIsNotNone(focus.current(self.conn, at(9, 21)))
        self.assertIsNone(self.check(at(9, 21)))

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            nudge.save_settings({"minutes": 1}, self.rules)
        with self.assertRaises(ValueError):
            nudge.save_settings({"categories": ["Nope"]}, self.rules)
        with self.assertRaises(ValueError):
            nudge.save_settings({"categories": []}, self.rules)
        s = nudge.save_settings({"minutes": 30, "categories": ["Social", "Entertainment"]}, self.rules)
        self.assertEqual(s["categories"], ["Entertainment", "Social"])  # your category order

    def test_windows_fallback_when_extension_does_not_collect(self):
        shown = []
        self.check(at(9, 21))
        self.assertIsNone(nudge.handoff(now=at(9, 21, 30), toast=lambda t, m: shown.append(t)))
        self.assertIsNotNone(nudge.handoff(now=at(9, 23), toast=lambda t, m: shown.append(t)))
        self.assertEqual(len(shown), 1)
        self.assertIsNone(nudge.take())

    def test_ingest_reply_carries_it(self):
        self.check(at(9, 21))

        class NoClose:
            def __init__(s, c): s.c = c
            def __getattr__(s, n): return getattr(s.c, n)
            def close(s): pass
        orig = db.connect
        db.connect = lambda *a, **k: NoClose(self.conn)
        try:
            reply = server.ingest({"visits": []}, server.SharedState())
        finally:
            db.connect = orig
        self.assertIn("netflix.com", reply["nudge"]["title"])


if __name__ == "__main__":
    unittest.main()
