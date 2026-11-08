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
// 5. Focus mode: while a focus session runs (started here or on the dashboard), the
//    sites on the focus list open a "blocked" page instead. The desktop app is the source
//    of truth; every /ingest reply says whether a session is running.
// The "Pause" switch stops 1-3 (focus mode keeps blocking: you asked for it).

importScripts("search.js", "blocklist.js", "adult.js");

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
  return { visits: queue.visits || [], pages: queue.pages || [], searches: queue.searches || [],
    blocks: queue.blocks || [] };
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
  const blocks = queue.blocks.slice(0, 500);

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
    queued: queue.visits.length + queue.pages.length + queue.searches.length + queue.blocks.length,
  };

  let res;
  try {
    res = await fetch(`${SERVER}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Tracker-Token": token },
      body: JSON.stringify({ visits: [...visits, ...live], pages, searches, focus_blocks: blocks,
        media: !!mediaPlaying, client }),
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
  queue.blocks.splice(0, blocks.length);
  await chrome.storage.local.set({ queue });

  if (res.ok) {
    const data = await res.json().catch(() => null);
    if (data && "adult" in data) await applyAdult(data.adult);
    if (data && "focus" in data) await applyFocus(data.focus);
  }
}

// ---------------------------------------------------------------- focus mode

const FOCUS_RULE_BASE = 1000; // DNR rule ids FOCUS_RULE_BASE + i
const ADULT_RULE_LIST = 1;    // adult filter: known sites
const ADULT_RULE_WORDS = 2;   // adult filter: words in the address

const getFocus = async () => {
  const f = await getLocal("focus", null);
  return f && f.end > now() ? f : null;
};

function domainOf(hostname) {
  const h = (hostname || "").toLowerCase();
  return h.startsWith("www.") ? h.slice(4) : h;
}

// Adult filter mode from the dashboard: "focus" (during focus sessions), "always" or "off".
const adultActive = (mode, focus) => mode === "always" || (mode !== "off" && !!focus);

// Why a URL is blocked right now: "adult", "focus" or null.
function blockReason(url, focus, adultMode) {
  const u = parseUrl(url);
  if (!u || !/^https?:$/.test(u.protocol)) return null;
  const host = domainOf(u.hostname);
  if (adultActive(adultMode, focus) && isAdultHost(host)) return "adult";
  if (focus && focus.block.some((d) => host === d || host.endsWith("." + d))) return "focus";
  return null;
}

const blockedPage = (url, reason) =>
  chrome.runtime.getURL(`blocked.html?r=${reason}`) + "#" + encodeURIComponent(url);

const redirectTo = (reason) =>
  ({ type: "redirect", redirect: { regexSubstitution: chrome.runtime.getURL(`blocked.html?r=${reason}`) + "#\\0" } });

// Network-level block: the site never loads. Needs site access (the all-sites permission);
// without it the tab check below still catches it, just after the page starts loading.
async function updateBlockRules() {
  const focus = await getFocus();
  const adultMode = await getLocal("adultMode", "focus");
  const addRules = (focus ? focus.block : []).slice(0, 500).map((d, i) => ({
    id: FOCUS_RULE_BASE + i,
    priority: 1,
    action: redirectTo("focus"),
    condition: { regexFilter: "^https?://.*", requestDomains: [d], resourceTypes: ["main_frame"] },
  }));
  if (adultActive(adultMode, focus)) {
    addRules.push({
      id: ADULT_RULE_LIST, priority: 2, action: redirectTo("adult"),
      condition: { regexFilter: "^https?://.*", requestDomains: ADULT_DOMAINS, resourceTypes: ["main_frame"] },
    }, {
      id: ADULT_RULE_WORDS, priority: 2, action: redirectTo("adult"),
      condition: {
        regexFilter: `^https?://[^/?#]*(${ADULT_WORDS.join("|")})[^/?#]*([/?#].*)?$`,
        resourceTypes: ["main_frame"],
      },
    }, {
      id: ADULT_RULE_WORDS + 1, priority: 2, action: redirectTo("adult"),
      condition: { regexFilter: "^https?://[^/?#]*\\.(xxx|porn|sex|adult)\\.?([:/?#].*)?$", resourceTypes: ["main_frame"] },
    });
  }
  const existing = (await chrome.declarativeNetRequest.getDynamicRules()).map((r) => r.id);
  try {
    await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: existing, addRules });
  } catch (err) {
    console.warn("tracker: block rules", err);
  }
}

async function applyAdult(mode) {
  if (!["focus", "always", "off"].includes(mode)) return;
  if ((await getLocal("adultMode", "focus")) === mode) return;
  await chrome.storage.local.set({ adultMode: mode });
  await updateBlockRules();
  if (mode === "always") await closeBlockedTabs();
}

// Send the tabs you're looking at to the blocked page if they're blocked right now.
async function closeBlockedTabs() {
  const focus = await getFocus();
  const adultMode = await getLocal("adultMode", "focus");
  for (const t of await chrome.tabs.query({ active: true })) {
    const reason = blockReason(t.url, focus, adultMode);
    if (reason) chrome.tabs.update(t.id, { url: blockedPage(t.url, reason) });
  }
}

