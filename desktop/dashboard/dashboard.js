// Good ol Tracker dashboard. Plain DOM, no libraries; all data from /api/summary.
"use strict";

const REFRESH_MS = 30000;
const $ = (sel) => document.querySelector(sel);

const state = { range: "today", day: null, device: "all", data: null, timer: null, editApps: false, editPages: false };

// ------------------------------------------------------------------ helpers

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

const pad = (n) => String(n).padStart(2, "0");
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseDay = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };

function fmtDur(seconds) {
  const s = Math.round(seconds || 0);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  const h = Math.floor(m / 60);
  if (!h) return `${m}m`;
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
}
const fmtClock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtShortDay = (iso) => parseDay(iso).toLocaleDateString([], { weekday: "short", day: "numeric" });
const fmtWhen = (ts, multiDay) => multiDay
  ? `${new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" })} ${fmtClock(ts)}`
  : fmtClock(ts);
const pct = (part, whole) => (whole ? Math.round((part / whole) * 100) : 0);

// Color follows the category (its position in categories.json), never its rank.
// 7 validated hue slots; Unsure and anything past slot 7 fold into neutral grays.
function colorFor(cat) {
  if (cat === "Unsure") return "var(--cat-unsure)";
  const i = (state.data?.categories || []).indexOf(cat);
  if (cat === "Other" || i < 0 || i >= 7) return "var(--cat-other)";
  return `var(--cat-${i + 1})`;
}

function swatch(cat) {
  const s = el("span", "swatch");
  s.style.background = colorFor(cat);
  return s;
}

function catLabel(cat) {
  const span = el("span", "cat");
  span.append(swatch(cat), el("span", null, cat || "-"));
  return span;
}

function legend(container, cats) {
  container.replaceChildren(...cats.map((c) => {
    const item = el("span");
    item.append(swatch(c), document.createTextNode(c));
    return item;
  }));
}

function table(headers, rows) {
  const wrap = el("div", "table-wrap");
  const t = el("table");
  const head = el("tr");
  headers.forEach((h) => head.append(el("th", h.num ? "num" : null, h.label)));
  t.append(el("thead"));
  t.tHead.append(head);
  const body = el("tbody");
  rows.forEach((r) => {
    const tr = el("tr");
    r.forEach((cell, i) => {
      const td = el("td", headers[i].num ? "num" : headers[i].cls || null);
      if (cell instanceof Node) td.append(cell); else td.textContent = cell ?? "";
      tr.append(td);
    });
    body.append(tr);
  });
  t.append(body);
  wrap.append(t);
  return wrap;
}

const empty = (text) => el("p", "empty", text);

// "Nice" axis top + ticks for a max value in seconds.
function niceAxis(maxSeconds, steps) {
  const step = steps.find((s) => maxSeconds / s <= 5) || steps[steps.length - 1];
  const top = Math.max(step, Math.ceil(maxSeconds / step) * step);
  const ticks = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
  return { top, ticks };
}

// ------------------------------------------------------------------ tooltip

const tips = new WeakMap();
const tipEl = $("#tip");

function setTip(node, tip) {
  tips.set(node, tip);
  node.classList.add("has-tip");
}

function renderTip(tip) {
  const parts = [el("div", "t", tip.title)];
  (tip.rows || []).forEach((r) => {
    const row = el("div", "r");
    const left = el("span");
    if (r.cat) left.append(swatch(r.cat));
    left.append(document.createTextNode(r.label));
    row.append(left, el("b", null, r.value));
    parts.push(row);
  });
  if (tip.note) parts.push(el("div", "muted", tip.note));
  tipEl.replaceChildren(...parts);
}

function placeTip(x, y) {
  tipEl.hidden = false;
  const r = tipEl.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > innerHeight - 8) top = y - r.height - 14;
  tipEl.style.left = `${Math.max(8, left)}px`;
  tipEl.style.top = `${Math.max(8, top)}px`;
}

document.addEventListener("pointermove", (e) => {
  const target = e.target.closest?.(".has-tip");
  if (!target) { tipEl.hidden = true; return; }
  renderTip(tips.get(target));
  placeTip(e.clientX, e.clientY);
});
document.addEventListener("focusin", (e) => {
  const target = e.target.closest?.(".has-tip");
  if (!target) { tipEl.hidden = true; return; }
  renderTip(tips.get(target));
  const r = target.getBoundingClientRect();
  placeTip(r.left + r.width / 2, r.top);
});
document.addEventListener("focusout", () => { tipEl.hidden = true; });
document.addEventListener("scroll", () => { tipEl.hidden = true; }, { passive: true });

// ------------------------------------------------------------------ column chart

// columns: [{ label, segments: [{cat, value}], tip }]; values in seconds.
function columnChart(columns, { top, ticks, tickLabel, labelEvery = 1 }) {
  const root = el("div", "cols");
  const yaxis = el("div", "yaxis");
  const plot = el("div", "plot");
  ticks.forEach((v) => {
    const y = (v / top) * 100;
    const label = el("span", null, tickLabel(v));
    label.style.bottom = `${y}%`;
    yaxis.append(label);
    if (v > 0) {
      const line = el("div", "gridline");
      line.style.bottom = `${y}%`;
      plot.append(line);
    }
  });
  const row = el("div", "row");
  const xaxis = el("div", "xaxis");
  columns.forEach((c, i) => {
    const col = el("div", "col");
    const total = c.segments.reduce((a, s) => a + s.value, 0);
    if (total > 0) {
      const stack = el("div", "stack");
      stack.style.height = `${Math.min(100, (total / top) * 100)}%`;
      c.segments.filter((s) => s.value > 0).forEach((s) => {
        const seg = el("div", "seg");
        seg.style.flexGrow = String(s.value);
        seg.style.background = colorFor(s.cat);
        stack.append(seg);
      });
      col.append(stack);
      col.tabIndex = 0;
      setTip(col, c.tip);
    }
    row.append(col);
    xaxis.append(el("span", null, i % labelEvery === 0 ? c.label : ""));
  });
  plot.append(row);
  root.append(yaxis, plot, xaxis);
  return root;
}

// ------------------------------------------------------------------ sections

function renderNow(d) {
  const now = d.now;
  const out = $("#now");
  if (!now || (!now.desktop && !now.browser)) {
    out.textContent = d.range.includes_today ? "Right now: idle or away" : "";
    return;
  }
  const BROWSERS = ["brave.exe", "chrome.exe", "msedge.exe"];
  const inBrowser = now.desktop && BROWSERS.includes(now.desktop.app.toLowerCase());
  let text;
  if (inBrowser && now.browser) text = `${now.browser.category} · ${now.browser.app}`;  // the site decides
  else if (now.desktop) text = `${now.desktop.category} · ${now.desktop.app}`;
  else text = `${now.browser.category} · ${now.browser.app}`;
  out.textContent = `Right now: ${text}`;
}

function tile(label, value, note, hero = false) {
  const t = el("div", hero ? "tile hero" : "tile");
  t.append(el("div", "label", label), el("div", "value", value));
  if (note) t.append(el("div", "note", note));
  return t;
}

function renderTiles(d) {
  const { active, productive } = d.totals;
  const days = d.range.days;
  const f = d.focus;
  const longest = f.longest;
  const longestNote = longest
    ? `${longest.category}, ${days > 1 ? fmtWhen(longest.start, true) : fmtClock(longest.start)}–${fmtClock(longest.end)}`
    : `No ${fmtDur(f.min_seconds)} streak yet`;
  $("#tiles").replaceChildren(
    tile("Productive time", fmtDur(productive),
      `${pct(productive, active)}% of ${fmtDur(active)} active` + (days > 1 ? ` · ${fmtDur(productive / days)}/day` : ""),
      true),
    tile("Active time", fmtDur(active), days > 1 ? `${fmtDur(active / days)} per day` : d.productive.join(", ") + " count as productive"),
    tile("Longest focus", longest ? fmtDur(longest.seconds) : "–", longestNote),
    tile("Deep focus sessions", String(f.deep_count), `${fmtDur(f.deep_threshold)}+ each · ${fmtDur(f.deep_seconds)} total`),
    tile("Switches per hour", d.switches.per_hour ?? "–", `${d.switches.count} app or site switches`),
  );
  if (d.phone?.paired) {
    const p = d.phone;
    $("#tiles").append(tile("Phone", fmtDur(p.seconds),
      `${p.unlocks} unlock${p.unlocks === 1 ? "" : "s"}` + (days > 1 ? ` · ${fmtDur(p.seconds / days)}/day` : "")
      + (d.device === "pc" ? " · not in the numbers above" : "")));
  }
}

