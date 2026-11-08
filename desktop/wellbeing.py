"""Phone app limits, bedtime mode, scheduled focus and daily goals.

Settings live in data/wellbeing.json (edited on the dashboard). The phone gets them with
every sync and enforces limits/bedtime itself, so they work away from the PC too.
Scheduled focus is started by the PC (tracker.py loop) and, if the PC isn't reachable,
by the phone on its own.
"""

import json
import re
import threading
import time
from datetime import date, datetime, timedelta

import categorize
import db
import focus

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_PKG = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
GOAL_KINDS = {
    "productive_min": "Productive time at least",
    "phone_max": "Phone time at most",
    "category_max": "Time in a category at most",
}

DEFAULTS = {
    "limits": {"com.facebook.katana": 60, "com.instagram.android": 30},  # package -> minutes per day
    "bedtime": {"enabled": True, "start": "23:30", "end": "06:00", "wind_down": 30},
    "schedules": [],  # [{days: [0-6, Mon=0], start, end, label}]
    "goals": [
        {"kind": "productive_min", "minutes": 180},
        {"kind": "phone_max", "minutes": 120},
    ],
}

_lock = threading.Lock()


class WellbeingError(ValueError):
    pass


def _path():
    return db.DATA_DIR / "wellbeing.json"


def settings() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = json.loads(json.dumps(DEFAULTS))
    for k in DEFAULTS:
        if k in data:
            out[k] = data[k]
    return out


def _minutes(v, lo=1, hi=24 * 60, what="minutes"):
    try:
        m = int(v)
    except (TypeError, ValueError):
        raise WellbeingError(f"{what} must be a number") from None
    if not lo <= m <= hi:
        raise WellbeingError(f"{what} must be {lo}-{hi}")
    return m


def _time(v, what):
    if not isinstance(v, str) or not _TIME.match(v):
        raise WellbeingError(f"{what} must be a time like 23:30")
    return v


def save(body: dict, rules=None) -> dict:
    rules = rules or categorize.Rules()
    cur = settings()
    if "limits" in body:
        lim = body["limits"]
        if not isinstance(lim, dict) or len(lim) > 50:
            raise WellbeingError("limits must be {package: minutes}")
        out = {}
        for pkg, m in lim.items():
            pkg = str(pkg).strip().lower()
            if not _PKG.match(pkg):
                raise WellbeingError(f"not an Android package name: {pkg!r}")
            out[pkg] = _minutes(m, 1, 12 * 60, "a daily limit")
        cur["limits"] = out
    if "bedtime" in body:
        b = body["bedtime"]
        if not isinstance(b, dict):
            raise WellbeingError("bedtime must be an object")
        cur["bedtime"] = {"enabled": bool(b.get("enabled")), "start": _time(b.get("start"), "bedtime start"),
                          "end": _time(b.get("end"), "bedtime end"),
                          "wind_down": _minutes(b.get("wind_down", 30), 0, 120, "wind-down")}
        if cur["bedtime"]["start"] == cur["bedtime"]["end"]:
            raise WellbeingError("bedtime start and end can't be the same")
    if "schedules" in body:
        s = body["schedules"]
        if not isinstance(s, list) or len(s) > 20:
            raise WellbeingError("schedules must be a list (up to 20)")
        out = []
        for i, sc in enumerate(s):
            days = sorted({int(d) for d in sc.get("days", []) if str(d).isdigit() and 0 <= int(d) <= 6})
            if not days:
                raise WellbeingError(f"schedule {i + 1}: pick at least one day")
            start, end = _time(sc.get("start"), "schedule start"), _time(sc.get("end"), "schedule end")
            length = _window_minutes(start, end)
            if not focus.MIN_MINUTES <= length <= focus.MAX_MINUTES:
                raise WellbeingError(f"schedule {i + 1}: a focus session is {focus.MIN_MINUTES}-{focus.MAX_MINUTES} min")
            out.append({"days": days, "start": start, "end": end, "label": str(sc.get("label") or "")[:60]})
        cur["schedules"] = out
    if "goals" in body:
        g = body["goals"]
        if not isinstance(g, list) or len(g) > 10:
            raise WellbeingError("goals must be a list (up to 10)")
        out = []
        for goal in g:
            kind = goal.get("kind")
            if kind not in GOAL_KINDS:
                raise WellbeingError(f"unknown goal: {kind!r}")
            item = {"kind": kind, "minutes": _minutes(goal.get("minutes"), 5, 24 * 60, "goal minutes")}
            if kind == "category_max":
                if goal.get("category") not in rules.categories:
                    raise WellbeingError("pick a category for that goal")
                item["category"] = goal["category"]
            out.append(item)
        cur["goals"] = out
    with _lock:
        db.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


# ------------------------------------------------------------------ time windows

def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _window_minutes(start, end) -> int:
    return (_mins(end) - _mins(start)) % (24 * 60)


