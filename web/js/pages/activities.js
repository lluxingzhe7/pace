/* 运动数据：按跑步 / 骑行 / 游泳分类浏览、筛选、搜索、排序、多选对比。 */
(function () {
  const SPORTS = [
    { key: 'all', label: '全部', icon: 'list' },
    { key: 'ride', label: '骑行', icon: 'ride' },
    { key: 'run', label: '跑步', icon: 'run' },
    { key: 'swim', label: '游泳', icon: 'swim' },
  ];
  const SPORT_LABEL = { all: '全部', ride: '骑行', run: '跑步', swim: '游泳' };

  const state = {
    limit: 30,
    offset: 0,
    sport: 'all',
    q: '',
    sort: 'date',
    from: null,
    to: null,
    items: [],
    total: 0,
    stats: null,
    families: null,
    selected: new Set(),
    data: null,
  };

  const FAMILY_COLOR = { ride: 'var(--ride)', run: 'var(--run)', swim: 'var(--swim)', walk: 'var(--walk)', other: 'var(--accent)' };

  /** 卡片封面：真实底图 + 轨迹。底图没网时自动退回深色底 + 轨迹。 */
  function routeThumb(a, fam) {
    if (!a.route_preview || a.route_preview.length < 2) {
      return `<div class="act-thumb empty-thumb">${Icon.family(fam)}<span>无轨迹</span></div>`;
    }
    return `<div class="act-thumb map">
      <canvas class="act-thumb-canvas" data-id="${a.id}"></canvas>
      <span class="act-thumb-badge">${Icon.family(fam)}</span>
    </div>`;
  }

  /** 卡片插入 DOM 后再画：canvas 需要有实际尺寸 */
  function paintThumbs(root) {
    const cfg = (window.PaceApp && window.PaceApp.mapConfig) || { url: '', gcj: false };
    const url = cfg.url || '';
    U.$$('.act-thumb-canvas', root).forEach(cv => {
      const a = state.items.find(x => String(x.id) === String(cv.dataset.id));
      if (!a) return;
      const fam = U.family(a.sport);
      MapThumb.render(cv, a.route_preview, {
        tileUrl: url,
        gcj: !!cfg.gcj,
        color: FAMILY_COLOR[fam] || '#fc4c02',
      });
    });
  }

  function cardHtml(a) {
    const fam = U.family(a.sport);
    const isRide = fam === 'ride' || fam === 'other';
    const paceText = isRide
      ? (a.avg_speed_kmh ? a.avg_speed_kmh.toFixed(1) : '—')
      : (a.pace_s_per_km ? U.pace(a.pace_s_per_km) : '—');
    const paceUnit = fam === 'ride' || fam === 'other' ? 'km/h' : fam === 'swim' ? '/100m' : '/km';
    const picked = state.selected.has(a.id);
    const speedUnit = isRide ? 'km/h' : fam === 'swim' ? '/100m' : '/km';
    return `
      <div class="act-card${picked ? ' picked' : ''}" data-id="${a.id}" tabindex="0" role="link"
           aria-label="${U.esc(a.title || '活动')}">
        <div class="act-card-top">
          ${routeThumb(a, fam)}
          <input type="checkbox" class="act-pick" data-id="${a.id}" ${picked ? 'checked' : ''}
            aria-label="选择用于对比" title="选择用于对比">
        </div>
        <div class="act-card-body">
          <div class="act-card-title">
            <span class="act-icon ${fam}">${Icon.family(fam)}</span>
            <span>${U.esc(a.title || U.sportLabel(a.sport))}</span>
          </div>
          <div class="act-card-meta">
            ${U.dateLabel(a.start_time)} · ${U.esc(U.sportLabel(a.sport))}
            ${a.has_gps ? '' : ' · 无轨迹'}
          </div>
          <div class="act-card-stats">
            <div><b>${U.km(a.distance_m)}</b><span>km</span></div>
            <div><b>${U.dur(a.moving_seconds)}</b><span>时长</span></div>
            <div><b>${paceText}</b><span>${speedUnit}</span></div>
          </div>
          <div class="act-card-foot">
            <span>↑ ${U.elev(a.elevation_gain_m)} m</span>
            <span>负荷 ${U.num(a.training_load || 0)}</span>
            ${a.avg_hr ? `<span class="data-tag">♥ ${Math.round(a.avg_hr)}</span>` : ''}
            ${a.avg_power ? `<span class="data-tag">⚡ ${Math.round(a.avg_power)}W</span>` : ''}
            ${(!a.avg_hr && !a.avg_power) ? '<span class="no-data-tag">无心率/功率</span>' : ''}
          </div>
        </div>
      </div>`;
  }

  function tabsHtml() {
    const fam = state.families || {};
    const allCount = Object.values(fam).reduce((a, b) => a + b, 0);
    return `<div class="sport-tabs">` + SPORTS.map(s => {
      const href = '#/activities' + (s.key === 'all' ? '' : `?sport=${s.key}`);
      const count = s.key === 'all' ? allCount : (fam[s.key] || 0);
      return `<a href="${href}" class="${state.sport === s.key ? 'active' : ''}">
        ${Icon.get(s.icon)}<span>${s.label}</span>${count ? `<span class="count">${count}</span>` : ''}
      </a>`;
    }).join('') + `</div>`;
  }

  function heroHtml() {
    const st = state.stats || {};
    const label = SPORT_LABEL[state.sport] || '全部';
    return `${state.dayLabel ? `<div class="row" style="gap:8px;margin-bottom:10px;align-items:center">
        <span class="chip chip-accent">${U.esc(state.dayLabel)} 这一天</span>
        <a class="tiny dim" href="#/activities${state.sport !== 'all' ? `?sport=${state.sport}` : ''}">看全部日期</a>
      </div>` : ''}
    <div class="sport-hero">
      <div class="stat"><span>${label}次数</span><b>${state.total}<small>次</small></b></div>
      <div class="stat"><span>累计距离</span><b>${U.km(st.distance_m)}<small>km</small></b></div>
      <div class="stat"><span>累计时间</span><b>${U.dur(st.moving_seconds)}</b></div>
      <div class="stat"><span>累计爬升</span><b>${U.elev(st.elevation_gain_m)}<small>m</small></b></div>
      <div class="stat"><span>累计负荷</span><b>${U.num(st.training_load)}</b></div>
    </div>`;
  }

  function toolbarHtml() {
    return `
      <div class="toolbar">
        <div class="search">
          ${Icon.get('search')}
          <input id="actSearch" type="search" placeholder="搜索标题…" value="${U.esc(state.q)}" aria-label="搜索活动">
        </div>
        <select class="control" id="actSort" aria-label="排序">
          <option value="date">最新优先</option>
          <option value="distance">距离最长</option>
          <option value="duration">时间最长</option>
          <option value="elevation">爬升最多</option>
          <option value="load">负荷最高</option>
        </select>
        <select class="control" id="actRange" aria-label="时间范围">
          <option value="all">全部时间</option>
          <option value="30">近 30 天</option>
          <option value="90">近 90 天</option>
          <option value="365">近一年</option>
        </select>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btnCompare" disabled>${Icon.get('compare')}对比所选</button>
      </div>`;
  }

  function render(root) {
    const list = state.items;
    const pages = Math.max(1, Math.ceil(state.total / state.limit));
    const page = Math.floor(state.offset / state.limit) + 1;

    root.innerHTML = `
      <div class="section-head" style="margin-top:0">
        <h2>运动数据</h2>
        <span class="dim">${SPORT_LABEL[state.sport] || '全部'} · 共 ${state.total} 条</span>
      </div>
      ${tabsHtml()}
      ${heroHtml()}
      ${toolbarHtml()}
      ${list.length ? `
        <div class="act-grid">${list.map(cardHtml).join('')}</div>
      ` : `<div class="card">${UI.empty(SPORTS.find(s => s.key === state.sport)?.icon || 'list',
            state.sport === 'all' ? '没有符合条件的活动' : `还没有${SPORT_LABEL[state.sport]}数据`,
            state.sport === 'all' ? '换个筛选条件，或者导入新的活动文件。'
              : `导入${SPORT_LABEL[state.sport]}的 GPX / FIT / TCX 文件后会显示在这里。`,
            `<button class="btn btn-primary" onclick="UI.Importer.open()">导入活动</button>`)}</div>`}
      ${pages > 1 ? `
        <div class="pager">
          <button class="btn btn-sm" id="prevPage" ${state.offset === 0 ? 'disabled' : ''}>${Icon.get('back')}上一页</button>
          <span class="dim">第 ${page} / ${pages} 页</span>
          <button class="btn btn-sm" id="nextPage" ${state.offset + state.limit >= state.total ? 'disabled' : ''}>下一页${Icon.get('chevron')}</button>
        </div>` : ''}`;

    Icon.hydrate(root);
    requestAnimationFrame(() => paintThumbs(root));

    // 卡片点击进详情；勾选框只做多选
    U.$$('.act-card', root).forEach(row => {
      const go = () => { location.hash = `#/activity/${row.dataset.id}`; };
      row.addEventListener('click', (e) => {
        if (e.target.closest('.act-pick')) return;
        go();
      });
      row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
      });
    });
    U.$$('.act-pick', root).forEach(box => {
      box.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = Number(box.dataset.id);
        if (box.checked) state.selected.add(id);
        else state.selected.delete(id);
        box.closest('.act-card').classList.toggle('picked', box.checked);
        updateCompareButton();
      });
    });
    updateCompareButton();

    const search = document.getElementById('actSearch');
    if (search) {
      const onSearch = U.debounce(() => {
        state.q = search.value.trim();
        state.offset = 0;
        window.Router.rerender();
      }, 260);
      search.addEventListener('input', onSearch);
    }
    const sort = document.getElementById('actSort');
    sort.value = state.sort;
    sort.addEventListener('change', () => {
      state.sort = sort.value; state.offset = 0; window.Router.rerender();
    });
    const range = document.getElementById('actRange');
    range.addEventListener('change', () => {
      const days = range.value;
      if (days === 'all') { state.from = null; state.to = null; }
      else {
        const now = Date.now() / 1000;
        state.from = now - Number(days) * 86400;
        state.to = null;
      }
      state.offset = 0;
      window.Router.rerender();
    });

    const prev = document.getElementById('prevPage');
    const next = document.getElementById('nextPage');
    if (prev) prev.addEventListener('click', () => { state.offset = Math.max(0, state.offset - state.limit); window.Router.rerender(); });
    if (next) next.addEventListener('click', () => { state.offset += state.limit; window.Router.rerender(); });
  }

