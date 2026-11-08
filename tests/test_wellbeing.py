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


class PcRulesTest(Base):
    def setUp(self):
        super().setUp()
        wellbeing._pc_cache["ts"] = 0.0

    def add(self, s, e, source, app, title="x", url=None):
        self.conn.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, url, client_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (s, e, source, app, title, url, f"{source}{s}{app}"))

    def browse(self, s, e, domain, title, url):
        self.add(s, e, "desktop", "brave.exe", "b")
        self.add(s, e, "browser", domain, title, url)

    def test_shared_budget_phone_plus_pc(self):
        # Facebook limit 60 min: 40 min on the phone + 25 min on facebook.com = over.
        self.add(at(9), at(9, 40), "phone", "com.facebook.katana", "Facebook")
        self.browse(at(10), at(10, 25), "facebook.com", "Facebook", "https://www.facebook.com/")
        self.conn.commit()
        u = {x["pkg"]: x for x in wellbeing.limit_usage(self.conn, now=at(11), rules=self.rules)}
        self.assertEqual((u["com.facebook.katana"]["phone"], u["com.facebook.katana"]["pc"]), (2400, 1500))
        st = wellbeing.pc_state(self.conn, now=at(11), rules=self.rules)
        self.assertEqual(st["limits"][0]["domains"], ["facebook.com"])
        self.assertEqual(st["limits"][0]["until"], at(0, day=1))
        self.assertEqual(wellbeing.pc_used_for_phone(self.conn, now=at(11))["used"], {"com.facebook.katana": 1500})

    def test_study_time_on_a_limited_site_doesnt_count(self):
        wellbeing.save({"limits": {"com.google.android.youtube": 30}}, self.rules)
        self.browse(at(9), at(10), "youtube.com", "SQL normalization full course", "https://www.youtube.com/watch?v=a")
        self.browse(at(10), at(10, 20), "youtube.com", "GTA funny moments", "https://www.youtube.com/watch?v=b")
        self.conn.commit()
        u = wellbeing.limit_usage(self.conn, now=at(11), rules=self.rules)[0]
        self.assertEqual(u["pc"], 20 * 60)  # only the GTA video
        st = wellbeing.pc_state(self.conn, now=at(11), rules=self.rules)
        self.assertEqual(st["limits"], [])
        self.assertEqual(st["warn"], [])     # 20 of 30 min: under 80%
        self.browse(at(10, 20), at(10, 26), "youtube.com", "GTA funny moments 2", "https://www.youtube.com/watch?v=c")
        self.conn.commit()
        wellbeing._pc_cache["ts"] = 0.0
        self.assertEqual(len(wellbeing.pc_state(self.conn, now=at(11), rules=self.rules)["warn"]), 1)

    def test_bedtime_and_wind_down_and_grace(self):
        st = wellbeing.pc_state(self.conn, now=at(23, 10), rules=self.rules)
        self.assertIsNone(st["bedtime"])
        self.assertEqual(st["wind_down"]["start"], "23:30")
        wellbeing._pc_cache["ts"] = 0.0
        st = wellbeing.pc_state(self.conn, now=at(23, 45), rules=self.rules)
        bed = st["bedtime"]
        self.assertEqual(bed["until"], at(6, day=1))
        self.assertIn("instagram.com", bed["domains"])
        self.assertNotIn("youtube.com", bed["domains"])  # YouTube isn't on the focus list by default
        wellbeing.grant_grace(bed["key"], now=at(23, 46))
        self.assertIsNone(wellbeing.pc_state(self.conn, now=at(23, 47), rules=self.rules)["bedtime"])
        wellbeing._pc_cache["ts"] = 0.0
        after = wellbeing.pc_state(self.conn, now=at(23, 52), rules=self.rules)["bedtime"]
        self.assertTrue(after["grace_used"])               # back on, and no second grace
        with self.assertRaises(ValueError):
            wellbeing.grant_grace(bed["key"], now=at(23, 53))
        with self.assertRaises(ValueError):
            wellbeing.grant_grace("nonsense")


if __name__ == "__main__":
    unittest.main()
