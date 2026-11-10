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
    _pc_cache["ts"] = 0.0  # the extension sees the change on its next check-in
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


# ------------------------------------------------------------------ the same rules on the PC

# A phone app's limit also covers its website on the PC: one shared daily budget.
APP_SITES = {
    "com.facebook.katana": ["facebook.com"], "com.facebook.lite": ["facebook.com"],
    "com.instagram.android": ["instagram.com"], "com.google.android.youtube": ["youtube.com"],
    "com.zhiliaoapp.musically": ["tiktok.com"], "com.ss.android.ugc.trill": ["tiktok.com"],
    "com.twitter.android": ["x.com", "twitter.com"], "com.reddit.frontpage": ["reddit.com"],
    "com.snapchat.android": ["snapchat.com"], "com.netflix.mediaclient": ["netflix.com"],
    "com.facebook.orca": ["messenger.com"], "com.linkedin.android": ["linkedin.com"],
    "com.pinterest": ["pinterest.com"], "tv.twitch.android.app": ["twitch.tv"],
    "com.instagram.barcelona": ["threads.net"], "com.spotify.music": ["open.spotify.com"],
}
GRACE_SECONDS = 5 * 60
_pc_cache = {"ts": 0.0, "value": None}


def _grace_path():
    return db.DATA_DIR / "wellbeing_grace.json"


def _graces() -> dict:
    try:
        return json.loads(_grace_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def grant_grace(key: str, now=None) -> float:
    """'5 more minutes' on the PC, once per bedtime night / limit per day."""
    now = now or time.time()
    if not isinstance(key, str) or not re.match(r"^(bed|limit:[a-z0-9_.]+)\|\d{4}-\d{2}-\d{2}$", key):
        raise WellbeingError("bad grace key")
    with _lock:
        g = {k: v for k, v in _graces().items() if v > now - 3 * 86400}  # forget old ones
        if key in g:
            raise WellbeingError("you've already had your 5 more minutes for this")
        g[key] = now + GRACE_SECONDS
        db.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _grace_path().write_text(json.dumps(g), encoding="utf-8")
    _pc_cache["ts"] = 0.0
    return g[key]


def _on(domain: str, domains) -> bool:
    d = (domain or "").lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in domains)


def _limit_allow(domains, rules) -> list:
    return focus.allowed(domains, rules) if domains else []


def limit_usage(conn, now=None, rules=None) -> list:
    """Each daily limit with today's use on the phone (the app) and the PC (its website).
    Study time doesn't count: on the PC only non-productive time on the site is added, so a
    YouTube lecture doesn't eat your YouTube budget."""
    rules = rules or categorize.Rules()
    now = now or time.time()
    lim = settings()["limits"]
    if not lim:
        return []
    d0 = datetime.combine(date.fromtimestamp(now), datetime.min.time()).timestamp()
    items = categorize.categorise(conn, d0, now, rules)
    out = []
    for pkg, minutes in lim.items():
        domains = APP_SITES.get(pkg, [])
        allow = _limit_allow(domains, rules)
        phone_s = sum(it.seconds for it in items if it.source == "phone" and it.title.lower() == pkg)
        pc_s = sum(it.seconds for it in items if it.source == "browser" and _on(it.app, domains)
                   and not _on(it.app, allow) and it.category not in rules.productive)
        label = next((it.app for it in items if it.source == "phone" and it.title.lower() == pkg), None)
        out.append({"pkg": pkg, "label": label or (domains[0] if domains else pkg), "minutes": minutes,
                    "domains": domains, "allow": allow, "phone": round(phone_s), "pc": round(pc_s), "used": round(phone_s + pc_s),
                    "key": f"limit:{pkg}|{date.fromtimestamp(now).isoformat()}"})
    return out


def pc_state(conn, now=None, rules=None) -> dict:
    """What the browser extension enforces right now (sent with every /ingest reply).
    Each block carries `until`, so a stale copy runs out on its own if the tracker stops."""
    now = now or time.time()
    if now - _pc_cache["ts"] < 20 and _pc_cache["value"] is not None:
        return _pc_cache["value"]
    s = settings()
    graces = _graces()
    graced = lambda key: graces.get(key, 0) > now  # noqa: E731
    midnight = datetime.combine(date.fromtimestamp(now) + timedelta(days=1), datetime.min.time()).timestamp()
    limits, warn = [], []
    for u in limit_usage(conn, now, rules):
        if not u["domains"]:
            continue
        info = {"label": u["label"], "domains": u["domains"], "allow": u["allow"], "used": u["used"], "minutes": u["minutes"],
                "key": u["key"], "until": midnight, "grace_used": u["key"] in graces}
        if u["used"] >= u["minutes"] * 60:
            if not graced(u["key"]):
                limits.append(info)
        elif u["used"] >= u["minutes"] * 60 * 0.8:
            warn.append(info)
    bed = s["bedtime"]
    bedtime = wind = None
    if bed["enabled"]:
        dt = datetime.fromtimestamp(now)
        win = window_now(bed["start"], bed["end"], dt)
        if win:
            key = f"bed|{win[0].date().isoformat()}"
            if not graced(key):
                bedtime = {"until": win[1].timestamp(), "end": bed["end"], "key": key, "grace_used": key in graces,
                           "domains": (blk := focus.settings()["block"]), "allow": focus.allowed(blk, rules)}
        elif bed["wind_down"]:
            start_today = dt.replace(hour=int(bed["start"][:2]), minute=int(bed["start"][3:]), second=0, microsecond=0)
            start_at = start_today if start_today > dt else start_today + timedelta(days=1)
            if (start_at - dt).total_seconds() <= bed["wind_down"] * 60:
                wind = {"at": start_at.timestamp(), "start": bed["start"], "key": f"wind|{start_at.date().isoformat()}"}
    value = {"limits": limits, "warn": warn, "bedtime": bedtime, "wind_down": wind}
    _pc_cache.update(ts=now, value=value)
    return value


# ------------------------------------------------------------------ for the phone

def for_phone() -> dict:
    s = settings()
    return {"limits": s["limits"], "bedtime": s["bedtime"], "schedules": s["schedules"]}


def pc_used_for_phone(conn, now=None) -> dict:
    """{package: seconds used on its website on the PC today}, so the phone's limit counts both."""
    now = now or time.time()
    return {"date": date.fromtimestamp(now).isoformat(),
            "used": {u["pkg"]: u["pc"] for u in limit_usage(conn, now) if u["pc"]}}