function updateCompareButton() {
  const btn = document.getElementById('btnCompare');
  if (!btn) return;
  const n = state.selected.size;
  btn.disabled = n < 1;
  btn.innerHTML = `${Icon.get('compare')}${n ? `对比所选 (${n})` : '对比所选'}`;
}

async function load(root, params) {
    if (params && params.sport) state.sport = params.sport;
    else if (!params || !params.day) state.sport = 'all';
    if (params && params.day && /^\d{4}-\d{2}-\d{2}$/.test(params.day)) {
      const [y, m, d] = params.day.split('-').map(Number);
      const start = new Date(y, m - 1, d, 0, 0, 0).getTime() / 1000;
      state.from = start;
      state.to = start + 86400;
      state.dayLabel = params.day;
    } else {
      state.from = undefined;
      state.to = undefined;
      state.dayLabel = null;
    }
    root.innerHTML = `<div class="section-head" style="margin-top:0"><h2>运动数据</h2></div>
      ${UI.spinner('读取中…')}`;
    try {
      const res = await api.activities({
        limit: state.limit,
        offset: state.offset,
        sport: state.sport !== 'all' ? state.sport : undefined,
        q: state.q || undefined,
        sort: state.sort,
        from: state.from,
        to: state.to,
      });
      state.items = res.items;
      state.total = res.total;
      state.stats = res.stats || null;
      state.families = res.families || null;
      render(root);
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty('info', '加载失败', U.esc(err.message))}</div>`;
    }
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('#btnCompare');
    if (!btn || state.selected.size < 1) return;
    location.hash = `#/compare?ids=${Array.from(state.selected).join(',')}`;
  });

  function setSport(sport) {
    state.sport = sport;
    state.offset = 0;
  }

  window.PageActivities = { load, setSport, get state() { return state; } };
})();
