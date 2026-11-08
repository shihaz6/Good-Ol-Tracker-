"""App limits / bedtime / scheduled focus / goals settings + logic (wellbeing.py). No network.
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
import phone  # noqa: E402
import wellbeing  # noqa: E402

DAY = date(2026, 11, 4)  # a Wednesday
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
        wellbeing._started.clear()
        self.rules = categorize.Rules()

    def tearDown(self):
        db.DATA_DIR = self._data
        self.tmp.cleanup()


class SettingsTest(Base):
    def test_defaults_and_save(self):
        s = wellbeing.settings()
        self.assertEqual(s["limits"]["com.facebook.katana"], 60)
        self.assertEqual(s["bedtime"]["start"], "23:30")
        saved = wellbeing.save({"limits": {"COM.Instagram.Android": 45}, "bedtime": {
            "enabled": True, "start": "23:00", "end": "06:30", "wind_down": 20}}, self.rules)
        self.assertEqual(saved["limits"], {"com.instagram.android": 45})
        self.assertEqual(wellbeing.settings()["bedtime"]["end"], "06:30")

    def test_validation(self):
        bad = [
            {"limits": {"not a package": 10}},
            {"limits": {"com.x.y": 0}},
            {"bedtime": {"enabled": True, "start": "25:00", "end": "06:00"}},
            {"bedtime": {"enabled": True, "start": "06:00", "end": "06:00"}},
            {"schedules": [{"days": [], "start": "19:00", "end": "21:00"}]},
            {"schedules": [{"days": [0], "start": "19:00", "end": "19:02"}]},   # 2 min: too short
            {"goals": [{"kind": "nope", "minutes": 60}]},
            {"goals": [{"kind": "category_max", "minutes": 60, "category": "Nope"}]},
        ]
        for body in bad:
            with self.assertRaises(ValueError, msg=body):
                wellbeing.save(body, self.rules)

    def test_windows_cross_midnight(self):
        w = wellbeing.window_now("23:30", "06:00", datetime.fromtimestamp(at(1)))
        self.assertEqual((w[0].timestamp(), w[1].timestamp()), (at(23, 30, day=-1), at(6)))
        self.assertIsNotNone(wellbeing.window_now("23:30", "06:00", datetime.fromtimestamp(at(23, 45))))
        self.assertIsNone(wellbeing.window_now("23:30", "06:00", datetime.fromtimestamp(at(12))))
        self.assertIsNotNone(wellbeing.window_now("19:00", "21:00", datetime.fromtimestamp(at(20))))


class ScheduleTest(Base):
    def test_starts_once_per_occurrence(self):
        wellbeing.save({"schedules": [{"days": [2], "start": "19:00", "end": "21:00", "label": "DBMS"}]}, self.rules)
        self.assertIsNone(wellbeing.scheduled_focus(self.conn, now=at(18, 59)))
        s = wellbeing.scheduled_focus(self.conn, now=at(19, 1))
        self.assertEqual((s["label"], round((s["end"] - s["start"]) / 60)), ("DBMS", 119))
        focus.stop(self.conn, now=at(19, 10))                  # ended early ...
        self.assertIsNone(wellbeing.scheduled_focus(self.conn, now=at(19, 11)))  # ... stays ended
        self.assertIsNone(wellbeing.scheduled_focus(self.conn, now=at(19, 1, day=1)))  # Thursday: not scheduled

    def test_phone_reply_carries_settings(self):
        r = phone.state_for_phone(self.conn, now=at(12))
        self.assertIn("com.facebook.katana", r["wellbeing"]["limits"])
        self.assertIn("goals", r["today"])


class GoalsTest(Base):
    def add(self, s, e, source="desktop", app="Code.exe", title="x"):
        cid = f"c{s}" if source == "phone" else None
        self.conn.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, client_id) VALUES (?, ?, ?, ?, ?, ?)",
                          (s, e, source, app, title, cid))

    def test_progress_and_streaks(self):
        wellbeing.save({"goals": [{"kind": "productive_min", "minutes": 120}, {"kind": "phone_max", "minutes": 60},
                                  {"kind": "category_max", "minutes": 30, "category": "Social"}]}, self.rules)
        # day -2: 3h coding; day -1: 2.5h coding + 2h Instagram; today: 1h coding, 20 min Instagram
        self.add(at(9, day=-2), at(12, day=-2))
        self.add(at(9, day=-1), at(11, 30, day=-1))
        self.add(at(20, day=-1), at(22, day=-1), "phone", "com.instagram.android", "Instagram")
        self.add(at(9), at(10))
        self.add(at(10), at(10, 20), "phone", "com.instagram.android", "Instagram")
        self.conn.commit()
        g = {x["kind"]: x for x in wellbeing.goals(self.conn, today=DAY, days=5, rules=self.rules, now=at(11))}
        prod, ph, soc = g["productive_min"], g["phone_max"], g["category_max"]
        self.assertEqual((prod["today"], prod["met_today"], prod["streak"]), (3600, False, 2))
        self.assertEqual([h["met"] for h in prod["history"]][-3:], [True, True, False])
        self.assertIsNone(prod["history"][0]["met"])   # before the PC was tracked
        self.assertEqual((ph["today"], ph["over"], ph["streak"]), (1200, False, 0))  # yesterday was 2h: broke it
        self.assertEqual(ph["history"][-2]["met"], False)
        self.assertIsNone(ph["history"][-3]["met"])    # phone not tracked yet on day -2
        self.assertEqual(soc["label"], "Social under 30m")
        # productive met today -> streak counts today too
        self.add(at(10, 20), at(11, 30))
        self.conn.commit()
        prod = wellbeing.goals(self.conn, today=DAY, days=5, rules=self.rules, now=at(11, 30))[0]
        self.assertEqual((prod["met_today"], prod["streak"]), (True, 3))


if __name__ == "__main__":
    unittest.main()
