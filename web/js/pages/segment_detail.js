/* 分段详情：路线、成绩榜、每次尝试的用时曲线。 */
(function () {
  const state = { segment: null, map: null, charts: [] };
  let host = null;

  function cleanup() {
    state.charts.forEach(c => c && c.destroy());
    state.charts = [];
    if (state.map) { state.map.destroy(); state.map = null; }
  }

  function paceText(seg, elapsed) {
    if (!seg.distance_m) return '—';
    const fam = seg.sport_family;
    if (fam === 'ride') return `${(seg.distance_m / elapsed * 3.6).toFixed(1)} km/h`;
    if (fam === 'swim') return `${U.pace(elapsed / (seg.distance_m / 100))}/100m`;
    return `${U.pace(elapsed / (seg.distance_m / 1000))}/km`;
  }

  function render(seg) {
    const efforts = seg.efforts || [];
    const best = efforts[0];
    const rows = efforts.map((e, i) => {
      const gap = best ? e.elapsed_s - best.elapsed_s : 0;
      const gapTxt = i === 0 ? '<span class="pr-badge">最佳</span>'
        : `<span class="dim">+${U.dur(gap)}</span>`;
      return `<tr>
        <td class="mono dim">${i + 1}</td>
        <td><a href="#/activity/${e.activity_id}">${U.esc(e.title || '活动 ' + e.activity_id)}</a></td>
        <td class="dim">${U.dateShort(e.start_time)}</td>
        <td class="mono"><b>${U.dur(e.elapsed_s)}</b> ${gapTxt}</td>
        <td class="mono dim">${paceText(seg, e.elapsed_s)}</td>
        <td class="mono dim">${e.avg_hr ? Math.round(e.avg_hr) : '—'}</td>
      </tr>`;
    }).join('');

    const chartSeries = efforts.slice().reverse().map((e, i) => e.elapsed_s);
    const chartLabels = efforts.slice().reverse().map(e => U.dateShort(e.start_time));

    host.innerHTML = `
      <div class="section-head" style="margin-top:0">
        <a class="btn btn-sm btn-ghost" href="#/segments">${Icon.get('back')}全部分段</a>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btnSegRebuild">${Icon.get('spark')}重新匹配</button>
        <button class="btn btn-sm btn-danger" id="btnSegDelete">${Icon.get('trash')}删除</button>
      </div>

      <div class="detail-hero">
        <div class="row" style="gap:8px;margin-bottom:8px">
          <span class="chip">${Icon.family(seg.sport_family)}${U.familyLabel(seg.sport_family)}</span>
          <span class="chip">${efforts.length} 次尝试</span>
        </div>
        <h1>${U.esc(seg.name)}</h1>
        <div class="detail-grid">
          <div class="detail-metric"><span>距离</span><b>${U.km(seg.distance_m, 2)}<small>km</small></b></div>
          <div class="detail-metric"><span>爬升</span><b>${U.elev(seg.elevation_gain_m)}<small>m</small></b></div>
          <div class="detail-metric"><span>平均坡度</span><b>${seg.avg_grade_pct != null ? seg.avg_grade_pct : '—'}<small>%</small></b></div>
          <div class="detail-metric"><span>最好成绩</span><b>${best ? U.dur(best.elapsed_s) : '—'}</b></div>
          <div class="detail-metric"><span>最佳配速</span><b>${best ? paceText(seg, best.elapsed_s) : '—'}</b></div>
        </div>
      </div>

      <div class="detail-cols">
        <div class="map-wrap card" style="padding:0;overflow:hidden">
          <div id="segMapHost" style="position:relative"></div>
        </div>
        <div class="card">
          <div class="card-head"><h3>成绩趋势</h3><span class="dim">越靠下越快</span></div>
          <div class="card-body">
            <div class="chart-holder"><canvas id="segChart"></canvas></div>
          </div>
        </div>
      </div>

      <div class="card section">
        <div class="card-head"><h3>成绩榜</h3><span class="dim">所有经过该路段的记录</span></div>
        ${efforts.length ? `<div style="overflow-x:auto"><table class="data-table">
          <thead><tr><th>#</th><th>活动</th><th>日期</th><th>用时</th><th>配速</th><th>心率</th></tr></thead>
          <tbody>${rows}</tbody></table></div>`
          : UI.empty('trophy', '还没有成绩', '这条分段创建后，还没有活动匹配到它。试试点击"重新匹配"，或用相同的路线再跑一次。')}
      </div>
      <p class="tiny dim" style="margin-top:16px;text-align:center">
        匹配基于起终点 45 米容差与路径长度一致性，跨活动自动识别。
      </p>`;

    Icon.hydrate(host);

    if (seg.polyline_points && seg.polyline_points.length > 1) {
      const mapCfg = (window.PaceApp && window.PaceApp.mapConfig) || { url: '', gcj: false };
      state.map = new TrackMap(document.getElementById('segMapHost'), {
        height: 300,
        tileUrl: mapCfg.url || '',
        gcj: !!mapCfg.gcj,
      });
      // 用线段取代单点轨迹：polyline_points 已经是 [lat,lon]
      state.map.setRoute(seg.polyline_points);
    } else {
      const el = document.getElementById('segMapHost');
      if (el) el.innerHTML = '<div class="empty"><p class="dim">这条分段没有保存轨迹</p></div>';
    }

    if (chartSeries.length > 1) {
      const chart = new Chart(document.getElementById('segChart'), {
        type: 'line', height: 220, invertY: true,
        xLabels: chartLabels,
        series: [{ key: 't', data: chartSeries, color: 'var(--accent)', fill: true, fillOpacity: 0.14, axis: 0 }],
        axes: [{ unit: 'sec', pad: 0.1 }],
        legend: false,
        tooltip: (i) => `<b>${chartLabels[i]}</b><br>用时 ${U.dur(chartSeries[i])}<br>${paceText(seg, chartSeries[i])}`,
      });
      state.charts.push(chart);
    } else if (best) {
      const holder = document.getElementById('segChart');
      if (holder && holder.parentElement) {
        holder.parentElement.innerHTML = `<div class="empty" style="padding:36px 10px">
          <p class="muted">只有 1 次尝试</p>
          <p class="tiny dim">再跑一次相同路线，或导入更多活动，这里会出现成绩趋势。</p>
          <div style="font-size:24px;font-weight:680;font-variant-numeric:tabular-nums">${U.dur(best.elapsed_s)}</div>
        </div>`;
      }
    }

    document.getElementById('btnSegDelete').addEventListener('click', async () => {
      if (!await UI.confirmDialog('删除分段', '只会删除分段定义与成绩，不影响活动本身。', '删除', true)) return;
      try {
        await api.deleteSegment(seg.id);
        UI.toast('已删除', 'ok');
        location.hash = '#/segments';
      } catch (err) { UI.toast('失败：' + U.esc(err.message), 'err'); }
    });

    document.getElementById('btnSegRebuild').addEventListener('click', async () => {
      try {
        const r = await api.rebuildSegment(seg.id);
        UI.toast(`重新匹配完成，共 ${r.matched} 次`, 'ok');
        load(host, { id: seg.id });
      } catch (err) { UI.toast('失败：' + U.esc(err.message), 'err'); }
    });
  }

  async function load(root, params) {
    cleanup();
    host = root;
    const id = params && params.id;
    root.innerHTML = UI.spinner('读取分段…');
    try {
      const seg = await api.segment(id);
      state.segment = seg;
      render(seg);
      document.getElementById('pageTitle').textContent = seg.name;
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty('info', '加载失败', U.esc(err.message),
        `<a class="btn" href="#/segments">返回分段列表</a>`)}</div>`;
    }
  }

  window.PageSegmentDetail = { load, cleanup };
})();
