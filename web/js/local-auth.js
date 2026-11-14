/* 纯前端模式的本地账号：注册 / 登录 / 改密 / 退出，数据和网页版一样按人隔离。
 *
 * 注意：这是**本机浏览器账号**，不是服务器账号——它防止别人随手打开你的 PACE，
 * 但数据仍在浏览器 IndexedDB 里，懂开发者工具的人能看。 */
(function () {
  const DB_NAME = 'pace-web';
  const DB_VER = 2;
  const STORE_KV = 'kv';
  const STORE_USERS = 'users';
  const SESSION_KEY = 'pace.session';
  const ITER = 120000;

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VER);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE_KV)) db.createObjectStore(STORE_KV);
        if (!db.objectStoreNames.contains(STORE_USERS)) db.createObjectStore(STORE_USERS, { keyPath: 'username' });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function tx(store, mode, fn) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const t = db.transaction(store, mode);
      const req = fn(t.objectStore(store));
      t.oncomplete = () => resolve(req ? req.result : undefined);
      t.onerror = () => reject(t.error);
    });
  }

  const users = {
    all: () => tx(STORE_USERS, 'readonly', s => s.getAll()),
    get: (name) => tx(STORE_USERS, 'readonly', s => s.get(name)),
    put: (u) => tx(STORE_USERS, 'readwrite', s => s.put(u)),
  };

  function b64(buf) {
    return btoa(String.fromCharCode.apply(null, new Uint8Array(buf)));
  }
  function unb64(str) {
    const bin = atob(str);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  async function hashPassword(password, saltB64) {
    const salt = saltB64 ? unb64(saltB64) : crypto.getRandomValues(new Uint8Array(16));
    const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(password),
      { name: 'PBKDF2' }, false, ['deriveBits']);
    const bits = await crypto.subtle.deriveBits(
      { name: 'PBKDF2', salt, iterations: ITER, hash: 'SHA-256' }, key, 256);
    return { salt: b64(salt), hash: b64(bits) };
  }

  function publicUser(u) {
    return { id: u.username, username: u.username, email: u.email || '', role: 'owner', status: 'active' };
  }

  function session() {
    try {
      const raw = localStorage.getItem(SESSION_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      if (!data.exp || data.exp < Date.now()) { localStorage.removeItem(SESSION_KEY); return null; }
      return data;
    } catch (e) { return null; }
  }

  function setSession(username, days) {
    const exp = Date.now() + (days || 14) * 86400000;
    localStorage.setItem(SESSION_KEY, JSON.stringify({ username, exp }));
  }

  async function currentUser() {
    const s = session();
    if (!s) return null;
    const u = await users.get(s.username);
    return u ? publicUser(u) : null;
  }

  function jsonResponse(status, payload) {
    return { status, payload };
  }

  async function handle(method, path, body) {
    const data = body || {};
    if (path === '/api/auth/status') {
      const list = await users.all();
      const u = await currentUser();
      return jsonResponse(200, {
        needs_setup: list.length === 0,
        registration_open: true,
        users_count: list.length,
        user: u,
        static_local: true,
      });
    }
    if (path === '/api/auth/me') {
      const u = await currentUser();
      return u ? jsonResponse(200, { user: u }) : jsonResponse(401, { error: '未登录' });
    }
    if (path === '/api/auth/register') {
      const username = String(data.username || '').trim();
      const password = String(data.password || '');
      if (!/^[\w\u4e00-\u9fa5-]{2,24}$/.test(username)) {
        return jsonResponse(400, { error: '用户名 2–24 个字符，只能用中英文/数字/下划线' });
      }
      if (password.length < 8) return jsonResponse(400, { error: '密码至少 8 位' });
      if (!/[a-zA-Z]/.test(password) || !/[0-9]/.test(password)) {
        return jsonResponse(400, { error: '密码要同时包含字母和数字' });
      }
      if (await users.get(username)) return jsonResponse(409, { error: '用户名已被占用' });
      const { salt, hash } = await hashPassword(password);
      const rec = { username, email: String(data.email || '').trim(), salt, hash, created: Date.now() };
      await users.put(rec);
      setSession(username, 60);
      return jsonResponse(200, { user: publicUser(rec), expires_at: Date.now() + 60 * 86400000 });
    }
    if (path === '/api/auth/login') {
      const username = String(data.username || '').trim();
      const u = await users.get(username);
      if (!u) return jsonResponse(401, { error: '用户名或密码不对' });
      const { hash } = await hashPassword(String(data.password || ''), u.salt);
      if (hash !== u.hash) return jsonResponse(401, { error: '用户名或密码不对' });
      setSession(username, data.remember ? 60 : 14);
      return jsonResponse(200, { user: publicUser(u), expires_at: Date.now() + 14 * 86400000 });
    }
    if (path === '/api/auth/logout') {
      localStorage.removeItem(SESSION_KEY);
      return jsonResponse(200, { ok: true });
    }
    if (path === '/api/auth/password') {
      const u = await currentUser();
      if (!u) return jsonResponse(401, { error: '未登录' });
      const rec = await users.get(u.username);
      const oldHash = (await hashPassword(String(data.old_password || ''), rec.salt)).hash;
      if (oldHash !== rec.hash) return jsonResponse(403, { error: '原密码不对' });
      const pw = String(data.new_password || '');
      if (pw.length < 8 || !/[a-zA-Z]/.test(pw) || !/[0-9]/.test(pw)) {
        return jsonResponse(400, { error: '新密码至少 8 位，且包含字母和数字' });
      }
      const fresh = await hashPassword(pw);
      await users.put(Object.assign({}, rec, { salt: fresh.salt, hash: fresh.hash }));
      return jsonResponse(200, { ok: true });
    }
    return jsonResponse(404, { error: '本地模式没有这个接口' });
  }

  window.PaceLocalAuth = {
    handle,
    currentUser,
    session,
    logout: () => localStorage.removeItem(SESSION_KEY),
    listUsers: () => users.all(),
  };
})();
