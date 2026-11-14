/* 仪表盘：周期汇总、12 周趋势、体能/疲劳、运动分布、热力图、近期最佳。 */
(function () {
  const state = { days: 28, sport: 'all', charts: [], data: null, calMonth: null };

  function destroyCharts() {
    state.charts.forEach(c => c && c.destroy());
    state.charts = [];
  }

  function buildDemoGpx() {
    const pts = [];
    const start = Math.floor(Date.now() / 1000) - 3600;
    for (let i = 0; i < 900; i++) {
      const t = start + i;
      const lat = 23.10 + i * 0.00012;
      const lon = 113.30 + Math.sin(i / 45) * 0.003;
      const ele = 25 + Math.sin(i / 50) * 18 + i * 0.02;
      const hr = 128 + Math.round(Math.sin(i / 35) * 22);
      pts.push('<trkpt lat="' + lat.toFixed(6) + '" lon="' + lon.toFixed(6) + '"><ele>'
        + ele.toFixed(1) + '</ele><time>' + new Date(t * 1000).toISOString()
        + '</time><extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>' + hr
        + '</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions></trkpt>');
    }
    return '<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="PACE demo" '
      + 'xmlns="http://www.topografix.com/GPX/1/1" '
      + 'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
      + '<trk><name>示例骑行</name><type>cycling</type><trkseg>' + pts.join('') + '</trkseg></trk></gpx>';
  }

  async function startDemo() {
    const btn = document.getElementById('btnDemo');
    if (btn) { btn.disabled = true; btn.textContent = '正在生成…'; }
    try {
      const gpx = buildDemoGpx();
      const file = new File([gpx], '示例-骑行.gpx', { type: 'application/gpx+xml' });
      const res = await api.importFiles([file]);
      const ok = Array.isArray(res) && res.some(r => r.status === 'ok');
      UI.toast(ok ? '示例数据已导入，正在刷新…' : '示例导入失败', ok ? 'ok' : 'err');
      if (ok && window.Router) Router.rerender();
    } catch (err) {
      UI.toast('生成示例失败：' + err.message, 'err');
      if (btn) { btn.disabled = false; btn.textContent = '先看示例数据'; }
    }
  }

  function heroHtml(d) {
    const p = d.period || {};
    const delta = d.delta || {};
    const fit = d.fitness || {};
    const status = U.trainingStatus(fit.status);
    const ctlMax = Math.max(fit.ctl || 0, fit.atl || 0, 1);
    const ctlW = Math.round(((fit.ctl || 0) / (ctlMax * 1.15)) * 100);
    const atlW = Math.round(((fit.atl || 0) / (ctlMax * 1.15)) * 100);

    return `
    <div class="hero">
      <div class="hero-main">
        <div class="row between">
          <div>
            <h1>最近 ${d.period_days} 天</h1>
            <p class="lead">共 <b>${p.count || 0}</b> 次活动 · 连续训练 <b>${d.streak_days || 0}</b> 天${
              d.activity_count ? ` · 全部 ${d.activity_count} 条记录` : ''}</p>
          </div>
          <a class="btn btn-sm" href="#/activities">查看全部 ${Icon.get('chevron')}</a>
        </div>
        <div class="hero-metrics">
          <div class="hero-metric">
            <b>${U.km(p.distance_m || 0, 1)}<small style="font-size:12px;color:var(--text-3)"> km</small></b>
            <span>距离 ${U.deltaPct(delta.distance_pct)}</span>
          </div>
          <div class="hero-metric">
            <b>${U.dur(p.moving_seconds || 0)}</b>
            <span>移动时间 ${U.deltaPct(delta.moving_pct)}</span>
          </div>
          <div class="hero-metric">
            <b>${U.num(p.elevation_gain_m || 0)}<small style="font-size:12px;color:var(--text-3)"> m</small></b>
            <span>累计爬升 ${U.deltaPct(delta.elevation_pct)}</span>
          </div>
          <div class="hero-metric">
            <b>${U.num(p.training_load || 0)}</b>
            <span>训练负荷 ${U.deltaPct(delta.load_pct)}</span>
          </div>
        </div>
        <div class="row" style="margin-top:16px;gap:18px;flex-wrap:wrap">
          <div class="tiny dim">本周 <b style="color:var(--text)">${U.km(d.week?.distance_m || 0, 1)} km</b> · ${U.dur(d.week?.moving_seconds || 0)}</div>
          <div class="tiny dim">本月 <b style="color:var(--text)">${U.km(d.month?.distance_m || 0, 1)} km</b> · ${U.dur(d.month?.moving_seconds || 0)}</div>
        </div>
      </div>

      <div class="card fitness-card">
        <div class="row between">
          <div>
            <div class="stat-label">训练状态</div>
            <div style="font-size:17px;font-weight:680;margin-top:2px" class="${status.tone === 'good' ? 'delta up' : status.tone === 'bad' ? 'delta down' : ''}">${status.label}</div>
          </div>
          <span class="chip ${status.tone === 'good' ? 'chip-accent' : ''}">TSB ${U.num(fit.tsb || 0, 1)}</span>
        </div>
        <div>
          <div class="fitness-row tiny"><span class="dim">体能 CTL</span><b>${U.num(fit.ctl || 0, 1)}</b></div>
          <div class="fitness-bar"><i style="width:${U.clamp(ctlW, 2, 100)}%;background:var(--ride)"></i></div>
        </div>
        <div>
          <div class="fitness-row tiny"><span class="dim">疲劳 ATL</span><b>${U.num(fit.atl || 0, 1)}</b></div>
          <div class="fitness-bar"><i style="width:${U.clamp(atlW, 2, 100)}%;background:var(--accent)"></i></div>
        </div>
        <p class="tiny dim" style="margin-top:2px">
          体能 = 42 天负荷均值，疲劳 = 7 天均值，状态 = 两者之差。
        </p>
      </div>
    </div>`;
  }

  function weeklyCard() {
    return `
    <div class="card">
      <div class="card-head">
        <h3>最近 12 周</h3>
        <span class="dim" id="weeklyNote">按周汇总的移动时间</span>
        <div class="spacer"></div>
        <div class="seg" id="weeklyMetric">
          <button data-metric="moving" class="active">时长</button>
          <button data-metric="distance">距离</button>
          <button data-metric="training">负荷</button>
        </div>
      </div>
      <div class="card-body">
        <div class="chart-holder"><canvas id="weeklyChart"></canvas></div>
      </div>
    </div>`;
  }

  function fitCard() {
    return `
    <div class="card">
      <div class="card-head">
        <h3>体能 / 疲劳</h3>
        <span class="dim">近 120 天</span>
      </div>
      <div class="card-body">
        <div class="chart-holder"><canvas id="fitChart"></canvas></div>
      </div>
    </div>`;
  }

  function sportCard(d) {
    const rows = (d.sport_breakdown || []).map(s => {
      const total = (d.sport_breakdown || []).reduce((acc, x) => acc + x.distance_m, 0) || 1;
      const pct = Math.round((s.distance_m / total) * 100);
      return `
        <div style="margin-bottom:12px">
          <div class="row between tiny" style="margin-bottom:4px">
            <span class="row" style="gap:6px;flex:none;white-space:nowrap">${Icon.family(s.family)}<b>${U.esc(s.label)}</b></span>
            <span class="dim mono" style="text-align:right">${U.km(s.distance_m, 0)} km · ${s.count} 次 · ${U.dur(s.moving_seconds)}</span>
          </div>
          <div class="zone-track"><i style="width:${Math.max(2, pct)}%;background:${U.familyColor(s.family)}"></i></div>
        </div>`;
    }).join('');
    return `
    <div class="card">
      <div class="card-head"><h3>运动分布</h3><span class="dim">近 90 天</span></div>
      <div class="card-body">${rows || '<p class="tiny dim">暂无数据</p>'}</div>
    </div>`;
  }

  const MONTH_NAMES = ['1 月', '2 月', '3 月', '4 月', '5 月', '6 月',
    '7 月', '8 月', '9 月', '10 月', '11 月', '12 月'];

  function calCard(d) {
    const byDate = {};
    (d.heatmap || []).forEach(c => { if (c && c.date) byDate[c.date] = c; });
    const dates = Object.keys(byDate).sort();
    if (!dates.length) return '';

    const month = state.calMonth || dates[dates.length - 1].slice(0, 7);
    const [y, m] = month.split('-').map(Number);
    const daysInMonth = new Date(y, m, 0).getDate();
    const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7;   // 周一开头
    const today = U.todayKey ? U.todayKey() : new Date().toISOString().slice(0, 10);
    const maxDist = Math.max(1, ...dates.map(k => byDate[k].distance_m || 0));

    let dist = 0, count = 0, secs = 0;
    const cells = [];
    for (let i = 0; i < lead; i++) cells.push('<div class="cal-cell is-blank"></div>');
    for (let day = 1; day <= daysInMonth; day++) {
      const key = `${y}-${String(m).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const raw = byDate[key];
      const c = raw && (raw.count || 0) > 0 ? raw : null;
      if (c) {
        dist += c.distance_m || 0;
        count += c.count || 0;
        secs += c.moving_seconds || 0;
      }
      const ratio = c ? (c.distance_m || 0) / maxDist : 0;
      const level = ratio === 0 ? 0 : ratio < 0.25 ? 1 : ratio < 0.5 ? 2 : ratio < 0.78 ? 3 : 4;
      const tip = c
        ? `${key} · ${U.km(c.distance_m, 1)} km · ${U.dur(c.moving_seconds)}${c.count > 1 ? ` · ${c.count} 次` : ''}`
        : `${key} · 休息`;
      cells.push(`<a class="cal-cell${c ? '' : ' is-rest'}${key === today ? ' is-today' : ''}" data-l="${level}"
        href="#/activities?day=${key}" title="${tip}">
        <span class="cal-day">${day}</span>
        ${c ? `<span class="cal-km">${U.km(c.distance_m, c.distance_m >= 10000 ? 0 : 1)}</span>
               <span class="cal-sub">km · ${U.dur(c.moving_seconds)}</span>` : ''}
      </a>`);
    }
    const canPrev = dates[0].slice(0, 7) < month;
    const canNext = dates[dates.length - 1].slice(0, 7) > month;

    return `
    <div class="card cal-card">
      <div class="card-head">
        <h3>训练日历</h3>
        <span class="dim">每天一格，深浅表示距离；点某天看当天记录</span>
        <div class="spacer"></div>
        <div class="row" style="gap:6px;align-items:center">
          <button class="icon-btn cal-nav" data-cal="-1" ${canPrev ? '' : 'disabled'} aria-label="上个月">${Icon.get('back')}</button>
          <b style="min-width:86px;text-align:center">${y} 年 ${MONTH_NAMES[m - 1]}</b>
          <button class="icon-btn cal-nav" data-cal="1" ${canNext ? '' : 'disabled'} aria-label="下个月">${Icon.get('chevron')}</button>
        </div>
      </div>
      <div class="card-body">
        <div class="cal-summary">
          <span><b>${m} 月</b> <b>${count}</b> 次</span>
          <span><b>${U.km(dist, 1)}</b> km</span>
          <span><b>${U.dur(secs)}</b></span>
          <div class="spacer"></div>
          <div class="heat-legend">少
            <i data-l="0"></i><i data-l="1"></i><i data-l="2"></i><i data-l="3"></i><i data-l="4"></i>多
          </div>
        </div>
        <div class="cal-week">${['一', '二', '三', '四', '五', '六', '日'].map(w => `<span>${w}</span>`).join('')}</div>
        <div class="cal-grid">${cells.join('')}</div>
      </div>
    </div>`;
  }

  function prCard(d) {
    const records = d.personal_records || [];
    if (!records.length) {
      return `<div class="card"><div class="card-head"><h3>个人最好成绩</h3></div>
        ${UI.empty('trophy', '还没有最佳成绩', '导入带 GPS 的跑步或骑行活动后，会自动计算 1km / 5km / 10km 等标准距离的最好用时。')}</div>`;
    }
    const rows = records.slice(0, 6).map(r => {
      const fam = r.family;
      return `<tr>
        <td style="white-space:nowrap">${Icon.family(fam)}</td>
        <td style="white-space:nowrap"><b>${U.esc(r.label)}</b></td>
        <td class="mono" style="white-space:nowrap"><b>${U.dur(r.seconds)}</b></td>
        <td class="mono dim" style="white-space:nowrap">${fam === 'ride' ? (r.seconds ? (r.distance_m / r.seconds * 3.6).toFixed(1) + ' km/h' : '—')
          : fam === 'swim' ? U.pace(r.pace_seconds) + '/100m' : U.pace(r.pace_seconds) + '/km'}</td>
        <td class="dim" style="white-space:nowrap">${U.dateShort(r.date)}</td>
        <td><a class="dim" href="#/activity/${r.activity_id}">${U.esc(r.title || '查看')}</a></td>
      </tr>`;
    }).join('');
    return `
    <div class="card">
      <div class="card-head"><h3>个人最好成绩</h3><div class="spacer"></div>
        <a class="btn btn-sm btn-ghost" href="#/segments">分段与记录${Icon.get('chevron')}</a></div>
      <div style="overflow-x:auto">
        <table class="data-table">
          <thead><tr><th></th><th>距离</th><th>用时</th><th>配速/速度</th><th>日期</th><th>活动</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
  }

  function render(root) {
    destroyCharts();
    const d = state.data;
    const empty = !d || !d.activity_count;
    if (empty) {
      root.innerHTML = `
        <div class="onboard">
          <div class="onboard-art">${Icon.get('cloud')}</div>
          <h1>开始你的第一条记录</h1>
          <p>把码表或手机里的骑行 / 跑步文件导进来，几秒后就能看到配速、心率、爬升与训练负荷。</p>
          <div class="onboard-actions">
            <button class="btn btn-primary" onclick="UI.Importer.open()">${Icon.get('upload')}导入活动</button>
            <button class="btn" id="btnDemo">先看示例数据</button>
            <a class="btn btn-ghost" href="#/settings">导入数据包</a>
          </div>
          <ol class="onboard-steps">
            <li><b>1 · 导入</b>GPX / FIT / TCX / CSV</li>
            <li><b>2 · 分析</b>配速、心率区间、训练负荷</li>
            <li><b>3 · 教练</b>按目标生成训练计划</li>
          </ol>
          <p class="onboard-foot">数据只存在这台设备的浏览器里 · 与电脑版不互通，可用「设置 → 导出 / 导入数据包」迁移</p>
        </div>`;
      const demoBtn = document.getElementById('btnDemo');
      if (demoBtn) demoBtn.addEventListener('click', startDemo);
      return;
    }

    root.innerHTML = `
      ${heroHtml(d)}
      <div class="grid g-2 section" style="grid-template-columns:minmax(0,1.5fr) minmax(0,1fr)">
        ${weeklyCard()}
        ${fitCard()}
      </div>
      <div class="grid g-2 section">
        ${sportCard(d)}
        ${prCard(d)}
      </div>
      <div class="section">${calCard(d)}</div>
      <p class="tiny dim" style="margin-top:16px;text-align:center">
        数据全部保存在本机 SQLite 文件中，不会上传到任何服务器。
      </p>`;

    Icon.hydrate(root);
    wireWeekly(d);
    wireFit(d);
    wireCal();
  }

  function wireCal() {
    document.querySelectorAll('[data-cal]').forEach(btn => {
      btn.addEventListener('click', () => {
        const [y, m] = (state.calMonth || new Date().toISOString().slice(0, 7)).split('-').map(Number);
        const next = new Date(y, m - 1 + Number(btn.dataset.cal), 1);
        state.calMonth = `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, '0')}`;
        Router.render();
      });
    });
  }

  function wireWeekly(d) {
    const canvas = document.getElementById('weeklyChart');
    if (!canvas) return;
    let metric = 'moving';
    const note = document.getElementById('weeklyNote');
    let weeklyChart = null;
    const build = () => {
      if (weeklyChart) weeklyChart.destroy();
      const buckets = d.weekly_buckets || [];
      const values = buckets.map(b => metric === 'moving' ? b.moving_seconds / 3600
        : metric === 'distance' ? b.distance_m / 1000 : b.training_load);
      weeklyChart = new Chart(canvas, {
        type: 'bar',
        height: 210,
        xLabels: buckets.map(b => b.label),
        series: [{ key: 'v', label: metric === 'moving' ? '小时' : metric === 'distance' ? '公里' : '负荷',
          data: values, color: 'var(--accent)', type: 'bar', opacity: 0.75, axis: 0 }],
        axes: [{ unit: metric === 'moving' ? 'h' : metric === 'distance' ? 'km' : '', min: 0 }],
        legend: false,
        tooltip: (i) => {
          const b = buckets[i];
          if (!b) return '';
          return `<b>${b.label} 起一周</b><br>${b.count} 次 · ${U.km(b.distance_m, 1)} km<br>${U.dur(b.moving_seconds)} · 负荷 ${U.num(b.training_load)}`;
        },
      });
      state.charts.push(weeklyChart);
      if (note) {
        note.textContent = metric === 'moving' ? '按周汇总的移动时间'
          : metric === 'distance' ? '按周汇总的距离' : '按周汇总的训练负荷';
      }
    };
    document.querySelectorAll('#weeklyMetric button').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#weeklyMetric button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        metric = btn.dataset.metric;
        build();
      });
    });
    build();
  }

  function wireFit(d) {
    const canvas = document.getElementById('fitChart');
    if (!canvas) return;
    const series = d.load_series || [];
    const chart = new Chart(canvas, {
      type: 'line',
      height: 210,
      xLabels: series.map(s => s.date.slice(5)),
      series: [
        // 左轴：体能/疲劳曲线（量级 0~60）；右轴：每日负荷柱与状态（量级 0~200）
        { key: 'load', label: '每日负荷', data: series.map(s => s.load), color: 'var(--text-3)', type: 'bar', opacity: 0.30, axis: 1 },
        { key: 'ctl', label: '体能', data: series.map(s => s.ctl), color: 'var(--ride)', axis: 0, width: 2 },
        { key: 'atl', label: '疲劳', data: series.map(s => s.atl), color: 'var(--accent)', axis: 0, width: 1.6 },
        { key: 'tsb', label: '状态', data: series.map(s => s.tsb), color: 'var(--good)', axis: 1, dash: [4, 3], width: 1.4 },
      ],
      axes: [{ min: 0, side: 'left' }, { min: 0, side: 'right' }],
      tooltip: (i) => {
        const s = series[i];
        if (!s) return '';
        return `<b>${s.date}</b><br>体能 ${U.num(s.ctl, 1)} · 疲劳 ${U.num(s.atl, 1)}<br>状态 ${U.num(s.tsb, 1)} · 当日负荷 ${U.num(s.load, 1)}`;
      },
    });
    state.charts.push(chart);
  }

  async function load(root, params) {
    root.innerHTML = UI.spinner('正在计算仪表盘…');
    try {
      const [dash] = await Promise.all([
        api.dashboard({ days: state.days, sport: state.sport }),
        api.health(),
      ]);
      state.data = dash;
      const countEl = document.getElementById('navActivityCount');
      if (countEl) countEl.textContent = dash.activity_count || '';
      render(root);
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty('info', '加载失败', U.esc(err.message),
        `<button class="btn" onclick="location.reload()">重试</button>`)}</div>`;
    }
  }

  function setSport(sport) {
    state.sport = sport;
  }

  window.PageDashboard = { load, setSport, get state() { return state; } };
})();
