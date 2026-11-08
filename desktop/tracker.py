"""Desktop activity logger + local server for the browser extension.

Every INTERVAL seconds: read the foreground app + window title and the idle time.
Consecutive identical samples are merged into one session row. Idle periods
(no input for IDLE_THRESHOLD seconds) end the current session and are not logged,
unless the browser is in front and the extension says media is playing.

Run:            python desktop/tracker.py -v       (console, prints each change)
Run hidden:     pythonw desktop/tracker.py         (logs to data/tracker.log)
"""

import argparse
import logging
import logging.handlers
import sys
import threading
import time

import ai_categorize
import ai_judge
import backup
import categorize
import db
import keys
import mailer
import nudge
import phone
import server
import subjects
import weekly
import win32

INTERVAL = 5            # seconds between samples
IDLE_THRESHOLD = 120    # seconds without input before you count as AFK
MAX_GAP = INTERVAL * 3  # a bigger gap between samples (sleep, hang) breaks the session
PAGE_TEXT_DAYS = 60     # full page text older than this is deleted (titles/time kept)
PURGE_EVERY = 3600      # seconds between retention clean-ups
AI_EVERY = 120          # seconds between AI categoriser runs (Voyage)
AUTO_WEEKLY = True      # after each Monday, summarise the previous Mon-Sun via Groq (if key set)

BROWSERS = {"brave.exe", "chrome.exe", "msedge.exe"}

# Titles that are pure noise (taskbar, alt-tab overlay, lock screen etc.)
IGNORED = {("explorer.exe", ""), ("LockApp.exe", "")}

log = logging.getLogger("tracker")


class Tracker:
    def __init__(self, conn, state: server.SharedState):
        self.conn = conn
        self.state = state
        self.current = None  # dict(id, app, title, start, end)

    def go_idle(self, now, idle):
        if self.current is None:
            return
        # Trim the session back to the moment of last input, not "now".
        end = max(now - idle, self.current["start"])
        db.extend_session(self.conn, self.current["id"], end)
        log.info("idle (%.0fs), session closed", idle)
        self.current = None

    def tick(self):
        now = time.time()
        self.state.last_tick = now  # health check: the sampling loop is alive
        idle = win32.idle_seconds()
        win = win32.active_window()

        watching = win is not None and win[0] in BROWSERS and self.state.media_playing()
        if idle >= IDLE_THRESHOLD and not watching:
            self.go_idle(now, idle)
            return

        if win is None or win in IGNORED:
            return
        app, title = win
        cur = self.current
        continuous = cur is not None and now - cur["end"] <= MAX_GAP

        if continuous and cur["app"] == app and cur["title"] == title:
            db.extend_session(self.conn, cur["id"], now)
            cur["end"] = now
            return

        # New session. If we were continuously active, start where the last one
        # ended so there's no gap between them.
        start = cur["end"] if continuous else now
        sid = db.start_session(self.conn, start, now, app, title)
        self.current = {"id": sid, "app": app, "title": title, "start": start, "end": now}
        log.debug("%-22s %s", app, title[:90])

    def purge(self):
        n = db.purge_page_text(self.conn, PAGE_TEXT_DAYS)
        if n:
            log.info("retention: removed page text from %d pages older than %d days", n, PAGE_TEXT_DAYS)

    def run(self):
        last_purge = 0.0
        while True:
            try:
                if time.time() - last_purge >= PURGE_EVERY:
                    last_purge = time.time()
                    self.purge()
                self.tick()
            except Exception:  # never let one bad sample kill the tracker
                log.exception("tick failed")
            time.sleep(INTERVAL)


def background_worker() -> None:
    """Background thread: daily backup, AI categories for new pages, weekly summary.
    Slow work lives here so the 5-second desktop sampling is never delayed."""
    conn = db.connect()
    last_weekly_check = last_backup_check = 0.0
    while True:
        if time.time() - last_backup_check >= 3600:
            last_backup_check = time.time()
            try:
                backup.backup_if_due(conn)
            except Exception:
                log.exception("backup failed")

        try:
            result = ai_categorize.run_batch(conn, categorize.Rules())
            ai_categorize.status.update(last_run=time.time(), last_error=None)
            if result.get("embedded") or result.get("reclassified"):
                log.info("ai categoriser: %s", result)
        except ai_categorize.AIError as exc:
            ai_categorize.status["last_error"] = str(exc)
            log.warning("ai categoriser: %s", exc)
        except Exception:
            log.exception("ai categoriser failed")

        try:  # after Voyage, so Groq only sees what it left undecided
            result = ai_judge.run_batch(conn, categorize.Rules())
            ai_judge.status.update(last_run=time.time(), last_error=None)
            if result.get("requests"):
                log.info("ai judge: %s", result)
        except ai_judge.JudgeError as exc:
            ai_judge.status["last_error"] = str(exc)
            log.warning("ai judge: %s (pausing %d min)", exc, ai_judge.BACKOFF // 60)
        except Exception:
            log.exception("ai judge failed")

        try:  # study pages your subject keywords miss
            result = subjects.run_batch(conn, categorize.Rules())
            if result.get("pages"):
                log.info("subjects: %s", result)
        except ai_judge.JudgeError as exc:
            log.warning("subjects: %s", exc)
        except Exception:
            log.exception("subjects failed")

        if AUTO_WEEKLY and time.time() - last_weekly_check >= 3600:
            last_weekly_check = time.time()
            try:
                due = weekly.due_week(conn)
                if due and keys.load_key("groq"):
                    weekly.generate(conn, *due)
            except weekly.SummaryError as exc:
                log.info("weekly summary not generated: %s", exc)
            except Exception:
                log.exception("weekly summary failed")

        try:
            mailer.maybe_send(conn)  # daily recap at the chosen time; weekly once its summary exists
        except Exception:
            log.exception("email check failed")
        time.sleep(AI_EVERY)


def setup_logging(verbose: bool) -> None:
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(
        db.DATA_DIR / "tracker.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    if verbose:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        log.addHandler(ch)


def main():
    parser = argparse.ArgumentParser(description="Good ol Tracker - desktop logger")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each window change")
    args = parser.parse_args()

    if not win32.acquire_single_instance("Local\\GoodOlTracker.Desktop"):
        if args.verbose:
            print("Tracker is already running; exiting.")
        sys.exit(0)

    setup_logging(args.verbose)
    state = server.SharedState()
    try:
        server.start(server.load_token(), state)
        log.info("extension server listening on 127.0.0.1:%d", server.PORT)
    except OSError as exc:
        log.error("could not start extension server: %r (desktop logging continues)", exc)

    conn = db.connect()
    log.info("logging to %s", db.DB_PATH)
    threading.Thread(target=background_worker, name="background", daemon=True).start()
    threading.Thread(target=nudge.worker, name="nudge", daemon=True).start()
    phone.start()
    try:
        Tracker(conn, state).run()
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
