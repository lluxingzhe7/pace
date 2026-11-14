/* 极简 API 客户端：统一超时、错误解析、查询串拼接。 */
(function () {
  const BASE = '';

  function qs(params) {
    if (!params) return '';
    const usable = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
    return usable.length ? `?${usable.join('&')}` : '';
  }

  async function request(path, opts) {
    const options = Object.assign({ headers: {} }, opts);
    if (options.body && typeof options.body !== 'string' && !(options.body instanceof Blob)) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(options.body);
    }
    if (options.timeout) {
      const controller = new AbortController();
      options.signal = controller.signal;
      setTimeout(() => controller.abort(), options.timeout);
    }
    let resp;
    try {
      resp = await fetch(BASE + path, options);
    } catch (err) {
      if (err.name === 'AbortError') throw new Error('请求超时，服务器没有响应');
      throw new Error('无法连接到本地服务，请确认应用还在运行');
    }
    const type = resp.headers.get('Content-Type') || '';
    let payload = null;
    if (type.includes('application/json')) {
      try { payload = await resp.json(); } catch (e) { payload = null; }
    }
    if (!resp.ok) {
      if (resp.status === 401 && path.indexOf('/api/auth/') !== 0) {
        window.dispatchEvent(new CustomEvent('pace:unauth'));
      }
      const msg = (payload && (payload.error || payload.message)) || `请求失败 (${resp.status})`;
      const error = new Error(msg);
      error.status = resp.status;
      throw error;
    }
    return payload;
  }

  function withTz(params) {
    return Object.assign({ tz: U.tzOffset() }, params || {});
  }

  const api = {
    health: () => request('/api/health', { timeout: 8000 }),
    // 账号
    authStatus: () => request('/api/auth/status'),
    login: (body) => request('/api/auth/login', { method: 'POST', body }),
    register: (body) => request('/api/auth/register', { method: 'POST', body }),
    logout: () => request('/api/auth/logout', { method: 'POST' }),
    me: () => request('/api/auth/me'),
    changePassword: (body) => request('/api/auth/password', { method: 'POST', body }),
    authUsers: () => request('/api/auth/users'),
    meta: () => request('/api/meta'),
    dashboard: (params) => request('/api/dashboard' + qs(withTz(params))),
    activities: (params) => request('/api/activities' + qs(params)),
    activity: (id) => request(`/api/activities/${id}`),
    updateActivity: (id, body) => request(`/api/activities/${id}`, { method: 'PATCH', body }),
    deleteActivity: (id) => request(`/api/activities/${id}`, { method: 'DELETE' }),
    candidates: (id) => request(`/api/activities/${id}/segments/candidates`),
    compare: (ids) => request('/api/activities/compare' + qs(withTz({ ids: ids.join(',') }))),
    prs: (family) => request('/api/prs' + qs(family ? { family } : null)),
    segments: () => request('/api/segments'),
    segment: (id) => request(`/api/segments/${id}`),
    createSegment: (body) => request('/api/segments', { method: 'POST', body }),
    deleteSegment: (id) => request(`/api/segments/${id}`, { method: 'DELETE' }),
    rebuildSegment: (id) => request(`/api/segments/${id}/rebuild`, { method: 'POST' }),
    suggestSegments: () => request('/api/segments/suggest'),
    rebuildAllSegments: () => request('/api/segments/rebuild', { method: 'POST' }),
    recompute: () => request('/api/activities/recompute', { method: 'POST' }),
    // 路书
    routebooks: () => request('/api/routebooks'),
    routebook: (id) => request(`/api/routebooks/${id}`),
    createRoutebook: (body) => request('/api/routebooks', { method: 'POST', body }),
    updateRoutebook: (id, body) => request(`/api/routebooks/${id}`, { method: 'PUT', body }),
    deleteRoutebook: (id) => request(`/api/routebooks/${id}`, { method: 'DELETE' }),
    routebookFromActivity: (body) => request('/api/routebooks/from-activity', { method: 'POST', body }),
    settings: () => request('/api/settings'),
    saveSettings: (body) => request('/api/settings', { method: 'POST', body }),
    // 教练
    coach: (params) => request('/api/coach' + qs(withTz(params))),
    plan: (params) => request('/api/coach/plan' + qs(withTz(params))),
    coachIntake: () => request('/api/coach/intake' + qs(withTz({}))),
    saveIntake: (body) => request('/api/coach/intake', { method: 'POST', body }),
    buildProgram: (body) => request('/api/coach/program' + qs(withTz({})), { method: 'POST', body }),
    askCoach: (body) => request('/api/coach/ask' + qs(withTz({})), { method: 'POST', body }),
    llmStatus: () => request('/api/coach/llm'),
    testLlm: (body) => request('/api/coach/llm/test', { method: 'POST', body }),
    // 数据源 / 设备
    sources: () => request('/api/sources'),
    saveSources: (sources) => request('/api/sources', { method: 'POST', body: { sources } }),
    testSource: (id) => request('/api/sources/test', { method: 'POST', body: { id } }),
    syncSources: (body) => request('/api/sources/sync', { method: 'POST', body: body || {} }),
    // 蓝牙
    bleProtocols: () => request('/api/ble/protocols'),
    decodeBle: (uuid, hex) => request('/api/ble/decode', { method: 'POST', body: { uuid, hex } }),
    saveBleCapture: (capture) => request('/api/ble/captures', { method: 'POST', body: capture }),
    bleDevices: () => request('/api/ble/devices'),
    registerBleDevice: (body) => request('/api/ble/devices', { method: 'POST', body }),
    lookupBleDevice: (name) => request('/api/ble/lookup' + qs({ name })),
    // Strava OAuth
    stravaStatus: () => request('/api/strava/status'),
    stravaSave: (body) => request('/api/strava/config', { method: 'POST', body }),
    stravaDisconnect: () => request('/api/strava/config', { method: 'POST', body: { disconnect: true } }),
    /** 支持多文件顺序上传，回调进度 */
    async importFiles(files, onProgress) {
      const total = files.length;
      const results = [];
      for (let i = 0; i < total; i++) {
        const file = files[i];
        if (onProgress) onProgress(i, total, file.name);
        try {
          const buf = await file.arrayBuffer();
          const resp = await fetch('/api/import', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/octet-stream',
              'X-Filename': encodeURIComponent(file.name),
            },
            body: buf,
          });
          const data = await resp.json().catch(() => null);
          if (!resp.ok) {
            results.push({ file: file.name, status: 'error', reason: (data && data.error) || `HTTP ${resp.status}` });
          } else {
            (data.results || []).forEach(r => results.push(r));
          }
        } catch (err) {
          results.push({ file: file.name, status: 'error', reason: err.message });
        }
      }
      if (onProgress) onProgress(total, total, null);
      return results;
    },
  };

  window.api = api;
})();
