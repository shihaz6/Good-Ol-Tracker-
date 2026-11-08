"""Email reports through Gmail (SMTP + an App Password).

Settings: data/email.json (edited from the dashboard). Password: data/gmail_key.txt
(or GMAIL_API_KEY), a 16-character Google App Password, never your normal password.

Emails contain totals, top app/site names, topic words and the AI summary. Never page
text, page titles, URLs or search queries.

    python desktop/mailer.py --test     # send today's recap now
"""

import argparse
import html
import json
import logging
import re
import smtplib
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import analytics
import categorize
import db
import focus
import insights
import keys
import subjects
import weekly
import wellbeing

log = logging.getLogger("tracker")

SETTINGS_PATH = db.DATA_DIR / "email.json"
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
DASHBOARD = "http://127.0.0.1:47321/"
DEFAULTS = {
    "address": "", "to": "",
    "daily_enabled": False, "daily_time": "21:00",            # today's numbers, every evening
    "periodic_enabled": True, "periodic_days": 3, "periodic_time": "21:00",  # AI recap every N days
    "weekly_enabled": False,                                  # Monday's weekly AI summary
}
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MailError(Exception):
    pass


# --------------------------------------------------------------------- settings

def load_settings() -> dict:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}


def save_settings(new: dict) -> dict:
    s = load_settings()
    address = str(new.get("address", s["address"])).strip()
    to = str(new.get("to", s["to"])).strip() or address
    daily_time = str(new.get("daily_time", s["daily_time"])).strip()
    periodic_time = str(new.get("periodic_time", s["periodic_time"])).strip()
    if address and not EMAIL_RE.match(address):
        raise ValueError("Gmail address doesn't look like an email address")
    if to and not EMAIL_RE.match(to):
        raise ValueError("'Send to' doesn't look like an email address")
    if not TIME_RE.fullmatch(daily_time) or not TIME_RE.fullmatch(periodic_time):
        raise ValueError("Times must be HH:MM (24-hour)")
    try:
        periodic_days = int(new.get("periodic_days", s["periodic_days"]))
    except (TypeError, ValueError):
        raise ValueError("'Every N days' must be a number") from None
    if not 1 <= periodic_days <= 14:
        raise ValueError("'Every N days' must be between 1 and 14")
    s.update(address=address, to=to, daily_time=daily_time, periodic_time=periodic_time,
             periodic_days=periodic_days,
             daily_enabled=bool(new.get("daily_enabled", s["daily_enabled"])),
             periodic_enabled=bool(new.get("periodic_enabled", s["periodic_enabled"])),
             weekly_enabled=bool(new.get("weekly_enabled", s["weekly_enabled"])))
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def save_password(password: str) -> None:
    pw = re.sub(r"\s+", "", password or "")  # Google shows it as "abcd efgh ijkl mnop"
    if not re.fullmatch(r"[A-Za-z0-9]{16}", pw):
        raise ValueError("An App Password is 16 letters (Google shows it in 4 groups of 4)")
    (db.DATA_DIR / "gmail_key.txt").write_text(pw, encoding="utf-8")


def status() -> dict:
    s = load_settings()
    return {**s, "has_password": bool(keys.load_key("gmail")),
            "configured": bool(s["address"] and keys.load_key("gmail"))}


# --------------------------------------------------------------------- content

