const tokenInput = document.getElementById("token");
const statusEl = document.getElementById("status");
const queueEl = document.getElementById("queue");

function show(text, ok) {
  statusEl.textContent = text;
  statusEl.className = ok ? "ok" : "bad";
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
  const v = queue?.visits?.length ?? 0, p = queue?.pages?.length ?? 0;
  queueEl.textContent = `Waiting to send: ${v} visits, ${p} pages.`;
}

document.getElementById("save").addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  await chrome.storage.local.set({ token });
  await test(token);
});

(async () => {
  const { token } = await chrome.storage.local.get("token");
  if (token) {
    tokenInput.value = token;
    await test(token);
  }
  await showQueue();
})();
