/*
 * UpstreamWX service worker — offline shell + last-briefing cache (FR-26, FR-28).
 * Strategy:
 *   - App shell (HTML/CSS/JS/icons): stale-while-revalidate — serve the cached
 *     copy instantly (offline-capable, FR-26), but always fetch a fresh copy in
 *     the background and update the cache so the next load converges on the
 *     latest deploy without needing a service-worker version bump.
 *   - Briefing data: network-first, fall back to the cached copy when offline so
 *     the most recent fully generated briefing is reviewable with zero connectivity.
 * New briefing generation requires connectivity (FR-28); offline is review-only.
 */

const VERSION = "uwx-v6";
const SHELL = `${VERSION}-shell`;
const DATA = `${VERSION}-data`;

const SHELL_ASSETS = [
  "./",
  "index.html",
  "manifest.webmanifest",
  "styles/tokens.css",
  "styles/app.css",
  "js/app.js",
  "js/icons.js",
  "icons/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  // Briefing data: network-first, cache fallback (FR-26).
  if (url.pathname.endsWith("sample-briefing.json") || url.pathname.includes("/v1/briefing")) {
    event.respondWith(
      fetch(request)
        .then((res) => {
          const copy = res.clone();
          caches.open(DATA).then((c) => c.put(request, copy));
          return res;
        })
        .catch(async () => {
          const cached = await caches.match(request);
          if (cached) {
            const body = await cached.blob();
            const headers = new Headers(cached.headers);
            headers.set("x-from-sw-cache", "1");
            return new Response(body, { headers, status: 200 });
          }
          return new Response("{}", { status: 503, headers: { "Content-Type": "application/json" } });
        })
    );
    return;
  }

  // Shell: stale-while-revalidate. Serve cache immediately; refresh it in the
  // background so the next navigation picks up new CSS/JS without a version bump.
  event.respondWith(
    caches.open(SHELL).then((cache) =>
      cache.match(request).then((cached) => {
        const network = fetch(request)
          .then((res) => {
            if (res && res.ok && res.type === "basic") cache.put(request, res.clone());
            return res;
          })
          .catch(() => cached);
        return cached || network;
      })
    )
  );
});
