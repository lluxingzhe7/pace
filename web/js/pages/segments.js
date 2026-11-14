/* 分段成绩：列表、详情、从活动创建、自动推荐。 */
(function () {
  const state = { segments: [], suggestions: [] };
  let host = null;

  function fmtHero(seg) {
    return U.familyLabel(seg.sport_family);
  }

  function cardHtml(s) {
    const best = s.best_effort;
    const fam = s.sport_family;
    const bestText = best ? U.dur(best.elapsed_s) : '—';
    const pace = best && s.distance_m
      ? (fam === 'ride' ? (s.distance_m / best.elapsed_s * 3.6).toFixed(1) + ' km/h'
        : fam === 'swim' ? U.pace(best.elapsed_s / (s.distance_m / 100)) + '/100m'
          : U.pace(best.elapsed_s / (s.distance_m / 1000)) + '/km')
      : '—';
    return `
      <a class="card seg-card" href="#/segment/${s.id}">
        <div class="row between">
          <h3>${U.esc(s.name)}</h3>
          <span class="chip">${Icon.family(fam)}${fmtHero(s)}</span>
        </div>
        <div class="seg-stats">
          <div><span>距离</span><b>${U.km(s.distance_m, 2)} km</b></div>
          <div><span>爬升</span><b>${U.elev(s.elevation_gain_m)} m</b></div>
          <div><span>平均坡度</span><b>${s.avg_grade_pct != null ? s.avg_grade_pct + '%' : '—'}</b></div>
        </div>
        <div class="row between" style="border-top:1px solid var(--line-soft);padding-top:12px">
          <div>
            <div class="tiny dim">最好成绩</div>
            <div style="font-size:19px;font-weight:680;font-variant-numeric:tabular-nums">${bestText}</div>
          </div>
          <div style="text-align:right">
            <div class="tiny dim">${pace}</div>
            <div class="tiny dim">${s.effort_count || 0} 次尝试</div>
          </div>
        </div>
      </a>`;
  }

  function render() {
    const segs = state.segments;
    const sug = state.suggestions || [];
    host.innerHTML = `
      <div class="section-head" style="margin-top:0">
        <h2>分段成绩</h2>
        <span class="dim">在你自己跑过的路线上定义路段，跨活动比较用时</span>
        <div class="spacer"></div>
        <button class="btn btn-primary btn-sm" id="btnNewSeg">${Icon.get('pin')}新建分段</button>
      </div>

      ${segs.length ? `<div class="grid g-auto">${segs.map(cardHtml).join('')}</div>`
        : `<div class="card">${UI.empty('segment', '还没有分段',
            '分段是从你的轨迹里截取的一段路线（比如"公园一圈"），之后每次经过它都会被自动计时，生成和 Strava Segment 类似的成绩榜。',
            `<button class="btn btn-primary" onclick="document.getElementById('btnNewSeg').click()">从活动创建第一个分段</button>`)}</div>`}

      ${sug.length ? `
        <div class="section">
          <div class="section-head"><h2>发现重复路线</h2>
            <span class="dim">这些起终点组合你走过多次，建议设为分段</span></div>
          <div class="grid g-3">
            ${sug.map((s, i) => `
              <div class="card card-pad">
                <div class="row between">
                  <b>${U.esc(s.suggested_name)}</b>
                  <span class="chip chip-accent">${s.count} 次</span>
                </div>
                <p class="tiny dim" style="margin-top:8px">
                  ${s.start[0].toFixed(4)}, ${s.start[1].toFixed(4)} → ${s.end[0].toFixed(4)}, ${s.end[1].toFixed(4)}
                </p>
                <button class="btn btn-sm" style="margin-top:10px" data-sug="${i}">用第一条活动创建</button>
              </div>`).join('')}
          </div>
        </div>` : ''}

      <div class="section">
        <div class="card card-pad">
          <div class="row between">
            <div>
              <b>重新匹配全部分段</b>
              <p class="tiny dim" style="margin-top:3px">导入了新活动、或调整了匹配容差后，可以重算所有历史成绩。</p>
            </div>
            <button class="btn btn-sm" id="btnRebuild">重新计算</button>
          </div>
        </div>
      </div>`;

    Icon.hydrate(host);

    document.getElementById('btnNewSeg').addEventListener('click', openCreator);
    document.getElementById('btnRebuild').addEventListener('click', async () => {
      try {
        const r = await api.rebuildAllSegments();
        UI.toast(`已重新匹配，共 ${r.matched} 条成绩`, 'ok');
        load(host);
      } catch (err) {
        UI.toast('失败：' + U.esc(err.message), 'err');
      }
    });
    U.$$('[data-sug]', host).forEach(btn => {
      btn.addEventListener('click', () => {
        const s = state.suggestions[Number(btn.dataset.sug)];
        createFromActivity(s.activity_ids[0], 0, 1, s.suggested_name);
      });
    });
  }

  /* ---------------------------------------------------------- 创建 */

  async function openCreator() {
    // 选一条最近的有 GPS 的活动
    let acts = [];
    try {
      const res = await api.activities({ limit: 40 });
      acts = res.items.filter(a => a.has_gps);
    } catch (err) {
      UI.toast('无法加载活动列表', 'err');
      return;
    }
    if (!acts.length) {
      UI.toast('还没有带 GPS 的活动，先导入一个 GPX/FIT 文件', 'warn');
      return;
    }
    const options = acts.map(a => `<option value="${a.id}">${U.dateShort(a.start_time)} · ${U.esc(a.title || U.sportLabel(a.sport))} · ${U.km(a.distance_m)} km</option>`).join('');

    const dlg = document.createElement('dialog');
    dlg.className = 'dialog';
    dlg.innerHTML = `
      <form method="dialog">
        <div class="dialog-head">
          <h2>新建分段</h2>
          <button class="icon-btn" value="cancel">${Icon.get('x')}</button>
        </div>
        <div class="dialog-body">
          <label class="tiny dim">选择活动
            <select id="segAct" class="control" style="width:100%;margin-top:6px">${options}</select>
          </label>
          <div class="grid g-2">
            <label class="tiny dim">起点（占全程 %）
              <input id="segStart" type="number" min="0" max="99" value="0" class="control" style="width:100%;margin-top:6px">
            </label>
            <label class="tiny dim">终点（占全程 %）
              <input id="segEnd" type="number" min="1" max="100" value="50" class="control" style="width:100%;margin-top:6px">
            </label>
          </div>
          <label class="tiny dim">名称
            <input id="segName" class="control" placeholder="例如：公园一圈" style="width:100%;margin-top:6px">
          </label>
          <p class="tiny dim">技巧：跑步按距离切分最自然。比如一条 10km 路线，取 0%–50% 就是前 5km。</p>
        </div>
        <div class="dialog-foot">
          <button class="btn" value="cancel">取消</button>
          <button class="btn btn-primary" value="ok">创建</button>
        </div>
      </form>`;
    document.body.appendChild(dlg);
    Icon.hydrate(dlg);
    dlg.addEventListener('close', async () => {
      if (dlg.returnValue !== 'ok') { dlg.remove(); return; }
      const actId = Number(document.getElementById('segAct').value);
      const startFrac = Number(document.getElementById('segStart').value) / 100;
      const endFrac = Number(document.getElementById('segEnd').value) / 100;
      const name = document.getElementById('segName').value.trim();
      dlg.remove();
      if (endFrac <= startFrac) { UI.toast('终点必须大于起点', 'err'); return; }
      await createFromActivity(actId, startFrac, endFrac, name);
    });
    dlg.showModal();
  }

  async function createFromActivity(activityId, startFrac, endFrac, name) {
    try {
      const act = await api.activity(activityId);
      const n = (act.route || []).length;
      if (n < 3) { UI.toast('这条活动没有足够的轨迹点', 'err'); return; }
      const payload = {
        activity_id: activityId,
        start_index: Math.round(n * startFrac),
        end_index: Math.max(Math.round(n * startFrac) + 1, Math.round(n * endFrac)),
        name: name || `${U.dateShort(act.start_time)} ${U.sportLabel(act.sport)} 分段`,
      };
      const r = await api.createSegment(payload);
      UI.toast(`分段已创建，匹配到 ${r.matched} 次成绩`, 'ok');
      await load(host);
    } catch (err) {
      UI.toast('创建失败：' + U.esc(err.message), 'err');
    }
  }

  async function load(root) {
    host = root;
    root.innerHTML = UI.spinner('读取分段…');
    try {
      const [segs, sug] = await Promise.all([api.segments(), api.suggestSegments().catch(() => ({ suggestions: [] }))]);
      state.segments = segs.segments || [];
      state.suggestions = sug.suggestions || [];
      render();
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty('info', '加载失败', U.esc(err.message))}</div>`;
    }
  }

  window.PageSegments = { load };
})();
