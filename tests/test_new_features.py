"""Rule editing from the dashboard, search what you've read, focus mode. No network.
Run:  python -m unittest discover -s tests -v"""

import json
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import ai_categorize  # noqa: E402
import analytics  # noqa: E402
import categorize  # noqa: E402
import db  # noqa: E402
import find  # noqa: E402
import focus  # noqa: E402
import keys  # noqa: E402
import rules_edit  # noqa: E402
import server  # noqa: E402

DAY = date(2026, 11, 2)
T0 = datetime.combine(DAY, datetime.min.time()).timestamp()


def at(hour, minute=0):
    return T0 + hour * 3600 + minute * 60


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


class TempData(unittest.TestCase):
    """A private copy of categories.json and a private data folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.rules_path = self.dir / "categories.json"
        shutil.copy(categorize.RULES_PATH, self.rules_path)
        self._data = db.DATA_DIR
        db.DATA_DIR = self.dir / "data"

    def tearDown(self):
        db.DATA_DIR = self._data
        self.tmp.cleanup()


class RulesEditTest(TempData):
    def text(self):
        return self.rules_path.read_text(encoding="utf-8")

    def set(self, section, key, category):
        return rules_edit.set_rules([{"section": section, "key": key, "category": category}], self.rules_path)

    def test_adds_a_site_rule_and_keeps_the_file_layout(self):
        before = self.text()
        r = self.set("sites", "https://www.Chess.com/play", "Entertainment")
        self.assertEqual(r["changed"], ["Site chess.com → Entertainment"])
        after = self.text()
        self.assertIn('"_help"', after)
        # Only one line added: everything else, including blank lines between groups, is untouched.
        old = {line.rstrip(",") for line in before.splitlines()}
        added = [line for line in after.splitlines() if line.rstrip(",") not in old]  # the line before gains a comma
        self.assertEqual([a.strip() for a in added], ['"chess.com": "Entertainment"'])
        self.assertEqual(len(after.splitlines()), len(before.splitlines()) + 1)
        rules = categorize.Rules(self.rules_path)
        self.assertEqual(rules.classify_browser("chess.com", "Play", None), ("Entertainment", True))

    def test_updates_existing_rule_case_insensitively_and_removes(self):
        self.set("apps", "spotify.EXE", "Study")  # existing key is "Spotify.exe"
        raw = json.loads(self.text())
        self.assertEqual(raw["apps"]["Spotify.exe"], "Study")
        self.assertNotIn("spotify.EXE", raw["apps"])
        self.set("apps", "Spotify.exe", None)
        self.assertNotIn("Spotify.exe", json.loads(self.text())["apps"])
        categorize.Rules(self.rules_path)  # still valid

    def test_removing_the_last_entry_fixes_the_comma(self):
        self.set("sites", "zzz-last.com", "Study")  # appended last
        self.set("sites", "zzz-last.com", None)
        self.assertNotIn("zzz-last.com", json.loads(self.text())["sites"])

    def test_page_rules_create_the_section_and_win_over_everything(self):
        url = "https://www.youtube.com/watch?v=abc&t=10"
        start = json.loads(self.text())
        start.pop("pages", None)  # your real categories.json may already have page rules
        self.rules_path.write_text(json.dumps(start, indent=2), encoding="utf-8")
        self.set("pages", url, "Study")
        raw = json.loads(self.text())
        self.assertEqual(raw["pages"], {"https://www.youtube.com/watch?v=abc": "Study"})
        rules = categorize.Rules(self.rules_path)
        page = {"title": "GTA 6 gameplay funny moments"}
        self.assertEqual(rules.classify_browser("youtube.com", "", page, "https://www.youtube.com/watch?v=abc"),
                         ("Study", True))
        self.assertEqual(rules.classify_browser("youtube.com", "", page, "https://www.youtube.com/watch?v=zzz")[0],
                         "Entertainment")
        self.set("pages", url, "Work")  # second edit goes into the now-existing section
        self.assertEqual(json.loads(self.text())["pages"], {"https://www.youtube.com/watch?v=abc": "Work"})

    def test_content_only_for_sites_and_bad_input_rejected(self):
        self.set("sites", "news.example.org", "content")
        before = self.text()
        for section, key, cat in [("apps", "x.exe", "content"), ("sites", "x.com", "Napping"),
                                  ("sites", "not a domain!", "Study"), ("pages", "ftp://x", "Study"),
                                  ("keywords", "x", "Study"), ("apps", "", "Study")]:
            with self.assertRaises(rules_edit.RuleError, msg=(section, key, cat)):
                self.set(section, key, cat)
        self.assertEqual(self.text(), before)  # nothing written on failure

    def test_line_endings_kept_and_undo_is_byte_exact(self):
        text = self.rules_path.read_bytes().replace(b"\r\n", b"\n")
        for nl in (b"\n", b"\r\n"):
            original = text.replace(b"\n", nl)
            self.rules_path.write_bytes(original)
            self.set("sites", "c.com", "Study")
            edited = self.rules_path.read_bytes()
            self.assertEqual(edited.count(b"\r\n"), edited.count(b"\n") if nl == b"\r\n" else 0)
            rules_edit.undo(self.rules_path)
            self.assertEqual(self.rules_path.read_bytes(), original)

    def test_undo_restores_and_refuses_after_hand_edits(self):
        original = self.text()
        rules_edit.set_rules([{"section": "sites", "key": "a.com", "category": "Study"},
                              {"section": "channels", "key": "Some Channel", "category": "Study"}], self.rules_path)
        self.assertEqual(rules_edit.recent()[0]["changes"], ["Site a.com → Study", "Channel Some Channel → Study"])
        self.assertEqual(rules_edit.undo(self.rules_path)["undone"][0], "Site a.com → Study")
        self.assertEqual(self.text(), original)
        with self.assertRaises(rules_edit.RuleError):
            rules_edit.undo(self.rules_path)  # nothing left
        self.set("sites", "b.com", "Study")
        self.rules_path.write_text(self.text().replace('"Unsure", "Other"]', '"Unsure", "Other"] '), encoding="utf-8")
        with self.assertRaises(rules_edit.RuleError):
            rules_edit.undo(self.rules_path)  # hand-edited since: don't throw that away


class SummaryRuleInfoTest(unittest.TestCase):
    def test_unsure_rows_carry_urls_and_channel_and_apps_their_rule(self):
        conn = memory_db()
        url = "https://www.youtube.com/watch?v=q1"
        conn.execute("INSERT INTO sessions (start_ts,end_ts,app,title) VALUES (?,?,?,?)",
                     (at(9), at(10), "brave.exe", "x"))
        conn.execute("INSERT INTO sessions (start_ts,end_ts,app,title) VALUES (?,?,?,?)",
                     (at(10), at(11), "Code.exe", "x"))
        conn.execute("INSERT INTO sessions (start_ts,end_ts,app,title,source,url) VALUES (?,?,?,?,?,?)",
                     (at(9), at(10), "youtube.com", "Some video", "browser", url))
        db.upsert_pages(conn, [{"url": url, "kind": "youtube", "title": "Some video", "author": "Some Channel"}])
        s = analytics.summary(conn, DAY, DAY)
        self.assertEqual(s["unsure"][0]["urls"], [url])
        self.assertEqual(s["unsure"][0]["channel"], "Some Channel")
        apps = {a["name"]: a for a in s["top_apps"]}
        self.assertEqual((apps["youtube.com"]["rule_section"], apps["youtube.com"]["rule"]), ("sites", "content"))
        self.assertEqual((apps["Code.exe"]["rule_section"], apps["Code.exe"]["rule"]), ("apps", "Coding"))
        self.assertEqual(s["pages"][0]["urls"], [url])


class FindTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self._key = keys.load_key
        keys.load_key = lambda name: None
        find._page_vecs.clear()
        find._query_vecs.clear()
        pages = [
            {"url": "https://blog.dev/normal-forms", "kind": "page", "title": "Database normalization explained",
             "body": "First normal form removes repeating groups. Third normal form removes transitive dependencies."},
            {"url": "https://blog.dev/btree", "kind": "page", "title": "How B-trees work",
             "body": "A B-tree keeps keys sorted and balanced, used by database indexes."},
            {"url": "https://www.youtube.com/watch?v=v1", "kind": "youtube", "title": "Café lofi beats",
             "author": "Lofi Girl", "description": "music to study to"},
        ]
        db.upsert_pages(self.conn, pages)
        for i, p in enumerate(pages):
            self.conn.execute("INSERT INTO sessions (start_ts,end_ts,app,title,source,url,scroll_pct)"
                              " VALUES (?,?,?,?,?,?,?)", (at(9 + i), at(9 + i, 20), db.domain_of(p["url"]),
                                                          p["title"], "browser", p["url"], 60))
        self.conn.commit()

    def tearDown(self):
        keys.load_key = self._key

    def test_words_with_prefix_highlight_and_stats(self):
        r = find.search(self.conn, "normaliz", meaning=False)
        self.assertEqual([p["title"] for p in r["pages"]], ["Database normalization explained"])
        p = r["pages"][0]
        self.assertEqual(p["match"], ["words"])
        self.assertEqual((p["seconds"], p["scroll"]), (1200, 60))
        r = find.search(self.conn, "transitive dependencies", meaning=False)
        self.assertIn(find.HL_START + "transitive" + find.HL_END, r["pages"][0]["snippet"])

    def test_accents_channel_and_any_word_fallback(self):
        self.assertEqual(find.search(self.conn, "cafe", meaning=False)["pages"][0]["author"], "Lofi Girl")
        self.assertEqual(find.search(self.conn, "lofi girl", meaning=False)["pages"][0]["kind"], "youtube")
        r = find.search(self.conn, "btree indexes zebra", meaning=False)  # no page has all three
        self.assertEqual(r["pages"][0]["title"], "How B-trees work")

    def test_fts_syntax_is_not_injectable(self):
        for q in ['"', "AND OR NOT", "title:x", "a*b(", "NEAR(", "-"]:
            find.search(self.conn, q, meaning=False)  # must not raise

    def test_purged_text_leaves_the_index(self):
        self.conn.execute("UPDATE pages SET scanned_ts = 0")
        db.purge_page_text(self.conn, 60)
        self.assertEqual(find.search(self.conn, "transitive", meaning=False)["pages"], [])
        self.assertEqual(len(find.search(self.conn, "normalization", meaning=False)["pages"]), 1)  # title kept

    def test_unscanned_pages_found_by_tab_title(self):
        self.conn.execute("INSERT INTO sessions (start_ts,end_ts,app,title,source,url) VALUES (?,?,?,?,?,?)",
                          (at(14), at(14, 5), "mybank.lk", "Statement for October", "browser", "https://mybank.lk/s"))
        r = find.search(self.conn, "statement october", meaning=False)
        self.assertEqual(r["pages"][0]["url"], "https://mybank.lk/s")

    def test_meaning_matches_combine_with_words(self):
        keys.load_key = lambda name: "k"
        vecs = {"https://blog.dev/normal-forms": [1, 0, 0], "https://blog.dev/btree": [0.95, 0.3, 0],
                "https://www.youtube.com/watch?v=v1": [0, 0, 1]}
        for url, v in vecs.items():
            self.conn.execute("INSERT INTO page_ai VALUES (?,?,?,?,?,?,?,?)",
                              (url, "m", "h", ai_categorize.pack(v), None, 0, "r", 1.0))
        calls = []

        def fake_embed(texts, key, model=None):
            calls.append(texts)
            return model, [[1, 0.1, 0]]

        r = find.search(self.conn, "splitting tables to avoid redundancy", embed_fn=fake_embed)
        titles = [p["title"] for p in r["pages"]]
        self.assertEqual(titles, ["Database normalization explained", "How B-trees work"])  # lofi: too dissimilar
        self.assertEqual(r["pages"][0]["match"], ["meaning"])
        find.search(self.conn, "splitting tables to avoid redundancy", embed_fn=fake_embed)
        self.assertEqual(len(calls), 1)  # query vector cached
        r = find.search(self.conn, "normalization", embed_fn=fake_embed)
        self.assertEqual(r["pages"][0]["match"], ["meaning", "words"])

    def test_past_searches(self):
        db.insert_searches(self.conn, [{"id": "1", "ts": at(8), "engine": "Google", "query": "Normalization 3NF",
                                        "url": "https://www.google.com/search?q=x"}])
        r = find.search(self.conn, "3nf", meaning=False)
        self.assertEqual(r["searches"][0]["query"], "Normalization 3NF")


class FocusTest(TempData):
    def setUp(self):
        super().setUp()
        self.conn = memory_db()

    def test_default_block_list_comes_from_categories(self):
        block = focus.settings()["block"]
        self.assertIn("netflix.com", block)
        self.assertIn("instagram.com", block)
        self.assertNotIn("youtube.com", block)  # studied on: not blocked by default
        s = focus.save_settings({"block": "https://www.YouTube.com/feed\n tiktok.com, ", "minutes": 25})
        self.assertEqual((s["block"], s["minutes"]), (["tiktok.com", "youtube.com"], 25))
        with self.assertRaises(focus.FocusError):
            focus.save_settings({"block": ["not a site"]})
        with self.assertRaises(focus.FocusError):
            focus.save_settings({"minutes": 1})

    def test_adult_filter_setting(self):
        self.assertEqual(focus.settings()["adult"], "focus")  # on during focus by default
        focus.start(self.conn, 25)
        self.assertTrue(focus.current(self.conn)["adult"])
        self.assertEqual(focus.save_settings({"adult": "always"})["adult"], "always")
        focus.save_settings({"adult": "off"})
        self.assertFalse(focus.current(self.conn)["adult"])
        with self.assertRaises(focus.FocusError):
            focus.save_settings({"adult": "sometimes"})

    def test_start_stop_complete(self):
        now = at(9)
        cur = focus.start(self.conn, 50, "DBMS", now=now)
        self.assertEqual((cur["end"] - cur["start"], cur["label"]), (3000, "DBMS"))
        self.assertEqual(focus.start(self.conn, 25, now=now + 10)["id"], cur["id"])  # one at a time
        self.assertEqual(focus.stop(self.conn, now=now + 600), "stopped")
        self.assertIsNone(focus.current(self.conn, now + 601))
        focus.start(self.conn, 25, now=now + 700)
        self.assertIsNone(focus.current(self.conn, now + 700 + 25 * 60))  # ran out: finished
        self.assertIsNone(focus.stop(self.conn, now=now + 9999))
        results = [r[0] for r in self.conn.execute("SELECT result FROM focus_sessions ORDER BY id")]
        self.assertEqual(results, ["stopped", "completed"])
        with self.assertRaises(focus.FocusError):
            focus.start(self.conn, 1000, now=now + 99999)

    def test_history_scores_productive_time_and_blocks(self):
        focus.start(self.conn, 60, now=at(9))
        self.conn.execute("INSERT INTO sessions (start_ts,end_ts,app,title) VALUES (?,?,?,?)",
                          (at(9), at(9, 45), "Code.exe", "x"))
        self.conn.execute("INSERT INTO sessions (start_ts,end_ts,app,title) VALUES (?,?,?,?)",
                          (at(9, 45), at(10), "Spotify.exe", "x"))
        focus.record_blocks(self.conn, [{"id": "a", "ts": at(9, 10), "domain": "www.instagram.com"},
                                        {"id": "a", "ts": at(9, 10), "domain": "instagram.com"},  # re-sent
                                        {"id": "b", "ts": at(9, 20), "domain": "netflix.com"},
                                        {"id": "c", "ts": at(12), "domain": "netflix.com"}])     # no session then
        h = focus.history(self.conn, at(0), at(24), now=at(11))
        s = h["sessions"][0]
        self.assertEqual((s["result"], s["seconds"], s["productive_seconds"]), ("completed", 3600, 2700))
        self.assertEqual(s["blocked"], 2)
        self.assertEqual((h["completed"], h["stopped"], h["blocked"]), (1, 0, 2))

    def test_ingest_reports_focus_to_the_extension(self):
        class NoClose:  # ingest closes its connection; keep the in-memory one alive
            def __init__(s, c):
                s.c = c

            def __getattr__(s, name):
                return getattr(s.c, name)

            def close(s):
                pass

        conn_factory = db.connect
        db.connect = lambda: NoClose(self.conn)
        try:
            st = server.SharedState()
            self.assertIsNone(server.ingest({}, st)["focus"])
            focus.start(self.conn, 25)
            reply = server.ingest({"focus_blocks": [{"id": "x", "ts": time.time(), "domain": "x.com"}]}, st)
            self.assertEqual(reply["focus"]["block"], focus.settings()["block"])
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM focus_blocks").fetchone()[0], 1)
        finally:
            db.connect = conn_factory


if __name__ == "__main__":
    unittest.main()
