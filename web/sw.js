const CACHE_NAME = "genx71-autovj-v2.3";
const ASSETS = ["/", "/static/style.css?v=2.3", "/static/app.js?v=2.3", "/manifest.json", "/logo.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  if (event.request.url.includes("/api/") || event.request.url.includes("/output/")) {
    return;
  }
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
