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

What counts as productive is the `"productive"` list in `categories.json`. Focus
streaks tolerate interruptions up to 2 minutes (`FOCUS_GAP` in `desktop/analytics.py`).
The dashboard loads nothing from the internet, and other websites can't read its data.

## AI (step 5)

Two optional API keys, each in its own file in `data\` (gitignored) or an env var:

| Key | File | Used for | What leaves the PC |
|---|---|---|---|
| Voyage | `data\voyage_key.txt` (`VOYAGE_API_KEY`) | Categorising pages the rules can't decide | Each scanned page's title, channel, description, headings and first ~1,500 characters, once per page |
| Groq | `data\groq_key.txt` (`GROQ_API_KEY`) | Weekly summary | Totals only: hours per category/day, focus stats, top app/site names, topic words. Never page text, titles, URLs or searches |

- **AI categoriser** (`desktop/ai_categorize.py`): runs in the tracker's background every
  2 minutes. Compares each page to the example `descriptions` per category in
  `categories.json`. Rules and keywords always win; the AI only fills in pages they
  can't decide. Each page is embedded once and cached; editing `descriptions`
  re-categorises without re-sending pages.
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
rules for.

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
