"""Daily backups of tracker.db -> data/backups/tracker-YYYY-MM-DD.db.zip (last KEEP kept).

Uses SQLite's online backup API, so the copy is consistent even while the tracker and
the extension server are writing. Zipped because page text compresses well.

Restore (see README): stop the tracker, unzip a backup over data/tracker.db, delete
data/tracker.db-wal and data/tracker.db-shm, start the tracker again.

    python desktop/backup.py          # make one now
"""

import logging
import sqlite3
import time
import zipfile
from datetime import date
from pathlib import Path

import db

log = logging.getLogger("tracker")

BACKUP_DIR = db.DATA_DIR / "backups"
KEEP = 14


def backup_files(directory: Path | None = None) -> list[Path]:
    directory = directory or BACKUP_DIR
    return sorted(directory.glob("tracker-????-??-??.db.zip")) if directory.exists() else []


def make_backup(conn, directory: Path | None = None, day: date | None = None, keep: int = KEEP) -> Path:
    directory = directory or BACKUP_DIR
    directory.mkdir(parents=True, exist_ok=True)
    day = day or date.today()
    target = directory / f"tracker-{day.isoformat()}.db.zip"
    tmp_db = directory / f".tracker-{day.isoformat()}.tmp.db"
    tmp_zip = target.with_suffix(".zip.tmp")
    try:
        dest = sqlite3.connect(tmp_db)
        try:
            conn.backup(dest, pages=2048, sleep=0.005)  # small steps: never blocks writers for long
        finally:
            dest.close()
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(tmp_db, arcname="tracker.db")
        tmp_zip.replace(target)  # atomic: a half-written backup never looks complete
    finally:
        tmp_db.unlink(missing_ok=True)
        tmp_zip.unlink(missing_ok=True)

    for old in backup_files(directory)[:-keep]:
        old.unlink(missing_ok=True)
    db.set_meta(conn, "last_backup", time.time())
    log.info("backup written: %s (%.1f MB)", target.name, target.stat().st_size / 1e6)
    return target


def backup_if_due(conn, directory: Path | None = None, day: date | None = None) -> Path | None:
    """One backup per calendar day."""
    day = day or date.today()
    target = (directory or BACKUP_DIR) / f"tracker-{day.isoformat()}.db.zip"
    return None if target.exists() else make_backup(conn, directory, day)


def status(conn, directory: Path | None = None) -> dict:
    files = backup_files(directory)
    last = db.get_meta(conn, "last_backup")
    return {
        "count": len(files),
        "latest": files[-1].name if files else None,
        "last_ts": float(last) if last else None,
        "total_mb": round(sum(f.stat().st_size for f in files) / 1e6, 1),
        "keep": KEEP,
    }


if __name__ == "__main__":
    c = db.connect()
    try:
        print(f"Backup written: {make_backup(c)}")
    finally:
        c.close()
