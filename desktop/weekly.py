"""Weekly AI summary via Groq: a coach's read of your week and a plan for the next one.

What is sent (see build_payload): totals and patterns only - hours per category and
day, productive hours by time of day, focus/switch stats, top app/site NAMES, top
topic WORDS, phone time / unlocks / top phone app names, pickups while studying, sleep
times, goals met, daily limits exceeded, your subject names with hours and exam
countdowns, focus-mode sessions, and the same totals for the previous period. Never
sent: page text, page titles, URLs, search queries, window titles.

    python desktop/weekly.py --preview          # print exactly what would be sent
    python desktop/weekly.py                    # last 7 days -> Groq -> saved + printed
    python desktop/weekly.py --start 2026-11-02 --end 2026-11-08
"""

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

import analytics
import categorize
import db
import focus
import insights
import keys
import subjects
import wellbeing

log = logging.getLogger("tracker")

GROQ_BASE = "https://api.groq.com/openai/v1"
PREFERRED_MODELS = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
                    "llama-3.1-8b-instant"]
SKIP_MODEL_WORDS = ("whisper", "guard", "tts", "playai", "embed", "safeguard")
MIN_ACTIVE_SECONDS = 10 * 60  # don't ask for a summary of almost nothing

SYSTEM_PROMPT = """You are a warm but direct study and focus coach for a university computing \
student. You receive their tracked stats for a period as JSON: PC and phone time, categories, \
focus, sleep, phone pickups while studying, daily goals, daily app limits, subjects with exam \
dates and weekly targets. Use ONLY these numbers; never invent data, apps, subjects or topics. \
Speak to them directly ("you"). Write durations like "3h 20m". Fields that are null or missing \
weren't tracked: don't mention them. Check "tracking": if the PC was tracked on fewer days than \
the phone, PC-based numbers (productive share, active hours) are partial, so don't present them \
as the whole picture; previous_period is null when it wasn't really tracked: then don't compare.

The only tracker settings that exist (suggest nothing else): daily app limits (phone app + its \
website share one budget), bedtime with a wind-down reminder, scheduled focus (days + times), \
focus mode (25/50/90 min, blocks distracting apps and sites), daily goals (productive at least, \
phone at most, a category at most), subjects with exam dates and weekly targets. Ordinary phone \
features (Do Not Disturb, charging the phone outside the bedroom) are fine to mention too.

Format (Markdown):
## How it went
2-3 sentences: the honest headline, including the biggest win and the biggest leak of time.
## What stood out
3-5 bullets, each with a number: e.g. the nearest exam vs hours studied for it, phone time and \
which apps, pickups while studying, sleep (bedtime, short nights), goals met/missed and streaks, \
peak productive hours. Compare with previous_period where it exists.
## Your plan for next week
3-4 concrete actions, most important first. Prioritise the nearest exam and the subject furthest \
behind its weekly target, then the goal missed most often. Each action says what, when and how \
much (e.g. "Three 50-minute focus sessions on DBMS before Thursday: exam in 9 days, 2h of 6h done"). \
Where it helps, suggest a tracker setting by name: a daily limit, bedtime, scheduled focus, a goal.

Be kind but don't sugar-coat. Keep it under 330 words."""


class SummaryError(Exception):
    pass


def _h(seconds) -> float:
    return round((seconds or 0) / 3600, 2)


