/* 路书：列表 → 只读详情 → 制作（按行者的流程：点途经点 → 路网吸附 → 存）。
   途经点是主角：名称、功能标签、备注、是否显示，和行者一致。 */
(function () {
  const TAGS = ['打卡点', '风景景点', '食物补给', '饮品补给', '洗手间', '紧急医疗',
    '摄影', '长期修整', '充电服务', '拉伸服务', '私人补给', '退赛收容'];

  const state = {
    books: [],
    book: null,
    mode: 'list',            // list | view | edit
    waypoints: [],           // [{lat, lon, name, tag, note, show}]
    track: [],               // 吸附后的轨迹 [[lat, lon], ...]
    plan: [],                // 按地点生成：[{kind, place, q}]
    name: '',
    routeMode: 'road',       // road（路网吸附） | straight（直线）
    kmMarks: false,
    satLayer: false,
    map: null,
    profileChart: null,
    elevChart: null,
    elevSeq: 0,
    elevSeries: [],      // 当前轨迹的海拔序列 [[距离, 海拔], ...]
    elevDebounce: 0,
    mapCfg: null,
    saving: false,
    routeInfo: null,
    undo: [],
    redo: [],
    snapSeq: 0,
    snapPromise: null,
    snapMsg: '',
    spanMeters: 1000,      // 地图显示范围（下拉：500 m / 1 km / 2 km …）
    spanManual: false,     // 用户自己选过范围后，选点就不再强行拉回 16 级
    mapBaseHeight: 560,
    generated: false,       // 点了「生成路书」后锁住地图，避免误加点
    pending: null,          // 正在地图上确认的点 {row, lat, lon, name}
    dirty: false,
  };
  let host = null;

  const DEFAULT_CENTER = [23.1291, 113.2644];
  const VERTEX_LIMIT = 150;
  const SAT_URL = 'https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}';
  const DRAFT_KEY = 'pace.rb.draft';
  const ARROW = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '
    + 'stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M6 11l6-6 6 6"/></svg>';

  function cleanup() {
    document.body.classList.remove('rb-locked');
    const card = document.getElementById('rbMapCard');
    if (card) card.classList.remove('is-fullscreen');
    clearTimeout(state.elevDebounce);
    destroyElev();
    if (state.profileChart) {
      try { state.profileChart.destroy(); } catch (e) { /* ignore */ }
      state.profileChart = null;
    }
    if (state.map) { try { state.map.destroy(); } catch (e) { /* ignore */ } state.map = null; }
  }

  function haversine(a, b) {
    const R = 6371008.8;
    const dLat = (b[0] - a[0]) * Math.PI / 180;
    const dLon = (b[1] - a[1]) * Math.PI / 180;
    const la1 = a[0] * Math.PI / 180;
    const la2 = b[0] * Math.PI / 180;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  function distanceOf(points) {
    let total = 0;
    for (let i = 1; i < points.length; i++) total += haversine(points[i - 1], points[i]);
    return total;
  }

  function setTitle(text) {
    const el = document.getElementById('pageTitle');
    if (el) el.textContent = text || '路书';
  }

  function sourceLabel(b) {
    if (!b) return '';
    if (b.source === 'activity') {
      return b.source_activity_id ? `来自活动 #${b.source_activity_id}` : '来自活动轨迹';
    }
    if (b.source === 'gpx') return 'GPX 导入';
    return '手绘路线';
  }

  /* ------------------------------------------------------------ 列表 */

  function cardHtml(b) {
    const pts = b.points || [];
    const gain = Number(b.climb_m || 0);
    const wps = Number(b.waypoint_count || 0);
    return `
      <div class="card rb-card" data-id="${b.id}">
        <a class="rb-thumb" href="#/routebook/${b.id}">
          <canvas class="rb-thumb-canvas" data-id="${b.id}"></canvas>
        </a>
        <div class="rb-body">
          <div class="row between">
            <a href="#/routebook/${b.id}" class="rb-name">${U.esc(b.name)}</a>
            <button class="icon-btn" data-del="${b.id}" title="删除路书">${Icon.get('trash')}</button>
          </div>
          <p class="tiny dim rb-note">${U.esc(b.note || (b.source === 'activity' ? '来自活动轨迹' : '手绘路书'))}</p>
          <div class="rb-stats">
            <span><b>${U.km(b.distance_m || 0, 1)}</b> km</span>
            ${gain > 0 ? `<span><b>+${U.num(gain)}</b> m</span>` : ''}
            ${wps > 0 ? `<span><b>${U.num(wps)}</b> 个途经点</span>` : ''}
            <span class="dim">${b.updated_at ? U.dateShort(b.updated_at) : ''}</span>
          </div>
          <div class="row" style="gap:6px;margin-top:10px">
            <a class="btn btn-sm" href="#/routebook/${b.id}">${Icon.get('chevron')}查看</a>
            <a class="btn btn-sm" href="/api/routebooks/${b.id}/gpx" download>${Icon.get('download')}导出 GPX</a>
          </div>
        </div>
      </div>`;
  }

  function renderList() {
    state.mode = 'list';
    const books = state.books || [];
    host.innerHTML = `
      <div class="section-head" style="margin-top:0">
        <h2>路书</h2>
        <span class="dim">点途经点，自动沿路生成；导出给码表照着骑</span>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="rbFromAct">${Icon.get('list')}从活动生成</button>
        <button class="btn btn-sm" id="rbImport">${Icon.get('upload')}导入 GPX</button>
        <button class="btn btn-primary btn-sm" id="rbNew">${Icon.get('route')}制作路书</button>
      </div>

      ${books.length ? `<div class="grid g-auto rb-grid">${books.map(cardHtml).join('')}</div>`
        : `<div class="card">${UI.empty('route', '还没有路书',
          '路书就是一份「照着骑」的路线：点几个途经点，系统沿道路生成轨迹；也能用骑过的活动或 GPX 直接生成。',
          `<button class="btn btn-primary" id="rbNewEmpty">做第一条路书</button>`)}</div>`}

      <div class="section">
        <div class="card card-pad">
          <b>制作路书的顺序</b>
          <p class="tiny dim" style="margin-top:4px;line-height:1.7">
            1. 地图上点起点、途经点、终点（点错了可以拖锚点，也可以撤销）；
            2. 「骑行（路网）」会沿着道路算出真实路线，「直线」就是两点一线；
            3. 双击/点锚点改名称、功能标签和备注；4. 保存，详情页能导出 GPX 给码表。
          </p>
        </div>
      </div>`;
    Icon.hydrate(host);

    document.getElementById('rbNew').addEventListener('click', () => openNew());
    const emptyNew = document.getElementById('rbNewEmpty');
    if (emptyNew) emptyNew.addEventListener('click', () => openNew());
    document.getElementById('rbImport').addEventListener('click', importGpx);
    document.getElementById('rbFromAct').addEventListener('click', openFromActivity);
    bindDelete(host, () => load(host, {}));

    paintThumbs(books);
  }

  function bindDelete(root, after) {
    U.$$('[data-del]', root).forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const id = Number(btn.dataset.del);
        const ok = await UI.confirmDialog('删除路书', '删除后这条路线就没了，确定吗？', '删除', true);
        if (!ok) return;
        try {
          await api.deleteRoutebook(id);
          UI.toast('已删除', 'ok');
          after();
        } catch (err) {
          UI.toast('删除失败：' + U.esc(err.message), 'err');
        }
      });
    });
  }

  function paintThumbs(books) {
    const cfg = state.mapCfg || {};
    books.forEach(b => {
      const cv = host.querySelector(`.rb-thumb-canvas[data-id="${b.id}"]`);
      if (!cv || !b.preview || b.preview.length < 2) return;
      const w = cv.clientWidth || 240;
      cv.width = Math.round(w * (window.devicePixelRatio || 1));
      cv.height = Math.round(112 * (window.devicePixelRatio || 1));
      MapThumb.render(cv, b.preview, { tileUrl: cfg.url, gcj: cfg.gcj, color: '#fc4c02' });
    });
  }

  /* ------------------------------------------------------------ 只读详情 */

  function tile(label, value, unit, sub) {
    return `<div class="stat">
      <span class="stat-label">${label}</span>
      <b class="stat-value">${value}${unit ? `<small>${unit}</small>` : ''}</b>
      ${sub ? `<div class="stat-sub">${sub}</div>` : ''}
    </div>`;
  }

  function waypointRows(waypoints) {
    return waypoints.map((w, i) => `<tr>
        <td class="mono dim">${i + 1}</td>
        <td class="mono">${w.d != null ? U.km(w.d, 1) : '—'}</td>
        <td><b>${U.esc(w.name || (i === 0 ? '起点' : '途经点 ' + (i + 1)))}</b></td>
        <td>${w.tag ? `<span class="chip">${U.esc(w.tag)}</span>` : '<span class="dim">—</span>'}</td>
        <td class="dim">${U.esc(w.note || '')}</td>
      </tr>`).join('');
  }

  function waypointCard(waypoints) {
    if (!waypoints.length) return '';
    return `<div class="card section">
      <div class="card-head">
        <h3>途经点</h3>
        <span class="dim">${waypoints.length} 个 · 起点到终点按里程排</span>
      </div>
      <div style="overflow-x:auto">
        <table class="data-table">
          <thead><tr><th>#</th><th>里程 km</th><th>名称</th><th>标签</th><th>备注</th></tr></thead>
          <tbody>${waypointRows(waypoints)}</tbody>
        </table>
      </div>
    </div>`;
  }

  function profileCard(info) {
    const profile = info.profile || [];
    const gain = Number(info.climb_gain_total != null ? info.climb_gain_total : (info.climb_gain || 0));
    const sub = [
      info.alt_min != null ? `最低 ${U.num(info.alt_min)} m` : '',
      info.alt_max != null ? `最高 ${U.num(info.alt_max)} m` : '',
      gain ? `累计爬升 +${U.num(gain)} m` : '',
    ].filter(Boolean).join(' · ');
    return `<div class="card section">
      <div class="card-head">
        <h3>海拔剖面</h3>
        <span class="dim">${U.esc(sub)}</span>
      </div>
      <div class="rb-profile"><canvas id="rbProfile"></canvas></div>
    </div>`;
  }

  function drawProfile(info) {
    const cv = document.getElementById('rbProfile');
    if (!cv) return;
    const profile = info.profile || [];
    if (profile.length < 3) return;
    const x = profile.map(p => p[0] / 1000);
    const y = profile.map(p => p[1]);
    if (state.profileChart) { try { state.profileChart.destroy(); } catch (e) { /* ignore */ } }
    state.profileChart = new Chart(cv, {
      type: 'line',
      height: 148,
      xValues: x,
      xMin: 0,
      xMax: Math.max(0.1, x[x.length - 1]),
      series: [{ key: 'alt', data: y, color: 'var(--walk)', fill: true, fillOpacity: 0.18, axis: 0 }],
      axes: [{ min: 0 }],
      legend: false,
      padding: { top: 10, right: 16, bottom: 22, left: 42 },
      onClick: (idx) => {
        const p = profile[idx];
        if (p) focusDistance(p[0]);
      },
      onHover: (idx) => {
        const p = idx >= 0 ? profile[idx] : null;
        if (p) focusDistance(p[0], { hover: true });
      },
    });
  }

  function climbRowHtml(c, i) {
    return `<tr data-climb-d="${c.start_m}" class="rb-climb-tr" title="点这一行在地图上定位">
        <td class="mono dim">${i + 1}</td>
        <td class="mono">K${(c.start_m / 1000).toFixed(1)} – K${(c.end_m / 1000).toFixed(1)}</td>
        <td class="mono">${(c.length_m / 1000).toFixed(1)} km</td>
        <td class="mono"><b>+${U.num(c.gain_m)} m</b></td>
        <td class="mono">${(c.avg_grade_pct || 0).toFixed(1)}%</td>
        <td class="mono dim">${U.esc(c.label || '')}</td>
      </tr>`;
  }

  /** 详情页爬坡按等级筛选：只重画 tbody，点行的地图联动重新接上 */
  function initClimbFilter(climbs) {
    const body = document.getElementById('rbClimbBody');
    if (!body) return;
    const summaryEl = document.getElementById('rbClimbSummary');
    const box = document.getElementById('rbClimbFilter');
    const all = climbs.map((c, i) => ({ c, i }));
    const bind = () => U.$$('[data-climb-d]', body).forEach(row => {
      row.addEventListener('click', () => focusDistance(Number(row.dataset.climbD)));
    });
    const paint = (level) => {
      const picked = new Set(Routing.filterClimbs(climbs, level));
      const list = all.filter(x => picked.has(x.c));
      body.innerHTML = list.map(x => climbRowHtml(x.c, x.i)).join('');
      bind();
      const head = level === 'all'
        ? `共 ${climbs.length} 段：${Routing.climbLevelSummary(climbs)}`
        : `只看 ${level}：${list.length} 段 / 共 ${climbs.length} 段`;
      if (summaryEl) summaryEl.textContent = head + ' · 点一行在地图上定位';
    };
    if (box) {
      U.$$('button', box).forEach(btn => {
        btn.addEventListener('click', () => {
          U.$$('button', box).forEach(b => {
            const on = b === btn;
            b.classList.toggle('active', on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
          });
          paint(btn.dataset.level);
        });
      });
    }
    bind();
  }

  function climbCard(climbs) {
    const counts = Routing.climbLevelCounts(climbs);
    const summary = Routing.climbLevelSummary(climbs);
    const levels = Routing.CLIMB_LEVELS.filter(k => counts[k]);
    const chips = levels.length > 1
      ? `<div class="seg rb-climb-filter" id="rbClimbFilter" role="group" aria-label="按等级筛选爬坡">
          <button type="button" class="active" data-level="all" aria-pressed="true">全部 ×${climbs.length}</button>
          ${levels.map(k => `<button type="button" data-level="${U.esc(k)}" aria-pressed="false">${U.esc(k)} ×${counts[k]}</button>`).join('')}
        </div>`
      : '';
    const rows = climbs.map(climbRowHtml).join('');
    return `<div class="card section" id="rbClimbCard">
      <div class="card-head">
        <h3>${Icon.get('mountain')}爬坡</h3>
        <span class="dim" id="rbClimbSummary">共 ${climbs.length} 段：${summary} · 点一行在地图上定位</span>
      </div>
      ${chips}
      <div style="overflow-x:auto">
        <table class="data-table">
          <thead><tr><th>#</th><th>里程区间</th><th>长度</th><th>爬升</th><th>平均坡度</th><th>等级</th></tr></thead>
          <tbody id="rbClimbBody">${rows}</tbody>
        </table>
      </div>
    </div>`;
  }

  function cueCard(cues) {
    const rows = cues.map(c => `<tr>
        <td class="mono">${U.km(c.d, 1)}</td>
        <td><span class="rb-turn">
          <span class="rb-arrow" style="transform:rotate(${Math.round(c.heading || 0)}deg)">${ARROW}</span>
          ${U.esc(c.dir)}
        </span></td>
        <td class="mono dim">${Math.round(c.heading || 0)}°</td>
        <td class="mono dim">${c.alt != null ? U.num(c.alt) + ' m' : '—'}</td>
      </tr>`).join('');
    return `<div class="card section">
      <div class="card-head">
        <h3>路口与转向</h3>
        <span class="dim">${cues.length} 个提示 · 按里程排 · 点越密越准</span>
      </div>
      <div style="overflow:auto;max-height:460px">
        <table class="data-table">
          <thead><tr><th>里程 km</th><th>提示</th><th>方位</th><th>海拔</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
  }

  function kmCard(kms) {
    const rows = kms.map(k => `<tr>
        <td class="mono">K${(k.d / 1000).toFixed(0)}</td>
        <td class="mono">${U.dur(k.t, { clock: true })}</td>
        <td class="mono dim">${k.alt != null ? U.num(k.alt) + ' m' : '—'}</td>
        <td class="mono dim">${k.speed != null ? Number(k.speed).toFixed(1) + ' km/h' : '—'}</td>
      </tr>`).join('');
    return `<details class="fold card section">
      <summary>
        <span class="fold-title">公里节点</span>
        <span class="tiny dim">每公里一个，给码表对表用</span>
        <span class="spacer"></span>
        <span class="fold-chevron">${Icon.get('chevron')}</span>
      </summary>
      <div class="fold-body" style="overflow:auto;max-height:420px">
        <table class="data-table">
          <thead><tr><th>里程</th><th>累计用时</th><th>海拔</th><th>均速</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </details>`;
  }

  /* ------------------------------------------------ 实时海拔 / 爬升 */

  function destroyElev() {
    if (state.elevChart) {
      try { state.elevChart.destroy(); } catch (e) { /* ignore */ }
      state.elevChart = null;
    }
  }

  function scheduleElevation() {
    clearTimeout(state.elevDebounce);
    state.elevDebounce = setTimeout(updateElevation, 260);
  }

  async function updateElevation() {
    const wrap = document.getElementById('rbElevWrap');
    const statsEl = document.getElementById('rbElevStats');
    if (!wrap || !statsEl) return;
    const track = state.track || [];
    if (track.length < 2) {
      wrap.hidden = true;
      statsEl.textContent = '生成路线后实时显示';
      state.elevGain = null;
      destroyElev();
      return;
    }
    const my = ++state.elevSeq;
    statsEl.textContent = '正在算海拔…';
    const res = await Routing.elevation(track.map(p => [p[0], p[1]]), 320);
    if (my !== state.elevSeq) return;
    if (!res.ok) {
      statsEl.textContent = '海拔数据不可用：' + (res.reason || '服务没响应');
      state.elevGain = null;
      wrap.hidden = true;
      return;
    }
    const st = Routing.climbStats(res.profile);
    state.elevGain = st.gain;
    statsEl.textContent = `累计爬升 +${st.gain} m · 下降 ${st.loss} m · 最高 ${st.max} m · 最低 ${st.min} m`;
    wrap.hidden = false;
    attachElevation(res.profile);
    // 统计和保存都以「写回轨迹点的高度」为准，这样编辑器里的数和详情页/列表完全一致
    const series = trackElevationSeries();
    const st2 = series.length >= 2 ? Routing.climbStats(series) : st;
    state.elevGain = st2.gain;
    statsEl.textContent = `累计爬升 +${st2.gain} m · 下降 ${st2.loss} m · 最高 ${st2.max} m · 最低 ${st2.min} m`;
    const finalSeries = series.length >= 2 ? series : res.profile;
    state.elevSeries = finalSeries;
    drawElevChart(finalSeries);
    renderElevClimbs(Routing.climbs(finalSeries));
  }

  /** 轨迹点上的海拔 → [[累计距离, 海拔], ...]（和保存到库里的数据完全同一份） */
  function trackElevationSeries() {
    const track = state.track || [];
    const out = [];
    let acc = 0;
    for (let i = 0; i < track.length; i++) {
      if (i > 0) acc += haversine(track[i - 1], track[i]);
      const alt = track[i][2];
      if (alt != null && isFinite(alt)) out.push([acc, alt]);
    }
    return out;
  }

  /** 把海拔按里程插值写回轨迹点（[lat, lon, ele]），保存后详情页才有剖面和爬坡 */
  function attachElevation(profile) {
    const track = state.track || [];
    if (track.length < 2 || !profile || profile.length < 2) return;
    let j = 0;
    let acc = 0;
    const out = [];
    for (let i = 0; i < track.length; i++) {
      if (i > 0) acc += haversine(track[i - 1], track[i]);
      while (j < profile.length - 2 && profile[j + 1][0] < acc) j++;
      const p0 = profile[j];
      const p1 = profile[Math.min(j + 1, profile.length - 1)];
      const span = Math.max(1e-6, p1[0] - p0[0]);
      const t = Math.max(0, Math.min(1, (acc - p0[0]) / span));
      const alt = p0[1] + (p1[1] - p0[1]) * t;
      out.push([track[i][0], track[i][1], Math.round(alt * 10) / 10]);
    }
    state.track = out;
  }

  function drawElevChart(profile) {
    const cv = document.getElementById('rbElevChart');
    if (!cv || !profile || profile.length < 2) return;
    const x = profile.map(p => p[0] / 1000);
    const y = profile.map(p => p[1]);
    destroyElev();
    state.elevChart = new Chart(cv, {
      type: 'line',
      height: 132,
      xValues: x,
      xMin: 0,
      xMax: Math.max(0.05, x[x.length - 1]),
      series: [{ key: 'alt', data: y, color: 'var(--walk)', fill: true, fillOpacity: 0.2, axis: 0 }],
      axes: [{ pad: 0.15 }],
      legend: false,
      padding: { top: 10, right: 16, bottom: 20, left: 42 },
      onClick: (idx) => {
        const p = profile[idx];
        if (p) focusDistance(p[0]);
      },
      onHover: (idx) => {
        const p = idx >= 0 ? profile[idx] : null;
        if (p) focusDistance(p[0], { hover: true });
      },
    });
  }

  /* ------------------------------------------------ 爬升 → 地图 */

  /** 轨迹上「里程 d 米」对应哪个点 */
  function trackIndexOf(distance) {
    const track = state.track || [];
    let acc = 0;
    for (let i = 0; i < track.length; i++) {
      if (i > 0) acc += haversine(track[i - 1], track[i]);
      if (acc >= distance) return i;
    }
    return track.length - 1;
  }

  let hoverRaf = 0;
  let hoverDistance = null;

  /** 剖面悬停：地图跟着平移（节流到每帧一次，避免卡） */
  function hoverFocus(distance) {
    hoverDistance = distance;
    if (hoverRaf) return;
    hoverRaf = requestAnimationFrame(() => {
      hoverRaf = 0;
      const track = state.track || [];
      if (hoverDistance == null || !state.map || track.length < 2) return;
      const p = track[trackIndexOf(hoverDistance)];
      if (p) state.map.panToPoint(p[0], p[1]);
    });
  }

  function focusDistance(distance, opts) {
    if (opts && opts.hover) { hoverFocus(distance); return; }
    const track = state.track || [];
    if (!state.map || track.length < 2) return;
    const i = trackIndexOf(distance);
    const p = track[i];
    if (!p) return;
    state.map.showPoint(p[0], p[1]);
    scrollMapIntoView();
  }

  /** 编辑器里：爬升概览 + 每段爬坡（点一行地图跳过去） */
  function renderElevClimbs(list) {
    const box = document.getElementById('rbElevClimbs');
    if (!box) return;
    if (!list || !list.length) {
      box.innerHTML = '<span class="tiny dim">这段路没有 ≥300 m 的持续爬坡</span>';
      return;
    }
    const counts = {};
    list.forEach(c => { counts[c.label] = (counts[c.label] || 0) + 1; });
    const summary = Object.keys(counts).map(k => `${k} ×${counts[k]}`).join(' · ');
    box.innerHTML = `<div class="tiny dim" style="margin-bottom:6px">本路书 ${list.length} 段爬坡：${summary}</div>`
      + list.map(c => `<button class="rb-climb-row" data-d="${c.start_m}">
          <span class="rb-climb-lv">${U.esc(c.label)}</span>
          <span class="mono">K${(c.start_m / 1000).toFixed(1)}–K${(c.end_m / 1000).toFixed(1)}</span>
          <span class="mono">${(c.length_m / 1000).toFixed(1)} km</span>
          <span class="mono">+${Math.round(c.gain_m)} m</span>
          <span class="mono">${c.avg_grade_pct.toFixed(1)}%</span>
        </button>`).join('');
    U.$$('.rb-climb-row', box).forEach(btn => {
      btn.addEventListener('click', () => focusDistance(Number(btn.dataset.d)));
    });
  }

  function renderView() {
    state.mode = 'view';
    cleanup();
    const b = state.book || {};
    const info = state.routeInfo || {};
    const cues = info.cues || [];
    const climbs = info.climbs || [];
    const kms = info.kms || [];
    const waypoints = b.waypoints || [];
    const dist = b.distance_m || info.total_m || 0;
    const climbGain = info.climb_gain_total != null
      ? Number(info.climb_gain_total)
      : (climbs.length
        ? climbs.reduce((s, c) => s + (c.gain_m || 0), 0)
        : Number(b.climb_m || 0));
    const hasElev = climbs.length > 0 || kms.length > 0 || (info.profile || []).length > 0;

    host.innerHTML = `
      <div class="detail-bar">
        <a class="btn btn-sm btn-ghost" href="#/routebook">${Icon.get('back')}全部路书</a>
        <div class="spacer"></div>
        <button class="btn btn-sm btn-danger" id="rbDel">${Icon.get('trash')}删除</button>
        <button class="btn btn-sm" id="rbEdit">编辑路线</button>
        <a class="btn btn-sm btn-primary" href="/api/routebooks/${b.id}/gpx" download>${Icon.get('download')}下载 GPX</a>
      </div>

      <div class="rb-head">
        <h2 class="rb-title-static">${U.esc(b.name || '未命名路书')}</h2>
        <div class="rb-meta">
          <span>${U.esc(sourceLabel(b))}</span>
          <span class="dim">${b.updated_at ? '更新于 ' + U.dateShort(b.updated_at) : ''}</span>
        </div>
      </div>

      <div class="grid g-4">
        ${tile('距离', U.km(dist, 1), 'km')}
        ${tile('累计爬升', climbGain ? '+' + U.num(climbGain) : '—', climbGain ? 'm' : '')}
        ${tile('途经点', U.num(waypoints.length), '个', waypoints.length ? '起点/补给/打卡都在这' : '没有途经点')}
        ${tile('爬坡', U.num(climbs.length), '段',
          climbs.length ? '≥300 m 且坡度 ≥3%' : (hasElev ? '没有成段爬坡' : '手绘路线没有海拔'))}
      </div>

      <div class="card rb-mapwrap rb-mapwrap--view section">
        <div id="rbMap" class="rb-map"></div>
      </div>

      ${waypointCard(waypoints)}
      ${(info.profile && info.profile.length >= 3) ? profileCard(info) : ''}
      ${climbs.length ? climbCard(climbs) : ''}
      ${cues.length ? cueCard(cues) : ''}
      ${kms.length ? kmCard(kms) : ''}
      ${(info.notes && info.notes.length) ? `<div class="card section" style="padding:14px 16px">
        ${info.notes.map(n => `<div class="tiny dim" style="line-height:1.7">· ${U.esc(n)}</div>`).join('')}
      </div>` : ''}
    `;
    Icon.hydrate(host);
    setTitle(b.name || '路书');

    document.getElementById('rbEdit').addEventListener('click', () => {
      state.waypoints = waypoints.map(w => ({
        lat: w.lat, lon: w.lon,
        name: w.name || '', tag: w.tag || '', note: w.note || '', show: w.show !== false,
      }));
      state.track = (b.points || []).slice();
      state.name = b.name || '未命名路书';
      state.routeMode = 'road';
      state.routeInfo = info;
      state.undo = [];
      state.redo = [];
      renderEditor();
    });
    document.getElementById('rbDel').addEventListener('click', async () => {
      const ok = await UI.confirmDialog('删除路书', '删除后这条路线就没了，确定吗？', '删除', true);
      if (!ok) return;
      try {
        await api.deleteRoutebook(b.id);
        UI.toast('已删除', 'ok');
        location.hash = '#/routebook';
      } catch (err) {
        UI.toast('删除失败：' + U.esc(err.message), 'err');
      }
    });

    initViewMap(waypoints);
    if ((info.profile || []).length >= 3) drawProfile(info);
    if (climbs.length) initClimbFilter(climbs);
  }

  function initViewMap(waypoints) {
    const el = document.getElementById('rbMap');
    if (!el) return;
    const cfg = state.mapCfg || {};
    state.map = new TrackMap(el, {
      height: 420,
      tileUrl: cfg.url,
      gcj: cfg.gcj,
      interactive: true,
      showVertices: false,
    });
    if (state.track && state.track.length) state.map.setRoute(state.track);
    if (waypoints && waypoints.length) state.map.setWaypoints(waypoints);
    el.__map = state.map;
  }

  /* ------------------------------------------------------------ 从活动 / 导入 */

  async function openFromActivity() {
    let acts = [];
    try {
      const res = await api.activities({ limit: 40 });
      acts = (res.items || []).filter(a => a.has_gps);
    } catch (err) {
      UI.toast('无法加载活动列表', 'err');
      return;
    }
    if (!acts.length) { UI.toast('还没有带轨迹的活动', 'err'); return; }

    const dlg = document.createElement('dialog');
    dlg.className = 'dialog';
    dlg.innerHTML = `
      <form method="dialog">
        <div class="dialog-head">
          <h2>从活动生成路书</h2>
          <button class="icon-btn" value="cancel" aria-label="关闭">${Icon.get('x')}</button>
        </div>
        <div class="dialog-body">
          <p class="tiny dim" style="margin-bottom:10px">选一条骑过的活动，用它的真实轨迹做路书。</p>
          <div class="rb-pick">
            ${acts.map(a => `
              <label class="rb-pick-row">
                <input type="radio" name="rbAct" value="${a.id}">
                <span class="rb-pick-main">
                  <b>${U.esc(a.title || '未命名')}</b>
                  <span class="tiny dim">${U.dateShort(a.start_time)} · ${U.km(a.distance_m, 1)} km</span>
                </span>
              </label>`).join('')}
          </div>
        </div>
        <div class="dialog-foot">
          <button class="btn" value="cancel">取消</button>
          <button class="btn btn-primary" value="ok">生成</button>
        </div>
      </form>`;
    document.body.appendChild(dlg);
    Icon.hydrate(dlg);
    dlg.addEventListener('close', async () => {
      const chosen = dlg.querySelector('input[name="rbAct"]:checked');
      const id = chosen ? Number(chosen.value) : 0;
      dlg.remove();
      if (!id) return;
      try {
        const book = await api.routebookFromActivity({ activity_id: id });
        UI.toast('已从活动生成路书', 'ok');
        location.hash = `#/routebook/${book.id}`;
      } catch (err) {
        UI.toast('生成失败：' + U.esc(err.message), 'err');
      }
    });
    dlg.showModal();
  }

  async function ingestGpxText(text, fallbackName) {
    const doc = new DOMParser().parseFromString(text, 'application/xml');
    if (doc.querySelector('parsererror')) throw new Error('这个文件不是合法的 GPX');
    const pts = [...doc.querySelectorAll('trkpt, rtept')].map(n => {
      const lat = Number(n.getAttribute('lat'));
      const lon = Number(n.getAttribute('lon'));
      const eleNode = n.querySelector('ele');
      const ele = eleNode ? Number(eleNode.textContent) : null;
      const point = [lat, lon];
      if (ele != null && isFinite(ele)) point.push(Math.round(ele * 10) / 10);
      return point;
    }).filter(p => isFinite(p[0]) && isFinite(p[1]));
    if (pts.length < 2) throw new Error('这个 GPX 里没有可用的坐标点');
    const hasEle = pts.some(p => p.length > 2);
    // 行者导入 GPX 会连 <wpt> 一起收：有就用，没有就把首尾当起终点
    const wpts = [...doc.querySelectorAll('wpt')].map(n => ({
      lat: Number(n.getAttribute('lat')),
      lon: Number(n.getAttribute('lon')),
      name: (n.querySelector('name') || {}).textContent || '',
      tag: (n.querySelector('sym') || {}).textContent || '',
      note: (n.querySelector('desc') || {}).textContent || '',
      show: true,
    })).filter(w => isFinite(w.lat) && isFinite(w.lon));
    const nameNode = doc.querySelector('trk > name, metadata > name');
    const name = ((nameNode && nameNode.textContent) || fallbackName || '').trim().slice(0, 120);
    const book = await api.createRoutebook({
      name: name || '导入的路书',
      points: pts,
      waypoints: wpts,
      source: 'gpx',
    });
    UI.toast(`已导入 ${pts.length} 个点${hasEle ? '（带海拔）' : ''}`
      + (wpts.length ? `、${wpts.length} 个途经点` : ''), 'ok');
    location.hash = `#/routebook/${book.id}`;
    return book;
  }

  function importGpx() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.gpx,application/gpx+xml';
    input.addEventListener('change', async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      try {
        await ingestGpxText(await file.text(), file.name.replace(/\.gpx$/i, ''));
      } catch (err) {
        UI.toast('导入失败：' + U.esc(err.message), 'err');
      }
    });
    input.click();
  }

  /* ------------------------------------------------------------ 制作（行者式） */

  function wpKind(i, n) {
    if (i === 0) return 'start';
    if (n > 1 && i === n - 1) return 'end';
    return 'mid';
  }

  function wpLabel(i, n) {
    const kind = wpKind(i, n);
    return kind === 'start' ? 'S' : (kind === 'end' ? 'E' : String(i));
  }

  function wpDistance(wp) {
    const track = state.track || [];
    if (track.length < 2) return null;
    let best = null, bestI = 0;
    for (let i = 0; i < track.length; i++) {
      const dd = (track[i][0] - wp.lat) ** 2 + (track[i][1] - wp.lon) ** 2;
      if (best === null || dd < best) { best = dd; bestI = i; }
    }
    let acc = 0;
    for (let i = 1; i <= bestI; i++) acc += haversine(track[i - 1], track[i]);
    return acc;
  }

  function snapshot() {
    return JSON.stringify({ waypoints: state.waypoints, routeMode: state.routeMode });
  }

  function pushUndo() {
    state.undo.push(snapshot());
    if (state.undo.length > 60) state.undo.shift();
    state.redo = [];
    state.dirty = true;
    updateUndoButtons();
  }

  function updateUndoButtons() {
    const u = document.getElementById('rbUndo');
    const r = document.getElementById('rbRedo');
    if (u) u.disabled = state.undo.length === 0;
    if (r) r.disabled = state.redo.length === 0;
  }

  function restore(snap) {
    const s = JSON.parse(snap);
    state.waypoints = s.waypoints || [];
    state.routeMode = s.routeMode || 'road';
    const sel = document.getElementById('rbRouteMode');
    if (sel) sel.value = state.routeMode;
    renderWpList();
    resnap(true);
  }

  function undo() {
    if (!state.undo.length) return;
    state.redo.push(snapshot());
    restore(state.undo.pop());
    updateUndoButtons();
  }

  function redo() {
    if (!state.redo.length) return;
    state.undo.push(snapshot());
    restore(state.redo.pop());
    updateUndoButtons();
  }

  function renderEditor(opts) {
    state.mode = 'edit';
    cleanup();
    const o = opts || {};
    const fromActivity = state.book && state.book.id && state.book.source === 'activity';
    host.innerHTML = `
      <div class="detail-bar rb-editor-bar">
        <a class="btn btn-sm btn-ghost" href="#/routebook">${Icon.get('back')}全部路书</a>
        <input class="rb-title" id="rbName" value="${U.esc(state.name || '未命名路书')}" maxlength="120" spellcheck="false">
        <div class="spacer"></div>
        <select class="control rb-mode" id="rbRouteMode" title="规划方式">
          <option value="road">骑行（大路优先）</option>
          <option value="walk">徒步（允许小路）</option>
          <option value="straight">直线</option>
        </select>
        <button class="btn btn-sm" id="rbUndo" title="撤销：退回上一步修改（Ctrl+Z）">撤销</button>
        <button class="btn btn-sm" id="rbRedo" title="重做：恢复刚刚撤销的那一步（Ctrl+Shift+Z）">重做</button>
        <button class="btn btn-sm" id="rbClear" title="清空路线：删掉所有途经点，重新画">清空路线</button>
        <button class="btn btn-sm btn-ghost" id="rbAdjust" title="解锁地图，继续加点/拖锚点" hidden>继续调整</button>
        <button class="btn btn-sm" id="rbCancel">取消</button>
        <button class="btn btn-primary btn-sm" id="rbSave">保存</button>
      </div>

      ${fromActivity ? `<p class="rb-warn tiny dim">这条路书是从活动轨迹复制的。只改名字不会影响它；
        一旦增删点，就和原活动的海拔、时间对不上，爬坡和公里节点会按新轨迹重算。</p>` : ''}

      <details class="fold card rb-plan" id="rbPlan" open>
        <summary>
          <span class="fold-title">按地点生成</span>
          <span class="tiny dim" id="rbPlanSummary">起点 → 途经点（可加多个）→ 终点，按顺序排路线</span>
          <span class="spacer"></span>
          <span class="fold-chevron">${Icon.get('chevron')}</span>
        </summary>
        <div class="rb-plan-rows" id="rbPlanRows"></div>
        <div class="rb-plan-tip tiny dim" id="rbPlanTip"></div>
        <div class="rb-plan-foot">
          <button class="btn btn-sm" id="rbPlanAddMid">+ 途经点</button>
          <button class="btn btn-sm btn-primary" id="rbPlanBuild">生成路书</button>
          <button class="btn btn-sm btn-ghost" id="rbPlanOptimize" title="按就近原则重排途经点，起点终点不动">优化顺序</button>
          <span class="tiny dim" id="rbPlanMsg"></span>
        </div>
      </details>

      <div class="card rb-mapwrap rb-editor-map" id="rbMapCard">
        <div id="rbMap" class="rb-map"></div>
        <div class="rb-hud">
          <span id="rbStat">0 个途经点 · 0.00 km</span>
          <span id="rbSnap" class="dim"></span>
        </div>
        <div class="rb-tools">
          <select class="rb-span" id="rbSpan" title="地图显示范围">
            <option value="500">500 m</option>
            <option value="1000" selected>1 km</option>
            <option value="2000">2 km</option>
            <option value="5000">5 km</option>
            <option value="10000">10 km</option>
            <option value="20000">20 km</option>
          </select>
          <button class="btn btn-sm" id="rbZoomIn" title="放大">${Icon.get('zoomIn')}</button>
          <button class="btn btn-sm" id="rbZoomOut" title="缩小">${Icon.get('zoomOut')}</button>
          <button class="btn btn-sm" id="rbLocate" title="定位到我">${Icon.get('pin')}</button>
          <button class="btn btn-sm" id="rbKms">KM 标记</button>
          <button class="btn btn-sm" id="rbLayer">卫星</button>
          <button class="btn btn-sm" id="rbFull" title="全屏">全屏</button>
        </div>
        <div class="rb-hint" id="rbHint">点地图加途经点 · 拖锚点改位置 · 滚轮缩放</div>
        <div class="rb-pickbar" id="rbPickBar" hidden>
          <span id="rbPickText" class="tiny">在地图上点一下，或拖动红点，选到准确位置</span>
          <button class="btn btn-sm btn-primary" id="rbPickOk">确认这个点</button>
          <button class="btn btn-sm" id="rbPickCancel">取消</button>
        </div>
      </div>

      <div class="card rb-elev section" id="rbElev">
        <div class="card-head">
          <h3>海拔 / 爬升</h3>
          <span class="dim" id="rbElevStats">生成路线后实时显示</span>
        </div>
        <div class="rb-profile" id="rbElevWrap" hidden><canvas id="rbElevChart"></canvas></div>
        <div class="rb-climb-list" id="rbElevClimbs"></div>
      </div>

      <div class="card rb-wp-card">
        <div class="card-head">
          <h3>途经点</h3>
          <span class="dim">名称 / 功能标签 / 备注，保存后详情页和 GPX 里都带着</span>
        </div>
        <div class="rb-wp-list" id="rbWpList"></div>
      </div>
    `;
    Icon.hydrate(host);
    state.satLayer = false;
    document.getElementById('rbName').addEventListener('input', (e) => {
      state.name = e.target.value;
      state.dirty = true;
    });
    document.getElementById('rbRouteMode').value = state.routeMode;
    document.getElementById('rbRouteMode').addEventListener('change', (e) => {
      pushUndo();
      state.routeMode = e.target.value;
      resnap(true);
    });
    const pickOk = document.getElementById('rbPickOk');
    if (pickOk) pickOk.addEventListener('click', confirmPending);
    const pickCancel = document.getElementById('rbPickCancel');
    if (pickCancel) pickCancel.addEventListener('click', cancelPending);
    const adjustBtn = document.getElementById('rbAdjust');
    if (adjustBtn) adjustBtn.addEventListener('click', () => {
      state.generated = false;
      adjustBtn.hidden = true;
      UI.toast('可以继续加点 / 拖锚点了', 'ok');
    });
    document.getElementById('rbUndo').addEventListener('click', undo);
    document.getElementById('rbRedo').addEventListener('click', redo);
    document.getElementById('rbClear').addEventListener('click', async () => {
      if (!state.waypoints.length) return;
      const ok = await UI.confirmDialog('清空路线', '所有途经点都会被删掉。', '清空', true);
      if (!ok) return;
      pushUndo();
      state.waypoints = [];
      state.track = [];
      clearDraft();
      renderWpList();
      paintTrack(false);
    });
    document.getElementById('rbCancel').addEventListener('click', () => {
      clearDraft();
      const target = (state.book && state.book.id) ? `#/routebook/${state.book.id}` : '#/routebook';
      // 从详情页点「编辑路线」进来时 hash 没变，直接赋一样值不会触发路由
      if (location.hash === target) {
        if (state.book && state.book.id) load(host, { id: state.book.id });
        else load(host, {});
      } else {
        location.hash = target;
      }
    });
    document.getElementById('rbSave').addEventListener('click', save);
    document.getElementById('rbKms').addEventListener('click', (e) => {
      state.kmMarks = !state.kmMarks;
      e.currentTarget.classList.toggle('is-on', state.kmMarks);
      if (state.map) state.map.setKmMarks(state.kmMarks);
    });
    document.getElementById('rbLayer').addEventListener('click', (e) => {
      state.satLayer = !state.satLayer;
      e.currentTarget.textContent = state.satLayer ? '标准图' : '卫星';
      const cfg = state.mapCfg || {};
      if (state.map) state.map.setTileUrl(state.satLayer ? SAT_URL : (cfg.url || ''));
    });
    wireEditorTools();
    state.plan = planFromWaypoints(state.waypoints);
    wirePlan();
    bindPlanEvents(document.getElementById('rbPlanRows'));
    renderPlan();
    state.snapMsg = '';
    state.generated = false;
    state.pending = null;

    initEditorMap(o.fit !== false);
    if ((state.track || []).length >= 2) scheduleElevation();
    bindWpEvents(document.getElementById('rbWpList'));
    renderWpList();
    const kmsBtn = document.getElementById('rbKms');
    if (kmsBtn && state.kmMarks) kmsBtn.classList.add('is-on');
    state.dirty = false;
    updateHud();
    updateUndoButtons();
    // 打开编辑器时就把地图带进视野（否则一进来只看到上面的地点表单）
    setTimeout(scrollMapIntoView, 120);
  }

  function wireEditorTools() {
    const span = document.getElementById('rbSpan');
    if (span) {
      span.value = String(state.spanMeters);
      span.addEventListener('change', () => {
        state.spanMeters = Number(span.value) || 1000;
        state.spanManual = true;
        if (state.map) state.map.setSpanMeters(state.spanMeters);
      });
    }
    const zoomIn = document.getElementById('rbZoomIn');
    const zoomOut = document.getElementById('rbZoomOut');
    if (zoomIn) zoomIn.addEventListener('click', () => state.map && state.map.zoomBy(1));
    if (zoomOut) zoomOut.addEventListener('click', () => state.map && state.map.zoomBy(-1));
    const locate = document.getElementById('rbLocate');
    if (locate) locate.addEventListener('click', () => {
      if (!navigator.geolocation) { UI.toast('这个浏览器不支持定位', 'err'); return; }
      locate.disabled = true;
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          locate.disabled = false;
          if (state.map) state.map.setView(pos.coords.latitude, pos.coords.longitude, 15);
          UI.toast('已定位到当前位置', 'ok');
        },
        () => {
          locate.disabled = false;
          UI.toast('定位失败：系统或浏览器没给权限', 'err');
        },
        { enableHighAccuracy: true, timeout: 8000 });
    });
    const full = document.getElementById('rbFull');
    const card = document.getElementById('rbMapCard');
    if (full && card) {
      full.addEventListener('click', () => {
        const on = !card.classList.contains('is-fullscreen');
        card.classList.toggle('is-fullscreen', on);
        document.body.classList.toggle('rb-locked', on);
        full.textContent = on ? '退出全屏' : '全屏';
        if (state.map) state.map.setHeight(on ? (window.innerHeight - 24) : (state.mapBaseHeight || 560));
      });
    }
  }

  /* ------------------------------------------------ 按地点生成（起点/途经/终点） */

  const PLAN_PH = {
    start: '起点，例如：广州东站',
    mid: '途经点，例如：白云山南门',
    end: '终点，例如：珠江新城',
  };

  function planFromWaypoints(wps) {
    const rows = (wps || []).map((w, i) => ({
      kind: i === 0 ? 'start' : (i === wps.length - 1 ? 'end' : 'mid'),
      place: { lat: w.lat, lon: w.lon, name: w.name || '' },
      q: w.name || '',
    }));
    if (!rows.length) return [{ kind: 'start' }, { kind: 'mid' }, { kind: 'end' }];
    if (rows.length === 1) rows.push({ kind: 'end' });
    return rows;
  }

  function planLabel(row, i) {
    return row.kind === 'start' ? 'S' : (row.kind === 'end' ? 'E' : String(i));
  }

  function renderPlan() {
    const box = document.getElementById('rbPlanRows');
    if (!box) return;
    box.innerHTML = state.plan.map((row, i) => `
      <div class="rb-plan-row" data-i="${i}">
        <span class="rb-wp-idx ${row.kind === 'start' ? 'start' : (row.kind === 'end' ? 'end' : 'mid')}">${planLabel(row, i)}</span>
        <input class="control rb-plan-input" data-i="${i}"
               value="${U.esc(row.q || (row.place && row.place.name) || '')}"
               placeholder="${PLAN_PH[row.kind] || PLAN_PH.mid}">
        <button class="icon-btn" data-plan-pick="${i}" title="在地图上点选这个位置">${Icon.get('pin')}</button>
        <button class="btn btn-sm" data-plan-search="${i}">搜索</button>
        <button class="icon-btn" data-plan-clear="${i}" title="清空这一个">${Icon.get('x')}</button>
        <div class="rb-plan-results" data-results="${i}" hidden></div>
      </div>`).join('');
    Icon.hydrate(box);
    updatePlanTip();
  }

  /** 搜索选中某个地点后，地图立刻飞过去并钉一个点 */
  function focusPlace(place) {
    if (!place || !state.map) return;
    const lat = Number(place.lat);
    const lon = Number(place.lon);
    if (!isFinite(lat) || !isFinite(lon)) return;
    state.map.focusLatLon(lat, lon, state.spanManual ? state.map.zoom : 16);
    if (state.spanManual) state.map.setSpanMeters(state.spanMeters);
    scrollMapIntoView();
  }

  /* ------------------------------------------------ 地图上确认地点 */

  function startPending(rowIndex, place) {
    const row = state.plan[rowIndex];
    if (!row) return;
    let lat, lon, name = '';
    if (place) {
      lat = Number(place.lat); lon = Number(place.lon); name = place.name || '';
    } else {
      const c = state.map ? state.map.screenToLatLon(state.map.w / 2, state.map.h / 2)
        : [23.13, 113.32];
      lat = c[0]; lon = c[1];
    }
    if (!isFinite(lat) || !isFinite(lon)) return;
    state.pending = { row: rowIndex, lat, lon, name };
    if (state.map) {
      state.map.focusLatLon(lat, lon, state.spanManual ? state.map.zoom : 16);
    }
    updatePickBar();
    scrollMapIntoView();
  }

  function updatePendingPin(lat, lon) {
    if (!state.pending) return;
    state.pending.lat = lat;
    state.pending.lon = lon;
    if (state.map) state.map.setFocusPin(lat, lon);
    updatePickBar();
  }

  function updatePickBar() {
    const bar = document.getElementById('rbPickBar');
    const txt = document.getElementById('rbPickText');
    const build = document.getElementById('rbPlanBuild');
    if (!bar) return;
    if (!state.pending) {
      bar.hidden = true;
      if (build) build.disabled = false;
      return;
    }
    bar.hidden = false;
    if (build) build.disabled = true;
    const p = state.pending;
    txt.textContent = `${p.name ? '已定位「' + p.name + '」' : '新点'}`
      + ` · ${p.lat.toFixed(5)}, ${p.lon.toFixed(5)} —— 点地图或拖动红点微调`;
  }

  function confirmPending() {
    const p = state.pending;
    if (!p) return;
    const row = state.plan[p.row];
    if (row) {
      row.place = {
        lat: Number(p.lat.toFixed(6)),
        lon: Number(p.lon.toFixed(6)),
        name: p.name || row.q || '地图选点',
      };
      row.q = row.place.name;
    }
    state.pending = null;
    if (state.map) state.map.setFocusPin(null);
    renderPlan();
    updatePickBar();
    UI.toast('位置已确认', 'ok');
  }

  function cancelPending() {
    state.pending = null;
    if (state.map) state.map.setFocusPin(null);
    updatePickBar();
  }

  /** 地图比视口还低时滚一下，别让用户自己找（留 16px 边距，别贴着边） */
  function scrollMapIntoView() {
    const card = document.getElementById('rbMapCard') || document.querySelector('.rb-mapwrap');
    if (!card) return;
    const r = card.getBoundingClientRect();
    const vh = window.innerHeight;
    if (r.bottom > vh - 12) {
      window.scrollBy({ top: r.bottom - vh + 16, behavior: 'smooth' });
    } else if (r.top < 56) {
      window.scrollBy({ top: r.top - 64, behavior: 'smooth' });
    }
  }

  function updatePlanTip() {
    const tip = document.getElementById('rbPlanTip');
    if (!tip) return;
    tip.innerHTML = window.Routing && window.Routing.amapReady()
      ? '用高德搜索，地铁站/小店都能找到。也可以直接粘贴坐标「23.12,113.32」。'
      : '免 Key 搜索基于 OSM，中文地名不一定全。搜不到可以换简称（如「嘉禾」），'
        + '或者直接粘贴坐标「23.12,113.32」。';
  }

  let amapWarnShown = '';
  function showSearchWarning(warning) {
    if (!warning) return;
    const tip = document.getElementById('rbPlanTip');
    if (tip) {
      tip.innerHTML = `<span style="color:var(--warn)">${U.esc(warning)}</span>`
        + ' —— 已用 Photon 顶上；要国内全量地名，去 <a href="#/settings">设置</a> 换一个'
        + '「Web服务」类型的高德 Key（JS API 的 Key 调不了搜索接口）。';
    }
    if (amapWarnShown !== warning) {
      amapWarnShown = warning;
      UI.toast(warning, 'err', 6000);
    }
  }

  function bindPlanEvents(box) {
    box.addEventListener('input', (e) => {
      const i = Number(e.target.dataset.i);
      const row = state.plan[i];
      if (!row || e.target.className.indexOf('rb-plan-input') < 0) return;
      row.q = e.target.value;
      if (row.place && row.q.trim() !== row.place.name) row.place = null;
    });
    box.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const i = Number(e.target.dataset.i);
      if (!isNaN(i) && e.target.classList.contains('rb-plan-input')) {
        e.preventDefault();
        searchPlanRow(i);
      }
    });
    box.addEventListener('click', (e) => {
      const pickBtn = e.target.closest('[data-plan-pick]');
      if (pickBtn) { startPending(Number(pickBtn.dataset.planPick), null); return; }
      const searchBtn = e.target.closest('[data-plan-search]');
      if (searchBtn) { searchPlanRow(Number(searchBtn.dataset.planSearch)); return; }
      const clearBtn = e.target.closest('[data-plan-clear]');
      if (clearBtn) {
        const i = Number(clearBtn.dataset.planClear);
        if (state.plan[i]) { state.plan[i].place = null; state.plan[i].q = ''; renderPlan(); }
        return;
      }
      const item = e.target.closest('.rb-search-item');
      if (item) {
        const results = item.closest('.rb-plan-results');
        const i = Number(results.dataset.results);
        const k = Number(item.dataset.k);
        const list = (results._items || [])[k];
        const row = state.plan[i];
        if (list && row) {
          startPending(i, list);
        }
      }
    });
  }

  async function searchPlanRow(i) {
    const row = state.plan[i];
    if (!row) return;
    const input = document.querySelector(`.rb-plan-input[data-i="${i}"]`);
    const box = document.querySelector(`.rb-plan-results[data-results="${i}"]`);
    if (!input || !box) return;
    const q = input.value.trim();
    if (!q) return;
    const center = state.map ? state.map.screenToLatLon(state.map.w / 2, state.map.h / 2) : null;
    box.hidden = false;
    box.innerHTML = `<div class="rb-search-tip dim">正在搜「${U.esc(q)}」…</div>`;
    const res = await Routing.search(q, center ? { lat: center[0], lon: center[1] } : null);
    if (res.warning) showSearchWarning(res.warning);
    if (!res.ok) {
      box.innerHTML = `<div class="rb-search-tip dim">搜索失败：${U.esc(res.error || '服务不可用')}</div>`;
      return;
    }
    if (!res.items.length) {
      box.innerHTML = `<div class="rb-search-tip dim">没搜到「${U.esc(q)}」，换个说法试试</div>`;
      return;
    }
    box._items = res.items;
    box.innerHTML = res.items.map((it, k) => `<button class="rb-search-item" data-k="${k}">
        <b>${U.esc(it.name)}</b>
        <span class="tiny dim">${U.esc(it.address || '')}${it.source === 'photon' ? ' · Photon' : ''}</span>
      </button>`).join('');
  }

  async function buildFromPlan() {
    if (state.pending) { UI.toast('先在地图上确认那个点', 'err'); return; }
    const msg = document.getElementById('rbPlanMsg');
    const picked = [];
    for (let i = 0; i < state.plan.length; i++) {
      const row = state.plan[i];
      const input = document.querySelector(`.rb-plan-input[data-i="${i}"]`);
      const q = (input ? input.value : (row.q || '')).trim();
      if (row.place && (!q || q === row.place.name)) {
        picked.push({ lat: row.place.lat, lon: row.place.lon, name: row.place.name, kind: row.kind });
        continue;
      }
      if (!q) continue;
      if (msg) msg.textContent = `正在定位「${q}」…`;
      const center = state.map ? state.map.screenToLatLon(state.map.w / 2, state.map.h / 2) : null;
      const res = await Routing.search(q, center ? { lat: center[0], lon: center[1] } : null);
      if (res.warning) showSearchWarning(res.warning);
      if (!res.ok || !res.items.length) {
        if (msg) msg.textContent = `没找到「${q}」，换个地名或点搜索挑一个`;
        return;
      }
      const it = res.items[0];
      row.place = { lat: it.lat, lon: it.lon, name: it.name };
      row.q = it.name;
      picked.push({ lat: it.lat, lon: it.lon, name: it.name, kind: row.kind });
    }
    if (picked.length < 2) {
      if (msg) msg.textContent = '至少要有一个起点和一个终点';
      return;
    }
    // 同一地点填了两遍：重复点会让路线绕回原地（看着像两条路），直接去掉
    const uniq = [];
    for (const p of picked) {
      const last = uniq[uniq.length - 1];
      if (last && haversine([last.lat, last.lon], [p.lat, p.lon]) < 25) continue;
      uniq.push(p);
    }
    if (uniq.length < 2) {
      if (msg) msg.textContent = '起点和终点重复了，换一个地点';
      return;
    }
    picked.length = 0;
    picked.push(...uniq);
    pushUndo();
    state.waypoints = picked.map(p => ({
      lat: Number(Number(p.lat).toFixed(6)),
      lon: Number(Number(p.lon).toFixed(6)),
      name: p.name || '',
      tag: '',
      note: '',
      show: true,
    }));
    renderWpList();
    resnap(true);
    saveDraft();
    renderPlan();
    if (msg) msg.textContent = `已按 ${picked.length} 个地点生成，可以再拖锚点微调`;
    state.generated = true;
    const adjustBtn = document.getElementById('rbAdjust');
    if (adjustBtn) adjustBtn.hidden = false;
    // 生成完把「按地点生成」收起来，让地图完整露出来
    const planEl = document.getElementById('rbPlan');
    if (planEl) planEl.open = false;
    const summary = document.getElementById('rbPlanSummary');
    if (summary) summary.textContent = `已按 ${picked.length} 个地点生成 · 点开可改`;
    scrollMapIntoView();
  }

  function wirePlan() {
    const addBtn = document.getElementById('rbPlanAddMid');
    if (addBtn) addBtn.addEventListener('click', () => {
      const endIdx = state.plan.findIndex(r => r.kind === 'end');
      const row = { kind: 'mid' };
      if (endIdx >= 0) state.plan.splice(endIdx, 0, row);
      else state.plan.push(row);
      renderPlan();
    });
    const buildBtn = document.getElementById('rbPlanBuild');
    if (buildBtn) buildBtn.addEventListener('click', buildFromPlan);
    const optBtn = document.getElementById('rbPlanOptimize');
    if (optBtn) optBtn.addEventListener('click', optimizeOrder);
  }

  function pathLen(list) {
    let d = 0;
    for (let i = 1; i < list.length; i++) d += haversine([list[i - 1].lat, list[i - 1].lon], [list[i].lat, list[i].lon]);
    return d;
  }

  /** 就近重排中间点（起点/终点不动），再做几轮 2-opt */
  function optimizeSequence(list) {
    if (list.length < 4) return list.slice();
    const start = list[0];
    const end = list[list.length - 1];
    let rest = list.slice(1, -1);
    const seq = [start];
    let cur = start;
    while (rest.length) {
      rest.sort((a, b) => haversine([cur.lat, cur.lon], [a.lat, a.lon])
        - haversine([cur.lat, cur.lon], [b.lat, b.lon]));
      cur = rest.shift();
      seq.push(cur);
    }
    seq.push(end);
    let improved = true;
    let guard = 0;
    while (improved && guard++ < 60) {
      improved = false;
      for (let i = 1; i < seq.length - 3; i++) {
        for (let j = i + 2; j < seq.length - 2; j++) {
          const a = seq[i - 1], b = seq[i], c = seq[j], d = seq[j + 1];
          const now = haversine([a.lat, a.lon], [b.lat, b.lon]) + haversine([c.lat, c.lon], [d.lat, d.lon]);
          const alt = haversine([a.lat, a.lon], [c.lat, c.lon]) + haversine([b.lat, b.lon], [d.lat, d.lon]);
          if (alt + 1 < now) {
            const seg = seq.slice(i, j + 1).reverse();
            seq.splice(i, seg.length, ...seg);
            improved = true;
          }
        }
      }
    }
    return seq;
  }

  async function optimizeOrder() {
    const msg = document.getElementById('rbPlanMsg');
    if (state.waypoints.length < 4) {
      if (msg) msg.textContent = '点太少了，不用优化';
      return;
    }
    const before = pathLen(state.waypoints);
    const seq = optimizeSequence(state.waypoints);
    const after = pathLen(seq);
    const saved = Math.round((before - after) / 1000);
    if (saved < 1) {
      if (msg) msg.textContent = '当前顺序已经挺顺了';
      return;
    }
    const ok = await UI.confirmDialog('优化途经点顺序',
      `按就近原则重排，直线里程大约能少 ${saved} km（起点、终点不动，其余中间点会换顺序）。`,
      '重排', true);
    if (!ok) return;
    pushUndo();
    state.waypoints = seq;
    renderWpList();
    resnap(true);
    saveDraft();
    if (msg) msg.textContent = `顺序已优化，直线里程少了约 ${saved} km`;
  }

  function initEditorMap(fit) {
    const el = document.getElementById('rbMap');
    if (!el) return;
    const cfg = state.mapCfg || {};
    // 地图高度跟着窗口走：窗口小的时候别把地图顶到屏幕外面
    const baseH = Math.max(360, Math.min(580, window.innerHeight - 300));
    state.mapBaseHeight = baseH;
    state.map = new TrackMap(el, {
      height: baseH,
      tileUrl: cfg.url,
      gcj: cfg.gcj,
      interactive: true,
      showVertices: false,
      showEndpoints: false,
      emptyHint: false,          // 新路书还没画线时别显示「该活动没有 GPS 轨迹」
      onPinMoved: (lat, lon) => updatePendingPin(lat, lon),
      onClick: (lat, lon) => {
        if (state.pending) { updatePendingPin(lat, lon); return; }
        if (state.generated) { UI.toast('已经生成路书了，要改点「继续调整」', 'err'); return; }
        addWaypoint(lat, lon, { autoName: true });
      },
      onWaypointMove: (i, lat, lon) => {
        if (state.generated) { UI.toast('已经生成路书了，要改点「继续调整」', 'err'); return; }
        const wp = state.waypoints[i];
        if (!wp) return;
        pushUndo();
        wp.lat = lat;
        wp.lon = lon;
        renderWpList();
        resnap(false);
      },
    });
    if (state.track && state.track.length) {
      state.map.setRoute(state.track, { fit: !!fit });
      state.map.setWaypoints(state.waypoints);
    } else if (state.waypoints.length) {
      state.map.setWaypoints(state.waypoints);
      state.map.fitPoints(state.waypoints.map(w => [w.lat, w.lon]));
    } else {
      const [lat, lon] = lastCenter();
      state.map.setView(lat, lon, 13);
    }
    if (state.kmMarks) state.map.setKmMarks(true);
    el.__map = state.map;          // 调试/自动化用：拿到地图实例算锚点坐标
  }

  function lastCenter() {
    try {
      const lat = Number(localStorage.getItem('pace.rb.centerLat') || 0);
      const lon = Number(localStorage.getItem('pace.rb.centerLon') || 0);
      if (lat && lon) return [lat, lon];
    } catch (e) { /* ignore */ }
    return DEFAULT_CENTER;
  }

  function addWaypoint(lat, lon, opts) {
    const o = opts || {};
    // 同一位置点两下：别加重复点（重复点会规划出一圈回头路）
    const last = state.waypoints[state.waypoints.length - 1];
    if (last && haversine([last.lat, last.lon], [lat, lon]) < 25 && !o.silent) {
      UI.toast('这个位置已经有点了', 'err');
      return;
    }
    if (!o.silent) pushUndo();
    state.waypoints.push({
      lat: Number(lat.toFixed(6)),
      lon: Number(lon.toFixed(6)),
      name: o.name || '',
      tag: '',
      note: '',
      show: true,
    });
    const idx = state.waypoints.length - 1;
    hideHint();
    renderWpList();
    resnap(true);
    saveDraft();
    if (o.center && state.map) focusPlace({ lat, lon });
    if (o.autoName && !o.name) {
      // 和行者一样：点一个地方，自动把那片的地名填进去（高德优先，没 Key 用 Photon 中文名）
      Routing.reverse(lat, lon).then(name => {
        const wp = state.waypoints[idx];
        if (!name || !wp || wp.name) return;
        wp.name = name;
        renderWpList();
      });
    }
  }

  function renderWpList() {
    const box = document.getElementById('rbWpList');
    if (!box) return;
    const n = state.waypoints.length;
    if (!n) {
      const hasTrack = (state.track || []).length > 1;
      box.innerHTML = `<div class="rb-wp-empty dim">${hasTrack
        ? '这条路书是导入或活动生成的，没有途经点。点地图加点会按途经点重新生成整条路线；只改名字的话直接保存就行。'
        : '还没有途经点。在地图上点一下加起点，再点几下加途经点，最后一个点就是终点。'}</div>`;
      updateHud();
      return;
    }
    box.innerHTML = state.waypoints.map((w, i) => {
      const kind = wpKind(i, n);
      const d = wpDistance(w);
      return `<div class="rb-wp" data-i="${i}">
        <button class="rb-wp-idx ${kind}" data-act="center" title="在地图上定位这个点">${wpLabel(i, n)}</button>
        <div class="rb-wp-main">
          <input class="rb-wp-name" data-field="name" value="${U.esc(w.name || '')}"
                 placeholder="${kind === 'start' ? '起点名称' : (kind === 'end' ? '终点名称' : '途经点 ' + (i + 1))}"
                 maxlength="60">
          <div class="rb-wp-meta">
            <select class="rb-wp-tag" data-field="tag">
              <option value="">功能标签</option>
              ${TAGS.map(t => `<option value="${t}"${w.tag === t ? ' selected' : ''}>${t}</option>`).join('')}
            </select>
            <input class="rb-wp-note" data-field="note" value="${U.esc(w.note || '')}"
                   placeholder="备注：补给、路况、集合点…" maxlength="300">
            <span class="dim rb-wp-d">${d != null ? U.km(d, 1) + ' km' : ''}</span>
          </div>
        </div>
        <div class="rb-wp-move">
          <button class="icon-btn rb-wp-btn" data-act="up" title="往前移"${i === 0 ? ' disabled' : ''}>↑</button>
          <button class="icon-btn rb-wp-btn" data-act="down" title="往后移"${i === n - 1 ? ' disabled' : ''}>↓</button>
        </div>
        <button class="icon-btn rb-wp-btn${w.show ? ' is-on' : ''}" data-act="show"
                title="${w.show ? '在地图上显示' : '已隐藏'}">${Icon.get('pin')}</button>
        <button class="icon-btn rb-wp-btn" data-act="del" title="删除这个途经点">${Icon.get('trash')}</button>
      </div>`;
    }).join('');
    Icon.hydrate(box);
    updateHud();
    syncPlanFromWaypoints();
  }

  /** 途经点被增删/拖动/排序后，「按地点生成」那几行也要跟着变，
      否则再点「生成路书」会把已经删掉的地点又加回来。 */
  function syncPlanFromWaypoints() {
    if (state.mode !== 'edit') return;
    if (!document.getElementById('rbPlanRows')) return;
    const next = planFromWaypoints(state.waypoints);
    const cur = state.plan || [];
    const same = next.length === cur.length && next.every((row, i) => {
      const a = row.place || {};
      const b = (cur[i] && cur[i].place) || {};
      return (a.lat || 0) === (b.lat || 0) && (a.lon || 0) === (b.lon || 0)
        && (row.q || '') === ((cur[i] && cur[i].q) || '');
    });
    if (same) return;
    state.plan = next;
    renderPlan();
  }

  function bindWpEvents(box) {
    box.addEventListener('input', (e) => {
      const field = e.target.dataset.field;
      if (!field) return;
      const row = e.target.closest('.rb-wp');
      const i = Number(row.dataset.i);
      if (!state.waypoints[i]) return;
      state.waypoints[i][field] = e.target.value;
      state.dirty = true;
      saveDraft();
    });
    box.addEventListener('change', (e) => {
      const field = e.target.dataset.field;
      if (field !== 'tag') return;
      const row = e.target.closest('.rb-wp');
      const i = Number(row.dataset.i);
      if (!state.waypoints[i]) return;
      state.waypoints[i].tag = e.target.value;
      state.dirty = true;
    });
    box.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      const row = btn.closest('.rb-wp');
      const i = Number(row.dataset.i);
      const wp = state.waypoints[i];
      if (!wp) return;
      const act = btn.dataset.act;
      if (act === 'center') {
        if (state.map) state.map.setView(wp.lat, wp.lon, 16);
        return;
      }
      if (act === 'up' || act === 'down') {
        const j = act === 'up' ? i - 1 : i + 1;
        if (j < 0 || j >= state.waypoints.length) return;
        pushUndo();
        const tmp = state.waypoints[i];
        state.waypoints[i] = state.waypoints[j];
        state.waypoints[j] = tmp;
        renderWpList();
        resnap(false);
        saveDraft();
        return;
      }
      if (act === 'show') {
        wp.show = !wp.show;
        btn.classList.toggle('is-on', wp.show);
        btn.title = wp.show ? '在地图上显示' : '已隐藏';
        if (state.map) state.map.setWaypoints(state.waypoints);
        state.dirty = true;
      } else if (act === 'del') {
        pushUndo();
        state.waypoints.splice(i, 1);
        renderWpList();
        resnap(false);
        saveDraft();
      }
    });
  }

  function updateHud() {
    const stat = document.getElementById('rbStat');
    if (stat) {
      const km = distanceOf(state.track || []) / 1000;
      stat.textContent = `${state.waypoints.length} 个途经点 · ${km.toFixed(2)} km`;
    }
    const snap = document.getElementById('rbSnap');
    if (snap) snap.textContent = state.snapMsg || '';
    const save = document.getElementById('rbSave');
    if (save) save.disabled = (state.track || []).length < 2;
  }

  function paintTrack(fit) {
    if (!state.map) return;
    const track = state.track || [];
    // 路线重画后，搜索时钉的那个临时点就没意义了
    state.map.setFocusPin(null);
    // 只有一个点时别 fit：零范围的框会把地图顶到最大级，后面几个点就挤在一小块里
    state.map.setRoute(track, { fit: !!fit && track.length >= 2 });
    state.map.setWaypoints(state.waypoints);
    const d = distanceOf(track);
    if (state.track && state.track.length > 1) {
      try {
        localStorage.setItem('pace.rb.centerLat', String(state.track[0][0]));
        localStorage.setItem('pace.rb.centerLon', String(state.track[0][1]));
      } catch (e) { /* ignore */ }
    }
    updateHud();
    scheduleElevation();
    return d;
  }

  function resnap(fit) {
    const p = doResnap(fit).catch(() => {});
    state.snapPromise = p;
    return p;
  }

  async function doResnap(fit) {
    const seq = ++state.snapSeq;
    state.elevGain = null;          // 路线要变了，旧的爬升数不能跟着保存
    state.elevSeq += 1;             // 让在途的海拔请求作废，避免贴到新轨迹上
    const wps = state.waypoints;
    if (!wps.length) {
      state.track = [];
      state.snapMsg = '';
      paintTrack(!!fit);
      return;
    }
    if (wps.length < 2) {
      state.track = wps.map(w => [w.lat, w.lon]);
      state.snapMsg = '再点一个点，成为终点';
      paintTrack(!!fit);
      return;
    }
    if (state.routeMode === 'straight') {
      state.track = wps.map(w => [w.lat, w.lon]);
      state.snapMsg = '直线连接（不吸附路网）';
      paintTrack(!!fit);
      return;
    }
    state.snapMsg = '正在按路网吸附…';
    updateHud();
    const res = await Routing.snap(wps, state.routeMode);
    if (seq !== state.snapSeq) return;          // 有更新的请求，丢弃这次结果
    state.track = res.coords;
    const label = state.routeMode === 'walk' ? '徒步' : '骑行';
    const detour = (res.detours && res.detours.length)
      ? ` · 已避开 ${res.detours.length} 段绕行` : '';
    const via = res.provider === 'osrm' ? ' · OSRM 兜底（驾车路网）' : '';
    const badLegs = (res.detourLegs || []);
    const warn = badLegs.length
      ? ` · 第 ${badLegs.map(x => x.leg).join('、')} 段绕行偏多`
      : '';
    state.snapMsg = res.ok
      ? `已按道路吸附（${label}${via}${detour}${warn}）`
      : `直线连接 · ${res.reason}`;
    if (res.ok && badLegs.length) {
      const d = badLegs[0];
      UI.toast(`第 ${d.leg} 段绕得比较多：直线 ${d.directKm} km、实际 ${d.km} km。`
        + '多半是锚点落在院子/单行线里，拖到路口再试', 'err', 7000);
    }
    paintTrack(!!fit);
  }

  /* 草稿：行者关掉页面会提醒继续；这里用 localStorage，避免白画。
     只给「新建」存草稿：编辑已有路书存草稿，下次点「制作路书」会变成复制一份，容易出事 */
  function saveDraft() {
    if (!state.waypoints.length) return;
    if (state.book && state.book.id) return;
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({
        name: state.name,
        waypoints: state.waypoints,
        routeMode: state.routeMode,
        ts: Date.now(),
      }));
    } catch (e) { /* ignore */ }
  }

  function readDraft() {
    try {
      const raw = localStorage.getItem(DRAFT_KEY);
      if (!raw) return null;
      const d = JSON.parse(raw);
      if (!d || !Array.isArray(d.waypoints) || !d.waypoints.length) return null;
      return d;
    } catch (e) {
      return null;
    }
  }

  function clearDraft() {
    try { localStorage.removeItem(DRAFT_KEY); } catch (e) { /* ignore */ }
  }

  async function openNew() {
    cleanup();
    state.book = null;
    state.routeInfo = null;
    state.undo = [];
    state.redo = [];
    state.track = [];
    state.kmMarks = false;
    state.routeMode = 'road';
    const draft = readDraft();
    if (draft) {
      const ago = Math.round((Date.now() - (draft.ts || 0)) / 60000);
      const ok = await UI.confirmDialog(
        '继续上次制作？',
        `上次还有一条没保存的路书（${draft.waypoints.length} 个途经点，${ago < 1 ? '刚刚' : ago + ' 分钟前'}）。`,
        '继续', true);
      if (ok) {
        state.name = draft.name || '未命名路书';
        state.waypoints = draft.waypoints;
        state.routeMode = draft.routeMode || 'road';
        renderEditor();
        resnap(true);
        return;
      }
      clearDraft();
    }
    state.name = '未命名路书';
    state.waypoints = [];
    renderEditor({ fit: false });
  }

  function hideHint() {
    const hint = document.getElementById('rbHint');
    if (hint) hint.classList.add('fade');
  }

  async function save() {
    if (state.saving) return;
    if (state.snapPromise) {
      // 刚点完点就按保存时，吸附还在路上：等它落地再判断能不能存
      try { await state.snapPromise; } catch (e) { /* ignore */ }
    }
    if ((state.track || []).length < 2) { UI.toast('至少两个途经点才能保存', 'err'); return; }
    state.saving = true;
    const btn = document.getElementById('rbSave');
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    try {
      // 活动路书没改点的话，海拔/爬升以原活动的为准，别用 Valhalla 重新算一遍把 315 改成别的
      const activityUntouched = state.book && state.book.source === 'activity'
        && state.book.point_count === (state.track || []).length;
      // 刚拖过点、海拔还没算完的话先等它：否则存下来的轨迹没带高度，详情页就没有剖面
      if (!activityUntouched && (state.track || []).length >= 2 && state.elevGain == null) {
        clearTimeout(state.elevDebounce);
        await updateElevation();
      }
      const payload = {
        name: (state.name || '').trim() || '未命名路书',
        points: state.track,
        waypoints: state.waypoints,
        distance_m: distanceOf(state.track),
      };
      if (!activityUntouched && state.elevGain != null) payload.climb_m = state.elevGain;
      let book;
      if (state.book && state.book.id) {
        book = await api.updateRoutebook(state.book.id, payload);
      } else {
        book = await api.createRoutebook(payload);
      }
      clearDraft();
      state.book = book;
      state.routeInfo = book.routebook || null;
      state.dirty = false;
      // 列表卡片读的是 climb_m，详情页读的是现场算的 climb_gain_total，对不上就对一次
      const total = book.routebook && book.routebook.climb_gain_total;
      if (total != null && Math.abs(Number(book.climb_m || 0) - Number(total)) > 1) {
        try {
          await api.updateRoutebook(book.id, { climb_m: Number(total) });
          book.climb_m = Number(total);
        } catch (e) { /* 不影响主流程 */ }
      }
      UI.toast('已保存', 'ok');
      if (!location.hash.endsWith(`/routebook/${book.id}`)) {
        location.hash = `#/routebook/${book.id}`;
      } else {
        load(host, { id: book.id });
      }
    } catch (err) {
      UI.toast('保存失败：' + U.esc(err.message), 'err');
    } finally {
      state.saving = false;
      if (btn) { btn.disabled = false; btn.textContent = '保存'; }
    }
  }

  /* ------------------------------------------------------------ 入口 */

  async function load(root, params) {
    cleanup();
    host = root;
    state.mapCfg = (window.PaceApp && window.PaceApp.mapConfig) || { url: '', gcj: true };
    const id = params && params.id ? Number(params.id) : 0;
    root.innerHTML = UI.spinner(id ? '读取路书…' : '读取路书列表…');
    try {
      if (id > 0) {
        const book = await api.routebook(id);
        state.book = book;
        state.track = (book.points || []).slice();
        state.waypoints = (book.waypoints || []).map(w => ({
          lat: w.lat, lon: w.lon,
          name: w.name || '', tag: w.tag || '', note: w.note || '', show: w.show !== false,
        }));
        state.name = book.name;
        state.routeInfo = book.routebook || null;
        state.mode = 'view';
        renderView();
      } else {
        const res = await api.routebooks();
        state.books = res.routebooks || [];
        state.book = null;
        state.routeInfo = null;
        state.mode = 'list';
        renderList();
      }
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty('info', '加载失败', U.esc(err.message),
        `<a class="btn" href="#/routebook">返回路书列表</a>`)}</div>`;
    }
  }

  window.PageRoutebook = { load, ingestGpxText };

  // Ctrl+Z / Ctrl+Shift+Z 撤销重做（在输入框里打字时不抢）
  document.addEventListener('keydown', (e) => {
    if (state.mode !== 'edit') return;
    if (!(e.ctrlKey || e.metaKey)) return;
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    const key = (e.key || '').toLowerCase();
    if (key === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); }
    else if (key === 'y') { e.preventDefault(); redo(); }
  });

  // Esc 退出地图全屏（只绑一次）
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const card = document.getElementById('rbMapCard');
    if (!card || !card.classList.contains('is-fullscreen')) return;
    card.classList.remove('is-fullscreen');
    document.body.classList.remove('rb-locked');
    const full = document.getElementById('rbFull');
    if (full) full.textContent = '全屏';
    if (state.map) state.map.setHeight(state.mapBaseHeight || 560);
  });
  // 离开路书页时收拾干净（全屏会把 body 锁住，地图实例也要销毁）
  window.addEventListener('hashchange', () => {
    if (location.hash.indexOf('#/routebook') === 0) return;
    cleanup();
    state.mode = 'list';
  });

  // 点空白处收起「按地点生成」的下拉结果
  document.addEventListener('click', (e) => {
    if (state.mode !== 'edit') return;
    if (e.target.closest && e.target.closest('.rb-plan-row')) return;
    U.$$('.rb-plan-results').forEach(box => {
      if (!box.hidden) { box.hidden = true; box.innerHTML = ''; }
    });
  });
})();
