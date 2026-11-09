# Good ol Tracker

A personal activity tracker for one Windows PC, Brave and an Android phone. It records
where your time goes (apps, sites, pages, phone apps), sorts it into categories, and
helps you change it: focus mode, distraction alerts, daily app limits, bedtime, goals,
subjects with exam countdowns, and a weekly AI coach.

**Your data stays on your PC.** It lives in a local SQLite database, the dashboard and APIs only
listen on `127.0.0.1`, and the phone syncs over your own hotspot/Wi-Fi. Three optional
services receive limited data, and each one is off until you add its key: Voyage (page
categories and search), Groq (summaries and category decisions) and Gmail (your own
report emails). Exactly what each one gets is listed under [Privacy](#privacy).

More detail: [docs/architecture.md](docs/architecture.md) (how it fits together) and
[docs/decisions.md](docs/decisions.md) (why it's built this way).

---

## Contents

- [Quick start](#quick-start)
- [What it does](#what-it-does)
- [Privacy](#privacy)
- [Categories](#categories)
- [Configuration](#configuration)
- [Day-to-day operations](#day-to-day-operations)
- [Tests](#tests)
- [Troubleshooting and known limitations](#troubleshooting-and-known-limitations)
- [Project layout](#project-layout)
- [License](#license)

---

## Quick start

Requirements: Windows 10/11, Python 3.10+ (standard library only, no `pip install`; developed on 3.15),
Brave (or Chrome/Edge). Optional: an Android phone, Voyage/Groq API keys, a Gmail App
Password.

### 1. Desktop tracker

```powershell
python desktop\tracker.py -v                                             # try it in a console (Ctrl+C stops)
powershell -ExecutionPolicy Bypass -File install_autostart.ps1          # run hidden at every login
```

The installer creates a per-user scheduled task **Good ol Tracker** (no admin) that starts
the tracker at login and relaunches it within 5 minutes if it ever stops. Only one copy
runs at a time. Remove it with `install_autostart.ps1 -Uninstall`.

Dashboard: **http://127.0.0.1:47321/**

### 2. Browser extension

1. `brave://extensions` → **Developer mode** on → **Load unpacked** → pick `extension\`.
2. On the options page, paste the contents of `data\token.txt` → **Save & test connection**.
3. Optional: tick **Read page content on all sites** (Brave asks for permission).
4. In the extension's **Details**, turn on **Allow in Private** if you want focus mode and
   the adult filter to work in private windows too.

After any extension update, press the reload icon on its card (current version **0.7.0**).

### 3. Optional: AI keys and email

| What | How |
|---|---|
| Voyage (categorising pages, "search by meaning") | key in `data\voyage_key.txt` or `VOYAGE_API_KEY` |
| Groq (AI judge, subject tagging, weekly coach) | key in `data\groq_key.txt` or `GROQ_API_KEY` |
| Gmail reports | dashboard → **Email reports**: your address + a Google App Password |

All of `data\` is gitignored.

### 4. Optional: Android phone

1. **Once, on the PC, as administrator:** `powershell -ExecutionPolicy Bypass -File phone_firewall.ps1`
   (opens TCP 47323 / UDP 47324 on **Private** networks only), then set the network you
   share with the phone (e.g. its hotspot) to **Private** in Windows Wi-Fi settings.
2. **Build and install the app:**
   ```powershell
   cd android
   C:\Android\gradle-8.9\bin\gradle.bat --offline assembleRelease
   C:\Android\sdk\platform-tools\adb.exe install -r app\build\outputs\apk\release\app-release.apk
   ```
   (Or copy `android\GoodOlTracker.apk` to the phone and open it.)
3. **In the app:** allow *Usage access* → **Connect to PC** → allow the request with the same
   4-digit code on the dashboard's **Phone** card.
4. **Also in the app:** switch on *App blocking* (Accessibility), *Alerts*, the *Adult content
   filter* (Private DNS) and, on Xiaomi, *Autostart* + battery *No restrictions*.

---

## What it does

### Tracking

- **Desktop:** the foreground app and window title every 5 s. No input for 2 min counts
  as away, unless the browser tab is playing media. This is the time total.
- **Browser:** the extension reports the active tab per site, so browser time is split by
  site and page. It also records:
  - **Searches:** read from the URL on any site (Google, Brave, YouTube, GitHub, Daraz…).
  - **Page content:** title, description, headings and main text (up to 3,000 characters).
    Always on YouTube, Medium and Reddit; on every site if you allow it. Never from text
    you type, password or card pages, private windows, or never-read sites (WhatsApp,
    banks, password managers; editable).
  - **Scroll depth:** how far down each page you got.
- **Phone:** which app was in front and when, plus unlocks, from Android's own usage
  history. Never notifications, messages or anything inside apps. Time on the phone
  replaces overlapping PC time, so nothing is counted twice.
- **Offline time:** log lectures, reading or study groups on the dashboard.
- **Retention:** page text is deleted after 60 days. Titles, URLs and times are kept.

### Seeing it

- **Dashboard** (http://127.0.0.1:47321/, refreshes every 30 s; light/dark):
  - **Today and ranges:** today, yesterday, 7/30 days or any day, with an *All / PC / Phone*
    filter.
  - **Tiles:** productive time, active time, longest focus, deep-focus sessions, switches
    per hour, phone time and unlocks.
  - **Charts:** time by category, productive hours by hour of day, day by day, the day
    block by block, and 30-day trends with this week vs last week.
  - **Lists:** top apps and sites, topics, pages read (with scroll depth), searches.
  - **Cards:** **Goals**, **Subjects** (exam countdowns), **Sleep and phone**, **Focus mode**,
    **Search what you've read**, **Fix categories**, **Phone**, **Email reports**.
  - Every chart has a *Show as table* view. **Export** gives CSV files or print/PDF.
- **Phone app:**
  - **Today:** phone and productive time, top apps, focus, goals, limits and bedtime.
  - **Week:** productive vs phone per day, goal streaks, subjects and exams, sleep, focus
    and pickups.
  - **Home-screen widget.**
- **Email** (Gmail):
  - an AI recap every 3 days at 21:00 (on by default);
  - optional daily numbers and a Monday weekly summary.
- **Weekly AI coach** (Groq): an honest read of the week and 3–4 concrete actions for the
  next one. It prioritises your nearest exam and your weakest goal, and only suggests
  settings the tracker actually has.

### Changing it

- **Focus mode** (25/50/90 min, or 5–240):
  - **Where it blocks:** distracting sites in Brave and distracting apps on the phone. Start
    it from the dashboard, the extension popup, the phone app or its widget.
  - **The blocked page/screen** shows the time left and a two-tap *End early*.
  - **Scoring:** each session is scored afterwards by the share of on-task time and the
    blocked attempts.
  - **Never locks you out:** *End early* works even if the PC is off.
- **Distraction alerts:**
  - **When:** after 20 min in a row of Entertainment/Social (short detours don't reset it).
  - **What:** a notification with *Start 25-min focus* / *Snooze*, on whichever device you're
    using.
- **Daily limits:**
  - **One budget per app across both devices,** e.g. the Facebook app + facebook.com = 60 min.
  - **Study videos don't count** towards a site's budget.
  - **At the limit:** a warning at 80%, then blocked on both devices until midnight, with one
    *5 more minutes* a day.
- **Bedtime** (23:30–06:00 by default):
  - **What locks:** social/video apps on the phone and the same sites in Brave.
  - **Reminder:** a wind-down notification 30 min before.
  - **Grace:** one *5 more minutes* a night.
- **Scheduled focus:** pick days and times; focus starts on its own. If the PC is off,
  the phone starts its own session.
- **Goals:** productive at least / phone at most / a category at most, with streaks.
  Days before a device was tracked don't count.
- **Subjects:**
  - **Tagging:** your modules (name, keywords, exam date, weekly target); productive time is
    tagged by keyword, and Groq tags study pages the keywords miss.
  - **Exam countdowns** turn amber a week out and coral 2 days out.
- **Adult filter:**
  - **Brave:** during focus or always (dashboard setting).
  - **Phone:** via Private DNS (`family.cloudflare-dns.com`). It covers every app, and the app
    warns if it's switched off.
- **Sleep and pickups** (insights):
  - **Sleep** is estimated from the longest quiet stretch of the night.
  - **Pickups while studying** count phone use between stretches of PC study.

---

## Privacy

Everything is stored locally in `data\tracker.db` (+ a few JSON settings files). The
dashboard and APIs answer only on `127.0.0.1`. Other websites can't read them (Host,
Origin and Sec-Fetch checks; the extension needs a token). The phone port accepts
private-network addresses only, behind a pairing token.

What leaves the PC, and only if you set it up:

| Service | Used for | What it receives |
|---|---|---|
| **Voyage** | Page categories, search by meaning | Each scanned page's title, channel, description, headings and first ~1,500 characters, once per page; the words you type in "search by meaning" |
| **Groq** | AI judge | *Unsure* pages: site, titles, channel, description, search words, first 500 characters. Sites/apps/phone apps with no rule: the name and up to 3 titles. Once per item |
| **Groq** | Subject tagging | Study pages your keywords didn't match: title, channel, description, first 400 characters |
| **Groq** | Weekly coach / recaps | Totals and names only: hours by category and day, app/site/phone-app names, topic words, phone time and unlocks, pickups, sleep times, goals, limits, subject names with hours and exam dates, focus sessions. **No** page text, titles, URLs or searches |
| **Gmail** | Your report emails | Totals, app/site names, topic words and the AI text, to your own address. **No** page text, titles, URLs or searches |

**Never sent anywhere:** anything on the never-read list (WhatsApp, banks, password managers,
plus banking and password-manager apps), adult sites (categorised locally), private-window
browsing (not recorded at all).

---

## Categories

Rules live in **`categories.json`** (project root). Categories are worked out every time
data is read, so **editing a rule re-categorises all history**.

Order, first match wins:

1. **Page rule** (exact URL, set from the dashboard).
2. **App rule** (`apps`: `Code.exe` → Coding), **site rule** (`sites`, subdomains included,
   most specific wins) or **phone-app rule** (`phone_apps`: `com.instagram.android` → Social).
3. **Adult site** → Entertainment.
4. For sites marked `"content"` (YouTube, Google, Claude, Reddit…): **channel rule**
   (`channels`), then **keywords** scored over the scanned page (title counts most).
5. **Voyage**: the page's similarity to the example `descriptions` per category.
6. **Groq judge**: for pages still *Unsure*, applied when it's ≥ 70% sure (shown with an
   **AI** badge).
7. Fallback: `Unsure` (content sites), `Browsing` (unknown sites), `Other` (unknown apps).

**Fix from the dashboard:** the *Fix categories* card lists everything without a rule and
every *Unsure* page:
- **Pickers:** pick a category for each one, or *Accept/Reject* Groq's suggestion.
- **Saving:** changes are written into `categories.json` in place, keeping your layout,
  checked before saving, and can be undone byte for byte. The undo is refused if you've
  edited the file by hand since.
- **Productive:** the `"productive"` list (Coding, Study, Work) decides what counts as
  productive.

---

## Configuration

| File / place | What |
|---|---|
| `categories.json` | categories, rules, keywords, AI descriptions, productive list |
| Dashboard → *Focus mode* | session length, blocked sites, adult filter mode, distraction alerts |
| Dashboard → *Limits, bedtime, schedule and goals* | `data\wellbeing.json` |
| Dashboard → *Subjects* | `data\subjects.json` |
| Dashboard → *Email reports* | `data\email.json` (+ `data\gmail_key.txt`) |
| Extension options | token, all-sites reading, never-read list, pause |

Constants worth knowing:

| Where | Setting | Default |
|---|---|---|
| `desktop/tracker.py` | `INTERVAL` / `IDLE_THRESHOLD` / `PAGE_TEXT_DAYS` | 5 s / 120 s / 60 days |
| `desktop/analytics.py` | `FOCUS_GAP` (break a focus streak tolerates) | 2 min |
| `desktop/nudge.py` | alert after / repeat / snooze (also on the dashboard) | 20 / 20 / 30 min |
| `desktop/ai_judge.py` | `MIN_CONFIDENCE`, `MAX_REQUESTS` per 2 min | 0.7, 3 |
| `extension/background.js` | `IDLE_SECONDS` | 120 s |

---

## Day-to-day operations

- **Restart after changing Python code:**
  - Stop the tracker: end `pythonw.exe` in Task Manager (the one running `tracker.py`).
  - Start it again with `Start-ScheduledTask "Good ol Tracker"`, or let the watchdog do it
    within 5 min.
- **Reports in the console:** `python desktop\report.py [YYYY-MM-DD]`
- **Weekly summary by hand:**
  - `python desktop\weekly.py --preview` prints exactly what would be sent;
  - `python desktop\weekly.py` writes one.
- **Test email:** `python desktop\mailer.py --test`, or the dashboard's *Send a test email*.
- **Backups:** daily zipped copies in `data\backups\` (newest 14 kept);
  `python desktop\backup.py` makes one now.
  - **Restore:** stop the tracker.
  - Unzip `tracker.db` over `data\tracker.db`.
  - Delete `tracker.db-wal` and `tracker.db-shm`, then start the tracker again.
- **Health:** the strip at the top of the dashboard shows the tracker, the extension (and
  its version), the AI keys and the last backup.
- **Logs:** `data\tracker.log`.

---

## Tests

```powershell
python -m unittest discover -s tests -v    # 145 tests: categories, analytics, AI (faked), focus,
                                           # alerts, phone sync, wellbeing, subjects, emails...
node tests\test_search.js                  # search detection from URLs
node tests\test_blocklist.js               # never-read sites
node tests\test_adult.js                   # adult-site filter
```

No test touches the network or your real data: APIs are faked and each test uses an
in-memory database and a temporary data folder.

---

## Troubleshooting and known limitations

- **The dashboard says the extension is old:** reload it in `brave://extensions`. The card
  must show the version above.
- **The phone can't find the PC:**
  - **Same network:** the laptop must be on the phone's hotspot (or the same Wi-Fi), and
    that network must be **Private**.
  - **Firewall:** `phone_firewall.ps1` must have been run as administrator.
- **Xiaomi phones** stop background apps: allow *Autostart*, battery *No restrictions*,
  and lock the app in Recents. Missed syncs catch up when you open the app (Android keeps
  about a week of usage history).
- **Not blocked:** desktop apps (only browser sites and phone apps); Brave's Tor windows; a
  browser set to its own custom "secure DNS" provider (this bypasses the phone filter).
  These are self-control tools, not locks.
- **Sleep** is an estimate: lying awake without the phone counts as sleep.
- **YouTube layout changes** can break page scanning; it falls back to the tab title.
- **Some Store apps** show up as `ApplicationFrameHost.exe`.

---

## Project layout

```
desktop/            Python tracker, local server, analytics, AI, email (stdlib only)
  dashboard/        the dashboard page (plain HTML/CSS/JS, no libraries)
extension/          Brave/Chrome extension (Manifest V3)
android/            Android companion app (Kotlin, Jetpack Compose)
tests/              Python unittest + Node tests
docs/               architecture.md, decisions.md
categories.json     your category rules
install_autostart.ps1, phone_firewall.ps1
data/               your database, settings, keys, logs, backups (gitignored)
```

---

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 shihaz6.
