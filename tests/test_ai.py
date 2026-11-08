"""AI categoriser + weekly summary, with fake APIs (no network).
Run:  python -m unittest discover -s tests -v"""

import json
import sqlite3
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import ai_categorize  # noqa: E402
import categorize  # noqa: E402
import db  # noqa: E402
import keys  # noqa: E402
import weekly  # noqa: E402

DAY = date(2026, 11, 2)  # a Monday
T0 = datetime.combine(DAY, datetime.min.time()).timestamp()


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


class FakeVoyage:
    """Bag-of-words 'embeddings': dim 0 = study words, dim 1 = fun words, dim 2 = noise."""
    STUDY = ("lecture", "tutorial", "normalization", "course")
    FUN = ("gaming", "funny", "gameplay", "meme")

    def __init__(self):
        self.calls = []

    def __call__(self, texts, key, model):
        self.calls.append(list(texts))
        def vec(t):
            t = t.lower()
            return [sum(w in t for w in self.STUDY), sum(w in t for w in self.FUN), 0.05]
        return (model or "fake-model"), [vec(t) for t in texts]


def rules_with(descriptions):
    r = categorize.Rules()
    r.descriptions = descriptions
    return r


class AICategoriserTest(unittest.TestCase):
    def setUp(self):
        self._load_key = keys.load_key
        keys.load_key = lambda name: "test-key"
        ai_categorize.status.update(model=None, last_error=None)
        self.conn = memory_db()
        self.rules = rules_with({"Study": ["a lecture or tutorial"], "Entertainment": ["funny gaming clips"]})
        self.fake = FakeVoyage()

    def tearDown(self):
        keys.load_key = self._load_key

    def add_page(self, url, title, body="", ts=1.0):
        db.upsert_pages(self.conn, [{"url": url, "kind": "page", "ts": ts, "title": title, "body": body}])

    def ai_cat(self, url):
        return self.conn.execute("SELECT category FROM page_ai WHERE url = ?", (url,)).fetchone()[0]

    def test_classify_needs_clear_winner(self):
        protos = {"Study": [[1, 0, 0]], "Entertainment": [[0, 1, 0]]}
        self.assertEqual(ai_categorize.classify([0.9, 0.1, 0], protos)[0], "Study")
        self.assertIsNone(ai_categorize.classify([1, 1, 0], protos)[0])   # tie -> undecided
        self.assertIsNone(ai_categorize.classify([0, 0, 1], protos)[0])   # unlike anything

    def test_pages_categorised_and_cached(self):
        self.add_page("https://a.dev/1", "Database normalization lecture")
        self.add_page("https://b.dev/2", "Funny gaming moments")
        res = ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)
        self.assertEqual(res["embedded"], 2)
        self.assertEqual(self.ai_cat("https://a.dev/1"), "Study")
        self.assertEqual(self.ai_cat("https://b.dev/2"), "Entertainment")
        calls = len(self.fake.calls)
        # Second run: nothing new -> no API calls at all.
        res = ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)
        self.assertEqual((res["embedded"], res["reclassified"]), (0, 0))
        self.assertEqual(len(self.fake.calls), calls)

    def test_changed_descriptions_reclassify_without_re_embedding_pages(self):
        self.add_page("https://a.dev/1", "Database normalization lecture")
        ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)
        calls = len(self.fake.calls)
        rules2 = rules_with({"Work": ["a lecture or tutorial"], "Entertainment": ["funny gaming clips",
                                                                                  "a meme compilation"]})
        res = ai_categorize.run_batch(self.conn, rules2, embed_fn=self.fake)
        self.assertEqual((res["embedded"], res["reclassified"]), (0, 1))
        self.assertEqual(self.ai_cat("https://a.dev/1"), "Work")
        # only the one genuinely new description text was embedded; the page was not
        self.assertEqual(self.fake.calls[calls:], [["a meme compilation"]])

    def test_purged_text_keeps_existing_result(self):
        self.add_page("https://a.dev/1", "Notes", body="normalization lecture tutorial")
        ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)
        self.conn.execute("UPDATE pages SET body = NULL, scanned_ts = 99")  # 60-day purge + later touch
        calls = len(self.fake.calls)
        ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(self.ai_cat("https://a.dev/1"), "Study")

    def test_no_key_does_nothing(self):
        keys.load_key = lambda name: None
        self.add_page("https://a.dev/1", "lecture")
        self.assertEqual(ai_categorize.run_batch(self.conn, self.rules, embed_fn=self.fake)["status"], "no_key")
        self.assertEqual(self.fake.calls, [])

    def test_rules_beat_ai_and_ai_fills_gaps(self):
        r = categorize.Rules()
        page = {"title": "My morning routine", "ai_category": "Entertainment"}
        # content site with no keyword winner: AI decides instead of "Unsure"
        self.assertEqual(r.classify_browser("youtube.com", "", page), ("Entertainment", True))
        # unknown site: AI decides instead of "Browsing / not categorised"
        self.assertEqual(r.classify_browser("someblog.dev", "Home", page), ("Entertainment", True))
        # an explicit site rule still wins
        self.assertEqual(r.classify_browser("github.com", "x", page), ("Coding", True))
        # keywords still win over AI
        page2 = {"title": "DBMS lecture 4", "ai_category": "Entertainment"}
        self.assertEqual(r.classify_browser("youtube.com", "", page2), ("Study", True))
        # a stale AI category that no longer exists is ignored
        self.assertEqual(r.classify_browser("someblog.dev", "Home", {"ai_category": "Gone"}), ("Browsing", False))


