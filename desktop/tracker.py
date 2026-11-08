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
import time

import db
import server
import win32

INTERVAL = 5            # seconds between samples
IDLE_THRESHOLD = 120    # seconds without input before you count as AFK
MAX_GAP = INTERVAL * 3  # a bigger gap between samples (sleep, hang) breaks the session
PAGE_TEXT_DAYS = 60     # full page text older than this is deleted (titles/time kept)
PURGE_EVERY = 3600      # seconds between retention clean-ups

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
    try:
        Tracker(conn, state).run()
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
