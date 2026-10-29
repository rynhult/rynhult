// Rynhult service worker: shows push notifications and opens the right page when one is tapped.
// No offline cache on purpose: Rynhult is live data, and a stale copy would be misleading.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { title: event.data?.text() }; }
  const title = data.title || "Rynhult";
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    if (windows.some((client) => new URL(client.url).origin === self.location.origin && client.visibilityState === "visible")) return;
    await self.registration.showNotification(title, {
      body: data.body || "",
      tag: data.tag || undefined,          // a newer notification with the same tag replaces the older one
      renotify: Boolean(data.tag),
      requireInteraction: false,
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      data: { url: data.url || "/inbox" },
    });
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  let target = new URL(event.notification.data?.url || "/inbox", self.location.origin);
  if (target.origin !== self.location.origin) target = new URL("/inbox", self.location.origin);  // only ever open Rynhult itself
  const url = target.href;
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const existing = open.find((c) => new URL(c.url).origin === self.location.origin);
    if (existing) { await existing.focus(); return existing.navigate(url); }
    return self.clients.openWindow(url);
  })());
});
