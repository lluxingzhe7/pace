/* 离线壳：静态资源缓存优先，接口请求不拦截（走 Pyodide） */
const CACHE = 'pace-shell-v1';
const ASSETS = [
  './', './index.html', './manifest.webmanifest', './favicon.svg',
  './css/app.css',
  './js/web-backend.js', './js/auth.js', './js/app.js', './js/api.js',
  './js/ui.js', './js/util.js', './js/icons.js', './js/chart.js', './js/map.js',
  './js/routing.js', './js/pages/dashboard.js', './js/pages/activities.js',
  './js/pages/detail.js', './js/pages/routebook.js', './js/pages/segments.js',
  './js/pages/segment_detail.js', './js/pages/compare.js', './js/pages/coach.js',
  './js/pages/devices.js', './js/pages/ble.js', './js/pages/settings.js',
  './py/pace.zip',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(k => k !== CACHE).map(k => caches.delete(k))
  )).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/api/')) return;            // 接口由 Pyodide 处理
  if (url.origin !== location.origin) return;              // CDN 交给浏览器缓存
  event.respondWith(
    caches.match(event.request).then(hit => hit || fetch(event.request).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(event.request, copy)).catch(() => {});
      return res;
    }))
  );
});
