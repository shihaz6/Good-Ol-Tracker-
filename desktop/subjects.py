"""Subjects / projects: which module your study time went to (DBMS, OOP, ...), and exams.

You list your subjects on the dashboard (data/subjects.json): a name, keywords, and
optionally an exam date and a weekly study target. Every productive stretch (Study /
Coding / Work, PC or offline time you logged) is tagged with the subject whose keywords
match best in its window title, tab title, page title, channel, description or headings.
Pages the keywords miss can be tagged by Groq (background, ai_judge-style; cached in
subject_judge). Only productive time is tagged; nothing else changes.
"""

import hashlib
import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import categorize
import db

log = logging.getLogger("tracker")

MAX_SUBJECTS = 20
MIN_PAGE_SECONDS = 60      # Groq only looks at study pages you spent at least this long on
GROQ_BATCH = 20
GROQ_MIN_CONFIDENCE = 0.7
_lock = threading.Lock()


class SubjectError(ValueError):
    pass


# ------------------------------------------------------------------ settings

def _path():
    return db.DATA_DIR / "subjects.json"


def settings() -> list:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data.get("subjects", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def save(subjects) -> list:
    if not isinstance(subjects, list) or len(subjects) > MAX_SUBJECTS:
        raise SubjectError(f"subjects must be a list (up to {MAX_SUBJECTS})")
    out, seen = [], set()
    for i, s in enumerate(subjects):
        if not isinstance(s, dict):
            raise SubjectError("each subject must be an object")
        name = str(s.get("name") or "").strip()[:40]
        if not name:
            raise SubjectError(f"subject {i + 1} needs a name")
        if name.lower() in seen:
            raise SubjectError(f"two subjects are called {name!r}")
        seen.add(name.lower())
        kw = s.get("keywords", [])
        if isinstance(kw, str):
            kw = kw.split(",")
        keywords = sorted({k.strip().lower() for k in kw if str(k).strip()})[:40]
        exam = s.get("exam") or None
        if exam:
            try:
                exam = date.fromisoformat(str(exam)).isoformat()
            except ValueError:
                raise SubjectError(f"{name}: exam date must be YYYY-MM-DD") from None
        weekly = s.get("weekly_minutes") or None
        if weekly is not None:
            try:
                weekly = int(weekly)
            except (TypeError, ValueError):
                raise SubjectError(f"{name}: weekly target must be a number of minutes") from None
            if not 0 <= weekly <= 100 * 60:
                raise SubjectError(f"{name}: weekly target must be 0-100 hours")
            weekly = weekly or None
        out.append({"name": name, "keywords": keywords, "exam": exam, "weekly_minutes": weekly})
    with _lock:
        db.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps({"subjects": out}, indent=2), encoding="utf-8")
    return out


def subjects_hash(subjects) -> str:
    return hashlib.sha1(json.dumps([[s["name"], s["keywords"]] for s in subjects]).encode()).hexdigest()


# ------------------------------------------------------------------ tagging

def _patterns(subjects):
    """name -> regex of its keywords (+ the name itself), whole words, case-insensitive."""
    out = {}
    for s in subjects:
        words = [s["name"].lower(), *s["keywords"]]
        out[s["name"]] = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words if w) + r")(?!\w)", re.I)
    return out


FIELDS = (("title", 3), ("page_title", 3), ("author", 2), ("description", 1), ("headings", 1), ("url", 1))


def match(patterns, fields: dict):
    """Best subject for the given text fields, or None (ties go to nobody)."""
    scores = defaultdict(float)
    for f, w in FIELDS:
        text = fields.get(f)
        if not text:
            continue
        for name, rx in patterns.items():
            n = min(len(rx.findall(text)), 3)
            if n:
                scores[name] += n * w
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
        return None
    return ranked[0][0]


