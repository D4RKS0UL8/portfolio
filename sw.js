const CACHE_NAME = 'portfolio-v1';
const assets = [
    './',
    './index.html',
    './manifest.json'
];

// Install and cache files offline
self.addEventListener('install', e => {
    e.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(assets))
    );
});

// Serve cached content when offline
self.addEventListener('fetch', e => {
    e.respondWith(
        caches.match(e.request).then(cachedResponse => {
            return cachedResponse || fetch(e.request);
        })
    );
});