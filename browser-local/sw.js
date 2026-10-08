// v4: escuta dinâmica, idioma configurável, seleção de modelo (Fase 2). Mudar a versão
// força todos os clientes a substituir o cache do "app shell" (verificação de
// integridade simples: nunca serve HTML/JS antigo depois de um deploy novo).
const CACHE_NAME = 'northstar-local-shell-v4';
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
  // Navegação (ex.: reabrir a página offline): cai sempre para o index.html em cache
  // em vez de falhar, para a PWA continuar a abrir sem o PC/servidor de origem.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() => caches.match('./index.html'))
    );
    return;
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request).then((response) => {
      const copy = response.clone();
      caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
      return response;
    }).catch(() => cached))
  );
});
