// Calendar component: Day (24h) / Week / Month views, drag to create, drag to move, drag the bottom edge to resize.
// Times are always shown in the user's configured timezone (not necessarily the browser's).
"use strict";

const SNAP = 15;           // minutes
const LONG_PRESS = 380;    // ms before a touch turns into a drag (so normal scrolling still works)
const DAY_MIN = 1440;
const PRIORITY = { P1: "Urgent", P2: "High", P3: "Normal", P4: "Low" };
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const pad = (n) => String(n).padStart(2, "0");
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export function createCalendar(root, deps) {
  // deps: { api, getTz, onOpen(item), onCreate({date, start, end}), onError(msg), onChanged() }
  const store = { get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
                  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } } };
  const S = {
    view: ["day", "week", "month"].includes(store.get("cal_view")) ? store.get("cal_view") : "day",
    anchor: null, items: [], zoom: +store.get("cal_zoom") || 64, range: null, scrolledKey: "", loading: false,
  };

  // ---------------------------------------------------------------- dates in the user's timezone
  let fmtCache = {};
  function parts(iso) {
    const tz = deps.getTz();
    const f = fmtCache[tz] || (fmtCache[tz] = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }));
    const p = Object.fromEntries(f.formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
    return { date: `${p.year}-${p.month}-${p.day}`, min: (+p.hour % 24) * 60 + +p.minute };
  }
  const today = () => parts(new Date().toISOString()).date;
  const addDays = (d, n) => { const t = new Date(d + "T00:00:00Z"); t.setUTCDate(t.getUTCDate() + n); return t.toISOString().slice(0, 10); };
  const weekday = (d) => (new Date(d + "T00:00:00Z").getUTCDay() + 6) % 7; // Monday = 0
  const weekStart = (d) => addDays(d, -weekday(d));
  const monthStart = (d) => d.slice(0, 8) + "01";
  const naive = (d, m) => { if (m >= DAY_MIN) { d = addDays(d, 1); m -= DAY_MIN; } return `${d}T${pad(Math.floor(m / 60))}:${pad(m % 60)}:00`; };
  const label = (m) => { const h = Math.floor(m / 60) % 24, mm = m % 60; return `${h % 12 || 12}${mm ? ":" + pad(mm) : ""}${h < 12 ? " AM" : " PM"}`; };
  const title = (d, o) => new Date(d + "T12:00:00Z").toLocaleDateString([], { timeZone: "UTC", ...o });
  // S.anchor (the date being looked at) is set on first use, once the user's timezone is known

  // ---------------------------------------------------------------- skeleton
  root.classList.add("cal");
  const bar = el("div", "cal-bar");
  const nav = el("div", "row");
  const prev = el("button", "icon-btn sm", "‹"), next = el("button", "icon-btn sm", "›"), todayBtn = el("button", "btn small", "Today");
  prev.setAttribute("aria-label", "Previous"); next.setAttribute("aria-label", "Next");
  const heading = el("h2", "cal-title");
  nav.append(prev, todayBtn, next, heading);
  const tools = el("div", "row");
  const seg = el("span", "seg"); seg.setAttribute("role", "group"); seg.setAttribute("aria-label", "Calendar view");
  const viewBtns = {};
  for (const v of ["day", "week", "month"]) { const b = el("button", null, v[0].toUpperCase() + v.slice(1)); b.onclick = () => setView(v); viewBtns[v] = b; seg.appendChild(b); }
  const zoomOut = el("button", "icon-btn sm", "−"), zoomIn = el("button", "icon-btn sm", "+");
  zoomOut.title = "Zoom out"; zoomIn.title = "Zoom in";
  const zoomBox = el("span", "row zoom"); zoomBox.append(zoomOut, zoomIn);
  const addBtn = el("button", "btn small primary", "+ Add");
  addBtn.title = "Add something at the next free hour";
  addBtn.onclick = () => {
    const n = parts(new Date().toISOString());
    const d = S.view === "day" ? S.anchor : n.date;
    const start = d === n.date ? Math.min(DAY_MIN - 60, Math.ceil((n.min + 1) / 30) * 30) : 9 * 60;
    deps.onCreate({ date: d, start, end: start + 60 });
  };
  tools.append(seg, zoomBox, addBtn);
  bar.append(nav, tools);
  const legend = el("div", "cal-legend");
  for (const [p, name] of Object.entries(PRIORITY)) { const c = el("span", "lg"); const d = el("i"); d.style.background = `var(--${p.toLowerCase()})`; c.append(d, document.createTextNode(name)); legend.appendChild(c); }
  legend.append(el("span", "lg", "🔁 Routine"), el("span", "lg", "⏰ Reminder"), el("span", "lg", "📌 Placed by you"), el("span", "lg hint", "Drag on empty time to add · drag items to move"));
  const body = el("div", "cal-body");
  root.append(bar, legend, body);

  prev.onclick = () => shift(-1); next.onclick = () => shift(1);
  todayBtn.onclick = () => { S.anchor = today(); S.scrolledKey = ""; refresh(); };
  zoomIn.onclick = () => setZoom(S.zoom + 16); zoomOut.onclick = () => setZoom(S.zoom - 16);

  function setZoom(z) { S.zoom = clamp(z, 32, 160); store.set("cal_zoom", S.zoom); render(); }
  function shift(dir) {
    if (S.view === "day") S.anchor = addDays(S.anchor, dir);
    else if (S.view === "week") S.anchor = addDays(S.anchor, 7 * dir);
    else { const [y, m] = S.anchor.split("-").map(Number); const d = new Date(Date.UTC(y, m - 1 + dir, 1)); S.anchor = d.toISOString().slice(0, 10); }
    S.scrolledKey = ""; refresh();
  }
  function setView(v) { S.view = v; store.set("cal_view", v); S.scrolledKey = ""; refresh(); }
  function openDay(d) { S.anchor = d; setView("day"); }

  function visibleDays() {
    if (!S.anchor) S.anchor = today();
    if (S.view === "day") return [S.anchor];
    if (S.view === "week") { const s = weekStart(S.anchor); return [0, 1, 2, 3, 4, 5, 6].map((i) => addDays(s, i)); }
    const s = weekStart(monthStart(S.anchor));
    return Array.from({ length: 42 }, (_, i) => addDays(s, i));
  }

  // ---------------------------------------------------------------- data
  async function refresh() {
    const days = visibleDays();
    const range = [days[0], days[days.length - 1]];
    S.loading = true;
    try {
      const data = await deps.api(`/calendar?start=${range[0]}&end=${range[1]}`);
      S.items = data.items; S.range = range;
    } catch (e) { deps.onError(e.message); }
    S.loading = false;
    render();
  }

  function progress(it) {
    if (it.task_status === "DONE" || it.status === "DONE") return 1;
    const now = Date.now(), s = Date.parse(it.start), e = Date.parse(it.end);
    return now <= s ? 0 : now >= e ? 1 : (now - s) / (e - s);
  }
  const draggable = (it) => (it.type === "block" && it.status === "PLANNED" && it.task_status !== "DONE") || (it.type === "reminder" && it.task_status !== "DONE");
  function color(it) {
    if (it.routine_id) return "var(--routine)";
    if (it.type === "reminder") return "var(--reminder)";
    return `var(--${(it.priority || "P3").toLowerCase()})`;
  }

  // ---------------------------------------------------------------- render
  function render() {
    heading.textContent = S.view === "day" ? title(S.anchor, { weekday: "long", day: "numeric", month: "long" })
      : S.view === "week" ? (() => { const d = visibleDays(); return `${title(d[0], { day: "numeric", month: "short" })} – ${title(d[6], { day: "numeric", month: "short", year: "numeric" })}`; })()
      : title(S.anchor, { month: "long", year: "numeric" });
    for (const [v, b] of Object.entries(viewBtns)) b.setAttribute("aria-pressed", v === S.view);
    zoomBox.hidden = S.view === "month";
    todayBtn.disabled = S.view === "month" ? S.anchor.slice(0, 7) === today().slice(0, 7) : visibleDays().includes(today());
    body.replaceChildren(S.view === "month" ? renderMonth() : renderTimeGrid(visibleDays()));
  }

  function itemsFor(day) {
    const out = [];
    for (const it of S.items) {
      const a = parts(it.start), b = parts(it.end);
      if (it.type === "reminder" || it.type === "deadline") { if (a.date === day) out.push({ it, s: a.min, e: a.min, pin: true }); continue; }
      if (a.date > day || b.date < day || (b.date === day && b.min === 0 && a.date !== day)) continue;
      const s = a.date < day ? 0 : a.min, e = b.date > day ? DAY_MIN : b.min;
      out.push({ it, s, e: Math.max(e, s + 15) });
    }
    return out;
  }
  function lanes(list) {
    list.sort((a, b) => a.s - b.s || b.e - a.e);
    let cluster = [], end = -1;
    const flush = () => { const ends = []; for (const x of cluster) { let i = ends.findIndex((v) => v <= x.s); if (i < 0) { i = ends.length; ends.push(0); } ends[i] = x.e; x.lane = i; } cluster.forEach((x) => (x.lanes = ends.length)); cluster = []; };
    for (const x of list) { if (cluster.length && x.s >= end) { flush(); end = -1; } cluster.push(x); end = Math.max(end, x.e); }
    if (cluster.length) flush();
    return list;
  }

  function renderTimeGrid(days) {
    const pxPerMin = (S.view === "week" ? S.zoom * 0.8 : S.zoom) / 60;
    const wrap = el("div", "tg" + (S.view === "week" ? " week" : ""));
    wrap.style.setProperty("--hour", pxPerMin * 60 + "px");
    // day headers (week)
    if (S.view === "week") {
      const head = el("div", "tg-head");
      head.appendChild(el("div", "tg-gutter"));
      for (const d of days) {
        const h = el("button", "tg-day" + (d === today() ? " today" : ""));
        h.append(el("span", null, WEEKDAYS[weekday(d)]), el("b", null, String(+d.slice(8))));
        h.onclick = () => openDay(d); h.title = "Open day";
        head.appendChild(h);
      }
      wrap.appendChild(head);
    }
    const scroll = el("div", "tg-scroll");
    const grid = el("div", "tg-grid"); grid.style.height = DAY_MIN * pxPerMin + "px";
    const gutter = el("div", "tg-gutter");
    for (let h = 0; h < 24; h++) { const l = el("span", "tg-hour", h ? label(h * 60) : ""); l.style.top = h * 60 * pxPerMin + "px"; gutter.appendChild(l); }
    grid.appendChild(gutter);
    const cols = el("div", "tg-cols");
    const nowP = parts(new Date().toISOString());
    for (const d of days) {
      const col = el("div", "tg-col" + (d === nowP.date ? " today" : ""));
      col.dataset.date = d;
      for (const x of lanes(itemsFor(d).filter((x) => !x.pin))) col.appendChild(renderBlock(x, pxPerMin, d));
      for (const x of itemsFor(d).filter((x) => x.pin)) col.appendChild(renderPin(x, pxPerMin));
      if (d === nowP.date) {
        const line = el("div", "tg-now"); line.style.top = nowP.min * pxPerMin + "px";
        if (S.view === "day") line.appendChild(el("span", null, label(nowP.min)));
        col.appendChild(line);
      }
      attachCreate(col, pxPerMin, scroll);
      cols.appendChild(col);
    }
    grid.appendChild(cols);
    scroll.appendChild(grid);
    wrap.appendChild(scroll);
    // first show: scroll so "now" (or 8 AM) sits near the top third
    const key = S.view + S.anchor;
    requestAnimationFrame(() => {
      if (S.scrolledKey === key) { scroll.scrollTop = S.lastScroll || 0; return; }
      S.scrolledKey = key;
      const focus = days.includes(nowP.date) ? nowP.min : 8 * 60;
      scroll.scrollTop = Math.max(0, focus * pxPerMin - scroll.clientHeight / 3);
    });
    scroll.addEventListener("scroll", () => (S.lastScroll = scroll.scrollTop), { passive: true });
    return wrap;
  }

  function renderBlock(x, pxPerMin, day) {
    const it = x.it, p = progress(it), live = p > 0 && p < 1;
    const done = it.task_status === "DONE" || it.status === "DONE";
    const missed = it.kind === "WORK" && p >= 1 && !done;
    const card = el("button", "ev" + (live ? " live" : "") + (done ? " done" : "") + (missed ? " missed" : "") + (it.type === "routine" ? " ghosted" : "") + (draggable(it) ? " can-drag" : ""));
    card.type = "button";
    card.style.setProperty("--c", color(it));
    card.style.setProperty("--fill", p);
    card.style.top = x.s * pxPerMin + "px";
    card.style.height = Math.max(20, (x.e - x.s) * pxPerMin - 2) + "px";
    card.style.left = `calc(${(x.lane / x.lanes) * 100}% + 1px)`;
    card.style.width = `calc(${100 / x.lanes}% - 3px)`;
    const icon = it.routine_id ? "🔁 " : it.kind === "EVENT" ? "📅 " : it.pinned ? "📌 " : "";
    card.appendChild(el("i", "fill"));
    card.appendChild(el("span", "t", icon + it.title));
    if ((x.e - x.s) * pxPerMin > 34) {
      const w = el("span", "w", `${label(parts(it.start).min)} – ${label(parts(it.end).min)}`);
      if (live) w.appendChild(el("b", "pct", ` · ${Math.round(p * 100)}%`));
      card.appendChild(w);
    }
    if (draggable(it) && (x.e - x.s) * pxPerMin > 26) { const rz = el("span", "rz"); rz.setAttribute("aria-hidden", "true"); card.appendChild(rz); }
    card.setAttribute("aria-label", `${it.title}, ${label(x.s)} to ${label(x.e)}, ${PRIORITY[it.priority] || ""} priority${live ? ", " + Math.round(p * 100) + "% done" : ""}`);
    card.dataset.id = it.id;
    attachItemDrag(card, x, pxPerMin, day);
    return card;
  }

  function renderPin(x, pxPerMin) {
    const it = x.it, done = it.task_status === "DONE";
    const pin = el("button", "pin2" + (done ? " done" : "") + (it.type === "deadline" ? " due" : "") + (draggable(it) ? " can-drag" : ""),
      (it.type === "deadline" ? "⏳ " : "⏰ ") + it.title);
    pin.type = "button"; pin.style.top = x.s * pxPerMin + "px"; pin.style.setProperty("--c", color(it));
    pin.title = `${it.type === "deadline" ? "Due" : "Reminder"} at ${label(x.s)}`;
    attachItemDrag(pin, x, pxPerMin, parts(it.start).date);
    return pin;
  }

  function renderMonth() {
    const days = visibleDays(), month = S.anchor.slice(0, 7), t = today();
    const wrap = el("div", "mv");
    const head = el("div", "mv-head");
    WEEKDAYS.forEach((w) => head.appendChild(el("span", null, w)));
    wrap.appendChild(head);
    const grid = el("div", "mv-grid");
    const byDay = {};
    for (const it of S.items) { const d = parts(it.start).date; (byDay[d] = byDay[d] || []).push(it); }
    for (const d of days) {
      const cell = el("div", "mv-cell" + (d.slice(0, 7) !== month ? " other" : "") + (d === t ? " today" : ""));
      cell.tabIndex = 0;
      const top = el("div", "mv-top");
      top.appendChild(el("b", null, String(+d.slice(8))));
      const add = el("button", "mv-add", "+"); add.title = "Add on this day"; add.setAttribute("aria-label", "Add on " + d);
      add.onclick = (e) => { e.stopPropagation(); deps.onCreate({ date: d, start: 9 * 60, end: 10 * 60 }); };
      top.appendChild(add);
      cell.appendChild(top);
      const list = (byDay[d] || []).slice().sort((a, b) => a.start.localeCompare(b.start));
      const shown = list.length > 3 ? 2 : 3;  // a busy day shows two items and "+N more", so nothing overlaps
      for (const it of list.slice(0, shown)) {
        const chip = el("span", "mv-chip" + (it.task_status === "DONE" ? " done" : "") + (it.type === "routine" ? " ghosted" : ""));
        chip.style.setProperty("--c", color(it));
        chip.textContent = `${label(parts(it.start).min).replace(" ", "").toLowerCase()} ${it.title}`;
        cell.appendChild(chip);
      }
      if (list.length > shown) cell.appendChild(el("span", "mv-more", `+${list.length - shown} more`));
      cell.onclick = () => openDay(d);
      cell.onkeydown = (e) => { if (e.key === "Enter") openDay(d); };
      grid.appendChild(cell);
    }
    wrap.appendChild(grid);
    return wrap;
  }

  // ---------------------------------------------------------------- dragging
  // Mouse: drag starts after a few pixels. Touch/pen: press and hold, then drag (a quick swipe still scrolls).
  let drag = null;
  const minuteAt = (col, clientY, pxPerMin) => clamp(Math.round(((clientY - col.getBoundingClientRect().top) / pxPerMin) / SNAP) * SNAP, 0, DAY_MIN);
  function colAt(clientX, clientY) { const e = document.elementFromPoint(clientX, clientY); return e && e.closest ? e.closest(".tg-col") : null; }
  function ghost(col, s, e, text) {
    let g = col.querySelector(".tg-ghost");
    if (!g) { document.querySelectorAll(".tg-ghost").forEach((x) => x.remove()); g = el("div", "tg-ghost"); col.appendChild(g); }
    const px = drag.pxPerMin;
    g.style.top = s * px + "px"; g.style.height = Math.max(14, (e - s) * px) + "px";
    g.textContent = text;
    return g;
  }
  function autoScroll(clientY) {
    const sc = body.querySelector(".tg-scroll"); if (!sc) return;
    const r = sc.getBoundingClientRect(), zone = 32;
    if (Math.abs(clientY - drag.startY) < 12) return;  // don't scroll just because a drag *started* near the edge
    const speed = (d) => Math.ceil(((zone - d) / zone) * 10); // faster the closer you get to the edge
    if (clientY < r.top + zone) sc.scrollTop -= speed(clientY - r.top);
    else if (clientY > r.bottom - zone) sc.scrollTop += speed(r.bottom - clientY);
  }
  function begin(kind, ev, extra) {
    drag = { kind, pointerId: ev.pointerId, startY: ev.clientY, startX: ev.clientX, active: false, timer: null, ...extra };
    if (ev.pointerType === "mouse") return;
    drag.timer = setTimeout(() => { if (drag) { drag.active = true; navigator.vibrate && navigator.vibrate(12); onMove(drag.lastEv || ev); } }, LONG_PRESS);
  }
  function attachCreate(col, pxPerMin) {
    col.addEventListener("pointerdown", (ev) => {
      if (ev.target !== col || ev.button > 0) return;
      const m = minuteAt(col, ev.clientY, pxPerMin);
      begin("create", ev, { col, pxPerMin, anchorMin: Math.floor(m / SNAP) * SNAP, date: col.dataset.date });
    });
  }
  function attachItemDrag(node, x, pxPerMin, day) {
    node.addEventListener("pointerdown", (ev) => {
      if (ev.button > 0) return;
      const mode = ev.target.classList.contains("rz") ? "resize" : "move";
      begin(mode, ev, { node, x, pxPerMin, date: day, grabOffset: minuteAt(node.closest(".tg-col"), ev.clientY, pxPerMin) - x.s });
      ev.stopPropagation();
    });
    node.addEventListener("click", (ev) => { if (node.dataset.justDragged) { ev.preventDefault(); delete node.dataset.justDragged; return; } deps.onOpen(x.it); });
  }
  function onMove(ev) {
    if (!drag || ev.pointerId !== drag.pointerId) return;
    drag.lastEv = ev;
    const dist = Math.hypot(ev.clientX - drag.startX, ev.clientY - drag.startY);
    if (!drag.active) {
      if (ev.pointerType === "mouse" && dist > 4) drag.active = true;
      else if (ev.pointerType !== "mouse" && dist > 8) { clearTimeout(drag.timer); drag = null; return; } // it's a scroll
      else return;
    }
    autoScroll(ev.clientY);
    const col = (drag.kind === "move" && S.view === "week" ? colAt(ev.clientX, ev.clientY) : null) || drag.col || drag.node.closest(".tg-col");
    const m = minuteAt(col, ev.clientY, drag.pxPerMin);
    if (drag.kind === "create") {
      const s = Math.min(drag.anchorMin, m), e = Math.max(drag.anchorMin + SNAP, m);
      drag.result = { date: drag.date, start: s, end: e };
      ghost(col, s, e, `${label(s)} – ${label(e)}`);
    } else if (drag.kind === "move") {
      if (!draggable(drag.x.it)) return;
      const len = drag.x.e - drag.x.s;
      const s = clamp(m - Math.round(drag.grabOffset / SNAP) * SNAP, 0, DAY_MIN - (drag.x.pin ? 0 : Math.min(len, SNAP)));
      drag.result = { date: col.dataset.date, start: s, end: drag.x.pin ? s : s + len };
      drag.node.classList.add("dragging");
      ghost(col, s, drag.x.pin ? s + 15 : s + len, `${label(s)}${drag.x.pin ? "" : " – " + label(s + len)}`);
    } else if (drag.kind === "resize") {
      const e = Math.max(drag.x.s + SNAP, m);
      drag.result = { date: drag.date, start: drag.x.s, end: e };
      drag.node.classList.add("dragging");
      ghost(col, drag.x.s, e, `${label(drag.x.s)} – ${label(e)} (${e - drag.x.s} min)`);
    }
  }
  async function onUp(ev) {
    if (!drag || ev.pointerId !== drag.pointerId) return;
    clearTimeout(drag.timer);
    const d = drag; drag = null;
    document.querySelectorAll(".tg-ghost").forEach((g) => g.remove());
    if (d.node) d.node.classList.remove("dragging");
    if (d.kind === "create") {
      if (!d.active && ev.pointerType !== "mouse") return;                     // a tap while scrolling: ignore
      const r = d.result || { date: d.date, start: d.anchorMin, end: d.anchorMin + 60 }; // a click = one hour
      deps.onCreate(r);
      return;
    }
    if (!d.active || !d.result) return;
    d.node.dataset.justDragged = "1";
    setTimeout(() => d.node && delete d.node.dataset.justDragged, 400);
    const it = d.x.it, r = d.result;
    if (!draggable(it)) { deps.onError(it.type === "routine" ? "Routine previews are edited in Routines." : "That item can't be moved."); return; }
    if (r.date === d.date && r.start === d.x.s && r.end === d.x.e) return;
    d.node.classList.add("saving");
    try {
      if (it.type === "reminder") await deps.api(`/reminders/${it.task_id}`, { method: "PATCH", body: JSON.stringify({ start: naive(r.date, r.start) }) });
      else await deps.api(`/blocks/${it.block_id}`, { method: "PATCH", body: JSON.stringify({ start: naive(r.date, r.start), end: naive(r.date, r.end) }) });
      await refresh();
      deps.onChanged();
    } catch (e) { d.node.classList.remove("saving"); deps.onError("Couldn't move it: " + e.message); }
  }
  window.addEventListener("pointermove", onMove, { passive: true });
  window.addEventListener("pointerup", onUp);
  window.addEventListener("pointercancel", () => { if (drag) { clearTimeout(drag.timer); drag = null; document.querySelectorAll(".tg-ghost").forEach((g) => g.remove()); } });
  // while a touch drag is active, stop the page from scrolling underneath it
  root.addEventListener("touchmove", (e) => { if (drag && drag.active) e.preventDefault(); }, { passive: false });

  // keyboard shortcuts (desktop): T today, D/W/M views, ←/→ move, +/- zoom
  document.addEventListener("keydown", (e) => {
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "t") todayBtn.click(); else if (k === "d") setView("day"); else if (k === "w") setView("week"); else if (k === "m") setView("month");
    else if (e.key === "ArrowLeft") shift(-1); else if (e.key === "ArrowRight") shift(1);
    else if (e.key === "+" || e.key === "=") setZoom(S.zoom + 16); else if (e.key === "-") setZoom(S.zoom - 16);
  });

  return {
    refresh,
    tick: () => { if (!drag) render(); },  // re-draw fills and the "now" line as time passes
    goTo: (d) => { S.anchor = d; S.scrolledKey = ""; refresh(); },
    get view() { return S.view; },
  };
}
