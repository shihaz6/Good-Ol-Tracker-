"""Phone focus mode + alerts (phone.py/nudge.py/focus.py), sleep + pickups (insights.py),
phone lines in emails (mailer.py). No network.
Run:  python -m unittest discover -s tests -v"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import categorize  # noqa: E402
import db  # noqa: E402
import focus  # noqa: E402
import insights  # noqa: E402
import mailer  # noqa: E402
import nudge  # noqa: E402
import phone  # noqa: E402

DAY = date(2026, 11, 2)
T0 = datetime.combine(DAY, datetime.min.time()).timestamp()


def at(hour, minute=0, day=0):
    return T0 + day * 86400 + hour * 3600 + minute * 60


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._data = db.DATA_DIR
        db.DATA_DIR = Path(self.tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(db.SCHEMA)
        db._migrate(self.conn)
        nudge.state.update(pending=None, last_alert=0.0, snooze_until=0.0, history=[])
        self.rules = categorize.Rules()
        self.n = 0

    def tearDown(self):
        db.DATA_DIR = self._data
        self.tmp.cleanup()

    def ph(self, s, e, pkg, label):
        self.n += 1
        self.conn.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, client_id) VALUES (?, ?, 'phone', ?, ?, ?)",
                          (s, e, pkg, label, f"p{self.n}"))

    def pc(self, s, e, app="Code.exe", title="x"):
        self.conn.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title) VALUES (?, ?, 'desktop', ?, ?)",
                          (s, e, app, title))


class PhoneFocusTest(Base):
    def test_block_list_from_rules_and_seen_apps(self):
        self.ph(at(9), at(9, 5), "com.example.memes", "Funny Memes")   # keywords say Entertainment
        self.ph(at(9), at(9, 5), "com.example.notes", "Notes")
        self.conn.commit()
        block = focus.phone_block(self.conn, self.rules, now=at(10))
        for pkg in ("com.instagram.android", "com.facebook.katana", "com.google.android.youtube", "com.example.memes"):
            self.assertIn(pkg, block)
        for pkg in ("com.whatsapp", "com.example.notes", "com.android.deskclock"):
            self.assertNotIn(pkg, block)

    def test_focus_from_phone_and_state_in_sync_reply(self):
        r = phone.focus_action(self.conn, {"action": "start", "minutes": 25}, now=at(9))
        self.assertEqual(round(r["focus"]["end"] - r["focus"]["start"]), 25 * 60)
        self.assertIn("com.instagram.android", r["phone_block"])
        dev = "dev-12345678"
        sync = phone.sync(self.conn, dev, {"sessions": [], "focus_blocks": [
            {"id": "a1", "ts": int(at(9, 5) * 1000), "app": "Instagram"}]}, now=at(9, 6))
        self.assertIsNotNone(sync["focus"])
        h = focus.history(self.conn, at(0), at(24), self.rules, now=at(9, 6))
        self.assertEqual(h["sessions"][0]["blocked_sites"], [{"domain": "Instagram (phone)", "count": 1}])
        phone.focus_action(self.conn, {"action": "stop"}, now=at(9, 10))
        self.assertIsNone(phone.state_for_phone(self.conn, now=at(9, 11))["focus"])
        phone.focus_action(self.conn, {"action": "snooze", "minutes": 30}, now=at(9, 11))
        self.assertGreater(nudge.state["snooze_until"], at(9, 30))
        with self.assertRaises(ValueError):
            phone.focus_action(self.conn, {"action": "explode"})


class PhoneAlertTest(Base):
    def test_alert_goes_to_the_phone_when_you_are_on_it(self):
        self.ph(at(9), at(9, 25), "com.instagram.android", "Instagram")
        self.conn.commit()
        a = nudge.check(self.conn, now=at(9, 25), rules=self.rules)
        self.assertEqual(a["target"], "phone")
        self.assertIn("Instagram", a["title"])
        self.assertIsNone(nudge.take("pc"))          # not for the extension
        self.assertEqual(phone.state_for_phone(self.conn, now=at(9, 25, ))["nudge"]["id"], a["id"])

    def test_phone_alert_falls_back_to_pc(self):
        self.ph(at(9), at(9, 25), "com.instagram.android", "Instagram")
        self.conn.commit()
        nudge.check(self.conn, now=at(9, 25), rules=self.rules)
        shown = []
        nudge.handoff(now=at(9, 25) + nudge.PHONE_HANDOFF + 1, toast=lambda t, m: shown.append(t))
        self.assertEqual(shown, [])                   # moved to the PC first ...
        self.assertIsNotNone(nudge.take("pc"))        # ... where the extension picks it up


class SleepTest(Base):
    def test_longest_quiet_stretch_is_the_night(self):
        # Evening of day 0: phone until 00:40, unlock at 07:10, PC from 08:00.
        self.ph(at(22), at(23), "com.instagram.android", "Instagram")
        self.ph(at(0, 30, day=1), at(0, 40, day=1), "com.whatsapp", "WhatsApp")
        self.conn.execute("INSERT INTO phone_unlocks VALUES ('d', ?)", (at(7, 10, day=1),))
        self.pc(at(8, day=1), at(12, day=1))           # 4 h Coding the next day
        self.conn.commit()
        s = insights.sleep(self.conn, DAY + timedelta(days=1), nights=1, rules=self.rules, now=at(1, day=2))
        n = s["nights"][0]
        self.assertEqual((n["bedtime"], n["wake"]), (at(0, 40, day=1), at(7, 10, day=1)))
        self.assertEqual(n["seconds"], 6 * 3600 + 30 * 60)
        self.assertEqual(n["next_day_productive"], 4 * 3600)
        self.assertEqual(s["avg_bedtime"], "00:40")
        self.assertEqual(s["productive_after_short"], 4 * 3600)

    def test_no_data_and_unfinished_morning(self):
        self.ph(at(23), at(23, 30), "com.whatsapp", "WhatsApp")
        self.conn.commit()
        s = insights.sleep(self.conn, DAY + timedelta(days=1), nights=1, rules=self.rules, now=at(3, day=1))
        self.assertIsNone(s["nights"][0]["seconds"])  # still asleep: nothing after the gap yet


class PickupTest(Base):
    def test_pickups_between_study(self):
        self.pc(at(9), at(9, 30))                              # study
        self.ph(at(9, 30), at(9, 33), "com.facebook.katana", "Facebook")   # glance
        self.pc(at(9, 33), at(10))                             # back to study
        self.ph(at(10), at(11), "com.facebook.katana", "Facebook")         # an hour: a break
        self.pc(at(11), at(11, 30))
        self.ph(at(20), at(20, 10), "com.instagram.android", "Instagram")  # evening, no study around
        self.conn.commit()
        r = insights.interruptions(self.conn, at(0), at(24), self.rules)
        self.assertEqual((r["count"], r["seconds"], r["breaks"]), (1, 180, 1))
        self.assertEqual(r["top_apps"], [{"app": "Facebook", "seconds": 180}])
        self.assertEqual(r["per_study_hour"], round(1 / ((30 + 27 + 30) / 60), 1))


class MailTest(Base):
    def test_phone_rows(self):
        self.pc(at(9), at(10))
        self.ph(at(10, 1), at(10, 4), "com.facebook.katana", "Facebook")
        self.pc(at(10, 4), at(11))
        self.conn.execute("INSERT INTO phone_unlocks VALUES ('d', ?)", (at(10, 1),))
        self.conn.commit()
        rows = dict(mailer._phone_rows(self.conn, DAY, DAY, self.rules))
        self.assertTrue(rows["Phone"].startswith("3m") and "1 unlocks" in rows["Phone"])
        self.assertEqual(rows["Top phone apps"], "Facebook 3m")
        self.assertIn("1 pickups", rows["Phone during study"])

    def test_no_phone_no_rows(self):
        self.pc(at(9), at(10))
        self.conn.commit()
        self.assertEqual(mailer._phone_rows(self.conn, DAY, DAY, self.rules), [])


if __name__ == "__main__":
    unittest.main()
