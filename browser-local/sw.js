const CACHE_NAME = 'northstar-local-shell-v3';
const APP_FILES = [
  './',
  './index.html',
  './styles.css',
  './app.js',
  './manifest.webmanifest',
  'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.2',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      await cache.addAll(APP_FILES.slice(0, 5));
      try { await cache.add(APP_FILES[5]); } catch (_) { /* optional CDN cache */ }
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  const isAppAsset = url.origin === self.location.origin;
  const isTransformersAsset = url.hostname === 'cdn.jsdelivr.net' && url.pathname.includes('/@huggingface/transformers');
  if (!isAppAsset && !isTransformersAsset) return;
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request).then((response) => {
      const copy = response.clone();
      caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
      return response;
    }))
  );
});
