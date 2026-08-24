// Bump this on every deploy that changes cached files (or automate via build step)
const CACHE_VERSION = 'v1';
const CACHE_NAME = `portfolio-cache-${CACHE_VERSION}`;

const PRECACHE_URLS = [
    './',
    './index.html',
    './manifest.json'
];

// Install: pre-cache core files into a NEW cache (old cache untouched for now)
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(PRECACHE_URLS))
    );
    // Don't auto-activate yet — wait for the page to tell us to (via skipWaiting message)
    // This lets index.html show the "update available" banner before switching over.
});

// Activate: delete every OLD cache that doesn't match current version
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys
                    .filter(key => key !== CACHE_NAME)
                    .map(key => caches.delete(key))
            )
        ).then(() => self.clients.claim())
    );
});

// Fetch: network-first for HTML/JS so changes are picked up fast,
// falling back to cache when offline. Cache-first for other static assets.
self.addEventListener('fetch', (event) => {
    const req = event.request;
    const isHTMLorJS = req.destination === 'document' || req.destination === 'script';

    if (isHTMLorJS) {
        event.respondWith(
            fetch(req)
                .then(networkResp => {
                    const clone = networkResp.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(req, clone));
                    return networkResp;
                })
                .catch(() => caches.match(req))
        );
    } else {
        event.respondWith(
            caches.match(req).then(cached => cached || fetch(req))
        );
    }
});

// Let the page force this worker to activate immediately
self.addEventListener('message', (event) => {
    if (event.data === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});
