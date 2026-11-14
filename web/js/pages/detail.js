/* 活动详情：指标、地图回放、联动图表、心率区间、最佳成绩、分段。 */
(function () {
  const state = { activity: null, charts: [], map: null, marker: -1, playing: false, raf: null };
  let host = null;

  function destroyCharts() {
    state.charts.forEach(c => c && c.destroy());
    state.charts = [];
  }

  function cleanup() {
    destroyCharts();
    if (state.map) { state.map.destroy(); state.map = null; }
    if (state.raf) { cancelAnimationFrame(state.raf); state.raf = null; }
    state.playing = false;
  }

  function metric(label, value, unit) {
    return `<div class="detail-metric"><span>${label}</span><b>${value}${unit ? `<small> ${unit}</small>` : ''}</b></div>`;
  }

  function heroHtml(a) {
    const fam = U.family(a.sport);
    const s = a;
    const paceUnit = fam === 'ride' || fam === 'other' ? 'km/h' : fam === 'swim' ? '/100m' : '/km';
    const paceVal = fam === 'ride' || fam === 'other'
      ? (s.avg_speed_kmh ? s.avg_speed_kmh.toFixed(1) : '—')
      : (s.pace_s_per_km ? U.paceLabel(s.pace_s_per_km, fam) : '—');
    const maxSpeed = fam === 'ride' || fam === 'other'
      ? (s.max_speed_kmh ? s.max_speed_kmh.toFixed(1) : '—')
      : (s.max_speed_kmh ? U.paceFromSpeed(s.max_speed_kmh, fam) : '—');

    return `
      <div class="detail-hero">
        <div class="row between" style="align-items:flex-start;gap:16px">
          <div style="min-width:0">
            <div class="row" style="gap:8px;margin-bottom:8px">
              ${UI.sportChip(a.sport)}
              ${a.has_gps ? `<span class="chip">${Icon.get('pin')}有轨迹</span>` : `<span class="chip">${Icon.get('info')}无轨迹</span>`}
              ${a.filename ? `<span class="chip">${U.esc(a.filename)}</span>` : ''}
            </div>
            <h1>${U.esc(a.title || U.sportLabel(a.sport))}</h1>
            <div class="sub">
              ${U.dateLabel(a.start_time)} · ${U.weekday(a.start_time)}
              ${a.source_distance_m ? ` · 设备记录距离 ${U.km(a.source_distance_m, 2)} km` : ''}
            </div>
          </div>
          <div class="row" style="gap:6px">
            <button class="btn btn-sm" id="btnEdit">${Icon.get('settings')}编辑</button>
            <button class="btn btn-sm btn-danger" id="btnDelete">${Icon.get('trash')}删除</button>
          </div>
        </div>

        <div class="detail-grid">
          ${metric('距离', U.km(a.distance_m), 'km')}
          ${metric('移动时间', U.dur(a.moving_seconds), '')}
          ${metric('平均' + (fam === 'ride' || fam === 'other' ? '速度' : '配速'), paceVal, paceUnit)}
          ${metric('爬升', U.elev(a.elevation_gain_m), 'm')}
          ${metric('平均心率', a.avg_hr ? Math.round(a.avg_hr) : '—', a.avg_hr ? 'bpm' : '')}
          ${metric('最大速度', maxSpeed, fam === 'ride' || fam === 'other' ? 'km/h' : paceUnit)}
          ${metric('训练负荷', U.num(a.training_load), '')}
          ${metric('总时长', U.dur(a.elapsed_seconds), '')}
        </div>
      </div>`;
  }

  function mapCardHtml(a) {
    if (!a.has_gps || !(a.route || []).length) {
      return `<div class="card"><div class="card-head"><h3>轨迹</h3></div>
        ${UI.empty('route', '没有 GPS 轨迹',
          '这个活动来自摘要记录（CSV）或室内设备。距离、时长、配速仍然可用，但没有地图与轨迹回放。')}</div>`;
    }
    return `
      <div class="map-wrap card" style="padding:0;overflow:hidden">
        <div id="mapHost" style="position:relative"></div>
        <div class="map-toolbar">
          <button id="mapZoomIn" title="放大">${Icon.get('zoomIn')}</button>
          <button id="mapZoomOut" title="缩小">${Icon.get('zoomOut')}</button>
          <button id="mapFit" title="适应边界">${Icon.get('layers')}</button>
        </div>
        <div class="map-overlay" id="mapOverlay">起点 S · 终点 E</div>
      </div>
      <div class="playbar">
        <button class="btn btn-sm" id="btnPlay">${Icon.get('play')}回放</button>
        <input type="range" id="playRange" min="0" max="100" value="0" step="0.1" aria-label="回放进度">
        <span class="time" id="playTime">00:00 / ${U.dur(a.moving_seconds)}</span>
      </div>`;
  }

  function chartCardHtml() {
    return `
      <div class="card">
        <div class="card-head"><h3>数据流</h3>
          <span class="dim">按距离排列 · 鼠标悬停查看</span>
          <div class="spacer"></div>
          <div class="seg" id="chartMetric">
            <button data-m="all" class="active">全部</button>
            <button data-m="pace">配速</button>
            <button data-m="hr">心率</button>
            <button data-m="alt">海拔</button>
          </div>
        </div>
        <div class="card-body" style="padding:10px 14px 16px">
          <div class="chart-block">
            <div class="chart-title"><span id="ct1">配速</span><span id="cv1" class="mono"></span></div>
            <div class="chart-holder"><canvas id="chartPace"></canvas></div>
          </div>
          <div class="chart-block">
            <div class="chart-title"><span>心率</span><span id="cv2" class="mono"></span></div>
            <div class="chart-holder"><canvas id="chartHr"></canvas></div>
          </div>
          <div class="chart-block">
            <div class="chart-title"><span>海拔</span><span id="cv3" class="mono"></span></div>
            <div class="chart-holder"><canvas id="chartAlt"></canvas></div>
          </div>
        </div>
      </div>`;
  }

  function zonesHtml(a) {
    const zones = a.hr_zones || {};
    const total = Object.values(zones).reduce((x, y) => x + y, 0);
    if (!total) {
      return `<div class="card"><div class="card-head"><h3>心率区间</h3></div>
        ${UI.empty('heart', '没有心率数据', '这个活动没有记录心率，或者心率区间设置不完整。', 
          `<a class="btn btn-sm" href="#/settings">去设置</a>`)}</div>`;
    }
    const colors = ['var(--hr1)', 'var(--hr2)', 'var(--hr3)', 'var(--hr4)', 'var(--hr5)'];
    const names = ['Z1 恢复', 'Z2 有氧', 'Z3 节奏', 'Z4 阈值', 'Z5 无氧'];
    const rows = ['z1', 'z2', 'z3', 'z4', 'z5'].map((k, i) => {
      const sec = zones[k] || 0;
      const pct = total ? (sec / total) * 100 : 0;
      return `<div class="zone-row">
        <span class="dim">${names[i].slice(0, 2)}</span>
        <div class="zone-track"><i style="width:${Math.max(1, pct)}%;background:${colors[i]}"></i></div>
        <span class="num">${U.dur(sec)}</span>
      </div>`;
    }).join('');
    return `<div class="card"><div class="card-head"><h3>心率区间</h3>
        <span class="dim">总计 ${U.dur(total)}</span></div>
      <div class="card-body zones">${rows}</div></div>`;
  }

  function effortsHtml(a) {
    const best = a.best_efforts || [];
    const segs = a.segments || [];
    const fam = U.family(a.sport);
    if (!best.length && !segs.length) {
      return '';
    }
    const bestRows = best.map(b => {
      const pace = fam === 'ride' ? (b.seconds ? (b.distance_m / b.seconds * 3.6).toFixed(1) + ' km/h' : '—')
        : fam === 'swim' ? U.pace(b.pace_seconds) + '/100m' : U.pace(b.pace_seconds) + '/km';
      const prs = (a._prLabels || {});
      const isPr = prs[b.label];
      return `<tr>
        <td>${U.esc(b.label)}${isPr ? '<span class="pr-badge">PR</span>' : ''}</td>
        <td class="mono"><b>${U.dur(b.seconds)}</b></td>
        <td class="mono dim">${pace}</td>
      </tr>`;
    }).join('');
    const segRows = segs.map(e => `<tr>
        <td><a href="#/segment/${e.segment_id}">${U.esc(e.name)}</a>${e.is_best ? '<span class="pr-badge">最佳</span>' : ''}</td>
        <td class="mono"><b>${U.dur(e.elapsed_s)}</b></td>
        <td class="mono dim">${(e.seg_distance_m / 1000).toFixed(2)} km</td>
      </tr>`).join('');

    return `
      <div class="grid g-2 section">
        ${best.length ? `<div class="card">
          <div class="card-head"><h3>最佳成绩</h3><span class="dim">本活动内的最快分段</span></div>
          <table class="effort-table"><thead><tr><th>距离</th><th>用时</th><th>配速</th></tr></thead>
          <tbody>${bestRows}</tbody></table>
        </div>` : ''}
        ${segs.length ? `<div class="card">
          <div class="card-head"><h3>分段成绩</h3><div class="spacer"></div>
            <a class="btn btn-sm btn-ghost" href="#/segments">管理分段${Icon.get('chevron')}</a></div>
          <table class="effort-table"><thead><tr><th>分段</th><th>用时</th><th>长度</th></tr></thead>
          <tbody>${segRows}</tbody></table>
        </div>` : ''}
      </div>`;
  }

  function channelsHtml(a) {
    const st = a.streams || {};
    const any = (key) => Array.isArray(st[key]) && st[key].some(v => v != null);
    const rows = [
      { label: 'GPS', ok: a.has_gps && (a.route || []).length > 1, need: '活动文件里没有轨迹点' },
      { label: '海拔', ok: any('alt'), need: '文件没有海拔数据（GPX 里是 0 或缺失）' },
      { label: '心率', ok: any('hr'), need: '文件里没有心率通道：需要心率带，且导出格式要带 HR（FIT）' },
      { label: '踏频', ok: any('cad'), need: '文件里没有踏频通道：需要踏频传感器，GPX 导出不写' },
      { label: '功率', ok: any('pwr'), need: '文件里没有功率通道：需要功率计或骑行台，GPX 导出不写' },
    ];
    const chips = rows.map(r => `<span class="ch ${r.ok ? 'ok' : 'no'}" title="${U.esc(r.need)}">
      <i></i>${r.label}</span>`).join('');
    const missing = rows.filter(r => !r.ok).map(r => r.label);
    return `<div class="channel-strip">
      <span class="ch-title">数据通道</span>${chips}
      <span class="ch-note">${missing.length
        ? `${missing.join('、')}缺失 — 悬停看原因（多半是文件本身没有这个通道）`
        : '五项都齐全'}</span>
    </div>`;
  }

  function render(a) {
    const fam = U.family(a.sport);
    const hasStreams = !!(a.streams && a.streams.t && a.streams.t.length);
    host.innerHTML = `
      <div class="detail-bar">
        <button class="btn btn-sm btn-ghost" id="btnBack">${Icon.get('back')}返回</button>
        <span class="tiny dim">${U.esc(U.sportLabel(a.sport))}详情</span>
        <div class="spacer"></div>
        <a class="btn btn-sm btn-ghost" href="#/activities">全部运动数据</a>
      </div>
      ${heroHtml(a)}
      ${channelsHtml(a)}
      ${analysisCard(a)}
      ${a.analysis ? specialtyBlock(a.analysis.specialty) : ''}
      <div class="detail-cols">
        <div>${mapCardHtml(a)}</div>
        ${hasStreams ? chartCardHtml() : `
          <div class="card">
            <div class="card-head"><h3>数据流</h3></div>
            ${UI.empty('trend', '没有逐点数据',
              '这个活动只有汇总数字（距离、时长、心率），没有记录逐点的时间序列，因此无法绘制配速与心率曲线。' +
              (a.has_gps ? '' : '导入 GPX / FIT / TCX 文件即可获得完整曲线。'))}
          </div>`}
      </div>
      <div class="grid g-2 section">
        ${zonesHtml(a)}
        <div class="card">
          <div class="card-head"><h3>明细</h3></div>
          <div class="card-body">
            <table class="data-table">
              <tbody>
                <tr><td class="dim">运动类型</td><td>${U.esc(U.sportLabel(a.sport))}</td></tr>
                <tr><td class="dim">开始时间</td><td>${new Date(a.start_time * 1000).toLocaleString('zh-CN')}</td></tr>
                <tr><td class="dim">结束时间</td><td>${a.end_time ? new Date(a.end_time * 1000).toLocaleString('zh-CN') : '—'}</td></tr>
                <tr><td class="dim">总时长 / 移动</td><td>${U.dur(a.elapsed_seconds)} / ${U.dur(a.moving_seconds)}</td></tr>
                <tr><td class="dim">爬升 / 下降</td><td>${U.elev(a.elevation_gain_m)} m / ${U.elev(a.elevation_loss_m)} m</td></tr>
                <tr><td class="dim">平均 / 最大心率</td><td>${a.avg_hr ? Math.round(a.avg_hr) : '—'} / ${a.max_hr ? Math.round(a.max_hr) : '—'} bpm</td></tr>
                ${a.avg_power ? `<tr><td class="dim">平均 / NP 功率</td><td>${Math.round(a.avg_power)} / ${a.np_power ? Math.round(a.np_power) : '—'} W</td></tr>` : ''}
                <tr><td class="dim">数据点</td><td>${U.num(a.point_count)} ${a.streams && a.streams.t ? `· 采样 ${a.streams.t.length} 点` : ''}</td></tr>
                <tr><td class="dim">训练负荷</td><td>${U.num(a.training_load)} <span class="dim">(${a.load_method === 'trimp' ? '心率模型' : a.load_method === 'estimated' ? '估算' : '—'})</span></td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
      ${a.analysis ? splitsFold(a.analysis) : ''}
      ${effortsHtml(a)}
      <p class="tiny dim" style="margin-top:18px;text-align:center">
        <a href="#/activities">← 返回活动列表</a>
      </p>`;

    Icon.hydrate(host);
    wireMap(a);
    if (hasStreams) wireCharts(a);
    wireActions(a);
    document.getElementById('pageTitle').textContent = a.title || '活动详情';
  }

  /* ---------------------------------------------------------- 地图 */

  function wireMap(a) {
    if (!a.has_gps || !(a.route || []).length) return;
    const mapHost = document.getElementById('mapHost');
    if (!mapHost) return;
    const settings = window.PaceApp ? window.PaceApp.settings : {};
    const mapCfg = (window.PaceApp && window.PaceApp.mapConfig) || { url: '', gcj: false };
    state.map = new TrackMap(mapHost, {
      height: 380,
      tileUrl: mapCfg.url || settings.tile_url || '',
      gcj: !!mapCfg.gcj,
      interactive: true,
    });
    state.map.setRoute(a.route);
    Icon.hydrate(mapHost);

    document.getElementById('mapZoomIn').addEventListener('click', () => state.map.zoomBy(1));
    document.getElementById('mapZoomOut').addEventListener('click', () => state.map.zoomBy(-1));
    document.getElementById('mapFit').addEventListener('click', () => {
      state.map.fit();
      UI.toast('已回到全程视野', 'ok', 1200);
    });

    const range = document.getElementById('playRange');
    const playBtn = document.getElementById('btnPlay');
    const timeEl = document.getElementById('playTime');
    const total = Math.max(1, a.moving_seconds || 1);
    // 回放固定 20 秒走完全程：原先按 30 倍速，4 小时的活动要放 8 分钟，
    // 点下去几秒内几乎看不出移动，用户会以为「播放没反应」。
    const PLAYBACK_SECONDS = 20;
    const rate = total / PLAYBACK_SECONDS;
    let trailIdx = -1;
    range.max = String(total);
    range.addEventListener('input', () => {
      stopPlay();
      seek(Number(range.value));
    });
    playBtn.addEventListener('click', () => {
      if (state.playing) stopPlay();
      else startPlay();
    });
    timeEl.textContent = `00:00 / ${U.dur(total)}`;
    playBtn.innerHTML = `${Icon.get('play')}回放 ${PLAYBACK_SECONDS}s`;

    function seek(seconds) {
      const frac = U.clamp(seconds / total, 0, 1);
      const idx = Math.round(frac * (a.route.length - 1));
      state.marker = idx;
      // 先更新时间与图表，地图绘制万一出错也不会让回放整条卡死
      timeEl.textContent = `${U.dur(seconds)} / ${U.dur(total)} · ${Math.round(frac * 100)}%`;
      try {
        state.map.setMarker(idx);
        // 已骑/已跑过的部分加粗，进度一眼可见
        if (idx !== trailIdx) {
          trailIdx = idx;
          state.map.setHighlight(a.route.slice(0, idx + 1), '#ffffff');
        }
      } catch (err) {
        console.error(err);
      }
      highlightCharts(idx, frac);
    }

    function startPlay() {
      state.playing = true;
      playBtn.innerHTML = `${Icon.get('pause')}暂停`;
      const startedAt = performance.now() - Number(range.value) / rate * 1000;
      const step = (now) => {
        if (!state.playing) return;
        const seconds = ((now - startedAt) / 1000) * rate;
        if (seconds >= total) {
          range.value = String(total);
          seek(total);
          stopPlay();
          return;
        }
        range.value = String(seconds);
        seek(seconds);
        state.raf = requestAnimationFrame(step);
      };
      state.raf = requestAnimationFrame(step);
    }

    function stopPlay() {
      state.playing = false;
      if (state.raf) cancelAnimationFrame(state.raf);
      state.raf = null;
      playBtn.innerHTML = `${Icon.get('play')}回放 ${PLAYBACK_SECONDS}s`;
    }

    state._seek = seek;
    state._stopPlay = stopPlay;
  }

  function highlightCharts(index, frac) {
    state.charts.forEach(c => {
      const n = c._pointCount();
      c.hoverIndex = Math.round(frac * (n - 1));
      c.draw();
    });
    const v1 = document.getElementById('cv1');
    if (v1 && state.activity) updateReadouts(state.activity, Math.round(frac * ((state.activity.streams.t || []).length - 1)));
  }

  /* ---------------------------------------------------------- 图表 */

  function wireCharts(a) {
    const st = a.streams || {};
    if (!st.t || !st.t.length) {
      const card = document.querySelector('.chart-holder');
      if (card && card.parentElement) card.parentElement.innerHTML =
        '<p class="tiny dim" style="padding:20px">没有可绘制的数据流</p>';
      return;
    }
    const fam = U.family(a.sport);
    const d = st.d || [];
    const totalKm = Math.max(0.001, (d[d.length - 1] || 0) / 1000);
    const paceData = st.pace || null;
    const speedData = st.speed || [];
    const isPace = fam !== 'ride' && fam !== 'other' && paceData;

    const tooltip = (i) => {
      const km = (d[i] || 0) / 1000;
      const parts = [`<b>${km.toFixed(2)} km</b> · ${U.dur(st.t[i])}`];
      if (isPace && paceData[i]) parts.push(`配速 ${U.pace(paceData[i])}${fam === 'swim' ? '/100m' : '/km'}`);
      else if (speedData[i] != null) parts.push(`速度 ${speedData[i].toFixed(1)} km/h`);
      if (st.hr && st.hr[i] != null) parts.push(`心率 ${Math.round(st.hr[i])} bpm`);
      if (st.alt && st.alt[i] != null) parts.push(`海拔 ${Math.round(st.alt[i])} m`);
      return parts.join('<br>');
    };

    const xLabels = [];
    const steps = 6;
    for (let i = 0; i <= steps; i++) xLabels.push((totalKm * i / steps).toFixed(totalKm < 10 ? 1 : 0) + ' km');

    const makeX = () => {
      const arr = [];
      for (let i = 0; i < d.length; i++) arr.push(d[i] / 1000);
      return arr;
    };

    // 所有图表共享同一个 hover 索引，鼠标在任意一张图上移动时其余图同步
    const setHover = (idx) => {
      state.charts.forEach(c => {
        if (c.hoverIndex !== idx) { c.hoverIndex = idx; c.draw(); }
      });
      updateReadouts(a, idx);
    };

    const onHover = (i) => { if (i !== undefined) setHover(i); };

    // 配速（越低越快，Y 轴反转）
    const paceCanvas = document.getElementById('chartPace');
    if (isPace) {
      const values = paceData.map(v => (v == null ? null : v));
      const valid = values.filter(v => v != null && isFinite(v));
        const chart = new Chart(paceCanvas, {
        type: 'line', height: 108,
        invertY: true,
        xValues: makeX(), xMin: 0, xMax: totalKm,
        series: [{ key: 'pace', data: values, color: 'var(--run)', fill: true, fillOpacity: 0.16, axis: 0 }],
        // 不指定 min/max，让引擎按数据范围留白；invertY 会把"快"画在上方
        axes: [{ pad: 0.18, unit: 'sec' }],
        legend: false, tooltip, onHover,
        padding: { top: 8, right: 10, bottom: 20, left: 34 },
      });
      state.charts.push(chart);
      document.getElementById('ct1').textContent = fam === 'swim' ? '配速 /100m' : '配速 /km';
    } else {
      const values = speedData;
      const chart = new Chart(paceCanvas, {
        type: 'line', height: 108,
        xValues: makeX(), xMin: 0, xMax: totalKm,
        series: [{ key: 'speed', data: values, color: fam === 'ride' ? 'var(--ride)' : 'var(--accent)', fill: true, fillOpacity: 0.16, axis: 0 }],
        axes: [{ min: 0, unit: 'km/h' }],
        legend: false, tooltip, onHover,
        padding: { top: 8, right: 10, bottom: 20, left: 34 },
      });
      state.charts.push(chart);
      document.getElementById('ct1').textContent = '速度';
    }

    // 心率
    if (st.hr && st.hr.some(v => v != null)) {
      const chart = new Chart(document.getElementById('chartHr'), {
        type: 'line', height: 96,
        xValues: makeX(), xMin: 0, xMax: totalKm,
        series: [{ key: 'hr', data: st.hr, color: 'var(--hr5)', fill: true, fillOpacity: 0.14, axis: 0 }],
        axes: [{ pad: 0.12 }],
        legend: false, tooltip, onHover,
        padding: { top: 8, right: 10, bottom: 20, left: 34 },
      });
      state.charts.push(chart);
    } else {
      document.getElementById('chartHr').closest('.chart-block').style.display = 'none';
    }

    // 海拔
    if (st.alt && st.alt.some(v => v != null)) {
      const chart = new Chart(document.getElementById('chartAlt'), {
        type: 'line', height: 96,
        xValues: makeX(), xMin: 0, xMax: totalKm,
        series: [{ key: 'alt', data: st.alt, color: 'var(--walk)', fill: true, fillOpacity: 0.16, axis: 0 }],
        axes: [{ pad: 0.2 }],
        legend: false, tooltip, onHover,
        padding: { top: 8, right: 10, bottom: 20, left: 34 },
      });
      state.charts.push(chart);
    } else {
      document.getElementById('chartAlt').closest('.chart-block').style.display = 'none';
    }
  }

  function updateReadouts(a, index) {
    const st = a.streams || {};
    if (!st.t || index < 0 || index >= st.t.length) {
      document.getElementById('cv1').textContent = '';
      document.getElementById('cv2').textContent = '';
      document.getElementById('cv3').textContent = '';
      return;
    }
    const fam = U.family(a.sport);
    const pace = (st.pace || [])[index];
    const speed = (st.speed || [])[index];
    const hr = (st.hr || [])[index];
    const alt = (st.alt || [])[index];
    const km = ((st.d || [])[index] || 0) / 1000;
    document.getElementById('cv1').textContent = fam !== 'ride' && fam !== 'other' && pace
      ? `${U.pace(pace)}${fam === 'swim' ? '/100m' : '/km'} · ${km.toFixed(2)} km`
      : (speed != null ? `${speed.toFixed(1)} km/h · ${km.toFixed(2)} km` : '');
    document.getElementById('cv2').textContent = hr != null ? `${Math.round(hr)} bpm` : '';
    document.getElementById('cv3').textContent = alt != null ? `${Math.round(alt)} m` : '';
  }

  /* ---------------------------------------------------------- 操作 */

  function wireActions(a) {
    const back = document.getElementById('btnBack');
    if (back) back.addEventListener('click', () => {
      // 回到对应运动类型的列表页，别把筛选丢了
      const fam = U.family(a.sport);
      const sport = ['run', 'ride', 'swim'].includes(fam) ? fam : '';
      location.hash = '#/activities' + (sport ? `?sport=${sport}` : '');
    });
    document.getElementById('btnDelete').addEventListener('click', async () => {
      const ok = await UI.confirmDialog('删除活动', `确定删除「${U.esc(a.title || '')}」吗？此操作不可撤销。`, '删除', true);
      if (!ok) return;
      try {
        await api.deleteActivity(a.id);
        UI.toast('已删除', 'ok');
        location.hash = '#/activities';
      } catch (err) {
        UI.toast('删除失败：' + U.esc(err.message), 'err');
      }
    });

    document.getElementById('btnEdit').addEventListener('click', async () => {
      const dlg = document.createElement('dialog');
      dlg.className = 'dialog';
      dlg.innerHTML = `
        <form method="dialog">
          <div class="dialog-head">
            <h2>编辑活动</h2>
            <button class="icon-btn" value="cancel">${Icon.get('x')}</button>
          </div>
          <div class="dialog-body">
            <label class="tiny dim">标题
              <input id="editTitle" class="control" style="width:100%;margin-top:6px;height:38px;padding:0 10px;border-radius:9px;border:1px solid var(--line);background:var(--bg-elev-2);color:var(--text)"
                value="${U.esc(a.title || '')}">
            </label>
            <label class="tiny dim">运动类型
              <select id="editSport" class="control" style="width:100%;margin-top:6px">
                ${['running', 'trail_running', 'treadmill_running', 'cycling', 'road_cycling',
                   'indoor_cycling', 'mountain_biking', 'swimming', 'pool_swimming',
                   'open_water_swimming', 'walking', 'hiking', 'workout', 'generic']
                  .map(s => `<option value="${s}" ${a.sport === s ? 'selected' : ''}>${U.sportLabel(s)}</option>`).join('')}
              </select>
            </label>
            <label class="tiny dim">备注
              <textarea id="editNotes" rows="3" style="width:100%;margin-top:6px;padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--bg-elev-2);color:var(--text);font:inherit;resize:vertical">${U.esc(a.notes || '')}</textarea>
            </label>
          </div>
          <div class="dialog-foot">
            <button class="btn" value="cancel">取消</button>
            <button class="btn btn-primary" value="ok" id="editSave">保存</button>
          </div>
        </form>`;
      document.body.appendChild(dlg);
      dlg.addEventListener('close', async () => {
        if (dlg.returnValue !== 'ok') { dlg.remove(); return; }
        const payload = {
          title: document.getElementById('editTitle').value.trim() || a.title,
          sport: document.getElementById('editSport').value,
          notes: document.getElementById('editNotes').value.trim(),
        };
        try {
          await api.updateActivity(a.id, payload);
          UI.toast('已保存', 'ok');
          window.Router.rerender();
        } catch (err) {
          UI.toast('保存失败：' + U.esc(err.message), 'err');
        }
        dlg.remove();
      });
      dlg.showModal();
      Icon.hydrate(dlg);
    });
  }

  /* ---------------------------------------------------------- 单次分析报告 */

  function scoreRing(score, grade, size) {
    const r = size / 2 - 6;
    const c = 2 * Math.PI * r;
    return `<div style="position:relative;width:${size}px;height:${size}px;flex:none">
      <svg viewBox="0 0 ${size} ${size}" style="width:${size}px;height:${size}px;transform:rotate(-90deg)">
        <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--bg-elev-3)" stroke-width="6"/>
        <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--accent)" stroke-width="6"
          stroke-linecap="round" stroke-dasharray="${(score / 100) * c} ${c}"/>
      </svg>
      <div style="position:absolute;inset:0;display:grid;place-items:center;text-align:center">
        <div><div style="font-size:${size / 4}px;font-weight:700;line-height:1">${grade}</div>
        <div class="tiny dim">${Math.round(score)}</div></div>
      </div></div>`;
  }

  function capBar(label, value) {
    return `<div style="margin-bottom:7px">
      <div class="row between tiny" style="margin-bottom:2px">
        <span class="dim">${label}</span><span class="mono">${Math.round(value)}</span>
      </div>
      <div class="zone-track" style="height:5px"><i style="width:${U.clamp(value, 2, 100)}%;background:var(--accent)"></i></div>
    </div>`;
  }

  const POWER_ZONE_LABELS = ['Z1 恢复', 'Z2 耐力', 'Z3 节奏', 'Z4 阈值', 'Z5 VO2max', 'Z6 无氧', 'Z7 神经'];
  const SPEC_TITLE = { ride: '骑行专项', run: '跑步专项', walk: '步行专项', swim: '游泳专项' };

  function powerZoneRows(zones) {
    const keys = ['z1', 'z2', 'z3', 'z4', 'z5', 'z6', 'z7'];
    const total = keys.reduce((n, k) => n + (zones[k] || 0), 0);
    if (!total) return '';
    const colors = ['var(--hr1)', 'var(--hr2)', 'var(--hr3)', 'var(--hr4)', 'var(--hr5)', '#b23', '#e33'];
    const rows = keys.map((k, i) => {
      const sec = zones[k] || 0;
      const pct = (sec / total) * 100;
      return `<div class="zone-row power">
        <span class="dim">${POWER_ZONE_LABELS[i]}</span>
        <div class="zone-track"><i style="width:${Math.max(0.5, pct)}%;background:${colors[i]}"></i></div>
        <span class="num">${U.dur(sec)}</span>
        <span class="num dim" style="text-align:right">${pct.toFixed(0)}%</span>
      </div>`;
    }).join('');
    return `<div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--line-soft)">
      <div class="tiny dim" style="margin-bottom:8px">功率区间分布（按 FTP 划分）</div>
      ${rows}</div>`;
  }

  function specialtyBlock(sp) {
    if (!sp || !sp.items || !sp.items.length) return '';
    const rows = sp.items.map(it => `
      <div class="spec${it.ok ? '' : ' off'}" title="${U.esc(it.hint || '')}">
        <span>${U.esc(it.label)}</span>
        <b>${it.ok ? U.esc(String(it.value)) : '—'}${it.ok && it.unit ? `<small>${U.esc(it.unit)}</small>` : ''}</b>
        <i>${U.esc(it.hint || '')}</i>
      </div>`).join('');
    const missing = sp.items.filter(i => !i.ok).length;
    return `<div class="card section">
      <div class="card-head">
        <h3>${SPEC_TITLE[sp.family] || '专项指标'}</h3>
        <span class="dim">${sp.family === 'ride' && sp.ftp ? `功率口径：FTP ${Math.round(sp.ftp)} W` :
          sp.family === 'run' ? '配速口径：分:秒/公里' : ''}</span>
        <div class="spacer"></div>
        ${missing ? `<span class="tiny dim">${missing} 项缺数据</span>` : ''}
      </div>
      <div class="card-body">
        <div class="spec-grid">${rows}</div>
        ${sp.power_zones ? powerZoneRows(sp.power_zones) : ''}
      </div>
    </div>`;
  }

  function splitsFold(rep) {
    if (!rep.splits || !rep.splits.length) return '';
    const family = rep.family;
    const paces = rep.splits.filter(s => !s.is_partial).map(s => s.pace_s || s.speed_kmh);
    const bestVal = paces.length ? (family === 'ride' || family === 'other' ? Math.max(...paces) : Math.min(...paces)) : null;
    const rows = rep.splits.map(s => {
      const isBest = bestVal != null && (family === 'ride' || family === 'other'
        ? s.speed_kmh === bestVal : s.pace_s === bestVal);
      const display = family === 'ride' || family === 'other'
        ? (s.speed_kmh ? s.speed_kmh.toFixed(1) + ' km/h' : '—')
        : (s.pace_s ? U.pace(s.pace_s) + (family === 'swim' ? '/100m' : '/km') : '—');
      const barBase = family === 'ride' || family === 'other' ? s.speed_kmh : (s.pace_s ? 1000 / s.pace_s : 0);
      const width = barBase ? U.clamp((family === 'ride' || family === 'other'
        ? barBase / (Math.max(...paces) || 1) : barBase / (Math.max(...[1, ...paces.map(p => 1000 / p)]) || 1)) * 100, 4, 100) : 4;
      return `<tr>
        <td class="mono dim">${s.index}${s.is_partial ? '*' : ''}</td>
        <td class="mono" style="white-space:nowrap">${s.distance_m >= 1000 ? (s.distance_m / 1000).toFixed(1) : Math.round(s.distance_m)}${s.distance_m >= 1000 ? ' km' : ' m'}</td>
        <td class="mono" style="white-space:nowrap"><b>${U.dur(s.elapsed_s)}</b></td>
        <td class="mono">${display}${isBest ? '<span class="pr-badge">最快</span>' : ''}</td>
        <td class="mono dim">${s.avg_hr ? Math.round(s.avg_hr) : '—'}</td>
        <td class="mono dim">${s.gain_m ? '+' + Math.round(s.gain_m) + ' m' : '—'}</td>
        <td style="width:90px;min-width:70px">
          <div class="zone-track" style="height:5px"><i style="width:${width}%;background:var(--accent)"></i></div>
        </td>
      </tr>`;
    }).join('');
    const unitLabel = rep.split_unit_m >= 1000 ? rep.split_unit_m / 1000 + ' 公里' : rep.split_unit_m + ' 米';
    // 分段动辄几十行，默认收起，避免把下面的模块全推到很下面
    return `<details class="fold card">
      <summary>
        <span class="fold-title">分段明细</span>
        <span class="tiny dim">${rep.splits.length} 段 · 每 ${unitLabel}一段 · 点开查看</span>
        <span class="spacer"></span>
        <span class="fold-chevron">${Icon.get('chevron')}</span>
      </summary>
      <div class="fold-body" style="overflow:auto;max-height:420px">
        <table class="data-table">
          <thead><tr><th>#</th><th>距离</th><th>用时</th><th>配速/速度</th><th>心率</th><th>爬升</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table>
      </div>
      <p class="tiny dim" style="padding:8px 14px">* 表示不足一整段</p>
    </details>`;
  }

  function analysisCard(a) {
    const rep = a.analysis || {};
    if (!rep || !rep.verdict) return '';
    const fam = U.family(a.sport);
    const isRide = fam === 'ride';
    const v = rep.verdict || {};
    const q = rep.data_quality || {};
    const ex = rep.execution || {};
    const climbs = (rep.climbs && rep.climbs.length) ? rep.climbs
      : ((a.climbs && a.climbs.length) ? a.climbs : []);
    const stopped = a.stopped_seconds != null ? a.stopped_seconds
      : Math.max(0, (a.elapsed_seconds || 0) - (a.moving_seconds || 0));
    const climbGain = climbs.reduce((s, c) => s + (c.gain_m || 0), 0);

    const tiles = [
      { label: '后程变化', value: ex.change_pct != null ? `${ex.change_pct > 0 ? '+' : ''}${ex.change_pct}%` : '—',
        hint: ex.label || '前后半程对比' },
      { label: '爬坡', value: climbs.length ? `${climbs.length} 段` : '无',
        hint: climbs.length ? `累计爬升 ${Math.round(climbGain)} m` : '没有 ≥300 m 的持续爬坡' },
      { label: '停顿时间', value: U.dur(stopped), hint: '等红灯、补给、拍照' },
    ];

    const climbRows = climbs.map((c, i) => {
      const from = (c.start_m / 1000).toFixed(1);
      const to = (c.end_m / 1000).toFixed(1);
      return `<tr>
        <td class="mono dim">${i + 1}</td>
        <td class="mono">K${from} – K${to}</td>
        <td class="mono">${(c.length_m / 1000).toFixed(1)} km</td>
        <td class="mono"><b>+${Math.round(c.gain_m)} m</b></td>
        <td class="mono">${c.avg_grade_pct.toFixed(1)}%</td>
        <td class="mono dim">${U.esc(c.label || '')}</td>
      </tr>`;
    }).join('');

    return `<div class="section">
      <div class="section-head"><h2>本次分析</h2>
        <span class="dim">${U.esc(rep.sport_label || '')} · 结论全部由逐点数据算出，不做主观打分</span></div>

      <div class="card" style="padding:18px">
        <p class="muted" style="font-size:13px;line-height:1.85;margin:0">${U.esc(v.body || v.headline || '')}</p>
        <div class="grid g-3" style="margin-top:14px;gap:10px">
          ${tiles.map(t => `<div style="background:var(--bg-elev-2);border-radius:10px;padding:12px">
            <div class="tiny dim">${t.label}</div>
            <div style="font-size:19px;font-weight:680;margin-top:2px">${U.esc(t.value)}</div>
            <div class="tiny dim" style="margin-top:2px;line-height:1.5">${U.esc(t.hint)}</div>
          </div>`).join('')}
        </div>
      </div>

      ${climbs.length ? `<div class="card section">
        <div class="card-head"><h3>爬坡</h3>
          <span class="dim">连续 ≥300 米且平均坡度 ≥3% 的段落，按爬升量排序</span></div>
        <div style="overflow-x:auto">
          <table class="data-table">
            <thead><tr><th>#</th><th>里程区间</th><th>长度</th><th>爬升</th><th>平均坡度</th><th>等级</th></tr></thead>
            <tbody>${climbRows}</tbody>
          </table>
        </div>
      </div>` : ''}

      ${q.issues && q.issues.length ? `
        <div class="card section" style="padding:14px 16px">
          <div class="tiny dim" style="margin-bottom:5px">数据质量提醒</div>
          ${q.issues.map(i => `<div class="tiny" style="color:var(--warn);margin-top:3px">· ${U.esc(i.text)}</div>`).join('')}
        </div>` : ''}
    </div>`;
  }

  /* ---------------------------------------------------------- 入口 */

  async function load(root, params) {
    cleanup();
    host = root;
    const id = params && params.id;
    root.innerHTML = UI.spinner('读取活动…');
    if (!id) {
      root.innerHTML = `<div class="card">${UI.empty('info', '活动不存在', '链接可能已失效。',
        `<a class="btn" href="#/activities">返回列表</a>`)}</div>`;
      return;
    }
    try {
      const a = await api.activity(id);
      state.activity = a;
      render(a);
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty('info', '加载失败', U.esc(err.message),
        `<a class="btn" href="#/activities">返回列表</a>`)}</div>`;
    }
  }

  document.addEventListener('pace:imported', () => {});
  window.PageDetail = { load, cleanup };
})();
