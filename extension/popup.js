const ALL_SITES = { origins: ["http://*/*", "https://*/*"] };
const $ = (id) => document.getElementById(id);

function setState(text, tone = "") {
  $("state").textContent = text;
  $("state").className = `pill ${tone}`.trim();
}

function setFavicon(tab, label) {
  const box = $("fav");
  box.textContent = "";
  const letter = () => { box.textContent = (label || "?").charAt(0).toUpperCase(); };
  const src = tab?.favIconUrl;
  if (!src || !/^(https?:|data:image\/)/.test(src)) return letter();
  const img = new Image();
  img.alt = "";
  img.onerror = () => { img.remove(); letter(); };
  img.src = src;
  box.append(img);
}

async function render() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const { paused, blocklist } = await chrome.storage.local.get(["paused", "blocklist"]);
  const list = blocklist ?? DEFAULT_BLOCKLIST;
  $("paused").checked = !!paused;
  $("pauseHint").textContent = paused ? "Paused. Turn off to resume" : "Nothing is recorded while paused";

  let url;
  try { url = new URL(tab?.url || ""); } catch { url = null; }
  if (!url || !/^https?:$/.test(url.protocol)) {
    $("site").textContent = url?.protocol === "chrome-extension:" ? "Good ol Tracker page" : "Browser page";
    setFavicon(null, "•");
    setState("Not tracked");
    $("toggleBlock").hidden = true;
    return;
  }

  const domain = normalizeDomain(url.hostname);
  const userBlocked = list.some((d) => domain === d || domain.endsWith("." + d));
  const blocked = isBlocked(domain, list);
  const allSites = await chrome.permissions.contains(ALL_SITES);
  $("site").textContent = domain;
  $("site").title = domain;
  setFavicon(tab, domain);

  if (paused) setState("Paused: nothing recorded", "warn");
  else if (tab.incognito) setState("Private window: not tracked", "warn");
  else if (blocked) setState("Time only, page never read", "warn");
  else if (allSites) setState("Recording time and page content", "ok");
  else setState("Recording time only");

  // The built-in blocks (banks, password managers, WhatsApp) can't be undone from here.
  const btn = $("toggleBlock");
  btn.hidden = blocked && !userBlocked;
  btn.textContent = userBlocked ? "Allow reading this site again" : "Never read this site's content";
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

// ---------------------------------------------------------------- desktop app status

function setConnection(online) {
  const dot = $("conn");
  dot.className = `conn ${online ? "on" : "off"}`;
  dot.title = online ? "Desktop app is running" : "Desktop app isn't running: data waits in the browser";
}

// ---------------------------------------------------------------- focus mode

const pad = (n) => String(n).padStart(2, "0");
const clock = (s) => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
};
const timeOfDay = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

let focusTimer = null;
function renderFocus(focus) {
  const on = focus && focus.end > Date.now() / 1000;
  $("focusOff").hidden = !!on;
  $("focusOn").hidden = !on;
  clearInterval(focusTimer);
  disarmStop();
  if (!on) return;
  $("focusLabel").textContent = focus.label ? `Working on: ${focus.label}` : "";
  $("focusLabel").hidden = !focus.label;
  $("focusEnds").textContent = `Ends at ${timeOfDay(focus.end)}`;
  const total = focus.start ? focus.end - focus.start : 0;
  const tick = () => {
    const s = Math.max(0, Math.round(focus.end - Date.now() / 1000));
    if (!s) { renderFocus(null); return; }
    $("focusLeft").textContent = clock(s);
    if (total > 0) {
      const pct = Math.min(100, Math.max(0, Math.round((1 - s / total) * 100)));
      $("focusBar").firstElementChild.style.width = `${pct}%`;
      $("focusBar").setAttribute("aria-valuenow", String(pct));
    }
  };
  tick();
  focusTimer = setInterval(tick, 1000);
}

async function focusSend(msg) {
  $("focusMsg").textContent = "";
  const buttons = document.querySelectorAll("[data-min], #focusStop");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const res = await chrome.runtime.sendMessage(msg);
    if (!res?.ok) $("focusMsg").textContent = res?.error || "Something went wrong";
    if (res) setConnection(!res.offline);
    renderFocus(res?.focus ?? null);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

// Ending early takes two clicks, like on the blocked page.
let armed = null;
function disarmStop() {
  clearTimeout(armed);
  armed = null;
  $("focusStop").classList.remove("armed");
  $("focusStop").textContent = "End early";
}
$("focusStop").addEventListener("click", () => {
  if (!armed) {
    $("focusStop").classList.add("armed");
    $("focusStop").textContent = "Click again to end";
    armed = setTimeout(disarmStop, 4000);
    return;
  }
  disarmStop();
  focusSend({ type: "focus-stop" });
});

document.querySelectorAll("[data-min]").forEach((b) => b.addEventListener("click", () =>
  focusSend({ type: "focus-start", minutes: Number(b.dataset.min) })));

chrome.storage.local.get("focus").then(({ focus }) => renderFocus(focus));
chrome.runtime.sendMessage({ type: "focus-status" })
  .then((res) => { setConnection(!!res?.ok); renderFocus(res?.focus ?? null); })
  .catch(() => setConnection(false));

$("openDashboard").addEventListener("click", () => chrome.tabs.create({ url: "http://127.0.0.1:47321/" }));
$("openOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());
render();
