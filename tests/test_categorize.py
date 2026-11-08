"""Run:  python -m unittest discover -s tests -v"""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import categorize  # noqa: E402
import db  # noqa: E402


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


def add_desktop(conn, start, end, app, title):
    conn.execute("INSERT INTO sessions (start_ts, end_ts, app, title) VALUES (?,?,?,?)",
                 (start, end, app, title))


def add_visit(conn, cid, start, end, url, title):
    db.insert_browser_visits(conn, [{"id": cid, "start": start, "end": end, "url": url, "title": title}])


class RulesFileTest(unittest.TestCase):
    def test_shipped_rules_load(self):
        categorize.Rules()  # raises on bad JSON or unknown category names


class ClassifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = categorize.Rules()

    def test_app_rules_case_insensitive(self):
        self.assertEqual(self.r.classify_desktop("code.EXE", "x"), ("Coding", True))
        self.assertEqual(self.r.classify_desktop("WindowsTerminal.exe", ""), ("Coding", True))

    def test_unknown_app_is_flagged(self):
        self.assertEqual(self.r.classify_desktop("mystery.exe", "untitled"), ("Other", False))

    def test_unknown_app_with_clear_title_keyword(self):
        self.assertEqual(self.r.classify_desktop("mystery.exe", "DBMS lecture 4"), ("Study", True))

    def test_site_rules_and_subdomains(self):
        self.assertEqual(self.r.classify_browser("github.com", "x", None), ("Coding", True))
        self.assertEqual(self.r.classify_browser("gist.github.com", "x", None), ("Coding", True))
        self.assertEqual(self.r.classify_browser("en.wikipedia.org", "x", None), ("Study", True))

    def test_most_specific_site_wins(self):
        # music.youtube.com has its own rule; youtube.com is "content"
        self.assertEqual(self.r.classify_browser("music.youtube.com", "x", None), ("Entertainment", True))
        self.assertEqual(self.r.classify_browser("mail.google.com", "Inbox", None), ("Communication", True))

    def test_unknown_site_is_flagged(self):
        self.assertEqual(self.r.classify_browser("porsche.com", "Porsche Sri Lanka", None), ("Browsing", False))

    def test_youtube_study_vs_entertainment(self):
        java = {"title": "Java OOP Full Course", "author": "Some Teacher",
                "description": "Learn classes, inheritance and polymorphism"}
        gta = {"title": "GTA 5 funny moments", "author": "Gamer", "description": "best gameplay clips"}
        self.assertEqual(self.r.classify_browser("youtube.com", "", java), ("Study", True))
        self.assertEqual(self.r.classify_browser("youtube.com", "", gta), ("Entertainment", True))

    def test_channel_rule_beats_keywords(self):
        page = {"title": "Funny gameplay compilation", "author": "freeCodeCamp.org"}
        self.assertEqual(self.r.classify_browser("youtube.com", "", page), ("Study", True))

    def test_ambiguous_content_is_unsure_not_guessed(self):
        page = {"title": "My morning routine", "author": "Someone"}
        self.assertEqual(self.r.classify_browser("youtube.com", "", page), ("Unsure", True))

    def test_content_site_without_scan_uses_tab_title(self):
        self.assertEqual(self.r.classify_browser("google.com", "sql normalization - Google Search", None),
                         ("Study", True))

    def test_broad_phrases_dont_make_everything_study(self):
        for title in ("How to cook perfect rice", "Learn to skateboard in 10 minutes",
                      "Of course I bought it"):
            self.assertNotEqual(self.r.classify_browser("youtube.com", "", {"title": title})[0],
                                "Study", title)

    def test_programming_videos_are_study_errors_are_coding(self):
        fireship = {"title": "React in 100 seconds", "author": "Fireship"}
        self.assertEqual(self.r.classify_browser("youtube.com", "", fireship)[0], "Study")
        err = {"title": "Fix Python error: ModuleNotFoundError"}
        self.assertEqual(self.r.classify_browser("youtube.com", "", err)[0], "Coding")

    def test_google_account_pages_not_unsure(self):
        self.assertEqual(self.r.classify_browser("accounts.google.com", "Sign in", None), ("Other", True))
        self.assertEqual(self.r.classify_browser("maps.google.com", "Maps", None), ("Browsing", True))

    def test_brave_search_page_judged_by_query(self):
        self.assertEqual(self.r.classify_browser("search.brave.com", "dbms normalization - Brave Search",
                                                 None)[0], "Study")

    def test_whole_words_only(self):
        # "lab" must not match inside "label"/"collaborate"
        self.assertIsNone(self.r.classify_text([("label collaborate", 3)]))
        self.assertEqual(self.r.classify_text([("learning c++ today", 3)]), "Study")