def build_payload(conn, start_day: date, end_day: date, rules=None) -> dict:
    rules = rules or categorize.Rules()
    cur = analytics.summary(conn, start_day, end_day, rules)
    days = cur["range"]["days"]
    prev = analytics.summary(conn, start_day - timedelta(days=days), start_day - timedelta(days=1), rules)
    productive = cur["productive"]

    def prod(by_cat):
        return sum(v for k, v in by_cat.items() if k in productive)

    focus = cur["focus"]
    longest = focus["longest"]
    return {
        "period": {"start": cur["range"]["start"], "end": cur["range"]["end"], "days": days},
        "productive_categories": productive,
        "totals": {
            "active_hours": _h(cur["totals"]["active"]),
            "productive_hours": _h(cur["totals"]["productive"]),
            "productive_share_pct": round(100 * cur["totals"]["productive"] / cur["totals"]["active"])
            if cur["totals"]["active"] else 0,
        },
        "hours_by_category": {k: _h(v) for k, v in sorted(cur["totals"]["by_category"].items(), key=lambda kv: -kv[1])},
        "per_day": [
            {"date": p["date"], "weekday": date.fromisoformat(p["date"]).strftime("%A"),
             "active_hours": _h(p["active"]), "productive_hours": _h(prod(p["by_category"]))}
            for p in cur["per_day"]
        ],
        "avg_productive_minutes_by_hour_of_day": {
            f"{h['hour']:02d}:00": round(prod(h["by_category"]) / days / 60)
            for h in cur["by_hour"] if prod(h["by_category"]) / days >= 60
        },
        "focus": {
            "longest_streak_minutes": round(longest["seconds"] / 60) if longest else 0,
            "longest_streak_category": longest["category"] if longest else None,
            "streaks_over_5_minutes": focus["count"],
            "deep_focus_sessions_25min_plus": focus["deep_count"],
            "deep_focus_hours": _h(focus["deep_seconds"]),
        },
        "app_or_site_switches_per_active_hour": cur["switches"]["per_hour"],
        "top_apps_and_sites": [
            {"name": a["name"], "category": a["category"], "hours": _h(a["seconds"])}
            for a in cur["top_apps"][:10]
        ],
        "top_topic_words": [t["term"] for t in cur["topics"][:15]],
        "number_of_searches": cur["search_count"],
        # A barely-tracked previous period (before install) would only make misleading comparisons.
        "previous_period": None if prev["totals"]["active"] < max(5 * 3600, 0.2 * cur["totals"]["active"]) else {
            "active_hours": _h(prev["totals"]["active"]),
            "productive_hours": _h(prev["totals"]["productive"]),
            "hours_by_category": {k: _h(v) for k, v in prev["totals"]["by_category"].items()},
            "phone_hours": _h(prev["phone"]["seconds"]) if prev["phone"]["seconds"] else None,
        },
        **coach_context(conn, start_day, end_day, rules, cur),
    }


