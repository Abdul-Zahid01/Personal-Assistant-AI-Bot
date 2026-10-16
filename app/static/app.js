// Personal AI Assistant — web app. Everything here talks to the API under /api/v1.
import { createCalendar } from "./calendar.js";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const store = { get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
                set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } } };
const PRIORITIES = [["P1", "Urgent"], ["P2", "High"], ["P3", "Normal"], ["P4", "Low"]];
const pcolor = (p) => `var(--${(p || "P3").toLowerCase()})`;
const isMobile = () => matchMedia("(max-width: 1023px)").matches;

const S = { tz: undefined, schedule: null, tasks: [], routines: [], lastId: 0, loadedOnce: false, pushActive: false,
            sound: store.get("sound") !== "off", unread: 0, view: "map", side: store.get("side") || "tasks", editing: null,
            notifs: [], lastRead: +store.get("last_read") || 0, live: false };

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const key = store.get("api_key"); if (key) headers["X-API-Key"] = key;
  const res = await fetch("/api/v1" + path, { ...opts, headers });
  if (res.status === 401) { askKey(); throw new Error("API key required"); }
  if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail || d; } catch { /* not json */ } throw new Error(typeof d === "string" ? d : JSON.stringify(d)); }
  return res.json();
}
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b ?? {}) });
const patch = (p, b) => api(p, { method: "PATCH", body: JSON.stringify(b ?? {}) });
const del = (p) => api(p, { method: "DELETE" });

function setStatus(t, cls = "") { const c = $("conn"); c.textContent = t; c.className = "sub status " + cls; }
function askKey() { const d = $("keyDialog"); if (!d.open) d.showModal(); }
$("keyBtn").onclick = askKey;
$("keyDialog").addEventListener("close", (e) => { if (e.target.returnValue === "ok") { store.set("api_key", $("keyInput").value.trim()); connectStream(); refreshAll(); } });

