/* Service Worker de Finanzas Familia — cache del "shell" para carga rápida y
   un fallback offline básico. Los datos (API, dashboard) siempre van a la red. */
const CACHE = "finanzas-v2";
const SHELL = ["/static/style.css", "/static/app.js", "/static/manifest.webmanifest",
  "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Estáticos: stale-while-revalidate. Sirve rápido desde cache PERO siempre
  // revalida en segundo plano, así tras un deploy la siguiente carga ya trae
  // el app.js/style.css nuevos (no se quedan pegados en una versión vieja).
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.open(CACHE).then(async (cache) => {
      const hit = await cache.match(req);
      const red = fetch(req).then((res) => {
        if (res && res.ok) cache.put(req, res.clone());
        return res;
      }).catch(() => hit);
      return hit || red;
    }));
    return;
  }
  // Resto (páginas, API): red primero, con fallback al cache si no hay conexión.
  e.respondWith(fetch(req).catch(() => caches.match(req)));
});