function renderCategories(d) {
  const total = d.totals.active;
  const entries = Object.entries(d.totals.by_category).filter(([, s]) => s > 0).sort((a, b) => b[1] - a[1]);
  $("#cats-sub").textContent = total ? `${fmtDur(total)} active` : "";
  if (!entries.length) {
    $("#cats").replaceChildren(empty("No activity in this range yet."));
    $("#cats-table").replaceChildren();
    return;
  }
  const max = entries[0][1];
  const grid = el("div", "bars");
  entries.forEach(([cat, secs]) => {
    const name = el("div", "name");
    name.append(swatch(cat), el("span", null, cat));
    const track = el("div", "track");
    const bar = el("div", "bar");
    bar.style.width = `${(secs / max) * 100}%`;
    bar.style.background = colorFor(cat);
    track.append(bar);
    const val = el("div", "val");
    val.append(el("b", null, fmtDur(secs)), document.createTextNode(`  ${pct(secs, total)}%`));
    [name, track, val].forEach((n) => setTip(n, { title: cat, rows: [{ cat, label: "Time", value: fmtDur(secs) }, { label: "Share", value: `${pct(secs, total)}%` }] }));
    track.tabIndex = 0;
    grid.append(name, track, val);
  });
  $("#cats").replaceChildren(grid);
  $("#cats-table").replaceChildren(table(
    [{ label: "Category" }, { label: "Time", num: true }, { label: "Share", num: true }],
    entries.map(([cat, s]) => [catLabel(cat), fmtDur(s), `${pct(s, total)}%`]),
  ));
}

function renderHours(d) {
  const days = d.range.days;
  const prod = d.productive;
  const series = [...prod, "Everything else"];
  const cols = d.by_hour.map((h) => {
    const segs = prod.map((c) => ({ cat: c, value: (h.by_category[c] || 0) / days }));
    const other = Object.entries(h.by_category).filter(([c]) => !prod.includes(c)).reduce((a, [, v]) => a + v, 0) / days;
    segs.push({ cat: "Other", value: other });
    return {
      label: `${pad(h.hour)}`,
      segments: segs,
      tip: {
        title: `${pad(h.hour)}:00–${pad((h.hour + 1) % 24)}:00${days > 1 ? " (average per day)" : ""}`,
        rows: [...segs.slice(0, -1).filter((s) => s.value > 0).map((s) => ({ cat: s.cat, label: s.cat, value: fmtDur(s.value) })),
          { cat: "Other", label: "Everything else", value: fmtDur(other) }],
      },
    };
  });
  const hasData = cols.some((c) => c.segments.some((s) => s.value > 0));
  $("#hours-sub").textContent = days > 1 ? "Average minutes per hour of the day" : "Minutes per hour of the day";
  const leg = $("#hours-legend");
  legend(leg, prod);
  const other = el("span");
  other.append(swatch("Other"), document.createTextNode("Everything else"));
  leg.append(other);
  if (!hasData) {
    $("#hours").replaceChildren(empty("No activity yet."));
    $("#hours-table").replaceChildren();
    return;
  }
  // An hour holds at most 60 minutes, so the axis is fixed: 0-60 min.
  $("#hours").replaceChildren(columnChart(cols, {
    top: 3600, ticks: [0, 900, 1800, 2700, 3600], tickLabel: (v) => `${v / 60}m`, labelEvery: 3,
  }));
  $("#hours-table").replaceChildren(table(
    [{ label: "Hour" }, ...series.map((s) => ({ label: s, num: true }))],
    cols.filter((c) => c.segments.some((s) => s.value > 0))
      .map((c) => [`${c.label}:00`, ...c.segments.map((s) => fmtDur(s.value))]),
  ));
}

function renderDays(d) {
  const card = $("#days-card");
  card.hidden = d.range.days < 2;
  if (card.hidden) return;
  const cats = d.categories.filter((c) => d.per_day.some((p) => p.by_category[c]));
  legend($("#days-legend"), cats);
  const max = Math.max(0, ...d.per_day.map((p) => p.active));
  if (!max) {
    $("#days").replaceChildren(empty("No activity in this range yet."));
    $("#days-table").replaceChildren();
    return;
  }
  const axis = niceAxis(max, [900, 1800, 3600, 7200, 10800, 14400, 21600, 28800]);
  const cols = d.per_day.map((p) => ({
    label: fmtShortDay(p.date),
    segments: cats.map((c) => ({ cat: c, value: p.by_category[c] || 0 })),
    tip: {
      title: `${parseDay(p.date).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" })} · ${fmtDur(p.active)}`,
      rows: cats.filter((c) => p.by_category[c]).map((c) => ({ cat: c, label: c, value: fmtDur(p.by_category[c]) })),
    },
  }));
  $("#days").replaceChildren(columnChart(cols, {
    ...axis, tickLabel: (v) => fmtDur(v), labelEvery: d.per_day.length > 14 ? 5 : 1,
  }));
  $("#days-table").replaceChildren(table(
    [{ label: "Day" }, { label: "Active", num: true }, ...cats.map((c) => ({ label: c, num: true }))],
    d.per_day.map((p) => [p.date, fmtDur(p.active), ...cats.map((c) => (p.by_category[c] ? fmtDur(p.by_category[c]) : ""))]),
  ));
}

function renderTimeline(d) {
  const days = d.timeline.filter((t) => t.blocks.length);
  const container = $("#timeline");
  if (!days.length) {
    container.replaceChildren(empty("No activity in this range yet."));
    $("#timeline-legend").replaceChildren();
    $("#timeline-table").replaceChildren();
    return;
  }
  // Only show the hours you were actually active (at least 4h wide).
  let minH = 24, maxH = 0;
  days.forEach((t) => {
    const midnight = parseDay(t.date).getTime() / 1000;
    t.blocks.forEach((b) => {
      minH = Math.min(minH, Math.floor((b.start - midnight) / 3600));
      maxH = Math.max(maxH, Math.ceil((b.end - midnight) / 3600));
    });
  });
  if (maxH - minH < 4) maxH = Math.min(24, minH + 4);
  if (maxH - minH < 4) minH = Math.max(0, maxH - 4);
  const span = (maxH - minH) * 3600;

  const usedCats = d.categories.filter((c) => days.some((t) => t.blocks.some((b) => b.category === c)));
  legend($("#timeline-legend"), usedCats);

  const grid = el("div", "timeline");
  const tickStep = maxH - minH > 12 ? 3 : maxH - minH > 6 ? 2 : 1;
  days.forEach((t) => {
    const midnight = parseDay(t.date).getTime() / 1000 + minH * 3600;
    grid.append(el("div", "dlabel", fmtShortDay(t.date)));
    const lane = el("div", "lane");
    for (let h = minH + tickStep; h < maxH; h += tickStep) {
      const tick = el("div", "tick");
      tick.style.left = `${((h - minH) / (maxH - minH)) * 100}%`;
      lane.append(tick);
    }
    t.blocks.forEach((b) => {
      const left = ((b.start - midnight) / span) * 100;
      const width = ((b.end - b.start) / span) * 100;
      const block = el("div", "block");
      block.style.left = `${left}%`;
      // 2px surface gap between touching blocks (skip for slivers).
      block.style.width = width > 0.6 ? `calc(${width}% - 2px)` : `${width}%`;
      block.style.background = colorFor(b.category);
      setTip(block, {
        title: `${fmtClock(b.start)}–${fmtClock(b.end)} · ${fmtDur(b.end - b.start)}`,
        rows: [{ cat: b.category, label: b.category, value: "" }],
        note: b.label,
      });
      lane.append(block);
    });
    grid.append(lane);
  });
  const hours = el("div", "hours");
  for (let h = minH; h <= maxH; h += tickStep) {
    const s = el("span", null, `${pad(h % 24)}:00`);
    s.style.left = `${((h - minH) / (maxH - minH)) * 100}%`;
    hours.append(s);
  }
  grid.append(hours);
  container.replaceChildren(grid);

  const rows = [];
  days.forEach((t) => t.blocks.filter((b) => b.end - b.start >= 60).forEach((b) => {
    rows.push([t.date, `${fmtClock(b.start)}–${fmtClock(b.end)}`, fmtDur(b.end - b.start), catLabel(b.category), b.label]);
  }));
  $("#timeline-table").replaceChildren(table(
    [{ label: "Day" }, { label: "From–to" }, { label: "Length", num: true }, { label: "Category" }, { label: "Mostly" }],
    rows,
  ));
}

