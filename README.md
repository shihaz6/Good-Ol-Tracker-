# Good ol Tracker

Local-only activity tracker. Nothing leaves this PC.

- **Step 1 - desktop logger** (`desktop/`): active app + window title, idle detection, SQLite.
- **Step 2 - browser extension** (`extension/`): per-site tab time, media-aware idle,
  and page scanning on a small allowlist. Talks to a server inside the desktop tracker
  on `127.0.0.1:47321`.

- **Step 3 - categories** (`categories.json`, `desktop/categorize.py`): Coding, Study,
  Work, ... by app, site, YouTube channel and page keywords.
- **Step 4 - dashboard** (`desktop/analytics.py`, `desktop/dashboard/`): served by the
  tracker at http://127.0.0.1:47321/.

No dependencies: Python stdlib only.

## Run

```powershell
python desktop\tracker.py -v      # watch it log window changes; Ctrl+C to stop
python desktop\report.py          # today's summary
python desktop\report.py 2026-11-07
```

Data lives in `data\`: `tracker.db` (SQLite), `tracker.log`, `token.txt` (extension key).

## Background, autostart and watchdog

```powershell
powershell -ExecutionPolicy Bypass -File install_autostart.ps1              # install / update
powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall   # remove
```

This creates a per-user scheduled task **Good ol Tracker** (no admin needed) that starts
the tracker at login and **checks every 5 minutes, relaunching it if it has stopped**.
Only one tracker runs at a time; extra launches exit immediately, so the watchdog never
creates duplicates. It also never stops on battery or after a time limit.

- Restart after changing Python code: end `pythonw.exe` in Task Manager, then
  `Start-ScheduledTask "Good ol Tracker"` (or just wait up to 5 minutes).
- Run by hand instead: `python desktop\tracker.py -v`.

## Backups

Once a day the tracker writes a consistent, zipped copy of the database to
`data\backups\tracker-YYYY-MM-DD.db.zip` and keeps the newest **14**. Make one now:
`python desktop\backup.py`.

**Restore:** end `pythonw.exe`, unzip the backup's `tracker.db` over `data\tracker.db`,
delete `data\tracker.db-wal` and `data\tracker.db-shm`, then start the tracker again.

## Health

The strip at the top of the dashboard shows whether the desktop tracker is sampling,
when the extension last checked in (every 30s while Brave is open) and whether page
scanning is on, whether the AI keys work, and when the last backup ran.

## Install the extension (Brave / Chrome / Edge)

1. Open `brave://extensions` and turn on **Developer mode** (top right).
2. **Load unpacked** and pick the `extension` folder.
3. The options page opens: paste the contents of `data\token.txt` and click
   **Save & test connection**. It should say *Connected*.
4. After editing extension code, click the reload icon on the extension card.

## What gets stored

| Table      | What                                                                  |
|------------|-----------------------------------------------------------------------|
| `sessions` | `source='desktop'`: every foreground window. This is the time total. |
|            | `source='browser'`: active tab per site (breakdown of browser time).  |
| `pages`    | Scanned content, one row per page (latest scan wins).                 |
| `searches` | Every search: time, engine/site, query, URL.                          |

**Searches** are read from the URL, so they're captured on any site without page
access: Brave Search, Google, Bing, DuckDuckGo, YouTube, GitHub, Stack Overflow,
Reddit, Wikipedia, Amazon, Daraz, LinkedIn, X, plus any site using `?q=`, `?query=`,
`?search=` or `?keyword=`. Paging through results or reloading within 10 minutes
isn't counted again. Add a site in `extension/search.js`. The extension's options
page has a **Pause** switch that stops all recording.

**Page content** (title, description, headings, main text up to 3,000 characters):

- Always on YouTube videos, Medium articles and Reddit threads.
- On **every other site** once you tick *Read page content on all sites* in the
  extension's options (Brave asks for permission; untick to revoke).
- **Never:** text you type (inputs, text boxes, editable areas are removed before
  reading), pages with password or card fields, incognito, and blocked sites. The
  default block list is **WhatsApp, banks and password managers** (plus localhost, so
  the tracker never reads its own pages). It's editable in the options page, and the
  toolbar popup has a **Never read this site** button. Any hostname containing "bank"
  is always skipped.
- **Retention:** full page text is deleted after **60 days** (`PAGE_TEXT_DAYS` in
  `desktop/tracker.py`); titles, URLs and time are kept.

**Scroll depth:** every browser visit records how far down the page you got
(`sessions.scroll_pct`, 0-100), shown as *read %* in the report's top pages.
Pages that scroll inside a box instead of the whole window (e.g. Gmail) report less.

## Dashboard

Open **http://127.0.0.1:47321/** while the tracker runs (or click **Open dashboard** in
the extension popup). Bookmark it. Ranges: today, yesterday, last 7/30 days, or any day.
Today's view refreshes every 30 seconds.