class SplitTest(unittest.TestCase):
    def test_browser_time_is_split_by_extension_visits(self):
        conn = memory_db()
        add_desktop(conn, 0, 100, "Code.exe", "tracker.py")
        add_desktop(conn, 100, 400, "brave.exe", "whatever - Brave")
        add_visit(conn, "a", 100, 200, "https://github.com/x/y", "repo")
        add_visit(conn, "b", 200, 350, "https://www.linkedin.com/feed", "Feed")
        # 350-400: no visit (e.g. new tab page) -> Browsing gap
        items = categorize.categorise(conn, 0, 1000)
        got = [(it.start, it.end, it.category) for it in items]
        self.assertEqual(got, [(0, 100, "Coding"), (100, 200, "Coding"),
                               (200, 350, "Social"), (350, 400, "Browsing")])

    def test_desktop_is_the_time_truth(self):
        conn = memory_db()
        add_desktop(conn, 0, 100, "brave.exe", "x - Brave")
        add_desktop(conn, 100, 200, "Code.exe", "y")
        # extension visit runs past the moment the user switched to VS Code
        add_visit(conn, "a", 0, 180, "https://www.netflix.com/watch/1", "Netflix")
        items = categorize.categorise(conn, 0, 1000)
        self.assertEqual(sum(it.seconds for it in items), 200)
        self.assertEqual([(it.category, it.seconds) for it in items],
                         [("Entertainment", 100), ("Coding", 100)])

    def test_overlapping_visits_never_double_count(self):
        conn = memory_db()
        add_desktop(conn, 0, 100, "brave.exe", "x")
        add_visit(conn, "a", 0, 60, "https://github.com/a", "a")
        add_visit(conn, "b", 40, 100, "https://www.linkedin.com/b", "b")
        items = categorize.categorise(conn, 0, 1000)
        self.assertEqual(sum(it.seconds for it in items), 100)

    def test_day_boundaries_clip(self):
        conn = memory_db()
        add_desktop(conn, -50, 50, "Code.exe", "x")
        items = categorize.categorise(conn, 0, 1000)
        self.assertEqual([(it.start, it.end) for it in items], [(0, 50)])


class PageContentTest(unittest.TestCase):
    def test_unknown_site_categorised_by_scanned_content(self):
        r = categorize.Rules()
        page = {"title": "Understanding 3NF", "body": "database normalization explained with sql examples"}
        self.assertEqual(r.classify_browser("someblog.dev", "Understanding 3NF", page), ("Study", True))
        # no scan and a vague title: still flagged for a rule
        self.assertEqual(r.classify_browser("someblog.dev", "Home", None), ("Browsing", False))

    def test_generic_page_kind_accepted(self):
        conn = memory_db()
        n = db.upsert_pages(conn, [{"url": "https://someblog.dev/post", "kind": "page", "title": "t", "body": "b"}])
        self.assertEqual(n, 1)

    def test_scroll_depth_keeps_the_maximum(self):
        conn = memory_db()
        v = {"id": "v1", "start": 0, "end": 10, "url": "https://a.com/x", "title": "t"}
        db.insert_browser_visits(conn, [{**v, "scroll": 40}])
        db.insert_browser_visits(conn, [{**v, "end": 20, "scroll": 85}])
        db.insert_browser_visits(conn, [{**v, "end": 30, "scroll": 10}])    # scrolled back up
        db.insert_browser_visits(conn, [{**v, "end": 40, "scroll": None}])  # older extension
        db.insert_browser_visits(conn, [{**v, "id": "v2", "scroll": 250}])  # clamped
        rows = conn.execute("SELECT client_id, scroll_pct FROM sessions ORDER BY client_id").fetchall()
        self.assertEqual(rows, [("v1", 85.0), ("v2", 100.0)])

    def test_unknown_scroll_stays_unknown(self):
        # regression: re-sending a live visit without scroll data turned NULL into 0%
        conn = memory_db()
        v = {"id": "v1", "start": 0, "end": 10, "url": "https://a.com/x", "title": "t", "scroll": None}
        db.insert_browser_visits(conn, [v])
        db.insert_browser_visits(conn, [{**v, "end": 20}])
        self.assertIsNone(conn.execute("SELECT scroll_pct FROM sessions").fetchone()[0])
        db.insert_browser_visits(conn, [{**v, "end": 30, "scroll": 40}])
        self.assertEqual(conn.execute("SELECT scroll_pct FROM sessions").fetchone()[0], 40.0)

    def test_retention_drops_old_text_keeps_titles(self):
        import time
        conn = memory_db()
        old, new = time.time() - 61 * 86400, time.time() - 59 * 86400
        db.upsert_pages(conn, [
            {"url": "https://a.com/old", "kind": "page", "ts": old, "title": "Old", "body": "old text",
             "headings": "h", "description": "d"},
            {"url": "https://a.com/new", "kind": "page", "ts": new, "title": "New", "body": "new text"},
        ])
        self.assertEqual(db.purge_page_text(conn, 60), 1)
        self.assertEqual(db.purge_page_text(conn, 60), 0)  # idempotent
        rows = conn.execute("SELECT title, description, headings, body FROM pages ORDER BY title").fetchall()
        self.assertEqual(rows, [("New", "", "", "new text"), ("Old", None, None, None)])


class SearchesTest(unittest.TestCase):
    def test_insert_dedupes_and_validates(self):
        conn = memory_db()
        good = {"id": "s1", "ts": 100, "engine": "Brave Search", "query": "  java oop ",
                "url": "https://search.brave.com/search?q=java+oop#frag"}
        n = db.insert_searches(conn, [
            good,
            good,                                                   # re-send: ignored
            {"id": "s2", "ts": 100, "query": "x", "url": "javascript:alert(1)"},  # bad url
            {"id": "s3", "ts": 100, "query": "   ", "url": "https://a.com/?q="},  # empty
            {"id": "s4", "ts": 100, "url": "https://a.com/?q=x"},                # no query
        ])
        rows = conn.execute("SELECT engine, domain, query, url FROM searches").fetchall()
        self.assertEqual(rows, [("Brave Search", "search.brave.com", "java oop",
                                 "https://search.brave.com/search?q=java+oop")])
        self.assertEqual(n, 2)  # both copies of the good one passed validation; one was stored


if __name__ == "__main__":
    unittest.main()
