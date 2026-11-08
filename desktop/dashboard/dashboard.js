// Good ol Tracker dashboard. Plain DOM, no libraries; all data from /api/summary.
"use strict";

const REFRESH_MS = 30000;
const $ = (sel) => document.querySelector(sel);

const state = { range: "today", day: null, data: null, timer: null };

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
  const parts = [];
  if (now.desktop) parts.push(`${now.desktop.category} · ${now.desktop.app}`);
  if (now.browser) parts.push(`${now.browser.app} (${now.browser.category})`);
  out.textContent = `Right now: ${parts.join(" · on ")}`;
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

function renderApps(d) {
  if (!d.top_apps.length) { $("#apps").replaceChildren(empty("Nothing yet.")); return; }
  const max = d.top_apps[0].seconds;
  const grid = el("div", "bars thin");
  d.top_apps.forEach((a) => {
    const name = el("div", "name");
    name.append(swatch(a.category), el("span", null, a.name));
    name.append(el("span", "tag", a.kind));
    const track = el("div", "track");
    const bar = el("div", "bar");
    bar.style.width = `${(a.seconds / max) * 100}%`;
    bar.style.background = colorFor(a.category);
    track.append(bar);
    const val = el("div", "val");
    val.append(el("b", null, fmtDur(a.seconds)));
    const tip = { title: a.name, rows: [{ cat: a.category, label: a.category, value: fmtDur(a.seconds) }] };
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
      return [link, p.domain, fmtDur(p.seconds), read, catLabel(p.category)];
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
  const card = $("#rules-card");
  card.hidden = !d.unmatched.length && !d.unsure.length;
  if (card.hidden) return;
  const parts = [];
  if (d.unmatched.length) {
    parts.push(el("p", "sub", "Not categorised yet: add under \"apps\" or \"sites\""));
    parts.push(table([{ label: "Name" }, { label: "Type" }, { label: "Time", num: true }],
      d.unmatched.map((u) => [u.name, u.kind, fmtDur(u.seconds)])));
  }
  if (d.unsure.length) {
    parts.push(el("p", "sub", "Unsure: add a channel rule or keywords"));
    parts.push(table([{ label: "Site" }, { label: "Title", cls: "title" }, { label: "Time", num: true }],
      d.unsure.map((u) => [u.site, u.title, fmtDur(u.seconds)])));
  }
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
    const res = await fetch(`/api/summary?start=${start}&end=${end}`, { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    render(data);
    loadAI();
    $("#error").hidden = true;
    $("#updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    if (data.range.includes_today) state.timer = setTimeout(load, REFRESH_MS);
  } catch (err) {
    const msg = err instanceof TypeError
      ? "Can't reach the desktop tracker. Is it running? (pythonw desktop\\tracker.py)"
      : `Couldn't load data: ${err.message}`;
    $("#error").textContent = msg;
    $("#error").hidden = false;
    state.timer = setTimeout(load, REFRESH_MS);
  } finally {
    document.body.classList.remove("loading");
  }
}

function syncControls() {
  document.querySelectorAll(".seg button").forEach((b) => {
    b.setAttribute("aria-pressed", String(!state.day && b.dataset.range === state.range));
  });
  $("#day").value = state.day || "";
}

document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  state.range = b.dataset.range;
  state.day = null;
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

syncControls();
load();
