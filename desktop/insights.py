"""Insights that need both devices: sleep (from when the phone and PC go quiet at night)
and phone interruptions while you're studying on the PC.

Sleep is an ESTIMATE: the longest quiet stretch of a night (no phone use, no unlock, no PC
use) between 18:00 and 15:00 the next day, if it's at least MIN_SLEEP long. Lying awake
without the phone counts as sleep; using the phone in bed doesn't.
"""

import time
from collections import Counter
from datetime import date, datetime, timedelta

import categorize

NIGHT_START_H = 18       # a "night" runs from 18:00 ...
NIGHT_END_H = 15         # ... to 15:00 the next day
MIN_SLEEP = 3 * 3600     # a shorter quiet stretch isn't treated as sleep
PICKUP_GAP = 30          # phone use with gaps up to this is one pickup
AROUND = 5 * 60          # a pickup counts as "during study" with study within 5 min before AND after
GOOD_SLEEP = 7 * 3600
MIN_PC_DAY = 30 * 60     # a day needs this much PC time to count in "sleep vs next-day productivity"
MAX_PICKUP = 15 * 60    # longer phone use between study stretches is a "break", counted separately


def _midnight(d: date) -> float:
    return datetime.combine(d, datetime.min.time()).timestamp()


def _unlocks(conn, start, end):
    return [ts for (ts,) in conn.execute("SELECT ts FROM phone_unlocks WHERE ts >= ? AND ts < ? ORDER BY ts",
                                         (start, end))]


# ------------------------------------------------------------------ sleep

def _longest_gap(spans, lo, hi):
    """Longest gap between activity spans inside [lo, hi], with activity on both sides."""
    best = None
    prev_end = None
    for s, e in sorted(spans):
        if e < lo or s > hi:
            continue
        if prev_end is not None and s - prev_end > (best[1] - best[0] if best else 0):
            best = (prev_end, s)
        prev_end = e if prev_end is None else max(prev_end, e)
    return best


def sleep(conn, end_day: date, nights: int = 14, rules=None, now=None) -> dict:
    """Nights ending on the mornings of the `nights` days up to end_day."""
    rules = rules or categorize.Rules()
    now = now or time.time()
    first = end_day - timedelta(days=nights)
    lo = _midnight(first) + NIGHT_START_H * 3600
    hi = min(_midnight(end_day) + NIGHT_END_H * 3600, now)
    items = [it for it in categorize.categorise(conn, lo - 86400, hi + 86400, rules) if it.source != "manual"]
    spans = [(it.start, it.end) for it in items] + [(t, t) for t in _unlocks(conn, lo, hi)]
    has_phone = conn.execute("SELECT 1 FROM sessions WHERE source = 'phone' LIMIT 1").fetchone() is not None

    out = []
    for i in range(nights):
        evening = first + timedelta(days=i)
        n_lo = _midnight(evening) + NIGHT_START_H * 3600
        n_hi = _midnight(evening + timedelta(days=1)) + NIGHT_END_H * 3600
        if n_hi > now:  # this morning isn't over yet: only count it once you're clearly up
            n_hi = now
        gap = _longest_gap(spans, n_lo, n_hi)
        night = {"date": evening.isoformat(), "bedtime": None, "wake": None, "seconds": None}
        if gap and gap[1] - gap[0] >= MIN_SLEEP:
            night.update(bedtime=gap[0], wake=gap[1], seconds=round(gap[1] - gap[0]))
        # How the next day went (that day's productive time), for "sleep vs productivity".
        nd = evening + timedelta(days=1)
        d0, d1 = _midnight(nd), _midnight(nd + timedelta(days=1))
        night["next_day_productive"] = round(sum(
            max(0.0, min(it.end, d1) - max(it.start, d0)) for it in items if it.category in rules.productive))
        # Only days the PC was actually tracked count for "sleep vs productivity" (the phone
        # brings a week of history; the PC may not have been tracked back then).
        pc_secs = sum(max(0.0, min(it.end, d1) - max(it.start, d0)) for it in items if it.source in ("desktop", "browser"))
        night["next_day_complete"] = d1 <= now and pc_secs >= MIN_PC_DAY
        out.append(night)

    known = [n for n in out if n["seconds"]]

    def avg_clock(key):
        # minutes after 18:00, so 23:30 and 00:30 average to midnight, not noon
        vals = [((datetime.fromtimestamp(n[key]).hour - NIGHT_START_H) % 24) * 60 + datetime.fromtimestamp(n[key]).minute
                for n in known]
        if not vals:
            return None
        m = round(sum(vals) / len(vals)) + NIGHT_START_H * 60
        return f"{(m // 60) % 24:02d}:{m % 60:02d}"

    def avg_prod(group):
        days = [n["next_day_productive"] for n in group if n["next_day_complete"]]
        return round(sum(days) / len(days)) if days else None

    return {
        "nights": out,
        "has_phone": has_phone,
        "avg_seconds": round(sum(n["seconds"] for n in known) / len(known)) if known else None,
        "avg_bedtime": avg_clock("bedtime"),
        "avg_wake": avg_clock("wake"),
        "productive_after_good": avg_prod([n for n in known if n["seconds"] >= GOOD_SLEEP]),
        "productive_after_short": avg_prod([n for n in known if n["seconds"] < GOOD_SLEEP]),
        "good_sleep_hours": GOOD_SLEEP / 3600,
    }


