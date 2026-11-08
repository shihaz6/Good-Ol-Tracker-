"""Weekly AI summary via Groq.

What is sent (see build_payload): totals and patterns only - hours per category and
day, productive hours by time of day, focus/switch stats, top app/site NAMES, top
topic WORDS and the same totals for the previous period. Never sent: page text, page
titles, URLs, search queries, window titles.

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
import keys

log = logging.getLogger("tracker")

GROQ_BASE = "https://api.groq.com/openai/v1"
PREFERRED_MODELS = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
                    "llama-3.1-8b-instant"]
SKIP_MODEL_WORDS = ("whisper", "guard", "tts", "playai", "embed", "safeguard")
MIN_ACTIVE_SECONDS = 10 * 60  # don't ask for a summary of almost nothing

SYSTEM_PROMPT = """You are a friendly, practical productivity coach. You receive one person's \
computer-usage statistics for a period as JSON. Use ONLY these numbers; never invent data, \
apps, or topics. Speak to the person directly ("you"). Write durations like "3h 20m".

Format (Markdown):
## Overview
2-3 sentences: how the period went overall.
## Patterns
3-5 bullet points: peak productive hours, focus streaks, context switching, standout days or apps.
## Compared with the previous period
1-3 bullet points. Leave this section out if previous_period is null.
## Try this week
3 specific, doable suggestions, each tied to a number above.

Keep it under 280 words."""


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
        "previous_period": None if not prev["totals"]["active"] else {
            "active_hours": _h(prev["totals"]["active"]),
            "productive_hours": _h(prev["totals"]["productive"]),
            "hours_by_category": {k: _h(v) for k, v in prev["totals"]["by_category"].items()},
        },
    }


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
        "max_tokens": 1200,
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
    return None if done else (start, last_sunday)


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
