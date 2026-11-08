const ALL_SITES = { origins: ["http://*/*", "https://*/*"] };
const $ = (id) => document.getElementById(id);

function show(text, ok) {
  $("status").textContent = text;
  $("status").className = ok ? "ok" : "bad";
}

async function test(token) {
  try {
    const res = await fetch("http://127.0.0.1:47321/health", {
      headers: { "X-Tracker-Token": token },
      signal: AbortSignal.timeout(3000),
    });
    if (res.ok) show("Connected to the desktop tracker.", true);
    else if (res.status === 401) show("Desktop tracker is running, but the token is wrong.", false);
    else show(`Desktop tracker answered with HTTP ${res.status}.`, false);
  } catch {
    show("Can't reach the desktop tracker. Is it running? Data is queued until it is.", false);
  }
}

async function showQueue() {
  const { queue } = await chrome.storage.local.get("queue");
  const v = queue?.visits?.length ?? 0, p = queue?.pages?.length ?? 0, s = queue?.searches?.length ?? 0;
  $("queue").textContent = `Waiting to send: ${v} visits, ${s} searches, ${p} pages.`;
}

// --- connection
$("save").addEventListener("click", async () => {
  const token = $("token").value.trim();
  await chrome.storage.local.set({ token });
  await test(token);
});

// --- pause
$("paused").addEventListener("change", () => chrome.storage.local.set({ paused: $("paused").checked }));

// --- all-sites permission (request must happen directly in the click handler)
$("allSites").addEventListener("change", async () => {
  const box = $("allSites");
  if (box.checked) {
    box.checked = await chrome.permissions.request(ALL_SITES);
  } else {
    await chrome.permissions.remove(ALL_SITES);
    box.checked = await chrome.permissions.contains(ALL_SITES);
  }
});

// --- blocklist
function fillBlocklist(list) {
  $("blocklist").value = list.join("\n");
}
$("saveBlocklist").addEventListener("click", async () => {
  const list = [...new Set($("blocklist").value.split(/\s+/).map(normalizeDomain).filter(Boolean))];
  await chrome.storage.local.set({ blocklist: list });
  fillBlocklist(list);
  $("blockStatus").textContent = `Saved ${list.length} sites.`;
});
$("resetBlocklist").addEventListener("click", async () => {
  await chrome.storage.local.set({ blocklist: DEFAULT_BLOCKLIST });
  fillBlocklist(DEFAULT_BLOCKLIST);
  $("blockStatus").textContent = "Reset to defaults.";
});

(async () => {
  const { token, paused, blocklist } = await chrome.storage.local.get(["token", "paused", "blocklist"]);
  $("paused").checked = !!paused;
  $("allSites").checked = await chrome.permissions.contains(ALL_SITES);
  fillBlocklist(blocklist ?? DEFAULT_BLOCKLIST);
  if (token) {
    $("token").value = token;
    await test(token);
  }
  await showQueue();
})();
