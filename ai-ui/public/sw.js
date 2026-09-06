// SPDX-License-Identifier: MIT
// ============================================================
// AiNxt Enterprise — Service Worker
// Strategy:
//   navigate  → network-first  (index.html NEVER cached)
//   /assets/* → cache-first    (hashed filenames, immutable)
//   everything else → network-only (API calls, SSE, etc.)
// ============================================================

// CACHE_NAME is stamped at build time (scripts/stamp-sw.js replaces
// __BUILD_HASH__ with a unique per-build token). This is critical:
//  1. It changes the bytes of sw.js every deploy, so the browser actually
//     detects a service-worker update (updatefound → SKIP_WAITING → reload).
//  2. The activate handler deletes every cache whose name !== CACHE_NAME,
//     so each deploy purges the previous build's cached assets.
// If the placeholder is ever left unstamped (e.g. running public/sw.js raw),
// it falls back to a literal string so the SW still functions in dev.
const CACHE_NAME = "ainxt-__BUILD_HASH__";

// Match only content-hashed static asset extensions.
// Anything else (API paths, navigate) is handled separately.
const STATIC_EXT_RE = /\.(js|css|png|svg|ico|webp|gif|jpg|jpeg|woff2?|ttf|eot)(\?.*)?$/i;

// The app is served under a base path (e.g. /portal/). The SW is registered
// at <base>/sw.js, so its scope IS the base path. Derive it from the scope so
// cached shell/manifest URLs match how the app is actually served — using a
// hardcoded "/index.html" would never match "/portal/index.html".
const BASE = new URL(self.registration.scope).pathname; // e.g. "/portal/"
const INDEX_URL = BASE + "index.html";                  // e.g. "/portal/index.html"
const MANIFEST_URL = BASE + "manifest.json";            // e.g. "/portal/manifest.json"

// ── Install ──────────────────────────────────────────────────
// Pre-cache the manifest only. index.html is intentionally NOT pre-cached:
// snapshotting it here is what caused stale UI (the cached shell kept
// pointing at old hashed asset filenames). Instead, navigation is
// network-first and we cache a fresh copy of index.html on each successful
// navigation (see fetch handler) purely as an OFFLINE fallback.

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      cache.addAll([MANIFEST_URL])
    )
  );
  // Activate immediately — don't wait for old tabs to close.
  self.skipWaiting();
});

// ── Activate ─────────────────────────────────────────────────
// Delete every cache that doesn't match the current version.
// clients.claim() inside the Promise ensures it runs AFTER old caches
// are cleared, then triggers controllerchange on all open pages.

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

// ── Fetch ─────────────────────────────────────────────────────

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Only intercept GET requests on our own origin.
  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return; // non-GET or cross-origin → browser handles normally
  }

  // ── 1. Navigation requests: network-first ─────────────────
  // Try network first for fresh content, fallback to cached index.html
  // if offline or server returns error (e.g., 404 on direct route access).
  // if (request.mode === "navigate") {
  //   event.respondWith(
  //     fetch(request).catch(() =>
  //       // Offline fallback — serve cached index.html for SPA routes
  //       caches.match("/index.html").then(
  //         (cached) => cached || new Response("Offline — please reconnect.", {
  //           status: 503,
  //           headers: { "Content-Type": "text/plain" },
  //         })
  //       )
  //     )
  //   );
  //   return;
  // }

  if (request.mode === "navigate") {
  event.respondWith(
    fetch(request).then((response) => {
      // ✅ If backend returns 404/500, fall back to a cached index.html (if any)
      if (!response.ok) {
        return caches.match(INDEX_URL).then(
          (cached) => cached || response
        );
      }
      // ✅ Fresh, OK navigation — refresh the offline fallback copy so an
      // offline visit later serves the latest shell we successfully loaded.
      // We always return the live network response, so online users never
      // see a stale index.html.
      const clone = response.clone();
      caches.open(CACHE_NAME).then((cache) => cache.put(INDEX_URL, clone));
      return response;
    }).catch(() =>
      // ✅ Network error (offline, DNS, etc.) — serve last-known-good shell
      caches.match(INDEX_URL).then(
        (cached) => cached || new Response("Offline", { status: 503 })
      )
    )
  );
  return;
}


  // ── 2. Static assets: cache-first ─────────────────────────
  // Only cache files with content-hashed extensions (JS, CSS, fonts, images).
  // Safe to cache forever — Vite changes the hash on every build, so stale
  // entries are never served for updated files.
  if (STATIC_EXT_RE.test(url.pathname)) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;

        return fetch(request).then((response) => {
          if (
            response &&
            response.status === 200 &&
            response.type === "basic"
          ) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // ── 3. Everything else: network-only ──────────────────────
  // Covers all API paths (/ask, /auth, /chats, /sdlc, /agents, etc.),
  // SSE streams, and any non-static same-origin request.
  // No respondWith() → browser handles the request natively.
});

// ── Message handler ───────────────────────────────────────────
// The page sends SKIP_WAITING when updatefound fires so the new SW
// activates immediately without waiting for all tabs to close.

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

// ── Push Notifications ───────────────────────────────────────

self.addEventListener("push", (event) => {
  let data = { title: "AiNxt", body: "New notification" };
  try { data = event.data.json(); } catch (_) {}

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body:    data.body,
      icon:    BASE + "icons/icon-192x192.png",
      badge:   BASE + "icons/icon-72x72.png",
      tag:     data.tag || "ainxt",
      data:    data.url || BASE,
      actions: data.actions || [],
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data || BASE;
  event.waitUntil(
    clients.matchAll({ type: "window" }).then((windowClients) => {
      for (const client of windowClients) {
        if (client.url === url && "focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