// Bring the browser in line with the desktop app's focus state (null = none running).
async function applyFocus(next) {
  const prev = await getLocal("focus", null);
  const valid = next && Array.isArray(next.block) && next.end > now() ? next : null;
  const same = prev && valid && prev.id === valid.id && prev.end === valid.end &&
    prev.block.join() === valid.block.join();
  if (same || (!prev && !valid)) return;
  await chrome.storage.local.set({ focus: valid });
  await updateBlockRules();
  if (valid) {
    await chrome.alarms.create("focus-end", { when: valid.end * 1000 + 1000 });
    await closeBlockedTabs(); // close the door on blocked tabs you're looking at right now
  } else {
    await chrome.alarms.clear("focus-end");
    if (prev && prev.end <= now() + 5) {
      chrome.notifications.create(`focus-${prev.id}`, {
        type: "basic", iconUrl: "icon128.png", title: "Focus session done",
        message: `${Math.round((prev.end - prev.start) / 60)} minutes${prev.label ? ` of ${prev.label}` : ""}. Nice work.`,
      });
    }
  }
}

// Fallback for when the network rules can't redirect (no site access): check on navigation.
async function enforceFocus(tab) {
  if (!tab?.url) return;
  const reason = blockReason(tab.url, await getFocus(), await getLocal("adultMode", "focus"));
  if (reason) await chrome.tabs.update(tab.id, { url: blockedPage(tab.url, reason) }).catch(() => {});
}

async function focusRequest(body) {
  const token = await getLocal("token", "");
  if (!token) throw new Error("Set the token in the extension settings first");
  let res;
  try {
    res = await fetch(`${SERVER}/focus`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Tracker-Token": token },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(5000),
    });
  } catch {
    const err = new Error("The desktop tracker isn't running");
    err.offline = true;
    throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  if ("adult" in data) await applyAdult(data.adult);
  await applyFocus(data.focus ?? null);
  return data;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "focus-blocked") {
    const u = parseUrl(msg.url);
    if (u) {
      serial(() => enqueue("blocks", { id: crypto.randomUUID(), ts: now(), domain: domainOf(u.hostname) }));
    }
    return;
  }
  if (!["focus-start", "focus-stop", "focus-status"].includes(msg?.type)) return;
  const body = { action: msg.type.slice(6), minutes: msg.minutes, label: msg.label };
  focusRequest(body)
    .then((data) => sendResponse({ ok: true, focus: data.focus ?? null }))
    .catch(async (err) => {
      if (body.action === "stop" && err.offline) {
        // Desktop app down: never leave you locked out. End it here; the app agrees on its next reply.
        await applyFocus(null);
        sendResponse({ ok: true, focus: null });
        return;
      }
      sendResponse({ ok: false, offline: !!err.offline, error: err.message, focus: await getFocus() });
    });
  return true; // async response
});

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

// Runs inside the page: reports the deepest scroll position (% of content) to the worker.
// Handles both normal pages (the window scrolls) and app-style pages where a large inner
// panel scrolls instead (Gmail, chat apps, HackerRank). No reading = nothing reported,
// never a made-up 100%.
function installScrollTracker() {
  if (window.__goodOlScroll2) return;
  window.__goodOlScroll2 = true;
  let href = location.href, max = 0, sent = 0, timer = null, target = null;
  const pct = (top, view, total) => Math.min(100, Math.round(((top + view) / total) * 100));
  const measure = (el) => {
    if (el && el.nodeType === 1 && el !== document.documentElement && el !== document.body) {
      // An inner panel counts only if it's a main content area, not a small widget.
      if (el.clientHeight < innerHeight * 0.5 || el.scrollHeight <= el.clientHeight + 1) return null;
      return pct(el.scrollTop, el.clientHeight, el.scrollHeight);
    }
    const height = Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0);
    if (height <= innerHeight + 1) return null; // the window itself doesn't scroll
    return pct(scrollY, innerHeight, height);
  };
  const report = () => {
    timer = null;
    if (location.href !== href) { href = location.href; max = 0; sent = 0; } // single-page app navigation
    const value = measure(target);
    target = null;
    if (value == null) return;
    max = Math.max(max, value);
    if (max >= sent + 5 || (max === 100 && sent < 100)) {
      sent = max;
      try { chrome.runtime.sendMessage({ type: "scroll", url: href, pct: max }).catch(() => {}); } catch {}
    }
  };
  // Capture phase: inner panels' scroll events don't bubble, but they do pass through here.
  addEventListener("scroll", (e) => {
    target = e.target === document ? null : e.target;
    if (!timer) timer = setTimeout(report, 1000);
  }, { passive: true, capture: true });
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
  chrome.tabs.get(tabId).then((tab) => { enforceFocus(tab); scheduleScan(tab); }).catch(() => {});
});
chrome.windows.onFocusChanged.addListener(() => serial(tick));
chrome.idle.onStateChanged.addListener(() => serial(tick));

chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (change.url) enforceFocus(tab); // any tab, active or not
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
  if (alarm.name === "focus-end") serial(async () => { await applyFocus(null); await tick(); });
});

async function init() {
  await chrome.alarms.create("heartbeat", { periodInMinutes: 0.5 });
  // A session that ended while the browser was closed: lift the blocks now.
  const f = await getLocal("focus", null);
  if (f && f.end <= now()) await serial(() => applyFocus(null));
  else await updateBlockRules();
  serial(tick);
}

chrome.runtime.onStartup.addListener(init);
chrome.runtime.onInstalled.addListener(async () => {
  await init();
  if (!(await getLocal("token", ""))) chrome.runtime.openOptionsPage();
});