// ------------------------------------------------------------------ editing categories

const CONTENT = "content";
const SECTION_FOR = { site: "sites", app: "apps", phone: "phone_apps" };

// A <select> of categories. `current` is preselected; "" shows a "Choose…" prompt.
function catSelect(current, { content = false, label = "Category" } = {}) {
  const sel = el("select", "catsel");
  sel.setAttribute("aria-label", label);
  if (!current) sel.append(new Option("Choose…", "", true, true));
  if (content) sel.append(new Option("By page content", CONTENT, false, current === CONTENT));
  (state.data?.categories || []).forEach((c) => sel.append(new Option(c, c, false, c === current)));
  return sel;
}

async function saveRules(changes, select) {
  if (select) select.disabled = true;
  try {
    const r = await post("/api/rules", { changes });
    say("#rules-msg", r.changed.length ? `Saved: ${r.changed.join(", ")}` : "No change needed.");
    state.trendsAt = 0;
    await Promise.all([load(), loadRecentRules()]);
  } catch (err) {
    say("#rules-msg", `Couldn't save: ${err.message}`, true);
    $("#rules-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
    if (select) select.disabled = false;
  }
}

async function loadRecentRules() {
  try {
    const res = await fetch("/api/rules/recent", { cache: "no-store" });
    const { recent } = await res.json();
    const box = $("#rules-recent");
    if (!recent.length) { box.replaceChildren(); return; }
    const head = el("div", "card-head");
    head.append(el("h3", "mini", "Recent changes"));
    const undo = el("button", "linkbtn", "Undo last change");
    undo.type = "button";
    undo.addEventListener("click", async () => {
      try {
        const r = await post("/api/rules/undo");
        say("#rules-msg", `Undone: ${r.undone.join(", ")}`);
        state.trendsAt = 0;
        await Promise.all([load(), loadRecentRules()]);
      } catch (err) { say("#rules-msg", err.message, true); }
    });
    head.append(undo);
    const list = el("ul", "recent");
    recent.forEach((h) => list.append(el("li", null,
      `${fmtWhen(h.ts, true)} · ${h.changes.join(", ")}`)));
    box.replaceChildren(head, list);
  } catch { /* ignore */ }
}

$("#rule-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  saveRules([{ section: f.section.value, key: f.key.value, category: f.category.value }])
    .then(() => { if (!$("#rules-msg").classList.contains("bad")) f.key.value = ""; });
});
$("#rule-form").elements.section.addEventListener("change", (e) => {
  const ph = { sites: "e.g. chess.com", apps: "e.g. Spotify.exe", channels: "e.g. Neso Academy",
    phone_apps: "e.g. com.instagram.android" };
  $("#rule-form").elements.key.placeholder = ph[e.target.value];
  fillRuleFormCategories();
});

function fillRuleFormCategories() {
  const f = $("#rule-form").elements;
  const keep = f.category.value;
  const sel = catSelect(keep || "", { content: f.section.value === "sites" });
  f.category.replaceChildren(...sel.options);
  if (keep) f.category.value = keep;
}

function toggleEdit(btn, key) {
  btn.addEventListener("click", () => {
    state[key] = !state[key];
    btn.setAttribute("aria-pressed", String(state[key]));
    btn.textContent = state[key] ? "Done" : "Change categories";
    if (state.data) render(state.data);
  });
}
toggleEdit($("#apps-edit"), "editApps");
toggleEdit($("#pages-edit"), "editPages");

function renderAppsEditor(d) {
  const rows = d.top_apps.map((a) => {
    let pick = el("span", "muted", a.kind === "offline" ? "logged by you" : "split by site");
    if (a.rule_section) {
      pick = catSelect(a.rule || "", { content: a.rule_section === "sites", label: `Category for ${a.name}` });
      pick.addEventListener("change", () => saveRules([{ section: a.rule_section, key: a.name, category: pick.value }], pick));
    }
    return [a.kind === "phone" ? a.label : a.name, a.kind, fmtDur(a.seconds), pick];
  });
  const wrap = el("div");
  wrap.append(el("p", "sub", "Sets a rule for the whole app or site. For YouTube and similar, keep \"By page content\" so each video is judged on its own."),
    table([{ label: "Name", cls: "title" }, { label: "Type" }, { label: "Time", num: true }, { label: "Rule" }], rows));
  $("#apps").replaceChildren(wrap);
}

function renderApps(d) {
  if (!d.top_apps.length) { $("#apps").replaceChildren(empty("Nothing yet.")); return; }
  if (state.editApps) { renderAppsEditor(d); return; }
  const max = d.top_apps[0].seconds;
  const grid = el("div", "bars thin");
  d.top_apps.forEach((a) => {
    const name = el("div", "name");
    name.append(swatch(a.category), el("span", null, a.kind === "phone" ? a.label : a.name));
    name.append(el("span", "tag", a.kind));
    const track = el("div", "track");
    const bar = el("div", "bar");
    bar.style.width = `${(a.seconds / max) * 100}%`;
    bar.style.background = colorFor(a.category);
    track.append(bar);
    const val = el("div", "val");
    val.append(el("b", null, fmtDur(a.seconds)));
    const tip = { title: a.kind === "phone" ? `${a.label} (phone)` : a.name, rows: [{ cat: a.category, label: a.category, value: fmtDur(a.seconds) }] };
    [name, track, val].forEach((n) => setTip(n, tip));
    grid.append(name, track, val);
  });
  $("#apps").replaceChildren(grid);
}

function renderTopics(d) {
  if (!d.topics.length) { $("#topics").replaceChildren(empty("Topics appear once you search or read pages.")); return; }
  const max = d.topics[0].score;
  const grid = el("div", "bars thin");
  d.topics.forEach((t) => {
    const name = el("div", "name");
    name.append(el("span", null, t.term));
    const track = el("div", "track");
    const bar = el("div", "bar");
    bar.style.width = `${(t.score / max) * 100}%`;
    bar.style.background = "var(--neutral-bar)";
    track.append(bar);
    const bits = [];
    if (t.searches) bits.push(`${t.searches} search${t.searches > 1 ? "es" : ""}`);
    if (t.pages) bits.push(`${t.pages} page${t.pages > 1 ? "s" : ""}`);
    grid.append(name, track, el("div", "val", bits.join(" · ")));
  });
  $("#topics").replaceChildren(grid);
}

function renderPages(d) {
  if (!d.pages.length) { $("#pages").replaceChildren(empty("No pages yet. Browse with the extension on.")); return; }
  $("#pages").replaceChildren(table(
    [{ label: "Page", cls: "title" }, { label: "Site" }, { label: "Time", num: true }, { label: "Read" }, { label: "Category" }],
    d.pages.map((p) => {
      const link = el("a", null, p.title || p.url);
      link.href = p.url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      let read = "–";
      if (p.scroll != null) {
        read = el("span");
        const meter = el("span", "meter");
        const fill = el("i");
        fill.style.width = `${p.scroll}%`;
        meter.append(fill);
        read.append(meter, document.createTextNode(`${Math.round(p.scroll)}%`));
      }
      let cat = catLabel(p.category);
      if (p.ai && !state.editPages) {
        const badge = el("span", "badge ai", "AI");
        const why = `Decided by AI (${Math.round(p.ai.confidence * 100)}% sure): ${p.ai.reason || "no reason given"}. Use "Change categories" to correct it.`;
        badge.title = why;
        badge.setAttribute("aria-label", why);
        cat.append(badge);
      }
      if (state.editPages) {
        cat = catSelect(p.category, { label: `Category for ${p.title || p.url}` });
        cat.addEventListener("change", () => saveRules(
          p.urls.map((u) => ({ section: "pages", key: u, category: cat.value })), cat));
      }
      return [link, p.domain, fmtDur(p.seconds), read, cat];
    }),
  ));
}