def _judged(conn, urls, h):
    out, urls = {}, list({u for u in urls if u})
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        out.update(conn.execute(
            f"SELECT url, subject FROM subject_judge WHERE subjects_hash = ? AND subject IS NOT NULL"
            f" AND url IN ({','.join('?' * len(chunk))})", (h, *chunk)).fetchall())
    return out


def tag_items(conn, items, rules, subjects=None):
    """[(item, subject or None)] for the productive items among `items`."""
    subjects = subjects if subjects is not None else settings()
    prod = [it for it in items if it.category in rules.productive]
    if not subjects:
        return [(it, None) for it in prod]
    pats = _patterns(subjects)
    pages = categorize._load_pages(conn, [it.url for it in prod if it.url])
    judged = _judged(conn, [it.url for it in prod if it.url], subjects_hash(subjects))
    names = {s["name"] for s in subjects}
    out = []
    for it in prod:
        p = pages.get(it.url) or {}
        subj = match(pats, {"title": it.title if it.source != "phone" else it.app, "page_title": p.get("title"),
                            "author": p.get("author"), "description": p.get("description"),
                            "headings": (p.get("headings") or "")[:600], "url": it.url})
        if subj is None and it.url and judged.get(it.url) in names:
            subj = judged[it.url]
        out.append((it, subj))
    return out


# ------------------------------------------------------------------ summary

def _midnight(d: date) -> float:
    return datetime.combine(d, datetime.min.time()).timestamp()


def summary(conn, start_day: date, end_day: date, rules=None, today: date | None = None) -> dict:
    """Time per subject in the range + this week (Mon-today) + exam countdowns."""
    rules = rules or categorize.Rules()
    today = today or date.today()
    subjects = settings()
    week0 = today - timedelta(days=today.weekday())
    lo = min(_midnight(start_day), _midnight(week0))
    hi = max(_midnight(end_day + timedelta(days=1)), _midnight(today + timedelta(days=1)))
    items = categorize.categorise(conn, lo, min(hi, time.time()), rules)
    tagged = tag_items(conn, items, rules, subjects)
    r0, r1 = _midnight(start_day), _midnight(end_day + timedelta(days=1))
    w0, w1 = _midnight(week0), _midnight(today + timedelta(days=1))

    def clip(it, a, b):
        return max(0.0, min(it.end, b) - max(it.start, a))

    rng, week, untagged = defaultdict(float), defaultdict(float), 0.0
    per_day = defaultdict(lambda: defaultdict(float))
    top = defaultdict(lambda: defaultdict(float))
    for it, subj in tagged:
        secs = clip(it, r0, r1)
        if subj:
            rng[subj] += secs
            week[subj] += clip(it, w0, w1)
            if secs:
                per_day[subj][datetime.fromtimestamp(max(it.start, r0)).date().isoformat()] += secs
                top[subj][it.title if it.source != "browser" else (it.title or it.app)] += secs
        else:
            untagged += secs
    out = []
    for s in subjects:
        n = s["name"]
        days_left = (date.fromisoformat(s["exam"]) - today).days if s.get("exam") else None
        out.append({
            "name": n, "keywords": s["keywords"], "exam": s.get("exam"), "days_left": days_left,
            "weekly_minutes": s.get("weekly_minutes"),
            "seconds": round(rng[n]), "week_seconds": round(week[n]),
            "per_day": {d: round(v) for d, v in sorted(per_day[n].items())},
            "top": [{"title": t, "seconds": round(v)} for t, v in sorted(top[n].items(), key=lambda kv: -kv[1])[:5]],
        })
    upcoming = sorted((s for s in out if s["days_left"] is not None and s["days_left"] >= 0), key=lambda s: s["days_left"])
    return {"subjects": out, "untagged_seconds": round(untagged), "week_start": week0.isoformat(),
            "next_exam": upcoming[0] if upcoming else None}


# ------------------------------------------------------------------ Groq: pages the keywords miss

