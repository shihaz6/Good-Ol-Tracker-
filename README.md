# Good ol Tracker

Local-only activity tracker. Nothing leaves this PC.

- **Step 1 - desktop logger** (`desktop/`): active app + window title, idle detection, SQLite.
- **Step 2 - browser extension** (`extension/`): per-site tab time, media-aware idle,
  and page scanning on a small allowlist. Talks to a server inside the desktop tracker
  on `127.0.0.1:47321`.

- **Step 3 - categories** (`categories.json`, `desktop/categorize.py`): Coding, Study,
  Work, ... by app, site, YouTube channel and page keywords.

No dependencies: Python stdlib only.

## Run

```powershell
python desktop\tracker.py -v      # watch it log window changes; Ctrl+C to stop
python desktop\report.py          # today's summary
python desktop\report.py 2026-11-07
```

Data lives in `data\`: `tracker.db` (SQLite), `tracker.log`, `token.txt` (extension key).

## Background / at login

- Autostart is set up via `Good ol Tracker.lnk` in the Startup folder
  (Win+R -> `shell:startup`). Delete that shortcut to disable it.
- Run hidden manually: `pythonw desktop\tracker.py`
- Stop it: Task Manager -> end the `pythonw.exe` process.
- After changing the Python code, stop and restart it so the changes load.

Only one tracker runs at a time; extra launches exit immediately.

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
python -m unittest discover -s tests -v   # categories, splitting, searches table
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