def coach_context(conn, start_day: date, end_day: date, rules, cur) -> dict:
    """The newer data for the coach: phone, sleep, pickups, goals, limits, subjects, focus.
    Still totals and names only: no page text, titles, URLs or searches."""
    days = (end_day - start_day).days + 1
    s0, s1 = analytics._midnight(start_day), analytics._midnight(end_day + timedelta(days=1))
    out = {}

    # How many days each device was actually tracked (30+ min of activity).
    pc_days, phone_days = set(), set()
    pc_secs, ph_secs = {}, {}
    for it in categorize.categorise(conn, s0, s1, rules):
        d = date.fromtimestamp(it.start)
        bucket = ph_secs if it.source == "phone" else pc_secs if it.source in ("desktop", "browser") else None
        if bucket is not None:
            bucket[d] = bucket.get(d, 0.0) + it.seconds
    pc_days = {d for d, s in pc_secs.items() if s >= 1800}
    phone_days = {d for d, s in ph_secs.items() if s >= 1800}
    out["tracking"] = {"days_in_period": days, "pc_days_tracked": len(pc_days), "phone_days_tracked": len(phone_days)}

    p = cur["phone"]
    if p["seconds"]:
        phone_apps = [a for a in cur["top_apps"] if a["kind"] == "phone"][:6]
        intr = insights.interruptions(conn, s0, s1, rules)
        out["phone"] = {
            "hours_per_day": _h(p["seconds"] / days), "unlocks_per_day": round(p["unlocks"] / days, 1),
            "top_apps": [{"name": a["label"], "hours": _h(a["seconds"])} for a in phone_apps],
            "pickups_while_studying": intr["count"], "pickups_per_study_hour": intr["per_study_hour"],
            "long_phone_breaks_during_study": intr["breaks"],
        }

    sl = insights.sleep(conn, end_day, min(days, 14), rules)
    known = [n for n in sl["nights"] if n["seconds"]]
    if known:
        out["sleep"] = {"avg_hours": _h(sl["avg_seconds"]), "usual_bedtime": sl["avg_bedtime"],
                        "usual_wake": sl["avg_wake"], "nights_tracked": len(known),
                        "nights_under_7h": sum(1 for n in known if n["seconds"] < 7 * 3600)}

    goals = []
    for g in wellbeing.goals(conn, today=end_day, rules=rules):
        hist = [h for h in g["history"][-days:] if h["met"] is not None and (h["final"] or h["met"])]
        if hist:
            goals.append({"goal": g["label"], "met_days": sum(1 for h in hist if h["met"]), "of_days": len(hist),
                          "current_streak": g["streak"]})
    if goals:
        out["daily_goals"] = goals

    out.update(_limits_context(conn, start_day, end_day, rules))

    subs = subjects.summary(conn, start_day, end_day, rules, today=end_day)
    if subs["subjects"]:
        out["subjects"] = [{"name": x["name"], "hours_this_period": _h(x["seconds"]),
                            "hours_this_week": _h(x["week_seconds"]),
                            "weekly_target_hours": _h(x["weekly_minutes"] * 60) if x["weekly_minutes"] else None,
                            "days_to_exam": x["days_left"] if x["days_left"] is not None and x["days_left"] >= 0 else None}
                           for x in subs["subjects"]]
        out["study_time_not_matched_to_a_subject_hours"] = _h(subs["untagged_seconds"])

    f = focus.history(conn, s0, s1, rules)
    if f["sessions"]:
        out["focus_mode"] = {"sessions": len(f["sessions"]), "finished": f["completed"], "ended_early": f["stopped"],
                             "hours": _h(f["focused_seconds"]), "blocked_attempts": f["blocked"]}
    bed = wellbeing.settings()["bedtime"]
    out["tracker_settings"] = {"bedtime": f"{bed['start']}-{bed['end']}" if bed["enabled"] else "off",
                               "scheduled_focus_slots": len(wellbeing.settings()["schedules"])}
    return out


def _limits_context(conn, start_day, end_day, rules) -> dict:
    """On how many days each daily limit (phone app + its website together) was exceeded."""
    lim = wellbeing.settings()["limits"]
    if not lim:
        return {}
    items = categorize.categorise(conn, analytics._midnight(start_day), analytics._midnight(end_day + timedelta(days=1)), rules)
    rows = []
    for pkg, minutes in lim.items():
        domains = wellbeing.APP_SITES.get(pkg, [])
        per_day, label = {}, None
        for it in items:
            if it.source == "phone" and it.title.lower() == pkg:
                label = label or it.app
            elif not (it.source == "browser" and wellbeing._on(it.app, domains) and it.category not in rules.productive):
                continue
            d = date.fromtimestamp(it.start)
            per_day[d] = per_day.get(d, 0.0) + it.seconds
        if per_day:
            rows.append({"app": label or (domains[0] if domains else pkg), "limit_minutes": minutes,
                         "days_over_limit": sum(1 for v in per_day.values() if v > minutes * 60),
                         "avg_minutes_per_day": round(sum(per_day.values()) / 60 / ((end_day - start_day).days + 1))})
    return {"daily_limits": rows} if rows else {}


# --------------------------------------------------------------------- Groq

