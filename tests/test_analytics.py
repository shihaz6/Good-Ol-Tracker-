"""Run:  python -m unittest discover -s tests -v"""

import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import analytics  # noqa: E402
import db  # noqa: E402

DAY = date(2026, 11, 2)
T0 = datetime.combine(DAY, datetime.min.time()).timestamp()  # local midnight


def at(hour, minute=0):
    return T0 + hour * 3600 + minute * 60


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


def desktop(conn, start, end, app, title="x"):
    conn.execute("INSERT INTO sessions (start_ts, end_ts, app, title) VALUES (?,?,?,?)", (start, end, app, title))


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()

    def test_totals_hours_and_productive(self):
        desktop(self.conn, at(9), at(10), "Code.exe")          # Coding 1h
        desktop(self.conn, at(10), at(10, 30), "Spotify.exe")  # Entertainment 30m
        s = analytics.summary(self.conn, DAY, DAY)
        self.assertEqual(s["totals"]["active"], 5400)
        self.assertEqual(s["totals"]["productive"], 3600)
        self.assertEqual(s["by_hour"][9]["by_category"], {"Coding": 3600})
        self.assertEqual(s["by_hour"][10]["by_category"], {"Entertainment": 1800})
        self.assertEqual(s["per_day"][0]["by_category"], {"Coding": 3600, "Entertainment": 1800})

    def test_session_crossing_hours_is_split(self):
        desktop(self.conn, at(9, 30), at(11, 15), "Code.exe")
        h = analytics.summary(self.conn, DAY, DAY)["by_hour"]
        self.assertEqual([h[i]["by_category"].get("Coding", 0) for i in (9, 10, 11)], [1800, 3600, 900])

    def test_focus_streak_survives_short_interruptions_only(self):
        desktop(self.conn, at(9), at(9, 20), "Code.exe")
        desktop(self.conn, at(9, 20), at(9, 21), "Discord.exe")   # 1 min: streak continues
        desktop(self.conn, at(9, 21), at(9, 40), "Code.exe")
        desktop(self.conn, at(9, 40), at(9, 50), "Spotify.exe")   # 10 min: streak breaks
        desktop(self.conn, at(9, 50), at(9, 58), "Code.exe")
        f = analytics.summary(self.conn, DAY, DAY)["focus"]
        self.assertEqual(f["longest"]["seconds"], 40 * 60)
        self.assertEqual(f["longest"]["category"], "Coding")
        self.assertEqual(f["count"], 2)          # 40 min + 8 min
        self.assertEqual(f["deep_count"], 1)     # only the 40 min one is >= 25 min

    def test_switches_count_app_changes_not_breaks(self):
        desktop(self.conn, at(9), at(9, 10), "Code.exe", "a.py")
        desktop(self.conn, at(9, 10), at(9, 20), "Code.exe", "b.py")       # same app: no switch
        desktop(self.conn, at(9, 20), at(9, 30), "Discord.exe")            # switch 1
        desktop(self.conn, at(9, 30), at(9, 40), "Code.exe")               # switch 2
        desktop(self.conn, at(11), at(11, 10), "Spotify.exe")              # after a break: not a switch
        sw = analytics.summary(self.conn, DAY, DAY)["switches"]
        self.assertEqual(sw["count"], 2)

    def test_timeline_merges_same_category_blocks(self):
        desktop(self.conn, at(9), at(9, 10), "Code.exe")
        desktop(self.conn, at(9, 10), at(9, 20), "WindowsTerminal.exe")   # also Coding: merged
        desktop(self.conn, at(9, 20), at(9, 30), "Spotify.exe")
        blocks = analytics.summary(self.conn, DAY, DAY)["timeline"][0]["blocks"]
        self.assertEqual([(b["category"], b["end"] - b["start"]) for b in blocks],
                         [("Coding", 1200), ("Entertainment", 600)])

    def test_multi_day_range_and_midnight_split(self):
        desktop(self.conn, at(23, 30), at(24, 30), "Code.exe")  # 30 min each side of midnight
        s = analytics.summary(self.conn, DAY, DAY + timedelta(days=1))
        self.assertEqual([p["active"] for p in s["per_day"]], [1800, 1800])
        self.assertEqual(s["range"]["days"], 2)

    def test_topics_from_searches_and_pages(self):
        db.insert_searches(self.conn, [
            {"id": "1", "ts": at(9), "engine": "Brave Search", "query": "sql normalization", "url": "https://search.brave.com/search?q=a"},
            {"id": "2", "ts": at(10), "engine": "YouTube", "query": "normalization 3nf", "url": "https://www.youtube.com/results?search_query=b"},
        ])
        desktop(self.conn, at(9), at(10), "brave.exe")
        db.insert_browser_visits(self.conn, [{"id": "v", "start": at(9), "end": at(9, 10),
                                              "url": "https://x.dev/p", "title": "Normalization explained"}])
        s = analytics.summary(self.conn, DAY, DAY)
        top = s["topics"][0]
        self.assertEqual(top["term"], "normalization")
        self.assertEqual((top["searches"], top["pages"]), (2, 1))
        terms = [t["term"] for t in s["topics"]]
        self.assertNotIn("the", terms)
        self.assertEqual(s["search_count"], 2)
        self.assertEqual(s["searches"][0]["query"], "normalization 3nf")  # newest first

    def test_pages_include_scroll(self):
        desktop(self.conn, at(9), at(10), "brave.exe")
        db.insert_browser_visits(self.conn, [{"id": "v", "start": at(9), "end": at(9, 30),
                                              "url": "https://x.dev/p", "title": "Post", "scroll": 70}])
        p = analytics.summary(self.conn, DAY, DAY)["pages"][0]
        self.assertEqual((p["url"], p["seconds"], p["scroll"]), ("https://x.dev/p", 1800, 70.0))

    def test_empty_range(self):
        s = analytics.summary(self.conn, DAY, DAY)
        self.assertEqual(s["totals"]["active"], 0)
        self.assertIsNone(s["focus"]["longest"])
        self.assertIsNone(s["switches"]["per_hour"])


if __name__ == "__main__":
    unittest.main()
