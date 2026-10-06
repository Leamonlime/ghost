/* Ghost — Habits & Todos service worker (Session 83, PWA shell).
 * Purpose: the home-screen icon opens INSTANTLY into the app shell even while Tailscale is
 * reconnecting — it does NOT fake data. Live data (/api/*) is always network-only, never cached,
 * so the board is never stale; only the static shell (HTML, CSS, icons, manifest) is cached.
 * Served from /sw.js so its scope is the whole origin (it must control /ghost-habits.html). */
const CACHE = "ghost-habits-v1";
const SHELL = [
  "/ghost-habits.html",
  "/assets/ghost-common.css?v=61",
  "/assets/ghost-icon-192.png",
  "/assets/ghost-icon-512.png",
  "/manifest.webmanifest"
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      // individual adds so one 404 (e.g. a bumped ?v=) can't fail the whole install
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Never touch live data or webhooks — always straight to network (fails offline, correctly).
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/webhook/")) return;

  // App shell HTML: network-first (so edits land when online), fall back to cache offline.
  if (req.mode === "navigate" || url.pathname === "/ghost-habits.html") {
    e.respondWith(
      fetch(req)
        .then((r) => { const cp = r.clone(); caches.open(CACHE).then((c) => c.put("/ghost-habits.html", cp)); return r; })
        .catch(() => caches.match("/ghost-habits.html"))
    );
    return;
  }

  // Static assets (css/js/icons/manifest): cache-first, refresh in the background.
  if (url.pathname.startsWith("/assets/") || url.pathname === "/manifest.webmanifest") {
    e.respondWith(
      caches.match(req).then((hit) =>
        hit || fetch(req).then((r) => { const cp = r.clone(); caches.open(CACHE).then((c) => c.put(req, cp)); return r; })
      )
    );
    return;
  }
});
