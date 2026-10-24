'use strict';
const CACHE = 'field-companion-shell-v1';
const SHELL = ['index.html', 'style.css', 'app.js', 'audio-worklet.js', 'manifest.json', 'icon.svg'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('field-companion-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname === '/ws') return;
  const filename = url.pathname.endsWith('/') ? 'index.html' : url.pathname.split('/').pop();
  if (!SHELL.includes(filename)) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) { const copy = response.clone(); event.waitUntil(caches.open(CACHE).then(cache => cache.put(filename, copy))); }
    return response;
  }).catch(() => caches.match(filename)));
});
