/* 账号门禁：未登录时挡住整个应用，只给登录/注册。 */
(function () {
  const state = { status: null, mode: 'login', busy: false, error: '', notice: '' };

  function gateEl() { return document.getElementById('authGate'); }

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g,
      ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  function authLock(locked) {
    document.body.classList.toggle('auth-locked', !!locked);
    const g = gateEl();
    if (g) g.hidden = !locked;
  }

  function render() {
    const g = gateEl();
    if (!g) return;
    const s = state.status || {};
    const isSetup = !!s.needs_setup;
    const isLogin = state.mode === 'login' && !isSetup;

    const title = isSetup ? '创建站长账号' : (isLogin ? '登录 PACE' : '注册新账号');
    const sub = isSetup
      ? (s.legacy_pending
        ? '这台机器上已有数据：创建账号后，原有活动、路书、设置都会迁进这个账号。'
        : '第一次使用，先给自己建一个账号。')
      : (isLogin ? '用你的账号登录，数据只属于你。' : '注册后拥有独立的数据库，别人看不到你的数据。');

    g.innerHTML = `
      <div class="auth-panel">
        <div class="auth-brand">
          <span class="brand-mark">
            <svg viewBox="0 0 32 32" aria-hidden="true"><path d="M4 22 L11 11 L17 18 L23 8 L28 15" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><circle cx="23" cy="8" r="2.6" fill="currentColor"/></svg>
          </span>
          <span class="brand-text"><b>PACE</b><small>运动数据分析</small></span>
        </div>
        <div class="card auth-card">
          <h1 class="auth-title">${esc(title)}</h1>
          <p class="tiny dim auth-sub">${esc(sub)}</p>
          ${state.error ? `<p class="auth-error">${esc(state.error)}</p>` : ''}
          <form id="authForm" autocomplete="on">
            <label class="auth-field">用户名
              <input class="control" name="username" autocomplete="username" required
                     placeholder="2–24 个字符" value="${esc(state.username || '')}">
            </label>
            ${isLogin ? '' : `
            <label class="auth-field">邮箱（可选）
              <input class="control" name="email" type="email" autocomplete="email" placeholder="找回密码用，可留空">
            </label>`}
            <label class="auth-field">密码
              <input class="control" name="password" type="password" required
                     autocomplete="${isLogin ? 'current-password' : 'new-password'}"
                     placeholder="至少 8 位，字母+数字">
            </label>
            ${isLogin ? `
            <label class="auth-check">
              <input type="checkbox" name="remember" checked> 记住我（60 天不用重登）
            </label>` : `
            <label class="auth-field">确认密码
              <input class="control" name="password2" type="password" required autocomplete="new-password">
            </label>`}
            <button class="btn btn-primary btn-block" type="submit" ${state.busy ? 'disabled' : ''}>
              ${state.busy ? '处理中…' : (isSetup ? '创建并进入' : (isLogin ? '登录' : '注册并进入'))}
            </button>
          </form>
          <div class="auth-alt tiny dim">
            ${isSetup ? '' : (isLogin
              ? (s.registration_open
                ? '还没有账号？<button class="linklike" data-mode="register">注册一个</button>'
                : '注册已关闭，找站长给你开账号')
              : '已有账号？<button class="linklike" data-mode="login">去登录</button>')}
          </div>
        </div>
        <p class="tiny dim auth-foot">数据存在这台服务器上 · 每个账号独立</p>
      </div>`;

    const form = g.querySelector('#authForm');
    form.addEventListener('submit', onSubmit);
    g.querySelectorAll('[data-mode]').forEach(btn => {
      btn.addEventListener('click', () => { state.mode = btn.dataset.mode; state.error = ''; render(); });
    });
    const first = form.querySelector('input[name=username]');
    if (first) first.focus();
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (state.busy) return;
    const form = event.target;
    const data = new FormData(form);
    const username = String(data.get('username') || '').trim();
    const password = String(data.get('password') || '');
    const isSetup = !!(state.status || {}).needs_setup;
    const isLogin = state.mode === 'login' && !isSetup;
    state.username = username;
    state.error = '';
    if (!isLogin) {
      const again = String(data.get('password2') || '');
      if (password !== again) { state.error = '两次输入的密码不一样'; render(); return; }
    }
    state.busy = true;
    render();
    try {
      if (isLogin) {
        await api.login({ username, password, remember: !!data.get('remember') });
      } else {
        await api.register({ username, password, email: String(data.get('email') || '').trim() });
      }
      location.reload();
    } catch (err) {
      state.busy = false;
      state.error = err.message || '请求失败';
      render();
    }
  }

  async function boot() {
    try {
      state.status = await api.authStatus();
    } catch (err) {
      // 服务没起来：让 app.js 走原来的错误提示
      return true;
    }
    if (state.status && state.status.user) {
      window.PaceUser = state.status.user;
      authLock(false);
      return true;
    }
    state.mode = state.status.needs_setup ? 'register' : 'login';
    authLock(true);
    render();
    return false;
  }

  async function logout() {
    try { await api.logout(); } catch (err) { /* ignore */ }
    location.reload();
  }

  /* 顶栏上的账号按钮 + 改密对话框 */
  function openAccount() {
    const user = window.PaceUser || {};
    const dlg = document.createElement('dialog');
    dlg.className = 'dialog';
    dlg.innerHTML = `
      <form method="dialog" class="dialog-head">
        <h2>账号</h2>
        <button class="icon-btn" value="cancel" aria-label="关闭"><span data-icon="x"></span></button>
      </form>
      <div class="dialog-body">
        <p class="tiny dim" style="margin-bottom:12px">
          当前账号：<b>${esc(user.username)}</b>${user.role === 'owner' ? ' · 站长' : ''}
        </p>
        <form id="pwdForm" style="display:flex;flex-direction:column;gap:8px">
          <label class="auth-field">原密码
            <input class="control" name="old_password" type="password" required autocomplete="current-password">
          </label>
          <label class="auth-field">新密码
            <input class="control" name="new_password" type="password" required
                   autocomplete="new-password" placeholder="至少 8 位，字母+数字">
          </label>
          <button class="btn btn-primary" type="submit">改密码</button>
        </form>
        <div class="row" style="gap:8px;justify-content:flex-end;margin-top:14px">
          <button class="btn" id="authLogout">退出登录</button>
        </div>
      </div>`;
    document.body.appendChild(dlg);
    if (window.Icon && Icon.hydrate) Icon.hydrate(dlg);
    dlg.querySelector('#pwdForm').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      try {
        await api.changePassword({
          old_password: fd.get('old_password'),
          new_password: fd.get('new_password'),
        });
        UI.toast('密码已改，请重新登录', 'ok');
        setTimeout(() => location.reload(), 700);
      } catch (err) {
        UI.toast(err.message || '改密码失败', 'err');
      }
    });
    dlg.querySelector('#authLogout').addEventListener('click', () => { dlg.close(); logout(); });
    dlg.addEventListener('close', () => dlg.remove());
    dlg.showModal();
  }

  function mountChip() {
    const user = window.PaceUser;
    if (!user) return;
    const right = document.querySelector('.topbar-right');
    if (!right || document.getElementById('btnUser')) return;
    const btn = document.createElement('button');
    btn.className = 'btn btn-sm btn-ghost auth-chip';
    btn.id = 'btnUser';
    btn.title = '账号设置 / 退出';
    btn.textContent = user.username;
    btn.addEventListener('click', openAccount);
    right.appendChild(btn);
  }

  // 任何接口返回 401：说明会话过期，重新走门禁
  window.addEventListener('pace:unauth', () => { location.reload(); });

  window.Auth = {
    boot,
    logout,
    mountChip,
    openAccount,
    user: () => window.PaceUser || null,
    esc,
  };
})();
