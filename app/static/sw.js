// Service worker: shows push notifications and handles taps on them, even when the app isn't open.
"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch { d = { body: event.data ? event.data.text() : "" }; }
  const actions = Array.isArray(d.actions) ? d.actions : [];
  event.waitUntil(self.registration.showNotification(d.title || "Your assistant", {
    body: d.body || "",
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    data: { url: d.url || "/", actions },
    actions: actions.map((a) => ({ action: a.action, title: a.title })),
    requireInteraction: actions.length > 0, // reminders stay until you act on them
  }));
});

async function openApp(url) {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const w of windows) {
    if ("focus" in w) return w.focus();
  }
  return self.clients.openWindow(url || "/");
}

async function runAction(action) {
  try {
    const res = await fetch("/api/v1/push/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data: action.action, sig: action.sig }),
    });
    const body = await res.json();
    await self.registration.showNotification("Your assistant", {
      body: res.ok ? body.reply : "Couldn't do that - open the app to try again.",
      icon: "/static/icon-192.png",
      tag: "action-result",
    });
  } catch {
    await openApp("/");
  }
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const action = (data.actions || []).find((a) => a.action === event.action);
  event.waitUntil(action ? runAction(action) : openApp(data.url));
});
