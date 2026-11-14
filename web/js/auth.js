/* 账号门禁 + 登录/注册/忘记密码/重置密码 四个页面。 */
(function () {
  const PAGES = {
    login: { title: '欢迎回来', sub: '登录后继续看你的训练数据', action: '登录' },
    register: { title: '创建账号', sub: '每人一个独立数据库，互不可见', action: '注册' },
    forgot: { title: '找回密码', sub: '填注册邮箱，我们把验证码发给你', action: '发送验证码' },
    reset: { title: '重置密码', sub: '输入邮箱收到的 6 位验证码', action: '重置密码' },
  };
  const state = { status: null, busy: false, error: '', notice: '', email: '', cooldown: 0 };

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function currentPage() {
    const name = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
    return PAGES[name] ? name : 'login';
  }

  function go(page) {
    state.error = '';
    if (location.hash === '#/' + page) render();
    else location.hash = '#/' + page;
  }

  function gateEl() { return document.getElementById('authGate'); }

  function lock(on) {
    document.body.classList.toggle('auth-locked', !!on);
    const g = gateEl();
    if (g) g.hidden = !on;
  }

  const LOGO = `<svg viewBox="0 0 32 32" aria-hidden="true">
    <path d="M4 22 L11 11 L17 18 L23 8 L28 15" fill="none" stroke="currentColor"
      stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="23" cy="8" r="2.6" fill="currentColor"/></svg>`;

  function field(label, name, opts) {
    const o = opts || {};
    return `<label class="auth-field">
      <span>${esc(label)}${o.optional ? '<i class="auth-opt">可选</i>' : ''}</span>
      <input class="control" name="${name}" type="${o.type || 'text'}"
        ${o.required === false ? '' : 'required'}
        autocomplete="${o.autocomplete || 'off'}"
        placeholder="${esc(o.placeholder || '')}"
        ${o.value ? 'value="' + esc(o.value) + '"' : ''}
        ${o.maxlength ? 'maxlength="' + o.maxlength + '"' : ''}>
    </label>`;
  }

  function formHtml(page) {
    const s = state.status || {};
    if (page === 'login') {
      return field('用户名或邮箱', 'username', { autocomplete: 'username', placeholder: '输入用户名' })
        + field('密码', 'password', { type: 'password', autocomplete: 'current-password', placeholder: '输入密码' })
        + `<label class="auth-check"><input type="checkbox" name="remember" checked> 记住我（60 天不用重登）</label>`;
    }
    if (page === 'register') {
      return field('用户名', 'username', { autocomplete: 'username', placeholder: '2–24 个字符，中英文/数字/下划线', value: state.username })
        + field('邮箱', 'email', { type: 'email', autocomplete: 'email', placeholder: '用来找回密码，建议填', optional: true })
        + field('密码', 'password', { type: 'password', autocomplete: 'new-password', placeholder: '至少 8 位，字母+数字' })
        + field('确认密码', 'password2', { type: 'password', autocomplete: 'new-password', placeholder: '再输一次' });
    }
    if (page === 'forgot') {
      return field('注册邮箱', 'email', { type: 'email', autocomplete: 'email', placeholder: '你注册时填的邮箱' });
    }
    return field('注册邮箱', 'email', { type: 'email', autocomplete: 'email', placeholder: '你注册时填的邮箱', value: state.email })
      + field('验证码', 'code', { placeholder: '6 位数字', maxlength: 6 })
      + field('新密码', 'password', { type: 'password', autocomplete: 'new-password', placeholder: '至少 8 位，字母+数字' })
      + field('确认新密码', 'password2', { type: 'password', autocomplete: 'new-password', placeholder: '再输一次' });
  }

  function footerHtml(page) {
    if (page === 'login') {
      return `<div class="auth-links">
        <button type="button" class="linklike" data-go="forgot">忘记密码？</button>
        <span class="auth-sep">·</span>
        <button type="button" class="linklike" data-go="register">注册新账号</button>
      </div>`;
    }
    if (page === 'register') {
      return `<div class="auth-links">已有账号？<button type="button" class="linklike" data-go="login">去登录</button></div>`;
    }
    if (page === 'forgot') {
      return `<div class="auth-links">
        <button type="button" class="linklike" data-go="login">返回登录</button>
        <span class="auth-sep">·</span>
        <button type="button" class="linklike" data-go="reset">已有验证码，去重置</button>
      </div>`;
    }
    return `<div class="auth-links">
      <button type="button" class="linklike" data-go="forgot">重新获取验证码</button>
      <span class="auth-sep">·</span>
      <button type="button" class="linklike" data-go="login">返回登录</button>
    </div>`;
  }

  function render() {
    const g = gateEl();
    if (!g) return;
    const page = currentPage();
    const meta = PAGES[page];
    const s = state.status || {};

    g.innerHTML = `
      <div class="auth-shell">
        <aside class="auth-art">
          <div class="auth-art-grid"></div>
          <div class="auth-art-glow"></div>
          <div class="auth-art-inner">
            <div class="auth-logo">${LOGO}<b>PACE</b></div>
            <h2>把每一次骑行<br><em>都算清楚</em></h2>
            <p class="auth-art-sub">本地优先的运动数据分析 · 自托管</p>
            <ul class="auth-points">
              <li><i class="pt-dot"></i>每人一个独立数据库，物理隔离</li>
              <li><i class="pt-dot"></i>GPX / FIT / TCX / CSV 直接导入</li>
              <li><i class="pt-dot"></i>教练计划、路书规划、爬坡分级</li>
            </ul>
          </div>
          <div class="auth-art-foot">v1.1 · 数据只存在这台服务器上</div>
        </aside>

        <main class="auth-main">
          <div class="auth-box">
            <h1>${esc(meta.title)}</h1>
            <p class="auth-sub">${esc(meta.sub)}</p>
            ${s.needs_setup && page === 'register'
              ? `<p class="auth-hint">这台机器上已有旧数据，创建后会自动迁进这个账号。</p>` : ''}
            ${page === 'register' && s.registration_open === false
              ? `<p class="auth-hint warn">站长关闭了注册，需要账号请找站长开。</p>` : ''}
            ${state.error ? `<p class="auth-error">${esc(state.error)}</p>` : ''}
            ${state.notice ? `<p class="auth-notice">${esc(state.notice)}</p>` : ''}
            <form id="authForm" novalidate>
              ${formHtml(page)}
              <button class="btn btn-primary btn-block auth-submit" type="submit" ${state.busy ? 'disabled' : ''}>
                ${state.busy ? '请稍候…' : (state.cooldown > 0 ? state.cooldown + ' 秒后可重发' : meta.action)}
              </button>
            </form>
            ${footerHtml(page)}
          </div>
        </main>
      </div>`;

    const form = g.querySelector('#authForm');
    form.addEventListener('submit', onSubmit);
    g.querySelectorAll('[data-go]').forEach(btn => {
      btn.addEventListener('click', () => go(btn.dataset.go));
    });
    const first = form.querySelector('input[name=username], input[name=email]');
    if (first) first.focus();
  }

  function cooldownTick() {
    if (state.cooldown > 0) {
      state.cooldown -= 1;
      const btn = gateEl() && gateEl().querySelector('.auth-submit');
      if (btn && !state.busy) btn.textContent = state.cooldown + ' 秒后可重发';
      setTimeout(cooldownTick, 1000);
    } else if (gateEl()) {
      render();
    }
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (state.busy) return;
    const page = currentPage();
    const form = event.target;
    const data = new FormData(form);
    const get = (k) => String(data.get(k) || '').trim();
    state.error = '';
    state.notice = '';
    state.busy = true;

    try {
      if (page === 'login') {
        await api.login({ username: get('username'), password: String(data.get('password') || ''),
                          remember: !!data.get('remember') });
        state.busy = false;
        location.hash = '#/';
        location.reload();
        return;
      }
      if (page === 'register') {
        if (get('password') !== get('password2')) throw new Error('两次输入的密码不一样');
        await api.register({ username: get('username'), email: get('email'),
                             password: get('password') });
        state.busy = false;
        location.hash = '#/';
        location.reload();
        return;
      }
      if (page === 'forgot') {
        const res = await api.forgotPassword({ email: get('email') });
        state.busy = false;
        state.email = get('email');
        if (res && res.dev_code) {
          state.notice = '测试模式：验证码 ' + res.dev_code + '（10 分钟内有效）';
        } else if (res && res.mail_sent) {
          state.notice = '验证码已发到 ' + get('email') + '，10 分钟内有效。';
        } else {
          state.notice = '如果这个邮箱注册过，验证码会发到邮箱里。';
        }
        state.cooldown = 60;
        render();
        cooldownTick();
        return;
      }
      // reset
      if (get('password') !== get('password2')) throw new Error('两次输入的密码不一样');
      await api.resetPassword({ email: get('email'), code: get('code'),
                                new_password: get('password') });
      state.busy = false;
      if (window.UI && UI.toast) UI.toast('密码已重置，请用新密码登录', 'ok');
      state.notice = '密码已重置，正在跳转登录…';
      render();
      setTimeout(() => go('login'), 900);
      return;
    } catch (err) {
      state.busy = false;
      state.error = err.message || '请求失败';
      render();
    }
  }

  async function boot() {
    // 纯前端模式没有账号系统：数据就在这台设备的浏览器里
    if (window.PACE_STATIC) return true;
    try {
      state.status = await api.authStatus();
    } catch (err) {
      return true;
    }
    if (state.status && state.status.user) {
      window.PaceUser = state.status.user;
      lock(false);
      return true;
    }
    lock(true);
    if (!PAGES[currentPage()]) location.hash = '#/login';
    render();
    window.addEventListener('hashchange', () => {
      if (!document.body.classList.contains('auth-locked')) return;
      render();
    });
    return false;
  }

  async function logout() {
    try { await api.logout(); } catch (err) { /* ignore */ }
    location.hash = '#/login';
    location.reload();
  }

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
        <div class="auth-account">
          <div class="auth-avatar">${esc((user.username || '?').slice(0, 1).toUpperCase())}</div>
          <div>
            <b>${esc(user.username)}</b>
            <div class="tiny dim">${user.role === 'owner' ? '站长' : '成员'} · ${esc(user.email || '没填邮箱')}</div>
          </div>
        </div>
        <form id="pwdForm" style="display:flex;flex-direction:column;gap:8px;margin-top:14px">
          <label class="auth-field"><span>原密码</span>
            <input class="control" name="old_password" type="password" required autocomplete="current-password"></label>
          <label class="auth-field"><span>新密码</span>
            <input class="control" name="new_password" type="password" required autocomplete="new-password"
                   placeholder="至少 8 位，字母+数字"></label>
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
        await api.changePassword({ old_password: fd.get('old_password'), new_password: fd.get('new_password') });
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
    btn.className = 'auth-chip';
    btn.id = 'btnUser';
    btn.title = '账号设置 / 退出';
    btn.innerHTML = `<span class="auth-chip-avatar">${esc((user.username || '?').slice(0, 1).toUpperCase())}</span><span class="auth-chip-name">${esc(user.username)}</span>`;
    btn.addEventListener('click', openAccount);
    right.appendChild(btn);
  }

  window.addEventListener('pace:unauth', () => { location.hash = '#/login'; location.reload(); });

  window.Auth = { boot, logout, mountChip, openAccount, esc, user: () => window.PaceUser || null };
})();