def _dur(seconds) -> str:
    m = round((seconds or 0) / 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _delta(now_v, prev_v, unit="dur", good_up=True) -> str:
    if prev_v in (None, 0) or now_v is None:
        return ""
    diff = now_v - prev_v
    if abs(diff) < (60 if unit == "dur" else 0.5):
        return " (same as last week)"
    arrow = "▲" if diff > 0 else "▼"
    word = "better" if (diff > 0) == good_up else "worse"
    shown = _dur(abs(diff)) if unit == "dur" else f"{abs(diff):g}"
    return f" ({arrow} {shown} vs last week, {word})"


def _html_doc(title: str, blocks: list) -> str:
    style = "font-family:Segoe UI,Arial,sans-serif;color:#0b0b0b;max-width:560px;line-height:1.5"
    return (f'<div style="{style}"><h2 style="font-size:18px;margin:0 0 12px">{html.escape(title)}</h2>'
            + "".join(blocks)
            + f'<p style="color:#898781;font-size:12px;margin-top:24px">Sent by your Good ol Tracker. '
              f'Dashboard (on your PC): <a href="{DASHBOARD}">{DASHBOARD}</a></p></div>')


def _html_table(rows) -> str:
    cells = "".join(
        f'<tr><td style="padding:4px 16px 4px 0;color:#52514e">{html.escape(str(a))}</td>'
        f'<td style="padding:4px 0;font-weight:600">{html.escape(str(b))}</td></tr>' for a, b in rows)
    return f'<table style="border-collapse:collapse;margin:0 0 16px">{cells}</table>'


def _html_markdown(md: str) -> str:
    out, in_list = [], None
    for raw in md.splitlines():
        line = re.sub(r"\*\*|__|`", "", raw.strip())
        bullet = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)", line)
        if bullet:
            if not in_list:
                out.append("<ul style='padding-left:20px;margin:0 0 10px'>")
                in_list = True
            out.append(f"<li>{html.escape(bullet.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = None
        if not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.*)", line)
        out.append(f"<h3 style='font-size:15px;margin:14px 0 4px'>{html.escape(heading.group(1))}</h3>" if heading
                   else f"<p style='margin:0 0 8px'>{html.escape(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def build_daily(conn, day: date, rules=None):
    s = analytics.summary(conn, day, day, rules)
    t = s["totals"]
    if not t["active"]:
        raise MailError(f"No activity recorded on {day.isoformat()}")
    pct = round(100 * t["productive"] / t["active"])
    label = day.strftime("%a %d %b")
    subject = f"Good ol Tracker · {label}: {_dur(t['productive'])} productive ({pct}%)"
    longest = s["focus"]["longest"]
    stats = [
        ("Active", _dur(t["active"])),
        ("Productive", f"{_dur(t['productive'])} ({pct}%)"),
        ("Longest focus", f"{_dur(longest['seconds'])} ({longest['category']})" if longest else "none over 5m"),
        ("Deep focus sessions (25m+)", s["focus"]["deep_count"]),
        ("Switches per hour", s["switches"]["per_hour"] if s["switches"]["per_hour"] is not None else "-"),
        ("Searches", s["search_count"]),
    ]
    if s["offline_seconds"]:
        stats.insert(2, ("Offline (logged)", _dur(s["offline_seconds"])))
    stats += _phone_rows(conn, day, day, rules)
    stats += _goal_rows(conn, day, day, rules)
    stats += _subject_rows(conn, day, day, rules)
    cats = sorted(t["by_category"].items(), key=lambda kv: -kv[1])
    apps = [(a["name"], _dur(a["seconds"])) for a in s["top_apps"][:5]]
    topics = ", ".join(x["term"] for x in s["topics"][:10]) or "-"

    text = "\n".join([subject, "", *[f"{a}: {b}" for a, b in stats], "", "By category:",
                      *[f"  {c}: {_dur(v)}" for c, v in cats], "", "Top apps and sites:",
                      *[f"  {a}: {b}" for a, b in apps], "", f"Topics: {topics}", "", f"Dashboard: {DASHBOARD}"])
    body = _html_doc(subject, [
        _html_table(stats),
        "<h3 style='font-size:15px;margin:0 0 4px'>By category</h3>", _html_table([(c, _dur(v)) for c, v in cats]),
        "<h3 style='font-size:15px;margin:0 0 4px'>Top apps and sites</h3>", _html_table(apps),
        f"<p><b>Topics:</b> {html.escape(topics)}</p>",
    ])
    return subject, text, body


def _subject_rows(conn, start: date, end: date, rules) -> list:
    """Study time per subject in the period, with exam countdowns."""
    s = subjects.summary(conn, start, end, rules, today=end)
    rows = []
    for x in sorted(s["subjects"], key=lambda x: (x["days_left"] is None or x["days_left"] < 0, x["days_left"] or 0, -x["seconds"])):
        if not x["seconds"] and x["days_left"] is None:
            continue
        line = _dur(x["seconds"])
        if x["weekly_minutes"]:
            line += f" · this week {_dur(x['week_seconds'])} of {_dur(x['weekly_minutes'] * 60)}"
        if x["days_left"] is not None and x["days_left"] >= 0:
            line += " · exam today!" if x["days_left"] == 0 else f" · exam in {x['days_left']} day{'s' if x['days_left'] != 1 else ''}"
        rows.append((f"Subject: {x['name']}", line))
    return rows


def _goal_rows(conn, start: date, end: date, rules) -> list:
    """Each daily goal: on how many finished days of the period it was met, and the streak."""
    n = (end - start).days + 1
    rows = []
    for g in wellbeing.goals(conn, today=end, rules=rules):
        days = [h for h in g["history"][-n:] if h["met"] is not None and (h["final"] or h["met"])]
        if days:
            rows.append((f"Goal: {g['label']}",
                         f"met {sum(1 for h in days if h['met'])} of {len(days)} days · streak {g['streak']}"))
    return rows


def _phone_rows(conn, start: date, end: date, rules, delta=None) -> list:
    """Phone time, phone pickups during study and sleep, when a phone is connected."""
    if not conn.execute("SELECT 1 FROM sessions WHERE source = 'phone' LIMIT 1").fetchone():
        return []
    rules = rules or categorize.Rules()
    n = (end - start).days + 1
    cur = analytics.summary(conn, start, end, rules, device="phone")
    p = cur["phone"]
    rows = []
    if p["seconds"]:
        line = f"{_dur(p['seconds'])}"
        if n > 1:
            line += f" ({_dur(p['seconds'] / n)}/day)"
        if delta:
            prev = analytics.summary(conn, start - timedelta(days=n), start - timedelta(days=1), rules, device="phone")
            if prev["phone"]["seconds"]:
                line += delta(p["seconds"], prev["phone"]["seconds"], "dur", False)
        line += f" · {p['unlocks']} unlocks"
        rows.append(("Phone", line))
        top = [a for a in cur["top_apps"] if a["kind"] == "phone"][:3]
        if top:
            rows.append(("Top phone apps", ", ".join(f"{a['label']} {_dur(a['seconds'])}" for a in top)))
    s0, s1 = analytics._midnight(start), analytics._midnight(end + timedelta(days=1))
    intr = insights.interruptions(conn, s0, s1, rules)
    if intr["count"]:
        line = f"{intr['count']} pickups, {_dur(intr['seconds'])} in total"
        if intr["per_study_hour"] is not None:
            line += f" · {intr['per_study_hour']} per study hour"
        if intr["top_apps"]:
            line += f" · mostly {intr['top_apps'][0]['app']}"
        rows.append(("Phone during study", line))
    sl = insights.sleep(conn, end, min(n, 14), rules)
    if sl["avg_seconds"]:
        rows.append(("Sleep (estimated)", f"{_dur(sl['avg_seconds'])} a night · usually {sl['avg_bedtime']} to {sl['avg_wake']}"))
    return rows


def build_period(conn, start: date, end: date, ai_text: str | None, ai_note: str = "", rules=None):
    """Recap email for any period: AI summary (if available) + numbers vs the period before."""
    now_p, prev_p = analytics.compare_periods(conn, start, end, rules)
    n = (end - start).days + 1
    span = "week" if n == 7 else f"{n} days"
    rng = f"{start.strftime('%d %b')} – {end.strftime('%d %b')}"
    subject = f"Good ol Tracker · {span} {rng}: {_dur(now_p['productive'])} productive"
    this, vs = ("This week", "last week") if n == 7 else (f"These {n} days", f"the previous {n} days")

    def d(a, b, unit="dur", good_up=True):
        return _delta(a, b, unit, good_up).replace("last week", vs) if prev_p["active"] else ""

    compare = [
        ("Active", _dur(now_p["active"]) + d(now_p["active"], prev_p["active"])),
        ("Productive", _dur(now_p["productive"]) + d(now_p["productive"], prev_p["productive"])),
        ("Longest focus", _dur(now_p["longest_focus"]) + d(now_p["longest_focus"], prev_p["longest_focus"])),
        ("Deep focus sessions", f"{now_p['deep_count']}" + d(now_p["deep_count"], prev_p["deep_count"], "n")),
        ("Switches per hour", f"{now_p['switches_per_hour'] or '-'}"
         + d(now_p["switches_per_hour"], prev_p["switches_per_hour"], "n", good_up=False)),
    ]
    f = focus.history(conn, analytics._midnight(start), analytics._midnight(end + timedelta(days=1)), rules)
    if f["sessions"]:
        line = f"{f['completed']} finished, {f['stopped']} ended early · {_dur(f['focused_seconds'])}"
        if f["blocked"]:
            line += f" · {f['blocked']} blocked site visits"
        compare.append(("Focus sessions", line))
    compare += _phone_rows(conn, start, end, rules, d)
    compare += _goal_rows(conn, start, end, rules)
    compare += _subject_rows(conn, start, end, rules)
    cats = sorted(now_p["by_category"].items(), key=lambda kv: -kv[1])
    if ai_text:
        ai_block = ai_text
    elif ai_note:
        ai_block = f"(AI summary unavailable: {ai_note})"
    else:
        ai_block = ""
    heading = f"{this} vs {vs}" if prev_p["active"] else this
    text = "\n".join([subject, "", ai_block, "", f"{heading}:", *[f"  {a}: {b}" for a, b in compare], "",
                      "By category:", *[f"  {c}: {_dur(v)}" for c, v in cats], "", f"Dashboard: {DASHBOARD}"])
    body = _html_doc(subject, [
        _html_markdown(ai_text) if ai_text else (f"<p style='color:#898781'>{html.escape(ai_block)}</p>" if ai_block else ""),
        f"<h3 style='font-size:15px;margin:16px 0 4px'>{html.escape(heading)}</h3>", _html_table(compare),
        "<h3 style='font-size:15px;margin:0 0 4px'>By category</h3>", _html_table([(c, _dur(v)) for c, v in cats]),
    ])
    return subject, text, body


def build_weekly(conn, summary: dict, rules=None):
    return build_period(conn, date.fromisoformat(summary["start"]), date.fromisoformat(summary["end"]),
                        summary["text"], rules=rules)


def build_periodic(conn, end: date, days: int, rules=None):
    """Every-N-days recap: writes a fresh AI summary for the period (Groq) and builds the email.
    Raises MailError if there was no real activity (nothing worth emailing)."""
    start = end - timedelta(days=days - 1)
    s = analytics.summary(conn, start, end, rules)
    if s["totals"]["active"] < weekly.MIN_ACTIVE_SECONDS:
        raise MailError(f"Almost no activity from {start} to {end}; skipped")
    try:
        ai_text, note = weekly.generate(conn, start, end, rules)["text"], ""
    except weekly.SummaryError as exc:
        ai_text, note = None, str(exc)  # still send the numbers
    return build_period(conn, start, end, ai_text, note, rules)


# --------------------------------------------------------------------- sending

def send(subject: str, text: str, body_html: str, smtp_factory=None) -> str:
    s = load_settings()
    password = keys.load_key("gmail")
    if not s["address"] or not password:
        raise MailError("Email isn't set up: add your Gmail address and App Password on the dashboard")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Good ol Tracker <{s['address']}>", s["to"] or s["address"]
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30))
    try:
        with factory() as smtp:
            smtp.login(s["address"], password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("Gmail rejected the login: check the address and App Password "
                        "(2-Step Verification must be on for App Passwords)") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"Couldn't send via Gmail: {exc}") from exc
    log.info("email sent: %s", subject)
    return msg["To"]