function renderSearches(d) {
  if (!d.searches.length) { $("#searches").replaceChildren(empty("No searches in this range.")); return; }
  const multi = d.range.days > 1;
  const rows = d.searches.map((s) => [fmtWhen(s.ts, multi), s.engine, s.query, s.category ? catLabel(s.category) : "–"]);
  const wrap = el("div");
  wrap.append(table([{ label: "When" }, { label: "Where" }, { label: "Search", cls: "title" }, { label: "Category" }], rows));
  if (d.search_count > d.searches.length) wrap.append(el("p", "muted", `Showing the latest ${d.searches.length} of ${d.search_count}.`));
  $("#searches").replaceChildren(wrap);
}

function renderRules(d) {
  fillRuleFormCategories();
  const parts = [];
  const aj = d.ai_judge;
  if (aj) {
    let note = "";
    if (!aj.groq_key) note = "AI checking is off: add a Groq key (data/groq_key.txt).";
    else if (aj.last_error) note = `AI checking paused: ${aj.last_error}`;
    else if (aj.pages_asked || aj.suggestions) {
      note = `AI checked ${aj.pages_asked} unsure page${aj.pages_asked === 1 ? "" : "s"} and decided ${aj.pages_decided}`
        + (aj.suggestions ? ` · ${aj.suggestions} site/app suggestion${aj.suggestions === 1 ? "" : "s"} waiting` : "") + ".";
    } else note = "AI checks unsure pages and new sites/apps in the background, every couple of minutes.";
    parts.push(el("p", "muted ai-note", note));
  }
  const suggested = d.unmatched.filter((u) => u.suggestion);
  if (suggested.length) {
    parts.push(el("h3", "mini", "AI suggestions"));
    parts.push(el("p", "sub", "Accepting saves a rule for the whole site or app (you can undo it)."));
    parts.push(table([{ label: "Name", cls: "title" }, { label: "Time", num: true }, { label: "Suggested" }, { label: "" }],
      suggested.map((u) => {
        const s = u.suggestion;
        const section = SECTION_FOR[u.kind];
        const label = s.category === CONTENT ? "By page content" : s.category;
        const what = el("div");
        what.append(s.category === CONTENT ? el("span", "cat", label) : catLabel(s.category));
        what.append(el("div", "scope", `${s.reason || ""} (${Math.round(s.confidence * 100)}% sure)`.trim()));
        const name = el("div");
        name.append(el("span", null, u.label || u.name), el("span", "tag", u.kind));
        const actions = el("div", "suggest-actions");
        const yes = el("button", "ghost small", "Accept");
        const no = el("button", "linkbtn", "Reject");
        yes.type = no.type = "button";
        yes.setAttribute("aria-label", `Accept ${label} for ${u.name}`);
        no.setAttribute("aria-label", `Reject the suggestion for ${u.name}`);
        yes.addEventListener("click", () => saveRules([{ section, key: u.name, category: s.category }], yes));
        no.addEventListener("click", async () => {
          no.disabled = true;
          try {
            await post("/api/ai/dismiss", { kind: u.kind, name: u.name });
            say("#rules-msg", `Rejected the suggestion for ${u.name}.`);
            await load();
          } catch (err) { say("#rules-msg", `Couldn't reject: ${err.message}`, true); no.disabled = false; }
        });
        actions.append(yes, no);
        return [name, fmtDur(u.seconds), what, actions];
      })));
  }
  const unmatched = d.unmatched.filter((u) => !u.suggestion);
  if (unmatched.length) {
    parts.push(el("h3", "mini", "No rule yet"));
    parts.push(table([{ label: "Name", cls: "title" }, { label: "Type" }, { label: "Time", num: true }, { label: "Set category" }],
      unmatched.map((u) => {
        const section = SECTION_FOR[u.kind];
        const sel = catSelect("", { content: section === "sites", label: `Category for ${u.label || u.name}` });
        sel.addEventListener("change", () => saveRules([{ section, key: u.name, category: sel.value }], sel));
        return [u.label || u.name, u.kind, fmtDur(u.seconds), sel];
      })));
  }
  if (d.unsure.length) {
    parts.push(el("h3", "mini", "Unsure pages and videos"));
    parts.push(table([{ label: "Site" }, { label: "Title", cls: "title" }, { label: "Time", num: true }, { label: "Set category" }],
      d.unsure.map((u) => {
        const box = el("div");
        const sel = catSelect("", { label: `Category for ${u.title}` });
        let scope = null;
        if (u.channel) {  // a channel rule fixes every video from it, past and future
          scope = el("select", "catsel");
          scope.setAttribute("aria-label", "Applies to");
          scope.append(new Option(`All from ${u.channel}`, "channel"), new Option("Just this one", "page"));
        }
        sel.addEventListener("change", () => {
          const changes = scope?.value === "channel"
            ? [{ section: "channels", key: u.channel, category: sel.value }]
            : u.urls.map((url) => ({ section: "pages", key: url, category: sel.value }));
          saveRules(changes, sel);
        });
        box.append(sel);
        if (scope) { box.append(document.createTextNode(" ")); box.append(scope); }
        else box.append(el("div", "scope", "this page"));
        return [u.site, u.title, fmtDur(u.seconds), box];
      })));
  }
  if (!parts.some((p) => p.tagName === "TABLE" || p.querySelector?.("table"))) parts.push(empty("Everything in this range has a category. Use the pickers in \"Top apps and sites\" or \"Pages read\" to change one."));
  $("#rules").replaceChildren(...parts);
}

function render(d) {
  state.data = d;
  renderNow(d);
  renderTiles(d);
  renderCategories(d);
  renderHours(d);
  renderDays(d);
  renderTimeline(d);
  renderApps(d);
  renderTopics(d);
  renderPages(d);
  renderSearches(d);
  renderRules(d);
}

// ------------------------------------------------------------------ health strip

function fmtAgo(seconds) {
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400 * 2) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

function pill(level, label, detail, title) {
  const p = el("span", `pill ${level}`);
  p.append(el("span", "ico", { good: "✓", warn: "!", bad: "✕" }[level]), el("b", null, label));
  if (detail) p.append(document.createTextNode(detail));
  if (title) p.title = title;
  return p;
}

async function loadHealth() {
  let h;
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    h = await res.json();
    if (!res.ok) throw new Error();
  } catch {
    $("#health").replaceChildren(pill("bad", "Tracker", " not reachable"));
    return;
  }
  state.health = h;
  const now = h.now;
  const pills = [];

  const tickAge = h.tracker.last_tick ? now - h.tracker.last_tick : Infinity;
  pills.push(tickAge < 20
    ? pill("good", "Desktop tracker", " running")
    : pill("bad", "Desktop tracker", ` not sampling${isFinite(tickAge) ? ` (last ${fmtAgo(tickAge)})` : ""}`));

  const ext = h.extension;
  const extAge = ext.last_seen ? now - ext.last_seen : null;
  if (extAge == null) {
    pills.push(pill("bad", "Extension", " never connected", "Check the token in the extension's settings"));
  } else if (extAge < 90) {
    pills.push(pill("good", "Extension", ` connected${ext.version ? ` (v${ext.version})` : ""}`));
  } else {
    pills.push(pill("warn", "Extension", ` last seen ${fmtAgo(extAge)}`, "Normal if Brave is closed"));
  }
  if (extAge != null) {
    if (ext.paused) pills.push(pill("warn", "Tracking", " paused in the extension"));
    else if (ext.all_sites === false) {
      pills.push(pill("warn", "Page scanning", " off", "Extension settings → tick 'Read page content on all sites'"));
    } else if (ext.all_sites) pills.push(pill("good", "Page scanning", " on"));
  }

  const ai = h.ai;
  if (ai.last_error) pills.push(pill("warn", "AI", ` ${ai.last_error}`));
  else if (ai.voyage_key || ai.groq_key) {
    const parts = [ai.voyage_key && "categoriser", ai.groq_key && "weekly summary"].filter(Boolean);
    pills.push(pill("good", "AI", ` ${parts.join(" + ")} ready`));
  }

  const b = h.backups;
  const bAge = b.last_ts ? now - b.last_ts : null;
  if (bAge == null) pills.push(pill("warn", "Backup", " none yet"));
  else if (bAge < 26 * 3600) pills.push(pill("good", "Backup", ` ${fmtAgo(bAge)} · ${b.count} kept (${b.total_mb} MB)`));
  else pills.push(pill("bad", "Backup", ` last one ${fmtAgo(bAge)}`));

  $("#health").replaceChildren(...pills);
}