def window_now(start: str, end: str, now: datetime):
    """If `now` is inside the daily window start-end (which may cross midnight), return the
    (start datetime, end datetime) of that occurrence, else None."""
    s, e = _mins(start), _mins(end)
    t = now.hour * 60 + now.minute
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s < e:
        if s <= t < e:
            return day0 + timedelta(minutes=s), day0 + timedelta(minutes=e)
        return None
    if t >= s:
        return day0 + timedelta(minutes=s), day0 + timedelta(days=1, minutes=e)
    if t < e:
        return day0 - timedelta(days=1) + timedelta(minutes=s), day0 + timedelta(minutes=e)
    return None


# ------------------------------------------------------------------ scheduled focus

_started = set()  # occurrences already started (so ending one early doesn't restart it)


def scheduled_focus(conn, now=None):
    """Start the focus session a schedule calls for right now. Returns the session or None."""
    now = now or time.time()
    dt = datetime.fromtimestamp(now)
    for i, sc in enumerate(settings()["schedules"]):
        win = window_now(sc["start"], sc["end"], dt)
        if not win or win[0].weekday() not in sc["days"]:
            continue
        key = (i, win[0].isoformat())
        if key in _started:
            continue
        _started.add(key)
        if focus.current(conn, now):
            return None  # one already running: leave it
        minutes = max(focus.MIN_MINUTES, min(focus.MAX_MINUTES, round((win[1].timestamp() - now) / 60)))
        return focus.start(conn, minutes, sc["label"] or "Scheduled focus", now=now)
    return None


# ------------------------------------------------------------------ goals

def _day_totals(items, rules, d0, d1):
    out = {"productive": 0.0, "phone": 0.0, "by_category": {}, "sources": set()}
    for it in items:
        o = min(it.end, d1) - max(it.start, d0)
        if o <= 0:
            continue
        out["sources"].add("phone" if it.source == "phone" else "pc")
        if it.category in rules.productive:
            out["productive"] += o
        if it.source == "phone":
            out["phone"] += o
        out["by_category"][it.category] = out["by_category"].get(it.category, 0.0) + o
    return out


def _goal_value(goal, totals):
    if goal["kind"] == "productive_min":
        return totals["productive"]
    if goal["kind"] == "phone_max":
        return totals["phone"]
    return totals["by_category"].get(goal.get("category"), 0.0)


def _label(goal):
    m = goal["minutes"]
    amount = f"{m // 60}h {m % 60}m".replace(" 0m", "") if m >= 60 else f"{m}m"
    if goal["kind"] == "productive_min":
        return f"Productive {amount}+"
    if goal["kind"] == "phone_max":
        return f"Phone under {amount}"
    return f"{goal.get('category')} under {amount}"


def goals(conn, today: date | None = None, days: int = 30, rules=None, now=None) -> list:
    """Each goal with today's progress, whether it's met, the current streak and the last 14 days."""
    rules = rules or categorize.Rules()
    now = now or time.time()
    today = today or date.fromtimestamp(now)
    first = today - timedelta(days=days - 1)
    t0 = datetime.combine(first, datetime.min.time()).timestamp()
    t1 = datetime.combine(today + timedelta(days=1), datetime.min.time()).timestamp()
    items = categorize.categorise(conn, t0, min(t1, now), rules)
    # A day only counts for a goal once the device it's about was being tracked.
    first_seen = {}
    for it in items:
        src = "phone" if it.source == "phone" else "pc"
        first_seen[src] = min(first_seen.get(src, now), it.start)
    per_day = []
    for i in range(days):
        d = first + timedelta(days=i)
        d0 = datetime.combine(d, datetime.min.time()).timestamp()
        tracked = {src for src, ts in first_seen.items() if ts < d0 + 86400}
        per_day.append((d, tracked, _day_totals(items, rules, d0, d0 + 86400)))

    out = []
    for goal in settings()["goals"]:
        target = goal["minutes"] * 60
        at_least = goal["kind"] == "productive_min"
        needs = {"productive_min": "pc", "phone_max": "phone"}.get(goal["kind"])
        history = []
        for d, tracked, totals in per_day:
            v = _goal_value(goal, totals)
            done = d < today  # a "max" goal is only won when the day is over
            met = (v >= target) if at_least else (v <= target and done)
            known = (needs in tracked) if needs else bool(tracked)
            history.append({"date": d.isoformat(), "value": round(v), "met": met if known else None,
                            "final": done})
        streak = 0
        for h in reversed(history[:-1]):  # days before today
            if not h["met"]:
                break
            streak += 1
        today_h = history[-1]
        if at_least and today_h["met"]:
            streak += 1
        value = today_h["value"]
        out.append({**goal, "label": _label(goal), "target": target, "today": value,
                    "progress": round(min(1.0, value / target), 3) if target else 0,
                    "met_today": bool(today_h["met"]) if at_least else value <= target,
                    "over": (not at_least) and value > target,
                    "streak": streak, "history": history[-14:]})
    return out


# ------------------------------------------------------------------ for the phone

def for_phone() -> dict:
    s = settings()
    return {"limits": s["limits"], "bedtime": s["bedtime"], "schedules": s["schedules"]}
