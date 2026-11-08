const ALL_SITES = { origins: ["http://*/*", "https://*/*"] };
const $ = (id) => document.getElementById(id);

async function render() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const { paused, blocklist } = await chrome.storage.local.get(["paused", "blocklist"]);
  const list = blocklist ?? DEFAULT_BLOCKLIST;
  $("paused").checked = !!paused;

  let url;
  try { url = new URL(tab?.url || ""); } catch { url = null; }
  if (!url || !/^https?:$/.test(url.protocol)) {
    $("site").textContent = "Browser page";
    $("state").textContent = "Not tracked.";
    $("toggleBlock").hidden = true;
    return;
  }

  const domain = normalizeDomain(url.hostname);
  const userBlocked = list.some((d) => domain === d || domain.endsWith("." + d));
  const blocked = isBlocked(domain, list);
  const allSites = await chrome.permissions.contains(ALL_SITES);
  $("site").textContent = domain;

  const state = $("state");
  if (paused) { state.textContent = "Paused: nothing is recorded."; state.className = "warn"; }
  else if (tab.incognito) { state.textContent = "Incognito: not tracked."; state.className = "warn"; }
  else if (blocked) { state.textContent = "Time tracked, content never read."; state.className = "warn"; }
  else if (allSites) { state.textContent = "Time + page content recorded."; state.className = "ok"; }
  else { state.textContent = "Time recorded (all-sites content is off)."; state.className = ""; }

  // The built-in "bank in the name" block can't be undone from here.
  const btn = $("toggleBlock");
  btn.hidden = blocked && !userBlocked;
  btn.textContent = userBlocked ? "Allow reading this site" : "Never read this site";
  btn.onclick = async () => {
    const next = userBlocked
      ? list.filter((d) => !(domain === d || domain.endsWith("." + d)))
      : [...list, domain];
    await chrome.storage.local.set({ blocklist: next });
    render();
  };
}

$("paused").addEventListener("change", async () => {
  await chrome.storage.local.set({ paused: $("paused").checked });
  render();
});
// ---------------------------------------------------------------- focus mode

let focusTimer = null;
function renderFocus(focus) {
  const on = focus && focus.end > Date.now() / 1000;
  $("focusOff").hidden = !!on;
  $("focusOn").hidden = !on;
  clearInterval(focusTimer);
  if (!on) return;
  $("focusLabel").textContent = focus.label ? `Working on: ${focus.label}` : "";
  const tick = () => {
    const s = Math.max(0, Math.round(focus.end - Date.now() / 1000));
    if (!s) { renderFocus(null); return; }
    const pad = (n) => String(n).padStart(2, "0");
    $("focusLeft").textContent = `${Math.floor(s / 60)}:${pad(s % 60)}`;
  };
  tick();
  focusTimer = setInterval(tick, 1000);
}

async function focusSend(msg) {
  $("focusMsg").textContent = "";
  const res = await chrome.runtime.sendMessage(msg);
  if (!res?.ok) $("focusMsg").textContent = res?.error || "Something went wrong";
  renderFocus(res?.focus ?? null);
}

document.querySelectorAll("[data-min]").forEach((b) => b.addEventListener("click", () =>
  focusSend({ type: "focus-start", minutes: Number(b.dataset.min) })));
$("focusStop").addEventListener("click", () => focusSend({ type: "focus-stop" }));
chrome.storage.local.get("focus").then(({ focus }) => renderFocus(focus));
chrome.runtime.sendMessage({ type: "focus-status" }).then((res) => renderFocus(res?.focus ?? null)).catch(() => {});

$("openDashboard").addEventListener("click", () => chrome.tabs.create({ url: "http://127.0.0.1:47321/" }));
$("openOptions").addEventListener("click", (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
render();