def _request(key: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{GROQ_BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": keys.USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code in (401, 403):
            raise SummaryError("Groq rejected the API key (check data/groq_key.txt)") from exc
        if exc.code == 429:
            raise SummaryError("Groq rate limit reached; try again in a minute") from exc
        raise SummaryError(f"Groq HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SummaryError(f"Can't reach Groq: {exc}") from exc


_model_cache = {}


def choose_model(key: str) -> str:
    if key in _model_cache:
        return _model_cache[key]
    ids = [m["id"] for m in _request(key, "/models").get("data", []) if m.get("active", True)]
    model = next((m for m in PREFERRED_MODELS if m in ids), None) or next(
        (m for m in ids if not any(w in m.lower() for w in SKIP_MODEL_WORDS)), None)
    if not model:
        raise SummaryError("No suitable chat model available on this Groq account")
    _model_cache[key] = model
    return model


def generate(conn, start_day: date, end_day: date, rules=None, request=None) -> dict:
    key = keys.load_key("groq")
    if not key:
        raise SummaryError("No Groq key: put it in data/groq_key.txt (or set GROQ_API_KEY)")
    payload = build_payload(conn, start_day, end_day, rules)
    if payload["totals"]["active_hours"] * 3600 < MIN_ACTIVE_SECONDS:
        raise SummaryError("Not enough activity in this period yet (need at least 10 minutes)")

    request = request or _request
    model = choose_model(key) if request is _request else "test-model"
    reply = request(key, "/chat/completions", {
        "model": model,
        "temperature": 0.4,
        "max_tokens": 1600,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "My stats:\n```json\n" + json.dumps(payload, indent=1) + "\n```"},
        ],
    })
    try:
        text = reply["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise SummaryError("Groq returned an unexpected response") from exc
    if not text:
        raise SummaryError("Groq returned an empty summary")

    row = (time.time(), payload["period"]["start"], payload["period"]["end"], model,
           json.dumps(payload), text)
    cur = conn.execute(
        "INSERT INTO summaries (created_ts, start_day, end_day, model, payload, text) VALUES (?,?,?,?,?,?)", row)
    conn.commit()
    log.info("weekly summary saved (%s to %s, %s)", row[1], row[2], model)
    return latest(conn, cur.lastrowid)


def latest(conn, summary_id=None) -> dict | None:
    q = "SELECT id, created_ts, start_day, end_day, model, payload, text FROM summaries"
    row = conn.execute(q + (" WHERE id = ?" if summary_id else " ORDER BY created_ts DESC LIMIT 1"),
                       (summary_id,) if summary_id else ()).fetchone()
    if not row:
        return None
    sid, created, start, end, model, payload, text = row
    return {"id": sid, "created_ts": created, "start": start, "end": end, "model": model,
            "payload": json.loads(payload), "text": text}


def due_week(conn, today: date | None = None):
    """Last full Monday-Sunday week, if it has no summary yet; else None."""
    today = today or date.today()
    last_sunday = today - timedelta(days=today.weekday() + 1)
    start = last_sunday - timedelta(days=6)
    done = conn.execute("SELECT 1 FROM summaries WHERE start_day = ? AND end_day = ?",
                        (start.isoformat(), last_sunday.isoformat())).fetchone()
    if done:
        return None
    # Skip weeks with no activity at all (e.g. before the tracker was installed).
    lo = analytics._midnight(start)
    hi = analytics._midnight(last_sunday + timedelta(days=1))
    active = conn.execute("SELECT 1 FROM sessions WHERE end_ts > ? AND start_ts < ? LIMIT 1", (lo, hi)).fetchone()
    return (start, last_sunday) if active else None


def last_7_days(today: date | None = None):
    today = today or date.today()
    return today - timedelta(days=6), today


def main():
    p = argparse.ArgumentParser(description="Weekly AI summary via Groq (totals only)")
    p.add_argument("--preview", action="store_true", help="print what would be sent, send nothing")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    args = p.parse_args()
    start, end = last_7_days()
    start, end = args.start or start, args.end or end

    conn = db.connect()
    try:
        if args.preview:
            print(json.dumps(build_payload(conn, start, end), indent=2))
            return
        try:
            s = generate(conn, start, end)
        except SummaryError as exc:
            raise SystemExit(f"Summary failed: {exc}")
        print(f"\nWeekly summary {s['start']} to {s['end']}  ({s['model']})\n")
        print(s["text"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
