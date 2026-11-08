# Decisions

The choices that shaped Good ol Tracker, why they were made, and what they cost. Grouped
by how much of the system they affect:

- **Major:** shapes the whole architecture or the privacy model; expensive to reverse.
- **Medium:** shapes one feature or subsystem.
- **Minor:** a local default, threshold or UX choice; easy to change.

Each entry: **Decision** · *Why* · *Alternatives* · *Consequences*. Settings that are
easy to change are named so you can find them. See [architecture.md](architecture.md) for
how the pieces fit.

---

## Major decisions

### M1. Local-first, single owner of the data
**Decision:** One desktop process owns one SQLite database on the PC. The extension and the
phone are clients that only send observations and receive state.
- *Why:* The data is very personal (every window title, page and app). Keeping it on your own
  machine makes privacy the default. One writer also avoids sync conflicts.
- *Alternatives:* a cloud backend; ActivityWatch-style per-device buckets.
- *Consequences:* The PC must be on for the dashboard, and to collect the phone's data
  (the phone catches up later). There's no access from elsewhere. Backups are local (D15).

### M2. Python standard library only, no build step
**Decision:** The desktop side is pure stdlib Python (ctypes for Win32, `http.server`,
`sqlite3`, `urllib`, `smtplib`). The dashboard is plain HTML/CSS/JS with no frameworks or CDNs.
- *Why:* Python 3.15 lacked wheels for many AI packages. The laptop has 8 GB of RAM. And
  nothing breaks when a dependency changes.
- *Alternatives:* Flask/FastAPI, pandas, a React dashboard, Electron.
- *Consequences:* More hand-written code: the HTTP routing, SVG charts and the CSV writer
  are all ours. Install is just "have Python".

### M3. Store raw facts, compute meaning at read time
**Decision:** `sessions` stores no categories. Categories, focus streaks, goals, subjects,
sleep and limits are computed by `categorize.categorise()` and the modules above it on
every read.
- *Why:* Editing a rule, adding keywords or adding a subject fixes all history at once,
  with no migrations or re-runs.
- *Alternatives:* store a category column and re-categorise in batches.
- *Consequences:* Every read re-categorises its range. That's fine at personal scale, and a
  few hot paths cache for 20–120 s. The AI results that *are* stored (Voyage vectors, Groq
  answers) are caches keyed by what was sent, never the source of truth.

### M4. Desktop time is the truth; browser and phone refine it
**Decision:** Desktop foreground sessions define the PC's time. Browser data only splits
`brave.exe` time by site. Phone time replaces any PC time it overlaps.
- *Why:* One clock means no double counting. When you're on the phone, the PC goes idle
  within 2 minutes anyway (unless a video is playing), so overlapping PC time is unattended.
- *Alternatives:* add up the devices; trust the browser's own clock.
- *Consequences:* Browser time without the extension falls back to the window title.
  Manual offline time only fills the gaps where the PC was unused.

### M5. Rules first, AI only for what rules can't decide
**Decision:** Order: your rules → keywords → Voyage similarity → Groq judge → fallback. AI
answers are cached and never override your rules.
- *Why:* Rules are predictable, free and offline. AI is good at the long tail: mixed sites
  like YouTube, claude.ai and Google, and vague titles.
- *Alternatives:* AI for everything; rules only.
- *Consequences:* Accuracy improves over time as you accept suggestions. The categoriser
  stays deterministic for anything you've decided.

### M6. Hosted AI APIs instead of local models
**Decision:** Voyage for embeddings, Groq for LLM work. No Ollama or local models.
- *Why:* You chose this: the 8 GB laptop shouldn't carry a model, and the AI parts are
  light, occasional work. Both APIs have free tiers that cover personal use.
- *Alternatives:* Ollama with a small model; no AI.
- *Consequences:* Some data leaves the PC (M7), and AI features need keys and internet. Both
  are optional, and everything fails soft without them.

### M7. The privacy line for AI
**Decision:**
- **Weekly/periodic summaries:** totals and names only. Never page text, titles, URLs or
  searches.
- **Category judging (your "option B"):** titles plus the first 500 characters of a page.
- **Never sent at all:** never-read sites (WhatsApp, banks, password managers), banking and
  password-manager apps, adult sites, private windows.