def run_batch(conn, rules, request=None, now=None) -> dict:
    """Ask Groq which subject recent, unmatched study pages belong to (once per page)."""
    import ai_judge  # local import: ai_judge -> weekly -> analytics would loop at import time
    import keys
    import weekly

    now = now or time.time()
    subjects = settings()
    if not subjects:
        return {"status": "no_subjects"}
    key = keys.load_key("groq")
    if not key:
        return {"status": "no_key"}
    if now < ai_judge.status["paused_until"]:
        return {"status": "waiting"}
    h = subjects_hash(subjects)
    items = categorize.categorise(conn, now - 14 * 86400, now, rules)
    per_url = defaultdict(float)
    for it in items:
        if it.url and it.category in rules.productive and it.source == "browser":
            per_url[it.url] += it.seconds
    urls = [u for u, s in sorted(per_url.items(), key=lambda kv: -kv[1]) if s >= MIN_PAGE_SECONDS]
    if not urls:
        return {"status": "ok", "pages": 0}
    done = set()
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        done.update(u for (u,) in conn.execute(
            f"SELECT url FROM subject_judge WHERE subjects_hash = ? AND url IN ({','.join('?' * len(chunk))})", (h, *chunk)))
    pats = _patterns(subjects)
    pages = categorize._load_pages(conn, urls)
    titles = {it.url: it.title for it in items if it.url}
    todo = []
    for u in urls:
        if u in done:
            continue
        p = pages.get(u) or {}
        fields = {"title": titles.get(u), "page_title": p.get("title"), "author": p.get("author"),
                  "description": p.get("description"), "headings": (p.get("headings") or "")[:600], "url": u}
        if match(pats, fields):
            continue  # keywords already decide it
        payload = {"title": ai_judge._clip(p.get("title") or titles.get(u), 200)}
        if p.get("author"):
            payload["channel"] = ai_judge._clip(p["author"], 80)
        if p.get("description"):
            payload["description"] = ai_judge._clip(p["description"], 250)
        if p.get("body"):
            payload["text_start"] = ai_judge._clip(p["body"], 400)
        todo.append((u, payload))
        if len(todo) >= GROQ_BATCH:
            break
    if not todo:
        return {"status": "ok", "pages": 0}
    model = ai_judge.status["model"] or ("test-model" if request else weekly.choose_model(key))
    lines = "\n".join(f"- {s['name']}" + (f": {', '.join(s['keywords'][:12])}" if s["keywords"] else "") for s in subjects)
    system = ("You sort a university student's study pages into their modules. Modules:\n" + lines +
              '\nFor each item pick the one module it is clearly about, or "none" if it fits none of them. '
              "confidence 0-1. Reply with JSON only: "
              '{"results": [{"id": <id>, "subject": "<module or none>", "confidence": <0-1>}]}')
    request = request or weekly._request
    try:
        reply = request(key, "/chat/completions", {
            "model": model, "temperature": 0, "max_tokens": 60 * len(todo) + 200,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps({"items": [{"id": i, **p} for i, (_, p) in enumerate(todo)]},
                                                                ensure_ascii=False)}]})
        results = json.loads(reply["choices"][0]["message"]["content"]).get("results") or []
    except weekly.SummaryError as exc:
        ai_judge.status["paused_until"] = now + ai_judge.BACKOFF
        raise ai_judge.JudgeError(str(exc)) from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ai_judge.JudgeError("Groq returned an unexpected answer") from exc
    names = {s["name"] for s in subjects}
    by_id = {}
    for r in results:
        if isinstance(r, dict):
            try:
                by_id[r.get("id")] = (str(r.get("subject") or ""), float(r.get("confidence", 0)))
            except (TypeError, ValueError):
                continue
    rows = []
    for i, (u, _) in enumerate(todo):
        subj, conf = by_id.get(i, ("", 0.0))
        rows.append((u, subj if subj in names and conf >= GROQ_MIN_CONFIDENCE else None, h, now))
    conn.executemany("INSERT OR REPLACE INTO subject_judge VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    return {"status": "ok", "pages": len(rows), "tagged": sum(1 for r in rows if r[1])}
