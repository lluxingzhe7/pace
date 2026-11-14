/* 活动对比：多选活动，叠加同一距离轴上的配速/心率/海拔曲线。 */
(function () {
  const state = { ids: [], data: null, charts: [] };
  let host = null;

  const COLORS = ['var(--accent)', 'var(--ride)', 'var(--good)', 'var(--walk)', 'var(--hr4)', 'var(--hr5)'];

  function cleanup() {
    state.charts.forEach(c => c && c.destroy());
    state.charts = [];
  }

  function renderPicker(root) {
    root.innerHTML = `
      <div class="section-head" style="margin-top:0">
        <h2>对比活动</h2>
        <span class="dim">最多 6 条，按距离对齐</span>
      </div>
      ${UI.spinner('读取活动列表…')}`;
    api.activities({ limit: 200, sort: 'date' }).then(res => {
      const acts = res.items;
      if (!acts.length) {
        root.innerHTML = `<div class="card">${UI.empty('compare', '没有可对比的活动',
          '对比需要带 GPS 轨迹的活动。', `<button class="btn btn-primary" onclick="UI.Importer.open()">导入活动</button>`)}</div>`;
        return;
      }
      root.innerHTML = `
        <div class="section-head" style="margin-top:0">
          <h2>对比活动</h2>
          <span class="dim">最多 6 条，按距离对齐</span>
          <div class="spacer"></div>
          <button class="btn btn-primary" id="btnRunCompare" disabled>${Icon.get('compare')}开始对比</button>
        </div>
        <div class="grid" style="grid-template-columns:minmax(0,380px) minmax(0,1fr);align-items:start">
          <div class="card">
            <div class="card-head"><h3>选择活动</h3><span class="dim">已选 <b id="selCount">0</b></span></div>
            <div class="card-body">
              <div class="search" style="max-width:none;margin-bottom:10px">
                ${Icon.get('search')}
                <input id="cmpSearch" type="search" placeholder="搜索…" aria-label="搜索活动">
              </div>
              <div class="compare-picker" id="cmpList">
                ${acts.map(a => `
                  <label class="pick-row" data-id="${a.id}" data-label="${U.esc((a.title || '') + ' ' + U.sportLabel(a.sport))}">
                    <input type="checkbox" value="${a.id}">
                    <div class="act-icon ${U.family(a.sport)}" style="width:30px;height:30px">${Icon.family(U.family(a.sport))}</div>
                    <div style="min-width:0;flex:1">
                      <div style="font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${U.esc(a.title || U.sportLabel(a.sport))}</div>
                      <div class="tiny dim">${U.dateShort(a.start_time)} · ${U.km(a.distance_m)} km · ${U.dur(a.moving_seconds)}${a.has_gps ? '' : ' · 仅汇总'}</div>
                    </div>
                  </label>`).join('')}
              </div>
            </div>
          </div>
          <div class="card card-pad" id="cmpPreview">
            ${UI.empty('compare', '选择至少两条活动', '勾选后点击"开始对比"，可以看到配速、心率随距离的变化差异。')}
          </div>
        </div>`;
      Icon.hydrate(root);

      const runBtn = document.getElementById('btnRunCompare');
      const countEl = document.getElementById('selCount');
      const update = () => {
        const n = state.ids.length;
        countEl.textContent = n;
        runBtn.disabled = n < 2;
        U.$$('.pick-row', root).forEach(row => {
          row.classList.toggle('sel', state.ids.includes(Number(row.dataset.id)));
        });
      };
      U.$$('.pick-row input', root).forEach(box => {
        box.addEventListener('change', () => {
          const id = Number(box.value);
          if (box.checked) {
            if (state.ids.length >= 6) { box.checked = false; UI.toast('最多对比 6 条活动', 'warn'); return; }
            state.ids.push(id);
          } else {
            state.ids = state.ids.filter(x => x !== id);
          }
          update();
        });
      });
      const search = document.getElementById('cmpSearch');
      search.addEventListener('input', () => {
        const q = search.value.trim().toLowerCase();
        U.$$('.pick-row', root).forEach(row => {
          row.style.display = row.dataset.label.toLowerCase().includes(q) ? '' : 'none';
        });
      });
      runBtn.addEventListener('click', () => runCompare(root));
      update();
    }).catch(err => {
      root.innerHTML = `<div class="card">${UI.empty('info', '加载失败', U.esc(err.message))}</div>`;
    });
  }


  /* 对比表：以指标为行、活动为列，比一行一条活动更好读 */
  function fmtSpeed(a, fam) {
    if (fam === 'run' || fam === 'walk') return U.pace(a.pace_s_per_km) + '/km';
    if (fam === 'swim') return U.pace(a.pace_s_per_km) + '/100m';
    return (a.avg_speed_kmh || 0).toFixed(1) + ' km/h';
  }

  function metricsCard(acts) {
    const rows = [
      { label: '距离', get: a => U.km(a.distance_m) + ' km' },
      { label: '移动时间', get: a => U.dur(a.moving_seconds) },
      { label: '总时间', get: a => U.dur(a.elapsed_seconds) },
      { label: '平均速度', get: a => fmtSpeed(a, U.family(a.sport)), best: 'speed' },
      { label: '平均心率', get: a => a.avg_hr ? Math.round(a.avg_hr) + ' bpm' : '—', best: 'hr' },
      { label: '最大心率', get: a => a.max_hr ? Math.round(a.max_hr) + ' bpm' : '—' },
      { label: '爬升', get: a => U.elev(a.elevation_gain_m) + ' m' },
      { label: '每公里爬升', get: a => a.distance_m ? (a.elevation_gain_m / (a.distance_m / 1000)).toFixed(1) + ' m/km' : '—' },
      { label: '平均功率', get: a => a.avg_power ? Math.round(a.avg_power) + ' W' : '—' },
      { label: '标准化功率 NP', get: a => a.np_power ? Math.round(a.np_power) + ' W' : '—' },
      { label: '效率（NP / 心率）', get: a => (a.np_power && a.avg_hr) ? (a.np_power / a.avg_hr).toFixed(2) : '—' },
      { label: '训练负荷', get: a => U.num(a.training_load) },
      { label: '移动时间占比', get: a => a.elapsed_seconds ? Math.round(a.moving_seconds / a.elapsed_seconds * 100) + '%' : '—' },
    ];
    // 每项找「最好」的那条：速度取最快、心率取最低（同等强度下更省）
    const bestIdx = {};
    rows.forEach((r, ri) => {
      if (!r.best) return;
      let best = -1, bestVal = null;
      acts.forEach((a, i) => {
        let v = null;
        if (r.best === 'speed') v = a.pace_s_per_km || (a.avg_speed_kmh ? 3600 / a.avg_speed_kmh : null);
        if (r.best === 'hr') v = a.avg_hr;
        if (v == null) return;
        if (best < 0 || (r.best === 'speed' ? v < bestVal : v < bestVal)) { best = i; bestVal = v; }
      });
      bestIdx[ri] = best;
    });
    const head = acts.map((a, i) => `<th style="min-width:120px">
      <span class="row" style="gap:6px"><i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${COLORS[i % COLORS.length]}"></i>
      ${U.esc(a.title || U.sportLabel(a.sport))}</span>
      <div class="tiny dim">${U.dateLabel(a.start_time)}</div></th>`).join('');
    const body = rows.map((r, ri) => `<tr>
      <td class="dim" style="white-space:nowrap">${r.label}</td>
      ${acts.map((a, i) => `<td class="mono${bestIdx[ri] === i ? ' is-best' : ''}">${r.get(a)}</td>`).join('')}
    </tr>`).join('');
    return `
      <div class="card section">
        <div class="card-head"><h3>指标对比</h3><span class="dim">加粗的是这一项里表现最好的</span></div>
        <div style="overflow-x:auto">
          <table class="data-table cmp-table">
            <thead><tr><th>指标</th>${head}</tr></thead>
            <tbody>${body}</tbody>
          </table>
        </div>
      </div>`;
  }

  /* 按 5 公里切分，做同距离段的横向对比 */
  function splitsCard(series) {
    const SPLIT = 5000;
    const usable = series.filter(s => (s.series.d || []).length > 2 && (s.series.t || []).length > 2);
    if (usable.length < 2) return '';
    const maxTotal = Math.max(...usable.map(s => (s.series.d || []).slice(-1)[0] || 0));
    const maxSplits = Math.min(12, Math.ceil(maxTotal / SPLIT));
    const atDistance = (s, d) => {
      const ds = s.series.d, ts = s.series.t;
      if (!ds.length) return null;
      if (d > ds[ds.length - 1] + 1) return null;
      let lo = 0, hi = ds.length - 1;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (ds[mid] < d) lo = mid + 1; else hi = mid; }
      const i = lo;
      if (i === 0) return ts[0];
      const span = ds[i] - ds[i - 1] || 1;
      return ts[i - 1] + (ts[i] - ts[i - 1]) * ((d - ds[i - 1]) / span);
    };
    const rows = [];
    for (let k = 1; k <= maxSplits; k++) {
      const end = k * SPLIT;
      const cells = usable.map(s => {
        const t0 = atDistance(s, end - SPLIT);
        const t1 = atDistance(s, end);
        if (t0 == null || t1 == null || t1 <= t0) return null;
        return { sec: t1 - t0, speed: (SPLIT / (t1 - t0)) * 3.6 };
      });
      if (cells.filter(Boolean).length < 2) continue;
      const fastest = Math.min(...cells.filter(Boolean).map(c => c.sec));
      rows.push(`<tr>
        <td class="dim" style="white-space:nowrap">第 ${k} 个 5 km</td>
        ${cells.map(c => c
          ? `<td class="mono${Math.abs(c.sec - fastest) < 0.5 ? ' is-best' : ''}">${U.dur(c.sec)}
             <span class="tiny dim">${c.speed.toFixed(1)} km/h</span></td>`
          : '<td class="dim">—</td>').join('')}
      </tr>`);
    }
    if (!rows.length) return '';
    return `
      <div class="card section">
        <div class="card-head"><h3>同距离分段对比</h3>
          <span class="dim">每 5 公里用时，加粗 = 这一段落最快</span></div>
        <div style="overflow-x:auto">
          <table class="data-table cmp-table">
            <thead><tr><th>分段</th>
              ${usable.map((s, i) => `<th><span class="row" style="gap:6px">
                <i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${COLORS[i % COLORS.length]}"></i>
                ${U.esc(actLabel(s))}</span></th>`).join('')}
            </tr></thead>
            <tbody>${rows.join('')}</tbody>
          </table>
        </div>
      </div>`;
  }

  function actLabel(a) {
    const base = a.title || U.sportLabel(a.sport);
    const d = U.dateLabel(a.start_time || '');
    return d && d !== '—' ? `${base} · ${d}` : base;
  }

  function renderCompare(root, data) {
    const acts = data.activities || [];
    const series = data.series || [];
    cleanup();
    root.innerHTML = `
      <div class="section-head" style="margin-top:0">
        <h2>对比结果</h2>
        <span class="dim">${acts.length} 条活动</span>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btnBackPick">${Icon.get('back')}重新选择</button>
      </div>

      <div class="card">
        <div class="card-head"><h3>配速 / 速度</h3>
          <span class="dim" id="cmpPaceNote"></span>
          <div class="spacer"></div>
          <div class="compare-legend">
            ${acts.map((a, i) => `<span><i style="background:${COLORS[i % COLORS.length]}"></i>${U.esc(actLabel(a))}</span>`).join('')}
          </div>
        </div>
        <div class="card-body"><div class="chart-holder"><canvas id="cmpPace"></canvas></div></div>
      </div>

      <div class="grid g-2 section">
        <div class="card">
          <div class="card-head"><h3>心率</h3></div>
          <div class="card-body"><div class="chart-holder"><canvas id="cmpHr"></canvas></div></div>
        </div>
        <div class="card">
          <div class="card-head"><h3>海拔</h3></div>
          <div class="card-body"><div class="chart-holder"><canvas id="cmpAlt"></canvas></div></div>
        </div>
      </div>

      ${metricsCard(acts)}
      ${splitsCard(series)}`;

    Icon.hydrate(root);
    document.getElementById('btnBackPick').addEventListener('click', () => renderPicker(root));

    const maxDist = Math.max(...series.map(s => (s.series.d || [0]).slice(-1)[0] || 0), 1) / 1000;

    const common = {
      type: 'line', height: 230,
      xMin: 0, xMax: maxDist,
      padding: { top: 10, right: 12, bottom: 22, left: 38 },
      tooltip: (i) => {
        const total = Math.max(...series.map(s => (s.series.d || []).length), 2);
        const km = (i / Math.max(1, total - 1)) * maxDist;
        const rows = [];
        series.forEach((s, k) => {
          const vals = s.series;
          if (!vals.d || !vals.d.length) return;
          const idx = nearestIndex(vals.d, km * 1000);
          if (idx < 0) return;
          const pace = (vals.pace || [])[idx];
          const speed = (vals.speed || [])[idx];
          const fam = U.family(s.sport);
          const runLike = fam === 'run' || fam === 'walk' || fam === 'swim';
          const paceTxt = speedMode
            ? (runLike ? (pace ? (3600 / pace).toFixed(1) : '—')
                       : (speed ? speed.toFixed(1) : '—')) + ' km/h'
            : (runLike ? (pace ? U.pace(pace) + '/km' : '—')
                       : (speed ? (3600 / speed).toFixed(0) + ' s/km' : '—'));
          rows.push(`<span style="color:${COLORS[k % COLORS.length]}">■</span> ${U.esc(s.title || '')} ` +
            paceTxt + (hr ? ` · ${Math.round(hr)} bpm` : ''));
        });
        return `<b>${km.toFixed(2)} km</b><br>` + rows.join('<br>');
      },
    };

    const paceSeries = [];
    const hrSeries = [];
    const altSeries = [];
    series.forEach((s, i) => {
      const fam = U.family(s.sport);
      const color = COLORS[i % COLORS.length];
      const hasStream = (s.series.d || []).length > 1;
      // 无逐点数据（CSV 汇总）时给一条空线，图表仍能对齐其他活动
      const data = !hasStream ? []
        : fam === 'ride' || fam === 'other'
          ? (s.series.speed || [])
          : (s.series.pace || (s.series.speed || []).map(v => (v ? 3600 / v : null)));
      paceSeries.push({ key: 'p' + i, label: s.title, data, color, axis: 0 });
      hrSeries.push({ key: 'h' + i, label: s.title, data: s.series.hr || [], color, axis: 0 });
      altSeries.push({ key: 'a' + i, label: s.title, data: s.series.alt || [], color, axis: 0 });
    });

    const maxLen = Math.max(...series.map(s => (s.series.d || []).length), 2);
    const resample = (arr) => {
      if (!arr || !arr.length) return [];
      const out = [];
      for (let i = 0; i < maxLen; i++) {
        const idx = Math.round((i / (maxLen - 1)) * (arr.length - 1));
        out.push(arr[idx]);
      }
      return out;
    };
    // 所有活动里最长的距离（用于按真实距离占比排布 x）
    const longestM = Math.max(1, ...series.map(s => {
      const d = s.series.d || [];
      return d.length ? (d[d.length - 1] || 0) : 0;
    }));
    const xValues = Array.from({ length: maxLen }, (_, i) => (i / (maxLen - 1)) * maxDist);
    const isRunLike = (fam) => fam === 'run' || fam === 'walk' || fam === 'swim';
    const speedMode = !isRunLike(U.family(acts[0] && acts[0].sport));
    // 每条活动按自己的实际距离占位：x = 该点距离 / 所有活动中最长的距离
    // （否则 5km 的活动会被人为拉伸到 45km 的宽度）
    const seriesForChart = paceSeries.map((s, i) => {
      const src = series[i].series;
      const d = src.d || [];
      // 服务端 speed 流统一是 km/h；画速度就直接用，画配速再换算成 秒/公里
      const kmh = src.speed || [];
      const data = new Array(maxLen).fill(null);
      if (d.length && kmh.length) {
        for (let k = 0; k < d.length && k < kmh.length; k++) {
          const pos = Math.round((d[k] / longestM) * (maxLen - 1));
          if (pos < 0 || pos >= maxLen) continue;
          const v = kmh[k];
          if (v == null || v <= 0.5) continue;      // 停下/推车的点不画
          data[pos] = speedMode ? v : 3600 / v;
        }
      }
      return Object.assign({}, s, { data });
    });
    const paceSeriesFlat = seriesForChart;
    const paceNote = document.getElementById('cmpPaceNote');
    if (paceNote) {
      paceNote.textContent = speedMode
        ? '单位 km/h（越高越快）'
        : '单位 秒/公里（越低越快，纵轴已反转）';
    }

    const c1 = new Chart(document.getElementById('cmpPace'), Object.assign({}, common, {
      xValues,
      invertY: !speedMode,
      series: paceSeriesFlat,
      axes: [{}],
      legend: false,
    }));
    state.charts.push(c1);

    // 统一到同一根距离轴：先按每条活动自己的长度等距重采样，再按真实距离占比放到 x 槽位
    const placeByDistance = (src, pick, transform) => {
      const d = src.d || [];
      const raw = pick(src);
      const out = new Array(maxLen).fill(null);
      if (!d.length || !raw.length) return out;
      const n = Math.min(d.length, raw.length);
      for (let slot = 0; slot < n; slot++) {
        const k = Math.round((slot / Math.max(1, n - 1)) * (n - 1));
        const pos = Math.round((d[k] / longestM) * (maxLen - 1));
        if (pos < 0 || pos >= maxLen) continue;
        const v = raw[k];
        out[pos] = transform ? transform(v) : v;
      }
      return out;
    };

    const c2 = new Chart(document.getElementById('cmpHr'), Object.assign({}, common, {
      xValues,
      series: hrSeries.map((s, i) => Object.assign({}, s, {
        data: placeByDistance(series[i].series, src => src.hr || []),
      })),
      axes: [{ pad: 0.1 }],
      legend: false,
    }));
    state.charts.push(c2);

    const c3 = new Chart(document.getElementById('cmpAlt'), Object.assign({}, common, {
      xValues,
      series: altSeries.map((s, i) => Object.assign({}, s, {
        data: placeByDistance(series[i].series, src => src.alt || []),
      })),
      axes: [{ min: 0 }],
      legend: false,
    }));
    state.charts.push(c3);

    const setAllHover = (idx) => {
      state.charts.forEach(c => { c.hoverIndex = idx; c.draw(); });
    };
    state.charts.forEach(c => { c.opts.onHover = setAllHover; });
  }

  function nearestIndex(arr, target) {
    if (!arr || !arr.length) return -1;
    let best = 0, bestD = Infinity;
    for (let i = 0; i < arr.length; i++) {
      const d = Math.abs(arr[i] - target);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }

  async function runCompare(root) {
    root.innerHTML = UI.spinner('加载活动数据…');
    try {
      const data = await api.compare(state.ids);
      state.data = data;
      renderCompare(root, data);
      document.getElementById('pageTitle').textContent = '活动对比';
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty('info', '对比失败', U.esc(err.message),
        `<button class="btn" id="btnBackPick2">返回</button>`)}</div>`;
      const back = document.getElementById('btnBackPick2');
      if (back) back.addEventListener('click', () => renderPicker(root));
    }
  }

  async function load(root, params) {
    cleanup();
    host = root;
    if (params && params.ids) {
      state.ids = String(params.ids).split(',').map(Number).filter(Boolean).slice(0, 6);
      if (state.ids.length >= 2) { await runCompare(root); return; }
    }
    state.ids = [];
    renderPicker(root);
  }

  window.PageCompare = { load, cleanup };
})();