// ------------------------------------------------------------------ AI summary

// Minimal, safe Markdown: headings, bullet/numbered lists, paragraphs. Text only (no HTML).
function renderMarkdown(md) {
  const out = [];
  let list = null;
  const clean = (s) => s.replace(/\*\*|__|`/g, "").replace(/^\*(.+)\*$/, "$1");
  md.split(/\r?\n/).forEach((raw) => {
    const line = raw.trim();
    const bullet = line.match(/^[-*•]\s+(.*)/);
    const numbered = line.match(/^\d+[.)]\s+(.*)/);
    if (bullet || numbered) {
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag); out.push(list); }
      list.append(el("li", null, clean((bullet || numbered)[1])));
      return;
    }
    list = null;
    if (!line) return;
    const heading = line.match(/^#{1,6}\s+(.*)/);
    out.push(heading ? el("h3", null, clean(heading[1])) : el("p", null, clean(line)));
  });
  return out;
}

async function loadAI() {
  let ai;
  try {
    const res = await fetch("/api/ai", { cache: "no-store" });
    ai = await res.json();
    if (!res.ok) throw new Error(ai.error || res.status);
  } catch {
    return; // main error banner already covers "tracker not running"
  }
  const s = ai.summary;
  const gen = $("#generate");
  gen.disabled = !ai.groq_key;
  gen.title = ai.groq_key ? "" : "Add your Groq key first";
  if (s) {
    const when = new Date(s.created_ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    $("#ai-sub").textContent = `${s.start} to ${s.end} · written ${when} by ${s.model}`;
    $("#summary").replaceChildren(...renderMarkdown(s.text));
  } else {
    $("#ai-sub").textContent = ai.groq_key
      ? "No summary yet. One is written automatically each Monday for the previous week, or make one now."
      : "Add your Groq API key to data\\groq_key.txt to get weekly summaries.";
    $("#summary").replaceChildren();
  }
  const c = ai.categoriser;
  let cat;
  if (!ai.voyage_key) cat = "AI categoriser off: add your Voyage API key to data\\voyage_key.txt.";
  else if (c.last_error) cat = `AI categoriser: ${c.last_error}`;
  else cat = `AI categoriser (${c.model || "Voyage"}): ${c.pages_categorised} of ${c.pages_done} pages placed`
    + (c.pages_pending ? `, ${c.pages_pending} waiting` : "") + ". It only decides pages your rules can't.";
  $("#ai-cat").textContent = cat;
}

$("#preview").addEventListener("toggle", async (e) => {
  if (!e.target.open) return;
  try {
    const res = await fetch("/api/weekly/preview", { cache: "no-store" });
    const data = await res.json();
    $("#preview-json").textContent = JSON.stringify(data, null, 2);
  } catch {
    $("#preview-json").textContent = "Couldn't load the preview.";
  }
});

$("#generate").addEventListener("click", async () => {
  const btn = $("#generate");
  btn.disabled = true;
  btn.textContent = "Writing summary…";
  try {
    const res = await fetch("/api/weekly", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    await loadAI();
  } catch (err) {
    $("#ai-sub").textContent = `Couldn't write a summary: ${err.message}`;
  } finally {
    btn.textContent = "Summarise last 7 days";
    btn.disabled = false;
  }
});

// ------------------------------------------------------------------ trends

const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs, text) {
  const n = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
  if (text != null) n.textContent = text;
  return n;
}

function niceCount(max) {
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200].find((s) => max / s <= 4) || 500;
  const top = Math.max(step, Math.ceil(max / step) * step);
  const ticks = [];
  for (let v = 0; v <= top; v += step) ticks.push(v);
  return { top, ticks };
}

