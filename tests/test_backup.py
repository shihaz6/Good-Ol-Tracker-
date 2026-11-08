"""Run:  python -m unittest discover -s tests -v"""

import sqlite3
import sys
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import backup  # noqa: E402
import db  # noqa: E402


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    conn.execute("INSERT INTO sessions (start_ts, end_ts, app, title) VALUES (1, 2, 'Code.exe', 'x')")
    conn.commit()
    return conn


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = memory_db()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_backup_is_a_working_database(self):
        path = backup.make_backup(self.conn, self.dir, date(2026, 11, 8))
        self.assertEqual(path.name, "tracker-2026-11-08.db.zip")
        with zipfile.ZipFile(path) as z:
            self.assertEqual(z.namelist(), ["tracker.db"])
            z.extract("tracker.db", self.dir / "restore")
        restored = sqlite3.connect(self.dir / "restore" / "tracker.db")
        self.assertEqual(restored.execute("SELECT app FROM sessions").fetchall(), [("Code.exe",)])
        restored.close()
        # no temp files left behind
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.is_file()), [path.name])

    def test_keeps_only_the_newest(self):
        start = date(2026, 10, 1)
        for i in range(17):
            backup.make_backup(self.conn, self.dir, start + timedelta(days=i), keep=14)
        names = [p.name for p in backup.backup_files(self.dir)]
        self.assertEqual(len(names), 14)
        self.assertEqual(names[0], "tracker-2026-10-04.db.zip")
        self.assertEqual(names[-1], "tracker-2026-10-17.db.zip")

    def test_once_per_day(self):
        day = date(2026, 11, 8)
        self.assertIsNotNone(backup.backup_if_due(self.conn, self.dir, day))
        self.assertIsNone(backup.backup_if_due(self.conn, self.dir, day))
        self.assertIsNotNone(backup.backup_if_due(self.conn, self.dir, day + timedelta(days=1)))

    def test_status(self):
        self.assertEqual(backup.status(self.conn, self.dir)["count"], 0)
        backup.make_backup(self.conn, self.dir, date(2026, 11, 8))
        s = backup.status(self.conn, self.dir)
        self.assertEqual((s["count"], s["latest"]), (1, "tracker-2026-11-08.db.zip"))
        self.assertIsNotNone(s["last_ts"])

    def test_meta_roundtrip(self):
        db.set_meta(self.conn, "k", 1.5)
        self.assertEqual(db.get_meta(self.conn, "k"), "1.5")
        self.assertEqual(db.get_meta(self.conn, "missing", "d"), "d")


if __name__ == "__main__":
    unittest.main()
