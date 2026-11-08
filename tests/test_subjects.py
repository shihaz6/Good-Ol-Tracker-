"""Subjects + exam countdown (subjects.py) and the phone's Week data (phone.week_for_phone).
No network: Groq is faked. Run:  python -m unittest discover -s tests -v"""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import ai_judge  # noqa: E402
import categorize  # noqa: E402
import db  # noqa: E402
import keys  # noqa: E402
import mailer  # noqa: E402
import phone  # noqa: E402
import subjects  # noqa: E402

DAY = date(2026, 11, 4)  # Wednesday
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
        self.rules = categorize.Rules()
        self.n = 0
        subjects.save([
            {"name": "DBMS", "keywords": "sql, normalization, er diagram", "exam": (DAY + timedelta(days=12)).isoformat(),
             "weekly_minutes": 360},
            {"name": "OOP", "keywords": ["java", "inheritance", "polymorphism"]},
        ])

    def tearDown(self):
        db.DATA_DIR = self._data
        self.tmp.cleanup()

    def pc(self, s, e, app, title):
        self.conn.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title) VALUES (?, ?, 'desktop', ?, ?)",
                          (s, e, app, title))

    def browse(self, s, e, domain, title, url):
        self.n += 1
        self.pc(s, e, "brave.exe", "b")
        self.conn.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, url, client_id) VALUES (?, ?, 'browser', ?, ?, ?, ?)",
                          (s, e, domain, title, url, f"b{self.n}"))


class SettingsTest(Base):
    def test_saved_and_validated(self):
        s = subjects.settings()
        self.assertEqual(s[0]["keywords"], ["er diagram", "normalization", "sql"])
        self.assertEqual(s[1]["keywords"], ["inheritance", "java", "polymorphism"])
        for bad in ([{"name": ""}], [{"name": "A"}, {"name": "a"}], [{"name": "A", "exam": "soon"}],
                    [{"name": "A", "weekly_minutes": 99999}], "nope"):
            with self.assertRaises(ValueError, msg=bad):
                subjects.save(bad)

    def test_match(self):
        pats = subjects._patterns(subjects.settings())
        self.assertEqual(subjects.match(pats, {"title": "SQL normalization explained - YouTube"}), "DBMS")
        self.assertEqual(subjects.match(pats, {"title": "Main.java - lab3 - Visual Studio Code"}), "OOP")
        self.assertEqual(subjects.match(pats, {"title": "dbms lecture 4"}), "DBMS")   # the name itself counts
        self.assertIsNone(subjects.match(pats, {"title": "java and sql together"}))   # a tie: nobody
        self.assertIsNone(subjects.match(pats, {"title": "Weather"}))


class SummaryTest(Base):
    def test_time_per_subject_week_and_exam(self):
        self.browse(at(9), at(10), "youtube.com", "SQL normalization full course", "https://www.youtube.com/watch?v=n")
        self.pc(at(10), at(11), "Code.exe", "Shape.java - lab3 - Visual Studio Code")
        self.pc(at(11), at(11, 30), "Code.exe", "notes.txt - Visual Studio Code")        # Coding, no subject
        self.browse(at(12), at(12, 30), "netflix.com", "Show", "https://netflix.com/x")   # not productive: ignored
        self.pc(at(9, day=-3), at(10, day=-3), "Code.exe", "query.sql - db - Visual Studio Code")  # last week (Sun)
        self.conn.commit()
        s = subjects.summary(self.conn, DAY - timedelta(days=6), DAY, self.rules, today=DAY)
        by = {x["name"]: x for x in s["subjects"]}
        self.assertEqual((by["DBMS"]["seconds"], by["DBMS"]["week_seconds"]), (7200, 3600))  # Sunday isn't this week
        self.assertEqual(by["OOP"]["seconds"], 3600)
        self.assertEqual(by["DBMS"]["days_left"], 12)
        self.assertEqual(s["next_exam"]["name"], "DBMS")
        self.assertEqual(s["untagged_seconds"], 1800)
        rows = dict(mailer._subject_rows(self.conn, DAY - timedelta(days=2), DAY, self.rules))
        self.assertIn("exam in 12 days", rows["Subject: DBMS"])
        self.assertIn("this week 1h", rows["Subject: DBMS"])


class GroqTest(Base):
    def test_unmatched_study_pages_get_tagged(self):
        # "Third normal form" page has none of the keywords in its title, but Groq knows it's DBMS.
        self.browse(at(9), at(9, 20), "geeksforgeeks.org", "Third Normal Form (3NF)", "https://www.geeksforgeeks.org/3nf")
        self.browse(at(9, 20), at(9, 40), "geeksforgeeks.org", "SQL joins", "https://www.geeksforgeeks.org/joins")  # keyword match
        self.conn.commit()
        sent = []

        def fake(key, path, body):
            items = json.loads(body["messages"][1]["content"])["items"]
            sent.extend(items)
            return {"choices": [{"message": {"content": json.dumps(
                {"results": [{"id": it["id"], "subject": "DBMS", "confidence": 0.9} for it in items]})}}]}
        orig = keys.load_key
        keys.load_key = lambda name: "k"
        ai_judge.status["paused_until"] = 0.0
        try:
            r = subjects.run_batch(self.conn, self.rules, request=fake, now=at(12))
        finally:
            keys.load_key = orig
        self.assertEqual((r["pages"], r["tagged"]), (1, 1))
        self.assertEqual([it["title"] for it in sent], ["Third Normal Form (3NF)"])
        s = subjects.summary(self.conn, DAY, DAY, self.rules, today=DAY)
        self.assertEqual({x["name"]: x["seconds"] for x in s["subjects"]}["DBMS"], 2400)


class WeekTest(Base):
    def test_week_payload(self):
        self.browse(at(9), at(10), "youtube.com", "SQL normalization full course", "https://www.youtube.com/watch?v=n")
        self.conn.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, client_id) VALUES (?, ?, 'phone',"
                          " 'com.instagram.android', 'Instagram', 'p1')", (at(20), at(21)))
        self.conn.commit()
        phone._week_cache["ts"] = 0.0
        w = phone.week_for_phone(self.conn, now=at(22))
        self.assertEqual(len(w["days"]), 7)
        self.assertEqual(w["days"][-1], {"date": DAY.isoformat(), "productive": 3600, "phone": 3600, "pc_tracked": True})
        self.assertFalse(w["days"][0]["pc_tracked"])
        self.assertEqual(w["top_apps"][0], {"app": "Instagram", "seconds": 3600})
        self.assertEqual(w["subjects"][0]["name"], "DBMS")
        self.assertEqual(w["subjects"][0]["days_left"], 12)
        for k in ("goals", "sleep", "focus", "pickups"):
            self.assertIn(k, w)


if __name__ == "__main__":
    unittest.main()