// One series, one axis. Gaps (null) break the line. Hover a day for its value.
function lineChart(container, points, { label, fmt, axis }) {
  const width = Math.max(240, Math.floor(container.clientWidth || 300));
  const height = 150, pl = 44, pr = 10, pt = 10, pb = 22;
  const vals = points.map((p) => p.value).filter((v) => v != null);
  const { top, ticks } = axis(Math.max(0, ...vals));
  const step = points.length > 1 ? (width - pl - pr) / (points.length - 1) : 0;
  const x = (i) => pl + i * step;
  const y = (v) => pt + (1 - v / top) * (height - pt - pb);
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": label });
  ticks.forEach((v) => {
    if (v > 0) svg.append(svgEl("line", { class: "grid", x1: pl, x2: width - pr, y1: y(v), y2: y(v) }));
    svg.append(svgEl("text", { x: pl - 6, y: y(v) + 4, "text-anchor": "end" }, fmt(v, true)));
  });
  svg.append(svgEl("line", { class: "base", x1: pl, x2: width - pr, y1: y(0), y2: y(0) }));
  let d = "", pen = false;
  points.forEach((p, i) => {
    if (p.value == null) { pen = false; return; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`;
    pen = true;
  });
  if (d) svg.append(svgEl("path", { class: "series", d }));
  const lastIdx = points.map((p) => p.value).findLastIndex((v) => v != null);
  if (lastIdx >= 0) svg.append(svgEl("circle", { class: "dot", cx: x(lastIdx), cy: y(points[lastIdx].value), r: 4 }));
  [0, Math.floor((points.length - 1) / 2), points.length - 1].forEach((i, n, arr) => {
    if (arr.indexOf(i) !== n) return;
    const anchor = n === 0 ? "start" : n === arr.length - 1 ? "end" : "middle";
    svg.append(svgEl("text", { x: x(i), y: height - 4, "text-anchor": anchor },
      parseDay(points[i].date).toLocaleDateString([], { month: "short", day: "numeric" })));
  });
  points.forEach((p, i) => {
    const hit = svgEl("rect", { class: "hit", x: x(i) - Math.max(step, 8) / 2, y: pt, width: Math.max(step, 8), height: height - pt - pb });
    setTip(hit, { title: parseDay(p.date).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" }),
      rows: [{ label, value: p.value == null ? "no data" : fmt(p.value) }] });
    svg.append(hit);
  });
  const wrap = el("div", "line");
  wrap.append(svg);
  container.replaceChildren(wrap);
}

function deltaCell(now, prev, kind, goodUp = true) {
  if (now == null || prev == null || (prev === 0 && now === 0)) return el("span", "delta same", "–");
  const diff = now - prev;
  const tiny = kind === "dur" ? Math.abs(diff) < 60 : Math.abs(diff) < (kind === "pct" ? 1 : 0.5);
  if (tiny) return el("span", "delta same", "no change");
  const better = (diff > 0) === goodUp;
  const span = el("span", `delta ${better ? "good" : "bad"}`);
  span.append(el("span", "ico", diff > 0 ? "▲" : "▼"));
  const mag = kind === "dur" ? fmtDur(Math.abs(diff)) : kind === "pct" ? `${Math.round(Math.abs(diff))} pts` : `${Math.round(Math.abs(diff) * 10) / 10}`;
  span.append(document.createTextNode(`${diff > 0 ? "+" : "−"}${mag} ${better ? "better" : "worse"}`));
  return span;
}

function renderTrends(t) {
  state.trends = t;
  const days = t.days;
  $("#trends-sub").textContent = `Last ${days.length} days, ending ${fmtShortDay(days[days.length - 1].date)}`;
  const durAxis = (m) => niceAxis(m, [900, 1800, 3600, 7200, 10800, 14400, 21600, 28800]);
  const durTick = (v) => (v === 0 ? "0" : fmtDur(v));
  lineChart($("#trend-productive"), days.map((d) => ({ date: d.date, value: d.active ? d.productive : null })),
    { label: "Productive", fmt: durTick, axis: durAxis });
  lineChart($("#trend-focus"), days.map((d) => ({ date: d.date, value: d.active ? d.longest_focus : null })),
    { label: "Longest focus", fmt: durTick, axis: (m) => niceAxis(m, [300, 600, 900, 1800, 3600, 7200]) });
  lineChart($("#trend-switches"), days.map((d) => ({ date: d.date, value: d.switches_per_hour })),
    { label: "Switches/hour", fmt: (v) => `${Math.round(v * 10) / 10}`, axis: niceCount });

  const w = t.this_week, l = t.last_week;
  const share = (p) => (p.active ? (100 * p.productive) / p.active : null);
  // Comparing against an empty week (e.g. before the tracker existed) would show fake "better".
  const noLast = !l.active;
  const cmp = (...args) => (noLast ? el("span", "delta same", "no data last week") : deltaCell(...args));
  const rows = [
    ["Active time", fmtDur(w.active), noLast ? "–" : fmtDur(l.active), cmp(w.active, l.active, "dur")],
    ["Productive time", fmtDur(w.productive), noLast ? "–" : fmtDur(l.productive), cmp(w.productive, l.productive, "dur")],
    ["Productive share", w.active ? `${Math.round(share(w))}%` : "–", l.active ? `${Math.round(share(l))}%` : "–",
      cmp(share(w), share(l), "pct")],
    ["Longest focus", fmtDur(w.longest_focus), noLast ? "–" : fmtDur(l.longest_focus), cmp(w.longest_focus, l.longest_focus, "dur")],
    ["Deep focus sessions", String(w.deep_count), noLast ? "–" : String(l.deep_count), cmp(w.deep_count, l.deep_count, "n")],
    ["Switches per hour", w.switches_per_hour ?? "–", l.switches_per_hour ?? "–",
      cmp(w.switches_per_hour, l.switches_per_hour, "n", false)],
  ];
  $("#week-compare").replaceChildren(table(
    [{ label: "" }, { label: `${fmtShortDay(w.start)}–${fmtShortDay(w.end)}`, num: true },
      { label: `${fmtShortDay(l.start)}–${fmtShortDay(l.end)}`, num: true }, { label: "Change" }], rows));
  $("#trends-table").replaceChildren(table(
    [{ label: "Day" }, { label: "Active", num: true }, { label: "Productive", num: true },
      { label: "Longest focus", num: true }, { label: "Switches/h", num: true }],
    days.map((d) => [d.date, fmtDur(d.active), fmtDur(d.productive), fmtDur(d.longest_focus), d.switches_per_hour ?? "–"])));
}

async function loadTrends(end) {
  if (state.trendsEnd === end && Date.now() - (state.trendsAt || 0) < 5 * 60 * 1000) return;
  try {
    const res = await fetch(`/api/trends?end=${end}&days=30`, { cache: "no-store" });
    const t = await res.json();
    if (!res.ok) throw new Error(t.error);
    state.trendsEnd = end;
    state.trendsAt = Date.now();
    renderTrends(t);
  } catch { /* the main error banner covers a dead tracker */ }
}

let resizeTimer;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state.trends) renderTrends(state.trends); }, 200);
});

// ------------------------------------------------------------------ forms (POST helper)

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function say(id, text, bad = false) {
  const m = $(id);
  m.textContent = text;
  m.className = bad ? "formmsg bad" : "formmsg";
}

// ------------------------------------------------------------------ offline time

function fillCategorySelect() {
  const sel = $("#offline-form").elements.category;
  if (sel.options.length || !state.data) return;
  state.data.categories.filter((c) => c !== "Unsure").forEach((c) => sel.append(new Option(c, c)));
  sel.value = state.data.productive.includes("Study") ? "Study" : sel.options[0].value;
}

async function loadOffline() {
  const today = new Date();
  const from = new Date(today); from.setDate(from.getDate() - 30);
  try {
    const res = await fetch(`/api/manual?start=${isoDay(from)}&end=${isoDay(today)}`, { cache: "no-store" });
    const { entries } = await res.json();
    if (!entries.length) { $("#offline-list").replaceChildren(empty("Nothing logged in the last 30 days.")); return; }
    $("#offline-list").replaceChildren(table(
      [{ label: "When" }, { label: "Length", num: true }, { label: "Category" }, { label: "Note", cls: "title" }, { label: "" }],
      entries.map((e) => {
        const del = el("button", "linkbtn", "Delete");
        del.type = "button";
        del.addEventListener("click", async () => {
          try { await post("/api/manual/delete", { id: e.id }); await loadOffline(); load(); }
          catch (err) { say("#offline-msg", err.message, true); }
        });
        const when = `${new Date(e.start * 1000).toLocaleDateString([], { month: "short", day: "numeric" })} ${fmtClock(e.start)}–${fmtClock(e.end)}`;
        return [when, fmtDur(e.end - e.start), catLabel(e.category), e.note, del];
      })));
  } catch { /* ignore */ }
}

$("#offline-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  try {
    await post("/api/manual", { date: f.date.value, start: f.start.value, end: f.end.value,
      category: f.category.value, note: f.note.value });
    say("#offline-msg", "Added.");
    f.note.value = "";
    await loadOffline();
    state.trendsAt = 0;
    load();
  } catch (err) {
    say("#offline-msg", `Couldn't add: ${err.message}`, true);
  }
});

// ------------------------------------------------------------------ email

async function loadEmail() {
  try {
    const res = await fetch("/api/email", { cache: "no-store" });
    const s = await res.json();
    const f = $("#email-form").elements;
    f.address.value = s.address || "";
    f.to.value = s.to && s.to !== s.address ? s.to : "";
    f.daily_enabled.checked = !!s.daily_enabled;
    f.daily_time.value = s.daily_time || "21:00";
    f.periodic_enabled.checked = !!s.periodic_enabled;
    f.periodic_days.value = s.periodic_days || 3;
    f.periodic_time.value = s.periodic_time || "21:00";
    f.weekly_enabled.checked = !!s.weekly_enabled;
    f.password.placeholder = s.has_password ? "saved · type a new one to replace it" : "16 letters from Google";
    $("#email-sub").textContent = s.configured
      ? `Sending from ${s.address} to ${s.to || s.address}.`
      : "Not set up yet: add your Gmail and an App Password.";
    $("#email-test").disabled = !s.configured;
  } catch { /* ignore */ }
}

$("#email-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  try {
    await post("/api/email/settings", {
      address: f.address.value.trim(), to: f.to.value.trim(), password: f.password.value,
      daily_enabled: f.daily_enabled.checked, daily_time: f.daily_time.value || "21:00",
      periodic_enabled: f.periodic_enabled.checked, periodic_days: Number(f.periodic_days.value || 3),
      periodic_time: f.periodic_time.value || "21:00",
      weekly_enabled: f.weekly_enabled.checked,
    });
    f.password.value = "";
    say("#email-msg", "Saved.");
    loadEmail();
  } catch (err) {
    say("#email-msg", `Couldn't save: ${err.message}`, true);
  }
});

$("#email-test").addEventListener("click", async () => {
  const btn = $("#email-test");
  btn.disabled = true;
  say("#email-msg", "Sending…");
  try {
    const r = await post("/api/email/test");
    say("#email-msg", `Sent to ${r.to}. Check your inbox (and spam, the first time).`);
  } catch (err) {
    say("#email-msg", err.message, true);
  } finally {
    btn.disabled = false;
  }
});

// ------------------------------------------------------------------ export & print

function updateExports(start, end) {
  ["activity", "daily", "searches"].forEach((k) => {
    $(`#export-${k}`).href = `/api/export?kind=${k}&start=${start}&end=${end}`;
  });
}
$("#print").addEventListener("click", () => { $("#export-menu").open = false; print(); });
document.addEventListener("click", (e) => {
  const menu = $("#export-menu");
  if (menu.open && !menu.contains(e.target)) menu.open = false;
});

// ------------------------------------------------------------------ data & controls

function rangeDates() {
  if (state.day) return [state.day, state.day];
  const today = new Date();
  const shift = (n) => { const x = new Date(today); x.setDate(x.getDate() - n); return isoDay(x); };
  switch (state.range) {
    case "yesterday": return [shift(1), shift(1)];
    case "7": return [shift(6), shift(0)];
    case "30": return [shift(29), shift(0)];
    default: return [shift(0), shift(0)];
  }
}