def maybe_send(conn, now: datetime | None = None) -> list:
    """Called by the tracker's background thread. Sends what's due, at most once each."""
    now = now or datetime.now()
    s = load_settings()
    if not (s["address"] and keys.load_key("gmail")):
        return []
    sent = []
    today = now.date().isoformat()
    if s["daily_enabled"] and now.strftime("%H:%M") >= s["daily_time"] and db.get_meta(conn, "last_daily_email") != today:
        db.set_meta(conn, "last_daily_email", today)  # mark first: never spam on repeated failures
        try:
            send(*build_daily(conn, now.date()))
            sent.append("daily")
        except MailError as exc:
            log.warning("daily email not sent: %s", exc)
    if s["periodic_enabled"] and now.strftime("%H:%M") >= s["periodic_time"]:
        last = db.get_meta(conn, "last_periodic_email")
        if last is None or (now.date() - date.fromisoformat(last)).days >= s["periodic_days"]:
            db.set_meta(conn, "last_periodic_email", today)
            try:
                send(*build_periodic(conn, now.date(), s["periodic_days"]))
                sent.append("periodic")
            except MailError as exc:
                log.warning("%s-day email not sent: %s", s["periodic_days"], exc)
    latest = weekly.latest(conn)
    if (s["weekly_enabled"] and latest and date.fromisoformat(latest["start"]).weekday() == 0
            and date.fromisoformat(latest["end"]) - date.fromisoformat(latest["start"]) == timedelta(days=6)
            and str(latest["id"]) != db.get_meta(conn, "last_weekly_email")):
        db.set_meta(conn, "last_weekly_email", latest["id"])
        try:
            send(*build_weekly(conn, latest))
            sent.append("weekly")
        except MailError as exc:
            log.warning("weekly email not sent: %s", exc)
    return sent


def send_test(conn) -> str:
    """Today's recap right now (or yesterday's if today is empty)."""
    rules = categorize.Rules()
    try:
        content = build_daily(conn, date.today(), rules)
    except MailError:
        content = build_daily(conn, date.today() - timedelta(days=1), rules)
    return send(*content)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Good ol Tracker email reports")
    p.add_argument("--test", action="store_true", help="send today's recap now")
    a = p.parse_args()
    c = db.connect()
    try:
        if a.test:
            print("Sent to", send_test(c))
        else:
            print(json.dumps(status(), indent=2))
    except MailError as exc:
        raise SystemExit(f"Email failed: {exc}")
    finally:
        c.close()
