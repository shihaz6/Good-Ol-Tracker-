"""Groq AI judge (ai_judge.py) + shared site lists. No network: Groq is faked.
Run:  python -m unittest discover -s tests -v"""

import json
import sqlite3
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import ai_judge  # noqa: E402
import analytics  # noqa: E402
import categorize  # noqa: E402
import db  # noqa: E402
import keys  # noqa: E402
import sitelists  # noqa: E402

DAY = date(2026, 11, 2)
T0 = datetime.combine(DAY, datetime.min.time()).timestamp()
NOW = T0 + 20 * 3600


def at(hour, minute=0):
    return T0 + hour * 3600 + minute * 60


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


class SiteListsTest(unittest.TestCase):
    def test_never_send_matches_the_extension_blocklist(self):
        for host in ("web.whatsapp.com", "www.sampath.lk", "combank.lk", "online.peoples-bank.lk",
                     "vault.bitwarden.com", "passwords.google.com"):
            self.assertTrue(sitelists.never_send(host), host)
        for host in ("claude.ai", "youtube.com", "shoeshop-example.com"):
            self.assertFalse(sitelists.never_send(host), host)

    def test_apps(self):
        for exe in ("Bitwarden.exe", "KeePassXC.exe", "1Password.exe", "SampathBankApp.exe"):
            self.assertTrue(sitelists.never_send_app(exe.lower()), exe)
        for exe in ("photos.exe", "snippingtool.exe", "code.exe"):
            self.assertFalse(sitelists.never_send_app(exe), exe)

    def test_adult_matches_adult_js(self):
        self.assertTrue(sitelists.is_adult("www.pornhub.com"))
        self.assertTrue(sitelists.is_adult("freepornvideos.net"))
        self.assertTrue(sitelists.is_adult("something.xxx"))
        for host in ("essex.ac.uk", "xbox.com", "sussex.ac.uk", "github.com"):
            self.assertFalse(sitelists.is_adult(host), host)


class ClassifyOrderTest(unittest.TestCase):
    def setUp(self):
        self.r = categorize.Rules()

    def test_adult_site_without_rule_is_entertainment(self):
        self.assertEqual(self.r.classify_browser_ex("pornhub.com", "x", None), ("Entertainment", True, "adult"))

    def test_judge_only_fills_what_others_left_unsure(self):
        page = {"judge_category": "Study"}
        self.assertEqual(self.r.classify_browser_ex("claude.ai", "Brain tumor causes", page), ("Study", True, "groq"))
        # keywords still win over the judge
        self.assertEqual(self.r.classify_browser_ex("claude.ai", "Debug this traceback error", page)[0], "Coding")
        # Voyage wins over the judge
        self.assertEqual(self.r.classify_browser_ex("claude.ai", "Chat", {**page, "ai_category": "Work"})[2], "voyage")
        # a site rule wins
        self.assertEqual(self.r.classify_browser_ex("github.com", "x", page), ("Coding", True, "rule"))
        # unknown categories are ignored
        self.assertEqual(self.r.classify_browser_ex("claude.ai", "Chat", {"judge_category": "Nope"})[0], "Unsure")


class FakeGroq:
    """Answers every item with the category from `answers` (keyed by a word in the item)."""

    def __init__(self, answers):
        self.answers = answers
        self.bodies = []

    def __call__(self, key, path, body):
        assert path == "/chat/completions"
        self.bodies.append(body)
        items = json.loads(body["messages"][1]["content"])["items"]
        results = []
        for it in items:
            text = json.dumps(it).lower()
            cat, conf = next(((c, p) for word, (c, p) in self.answers.items() if word in text), ("Browsing", 0.3))
            results.append({"id": it["id"], "category": cat, "confidence": conf, "reason": f"because {cat}"})
        return {"choices": [{"message": {"content": json.dumps({"results": results})}}]}


class JudgeRunTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.rules = categorize.Rules()
        self._key = keys.load_key
        keys.load_key = lambda name: "test-key"
        ai_judge.status.update(model=None, paused_until=0.0, last_error=None)
        c = self.conn
        rows = [
            # (start, end, app, title, url)
            (at(9), at(9, 20), "claude.ai", "Brain tumor causes - Claude", "https://claude.ai/chat/1"),
            (at(9, 20), at(9, 30), "claude.ai", "Weekend plans - Claude", "https://claude.ai/chat/2"),
            (at(9, 30), at(9, 40), "youtube.com", "Java full course", "https://www.youtube.com/watch?v=j"),
            (at(9, 40), at(9, 50), "shoeshop-example.com", "Shoeshop-example Sri Lanka", "https://www.shoeshop-example.com/sl"),
            (at(9, 50), at(10), "combank.lk", "Online banking", "https://combank.lk/login"),
            (at(10), at(10, 5), "pornhub.com", "x", "https://pornhub.com/"),
        ]
        for s, e, app, title, url in rows:
            c.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title, url) VALUES (?, ?, 'browser', ?, ?, ?)",
                      (s, e, app, title, url))
        c.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title) VALUES (?, ?, 'desktop', 'brave.exe', 'b')",
                  (at(9), at(10, 5)))
        c.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title) VALUES (?, ?, 'desktop', 'Photos.exe', 'IMG_2041.jpg - Photos')",
                  (at(11), at(11, 5)))
        c.execute("INSERT INTO sessions (start_ts, end_ts, source, app, title) VALUES (?, ?, 'desktop', 'Bitwarden.exe', 'My vault')",
                  (at(11, 5), at(11, 10)))
        c.execute("INSERT INTO pages (url, domain, kind, title, body, scanned_ts) VALUES (?, 'claude.ai', 'page', ?, ?, ?)",
                  ("https://claude.ai/chat/1", "Brain tumor causes", "What causes brain tumours? Genetic factors " * 40, at(9)))
        c.commit()
        self.groq = FakeGroq({"brain": ("Study", 0.9), "weekend": ("Browsing", 0.4),
                              "shoeshop-example": ("Browsing", 0.85), "photos": ("Other", 0.8)})

    def tearDown(self):
        keys.load_key = self._key

    def run_judge(self):
        return ai_judge.run_batch(self.conn, self.rules, request=self.groq, now=NOW)

    def sent(self):
        return " ".join(json.dumps(b["messages"][1]["content"]) for b in self.groq.bodies).lower()

    def test_only_unsure_pages_and_unruled_names_are_sent(self):
        r = self.run_judge()
        self.assertEqual(r["pages"], 2)   # the two claude.ai chats; YouTube was decided by keywords
        self.assertEqual(r["names"], 2)   # shoeshop-example.com + Photos.exe
        sent = self.sent()
        for private in ("combank", "online banking", "bitwarden", "my vault", "pornhub"):
            self.assertNotIn(private, sent)
        self.assertIn("genetic factors", sent)              # option B: start of the page text ...
        self.assertLess(sent.count("genetic factors"), 30)  # ... but only an excerpt
        self.assertIn("img_2041.jpg", sent)

    def test_confident_answers_categorise_pages(self):
        self.run_judge()
        items = categorize.categorise(self.conn, at(0), at(24), self.rules)
        by_url = {it.url: it for it in items if it.url}
        self.assertEqual((by_url["https://claude.ai/chat/1"].category, by_url["https://claude.ai/chat/1"].via),
                         ("Study", "groq"))
        self.assertEqual(by_url["https://claude.ai/chat/2"].category, "Unsure")  # 0.4: not confident
        self.assertEqual(by_url["https://pornhub.com/"].category, "Entertainment")

    def test_asked_once(self):
        self.run_judge()
        n = len(self.groq.bodies)
        self.assertEqual(self.run_judge()["requests"], 0)
        self.assertEqual(len(self.groq.bodies), n)
        # page text changes -> asked again
        self.conn.execute("UPDATE pages SET body = 'something new' WHERE url = 'https://claude.ai/chat/1'")
        self.assertEqual(self.run_judge()["pages"], 1)

    def test_suggestions_and_dismiss(self):
        self.run_judge()
        s = ai_judge.suggestions(self.conn, self.rules)
        self.assertEqual(s[("site", "shoeshop-example.com")]["category"], "Browsing")
        self.assertEqual(s[("app", "photos.exe")]["category"], "Other")
        summary = analytics.summary(self.conn, DAY, DAY, self.rules)
        um = {u["name"]: u for u in summary["unmatched"]}
        self.assertEqual(um["shoeshop-example.com"]["suggestion"]["category"], "Browsing")
        page = next(p for p in summary["pages"] if p["url"] == "https://claude.ai/chat/1")
        self.assertEqual(page["category"], "Study")
        self.assertEqual(page["ai"]["reason"], "because Study")
        ai_judge.dismiss(self.conn, "site", "shoeshop-example.com")
        self.assertNotIn(("site", "shoeshop-example.com"), ai_judge.suggestions(self.conn, self.rules))
        self.assertEqual(self.run_judge()["names"], 0)  # not asked again
        with self.assertRaises(ValueError):
            ai_judge.dismiss(self.conn, "site", "never-suggested.com")

    def test_rate_limit_pauses(self):
        def boom(*a):
            raise ai_judge.weekly.SummaryError("Groq rate limit reached")
        with self.assertRaises(ai_judge.JudgeError):
            ai_judge.run_batch(self.conn, self.rules, request=boom, now=NOW)
        self.assertEqual(ai_judge.run_batch(self.conn, self.rules, request=self.groq, now=NOW + 60)["status"], "waiting")
        self.assertEqual(ai_judge.run_batch(self.conn, self.rules, request=self.groq,
                                            now=NOW + ai_judge.BACKOFF + 1)["status"], "ok")

    def test_bad_reply_is_an_error_not_a_crash(self):
        bad = lambda *a: {"choices": [{"message": {"content": "not json"}}]}  # noqa: E731
        with self.assertRaises(ai_judge.JudgeError):
            ai_judge.run_batch(self.conn, self.rules, request=bad, now=NOW)

    def test_no_key(self):
        keys.load_key = lambda name: None
        self.assertEqual(self.run_judge()["status"], "no_key")


if __name__ == "__main__":
    unittest.main()