- **Tiles:** productive time, active time, longest focus streak, deep-focus sessions
  (25 min+), app/site switches per hour.
- **Time by category**, **when you're productive** (by hour of day), **day by day**
  (multi-day ranges), and **your day block by block** (hover a block to see what it was).
- **Top apps and sites**, **top topics** (from searches + pages you read 30s+),
  **pages read** (with scroll depth), **searches**, and **needs a rule**.
- Every chart has a *Show as table* view. Light/dark follows the system; the
  *Theme* button overrides it.

- **Trends:** 30-day lines for productive time, longest focus and switches per hour,
  plus a this-week-vs-last-week table.
- **Offline time:** log time away from the PC (lecture, reading, study group) with a
  category. It counts in categories and totals, only for the parts when the PC wasn't
  in use (never double counted), and never as PC focus or switching.
- **Export** (button next to the date picker): CSV of every activity stretch, daily
  totals, or searches for the selected range, and *Print / save as PDF* for a report.
- **Email reports:** see below.

What counts as productive is the `"productive"` list in `categories.json`.

## Fixing categories from the dashboard

- **Fix categories** card: everything with no rule yet, and Unsure pages/videos, each with
  a category picker. For a YouTube video you choose "All from <channel>" (a `channels`
  rule) or "Just this one" (a `pages` rule for that URL).
- **Change categories** links on *Top apps and sites* (whole app/site rule; sites can stay
  "By page content") and *Pages read* (that page only).
- **Add a rule** form for sites, apps and channels you want set up front.
- Every change is written into `categories.json` in place (your layout and line endings
  are kept), checked by loading it exactly as the categoriser does, and all history is
  recategorised at once. **Undo last change** reverts it byte for byte; it refuses if
  you edited the file by hand since, so it never throws your own edits away. History of
  dashboard changes: `data\rules_history.json`.
- **AI suggestions** (Groq): sites and apps with no rule get a suggested category with a
  short reason. *Accept* writes the rule (undoable); *Reject* hides it for good.
- Pages Groq decided show an **AI** badge in *Pages read* (hover for its reason).
- Rule order: page rule > site rule > adult site (→ Entertainment) > channel > keywords >
  Voyage > Groq judge > fallback.

## Search what you've read

The **Search what you've read** card searches every scanned page (title, channel,
description, headings, text), pages you visited but that were never scanned (by tab
title), and your past searches.

- **Words:** SQLite full-text index (`pages_fts`, prefix matches, accents ignored, filler
  words skipped). Offline.
- **Meaning** (checkbox, needs the Voyage key): your search text is embedded and compared
  with the page vectors the AI categoriser already stored. Only the words you type are
  sent to Voyage. Calibrated so unrelated pages (similarity ~0.35) don't show.
- Results are merged (rank fusion), with time spent, last visit and how far you read.
- Page text is purged after 60 days; after that a page is still found by title/channel.

## Focus mode

Start a session from the dashboard's **Focus mode** card or the extension popup (25 / 50 /
90 min or any length 5-240). While it runs, the sites on the focus list open a "blocked"
page in Brave showing the time left, with *Back to work* and a two-click *End early*.

- Default list: every site your `categories.json` marks Entertainment or Social, plus
  reddit.com and 9gag.com. YouTube isn't included (you study there); edit the list under
  *Blocked sites during focus* (subdomains included).
- Blocking happens in the network layer when the extension has all-sites access (the
  page never loads), with a tab check as fallback.
