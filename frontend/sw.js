/*
 * UpstreamWX service worker — offline shell + last-briefing cache (FR-26, FR-28).
 * Strategy:
 *   - App shell (HTML/CSS/JS/icons): network-first — serve the freshly deployed copy
 *     when online (so a deploy shows up on the next reload, no version bump or double
 *     reload needed) and fall back to the cached copy offline (FR-26). The previous
 *     stale-while-revalidate strategy served the cached shell first, so a deploy was
 *     invisible until a second reload — surprising during active iteration.
 *   - Briefing data: network-first, fall back to the cached copy when offline so
 *     the most recent fully generated briefing is reviewable with zero connectivity.
 * New briefing generation requires connectivity (FR-28); offline is review-only.
 */

// Cache namespace is tied to the deployed release: app.js registers this worker as
// `sw.js?v=<release>` (see version.json, docs/deployment-workflow.md). Each release is
// therefore a new worker URL, so the browser reinstalls and the activate handler below
// evicts the prior release's caches — no manual version bump in this file anymore.
const RELEASE = new URL(self.location).searchParams.get("v") || "dev";
const VERSION = `uwx-${RELEASE}`;
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
  "icons/cave.png",
  "icons/canyon.png",
  // PDF export template + its logo, so Export-to-PDF works offline (FR-27).
  "pdf/briefing-pdf.html",
  "pdf/logo-light.png",
  // Posture-label config the PDF (and app) read for approachable language.
  "data/display-config.json",
];

self.addEventListener("install", (event) => {
  // skipWaiting unconditionally — activation must not be gated on precache completing.
  // The fetch handler is network-first, so a partial precache still serves correct assets
  // online; the offline fallback fills in as each asset is fetched and cached normally.
  // Previously skipWaiting() was chained after addAll(), so a slow or failed asset fetch
  // (addAll is all-or-nothing) left the new SW stuck in "installing" indefinitely, forcing
  // multiple close/reopen cycles before the update applied.
  self.skipWaiting();
  event.waitUntil(
    caches.open(SHELL).then((cache) =>
      // Best-effort: one slow or missing asset must not stall the install or abort
      // the entire precache (FR-26 offline support for the other assets still applies).
      Promise.all(SHELL_ASSETS.map((url) => cache.add(url).catch(() => {})))
    )
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k).catch(() => {})))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  // Release stamp: always hit the network, never cache. The app polls this to detect a
  // new deploy, so a cached copy would mask the update (docs/deployment-workflow.md).
  if (url.pathname.endsWith("version.json")) return;

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

  // Shell: network-first. Fetch the deployed copy when online and refresh the cache;
  // fall back to the cached shell only when the network is unavailable (FR-26).
  event.respondWith(
    caches.open(SHELL).then(async (cache) => {
      try {
        const res = await fetch(request);
        if (res && res.ok && res.type === "basic") cache.put(request, res.clone());
        return res;
      } catch (e) {
        const cached = await cache.match(request);
        if (cached) return cached;
        throw e;
      }
    })
  );
});
