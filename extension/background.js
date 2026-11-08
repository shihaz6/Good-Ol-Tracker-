// Good ol Tracker - browser extension service worker.
//
// 1. Time tracking: one "visit" per stretch on the same active tab + URL, paused when
//    the browser loses focus, the screen locks, or you go idle (unless the tab plays media).
//    Each visit also records how far down the page you scrolled.
// 2. Searches: every search on any site, read from the URL (see search.js).
// 3. Page content: YouTube/Medium/Reddit always; every other site once you grant
//    "scan all sites" on the options page. Never on blocked sites (blocklist.js), pages
//    with password/card fields, or text you type.
// 4. Everything is queued in chrome.storage.local and sent to the desktop app on
//    127.0.0.1; if it isn't running, the queue waits and retries.
// The "Pause" switch stops all of the above.

importScripts("search.js", "blocklist.js");

const SERVER = "http://127.0.0.1:47321";
const IDLE_SECONDS = 120;       // matches the desktop tracker
const STALE_SECONDS = 90;       // visit with no heartbeat for this long ended at lastSeen
const MIN_VISIT_SECONDS = 1;    // ignore tab flicks shorter than this
const MAX_QUEUE = 5000;         // per list; oldest dropped beyond this
const SCAN_DELAY_MS = 2500;     // let single-page apps (YouTube) finish rendering
const SEARCH_REPEAT_SECONDS = 600; // same search again within this = paging/reload, not new
const ALL_SITES = { origins: ["http://*/*", "https://*/*"] };

// Always scanned (manifest host_permissions), with a dedicated extractor for YouTube.
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

const isPaused = () => getLocal("paused", false);
const getBlocklist = () => getLocal("blocklist", DEFAULT_BLOCKLIST);

let allSites = false;
let permissionReady = refreshPermission();
async function refreshPermission() {
  allSites = await chrome.permissions.contains(ALL_SITES);
}
chrome.permissions.onAdded.addListener(() => { permissionReady = refreshPermission(); });
chrome.permissions.onRemoved.addListener(() => { permissionReady = refreshPermission(); });

async function getQueue() {
  const queue = await getLocal("queue", {});
  return { visits: queue.visits || [], pages: queue.pages || [], searches: queue.searches || [] };
}

async function enqueue(list, item) {
  const queue = await getQueue();
  queue[list].push(item);
  if (queue[list].length > MAX_QUEUE) queue[list].splice(0, queue[list].length - MAX_QUEUE);
  await chrome.storage.local.set({ queue });
}

// ---------------------------------------------------------------- time tracking

// The tab the user is actually looking at right now, or null.
async function viewedTab() {
  if (await isPaused()) return null;
  const win = await chrome.windows.getLastFocused().catch(() => null);
  if (!win || !win.focused) return null;
  const [tab] = await chrome.tabs.query({ active: true, windowId: win.id });
  if (!isTrackable(tab)) return null;
  const idle = await chrome.idle.queryState(IDLE_SECONDS);
  if (idle === "locked") return null;
  if (idle === "idle" && !tab.audible) return null; // watching a video isn't AFK
  return tab;
}

const visitPayload = (v, end) =>
  ({ id: v.id, start: v.start, end, url: v.url, title: v.title, scroll: v.scroll ?? null });

