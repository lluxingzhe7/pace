/* 离线壳：静态资源缓存优先，接口请求不拦截（走 Pyodide） */
const CACHE = 'pace-shell-v3';
const CDN_CACHE = 'pace-cdn-v1';
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

/* 分享进来的文件先存进 IndexedDB，页面启动时再导入 */
function idbOpen() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('pace-web', 2);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('kv')) db.createObjectStore('kv');
      if (!db.objectStoreNames.contains('users')) db.createObjectStore('users', { keyPath: 'username' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function storeShared(items) {
  const db = await idbOpen();
  await new Promise((resolve, reject) => {
    const tx = db.transaction('kv', 'readwrite');
    tx.objectStore('kv').put(items, 'shared-files');
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function handleShare(request) {
  try {
    const form = await request.formData();
    const items = [];
    for (const file of form.getAll('files')) {
      if (!file || typeof file.arrayBuffer !== 'function') continue;
      items.push({ name: file.name || 'shared.gpx', type: file.type || '', data: await file.arrayBuffer() });
    }
    await storeShared(items);
    return Response.redirect('/?shared=1', 303);
  } catch (err) {
    return Response.redirect('/?shared=error', 303);
  }
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === 'POST' && url.pathname.endsWith('/share')) {
    event.respondWith(handleShare(event.request));
    return;
  }
  if (url.pathname.startsWith('/api/')) return;            // 接口由 Pyodide 处理
  // Pyodide 运行时（约 9 MB）单独缓存，第二次打开就不用再下
  if (/(^|\.)jsdelivr\.net$/.test(url.hostname)) {
    event.respondWith(caches.open(CDN_CACHE).then(cache =>
      cache.match(event.request).then(hit => hit || fetch(event.request).then(res => {
        if (res && (res.ok || res.type === 'opaque')) cache.put(event.request, res.clone());
        return res;
      }))));
    return;
  }
  if (url.origin !== location.origin) return;
  event.respondWith(
    caches.match(event.request).then(hit => hit || fetch(event.request).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(event.request, copy)).catch(() => {});
      return res;
    }))
  );
});
