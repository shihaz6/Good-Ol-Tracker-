// Good ol Tracker - browser extension service worker.
//
// 1. Time tracking: one "visit" per stretch on the same active tab + URL, paused when
//    the browser loses focus, the screen locks, or you go idle (unless the tab plays media).
// 2. Page scanning: ONLY on the sites in SCAN_RULES (the manifest grants no other
//    page access). Pages with a password field are never scanned.
// 3. Everything is queued in chrome.storage.local and sent to the desktop app on
//    127.0.0.1; if it isn't running, the queue waits and retries.

const SERVER = "http://127.0.0.1:47321";
const IDLE_SECONDS = 120;       // matches the desktop tracker
const STALE_SECONDS = 90;       // visit with no heartbeat for this long ended at lastSeen
const MIN_VISIT_SECONDS = 1;    // ignore tab flicks shorter than this
const MAX_QUEUE = 5000;         // per list; oldest dropped beyond this
const SCAN_DELAY_MS = 2500;     // let single-page apps (YouTube) finish rendering

// Sites whose content decides the category. Hosts must also be in manifest host_permissions.
const SCAN_RULES = [
  { kind: "youtube", test: (u) => u.hostname === "www.youtube.com" && u.pathname === "/watch" },
  {
    kind: "article",
    test: (u) => (u.hostname === "medium.com" || u.hostname.endsWith(".medium.com")) &&
      u.pathname.split("/").filter(Boolean).length >= 2,
  },
  { kind: "article", test: (u) => u.hostname === "www.reddit.com" && /^\/r\/[^/]+\/comments\//.test(u.pathname) },
];

chrome.idle.setDetectionInterval(IDLE_SECONDS);

// ---------------------------------------------------------------- utilities

const now = () => Date.now() / 1000;

function parseUrl(url) {
  try { return new URL(url); } catch { return null; }
}

function isTrackable(tab) {
  return tab && !tab.incognito && /^https?:\/\//.test(tab.url || "");
}

// Serialize all state changes: events fire concurrently, storage reads/writes are async.
let chain = Promise.resolve();
function serial(fn) {
  const run = chain.then(fn, fn);
  chain = run.catch((err) => console.warn("tracker:", err));
  return run;
}

async function getLocal(key, fallback) {
  const result = await chrome.storage.local.get(key);
  return result[key] ?? fallback;
}

async function enqueue(list, item) {
  const queue = await getLocal("queue", { visits: [], pages: [] });
  queue[list].push(item);
  if (queue[list].length > MAX_QUEUE) queue[list].splice(0, queue[list].length - MAX_QUEUE);
  await chrome.storage.local.set({ queue });
}

// ---------------------------------------------------------------- time tracking

// The tab the user is actually looking at right now, or null.
async function viewedTab() {
  const win = await chrome.windows.getLastFocused().catch(() => null);
  if (!win || !win.focused) return null;
  const [tab] = await chrome.tabs.query({ active: true, windowId: win.id });
  if (!isTrackable(tab)) return null;
  const idle = await chrome.idle.queryState(IDLE_SECONDS);
  if (idle === "locked") return null;
  if (idle === "idle" && !tab.audible) return null; // watching a video isn't AFK
  return tab;
}

async function closeVisit(visit, end) {
  if (end - visit.start >= MIN_VISIT_SECONDS) {
    await enqueue("visits", { id: visit.id, start: visit.start, end, url: visit.url, title: visit.title });
  }
}

// Bring the stored "current visit" in line with what's on screen.
async function refresh() {
  const t = now();
  const tab = await viewedTab();
  let cur = await getLocal("current", null);

  // Service worker / browser was asleep or closed: end the old visit when last seen.
  if (cur && t - cur.lastSeen > STALE_SECONDS) {
    await closeVisit(cur, cur.lastSeen);
    cur = null;
  }

  if (cur && tab && cur.tabId === tab.id && cur.url === tab.url) {
    cur.lastSeen = t;
    if (tab.title) cur.title = tab.title; // titles often arrive after the URL
    await chrome.storage.local.set({ current: cur });
    return tab;
  }

  if (cur) await closeVisit(cur, t);
  if (tab) {
    await chrome.storage.local.set({
      current: { id: crypto.randomUUID(), tabId: tab.id, url: tab.url, title: tab.title || "", start: t, lastSeen: t },
    });
    await recordSearch(tab.url);
  } else {
    await chrome.storage.local.remove("current");
  }
  return tab;
}

// ---------------------------------------------------------------- sending

async function flush(mediaPlaying) {
  const token = await getLocal("token", "");
  if (!token) return;
  const queue = await getLocal("queue", { visits: [], pages: [] });
  const visits = queue.visits.slice(0, 500);
  const pages = queue.pages.slice(0, 100);

  // Also send the visit still in progress, so the desktop DB shows the current site live
  // (the server updates it in place by id). Final version is queued when it closes.
  const cur = await getLocal("current", null);
  const live = cur && cur.lastSeen - cur.start >= MIN_VISIT_SECONDS
    ? [{ id: cur.id, start: cur.start, end: cur.lastSeen, url: cur.url, title: cur.title }]
    : [];
  if (!visits.length && !pages.length && !live.length && !mediaPlaying) return;

  let res;
  try {
    res = await fetch(`${SERVER}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Tracker-Token": token },
      body: JSON.stringify({ visits: [...visits, ...live], pages, media: !!mediaPlaying }),
      signal: AbortSignal.timeout(5000),
    });
  } catch {
    return; // desktop app not running; keep the queue
  }
  await chrome.storage.local.set({ lastStatus: { ok: res.ok, code: res.status, at: Date.now() } });
  // 400/413 = the server can never accept this batch; drop it so it can't block the queue.
  // Anything else (401 wrong token, 5xx) = keep it and retry later.
  if (!res.ok && res.status !== 400 && res.status !== 413) return;

  // Only events in serial() touch the queue, so nothing was added meanwhile.
  queue.visits.splice(0, visits.length);
  queue.pages.splice(0, pages.length);
  await chrome.storage.local.set({ queue });
}

async function tick() {
  const tab = await refresh();
  await flush(tab?.audible);
}

// ---------------------------------------------------------------- page scanning

async function recordSearch(url) {
  const u = parseUrl(url);
  if (!u || !/(^|\.)google\.[a-z.]+$/.test(u.hostname) || u.pathname !== "/search") return;
  const q = u.searchParams.get("q");
  if (q) await enqueue("pages", { url, kind: "search", ts: now(), title: q.slice(0, 300) });
}

function scanRule(url) {
  const u = parseUrl(url);
  return u ? SCAN_RULES.find((r) => r.test(u)) : undefined;
}

// Runs inside the page. Must be self-contained (no outer variables).
function scanPage(kind) {
  if (document.querySelector('input[type="password"]')) return null;
  const clean = (s, n) => (s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const text = (sel, root = document) => root.querySelector(sel)?.innerText;
  const meta = (name) => document.querySelector(`meta[name="${name}"], meta[property="${name}"]`)?.content;

  if (kind === "youtube") {
    return {
      href: location.href,
      title: clean(text("ytd-watch-metadata h1") || document.title.replace(/ - YouTube$/, ""), 300),
      author: clean(text("ytd-watch-metadata ytd-channel-name a") || text("#owner #channel-name a"), 200),
      description: clean(text("ytd-watch-metadata #description-inline-expander") || text("#description"), 2000),
    };
  }
  const root = document.querySelector("article") || document.querySelector("main") || document.body;
  const headings = [...root.querySelectorAll("h1, h2, h3")]
    .map((h) => clean(h.innerText, 150)).filter(Boolean).slice(0, 20);
  return {
    href: location.href,
    title: clean(text("h1", root) || meta("og:title") || document.title, 300),
    author: clean(meta("author"), 200),
    description: clean(meta("description") || meta("og:description"), 1000),
    headings: headings.join("\n"),
    body: clean(root.innerText, 3000),
  };
}

const scanTimers = new Map(); // tabId -> timeout; in-memory is fine for a 2.5s delay
const scannedUrls = new Set(); // avoid rescanning the same page within a worker lifetime

function scheduleScan(tab) {
  const rule = scanRule(tab.url);
  if (!rule || tab.incognito || scannedUrls.has(tab.url)) return;
  clearTimeout(scanTimers.get(tab.id));
  scanTimers.set(tab.id, setTimeout(() => serial(() => scan(tab.id, tab.url, rule.kind)), SCAN_DELAY_MS));
}

async function scan(tabId, url, kind) {
  scanTimers.delete(tabId);
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab || tab.url !== url || !tab.active) return; // navigated away or switched tab
  let result;
  try {
    [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, func: scanPage, args: [kind] });
  } catch (err) {
    console.warn("tracker: scan failed", err);
    return;
  }
  if (!result || result.href !== url) return; // password page, or the SPA moved on
  const { href, ...fields } = result;
  scannedUrls.add(url);
  await enqueue("pages", { url, kind, ts: now(), ...fields });
}

// ---------------------------------------------------------------- events

chrome.tabs.onActivated.addListener(() => serial(tick));
chrome.windows.onFocusChanged.addListener(() => serial(tick));
chrome.idle.onStateChanged.addListener(() => serial(tick));

chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (!tab.active) return;
  if (change.url || change.title || change.audible !== undefined || change.status === "complete") {
    serial(tick);
  }
  // Title changes are the reliable "page finished updating" signal on single-page sites.
  if (change.title || change.status === "complete") scheduleScan(tab);
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "heartbeat") serial(tick);
});

async function init() {
  await chrome.alarms.create("heartbeat", { periodInMinutes: 0.5 });
  serial(tick);
}

chrome.runtime.onStartup.addListener(init);
chrome.runtime.onInstalled.addListener(async () => {
  await init();
  if (!(await getLocal("token", ""))) chrome.runtime.openOptionsPage();
});
