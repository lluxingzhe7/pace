/* 设置：心率参数、地图瓦片、主题、重算、数据导出、关于。 */
(function () {
  const state = { settings: null, health: null };
  let host = null;

  function render() {
    const s = state.settings || {};
    const h = state.health || {};
    host.innerHTML = `
      <div class="section-head" style="margin-top:0">
        <h2>设置</h2>
        <span class="dim">影响所有活动的指标计算</span>
      </div>

      <div class="grid g-2">
        <div class="card">
          <div class="card-head"><h3>心率</h3><span class="dim">用于心率区间与 TRIMP 训练负荷</span></div>
          <div class="card-body">
            <div class="grid g-2">
              <label class="tiny dim">最大心率
                <input id="setHrMax" type="number" min="120" max="230" value="${s.hr_max || 190}" class="control" style="width:100%;margin-top:6px">
              </label>
              <label class="tiny dim">静息心率
                <input id="setHrRest" type="number" min="30" max="100" value="${s.hr_rest || 55}" class="control" style="width:100%;margin-top:6px">
              </label>
            </div>
            <label class="tiny dim" style="display:block;margin-top:12px">阈值心率（可选）
              <input id="setHrThr" type="number" min="100" max="220" value="${s.hr_threshold || 168}" class="control" style="width:100%;margin-top:6px">
            </label>
            <div class="row" style="margin-top:16px">
              <button class="btn btn-primary" id="btnSaveHr">${Icon.get('check')}保存并重算</button>
              <span class="tiny dim">重算会用新参数刷新所有活动的区间与负荷</span>
            </div>
          </div>
        </div>

        <div class="card" style="grid-column:1/-1">
          <div class="card-head"><h3>AI 教练（DeepSeek / OpenAI 兼容）</h3>
            <span class="dim">${s.llm_api_key_set ? "已配置" : "未配置，教练走本地规则引擎"}</span></div>
          <div class="card-body">
            <div class="grid g-4">
              <label class="tiny dim">API Key
                <input id="setLlmKey" type="password" autocomplete="off"
                  placeholder="${s.llm_api_key_set ? U.esc(s.llm_api_key_mask || "已设置，留空则不修改") : "sk-…"}"
                  class="control mono" style="width:100%;margin-top:6px">
              </label>
              <label class="tiny dim">模型
                <input id="setLlmModel" list="llmModels" value="${U.esc(s.llm_model || "deepseek-chat")}"
                  class="control mono" style="width:100%;margin-top:6px">
                <datalist id="llmModels">
                  <option value="deepseek-chat"></option>
                  <option value="deepseek-reasoner"></option>
                  <option value="deepseek-v4.1-flash"></option>
                </datalist>
              </label>
              <label class="tiny dim">接口地址（base_url）
                <input id="setLlmBase" value="${U.esc(s.llm_base_url || "https://api.deepseek.com/v1")}"
                  class="control mono" style="width:100%;margin-top:6px">
              </label>
              <label class="tiny dim">超时（秒）
                <input id="setLlmTimeout" type="number" min="5" max="180" value="${s.llm_timeout_s || 45}"
                  class="control" style="width:100%;margin-top:6px">
              </label>
            </div>
            <p class="tiny dim" style="margin-top:10px;line-height:1.6">
              Key 只存在本机数据库（<span class="mono">data/pace.db</span>），不会上传到别处；
              教练问答时会把「负荷、最近 8 次活动、本周课表」发给你填的接口。
              留空或接口失败时自动回退本地规则引擎，功能不丢。
            </p>
            <div class="row" style="margin-top:14px;gap:8px">
              <button class="btn btn-primary" id="btnSaveLlm">${Icon.get('check')}保存</button>
              <button class="btn" id="btnTestLlm">${Icon.get('bolt')}测试连接</button>
              <span class="tiny dim" id="llmStatus"></span>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h3>训练阈值</h3><span class="dim">专项指标的基准值</span></div>
          <div class="card-body">
            <div class="grid g-3">
              <label class="tiny dim">骑行 FTP（瓦）
                <input id="setFtp" type="number" min="60" max="700" value="${s.ftp || 200}" class="control" style="width:100%;margin-top:6px">
              </label>
              <label class="tiny dim">跑步阈值配速（秒/公里）
                <input id="setThresholdPace" type="number" min="150" max="600" value="${s.threshold_pace_s || 300}" class="control" style="width:100%;margin-top:6px">
              </label>
              <label class="tiny dim">游泳阈值配速（秒/100 米）
                <input id="setSwimPace" type="number" min="70" max="400" value="${s.swim_pace_s || 150}" class="control" style="width:100%;margin-top:6px">
              </label>
            </div>
            <p class="tiny dim" style="margin-top:10px;line-height:1.6">
              FTP 用来算 IF / TSS 与功率区间；阈值配速用来判断强度，游泳也影响课程距离（150 秒 = 2:30/100 米）。
              填自己的实测值，200 W / 5:00 / 2:30 只是占位默认。
            </p>
            <div class="row" style="margin-top:14px">
              <button class="btn btn-primary" id="btnSaveThreshold">${Icon.get('check')}保存阈值</button>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h3>显示</h3></div>
          <div class="card-body">
            <div class="row between" style="padding:6px 0">
              <div><b style="font-size:13px">主题</b><p class="tiny dim">深蓝（默认）/ 石墨 / 浅色</p></div>
              <div class="seg" id="setTheme">
                <button data-v="navy" class="${(s.theme || 'navy') === 'navy' ? 'active' : ''}">深蓝</button>
                <button data-v="slate" class="${s.theme === 'slate' ? 'active' : ''}">石墨</button>
                <button data-v="light" class="${s.theme === 'light' ? 'active' : ''}">浅色</button>
              </div>
            </div>
            <div style="padding:10px 0 4px">
              <b style="font-size:13px">地图底图</b>
              <p class="tiny dim" style="margin:3px 0 8px">
                高德在国内最快，会自动做 GCJ-02 坐标校正（否则轨迹会偏离道路）；OSM 无需校正但国内常被墙。
              </p>
              <div class="seg" id="setMapProvider" style="margin-bottom:10px">
                <button data-v="amap" class="${(s.map_provider || 'amap') === 'amap' ? 'active' : ''}">高德</button>
                <button data-v="osm" class="${s.map_provider === 'osm' ? 'active' : ''}">OSM</button>
                <button data-v="custom" class="${s.map_provider === 'custom' ? 'active' : ''}">自定义</button>
                <button data-v="offline" class="${s.map_provider === 'offline' ? 'active' : ''}">离线轨迹图</button>
              </div>
              <input id="setTile" class="control" style="width:100%;height:34px;font-family:var(--mono);font-size:11.5px"
                value="${U.esc(s.map_provider === 'custom' ? (s.tile_url || '') : '')}"
                placeholder="自定义瓦片模板（选「自定义」才生效，如 https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png）">
              <div class="row" style="margin-top:10px">
                <button class="btn btn-sm" id="btnSaveTile">保存自定义地址</button>
                <button class="btn btn-sm btn-ghost" id="btnClearTile">清空自定义</button>
              </div>
            </div>
            <div style="padding:14px 0 4px;border-top:1px solid var(--line-soft);margin-top:6px">
              <b style="font-size:13px">路网规划服务</b>
              <p class="tiny dim" style="margin:3px 0 8px">
                制作路书时用来「沿道路吸附」，和行者里的规划方式是一个意思。
                默认用 OSRM 公共 demo；慢或不通可以换成自己的 OSRM 地址，
                连不上时路书会自动退回直线连接，不会卡住。
              </p>
              <input id="setRouting" class="control" style="width:100%;height:34px;font-family:var(--mono);font-size:11.5px"
                value="${U.esc(window.Routing ? window.Routing.osrmUrl() : '')}"
                placeholder="https://router.project-osrm.org/route/v1/driving/">
              <div class="row" style="margin-top:10px">
                <button class="btn btn-sm" id="btnSaveRouting">保存地址</button>
                <button class="btn btn-sm btn-ghost" id="btnResetRouting">恢复默认</button>
              </div>
              <div style="padding:14px 0 4px;border-top:1px solid var(--line-soft);margin-top:12px">
                <b style="font-size:13px">高德 Web 服务 Key（可选）</b>
                <p class="tiny dim" style="margin:3px 0 8px">
                  填了之后：路书里搜地点用高德（国内最准），地图上点途经点会自动取地名（和行者一样）。
                  不填就用免 Key 的 Photon 搜索，中文地名不全。
                  <b>Key 必须是「Web服务」类型</b>——「Web端(JS API)」「Android/iOS」的 Key
                  调搜索接口会报 <code>USERKEY_PLAT_NOMATCH(10009)</code>。
                  申请：<a href="https://console.amap.com/dev/key/app" target="_blank" rel="noopener">高德控制台</a>
                  → 创建新应用 → 添加 Key → 服务平台选「Web服务」。
                </p>
                <input id="setAmapKey" class="control" style="width:100%;height:34px;font-family:var(--mono);font-size:11.5px"
                  value="${U.esc(window.Routing ? window.Routing.amapKey() : '')}"
                  placeholder="32 位 Web 服务 Key，留空则不用高德">
                <div class="row" style="margin-top:10px">
                  <button class="btn btn-sm" id="btnSaveAmap">保存 Key</button>
                  <button class="btn btn-sm btn-ghost" id="btnClearAmap">清空</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="grid g-2 section">
        <div class="card">
          <div class="card-head"><h3>数据</h3><span class="dim">${h.activities || 0} 条活动</span></div>
          <div class="card-body">
            <div class="row wrap" style="gap:8px">
              <a class="btn" href="/api/activities?limit=500" download="pace-activities.json">${Icon.get('download')}导出活动 JSON</a>
              <button class="btn" id="btnRecompute">${Icon.get('spark')}重算全部指标</button>
              <button class="btn" onclick="UI.Importer.open()">${Icon.get('upload')}导入更多</button>
            </div>
            <p class="tiny dim" style="margin-top:12px;line-height:1.7">
              数据库位置：<code>data/pace.db</code>（SQLite）。直接备份这个文件即可。
              所有分析都在本机完成，不会上传任何数据。
            </p>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h3>关于</h3></div>
          <div class="card-body">
            <p class="tiny dim" style="line-height:1.8">
              <b style="color:var(--text)">PACE</b> 是一个自托管的运动数据分析工具，
              参考了 Strava 的活动模型与训练负荷口径（TRIMP、CTL/ATL/TSB、标准距离最佳成绩），
              但界面、代码与存储都是独立实现。
            </p>
            <p class="tiny dim" style="margin-top:10px;line-height:1.8">
              版本 ${h.version || '—'} · 后端纯 Python 标准库，无第三方依赖。
            </p>
            <p class="tiny dim" style="margin-top:10px;line-height:1.8">
              <b style="color:var(--text)">指标口径</b>：距离用 haversine 逐点累加；移动时间排除停顿与长间隔；
              爬升先平滑再用滞回阈值；跑步用秒/公里，游泳用秒/100 米，骑行用 km/h。
            </p>
          </div>
        </div>
      </div>

      <div class="card section">
        <div class="card-head"><h3>支持的导入格式</h3></div>
        <div style="overflow-x:auto">
          <table class="data-table">
            <thead><tr><th>格式</th><th>来源</th><th>含轨迹</th><th>说明</th></tr></thead>
            <tbody>
              <tr><td class="mono">GPX</td><td>Strava / Garmin / 手表导出</td><td>是</td><td>解析 ele / hr / cad / power 扩展</td></tr>
              <tr><td class="mono">FIT</td><td>码表 / 手表原生</td><td>是</td><td>二进制协议，含气压高度、功率、踏频</td></tr>
              <tr><td class="mono">TCX</td><td>Garmin Training Center</td><td>是</td><td>XML，含心率、踏频、功率</td></tr>
              <tr><td class="mono">CSV</td><td>Strava 批量导出 / 手工表</td><td>否</td><td>至少需要日期列，自动识别常见表头</td></tr>
            </tbody>
          </table>
        </div>
      </div>`;

    Icon.hydrate(host);

    document.getElementById('btnSaveHr').addEventListener('click', async () => {
      const payload = {
        hr_max: Number(document.getElementById('setHrMax').value),
        hr_rest: Number(document.getElementById('setHrRest').value),
        hr_threshold: Number(document.getElementById('setHrThr').value),
      };
      if (!(payload.hr_max > payload.hr_rest)) {
        UI.toast('最大心率必须大于静息心率', 'err');
        return;
      }
      try {
        await api.saveSettings(payload);
        UI.toast('已保存，正在重算…', 'ok');
        const r = await api.recompute();
        UI.toast(`已重算 ${r.recomputed} 条活动的指标`, 'ok');
        await load(host);
      } catch (err) {
        UI.toast('失败：' + U.esc(err.message), 'err');
      }
    });

    document.getElementById('btnSaveLlm').addEventListener('click', async () => {
      const key = document.getElementById('setLlmKey').value.trim();
      const payload = {
        llm_model: document.getElementById('setLlmModel').value.trim() || 'deepseek-chat',
        llm_base_url: document.getElementById('setLlmBase').value.trim() || 'https://api.deepseek.com/v1',
        llm_timeout_s: Number(document.getElementById('setLlmTimeout').value) || 45,
        llm_enabled: true,
      };
      if (key) payload.llm_api_key = key;   // 留空表示沿用已存的 Key
      try {
        const res = await api.saveSettings(payload);
        Object.assign(state.settings, res.settings || payload);
        if (window.PaceApp && res.settings) Object.assign(window.PaceApp.settings, res.settings);
        document.getElementById('setLlmKey').value = '';
        const saved = res.settings && res.settings.llm_api_key_set;
        document.getElementById('llmStatus').textContent = saved ? '已保存，教练将使用大模型' : '已保存（还没填 Key，仍用规则引擎）';
        UI.toast('AI 教练设置已保存', 'ok');
      } catch (err) {
        UI.toast('保存失败：' + U.esc(err.message), 'err');
      }
    });

    document.getElementById('btnTestLlm').addEventListener('click', async () => {
      const btn = document.getElementById('btnTestLlm');
      const status = document.getElementById('llmStatus');
      const key = document.getElementById('setLlmKey').value.trim();
      btn.disabled = true;
      status.textContent = '测试中…';
      try {
        const res = await api.testLlm({
          llm_api_key: key || undefined,
          llm_base_url: document.getElementById('setLlmBase').value.trim(),
          llm_model: document.getElementById('setLlmModel').value.trim(),
        });
        status.textContent = res.ok
          ? `连接正常：${res.model} · ${res.elapsed_ms}ms · 回复「${res.reply}」`
          : `失败：${res.error}`;
        status.style.color = res.ok ? 'var(--ok)' : 'var(--warn)';
      } catch (err) {
        status.textContent = '失败：' + err.message;
        status.style.color = 'var(--warn)';
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById('btnSaveThreshold').addEventListener('click', async () => {
      const payload = {
        ftp: Number(document.getElementById('setFtp').value),
        threshold_pace_s: Number(document.getElementById('setThresholdPace').value),
        swim_pace_s: Number(document.getElementById('setSwimPace').value),
      };
      if (!(payload.ftp > 0) || !(payload.threshold_pace_s > 0) || !(payload.swim_pace_s > 0)) {
        UI.toast('阈值必须是正数', 'err');
        return;
      }
      try {
        await api.saveSettings(payload);
        state.settings.ftp = payload.ftp;
        state.settings.threshold_pace_s = payload.threshold_pace_s;
        state.settings.swim_pace_s = payload.swim_pace_s;
        if (window.PaceApp) Object.assign(window.PaceApp.settings, payload);
        UI.toast('阈值已保存，专项指标会按新值计算', 'ok');
      } catch (err) {
        UI.toast('保存失败：' + U.esc(err.message), 'err');
      }
    });

    document.querySelectorAll('#setTheme button').forEach(btn => {
      btn.addEventListener('click', async () => {
        const theme = btn.dataset.v;
        document.documentElement.dataset.theme = theme;
        try {
          await api.saveSettings({ theme });
          state.settings.theme = theme;
          UI.toast('主题已切换', 'ok');
          if (window.Router) window.Router.rerender();
        } catch (err) {
          UI.toast('保存失败：' + U.esc(err.message), 'err');
        }
      });
    });

    document.querySelectorAll('#setMapProvider button').forEach(btn => {
      btn.addEventListener('click', async () => {
        const provider = btn.dataset.v;
        try {
          await api.saveSettings({ map_provider: provider });
          state.settings.map_provider = provider;
          document.querySelectorAll('#setMapProvider button').forEach(b => b.classList.toggle('active', b === btn));
          if (window.PaceApp) {
            window.PaceApp.settings.map_provider = provider;
            window.PaceApp.mapConfig = window.PaceApp.resolveMapConfig(window.PaceApp.settings);
          }
          const names = {'amap': '高德', 'osm': 'OSM', 'custom': '自定义', 'offline': '离线轨迹图'};
          UI.toast(`底图已切换为${names[provider] || provider}`, 'ok');
        } catch (err) {
          UI.toast('保存失败：' + U.esc(err.message), 'err');
        }
      });
    });

    document.getElementById('btnSaveTile').addEventListener('click', async () => {
      const url = document.getElementById('setTile').value.trim();
      try {
        await api.saveSettings({ tile_url: url, map_provider: url ? 'custom' : 'amap' });
        if (window.PaceApp) {
          window.PaceApp.settings.tile_url = url;
          window.PaceApp.settings.map_provider = url ? 'custom' : 'amap';
          window.PaceApp.mapConfig = window.PaceApp.resolveMapConfig(window.PaceApp.settings);
        }
        UI.toast(url ? '已启用自定义底图' : '已清空自定义地址', 'ok');
      } catch (err) {
        UI.toast('保存失败：' + U.esc(err.message), 'err');
      }
    });
    document.getElementById('btnClearTile').addEventListener('click', async () => {
      document.getElementById('setTile').value = '';
      try {
        await api.saveSettings({ tile_url: '', map_provider: 'amap' });
        if (window.PaceApp) {
          window.PaceApp.settings.tile_url = '';
          window.PaceApp.settings.map_provider = 'amap';
          window.PaceApp.mapConfig = window.PaceApp.resolveMapConfig(window.PaceApp.settings);
        }
        UI.toast('已清空自定义地址', 'ok');
      } catch (err) {
        UI.toast('保存失败：' + U.esc(err.message), 'err');
      }
    });

    const routingInput = document.getElementById('setRouting');
    const btnSaveRouting = document.getElementById('btnSaveRouting');
    if (routingInput && btnSaveRouting && window.Routing) {
      btnSaveRouting.addEventListener('click', () => {
        window.Routing.setOsrmUrl(routingInput.value.trim());
        UI.toast('路网服务地址已保存', 'ok');
      });
      document.getElementById('btnResetRouting').addEventListener('click', () => {
        window.Routing.setOsrmUrl('');
        routingInput.value = window.Routing.DEFAULT_OSRM;
        UI.toast('已恢复默认 OSRM 地址', 'ok');
      });
      const amapInput = document.getElementById('setAmapKey');
      document.getElementById('btnSaveAmap').addEventListener('click', async () => {
        const key = amapInput.value.trim();
        await window.Routing.setAmapKey(key);
        if (!key) { UI.toast('已清空高德 Key', 'ok'); return; }
        UI.toast('Key 已保存，正在校验…', 'ok');
        const r = await testAmapKey(key);
        UI.toast(r.ok ? `高德 Key 可用（${r.detail}）` : `高德 Key 不可用：${r.detail}`,
          r.ok ? 'ok' : 'err');
      });
      document.getElementById('btnClearAmap').addEventListener('click', async () => {
        amapInput.value = '';
        await window.Routing.setAmapKey('');
        UI.toast('已清空高德 Key', 'ok');
      });
    }

    async function testAmapKey(key) {
      const t0 = performance.now();
      try {
        const url = 'https://restapi.amap.com/v3/place/text'
          + `?keywords=${encodeURIComponent('地铁站')}&city=${encodeURIComponent('广州')}`
          + `&offset=1&page=1&key=${encodeURIComponent(key)}`;
        const res = await fetch(url);
        const json = await res.json();
        if (json.status === '1') {
          return { ok: true, detail: `${Math.round(performance.now() - t0)}ms，命中 ${json.count} 条` };
        }
        return { ok: false, detail: `${json.info || '未知错误'}（${json.infocode || ''}）` };
      } catch (err) {
        return { ok: false, detail: String((err && err.message) || err) };
      }
    }

    document.getElementById('btnRecompute').addEventListener('click', async (e) => {      e.target.disabled = true;
      try {
        const r = await api.recompute();
        UI.toast(`已重算 ${r.recomputed} 条活动`, 'ok');
      } catch (err) {
        UI.toast('失败：' + U.esc(err.message), 'err');
      } finally {
        e.target.disabled = false;
      }
    });
  }

  async function load(root) {
    host = root;
    root.innerHTML = UI.spinner('读取设置…');
    try {
      const [settings, health] = await Promise.all([api.settings(), api.health()]);
      state.settings = settings.settings;
      state.health = health;
      if (state.settings.theme) document.documentElement.dataset.theme = state.settings.theme;
      render();
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty('info', '加载失败', U.esc(err.message))}</div>`;
    }
  }

  window.PageSettings = { load };
})();
