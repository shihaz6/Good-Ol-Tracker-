"""Distraction alerts: a nudge after a long unbroken stretch of distracting time.

Every CHECK_EVERY seconds (tracker.py) the last few hours are categorised and the streak
still going right now is measured: time in the alert categories (Entertainment and Social
by default), where short detours (< OTHER_GAP, e.g. a quick WhatsApp reply) and short
pauses (< IDLE_GAP) don't break it. Once it reaches the limit you get one alert, and
another every `repeat` minutes while it carries on.

Delivery: the browser extension picks the alert up in its next /ingest reply (~30 s) and
shows a notification with "Start 25-min focus" and "Snooze". If the extension doesn't
collect it (Brave closed: a game, Spotify, ...) a plain Windows notification is shown.

No alerts while a focus session runs, while snoozed, or when switched off.
"""

import json
import logging
import os
import subprocess
import threading
import time
from collections import Counter

import categorize
import db
import focus

log = logging.getLogger("tracker")

CHECK_EVERY = 30
LOOKBACK = 4 * 3600     # how far back a streak can reach
OTHER_GAP = 90          # a non-distracting stretch shorter than this doesn't end the streak
IDLE_GAP = 180          # nor does a pause (nothing logged) shorter than this
LIVE = 90               # the streak must still be going: last activity at most this long ago
HANDOFF = 75            # seconds the extension has to collect an alert before Windows shows it
FOCUS_MINUTES = 25      # the alert's "Start focus" button

DEFAULTS = {"enabled": True, "minutes": 20, "repeat": 20, "snooze": 30,
            "categories": ["Entertainment", "Social"]}


class NudgeError(ValueError):
    pass


# --------------------------------------------------------------------- settings

def _path():
    return db.DATA_DIR / "nudge.json"


def settings() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save_settings(body: dict, rules=None) -> dict:
    cur = settings()
    if "enabled" in body:
        cur["enabled"] = bool(body["enabled"])
    for key, lo, hi in (("minutes", 5, 240), ("repeat", 5, 240), ("snooze", 5, 240)):
        if key in body:
            try:
                v = int(body[key])
            except (TypeError, ValueError):
                raise NudgeError(f"{key} must be a number") from None
            if not lo <= v <= hi:
                raise NudgeError(f"{key} must be {lo}-{hi} minutes")
            cur[key] = v
    if "categories" in body:
        cats = body["categories"]
        known = (rules or categorize.Rules()).categories
        if not isinstance(cats, list) or not cats or any(c not in known for c in cats):
            raise NudgeError("pick at least one category from your category list")
        cur["categories"] = [c for c in known if c in cats]
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


# --------------------------------------------------------------------- streak

def streak(items, cats, now):
    """The distracting streak still going at `now`, or None.
    -> {start, seconds (distracting time only), top: [(name, seconds)]}"""
    cats = set(cats)
    items = sorted((it for it in items if it.end > it.start), key=lambda it: it.end)
    if not items or now - items[-1].end > LIVE or items[-1].category not in cats:
        return None
    seconds, start, cursor, other = 0.0, items[-1].start, items[-1].end, 0.0
    names = Counter()
    for it in reversed(items):
        if cursor - it.end > IDLE_GAP:
            break
        if it.category in cats:
            seconds += it.seconds
            names[it.app] += it.seconds
            start, other = it.start, 0.0
        else:
            other += it.seconds
            if other >= OTHER_GAP:
                break
        cursor = min(cursor, it.start)
    return {"start": start, "seconds": seconds, "top": names.most_common(2)}


def _pretty(name: str) -> str:
    n = name.removesuffix(".exe").removesuffix(".EXE")
    return n.removeprefix("www.")


def message_for(s, cats) -> dict:
    mins = round(s["seconds"] / 60)
    names = " and ".join(_pretty(n) for n, _ in s["top"]) or "distractions"
    return {"title": f"{mins} min on {names}",
            "message": f"That's {mins} minutes of {' / '.join(cats)} in a row. Start a {FOCUS_MINUTES}-minute focus session?",
            "minutes": mins}


# --------------------------------------------------------------------- state + loop

_lock = threading.Lock()
state = {"pending": None, "last_alert": 0.0, "snooze_until": 0.0, "next_id": 1, "history": []}


def check(conn, now=None, rules=None):
    """Decide whether to alert now. Returns the new alert (also queued as pending) or None."""
    now = now or time.time()
    cfg = settings()
    if not cfg["enabled"] or now < state["snooze_until"] or focus.current(conn, now):
        return None
    items = categorize.categorise(conn, now - LOOKBACK, now, rules or categorize.Rules())
    s = streak([it for it in items if it.source != "manual"], cfg["categories"], now)
    if not s or s["seconds"] < cfg["minutes"] * 60:
        return None
    same_streak = state["last_alert"] and s["start"] <= state["last_alert"]
    if same_streak and now - state["last_alert"] < cfg["repeat"] * 60:
        return None
    with _lock:
        alert = {"id": state["next_id"], "ts": now, "focus_minutes": FOCUS_MINUTES,
                 "snooze_minutes": cfg["snooze"], **message_for(s, cfg["categories"])}
        state["next_id"] += 1
        state["last_alert"] = now
        state["pending"] = alert
        state["history"] = (state["history"] + [{"ts": now, "title": alert["title"]}])[-20:]
    log.info("distraction alert: %s", alert["title"])
    return alert


def take():
    """The extension collects the pending alert (once)."""
    with _lock:
        alert, state["pending"] = state["pending"], None
    return alert


def snooze(minutes=None, now=None):
    now = now or time.time()
    minutes = int(minutes or settings()["snooze"])
    with _lock:
        state["snooze_until"] = now + max(1, min(240, minutes)) * 60
        state["pending"] = None
    return state["snooze_until"]


def status() -> dict:
    return {"settings": settings(), "snoozed_until": state["snooze_until"] or None,
            "recent": list(reversed(state["history"]))[:5]}


def windows_toast(title: str, message: str) -> None:
    """Plain Windows notification (no buttons), for when the browser isn't open.
    Text goes in through environment variables, never into the script itself."""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        "$x.Item(0).AppendChild($t.CreateTextNode($env:GOT_TITLE)) > $null;"
        "$x.Item(1).AppendChild($t.CreateTextNode($env:GOT_MESSAGE)) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        env={**os.environ, "GOT_TITLE": title, "GOT_MESSAGE": message},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def handoff(now=None, toast=windows_toast):
    """An alert the extension didn't collect in time goes to Windows instead."""
    now = now or time.time()
    with _lock:
        alert = state["pending"]
        if not alert or now - alert["ts"] < HANDOFF:
            return None
        state["pending"] = None
    toast(alert["title"], alert["message"].replace("Start a", "Open the extension to start a"))
    return alert


def worker() -> None:
    """Background thread (tracker.py)."""
    conn = db.connect()
    while True:
        try:
            check(conn)
            handoff()
        except Exception:
            log.exception("distraction check failed")
        time.sleep(CHECK_EVERY)