class WeeklyTest(unittest.TestCase):
    def setUp(self):
        self._load_key = keys.load_key
        keys.load_key = lambda name: "test-key"
        self.conn = memory_db()
        c = self.conn
        c.execute("INSERT INTO sessions (start_ts, end_ts, app, title) VALUES (?,?,?,?)",
                  (T0 + 9 * 3600, T0 + 11 * 3600, "Code.exe", "secret_project.py - VS Code"))
        c.execute("INSERT INTO sessions (start_ts, end_ts, app, title) VALUES (?,?,?,?)",
                  (T0 + 11 * 3600, T0 + 12 * 3600, "brave.exe", "x"))
        db.insert_browser_visits(c, [{"id": "v", "start": T0 + 11 * 3600, "end": T0 + 12 * 3600,
                                      "url": "https://private.example/diary?id=7", "title": "My private diary page"}])
        db.upsert_pages(c, [{"url": "https://private.example/diary?id=7", "kind": "page", "ts": T0,
                             "title": "My private diary page", "body": "VERY PERSONAL BODY TEXT"}])
        db.insert_searches(c, [{"id": "s", "ts": T0 + 3600 * 10, "engine": "Google",
                                "query": "embarrassing medical question", "url": "https://www.google.com/search?q=x"}])

    def tearDown(self):
        keys.load_key = self._load_key

    def test_payload_has_totals_but_no_private_text(self):
        payload = weekly.build_payload(self.conn, DAY, DAY)
        blob = json.dumps(payload).lower()
        for secret in ("very personal body text", "embarrassing medical question", "private.example/diary",
                       "secret_project.py", "diary?id=7"):
            self.assertNotIn(secret, blob, secret)
        self.assertEqual(payload["totals"]["active_hours"], 3.0)
        self.assertEqual(payload["hours_by_category"]["Coding"], 2.0)
        self.assertEqual(payload["number_of_searches"], 1)
        self.assertIsNone(payload["previous_period"])

    def test_generate_saves_and_returns_summary(self):
        sent = {}

        def fake_request(key, path, body):
            sent.update(body)
            return {"choices": [{"message": {"content": "## Overview\nGood day."}}]}

        s = weekly.generate(self.conn, DAY, DAY, request=fake_request)
        self.assertEqual(s["text"], "## Overview\nGood day.")
        self.assertEqual((s["start"], s["end"]), ("2026-11-02", "2026-11-02"))
        self.assertEqual(weekly.latest(self.conn)["id"], s["id"])
        self.assertNotIn("VERY PERSONAL", json.dumps(sent))
        self.assertEqual(sent["messages"][0]["role"], "system")

    def test_not_enough_data_and_no_key(self):
        empty = memory_db()
        with self.assertRaises(weekly.SummaryError):
            weekly.generate(empty, DAY, DAY, request=lambda *a: None)
        keys.load_key = lambda name: None
        with self.assertRaises(weekly.SummaryError):
            weekly.generate(self.conn, DAY, DAY, request=lambda *a: None)

    def test_due_week_is_last_full_mon_to_sun(self):
        wednesday = date(2026, 11, 11)
        self.assertEqual(weekly.due_week(self.conn, wednesday), (date(2026, 11, 2), date(2026, 11, 8)))
        self.assertIsNone(weekly.due_week(memory_db(), wednesday))  # week with no activity: skipped
        self.conn.execute("INSERT INTO summaries (created_ts, start_day, end_day, model, payload, text)"
                          " VALUES (0, '2026-11-02', '2026-11-08', 'm', '{}', 't')")
        self.assertIsNone(weekly.due_week(self.conn, wednesday))


if __name__ == "__main__":
    unittest.main()