- Starting from the popup blocks instantly; from the dashboard within ~30 s (the
  extension's next heartbeat). A Windows notification says when a session finishes.
- Each session is scored afterwards: **on task** = share of the session spent in your
  productive categories, plus which blocked sites you tried to open. Shown on the card
  (last 14 days) and in the 3-day email.
- If the desktop tracker isn't running, *End early* still works (never locks you out).
- Desktop apps aren't blocked, only sites in the browser.
- Needs extension **v0.6.0**: reload it in `brave://extensions` (it asks for the new
  "block content" and notifications permissions).

### Distraction alerts

After 20 minutes in a row of Entertainment or Social (settings under *Distraction alerts*
on the Focus mode card), you get a notification: "25 min on youtube.com and instagram.com"
with **Start 25-min focus** and **Snooze 30 min** buttons. It repeats every 20 minutes while
it carries on.

- Detours under 1½ minutes and pauses under 3 minutes don't reset the count; 1½ minutes of
  anything else does.
- No alerts during a focus session or while snoozed (*Snooze now* on the dashboard too).
- Checked every 30 s by the desktop app (`desktop/nudge.py`), shown by the extension
  (needs **v0.6.3**). If Brave isn't open (a game, Spotify), a plain Windows notification
  without buttons appears instead.

## Email reports (Gmail)

Set up on the dashboard's **Email reports** card: your Gmail address, where to send,
and a Google **App Password** (16 letters; needs 2-Step Verification; create it at
myaccount.google.com/apppasswords, revoke it there any time). It's saved to
`data\gmail_key.txt` (gitignored), never shown again.

- **Daily recap** at a time you choose (off by default): today's totals, categories,
  focus, top apps/sites and topics.
- **AI recap every N days** (on by default, every 3 days at 21:00): a Groq summary of
  those days, numbers vs the N days before, categories and focus sessions.
- **Weekly AI summary** (off by default): emailed once each Monday's summary is written.
- **Send a test email** sends today's recap right away.

Emails never contain page text, page titles, URLs or your searches.
`python desktop\mailer.py --test` sends a test from the command line. Focus
streaks tolerate interruptions up to 2 minutes (`FOCUS_GAP` in `desktop/analytics.py`).
The dashboard loads nothing from the internet, and other websites can't read its data.

## AI (step 5)

Two optional API keys, each in its own file in `data\` (gitignored) or an env var:

| Key | File | Used for | What leaves the PC |
|---|---|---|---|
| Voyage | `data\voyage_key.txt` (`VOYAGE_API_KEY`) | Categorising pages the rules can't decide | Each scanned page's title, channel, description, headings and first ~1,500 characters, once per page |
| Groq | `data\groq_key.txt` (`GROQ_API_KEY`) | Weekly summary | Totals only: hours per category/day, focus stats, top app/site names, topic words. Never page text, titles, URLs or searches |
| Groq | same key | AI judge: Unsure pages, sites/apps with no rule | Unsure pages: site, tab/page title, channel, description, search words and the first 500 characters of text. Unruled sites/apps: the name and up to 3 titles. Once per item |

- **AI categoriser** (`desktop/ai_categorize.py`): runs in the tracker's background every
  2 minutes, for pages and for search queries (each distinct query once; keywords still win). Compares each page to the example `descriptions` per category in
  `categories.json`. Rules and keywords always win; the AI only fills in pages they
  can't decide. Each page is embedded once and cached; editing `descriptions`
  re-categorises without re-sending pages.
- **AI judge** (`desktop/ai_judge.py`): runs right after the categoriser, only on what it
  left undecided. Pages on mixed sites (YouTube, claude.ai, Google, ...) that are still
  Unsure get Groq's category automatically when it's at least 70% sure. Sites/apps
  without a rule only get a *suggestion* on the Fix categories card. Each item is asked
  once (again only if its text or the category list changes); at most 3 requests every
  2 minutes, pausing 10 minutes after a rate limit. Never sent: the never-read sites
  (WhatsApp, banks, password managers, see `extension/blocklist.js`), password-manager
  and banking apps, and adult sites (categorised Entertainment locally).
- **Weekly summary** (`desktop/weekly.py`): written automatically each Monday for the
  previous Mon-Sun, or on demand with the dashboard button. The dashboard shows
  exactly what gets sent (*What gets sent to Groq*). Command line:
  `python desktop\weekly.py --preview` / `python desktop\weekly.py`.

## Categories

Rules live in `categories.json` at the project root. Categories are worked out when
data is read, so **editing the rules re-categorises all past data** - just re-run the report.

How something gets its category:

1. **Desktop app** -> `apps` rule (e.g. `Code.exe` -> Coding).
2. **Browser in front** -> the time is split by the tabs the extension saw, then per site:
   - `sites` rule (covers subdomains; most specific wins), or
   - `"content"` sites (YouTube, Reddit, Google, Claude...) -> `channels` rule, else
     `keywords` scored over the scanned page (title counts most); no clear winner = **Unsure**.
3. Nothing matched -> keywords on the window/tab title, else `Other` (apps) / `Browsing` (sites).

The report lists **Not categorised yet** and **Unsure** items, the things worth adding
rules for; the dashboard's **Fix categories** card lets you set them with one click.
`"pages"` holds per-URL overrides set from the dashboard.

## Tests

```powershell
python -m unittest discover -s tests -v   # categories, searches, dashboard, AI, backups
node tests\test_search.js                 # search detection from URLs
node tests\test_blocklist.js              # which sites' content is never read
```

## Settings

| Where                      | Setting          | Default | Meaning                              |
|----------------------------|------------------|---------|--------------------------------------|
| `desktop/tracker.py`       | `INTERVAL`       | 5 s     | how often the foreground is sampled  |
| `desktop/tracker.py`       | `IDLE_THRESHOLD` | 120 s   | no input this long = AFK             |
| `extension/background.js`  | `IDLE_SECONDS`   | 120 s   | same, for the browser                |

If the browser's active tab is playing audio, neither side counts you as idle.

## Known limitations

- YouTube page selectors can break when YouTube changes its layout; scans then
  fall back to the tab title.
- Some Store apps show up as `ApplicationFrameHost.exe`.
