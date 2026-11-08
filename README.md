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

Page content is read **only** on YouTube videos, Medium articles and Reddit threads
(the extension has no permission to read any other site), and never on pages with a
password field. Google searches store just the search text from the URL. Incognito
is never tracked. To scan another site, add it to `SCAN_RULES` in
`extension/background.js` **and** to `host_permissions` in `extension/manifest.json`.

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
rules for. Tests: `python -m unittest discover -s tests -v`

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