- *Why:* The judge needs some page text to be useful. The summary doesn't, so it gets the
  least.
- *Alternatives:* titles only for the judge (option A); send everything.
- *Consequences:* Every outbound payload is documented in the README, and the dashboard
  shows exactly what the summary sends. `sitelists.py` reads the extension's own lists so
  the two sides never drift.

### M8. Page content on all sites, with a never-read list
**Decision:** The extension may read page content (up to 3,000 characters, never typed
text) on every site once you allow it. The fixed exclusions are WhatsApp, banks, password
managers, password/card pages and private windows. Page text is deleted after 60 days.
- *Why:* You wanted all sites. Page content makes categories, search-what-you've-read and
  subjects far better than titles alone.
- *Alternatives:* a small allowlist (the original design); titles only.
- *Consequences:* The database holds sensitive text for 60 days. The block list is editable,
  and any hostname containing "bank" is always skipped.

### M9. Phone via a companion app that re-reads Android's usage history
**Decision:** A small Android app reads `UsageStatsManager` (the Digital Wellbeing data) and
syncs it to the PC over the local network. It keeps a cursor and re-reads, rather than
streaming events.
- *Why:* Android keeps about a week of usage history. Re-reading from a confirmed cursor
  makes sync idempotent and loses nothing while the PC is off. No cloud is needed.
- *Alternatives:* a cloud relay; Tasker-style automation; ActivityWatch for Android; iOS
  (no Screen Time API).