async function closeVisit(visit, end) {
  if (end - visit.start >= MIN_VISIT_SECONDS) await enqueue("visits", visitPayload(visit, end));
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

// ---------------------------------------------------------------- searches

async function recordSearch(url) {
  const found = detectSearch(url);
  if (!found) return;
  const t = now();
  // Paging through results or reloading repeats the URL; count the search once.
  const recent = await getLocal("recentSearches", {});
  for (const [k, ts] of Object.entries(recent)) if (t - ts > SEARCH_REPEAT_SECONDS) delete recent[k];
  const key = `${found.engine}|${found.query.toLowerCase()}`;
  const repeat = key in recent;
  recent[key] = t;
  await chrome.storage.local.set({ recentSearches: recent });
  if (repeat) return;
  await enqueue("searches", { id: crypto.randomUUID(), ts: t, engine: found.engine, query: found.query, url });
}

// ---------------------------------------------------------------- sending

async function flush(mediaPlaying) {
  const token = await getLocal("token", "");
  if (!token) return;
  const queue = await getQueue();
  const visits = queue.visits.slice(0, 500);
  const pages = queue.pages.slice(0, 100);
  const searches = queue.searches.slice(0, 500);

  // Also send the visit still in progress, so the desktop DB shows the current site live
  // (the server updates it in place by id). Final version is queued when it closes.
  const cur = await getLocal("current", null);
  const live = cur && cur.lastSeen - cur.start >= MIN_VISIT_SECONDS ? [visitPayload(cur, cur.lastSeen)] : [];

  // Always sent, even with nothing queued: doubles as the dashboard's "extension is alive"
  // heartbeat and tells it whether page scanning is on.
  await permissionReady;
  const client = {
    version: chrome.runtime.getManifest().version,
    all_sites: allSites,
    paused: await isPaused(),
    queued: queue.visits.length + queue.pages.length + queue.searches.length,
  };

  let res;
  try {
    res = await fetch(`${SERVER}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Tracker-Token": token },
      body: JSON.stringify({ visits: [...visits, ...live], pages, searches, media: !!mediaPlaying, client }),
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
  queue.searches.splice(0, searches.length);
  await chrome.storage.local.set({ queue });
}

async function tick() {
  const tab = await refresh();
  await flush(tab?.audible);
}

// ---------------------------------------------------------------- page content

// What kind of scan a URL gets, or null if its content must not be read.
async function scanKind(url) {
  const u = parseUrl(url);
  if (!u || !/^https?:$/.test(u.protocol)) return null;
  if (isBlocked(u.hostname, await getBlocklist())) return null;
  const rule = SCAN_RULES.find((r) => r.test(u));
  if (rule) return rule.kind;
  await permissionReady;
  return allSites ? "page" : null;
}

// Runs inside the page. Must be self-contained (no outer variables).
function scanPage(kind) {
  if (document.querySelector('input[type="password"], input[autocomplete^="cc-"]')) return null;
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

  const root = document.querySelector("article") || document.querySelector("main, [role=main]") || document.body;
  if (!root) return null;
  // Work on a copy with everything that isn't "content you're reading" removed,
  // including anything you can type into (forms, chat boxes, editors).
  const copy = root.cloneNode(true);
  copy.querySelectorAll([
    "input", "textarea", "select", "[contenteditable]:not([contenteditable=false])", "[role=textbox]",
    "script", "style", "noscript", "template", "svg", "iframe",
    "nav", "footer", "aside", "[role=navigation]", "[role=dialog]", "[aria-hidden=true]", "[hidden]",
  ].join(",")).forEach((el) => el.remove());

  const headings = [...copy.querySelectorAll("h1, h2, h3")]
    .map((h) => clean(h.textContent, 150)).filter(Boolean).slice(0, 20);
  const parts = [];
  let length = 0;
  const walker = document.createTreeWalker(copy, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && length < 4000) {
    const t = walker.currentNode.nodeValue.trim();
    if (t) { parts.push(t); length += t.length + 1; }
  }
  return {
    href: location.href,
    title: clean(copy.querySelector("h1")?.textContent || meta("og:title") || document.title, 300),
    author: clean(meta("author"), 200),
    description: clean(meta("description") || meta("og:description"), 1000),
    headings: headings.join("\n"),
    body: clean(parts.join(" "), 3000),
  };
}

// Runs inside the page: reports the deepest scroll position (% of page) to the worker.
function installScrollTracker() {
  if (window.__goodOlScroll) return;
  window.__goodOlScroll = true;
  let href = location.href, max = 0, sent = 0, timer = null;
  const measure = () => {
    const height = Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0);
    if (height <= innerHeight + 1) return 100; // whole page fits on screen
    return Math.min(100, Math.round(((scrollY + innerHeight) / height) * 100));
  };
  const report = () => {
    timer = null;
    if (location.href !== href) { href = location.href; max = 0; sent = 0; } // single-page app navigation
    max = Math.max(max, measure());
    if (max >= sent + 5 || (max === 100 && sent < 100)) {
      sent = max;
      try { chrome.runtime.sendMessage({ type: "scroll", url: href, pct: max }).catch(() => {}); } catch {}
    }
  };
  addEventListener("scroll", () => { if (!timer) timer = setTimeout(report, 1000); }, { passive: true });
  report();
}

const scanTimers = new Map(); // tabId -> timeout; in-memory is fine for a 2.5s delay
const scannedUrls = new Set(); // avoid rescanning the same page within a worker lifetime

async function scheduleScan(tab) {
  if (tab.incognito) return;
  const kind = await scanKind(tab.url);
  if (!kind) return;
  clearTimeout(scanTimers.get(tab.id));
  scanTimers.set(tab.id, setTimeout(() => serial(() => scan(tab.id, tab.url, kind)), SCAN_DELAY_MS));
}

async function scan(tabId, url, kind) {
  scanTimers.delete(tabId);
  if (await isPaused()) return;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab || tab.url !== url || !tab.active) return; // navigated away or switched tab
  const target = { tabId };
  // Scroll tracking: installed once per page, harmless to call again.
  chrome.scripting.executeScript({ target, func: installScrollTracker }).catch(() => {});
  if (scannedUrls.has(url)) return;
  let result;
  try {
    [{ result }] = await chrome.scripting.executeScript({ target, func: scanPage, args: [kind] });
  } catch (err) {
    return; // e.g. PDF viewer, browser error page, permission just revoked
  }
  if (!result || result.href !== url) return; // password page, or the SPA moved on
  const { href, ...fields } = result;
  scannedUrls.add(url);
  await enqueue("pages", { url, kind, ts: now(), ...fields });
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg?.type !== "scroll" || !sender.tab) return;
  const pct = Math.max(0, Math.min(100, Number(msg.pct) || 0));
  serial(async () => {
    const cur = await getLocal("current", null);
    if (cur && cur.tabId === sender.tab.id && cur.url === msg.url && pct > (cur.scroll ?? 0)) {
      cur.scroll = pct;
      await chrome.storage.local.set({ current: cur });
    }
  });
});

// ---------------------------------------------------------------- events

chrome.tabs.onActivated.addListener(({ tabId }) => {
  serial(tick);
  chrome.tabs.get(tabId).then(scheduleScan).catch(() => {}); // re-install scroll tracking after a reload
});
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

chrome.storage.onChanged.addListener((changes) => {
  if (changes.paused) serial(tick); // pausing closes the current visit right away
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