async function load() {
  clearTimeout(state.timer);
  const [start, end] = rangeDates();
  document.body.classList.add("loading"); // keep the old render visible, just dimmed
  try {
    const res = await fetch(`/api/summary?start=${start}&end=${end}&device=${state.device}`, { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    render(data);
    loadAI();
    loadHealth();
    updateExports(start, end);
    loadTrends(end);
    loadFocus();
    fillCategorySelect();
    $("#error").hidden = true;
    $("#updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    if (data.range.includes_today) state.timer = setTimeout(autoLoad, REFRESH_MS);
  } catch (err) {
    const msg = err instanceof TypeError
      ? "Can't reach the desktop tracker. Is it running? (pythonw desktop\\tracker.py)"
      : `Couldn't load data: ${err.message}`;
    $("#error").textContent = msg;
    $("#error").hidden = false;
    loadHealth();
    state.timer = setTimeout(autoLoad, REFRESH_MS);
  } finally {
    document.body.classList.remove("loading");
  }
}

// The periodic refresh re-draws tables; don't do it under you while you're picking a category.
function autoLoad() {
  if (document.activeElement?.matches?.("select.catsel")) { state.timer = setTimeout(autoLoad, 5000); return; }
  load();
}

function syncControls() {
  document.querySelectorAll(".seg button[data-range]").forEach((b) => {
    b.setAttribute("aria-pressed", String(!state.day && b.dataset.range === state.range));
  });
  document.querySelectorAll(".seg button[data-device]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.device === state.device));
  });
  $("#day").value = state.day || "";
}

document.querySelectorAll(".seg button[data-range]").forEach((b) => b.addEventListener("click", () => {
  state.range = b.dataset.range;
  state.day = null;
  syncControls();
  load();
}));
document.querySelectorAll(".seg button[data-device]").forEach((b) => b.addEventListener("click", () => {
  state.device = b.dataset.device;
  syncControls();
  load();
}));
$("#day").max = isoDay(new Date());
$("#day").addEventListener("change", (e) => {
  state.day = e.target.value || null;
  syncControls();
  load();
});

// Theme: system -> light -> dark, remembered per browser (best effort).
const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("#theme").textContent = `Theme: ${t}`;
}
let theme = "system";
try { theme = localStorage.getItem("got-theme") || "system"; } catch { /* storage blocked */ }
applyTheme(THEMES.includes(theme) ? theme : "system");
$("#theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
  try { localStorage.setItem("got-theme", theme); } catch { /* ignore */ }
});

// ------------------------------------------------------------------ search what you've read

function highlighted(text) {
  // Snippets mark matches with \u0002 … \u0003; build <mark> nodes (never HTML from data).
  const frag = document.createDocumentFragment();
  String(text || "").split("\u0002").forEach((chunk, i) => {
    if (i === 0) { frag.append(chunk.replace(/\u0003/g, "")); return; }
    const [hit, ...rest] = chunk.split("\u0003");
    frag.append(el("mark", null, hit), rest.join(""));
  });
  return frag;
}

function fmtAgoTs(ts) {
  if (!ts) return "";
  const days = Math.floor((Date.now() / 1000 - ts) / 86400);
  if (days < 1) return `today ${fmtClock(ts)}`;
  if (days < 2) return "yesterday";
  if (days < 30) return `${days} days ago`;
  return new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

async function runFind(q, meaning) {
  const out = $("#find-results");
  if (!q.trim()) { out.replaceChildren(); say("#find-msg", ""); return; }
  say("#find-msg", "Searching…");
  let r;
  try {
    const res = await fetch(`/api/find?q=${encodeURIComponent(q)}&meaning=${meaning ? 1 : 0}`, { cache: "no-store" });
    r = await res.json();
    if (!res.ok) throw new Error(r.error || `HTTP ${res.status}`);
  } catch (err) {
    say("#find-msg", `Search failed: ${err.message}`, true);
    return;
  }
  const n = r.pages.length;
  say("#find-msg", (n ? `${n} page${n > 1 ? "s" : ""}` : "No pages") +
    (r.searches.length ? ` · ${r.searches.length} past search${r.searches.length > 1 ? "es" : ""}` : "") +
    (r.note ? ` · ${r.note}` : ""));
  const parts = [];
  if (n) {
    const list = el("ul", "results");
    r.pages.forEach((p) => {
      const li = el("li");
      const a = el("a", "t", p.title || p.url);
      a.href = p.url; a.target = "_blank"; a.rel = "noreferrer noopener";
      const meta = el("div", "meta");
      const bits = [p.domain, p.author, fmtAgoTs(p.last_seen), p.seconds ? `${fmtDur(p.seconds)} spent` : "",
        p.scroll != null ? `read ${Math.round(p.scroll)}%` : ""].filter(Boolean);
      meta.append(bits.join(" · "));
      p.match.forEach((m) => meta.append(el("span", "badge", m === "words" ? "words match" : "similar meaning")));
      li.append(a, meta);
      if (p.snippet) { const s = el("div", "snip"); s.append(highlighted(p.snippet)); li.append(s); }
      list.append(li);
    });
    parts.push(list);
  }
  if (r.searches.length) {
    parts.push(el("h3", "mini", "Your past searches"));
    parts.push(table([{ label: "When" }, { label: "Where" }, { label: "Search", cls: "title" }],
      r.searches.map((s) => [fmtWhen(s.ts, true), s.engine, s.query])));
  }
  out.replaceChildren(...parts);
}

$("#find-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  runFind(f.q.value, f.meaning.checked);
});

// ------------------------------------------------------------------ focus mode

const focusState = { data: null, timer: null };