- *Consequences:* Android only. The PC and phone must share a network now and then (the PC
  usually runs on the phone's hotspot, which makes this easy). Xiaomi's background
  restrictions need user steps.

### M10. Phone networking: private networks, pairing, proof of identity
**Decision:**
- **Phone port:** a separate server on `0.0.0.0:47323`, with only `/phone/*` routes.
- **Who can reach it:** private client addresses only, and the firewall opens it for
  Private networks only.
- **Pairing:** a 4-digit code that you approve on the dashboard, which issues a per-phone
  token.
- **Discovery:** UDP broadcast, where the PC's answer proves it knows the token
  (HMAC of a nonce).

- *Why:* The PC's address on a hotspot changes on every connection, so the phone has to
  discover it. That must not open the tracker to café or campus networks, or let another PC
  impersonate yours.
- *Alternatives:* a fixed IP in a QR code; a cloud relay; Tailscale.
- *Consequences:* You must run `phone_firewall.ps1` and set the network to Private once.
  The dashboard API itself stays on `127.0.0.1`.

### M11. Self-control tools, never locks
**Decision:** Every block can be undone by you:
- *End early* works even when the PC is down;
- every block carries an `until` and lapses on its own if contact is lost;
- you get one "5 more minutes" per night or day.

- *Why:* A tracker that locks you out (or blocks forever after a crash) gets uninstalled.
  The goal is friction at the moment of temptation, not enforcement.
- *Alternatives:* hard locks, device admin, uninstall protection.
- *Consequences:* A determined you can get around it: disable the extension, use another
  browser, switch off Accessibility. The README says so.

### M12. Rules are decided centrally and enforced by each client
**Decision:** Focus, limits, bedtime, schedules and alerts are decided by the desktop
process and pushed to clients in the replies they already get (`/ingest` every 30 s,
`/phone/sync` every minute). The phone keeps a copy to enforce offline.
- *Why:* One source of truth, no extra connections, and "the same rules on both devices".
- *Alternatives:* each client with its own settings; push notifications.
- *Consequences:* Up to 30–60 s of delay. Limits are one shared budget across devices (D8).

---

## Medium decisions

### D1. Brave extension on Manifest V3, with network-level blocking
**Decision:** Blocking uses `declarativeNetRequest` dynamic redirect rules (the page never
loads), with a tab-URL check as a fallback.
- *Why:* MV3 is the only option going forward, and network rules are instant and reliable.
- *Consequences:* Needs all-sites permission for the network layer. Rule id ranges per
  reason: adult 1–3, focus 1000+, bedtime 3000+, limits 4000+.

### D2. Local HTTP API secured for a browser world
**Decision:** The dashboard API checks Host (against DNS rebinding), Origin and
`Sec-Fetch-Site`; POSTs must be strictly same-origin. Extension routes need a token from
`data/token.txt` and an extension origin.
- *Why:* Any website you visit could otherwise call `127.0.0.1`.
- *Consequences:* The extension needs a one-time token paste.

### D3. Editing `categories.json` in place, with a guarded undo
**Decision:** Dashboard edits change only the affected lines, keep your layout and line
endings, validate the result by loading it as the categoriser would, then replace the file
atomically. Undo is refused if the file changed by hand since (checked by hash).
- *Why:* The rules file is meant to be hand-edited too. The dashboard must never mangle or
  overwrite your edits.
- *Alternatives:* move rules into the database.

### D4. The Groq judge applies to pages, but only suggests for sites and apps
**Decision:** *Unsure* pages get Groq's category automatically when it's ≥ 70% sure, shown
with an **AI** badge. Whole sites, apps and phone apps only get Accept/Reject suggestions.
- *Why:* A wrong page label affects one page. A wrong site rule would re-categorise all of
  its history. You chose suggestions for apps.

### D5. Search what you've read: FTS5 plus meaning, merged by rank
**Decision:** SQLite FTS5 (prefix matching, stop words removed) plus Voyage similarity
against the page vectors already stored, merged with reciprocal rank fusion. Meaning
matches need similarity ≥ 0.42 and must be within 0.10 of the best match.
- *Why:* Words find exact things and meaning finds "that article about…". The thresholds
  were calibrated on real data, where unrelated pages scored about 0.35.

### D6. Distraction alerts go to the device in use
**Decision:** The streak counts Entertainment/Social time. Detours under 90 s and pauses
under 3 min don't reset it. The alert goes to the phone if the streak ended there, otherwise
to the extension. Uncollected alerts move to the PC after 150 s, then to a plain Windows
toast after 75 s.
- *Why:* An alert on the laptop is useless while you're scrolling on the phone.

### D7. Bedtime and the adult filter on the phone without reading the screen
**Decision:**
- **App blocking:** an Accessibility service that only looks at *which app* is in front
  (`canRetrieveWindowContent=false`).
- **Adult content:** Android Private DNS with a family filter (`family.cloudflare-dns.com`)
  rather than inspecting browsers.

- *Why:* It keeps the promise that the app never reads what's on screen. DNS covers every
  browser and app, and keeps working when the app is closed.
- *Consequences:* The user must turn on Private DNS, and an app can't do it for you.
  Browsers with their own DNS-over-HTTPS provider bypass it. The app warns when the filter
  is off.

### D8. Daily limits are one shared budget, and study time doesn't count
**Decision:** A limit on an app (e.g. Facebook 60 min) counts the phone app plus its
website on the PC (`wellbeing.APP_SITES`). Only non-productive site time counts.
- *Why:* Otherwise the laptop is the obvious way round the phone limit. Excluding study
  time means a YouTube limit won't eat your lecture videos.

### D9. Sleep is estimated, not measured
**Decision:** Sleep = the longest stretch with no phone use, no unlock and no PC use
between 18:00 and 15:00, if it's at least 3 h. "Sleep vs productivity" only uses days the
PC was actually tracked.
- *Why:* There's no wearable. Phone and PC silence is a good proxy for a phone-heavy user.
- *Consequences:* Lying awake without the phone counts as sleep. It's labelled "estimated".

### D10. Subjects by keywords first, Groq for the misses
**Decision:** You define modules (name, keywords, exam date, weekly target). Productive
time is tagged by the best keyword match (ties go to nobody). Unmatched study pages are
sent to Groq once and cached by a hash of the subject list.
- *Why:* Module codes and project names are precise and free. Groq catches "Third Normal
  Form" belongs to DBMS.

### D11. Goals only count days the device was tracked
**Decision:** A "phone under 2h" day before the phone was paired, or a "productive 3h+" day
before the PC tracker was installed, is *not tracked*, not *missed*. "Max" goals are only
won when the day is over.
- *Why:* Streaks and "met 0 of 6 days" must be honest from day one.

### D12. Email via Gmail SMTP with an App Password; a 3-day recap by default
**Decision:**
- **Delivery:** reports go out through your own Gmail (`smtplib` + App Password).
- **Default schedule:** an AI recap every 3 days at 21:00. The Monday weekly email is off.

- *Why:* No email service to sign up for, no dependency. You preferred every 3 days to
  weekly.
- *Consequences:* Needs 2-Step Verification for the App Password. It's revocable at any time.

### D13. The AI coach is told what exists
**Decision:**
- **Grounding:** the weekly prompt lists the tracker's real settings and forbids inventing
  others.
- **Tracking info:** it receives how many days each device was tracked.
- **No false comparisons:** it gets no "previous period" when that period was barely tracked.

- *Why:* The first runs suggested a non-existent "App Pause" and compared against a
  pre-install week of 0.35 h.

### D14. Android app built offline from the local Gradle cache
**Decision:** It uses the same AGP / Kotlin / Compose versions as another project already
on the machine. Lifecycle libraries are pinned to cached versions, and the empty
`kotlin-stdlib-jdk7/8` artifacts are excluded.
- *Why:* No Android Studio and no big downloads on the 8 GB laptop.
- *Consequences:* Upgrading libraries means going online once.

### D15. Operations: scheduled task with watchdog, daily zipped backups
**Decision:** A per-user scheduled task starts the tracker at login and every 5 min
(single-instance lock, so extra launches exit). Daily online backups (`sqlite3.backup`)
are zipped, and the newest 14 are kept.
- *Why:* No admin rights or Windows service needed, it survives crashes, and backups are
  consistent even with WAL.

---

## Minor decisions

| # | Decision | Why |
|---|---|---|
| m1 | Sample every **5 s**; **2 min** without input = away (unless media is playing) | Fine enough for app switches, light on CPU; videos aren't counted as idle |
| m2 | Extension heartbeat every **30 s**, with an offline queue | Live dashboard and "extension alive" health without many requests |
| m3 | Page text retention **60 days** (`PAGE_TEXT_DAYS`) | Your choice: useful for search and revision, bounded storage |
| m4 | Focus streak tolerates **2 min** gaps (`FOCUS_GAP`); deep focus = **25 min** | A quick message shouldn't break a streak |
| m5 | Focus block list defaults to Entertainment/Social sites + reddit/9gag; **YouTube excluded** | You study on YouTube |
| m6 | Adult filter in Brave: **during focus** by default, or always, or off | Asked for after NSFW got through focus mode; "always" for stronger self-control |
| m7 | Adult sites are categorised **Entertainment** locally and never sent to AI | Privacy and fewer API calls |
| m8 | Default limits **Facebook 60 min, Instagram 30 min**; bedtime **23:30–06:00**; goals **3h productive, phone under 2h** | Your examples, adjustable on the dashboard |
| m9 | One **"5 more minutes"** per night (bedtime) / per app per day (limit), per device | A release valve without making the limit meaningless |
| m10 | Groq judge: confidence ≥ **0.7**, ≤ **3** requests per 2 min, **10 min** back-off | Stays well inside free-tier limits; low-confidence answers stay Unsure |
| m11 | Phone: sync every **1 min** with the screen on (Accessibility running), **15 min** otherwise | Fast enough for focus and alerts; no battery cost while the screen is off |
| m12 | Phone ignores launcher, System UI, **Clock, App vault, share sheet** | They sit in front without real use (Clock added 5.9 h of "use") |
| m13 | Pickups over **15 min** between study stretches count as **breaks**, not interruptions | An hour of Facebook isn't a "glance" |
| m14 | Windows system pop-ups (ShellHost, CredentialUIBroker…) → **Other** | Removes noise from "No rule yet" |
| m15 | Dashboard and phone use one **"calm focus" palette**: ink surfaces, blue = focus, mint = done, amber = heads-up, coral = over; fixed, not wallpaper colours | Colour means the same thing everywhere; the dashboard's categorical colours were checked for colour-blind safety |
| m16 | Charts always have a **Show as table** view; one axis per chart | Accessibility and readability |
| m17 | Phone Week tab averages only over **tracked days** | A week with 1 PC day shouldn't show "28m productive a day" |
| m18 | Searches are read from **URLs**, not pages; repeats within 10 min are ignored | Works on any site without page access; paging isn't a new search |
| m19 | Rule order puts **page rule > site rule** | "This one video is Study" must beat "YouTube is by content" |
| m20 | Version bumps on every extension/app change (extension 0.7.0, app 0.3.0) | The health strip and phone card show which version is actually running |
