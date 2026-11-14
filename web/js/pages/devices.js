/* 数据源页：华为 / iGPSPORT / Intervals.icu 的接入设置与同步。 */
(function () {
  const state = { status: null, sources: [], pending: {}, showHidden: false };
  let host = null;

  const TYPE_META = {
    folder: {
      icon: 'cloud',
      intro: '监视一个文件夹，出现新的活动文件就自动导入。适合华为运动健康导出、码表 App 下载目录。',
    },
    usb: {
      icon: 'link',
      intro: '码表插上电脑（选「文件传输」模式）后被识别为 U 盘，自动读取其中的 FIT 文件。只读，不改设备。',
    },
    intervals: {
      icon: 'trend',
      intro: '用个人 API Key 拉取原始活动文件。Strava 同步到 Intervals.icu 的活动也在这里。',
    },
    strava: {
      icon: 'link',
      intro: '直接走 Strava 官方 API，拿你自己活动的完整逐点数据（心率/功率/GPS）。本机运行，不上传、不分发。',
    },
  };

  const PLACEHOLDERS = {
    huawei_export: 'C:\\Users\\你\\Downloads\\huawei-export',
    igpsport_folder: 'C:\\Users\\你\\Downloads\\igpsport',
    phone_export: 'C:\\Users\\你\\OneDrive\\bike-exports',
    outbase_export: 'C:\\Users\\你\\Downloads\\outbase-export',
  };

  const SOURCE_NOTES = {
    outbase_export: 'Outbase 没有自助导出；可发邮件到 outbase_feedback@hellogroup.com 要数据副本，或看活动详情里是否有「导出 GPX」。',
  };

  function sourceCard(s) {
    const meta = TYPE_META[s.type] || TYPE_META.folder;
    const cfg = s.config || {};
    const ok = s.available;
    const dot = ok ? 'var(--good)' : 'var(--text-3)';
    const placeholder = PLACEHOLDERS[s.id] || 'C:\\Users\\你\\Downloads\\exports';
    return `
      <div class="card card-pad" data-source="${s.id}">
        <div class="row between" style="align-items:flex-start;gap:10px">
          <div class="row" style="gap:10px;min-width:0">
            <div class="act-icon" style="width:34px;height:34px;color:${dot}">${Icon.get(meta.icon)}</div>
            <div style="min-width:0">
              <div class="row" style="gap:6px">
                <b style="font-size:13.5px">${U.esc(s.label)}</b>
                <span class="chip" style="color:${dot}">${ok ? '可用' : '未就绪'}</span>
              </div>
              <p class="tiny dim" style="margin-top:3px">${U.esc(s.hint || '')}</p>
              ${SOURCE_NOTES[s.id] ? `<p class="tiny" style="margin-top:4px;color:var(--warn)">${U.esc(SOURCE_NOTES[s.id])}</p>` : ''}
            </div>
          </div>
          <label class="row tiny dim" style="gap:5px;flex:none;cursor:pointer">
            <input type="checkbox" class="src-enable" ${s.enabled ? 'checked' : ''}
              style="accent-color:var(--accent)">启用
          </label>
        </div>

        ${s.type === 'folder' ? `
          <div class="row" style="gap:8px;margin-top:12px">
            <input class="control src-path" style="flex:1;min-width:0;height:34px;font-family:var(--mono);font-size:11.5px"
              placeholder="${U.esc(placeholder)}"
              value="${U.esc(cfg.path || '')}">
          </div>` : ''}

        ${s.type === 'usb' ? `
          <div style="margin-top:12px">
            ${(s.devices || []).length
              ? (s.devices || []).map(d => `<div class="tiny mono dim">${U.esc(d.path)} · ${U.esc(d.label)} · ${d.dirs.length} 个目录</div>`).join('')
              : `<p class="tiny dim">插上码表并选「文件传输」后，点上面的「立即同步」会自动识别。</p>`}
          </div>` : ''}

        ${s.type === 'strava' ? `
          <div class="grid g-2" style="margin-top:12px;gap:8px">
            <label class="tiny dim">Client ID
              <input class="control src-sid" style="width:100%;height:34px;margin-top:4px"
                value="${U.esc(cfg.client_id || '')}" placeholder="例：123456">
            </label>
            <label class="tiny dim">Client Secret
              <input class="control src-secret" type="password" style="width:100%;height:34px;margin-top:4px"
                value="" placeholder="${cfg.client_secret ? '已保存，留空则不变' : '填 Client Secret'}">
            </label>
          </div>
          <p class="tiny dim" style="margin-top:6px;line-height:1.7">
            在 <b>strava.com/settings/api</b> 创建应用，
            <b>Authorization Callback Domain</b> 填 <code>localhost</code>，再把 Client ID / Secret 填到这里。
          </p>
          <p class="tiny" style="margin-top:6px;line-height:1.7;color:var(--warn)">
            注意：Strava 从 2026-06 起要求开发者持有订阅才能用 API。取消订阅后应用会被停用，
            同步会失败，但<b>已导入 PACE 的数据不受影响</b>，iGPSPORT → Strava 的上传也不受影响。
            不想长期订阅的话，用下面「Strava 批量导出」把历史一次性拉进来即可。
          </p>
          <div class="row" style="gap:8px;margin-top:8px;flex-wrap:wrap">
            <button class="btn btn-sm btn-save">保存凭据</button>
            <button class="btn btn-sm btn-primary btn-connect"
              ${cfg.client_id && cfg.client_secret ? '' : 'disabled'}>${Icon.get('link')}连接 Strava</button>
            <button class="btn btn-sm btn-ghost btn-disconnect"
              ${(cfg.tokens && cfg.tokens.access_token) ? '' : 'disabled'}>断开</button>
          </div>
          ${(cfg.tokens && cfg.tokens.access_token) ? `
            <p class="tiny" style="margin-top:8px;color:var(--good)">
              已连接${cfg.tokens.athlete_name ? '：' + U.esc(cfg.tokens.athlete_name) : ''}
            </p>` : ''}` : ''}

        ${s.type === 'intervals' ? `
          <div class="grid g-2" style="margin-top:12px;gap:8px">
            <label class="tiny dim">Athlete ID
              <input class="control src-athlete" style="width:100%;height:34px;margin-top:4px"
                value="${U.esc(cfg.athlete_id || '')}" placeholder="0">
              <span class="tiny dim">填 0 表示「Key 所属运动员」，一般不用改</span>
            </label>
            <label class="tiny dim">API Key
              <input class="control src-key" type="password" style="width:100%;height:34px;margin-top:4px"
                value="${U.esc(cfg.api_key || '')}" placeholder="粘贴 Key 本身">
              <span class="tiny dim">在 intervals.icu/settings 页面最下方复制，不是登录密码</span>
            </label>
          </div>` : ''}

        <div class="row" style="gap:8px;margin-top:12px">
          <button class="btn btn-sm btn-test">${Icon.get('spark')}测试</button>
          ${s.type === 'strava' ? '' : `<button class="btn btn-sm btn-save">保存设置</button>`}
          <span class="tiny dim test-result"></span>
        </div>
      </div>`;
  }

  function guideBlock() {
    return `
      <div class="card">
        <div class="card-head"><h3>iGPSPORT 数据怎么进来</h3>
          <span class="dim">两条通道都实测可用</span></div>
        <div class="card-body">
          <p class="tiny muted" style="line-height:1.9">
            <b style="color:var(--text)">① 数据线（最快，只读）</b><br>
            码表用数据线连电脑 → 在码表上选「文件传输 / USB 存储」→
            本页点「立即同步」。会自动读取 <code>Activities</code> 等目录里的 FIT，
            不改动、不删除设备上的任何文件。
          </p>
          <p class="tiny muted" style="margin-top:12px;line-height:1.9">
            <b style="color:var(--text)">② 数据文件（无线，适合批量）</b><br>
            把从 App 导出、或从别处下载的 <code>.fit</code> / <code>.gpx</code> /
            <code>.tcx</code> / <code>.fit.gz</code> 放进一个固定目录，
            把该目录填到上面「iGPSPORT 数据文件」的路径。
            之后每次往里丢文件，点同步或等自动同步即可。
          </p>
          <p class="tiny dim" style="margin-top:12px;line-height:1.7">
            两种方式支持同一批格式，重复导入会按内容哈希与活动 ID 自动去重。
            也可以直接把文件拖到页面任意位置手动上传。
          </p>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>已关闭的通道</h3>
          <span class="dim">实测走不通，先收起来</span></div>
        <div class="card-body">
          <p class="tiny muted" style="line-height:1.9">
            华为运动健康导出、Intervals.icu、Strava API、Outbase、蓝牙直读——
            这几条要么依赖厂商开放接口（没有），要么被平台条款限制。
            代码都还在，需要时可以点页面底部「显示其他通道」重新打开。
          </p>
          <p class="tiny dim" style="margin-top:8px;line-height:1.7">
            已导入 PACE 的活动不受影响，关通道只是不再扫描它们。
          </p>
        </div>
      </div>`;
  }

  function render() {
    const st = state.status || {};
    const all = st.sources || [];
    const sources = all.filter(s => state.showHidden || s.visible !== false);
    const hiddenCount = all.filter(s => s.visible === false).length;
    host.innerHTML = `
      <div class="toolbar" style="margin-bottom:14px">
        <h2 style="font-size:17px;margin-right:6px">数据源</h2>
        <span class="dim tiny">${st.tracked_files || 0} 个文件已记录 · ${
          st.last_sync ? '上次同步 ' + U.dateLabel(st.last_sync) : '从未同步'}</span>
        <div class="spacer"></div>
        <div class="seg" id="autoSync">
          <button data-sec="0" class="active">手动</button>
          <button data-sec="300">每 5 分钟</button>
          <button data-sec="900">每 15 分钟</button>
        </div>
        <button class="btn btn-primary btn-sm" id="btnSyncAll">${Icon.get('spark')}立即同步</button>
      </div>

      <div class="grid g-2">
        ${sources.map(sourceCard).join('')}
      </div>

      <div class="grid g-2 section">
        ${guideBlock()}
      </div>

      ${state.showHidden ? `
        <div class="card section">
          <div class="card-head"><h3>蓝牙直连（实验性）</h3>
            <span class="dim">浏览器主动连设备，不是模拟设备</span></div>
          <div class="card-body">
            <p class="tiny muted" style="line-height:1.8">
              如果设备通过标准蓝牙服务（心率 0x180D / 踏频 0x1816 / 功率 0x1818 / FTMS 0x1826）
              暴露数据，浏览器可以直接读。iGPSPORT 与华为用的是私有服务，
              需要先在「蓝牙采集」页抓一次包，才能补上解析规则。
            </p>
            <a class="btn btn-sm" href="#/ble" style="margin-top:10px">${Icon.get('link')}打开蓝牙采集</a>
          </div>
        </div>` : ''}

      <div class="card section" id="syncResultCard" hidden>
        <div class="card-head"><h3>同步结果</h3></div>
        <div class="card-body" id="syncResult"></div>
      </div>

      <p class="tiny dim" style="margin-top:16px;text-align:center">
        所有读取都在本机完成；设备上的文件不会被修改或删除。
      </p>
      ${hiddenCount ? `
        <p class="tiny dim" style="margin-top:6px;text-align:center">
          <button class="btn btn-sm btn-ghost" id="btnToggleHidden">
            ${state.showHidden ? '收起其他通道' : `显示其他通道（${hiddenCount}）`}
          </button>
        </p>` : ''}`;

    Icon.hydrate(host);
    wire();
  }

  function wire() {
    U.$$('.card[data-source]', host).forEach(card => {
      const id = card.dataset.source;
      const meta = state.sources.find(s => s.id === id) || {};

      // Strava 走 OAuth，单独处理（避免把 token 字段一起覆盖）
      if (meta.type === 'strava') {
        const sid = card.querySelector('.src-sid');
        const sec = card.querySelector('.src-secret');
        const saveBtn = card.querySelector('.btn-save');
        const connectBtn = card.querySelector('.btn-connect');
        const disconnectBtn = card.querySelector('.btn-disconnect');
        const result = card.querySelector('.test-result');

        if (saveBtn) saveBtn.addEventListener('click', async () => {
          const body = { client_id: sid ? sid.value.trim() : '' };
          if (sec && sec.value.trim()) body.client_secret = sec.value.trim();
          try {
            await api.stravaSave(body);
            UI.toast('凭据已保存', 'ok');
            await load(host);
          } catch (err) {
            UI.toast('保存失败：' + U.esc(err.message), 'err');
          }
        });
        if (connectBtn) connectBtn.addEventListener('click', () => {
          location.href = '/api/strava/auth';
        });
        if (disconnectBtn) disconnectBtn.addEventListener('click', async () => {
          if (!confirm('断开后需要重新授权才能同步。确定断开？')) return;
          try {
            await api.stravaDisconnect();
            UI.toast('已断开', 'ok');
            await load(host);
          } catch (err) {
            UI.toast('断开失败：' + U.esc(err.message), 'err');
          }
        });
        if (result) result.textContent = meta.hint || '';
        return;
      }

      const saveBtn = card.querySelector('.btn-save');
      const testBtn = card.querySelector('.btn-test');
      const enable = card.querySelector('.src-enable');
      const pathInput = card.querySelector('.src-path');
      const athlete = card.querySelector('.src-athlete');
      const key = card.querySelector('.src-key');
      const result = card.querySelector('.test-result');

      const collect = () => {
        const base = state.sources.find(s => s.id === id);
        const cfg = Object.assign({}, (base && base.config) || {});
        if (pathInput) cfg.path = pathInput.value.trim();
        if (athlete) cfg.athlete_id = athlete.value.trim();
        if (key) cfg.api_key = key.value.trim();
        return { id, type: base.type, label: base.label,
                 enabled: enable ? enable.checked : true, config: cfg };
      };

      saveBtn.addEventListener('click', async () => {
        const updated = state.sources.map(s => (s.id === id ? collect() : s));
        try {
          const res = await api.saveSources(updated);
          state.status = res.status;
          state.sources = res.status.sources;
          const saved = state.sources.find(s => s.id === id);
          if (saved && saved.configured && !saved.enabled) {
            UI.toast('已保存，但还没勾选「启用」，同步时不会扫描它', 'warn', 4500);
          } else {
            UI.toast('已保存', 'ok');
          }
          render();
        } catch (err) {
          UI.toast('保存失败：' + U.esc(err.message), 'err');
        }
      });

      // 勾选框立即生效，避免"改了没保存"导致同步扫不到
      if (enable) {
        enable.addEventListener('change', async () => {
          const updated = state.sources.map(s => (s.id === id ? collect() : s));
          try {
            const res = await api.saveSources(updated);
            state.status = res.status;
            state.sources = res.status.sources;
            UI.toast(enable.checked ? '已启用' : '已停用', 'ok');
            render();
          } catch (err) {
            UI.toast('切换失败：' + U.esc(err.message), 'err');
          }
        });
      }

      testBtn.addEventListener('click', async () => {
        result.textContent = '测试中…';
        try {
          const r = await api.testSource(id);
          result.textContent = r.ok ? `连接正常${r.activities != null ? ` · ${r.activities} 条活动` : ''}` : (r.error || r.detail || '未通过');
          result.style.color = r.ok ? 'var(--good)' : 'var(--warn)';
        } catch (err) {
          result.textContent = '测试失败：' + err.message;
          result.style.color = 'var(--bad)';
        }
      });
    });

    const auto = document.getElementById('autoSync');
    if (auto) {
      auto.querySelectorAll('button').forEach(btn => {
        btn.addEventListener('click', async () => {
          auto.querySelectorAll('button').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          try {
            await api.saveSettings({ auto_sync_seconds: Number(btn.dataset.sec) });
            UI.toast(btn.dataset.sec === '0' ? '已切回手动同步' : `已开启自动同步（每 ${btn.dataset.sec / 60} 分钟）`, 'ok');
          } catch (err) {
            UI.toast('设置失败：' + U.esc(err.message), 'err');
          }
        });
      });
    }

    document.getElementById('btnSyncAll').addEventListener('click', runSync);
    const toggleHidden = document.getElementById('btnToggleHidden');
    if (toggleHidden) toggleHidden.addEventListener('click', () => {
      state.showHidden = !state.showHidden;
      render();
    });
  }

  async function runSync() {
    const btn = document.getElementById('btnSyncAll');
    btn.disabled = true;
    btn.innerHTML = `${Icon.get('spark')}同步中…`;
    try {
      const res = await api.syncSources({});
      const card = document.getElementById('syncResultCard');
      const box = document.getElementById('syncResult');
      card.hidden = false;
      const lines = (res.results || []).map(r => {
        const skipped = (r.skipped_activities || []).map(a => `
          <div class="result-row" style="margin-left:14px">
            <span style="color:var(--warn)">${Icon.get('info')}</span>
            <span class="mono">${U.esc(a.activity_id)}</span>
            <span class="dim">${U.esc(a.name || a.sport || '')}</span>
            <span style="color:var(--warn)">${U.esc(a.reason || '取不到原始文件')}</span>
          </div>`).join('');
        if (r.file) {
          const ok = r.status === 'ok';
          return `<div class="result-row">
            <span class="${ok ? 'ok' : 'err'}">${Icon.get(ok ? 'check' : 'x')}</span>
            <span class="mono">${U.esc(r.file)}</span>
            <span class="dim">${ok ? `导入 ${(r.imported || []).length} 条` : U.esc(r.reason || '失败')}</span>
          </div>`;
        }
        return `<div class="result-row">
          <span class="dim">${Icon.get('info')}</span>
          <span>${U.esc(labelOf(r.source))}</span>
          <span class="dim">${U.esc(r.detail || '')}</span>
        </div>` + skipped;
      }).join('');
      box.innerHTML = lines || '<p class="tiny dim">没有启用的数据源</p>';
      if (!(res.results || []).length) {
        UI.toast('没有启用的数据源：先勾选「启用」再同步', 'warn', 5000);
        return;
      }
      const skipped = (res.results || []).reduce((n, r) => n + (r.skipped_activities || []).length, 0);
      if (res.imported > 0) {
        UI.toast(`同步完成，导入 ${res.imported} 条活动`, 'ok');
        document.dispatchEvent(new CustomEvent('pace:imported'));
        await load(host);
      } else if (skipped > 0) {
        UI.toast(`${skipped} 条活动取不到原始文件，看下方原因`, 'warn', 6000);
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } else if (res.errors) {
        UI.toast(`同步完成，但 ${res.errors} 个文件失败`, 'warn');
      } else {
        UI.toast('没有发现新活动', 'ok');
      }
    } catch (err) {
      UI.toast('同步失败：' + U.esc(err.message), 'err');
    } finally {
      btn.disabled = false;
      btn.innerHTML = `${Icon.get('spark')}立即同步`;
    }
  }

  function labelOf(id) {
    const s = state.sources.find(x => x.id === id);
    return s ? s.label : id;
  }

  async function load(root) {
    host = root;
    root.innerHTML = UI.spinner('检测设备与数据源…');
    try {
      const st = await api.sources();
      state.status = st;
      state.sources = st.sources;
      render();
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty('info', '加载失败', U.esc(err.message))}</div>`;
    }
  }

  window.PageDevices = { load };
})();
