/* Service worker de l'appli enfant : précache de la coquille UNIQUEMENT (HTML, CSS, JS, icônes).
   Aucune réponse réseau n'est jamais ajoutée au cache : ni réponses d'API, ni audio, ni vidéo, ni texte dicté. */
const PRECACHE = [
  "/",
  "/index.html",
  "/tokens-enfant.css",
  "/shell.css",
  "/theme.js",
  "/shell.js",
  "/status.js",
  "/mimo.svg",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/maskable-512.png",
  "/fonts/tajawal-arabic-500.woff2",
  "/fonts/tajawal-arabic-700.woff2",
  "/fonts/tajawal-arabic-800.woff2"
];

// Nom du cache dérivé du contenu de PRECACHE (FNV-1a 32 bits) : toute modification de la liste
// crée un nouveau cache et l'activation supprime l'ancien.
function hashList(list) {
  let h = 0x811c9dc5;
  for (const c of list.join("|")) {
    h = Math.imul(h ^ c.codePointAt(0), 0x01000193) >>> 0;
  }
  return h.toString(16);
}
const CACHE = "mimo-shell-" + hashList(PRECACHE);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Navigations : réseau d'abord, coquille en cache seulement si le réseau échoue.
// Ressources de la liste : servies depuis le cache. Tout le reste passe au réseau tel quel.
self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/index.html")));
  } else if (PRECACHE.includes(url.pathname)) {
    event.respondWith(caches.match(url.pathname).then((hit) => hit || fetch(req)));
  }
});