// ------------------------------------------------------------------ time (in the user's configured timezone)
const fmtCache = {};
function partsTz(iso) {
  const f = fmtCache[S.tz] || (fmtCache[S.tz] = new Intl.DateTimeFormat("en-CA", { timeZone: S.tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }));
  const p = Object.fromEntries(f.formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
  return { date: `${p.year}-${p.month}-${p.day}`, hm: `${String(+p.hour % 24).padStart(2, "0")}:${p.minute}` };
}
function fmt(iso, withDate) {
  if (!iso) return "";
  const o = { hour: "numeric", minute: "2-digit", timeZone: S.tz };
  if (withDate) Object.assign(o, { weekday: "short", day: "numeric", month: "short" });
  return new Date(iso).toLocaleString([], o);
}
const humanMin = (m) => { m = Math.max(0, Math.round(m)); const h = Math.floor(m / 60), r = m % 60; return h ? (r ? `${h}h ${r}m` : `${h}h`) : `${r}m`; };
const hmToMin = (s) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
const minToHm = (m) => `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

// ------------------------------------------------------------------ sound, toasts, alerts
let audio;
function unlockAudio() { try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); if (audio.state === "suspended") audio.resume(); } catch { /* no audio */ } }
document.addEventListener("pointerdown", unlockAudio, { once: true });
function chime() {
  if (!S.sound || !audio) return;
  const t = audio.currentTime;
  [[880, 0], [1320, .16]].forEach(([f, d]) => {
    const o = audio.createOscillator(), g = audio.createGain();
    o.type = "sine"; o.frequency.value = f; o.connect(g); g.connect(audio.destination);
    g.gain.setValueAtTime(0, t + d); g.gain.linearRampToValueAtTime(.18, t + d + .02); g.gain.exponentialRampToValueAtTime(.001, t + d + .5);
    o.start(t + d); o.stop(t + d + .55);
  });
}
function actionButtons(buttons, onDone) {
  const row = el("div", "actions");
  for (const [label, data] of buttons) {
    const b = el("button", "btn small", label);
    b.onclick = async () => { row.querySelectorAll("button").forEach((x) => (x.disabled = true)); try { await post("/actions", { data }); onDone && onDone(); await refreshAll(); } catch (e) { toast("Couldn't do that\n" + e.message); row.querySelectorAll("button").forEach((x) => (x.disabled = false)); } };
    row.appendChild(b);
  }
  return row;
}
function toast(text, buttons = [], color) {
  const [first, ...rest] = String(text).split("\n");
  const t = el("div", "toast glass"); t.setAttribute("role", "status");
  if (color) t.style.setProperty("--c", color);
  const head = el("div", "tt"); head.appendChild(el("span", null, rest.length ? first : "Your assistant"));
  const x = el("button", "x", "✕"); x.setAttribute("aria-label", "Dismiss"); x.onclick = () => t.remove(); head.appendChild(x);
  t.append(head, el("div", "tb", rest.length ? rest.slice(0, 6).join("\n") : first));
  if (buttons.length) t.appendChild(actionButtons(buttons, () => t.remove()));
  $("toasts").prepend(t);
  while ($("toasts").children.length > 3) $("toasts").lastChild.remove();
  setTimeout(() => t.remove(), buttons.length ? 60000 : 8000);
}
function alertUser(m) {
  toast(m.content, m.buttons || [], "var(--accent)");
  chime();
  if (S.sound && navigator.vibrate) navigator.vibrate([120, 60, 120]);
  if (!S.pushActive && "Notification" in window && Notification.permission === "granted" && document.hidden) {
    const [title, ...rest] = m.content.split("\n");
    const n = new Notification(rest.length ? title : "Your assistant", { body: rest.length ? rest.join("\n") : title, icon: "/static/icon-192.png", tag: "msg-" + m.id });
    n.onclick = () => { window.focus(); n.close(); };
  }
}

// ------------------------------------------------------------------ notification centre
function renderNotifs() {
  const unread = S.notifs.filter((m) => m.id > S.lastRead).length;
  $("bellCount").hidden = !unread; $("bellCount").textContent = unread > 9 ? "9+" : unread;
  const list = $("notifList"); list.replaceChildren();
  if (!S.notifs.length) { list.appendChild(el("div", "empty", "No notifications yet. Reminders and daily briefs show up here.")); return; }
  for (const m of S.notifs.slice().reverse().slice(0, 40)) {
    const [first, ...rest] = m.content.split("\n");
    const it = el("div", "nitem" + (m.id > S.lastRead ? " unread" : ""));
    it.append(el("div", "nt", first), el("div", "sub", fmt(m.created_at, true)));
    if (rest.length) it.appendChild(el("div", "nb", rest.slice(0, 4).join("\n")));
    if (m.buttons && m.buttons.length && m.id > S.lastRead) it.appendChild(actionButtons(m.buttons));
    list.appendChild(it);
  }
}
function toggleNotifs(force) {
  const p = $("notifPanel"), open = force ?? p.hidden;
  p.hidden = !open;
  if (open) { renderPushOpt(); renderNotifs(); }
}
$("bellBtn").onclick = (e) => { e.stopPropagation(); unlockAudio(); toggleNotifs(); };
document.addEventListener("click", (e) => { if (!$("notifPanel").hidden && !e.target.closest("#notifPanel, #bellBtn")) toggleNotifs(false); });
$("markRead").onclick = () => { S.lastRead = Math.max(S.lastRead, ...S.notifs.map((m) => m.id), 0); store.set("last_read", S.lastRead); renderNotifs(); };
function renderSound() { $("soundSwitch").setAttribute("aria-checked", S.sound); }
$("soundSwitch").onclick = () => { S.sound = !S.sound; store.set("sound", S.sound ? "on" : "off"); unlockAudio(); renderSound(); if (S.sound) chime(); };

const pushSupported = "serviceWorker" in navigator && "PushManager" in window && window.isSecureContext;
const b64ToBytes = (b64) => { const raw = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/")); return Uint8Array.from(raw, (c) => c.charCodeAt(0)); };
function renderPushOpt() {
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  const b = $("pushBtn"), l = $("pushLabel");
  if (perm === "unsupported") { l.textContent = "This browser can't show system pop-ups"; b.hidden = true; return; }
  if (perm === "denied") { l.textContent = "Pop-ups are blocked in browser settings"; b.hidden = true; return; }
  b.hidden = false;
  l.textContent = perm === "granted" ? (S.pushActive ? "Pop-ups on — even when the app is closed" : "Pop-ups on while the app is open") : "Pop-ups on this device";
  b.textContent = perm === "granted" ? "Send test" : "Turn on";
}
async function savePush(sub) { const j = sub.toJSON(); await post("/push/subscribe", { endpoint: j.endpoint, keys: j.keys }); S.pushActive = true; }
$("pushBtn").onclick = async () => {
  try {
    if (Notification.permission === "default") await Notification.requestPermission();
    if (Notification.permission !== "granted") { renderPushOpt(); return; }
    if (pushSupported) {
      const reg = await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) { const { public_key } = await api("/push/public-key"); sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(public_key) }); }
      await savePush(sub);
      const { sent } = await post("/push/test");
      if (!sent) toast("Pop-ups are on\nBut the test didn't arrive — check the server log.");
    } else new Notification("Notifications are on", { body: "You'll see reminders while the app is open.", icon: "/static/icon-192.png" });
  } catch (e) { toast("Couldn't enable pop-ups\n" + e.message); }
  renderPushOpt();
};
if (pushSupported) {
  navigator.serviceWorker.register("/sw.js").then(async (reg) => {
    const sub = await reg.pushManager.getSubscription();
    if (sub && Notification.permission === "granted") { try { await savePush(sub); } catch { /* retried next load */ } }
  }).catch(() => {});
}

// ------------------------------------------------------------------ live updates (Server-Sent Events) with polling as a fallback
let es, esRetry;
async function connectStream() {
  if (!("EventSource" in window)) return;
  clearTimeout(esRetry); if (es) es.close();
  let qs = "";
  try { const { token } = await post("/stream/token"); qs = "?token=" + encodeURIComponent(token); } catch { /* dev server: no token needed */ }
  es = new EventSource("/api/v1/stream" + qs);
  es.addEventListener("hello", () => { S.live = true; setStatus("live", "live"); });
  es.addEventListener("message", () => poll());
  let t; es.addEventListener("changed", () => { clearTimeout(t); t = setTimeout(refreshPanels, 250); });
  es.onerror = () => { S.live = false; setStatus("reconnecting…", "bad"); es.close(); esRetry = setTimeout(connectStream, 5000); };
}

// ------------------------------------------------------------------ views / tabs
function setView(v) {
  S.view = v;
  if (v === "tasks" || v === "routines") { S.side = v; store.set("side", v); }
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", t.dataset.view === v));
  for (const name of ["map", "chat", "tasks", "routines"]) $("view-" + name).classList.toggle("active", name === v);
  $("view-tasks").classList.toggle("side-hidden", S.side !== "tasks");
  $("view-routines").classList.toggle("side-hidden", S.side !== "routines");
  document.querySelectorAll(".seg-side button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.side === S.side));
  if (v === "chat") { S.unread = 0; renderBadge(); setTimeout(() => { $("log").scrollTop = $("log").scrollHeight; }, 0); }
  if (location.hash !== "#" + v) history.replaceState(null, "", "#" + v);
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => setView(t.dataset.view)));
document.querySelectorAll(".seg-side button").forEach((b) => (b.onclick = () => setView(b.dataset.side)));
function renderBadge() { const b = $("chatBadge"); b.hidden = !S.unread; b.textContent = S.unread > 9 ? "9+" : S.unread; }

// ------------------------------------------------------------------ chat
const seen = new Set();
function addMessage(m) {
  if (seen.has(m.id)) return;
  seen.add(m.id); S.lastId = Math.max(S.lastId, m.id);
  if (m.channel === "proactive") S.notifs.push(m);
  const e = el("div", "msg " + m.role + (m.channel === "proactive" ? " proactive" : ""), m.content);
  e.appendChild(el("span", "meta", fmt(m.created_at) + (m.channel === "proactive" ? " · reminder" : m.channel === "telegram" ? " · Telegram" : "")));
  if (m.buttons && m.buttons.length) e.appendChild(actionButtons(m.buttons));
  $("log").appendChild(e);
}
let polling = false;
async function poll() {
  if (polling) return; polling = true;
  try {
    const log = $("log"), near = log.scrollHeight - log.scrollTop - log.clientHeight < 90;
    const msgs = await api("/messages?after_id=" + S.lastId + "&limit=100");
    msgs.forEach(addMessage);
    if (msgs.length && (near || !S.loadedOnce)) log.scrollTop = log.scrollHeight;
    if (S.loadedOnce) {
      const fresh = msgs.filter((m) => m.channel === "proactive");
      fresh.forEach(alertUser);
      const replies = msgs.filter((m) => m.role === "assistant").length;
      if (replies && S.view !== "chat" && isMobile()) { S.unread += replies; renderBadge(); }
    }
    if (!S.loadedOnce && !S.lastRead) { S.lastRead = S.lastId; store.set("last_read", S.lastRead); } // first visit: history isn't "unread"
    S.loadedOnce = true;
    renderNotifs();
    if (!S.live) setStatus("online");
  } catch (e) { setStatus(e.message === "API key required" ? "API key required" : "offline — is the server running?", "bad"); }
  finally { polling = false; }
}
async function sendChat(text) {
  const send = $("send");
  send.disabled = true; send.textContent = "…";
  const typing = el("div", "msg assistant", "Thinking…"); $("log").appendChild(typing); $("log").scrollTop = $("log").scrollHeight;
  try { await post("/chat", { message: text }); } catch (e) { $("log").appendChild(el("div", "msg assistant", "Couldn't send: " + e.message)); }
  typing.remove(); send.disabled = false; send.textContent = "Send"; if (!isMobile()) $("input").focus();
  await poll(); await refreshPanels();
}
$("composer").addEventListener("submit", (e) => { e.preventDefault(); const t = $("input").value.trim(); if (!t) return; $("input").value = ""; sendChat(t); });
for (const [label, text] of [["Plan my day", "plan my day"], ["What's next?", "what should I do next?"], ["Plan tomorrow", "plan tomorrow"], ["Day review", "end of day review"]]) {
  const b = el("button", null, label); b.type = "button"; b.onclick = () => sendChat(text); $("chips").appendChild(b);
}

// ------------------------------------------------------------------ sheets: details and editor
function closeSheet() { $("sheet").classList.remove("open"); $("scrim").classList.remove("open"); setTimeout(() => { if (!$("sheet").classList.contains("open")) $("sheet").hidden = true; }, 260); }
function showSheet(build) {
  const sh = $("sheet"); sh.replaceChildren(); build(sh);
  sh.hidden = false;
  requestAnimationFrame(() => { sh.classList.add("open"); $("scrim").classList.add("open"); const f = sh.querySelector("input, button"); f && f.focus(); });
}
$("scrim").onclick = closeSheet;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSheet(); toggleNotifs(false); } });

function prioChips(container, initial, onChange) {
  container.replaceChildren();
  let value = initial || "P3";
  for (const [p, name] of PRIORITIES) {
    const b = el("button"); b.type = "button"; b.style.setProperty("--c", pcolor(p)); b.dataset.p = p;
    b.append(el("i"), document.createTextNode(name));
    b.setAttribute("aria-pressed", p === value);
    b.onclick = () => { value = p; container.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.p === p)); onChange && onChange(p); };
    container.appendChild(b);
  }
  return () => value;
}

function chipPrio(p) { const c = el("span", "chip-prio"); c.style.setProperty("--c", pcolor(p)); c.append(el("i"), document.createTextNode((PRIORITIES.find((x) => x[0] === p) || [, "Normal"])[1])); return c; }

function openItem(it) {
  if (it.type === "routine") {
    const r = S.routines.find((x) => x.id === it.routine_id);
    showSheet((sh) => {
      const h = el("h3", null, "🔁 " + it.title); h.id = "sheetTitle"; sh.appendChild(h);
      sh.appendChild(el("div", "sub", `${fmt(it.start, true)} – ${fmt(it.end)} · routine${r ? " · " + r.days_label : ""}`));
      sh.appendChild(el("div", "sub", "This day's entry is created automatically the day before."));
      const row = el("div", "actions");
      const edit = el("button", "btn primary", "Edit routine"); edit.onclick = () => { closeSheet(); setView("routines"); if (r) editRoutine(r); };
      const close = el("button", "btn", "Close"); close.onclick = closeSheet;
      row.append(edit, close); sh.appendChild(row);
    });
    return;
  }
  const done = it.task_status === "DONE" || it.status === "DONE";
  const now = Date.now(), s = Date.parse(it.start), e = Date.parse(it.end);
  const p = done ? 1 : now <= s ? 0 : now >= e ? 1 : (now - s) / (e - s);
  showSheet((sh) => {
    const icon = it.routine_id ? "🔁 " : it.type === "reminder" ? "⏰ " : it.type === "deadline" ? "⏳ " : it.kind === "EVENT" ? "📅 " : it.pinned ? "📌 " : "";
    const h = el("h3", null, icon + it.title); h.id = "sheetTitle"; sh.appendChild(h);
    const when = it.type === "reminder" || it.type === "deadline" ? `${it.type === "deadline" ? "Due" : "At"} ${fmt(it.start, true)}` : `${fmt(it.start, true)} – ${fmt(it.end)}`;
    const meta = el("div", "row"); meta.style.marginTop = "4px";
    meta.append(el("span", "sub", when), chipPrio(it.priority));
    if (it.remind_before_min) meta.appendChild(el("span", "sub", `🔔 ${humanMin(it.remind_before_min)} before`));
    sh.appendChild(meta);
    if (it.type === "block") {
      const bar = el("div", "bar"); bar.style.margin = "12px 0 4px"; const f = el("i"); f.style.width = p * 100 + "%"; bar.appendChild(f); sh.appendChild(bar);
      sh.appendChild(el("div", "sub", done ? "Done ✓" : p === 0 ? "Starts in " + humanMin((s - now) / 60000) : p < 1 ? `${Math.round(p * 100)}% through · ${humanMin((e - now) / 60000)} left` : "Time block has ended"));
      if (it.reason && it.reason !== "placed by you") sh.appendChild(el("div", "sub", "Why now: " + it.reason));
    }
    if (it.notes) sh.appendChild(el("div", "notes", it.notes));
    const row = el("div", "actions");
    const add = (label, fn, cls = "btn") => { const x = el("button", cls, label); x.onclick = async () => { x.disabled = true; try { await fn(); closeSheet(); await refreshAll(); } catch (er) { toast("Couldn't do that\n" + er.message); x.disabled = false; } }; row.appendChild(x); };
    if (it.task_id && !done) {
      if (it.kind === "WORK") it.task_status === "IN_PROGRESS" ? add("⏸ Pause", () => post(`/tasks/${it.task_id}/pause`)) : add("▶ Start", () => post(`/tasks/${it.task_id}/start`));
      add("✅ Done", () => post(`/tasks/${it.task_id}/complete`), "btn primary");
      if (it.type === "block") add("⏩ +15m", () => patch(`/blocks/${it.block_id}`, { start: shiftIso(it.start, 15), end: shiftIso(it.end, 15) }));
      else add("⏰ +10m", () => post(`/tasks/${it.task_id}/snooze`, { minutes: 10 }));
      if (!it.routine_id) add("➡️ Tomorrow", () => post("/actions", { data: "tmr:" + it.task_id }));
      const edit = el("button", "btn", "✏️ Edit"); edit.onclick = () => openEditor({ item: it }); row.appendChild(edit);
      if (it.type === "block") add(it.routine_id ? "Skip today" : it.kind === "EVENT" ? "🗑 Delete" : "Remove from calendar", () => del(`/blocks/${it.block_id}`), "btn danger");
      else add("🗑 Delete", () => del(`/tasks/${it.task_id}`), "btn danger");
    }
    const close = el("button", "btn", "Close"); close.onclick = closeSheet; row.appendChild(close);
    sh.appendChild(row);
  });
}
function shiftIso(iso, minutes) { const p = partsTz(new Date(Date.parse(iso) + minutes * 60000).toISOString()); return `${p.date}T${p.hm}:00`; }

const REMIND = [["", "Default"], ["0", "At the start"], ["5", "5 min before"], ["10", "10 min before"], ["15", "15 min before"], ["30", "30 min before"], ["60", "1 hour before"], ["1440", "1 day before"]];
function openEditor({ range, item }) {
  const editing = !!item;
  let kind = editing ? (item.type === "reminder" ? "REMINDER" : item.kind === "EVENT" ? "EVENT" : "TASK") : "TASK";
  const startP = editing ? partsTz(item.start) : null, endP = editing ? partsTz(item.end) : null;
  showSheet((sh) => {
    const h = el("h3", null, editing ? "Edit" : "New on your calendar"); h.id = "sheetTitle"; sh.appendChild(h);
    const form = el("form"); form.autocomplete = "off";
    const title = el("input"); title.type = "text"; title.placeholder = "What is it?"; title.maxLength = 255; title.required = true; title.value = editing ? item.title : "";
    title.setAttribute("aria-label", "Title");
    const typeSeg = el("span", "seg"); typeSeg.setAttribute("role", "group"); typeSeg.setAttribute("aria-label", "Type");
    const types = [["TASK", "✅ Task", "Work you'll do in this slot"], ["EVENT", "📅 Event", "A fixed appointment"], ["REMINDER", "⏰ Reminder", "Just a nudge at a time"]];
    for (const [k, label, tip] of types) {
      const b = el("button", null, label); b.type = "button"; b.title = tip; b.dataset.k = k; b.disabled = editing;
      b.onclick = () => { kind = k; paintType(); }; typeSeg.appendChild(b);
    }
    const date = el("input"); date.type = "date"; date.value = editing ? startP.date : range.date;
    const start = el("input"); start.type = "time"; start.step = 300; start.value = editing ? startP.hm : minToHm(range.start);
    const end = el("input"); end.type = "time"; end.step = 300; end.value = editing ? endP.hm : minToHm(Math.min(range.end, 1439));
    const fDate = el("label", "field", "Date"), fStart = el("label", "field", "Start"), fEnd = el("label", "field", "End");
    fDate.appendChild(date); fStart.appendChild(start); fEnd.appendChild(end);
    const grid = el("div", "grid3"); grid.append(fDate, fStart, fEnd);
    const dur = el("div", "sub");
    const updateDur = () => { let m = hmToMin(end.value) - hmToMin(start.value); if (m <= 0) m += 1440; dur.textContent = kind === "REMINDER" ? "" : `Duration: ${humanMin(m)}${hmToMin(end.value) <= hmToMin(start.value) ? " (ends the next day)" : ""}`; };
    start.oninput = end.oninput = updateDur;
    const prio = el("span", "prio"); prio.setAttribute("aria-label", "Importance");
    const getPrio = prioChips(prio, editing ? item.priority : "P3");
    const fPrio = el("label", "field", "Importance"); fPrio.appendChild(prio);
    const notes = el("textarea"); notes.placeholder = "Details, links, checklist…"; notes.maxLength = 4000; notes.value = editing ? item.notes || "" : "";
    const fNotes = el("label", "field", "Notes"); fNotes.appendChild(notes);
    const remind = el("select");
    for (const [v, label] of REMIND) { const o = el("option", null, label); o.value = v; remind.appendChild(o); }
    remind.value = editing && item.remind_before_min != null ? String(item.remind_before_min) : "";
    const fRemind = el("label", "field", "Notify me"); fRemind.appendChild(remind);
    function paintType() {
      typeSeg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.k === kind));
      fEnd.hidden = kind === "REMINDER"; grid.style.gridTemplateColumns = kind === "REMINDER" ? "1.3fr 1fr" : ""; updateDur();
    }
    const row = el("div", "actions");
    const cancel = el("button", "btn", "Cancel"); cancel.type = "button"; cancel.onclick = closeSheet;
    const save = el("button", "btn primary", editing ? "Save" : "Add to calendar"); save.type = "submit";
    row.append(cancel, save);
    form.append(title, typeSeg, grid, dur, fPrio, fNotes, fRemind, row);
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const t = title.value.trim(); if (!t) return;
      const endDate = kind !== "REMINDER" && hmToMin(end.value) <= hmToMin(start.value) ? addDay(date.value) : date.value;
      const startS = `${date.value}T${start.value}:00`, endS = `${endDate}T${end.value}:00`;
      const lead = remind.value === "" ? null : +remind.value;
      save.disabled = true;
      try {
        if (!editing) {
          await post("/blocks", { title: t, kind, start: startS, end: kind === "REMINDER" ? null : endS, priority: getPrio(), notes: notes.value || null, remind_before_min: lead });
          toast(`${kind === "EVENT" ? "📅" : kind === "REMINDER" ? "⏰" : "✅"} Added\n${t} · ${start.value}${kind === "REMINDER" ? "" : "–" + end.value}`, [], pcolor(getPrio()));
        } else {
          await patch(`/tasks/${item.task_id}`, { title: t, priority: getPrio(), notes: notes.value || null, remind_before_min: lead });
          const timeChanged = startP.date !== date.value || startP.hm !== start.value || (kind !== "REMINDER" && endP.hm !== end.value);
          if (timeChanged) {
            if (item.type === "block") await patch(`/blocks/${item.block_id}`, { start: startS, end: endS });
            else if (item.type === "reminder") await patch(`/reminders/${item.task_id}`, { start: startS });
            else await patch(`/tasks/${item.task_id}`, { due_at: startS });
          }
          toast("Saved\n" + t, [], pcolor(getPrio()));
        }
        closeSheet(); await refreshAll();
      } catch (e) { toast("Couldn't save\n" + e.message); save.disabled = false; }
    };
    sh.appendChild(form);
    paintType();
    setTimeout(() => title.focus(), 50);
  });
}
const addDay = (d) => { const t = new Date(d + "T00:00:00Z"); t.setUTCDate(t.getUTCDate() + 1); return t.toISOString().slice(0, 10); };

// ------------------------------------------------------------------ now card + day ring
function renderNow() {
  const s = S.schedule, card = $("nowCard");
  if (!s) return;
  card.hidden = false; card.replaceChildren();
  const now = Date.now();
  const running = s.in_progress.find((t) => t.started_at);
  const current = s.blocks.find((b) => Date.parse(b.start) <= now && now < Date.parse(b.end) && b.task_status !== "DONE");
  const next = s.blocks.filter((b) => Date.parse(b.start) > now).sort((a, b) => Date.parse(a.start) - Date.parse(b.start))[0];
  let pct = 0, over = false, title, kicker, left = "", right = "", actions = [], color = "var(--accent)";
  if (running) {
    const mins = (now - Date.parse(running.started_at)) / 60000, est = running.estimate_min || 30;
    pct = mins / est; over = pct > 1; kicker = "Working on"; title = running.title; color = pcolor(running.priority);
    left = humanMin(mins) + " elapsed"; right = over ? humanMin(mins - est) + " over estimate" : humanMin(est - mins) + " left";
    actions = [["⏸ Pause", () => post(`/tasks/${running.id}/pause`)], ["✅ Done", () => post(`/tasks/${running.id}/complete`)]];
  } else if (current) {
    const s0 = Date.parse(current.start), e0 = Date.parse(current.end);
    pct = (now - s0) / (e0 - s0); color = current.routine ? "var(--routine)" : pcolor(current.priority);
    kicker = current.kind === "EVENT" ? (current.routine ? "Routine now" : "Happening now") : "Focus block now";
    title = current.title; left = current.label; right = humanMin((e0 - now) / 60000) + " left";
    if (current.task_id && current.kind === "WORK" && current.task_status !== "IN_PROGRESS") actions.push(["▶ Start", () => post(`/tasks/${current.task_id}/start`)]);
    if (current.task_id) actions.push(["✅ Done", () => post(`/tasks/${current.task_id}/complete`)]);
  } else if (next) {
    kicker = "Up next"; title = next.title; left = next.label; right = "in " + humanMin((Date.parse(next.start) - now) / 60000);
  } else { kicker = "Free"; title = s.blocks.length ? "Nothing else planned today" : "Your day is open — drag on the calendar to add something"; }
  card.style.borderLeft = `4px solid ${color}`;
  card.append(el("div", "k", kicker), el("div", "t", title));
  if (running || current) {
    const bar = el("div", "bar" + (over ? " over" : "")); const f = el("i"); f.style.width = Math.min(100, pct * 100) + "%";
    if (!over) f.style.background = color;
    bar.appendChild(f); bar.setAttribute("role", "progressbar"); bar.setAttribute("aria-valuenow", Math.round(pct * 100)); bar.setAttribute("aria-valuemin", 0); bar.setAttribute("aria-valuemax", 100);
    card.appendChild(bar);
  }
  if (left || right) { const meta = el("div", "meta"); meta.append(el("span", null, left), el("span", null, (running || current ? Math.round(Math.min(pct, 9.99) * 100) + "% · " : "") + right)); card.appendChild(meta); }
  if (actions.length) {
    const row = el("div", "row"); row.style.marginTop = "8px";
    for (const [label, fn] of actions) { const b = el("button", "btn small", label); b.onclick = async () => { b.disabled = true; try { await fn(); await refreshAll(); } catch (e) { toast(e.message); } }; row.appendChild(b); }
    card.appendChild(row);
  }
  const total = s.blocks.reduce((a, b) => a + (Date.parse(b.end) - Date.parse(b.start)), 0);
  const doneMs = s.blocks.reduce((a, b) => { const st = Date.parse(b.start), en = Date.parse(b.end); const pr = b.task_status === "DONE" ? 1 : now <= st ? 0 : now >= en ? 1 : (now - st) / (en - st); return a + pr * (en - st); }, 0);
  const p = total ? Math.round((doneMs / total) * 100) : 0;
  $("dayRing").style.setProperty("--p", p); $("dayRing").dataset.label = p + "%";
}

// ------------------------------------------------------------------ calendar
const calendar = createCalendar($("calendar"), {
  api, getTz: () => S.tz,
  onOpen: openItem,
  onCreate: (range) => openEditor({ range }),
  onChanged: () => refreshPanels({ skipCalendar: true }),
  onError: (msg) => toast(msg),
});

// ------------------------------------------------------------------ tasks
const getTaskPrio = prioChips($("tPrio"), "P3");
$("taskForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const title = $("tTitle").value.trim(); if (!title) return;
  try {
    await post("/tasks", { title, priority: getTaskPrio(), estimate_min: +$("tEst").value || 30, due_at: $("tDue").value ? $("tDue").value + ":00" : null });
    $("tTitle").value = ""; $("tDue").value = ""; toast("Task added\n" + title, [], pcolor(getTaskPrio())); await refreshAll();
  } catch (er) { toast("Couldn't add task\n" + er.message); }
});
function renderTasks() {
  const box = $("taskList"); box.replaceChildren();
  const list = S.tasks.filter((t) => !t.routine_id);
  if (!list.length) { box.appendChild(el("div", "empty", "No open tasks. 🎉")); return; }
  for (const t of list) {
    const it = el("div", "item");
    const dot = el("span", "dot"); dot.style.background = pcolor(t.priority); dot.title = t.priority; it.appendChild(dot);
    const g = el("div", "grow");
    g.appendChild(el("div", "name", (t.kind === "REMINDER" ? "⏰ " : t.kind === "EVENT" ? "📅 " : "") + t.title));
    const bits = [(PRIORITIES.find((x) => x[0] === t.priority) || [, ""])[1]];
    if (t.kind === "TASK") bits.push(humanMin(t.estimate_min)); if (t.due_at) bits.push("due " + fmt(t.due_at, true)); if (t.recurrence) bits.push("repeats " + t.recurrence.toLowerCase());
    g.appendChild(el("div", "sub", bits.join(" · ")));
    if (t.status === "IN_PROGRESS" && t.started_at) {
      const mins = (Date.now() - Date.parse(t.started_at)) / 60000, pct = mins / (t.estimate_min || 30);
      const bar = el("div", "bar" + (pct > 1 ? " over" : "")); const f = el("i"); f.style.width = Math.min(100, pct * 100) + "%"; bar.appendChild(f); g.appendChild(bar);
      g.appendChild(el("div", "sub", `${humanMin(mins)} of ${humanMin(t.estimate_min)}` + (pct > 1 ? " · over estimate" : "")));
    }
    it.appendChild(g);
    if (t.kind === "TASK") {
      const sp = el("button", "btn small", t.status === "IN_PROGRESS" ? "⏸" : "▶");
      sp.title = t.status === "IN_PROGRESS" ? "Pause" : "Start"; sp.setAttribute("aria-label", sp.title + " " + t.title);
      sp.onclick = async () => { sp.disabled = true; try { await post(`/tasks/${t.id}/${t.status === "IN_PROGRESS" ? "pause" : "start"}`); await refreshAll(); } catch (e) { toast(e.message); sp.disabled = false; } };
      it.appendChild(sp);
    }
    const done = el("button", "btn small", "✓"); done.title = "Done"; done.setAttribute("aria-label", "Mark " + t.title + " done");
    done.onclick = async () => { done.disabled = true; try { await post(`/tasks/${t.id}/complete`); toast("✅ Done\n" + t.title, [], pcolor(t.priority)); await refreshAll(); } catch (e) { toast(e.message); done.disabled = false; } };
    it.appendChild(done);
    box.appendChild(it);
  }
}

// ------------------------------------------------------------------ routines
const DAY_LBL = ["M", "T", "W", "T", "F", "S", "S"], DAY_NAME = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
let rDays = new Set([0, 1, 2, 3, 4, 5, 6]), rKind = "EVENT";
DAY_LBL.forEach((l, i) => { const b = el("button", null, l); b.type = "button"; b.setAttribute("aria-label", DAY_NAME[i]); b.onclick = () => { rDays.has(i) ? rDays.delete(i) : rDays.add(i); paintDays(); }; $("rDays").appendChild(b); });
function paintDays() { [...$("rDays").children].forEach((b, i) => b.setAttribute("aria-pressed", rDays.has(i))); }
function paintKind() { $("rKind").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.k === rKind)); }
$("rKind").querySelectorAll("button").forEach((b) => (b.onclick = () => { rKind = b.dataset.k; paintKind(); }));
for (const [name, time, dur] of [["🍳 Breakfast", "08:00", 30], ["🥗 Lunch", "13:00", 45], ["🍽️ Dinner", "20:00", 45], ["🏋️ Gym", "18:00", 60], ["📞 Call mom", "19:30", 15], ["🧘 Meditate", "07:00", 15]]) {
  const b = el("button", null, "+ " + name); b.type = "button";
  b.onclick = () => { resetRoutineForm(); $("rTitle").value = name.replace(/^\S+\s/, ""); $("rTime").value = time; $("rDur").value = dur; $("rSave").focus(); };
  $("presets").appendChild(b);
}
function resetRoutineForm() {
  S.editing = null; $("rTitle").value = ""; $("rTime").value = "18:00"; $("rDur").value = 30; rDays = new Set([0, 1, 2, 3, 4, 5, 6]); rKind = "EVENT";
  paintDays(); paintKind(); $("rSave").textContent = "Add routine"; $("rDelete").hidden = true; $("rCancel").hidden = true;
}
function editRoutine(r) {
  S.editing = r.id; $("rTitle").value = r.title; $("rTime").value = r.time_of_day; $("rDur").value = r.duration_min;
  rDays = new Set(r.days); rKind = r.kind; paintDays(); paintKind();
  $("rSave").textContent = "Save"; $("rDelete").hidden = false; $("rCancel").hidden = false; $("rTitle").focus();
}
$("rCancel").onclick = resetRoutineForm;
$("rDelete").onclick = async () => {
  if (!S.editing || !confirm("Delete this routine and its upcoming entries?")) return;
  try { await del(`/routines/${S.editing}`); toast("Routine deleted"); resetRoutineForm(); await refreshAll(); } catch (e) { toast("Couldn't delete\n" + e.message); }
};
$("routineForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!rDays.size) { toast("Pick at least one day"); return; }
  const body = { title: $("rTitle").value.trim(), time_of_day: $("rTime").value, duration_min: +$("rDur").value || 30, days: [...rDays].sort(), kind: rKind };
  try {
    if (S.editing) { await patch(`/routines/${S.editing}`, body); toast("Routine updated\n" + body.title, [], "var(--routine)"); }
    else { const r = await post("/routines", body); toast(`🔁 Routine added\n${r.title} at ${r.time_of_day}, ${r.days_label}`, [], "var(--routine)"); }
    resetRoutineForm(); await refreshAll();
  } catch (er) { toast("Couldn't save routine\n" + er.message); }
});
function renderRoutines() {
  const box = $("routineList"); box.replaceChildren();
  if (!S.routines.length) { box.appendChild(el("div", "empty", "No routines yet. Tap a preset above — they'll show on your calendar every day.")); return; }
  for (const r of S.routines) {
    const it = el("div", "item" + (r.active ? "" : " off"));
    const time = el("div", null, r.time_of_day); time.style.cssText = "font-weight:700;font-variant-numeric:tabular-nums;min-width:50px"; it.appendChild(time);
    const g = el("div", "grow"); g.style.cursor = "pointer"; g.onclick = () => editRoutine(r);
    g.appendChild(el("div", "name", (r.kind === "REMINDER" ? "⏰ " : "🔁 ") + r.title));
    let today = "";
    if (r.today) today = r.today.status === "DONE" ? " · ✓ done today" : Date.parse(r.today.due_at) <= Date.now() && Date.now() < Date.parse(r.today.due_at) + r.duration_min * 60000 ? " · happening now" : "";
    g.appendChild(el("div", "sub", `${humanMin(r.duration_min)} · ${r.days_label}${r.active ? "" : " · paused"}${today}`));
    it.appendChild(g);
    const sw = el("button", "switch"); sw.setAttribute("role", "switch"); sw.setAttribute("aria-checked", r.active); sw.setAttribute("aria-label", (r.active ? "Pause " : "Resume ") + r.title);
    sw.onclick = async () => { sw.disabled = true; try { await patch(`/routines/${r.id}`, { active: !r.active }); await refreshAll(); } catch (e) { toast(e.message); sw.disabled = false; } };
    it.appendChild(sw);
    box.appendChild(it);
  }
}

// ------------------------------------------------------------------ data
let refreshing = null;
async function refreshPanels({ skipCalendar = false } = {}) {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      const [sched, tasks, routines] = await Promise.all([api("/schedule?date=today"), api("/tasks"), api("/routines")]);
      S.schedule = sched; S.tz = sched.timezone; S.tasks = tasks; S.routines = routines;
      renderNow(); renderTasks(); renderRoutines();
      if (!skipCalendar) await calendar.refresh();
    } catch (e) { if (e.message !== "API key required") toast(e.message); }
    finally { refreshing = null; }
  })();
  return refreshing;
}
async function refreshAll() { await poll(); await refreshPanels(); }
$("planBtn").onclick = async () => {
  const b = $("planBtn"); b.disabled = true;
  try {
    const r = await post("/plan", { date: "today", commit: true });
    toast(r.blocks.length ? `✨ Planned ${r.blocks.length} block(s)` + (r.deferred.length ? `\nCouldn't fit: ${r.deferred.map((d) => d.title).join(", ")}` : "\nAround your events, routines and pinned items.") : "Nothing to plan\nAdd some tasks first.");
    await refreshAll();
  } catch (e) { toast("Couldn't plan\n" + e.message); }
  b.disabled = false;
};

// ------------------------------------------------------------------ start
paintDays(); paintKind(); renderSound();
const startView = location.hash.slice(1);
setView(["map", "chat", "tasks", "routines"].includes(startView) ? startView : "map");
matchMedia("(max-width: 1023px)").addEventListener("change", () => setView(S.view));
refreshAll().then(connectStream);
setInterval(() => { if (!S.live) poll(); }, 5000);          // fallback when the live stream is down
setInterval(refreshPanels, 60000);                           // safety net
setInterval(() => { renderNow(); renderTasks(); calendar.tick(); }, 15000); // live fill as time passes
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refreshAll(); if (!S.live) connectStream(); } });
