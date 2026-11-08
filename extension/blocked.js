// The page shown instead of a blocked site during a focus session.
// The original URL is in the hash: raw (network rule) or URI-encoded (tab check).
const $ = (id) => document.getElementById(id);

let target = location.hash.slice(1);
if (!/^https?:/i.test(target)) {
  try { target = decodeURIComponent(target); } catch { target = ""; }
}
let url = null;
try { url = new URL(target); } catch { /* unknown */ }
if (url && !/^https?:$/.test(url.protocol)) url = null;
const host = url ? url.hostname.replace(/^www\./, "") : "";

const REASONS = ["adult", "focus", "bedtime", "limit"];
const asked = new URLSearchParams(location.search).get("r");
const reason = REASONS.includes(asked) ? asked : "focus";
$("site").textContent = reason === "adult" ? "Adult site blocked" : host ? `${host} is blocked` : "This site is blocked";
document.title = host ? `Blocked: ${host}` : "Focus mode";
document.body.className = reason;

// Count the attempt once per blocked page (not on reload of this page). Focus sessions only.
if (url && reason === "focus" && !sessionStorage.getItem("counted")) {
  sessionStorage.setItem("counted", "1");
  chrome.runtime.sendMessage({ type: "focus-blocked", url: url.href }).catch(() => {});
}

let focus = null;
let timer = null;

function fmt(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

function over() {
  clearInterval(timer);
  if (reason === "adult") {
    // Blocked all the time (dashboard setting), not because of a session: nothing counts down.
    const { adultMode } = adultState;
    if (adultMode === "always") {
      $("kicker").textContent = "Blocked";
      $("left").hidden = true;
      $("left-note").textContent = "Adult sites are blocked all the time. Change it on the dashboard's Focus mode card.";
      $("stop").hidden = true;
      return;
    }
  }
  $("kicker").textContent = "Focus session over";
  $("left").textContent = "Done";
  $("left-note").textContent = "";
  $("stop").hidden = true;
  if (url) {
    $("continue").hidden = false;
    $("continue").href = url.href;
  }
}

function render() {
  if (!focus) return over();
  const left = focus.end - Date.now() / 1000;
  if (left <= 0) return over();
  $("left").textContent = fmt(left);
  $("label").textContent = focus.label ? `Working on: ${focus.label}` : "";
}

const adultState = { adultMode: "focus" };

// ---- bedtime / daily limit (same rules as the phone; details from the background's copy)
const onDomain = (h, list) => (list || []).some((d) => h === d || h.endsWith("." + d));
const clockTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
let rule = null;

function renderRule() {
  clearInterval(timer);
  $("stop").hidden = true;
  if (!rule || rule.until <= Date.now() / 1000) {
    $("kicker").textContent = reason === "bedtime" ? "Bedtime over" : "Limit lifted";
    $("left").textContent = "Open";
    $("left-note").textContent = "";
    $("grace").hidden = true;
    $("meter").hidden = true;
    if (url) { $("continue").hidden = false; $("continue").href = url.href; }
    return;
  }
  if (reason === "bedtime") {
    $("kicker").textContent = "Bedtime";
    $("site").textContent = `${host || "This site"} is locked until ${rule.end}`;
    $("label").textContent = "Social and video sites lock overnight, here and on your phone. Time to wind down.";
    $("left-note").textContent = "until it opens";
    $("back").textContent = "Close it and rest";
    $("grace").textContent = "5 more minutes (once tonight)";
  } else {
    const usedMin = Math.round(rule.used / 60);
    $("kicker").textContent = "Daily limit";
    $("site").textContent = `${rule.label}: daily limit reached`;
    $("label").textContent = `${usedMin} of ${rule.minutes} min used today, phone and PC together. It opens again at midnight.`;
    $("left-note").textContent = `until midnight (${clockTime(rule.until)})`;
    $("meter").hidden = false;
    $("meter").firstElementChild.style.width = "100%";
    $("grace").textContent = "5 more minutes (once today)";
  }
  $("grace").hidden = !!rule.grace_used;
  const tickRule = () => {
    const left = rule.until - Date.now() / 1000;
    if (left <= 0) { rule = null; renderRule(); return; }
    $("left").textContent = fmt(left);
  };
  tickRule();
  timer = setInterval(tickRule, 1000);
}

async function loadRule() {
  const { pcRules } = await chrome.storage.local.get("pcRules");
  const t = Date.now() / 1000;
  if (reason === "bedtime") rule = pcRules?.bedtime && pcRules.bedtime.until > t ? pcRules.bedtime : null;
  else rule = (pcRules?.limits || []).find((l) => l.until > t && (!host || onDomain(host, l.domains))) || null;
  renderRule();
}

$("grace").addEventListener("click", async () => {
  if (!rule) return;
  $("grace").disabled = true;
  const res = await chrome.runtime.sendMessage({ type: "pc-grace", key: rule.key }).catch(() => null);
  if (res?.ok) {
    $("msg").textContent = "5 more minutes. Make them count.";
    if (url) setTimeout(() => location.replace(url.href), 600);
  } else {
    $("msg").textContent = res?.error || "Couldn't do that.";
    $("grace").disabled = false;
  }
});

async function load() {
  if (reason === "bedtime" || reason === "limit") return loadRule();
  const { focus: f, adultMode } = await chrome.storage.local.get(["focus", "adultMode"]);
  adultState.adultMode = adultMode || "focus";
  focus = f && f.end > Date.now() / 1000 ? f : null;
  render();
  clearInterval(timer);
  if (focus) timer = setInterval(render, 1000);
}

chrome.storage.onChanged.addListener((changes) => {
  if (changes.focus || changes.adultMode || changes.pcRules) load();
});

$("back").addEventListener("click", () => {
  if (history.length > 1) history.back();
  else chrome.tabs.getCurrent().then((t) => t && chrome.tabs.remove(t.id)).catch(() => window.close());
});

let armed = false;
$("stop").addEventListener("click", async () => {
  if (!armed) {
    armed = true;
    $("stop").textContent = "Sure? Click again to end it";
    setTimeout(() => { armed = false; $("stop").textContent = "End focus session early"; }, 5000);
    return;
  }
  $("stop").disabled = true;
  const res = await chrome.runtime.sendMessage({ type: "focus-stop" });
  if (res?.ok) {
    $("msg").textContent = "Focus session ended.";
    if (url) location.replace(url.href);
  } else {
    $("msg").textContent = res?.error || "Couldn't end it.";
    $("stop").disabled = false;
  }
});

load();
