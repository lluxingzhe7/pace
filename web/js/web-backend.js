/* 纯前端模式：用 Pyodide 在浏览器里跑 PACE 的 Python 引擎。
 *
 * 思路：server.py 的接口逻辑原样复用（pace/webbridge.py 造一个假连接），
 * 这里只做三件事：
 *   1. 启动 Pyodide、解包 pace.zip、从 IndexedDB 恢复数据库；
 *   2. 把 window.fetch 的 /api/* 请求转给 Python；
 *   3. 写操作之后把 SQLite 文件存回 IndexedDB。
 */
(function () {
  const PYODIDE_CDN = 'https://cdn.jsdelivr.net/pyodide/v0.26.4/full/';
  const state = { py: null, ready: false, booting: null, db: null, user: 'default' };
  function dbKey() { return 'db:' + (state.user || 'default'); }
  // 先抓住原始 fetch，免得被后面的 shim 换掉后自己调不到
  const rawFetch = window.fetch.bind(window);

  function enabled() {
    return !!window.PACE_STATIC;
  }

  /* ------------------------------------------------------------ IndexedDB */

  function idb() {
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

  async function idbGet(key) {
    const db = await idb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('kv', 'readonly');
      const req = tx.objectStore('kv').get(key);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbSet(key, value) {
    const db = await idb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('kv', 'readwrite');
      tx.objectStore('kv').put(value, key);
      tx.oncomplete = () => resolve(true);
      tx.onerror = () => reject(tx.error);
    });
  }

  /* ------------------------------------------------------------ 小工具 */

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function bytesToB64(buf) {
    const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }

  function overlay(text, pct, opts) {
    let el = document.getElementById('pyBoot');
    if (!el) {
      el = document.createElement('div');
      el.id = 'pyBoot';
      el.className = 'py-boot';
      document.body.appendChild(el);
    }
    const o = opts || {};
    const percent = Math.max(0, Math.min(100, Math.round(pct || 0)));
    el.innerHTML = '<div class="py-boot-card">' +
      (o.error ? '<div class="py-boot-err">!</div>' : '<div class="py-boot-spin"></div>') +
      '<b>' + (o.error ? '加载失败' : '正在加载分析引擎') + '</b>' +
      '<div class="py-boot-bar"><i style="width:' + percent + '%"></i></div>' +
      '<span>' + (text || '首次打开要下载约 10 MB，之后会缓存') + '</span>' +
      (o.error ? '<button class="btn btn-sm py-boot-retry" id="pyRetry">重试</button>' : '') +
      '</div>';
    el.hidden = false;
    const retry = document.getElementById('pyRetry');
    if (retry) retry.addEventListener('click', () => location.reload());
  }

  function hideOverlay() {
    const el = document.getElementById('pyBoot');
    if (el) el.hidden = true;
  }

  /* ------------------------------------------------------------ 启动 */

  async function ensurePyodide() {
    if (window.loadPyodide) return;
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = PYODIDE_CDN + 'pyodide.js';
      s.onload = resolve;
      s.onerror = () => reject(new Error('Pyodide 脚本加载失败（网络？）'));
      document.head.appendChild(s);
    });
  }

  async function boot(userKey) {
    if (!enabled()) return false;
    if (userKey) state.user = String(userKey);
    if (state.ready) return true;
    if (state.booting) return state.booting;
    state.booting = (async () => {
      const t0 = Date.now();
      overlay('① 下载 Python 运行时（约 9 MB）…', 8);
      await ensurePyodide();
      overlay('② 启动 Python 运行时…', 22);
      const py = await window.loadPyodide({ indexURL: PYODIDE_CDN });
      // sqlite3 在 Pyodide 里是「未内置」的包，要显式加载
      overlay('③ 加载数据库引擎 sqlite3…', 55);
      await py.loadPackage(['sqlite3']);
      state.py = py;
      overlay('④ 下载 PACE 引擎（约 160 KB）…', 70);
      const zipResp = await rawFetch('py/pace.zip', { cache: 'no-cache' });
      if (!zipResp.ok) throw new Error('pace.zip 下载失败: ' + zipResp.status);
      const zip = new Uint8Array(await zipResp.arrayBuffer());
      // 用 Python 自己的 zipfile 解包，比 JS 的 unpackArchive 稳
      py.FS.writeFile('/tmp/pace.zip', zip);
      py.runPython("import zipfile; zipfile.ZipFile('/tmp/pace.zip').extractall('/home/pyodide')");
      window.__py = py;

      overlay('⑤ 恢复本地数据…', 92);
      py.runPython("import os\nos.makedirs('/data', exist_ok=True)");
      const saved = await idbGet(dbKey());
      if (saved && saved.byteLength) {
        py.FS.writeFile('/data/pace.db', new Uint8Array(saved));
      }
      const info = py.runPython(
        "import json\nfrom pace import webbridge\njson.dumps(webbridge.init('/data/pace.db'))");
      state.db = JSON.parse(info);
      state.ready = true;
      hideOverlay();
      console.log('[PACE] 引擎就绪，用时 ' + ((Date.now() - t0) / 1000).toFixed(1) + ' 秒');
      window.dispatchEvent(new CustomEvent('pace:backend-ready'));
      return true;
    })().catch((err) => {
      state.booting = null;
      overlay(err.message || '未知错误', 0, { error: true });
      throw err;
    });
    return state.booting;
  }

  /* ------------------------------------------------------------ 请求转发 */

  function pyCall(method, path, bodyB64, headers) {
    const py = state.py;
    py.globals.set('__m', method);
    py.globals.set('__p', path);
    py.globals.set('__b', bodyB64 || '');
    py.globals.set('__h', py.toPy(headers || {}));
    const out = py.runPython(
      "import json\nfrom pace import webbridge\njson.dumps(webbridge.handle(__m, __p, __b, __h))");
    return JSON.parse(out);
  }

  async function persist(result) {
    if (!result.db_b64) return;
    try {
      const bytes = b64ToBytes(result.db_b64);
      await idbSet(dbKey(), bytes.buffer);
    } catch (err) {
      console.error('保存数据库失败', err);
    }
  }

  async function readBody(init) {
    if (!init || init.body == null) return { b64: '', text: '' };
    if (typeof init.body === 'string') {
      return { b64: bytesToB64(new TextEncoder().encode(init.body)), text: init.body };
    }
    if (init.body instanceof ArrayBuffer) return { b64: bytesToB64(init.body), text: '' };
    if (ArrayBuffer.isView(init.body)) return { b64: bytesToB64(init.body.buffer), text: '' };
    const txt = String(init.body);
    return { b64: bytesToB64(new TextEncoder().encode(txt)), text: txt };
  }

  function installFetchShim() {
    if (window.__paceFetchRaw) return;              // 只装一次
    window.__paceFetchRaw = rawFetch;
    window.fetch = async function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (url.indexOf('/api/') !== 0) return rawFetch(input, init);
      const method = ((init && init.method) || 'GET').toUpperCase();
      const headers = Object.assign({}, (init && init.headers) || {});
      const body = await readBody(init);

      // 账号接口由浏览器本地实现（不需要 Python 引擎）
      if (url.indexOf('/api/auth/') === 0 && window.PaceLocalAuth) {
        let payload = {};
        if (body.text) { try { payload = JSON.parse(body.text); } catch (e) { payload = {}; } }
        const out = await window.PaceLocalAuth.handle(method, url.split('?')[0], payload);
        return new Response(JSON.stringify(out.payload), {
          status: out.status, headers: { 'Content-Type': 'application/json' },
        });
      }

      if (!state.ready) {
        if (state.booting) { try { await state.booting; } catch (e) { /* 交给下面报错 */ } }
        if (!state.ready) {
          return new Response(JSON.stringify({ error: '引擎还没加载完，请稍候' }), {
            status: 503, headers: { 'Content-Type': 'application/json' },
          });
        }
      }
      const result = pyCall(method, url, body.b64, headers);
      if (result.db_b64) persist(result);
      const bytes = b64ToBytes(result.body_b64 || '');
      const respHeaders = new Headers();
      (result.headers || []).forEach(([k, v]) => {
        if (/^content-(type|encoding)$/i.test(k)) respHeaders.set(k, v);
      });
      return new Response(bytes, { status: result.status, headers: respHeaders });
    };
  }

  if (enabled()) installFetchShim();

  async function consumeShared() {
    const items = await idbGet('shared-files');
    if (!items || !items.length) return [];
    await idbSet('shared-files', []);
    return items.map(it => new File([it.data], it.name || 'shared.gpx',
      { type: it.type || 'application/octet-stream' }));
  }

  window.PaceWeb = {
    enabled,
    boot,
    consumeShared,
    ready: () => state.ready,
    get: idbGet,
    set: idbSet,
    reset: async () => {
      await idbSet(dbKey(), new ArrayBuffer(0));
      location.reload();
    },
  };
})();