# ------------------------------------------------------------------ interruptions

def _pickups(phone_items):
    picks = []
    for it in sorted(phone_items, key=lambda it: it.start):
        if picks and it.start - picks[-1]["end"] <= PICKUP_GAP:
            p = picks[-1]
            p["end"] = max(p["end"], it.end)
        else:
            p = {"start": it.start, "end": it.end, "apps": Counter()}
            picks.append(p)
        p["apps"][it.app] += it.seconds
    return picks


def interruptions(conn, start: float, end: float, rules=None) -> dict:
    """Phone pickups in the middle of PC study/work: productive PC time within AROUND before
    AND after the pickup (you were studying, picked the phone up, and went back)."""
    rules = rules or categorize.Rules()
    items = categorize.categorise(conn, start - AROUND, end + AROUND, rules)
    phone = [it for it in items if it.source == "phone" and start <= it.start < end]
    study = sorted((it.start, it.end) for it in items
                   if it.source in ("desktop", "browser") and it.category in rules.productive)
    study_secs = sum(max(0.0, min(e, end) - max(s, start)) for s, e in study)
    focus_rows = conn.execute("SELECT start_ts, COALESCE(end_ts, planned_end_ts) FROM focus_sessions"
                              " WHERE start_ts < ? AND COALESCE(end_ts, planned_end_ts) > ?", (end, start)).fetchall()

    def near(t0, t1):
        return any(s < t1 and e > t0 for s, e in study)

    hits, breaks, apps = [], [], Counter()
    for p in _pickups(phone):
        before = near(p["start"] - AROUND, p["start"])
        after = near(p["end"], p["end"] + AROUND)
        in_focus = any(s <= p["start"] < e for s, e in focus_rows)
        if not ((before and after) or in_focus):
            continue
        if p["end"] - p["start"] > MAX_PICKUP:
            breaks.append(p)  # an hour of Facebook between study stretches is a break, not a glance
            continue
        hits.append({**p, "in_focus": in_focus})
        apps.update(p["apps"])
    total = sum(h["end"] - h["start"] for h in hits)
    return {
        "count": len(hits),
        "seconds": round(total),
        "avg_seconds": round(total / len(hits)) if hits else 0,
        "in_focus": sum(1 for h in hits if h["in_focus"]),
        "study_seconds": round(study_secs),
        "per_study_hour": round(len(hits) / (study_secs / 3600), 1) if study_secs >= 1800 else None,
        "top_apps": [{"app": a, "seconds": round(s)} for a, s in apps.most_common(5)],
        "longest": max((round(h["end"] - h["start"]) for h in hits), default=0),
        "breaks": len(breaks),
        "break_seconds": round(sum(b["end"] - b["start"] for b in breaks)),
    }


def summary(conn, start_day: date, end_day: date, rules=None) -> dict:
    rules = rules or categorize.Rules()
    s, e = _midnight(start_day), _midnight(end_day + timedelta(days=1))
    return {"sleep": sleep(conn, end_day, 14, rules), "interruptions": interruptions(conn, s, e, rules)}