function fmtLeft(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function resultCell(s) {
  const kinds = { completed: ["good", "✓", "Finished"], stopped: ["warn", "!", "Ended early"], running: ["run", "●", "Running"] };
  const [cls, ico, label] = kinds[s.result] || kinds.running;
  const span = el("span", `result ${cls}`);
  span.append(el("span", "ico", ico), document.createTextNode(label));
  return span;
}

function renderFocus() {
  const f = focusState.data;
  if (!f) return;
  const cur = f.current;
  clearInterval(focusState.timer);
  $("#focus-form").hidden = !!cur;
  $("#focus-sub").textContent = `${f.settings.block.length} sites blocked during focus`;

  if (cur) {
    const live = el("div", "focus-live");
    const left = el("div", "left");
    const what = el("div", "what");
    const running = (f.history.sessions || []).find((s) => s.id === cur.id);
    what.append(el("b", null, cur.label || "Focus session"), el("br"),
      `Until ${fmtClock(cur.end)}` + (running?.blocked ? ` · ${running.blocked} blocked site visit${running.blocked > 1 ? "s" : ""} so far` : ""));
    const stop = el("button", "ghost", "End early");
    stop.type = "button";
    let armed = false;
    stop.addEventListener("click", async () => {
      if (!armed) { armed = true; stop.textContent = "Sure? Click again"; setTimeout(() => { armed = false; stop.textContent = "End early"; }, 5000); return; }
      try { await post("/api/focus/stop"); say("#focus-msg", "Ended. Brave unblocks within 30 seconds."); }
      catch (err) { say("#focus-msg", err.message, true); }
      loadFocus();
    });
    live.append(left, what, stop);
    $("#focus-now").replaceChildren(live);
    const tick = () => {
      const s = cur.end - Date.now() / 1000;
      if (s <= 0) { clearInterval(focusState.timer); loadFocus(); return; }
      left.textContent = fmtLeft(s);
    };
    tick();
    focusState.timer = setInterval(tick, 1000);
  } else {
    $("#focus-now").replaceChildren();
    const mins = $("#focus-form").elements.minutes;
    if (!mins.value) mins.value = f.settings.minutes;
  }

  const h = f.history;
  const done = h.completed + h.stopped;
  if (h.sessions.length) {
    const stat = (label, value) => { const s = el("div", "stat"); s.append(el("div", "label", label), el("div", "value", value)); return s; };
    const row = el("div", "statrow");
    row.append(
      stat("Finished (14 days)", `${h.completed} of ${done}`),
      stat("Focused time", fmtDur(h.focused_seconds)),
      stat("On task during focus", h.focused_seconds ? `${pct(h.productive_seconds, h.focused_seconds)}%` : "–"),
      stat("Blocked site visits", String(h.blocked)),
    );
    $("#focus-stats").replaceChildren(row);
    $("#focus-history").replaceChildren(table(
      [{ label: "When" }, { label: "Working on", cls: "title" }, { label: "Planned", num: true }, { label: "Lasted", num: true },
        { label: "Result" }, { label: "On task", num: true }, { label: "Blocked" }],
      h.sessions.slice(0, 15).map((s) => [
        fmtWhen(s.start, true), s.label || "–", fmtDur(s.planned_seconds), fmtDur(s.seconds), resultCell(s),
        s.active_seconds ? `${pct(s.productive_seconds, s.seconds)}%` : "–",
        s.blocked ? s.blocked_sites.map((b) => `${b.domain} ×${b.count}`).join(", ") : "–",
      ])));
  } else {
    $("#focus-stats").replaceChildren();
    $("#focus-history").replaceChildren(empty("No focus sessions yet. Start one above or from the extension's popup."));
  }
  if (document.activeElement !== $("#adult-mode")) $("#adult-mode").value = f.settings.adult || "focus";
  const ta = $("#focus-sites-form").elements.block;
  if (document.activeElement !== ta && !ta.dataset.dirty) ta.value = f.settings.block.join("\n");
}

async function loadFocus() {
  try {
    const res = await fetch("/api/focus", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    focusState.data = data;
    renderFocus();
  } catch { /* main banner covers a dead tracker */ }
}

async function startFocus(minutes) {
  const f = $("#focus-form").elements;
  try {
    await post("/api/focus/start", { minutes: Number(minutes), label: f.label.value.trim() });
    f.label.value = "";
    const ext = state.health?.extension;
    const old = ext?.version && ext.version.localeCompare("0.6.0", undefined, { numeric: true }) < 0;
    say("#focus-msg", old
      ? "Started, but the extension is too old to block sites: reload it in brave://extensions."
      : "Started. Brave starts blocking within 30 seconds (instantly if you start from the extension).", old);
  } catch (err) {
    say("#focus-msg", `Couldn't start: ${err.message}`, true);
  }
  loadFocus();
}

document.querySelectorAll("#focus-form [data-min]").forEach((b) =>
  b.addEventListener("click", () => startFocus(b.dataset.min)));
$("#focus-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  startFocus(ev.target.elements.minutes.value || 50);
});
$("#adult-mode").addEventListener("change", async (e) => {
  try {
    await post("/api/focus/settings", { adult: e.target.value });
    say("#focus-msg", { focus: "Adult sites are blocked during focus sessions.",
      always: "Adult sites are blocked all the time. Brave picks this up within 30 seconds.",
      off: "Adult sites aren't blocked." }[e.target.value]);
    loadFocus();
  } catch (err) { say("#focus-msg", `Couldn't save: ${err.message}`, true); }
});
$("#focus-sites-form").elements.block.addEventListener("input", (e) => { e.target.dataset.dirty = "1"; });
$("#focus-sites-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ta = ev.target.elements.block;
  try {
    const r = await post("/api/focus/settings", { block: ta.value });
    delete ta.dataset.dirty;
    ta.value = r.settings.block.join("\n");
    say("#focus-msg", `Saved ${r.settings.block.length} sites.`);
    loadFocus();
  } catch (err) {
    say("#focus-msg", `Couldn't save: ${err.message}`, true);
  }
});

// ------------------------------------------------------------------ distraction alerts

async function loadNudge() {
  try {
    const res = await fetch("/api/nudge", { cache: "no-store" });
    const n = await res.json();
    if (!res.ok) throw new Error(n.error);
    const s = n.settings;
    const f = $("#nudge-form").elements;
    const box = $("#nudge-form");
    if (!box.contains(document.activeElement)) {
      f.enabled.checked = s.enabled;
      f.minutes.value = s.minutes;
      f.repeat.value = s.repeat;
      f.snooze.value = s.snooze;
      const cats = (state.data?.categories || s.categories).filter((c) => c !== "Unsure");
      $("#nudge-cats").replaceChildren(...cats.map((c) => {
        const l = el("label", "check");
        const cb = el("input");
        cb.type = "checkbox";
        cb.name = "cat";
        cb.value = c;
        cb.checked = s.categories.includes(c);
        l.append(cb, swatch(c), document.createTextNode(c));
        return l;
      }));
    }
    let sum = s.enabled ? `· after ${s.minutes} min of ${s.categories.join(" / ")}` : "· off";
    if (n.snoozed_until && n.snoozed_until * 1000 > Date.now()) sum += ` · snoozed until ${fmtClock(n.snoozed_until)}`;
    $("#nudge-summary").textContent = sum;
  } catch { /* the focus card already reports a dead server */ }
}

$("#nudge-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  const categories = [...ev.target.querySelectorAll("input[name=cat]:checked")].map((c) => c.value);
  try {
    await post("/api/nudge/settings", { enabled: f.enabled.checked, minutes: f.minutes.value,
      repeat: f.repeat.value, snooze: f.snooze.value, categories });
    say("#nudge-msg", "Saved.");
    document.activeElement?.blur();
    loadNudge();
  } catch (err) { say("#nudge-msg", `Couldn't save: ${err.message}`, true); }
});
$("#nudge-snooze").addEventListener("click", async () => {
  try {
    const r = await post("/api/nudge/snooze", {});
    say("#nudge-msg", `No alerts until ${fmtClock(r.snoozed_until)}.`);
    loadNudge();
  } catch (err) { say("#nudge-msg", err.message, true); }
});

// ------------------------------------------------------------------ phone

let phoneTimer = null;
async function loadPhone() {
  clearTimeout(phoneTimer);
  let p;
  try {
    const res = await fetch("/api/phone", { cache: "no-store" });
    p = await res.json();
    if (!res.ok) throw new Error(p.error);
  } catch { phoneTimer = setTimeout(loadPhone, 15000); return; }

  const pend = p.pending.map((r) => {
    const box = el("div", "pair-request");
    box.append(el("div", null, `${r.name} wants to connect`),
      el("div", "pair-code", r.code),
      el("div", "scope", `Allow only if the phone shows the same code · from ${r.ip}`));
    const yes = el("button", "primary small", "Allow");
    const no = el("button", "linkbtn", "Deny");
    yes.type = no.type = "button";
    const decide = async (allow) => {
      yes.disabled = no.disabled = true;
      try {
        await post("/api/phone/decide", { id: r.id, allow });
        say("#phone-msg", allow ? `${r.name} is connected. It sends its first data within a minute.` : "Denied.");
      } catch (err) { say("#phone-msg", err.message, true); }
      loadPhone();
    };
    yes.addEventListener("click", () => decide(true));
    no.addEventListener("click", () => decide(false));
    const actions = el("div", "suggest-actions");
    actions.append(yes, no);
    box.append(actions);
    return box;
  });
  $("#phone-pending").replaceChildren(...pend);

  if (!p.devices.length) {
    $("#phone-devices").replaceChildren(empty("No phone connected yet."));
  } else {
    $("#phone-devices").replaceChildren(table(
      [{ label: "Phone", cls: "title" }, { label: "Last sync" }, { label: "" }],
      p.devices.map((d) => {
        const rm = el("button", "linkbtn", "Disconnect");
        rm.type = "button";
        rm.addEventListener("click", async () => {
          if (!confirm(`Disconnect ${d.name}? Its data stays; it just stops syncing until you connect it again.`)) return;
          try { await post("/api/phone/unpair", { id: d.id }); loadPhone(); }
          catch (err) { say("#phone-msg", err.message, true); }
        });
        let last = d.last_sync ? fmtWhen(d.last_sync, Date.now() / 1000 - d.last_sync > 20 * 3600) : "not yet";
        if (d.last_sync && Date.now() / 1000 - d.last_sync > 3600) last += " (not reachable since)";
        return [d.name + (d.version ? ` · app ${d.version}` : ""), last, rm];
      })));
  }
  // Poll quickly while a pairing request is waiting, slowly otherwise.
  phoneTimer = setTimeout(loadPhone, p.pending.length ? 2000 : 15000);
}

$("#offline-form").elements.date.value = isoDay(new Date());
$("#offline-form").elements.date.max = isoDay(new Date());

syncControls();
load();
loadOffline();
loadEmail();
loadRecentRules();
loadFocus();
loadNudge();
loadPhone();
