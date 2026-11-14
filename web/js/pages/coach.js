/* 教练页：先定目标，再出计划，可随时提问。

   设计原则：不猜目标。没填目标就只显示数据评估，不出课程。
*/
(function () {
  const state = {
    report: null, intake: null, meta: null,
    program: null, thread: [], asking: false, openWeek: 1, busy: false,
  };
  let host = null;

  const HORIZONS = [
    { v: 1, label: "1 周" }, { v: 4, label: "1 个月" },
    { v: 13, label: "1 季度" }, { v: 26, label: "半年" },
  ];
  const DAYS = [2, 3, 4, 5, 6];
  const MINUTES = [30, 45, 60, 90];

  const KIND = {
    rest: { label: "休息", color: "var(--text-3)" },
    easy: { label: "有氧", color: "var(--good)" },
    quality: { label: "质量", color: "var(--accent)" },
    long: { label: "长距离", color: "var(--ride)" },
    strength: { label: "力量", color: "var(--walk)" },
    race: { label: "比赛", color: "var(--bad)" },
  };

  const PRESETS = [
    "5 公里怎么跑进 25 分",
    "每周只有 3 天怎么安排",
    "膝盖有点疼还能练吗",
    "减脂要跑多少",
    "比赛前两周怎么减量",
    "力量训练怎么加",
  ];

  /* ---------------------------------------------------------------- 表单 */

  function formHtml() {
    const i = state.intake || {};
    const meta = state.meta || {};
    const goals = meta.goals || [];
    const levels = meta.levels || [];
    const cons = meta.constraints || [];
    const picked = new Set(i.constraints || []);
    return `
      <div class="card">
        <div class="card-head">
          <h3>目标与条件</h3>
          <span class="dim">先告诉我你要练什么，我才能排课</span>
        </div>
        <div class="card-body">
          <div class="row wrap" style="gap:6px;margin-bottom:12px">
            <span class="tiny dim">快速目标：</span>
            ${[["improve_pace", "跑步"], ["sportive", "骑行"], ["ride_base", "骑行打底"],
               ["ride_ftp", "FTP 间歇"], ["ride_climb", "爬坡"], ["ride_century", "百公里"],
               ["swim_1000", "游泳"], ["swim_openwater", "铁三/公开水域"]].map(([k, label]) =>
              `<button class="chip" data-goal="${k}" style="cursor:pointer">${label}</button>`).join("")}
          </div>
          <div class="grid g-2" style="gap:12px">
            <label class="tiny dim">目标
              <select class="control" id="ckGoal" style="width:100%;margin-top:5px">
                ${goalGroups(goals, i.goal)}
              </select>
            </label>
            <label class="tiny dim">周期
              <select class="control" id="ckHorizon" style="width:100%;margin-top:5px">
                ${HORIZONS.map(h => `<option value="${h.v}" ${Number(i.horizon_weeks) === h.v ? "selected" : ""}>${h.label}</option>`).join("")}
              </select>
            </label>
            <label class="tiny dim">每周可练天数
              <select class="control" id="ckDays" style="width:100%;margin-top:5px">
                ${DAYS.map(d => `<option value="${d}" ${Number(i.days_per_week) === d ? "selected" : ""}>${d} 天</option>`).join("")}
              </select>
            </label>
            <label class="tiny dim">单次时长
              <select class="control" id="ckMinutes" style="width:100%;margin-top:5px">
                ${MINUTES.map(m => `<option value="${m}" ${Number(i.session_minutes) === m ? "selected" : ""}>${m} 分钟</option>`).join("")}
              </select>
            </label>
            <label class="tiny dim">经验
              <select class="control" id="ckLevel" style="width:100%;margin-top:5px">
                ${levels.map(l => `<option value="${l.key}" ${i.level === l.key ? "selected" : ""}>${U.esc(l.label)}</option>`).join("")}
              </select>
            </label>
            <label class="tiny dim">目标日期（可选）
              <input class="control" id="ckDate" type="date" value="${U.esc(i.target_date || "")}"
                style="width:100%;margin-top:5px" ${i.horizon_weeks <= 4 ? "" : ""}>
            </label>
          </div>
          <div style="margin-top:12px">
            <div class="tiny dim" style="margin-bottom:6px">限制条件（可多选）</div>
            <div class="row wrap" style="gap:6px">
              ${cons.map(c => `
                <label class="chip" style="cursor:pointer;${picked.has(c.key) ? "color:var(--accent-2);background:var(--accent-soft)" : ""}">
                  <input type="checkbox" class="ckCons" value="${c.key}" ${picked.has(c.key) ? "checked" : ""}
                    style="accent-color:var(--accent);margin-right:4px">
                  ${U.esc(c.label)}
                </label>`).join("")}
            </div>
          </div>
          ${i.note ? `<p class="tiny dim" style="margin-top:10px">建议：${U.esc(i.note)}</p>` : ""}
          <div class="row" style="margin-top:14px;gap:8px">
            <button class="btn btn-primary" id="ckBuild">${Icon.get("spark")}生成计划</button>
            <span class="tiny dim" id="ckStatus"></span>
          </div>
        </div>
      </div>`;
  }

  /* ---------------------------------------------------------------- 计划 */

  function phaseBar(p) {
    const total = p.weeks.length;
    return `<div class="card" style="padding:16px">
      <div class="row between" style="margin-bottom:10px">
        <b style="font-size:13px">${U.esc(p.goal.label)} · ${p.horizon_weeks} 周</b>
        <span class="tiny dim">${U.esc(p.level_label)} · 每周 ${p.intake.days_per_week} 练 · 单次 ${p.intake.session_minutes} 分钟</span>
      </div>
      <div style="display:flex;gap:2px;height:8px;border-radius:5px;overflow:hidden">
        ${p.phases.map(ph => `
          <div title="${U.esc(ph.name)}（第${ph.weeks[0]}–${ph.weeks[ph.weeks.length - 1]}周）"
            style="flex:${ph.weeks.length};background:${phaseColor(ph.name)};opacity:.85"></div>`).join("")}
      </div>
      <div class="row wrap" style="gap:12px;margin-top:8px">
        ${p.phases.map(ph => `
          <span class="tiny dim"><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${phaseColor(ph.name)};margin-right:4px"></i>
            ${U.esc(ph.name)} · 第${ph.weeks[0]}–${ph.weeks[ph.weeks.length - 1]}周</span>`).join("")}
      </div>
      <div class="row wrap" style="gap:16px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line-soft)">
        <span class="tiny dim">当前 CTL <b style="color:var(--text)">${p.current.ctl}</b></span>
        <span class="tiny dim">TSB <b style="color:var(--text)">${p.current.tsb}</b></span>
        <span class="tiny dim">近期周均 <b style="color:var(--text)">${p.current.weekly_km} km</b></span>
      </div>
      ${(p.warnings || []).length ? `
        <div style="margin-top:10px">
          ${p.warnings.map(w => `<div class="tiny" style="color:var(--warn);margin-top:3px">· ${U.esc(w)}</div>`).join("")}
        </div>` : ""}
    </div>`;
  }

  function phaseColor(name) {
    if (name.includes("基础")) return "var(--ride)";
    if (name.includes("建设")) return "var(--good)";
    if (name.includes("强化")) return "var(--accent)";
    if (name.includes("减量")) return "var(--walk)";
    if (name.includes("恢复")) return "var(--text-3)";
    return "var(--accent)";
  }

  function goalGroups(goals, picked) {
    const order = [["run", "跑步"], ["ride", "骑行"], ["swim", "游泳"], ["other", "其他"]];
    return order.map(([fam, label]) => {
      const items = goals.filter(g => (g.family || "run") === fam);
      if (!items.length) return "";
      return `<optgroup label="${label}">${items.map(g =>
        `<option value="${g.key}" ${picked === g.key ? "selected" : ""}>${U.esc(g.label)}</option>`
      ).join("")}</optgroup>`;
    }).join("");
  }

  function weekCard(w, p) {
    const open = state.openWeek === w.index;
    const days = w.days.map(d => {
      const k = KIND[d.kind] || KIND.easy;
      return `<div class="card" style="padding:11px;${d.kind === "rest" ? "opacity:.55" : ""}">
        <div class="row between">
          <span class="tiny dim">${d.day}</span>
          <span class="chip" style="color:${k.color}">${k.label}</span>
        </div>
        <div style="font-size:12.5px;font-weight:600;margin-top:5px">${U.esc(d.title)}</div>
        ${d.duration_min ? `<div class="tiny dim" style="margin-top:2px">${d.duration_min} 分钟${d.distance_km ? ` · ${d.distance_km} km` : d.distance_m ? ` · ${d.distance_m} 米` : ""}</div>` : ""}
        ${d.target ? `<div class="tiny muted" style="margin-top:5px;line-height:1.55">${U.esc(d.target)}</div>` : ""}
        ${d.pace_hint ? `<div class="tiny mono" style="margin-top:4px;color:var(--accent-2)">${U.esc(d.pace_hint)}</div>` : ""}
      </div>`;
    }).join("");
    return `<div class="card" style="margin-top:10px">
      <button class="row between" data-week="${w.index}" style="width:100%;background:none;border:0;padding:14px 16px;cursor:pointer;text-align:left">
        <span class="row" style="gap:10px">
          <b style="font-size:13px">第 ${w.index} 周</b>
          <span class="chip">${U.esc(w.phase)}</span>
        </span>
        <span class="row" style="gap:12px">
          <span class="tiny dim">${w.target_km ? `${w.target_km} km · ` : w.target_m ? `${w.target_m} 米 · ` : ""}${Math.round(w.target_seconds / 60)} 分钟</span>
          <span class="tiny dim">${open ? "收起" : "展开"}</span>
        </span>
      </button>
      ${open ? `
        <div style="padding:0 16px 16px">
          <p class="tiny dim" style="margin-bottom:10px">${U.esc(w.focus)}</p>
          <div class="grid g-auto" style="grid-template-columns:repeat(auto-fill,minmax(160px,1fr))">${days}</div>
        </div>` : ""}
    </div>`;
  }

  function programHtml(p) {
    return `
      <div class="section">
        <div class="section-head"><h2>训练计划</h2>
          <span class="dim">按周期化排：基础 → 建设 → 强化 → 减量</span></div>
        ${phaseBar(p)}
        <div style="margin-top:6px">${p.weeks.map(w => weekCard(w, p)).join("")}</div>
        <div class="card" style="margin-top:12px;padding:16px">
          <b style="font-size:13px">硬性护栏</b>
          <ul class="tiny muted" style="margin:8px 0 0;padding-left:18px;line-height:1.9">
            ${(p.guardrails || []).map(g => `<li>${U.esc(g)}</li>`).join("")}
          </ul>
        </div>
      </div>`;
  }

  /* ---------------------------------------------------------------- 问答 */

  // 极简 Markdown：标题、粗体、列表、代码，够模型输出用，也不引第三方库
  function mdLite(text) {
    const esc = (s) => U.esc(s);
    const inline = (s) => esc(s)
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    const lines = String(text || "").replace(/\r/g, "").split("\n");
    const out = [];
    let list = null; // "ul" | "ol"
    const flush = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const raw of lines) {
      const line = raw.trimEnd();
      if (!line.trim()) { flush(); continue; }
      const h = line.match(/^#{1,4}\s+(.*)$/);
      if (h) {
        flush();
        out.push(`<div class="md-h">${inline(h[1])}</div>`);
        continue;
      }
      const ul = line.match(/^\s*[-*·]\s+(.*)$/);
      if (ul) {
        if (list !== "ul") { flush(); out.push('<ul class="md-ul">'); list = "ul"; }
        out.push(`<li>${inline(ul[1])}</li>`);
        continue;
      }
      const ol = line.match(/^\s*(\d+)[.、)]\s+(.*)$/);
      if (ol) {
        if (list !== "ol") { flush(); out.push('<ol class="md-ol">'); list = "ol"; }
        out.push(`<li>${inline(ol[2])}</li>`);
        continue;
      }
      flush();
      out.push(`<p class="md-p">${inline(line)}</p>`);
    }
    flush();
    return out.join("");
  }

  function answerMeta(a) {
    const bits = [];
    bits.push(a.source === "llm"
      ? `<span class="chip chip-accent">${Icon.get("spark")}AI · ${U.esc(a.model || "模型")}</span>`
      : `<span class="chip">本地规则引擎</span>`);
    if (a.elapsed_ms) bits.push(`<span class="tiny dim">${(a.elapsed_ms / 1000).toFixed(1)}s</span>`);
    if (a.usage && a.usage.total) {
      bits.push(`<span class="tiny dim">本次 ${a.usage.total} tokens（输入 ${a.usage.prompt} / 输出 ${a.usage.completion}）</span>`);
    } else if (a.source === "llm") {
      bits.push(`<span class="tiny dim">本次消耗见 DeepSeek 账单</span>`);
    } else {
      bits.push(`<span class="tiny dim">本地计算，不消耗 token</span>`);
    }
    return `<div class="row wrap" style="gap:8px;align-items:center">${bits.join("")}</div>`;
  }

  function answerBody(a) {
    if (a.source === "llm") return `<div class="md">${mdLite(a.answer)}</div>`;
    return `
      <h4 style="font-size:15px;margin:0 0 8px">${U.esc(a.title)}</h4>
      ${(a.sections || []).map(s => `
        <div style="margin-top:12px">
          <div class="tiny" style="color:var(--accent-2);font-weight:600">${U.esc(s.h)}</div>
          <p class="tiny muted" style="margin-top:4px;line-height:1.9;white-space:pre-line">${U.esc(s.p)}</p>
        </div>`).join("")}
      ${(a.actions || []).length ? `
        <div style="margin-top:12px">
          <div class="tiny" style="color:var(--accent-2);font-weight:600">接下来做</div>
          <ul class="tiny muted" style="margin:5px 0 0;padding-left:18px;line-height:1.9">
            ${a.actions.map(x => `<li>${U.esc(x)}</li>`).join("")}
          </ul>
        </div>` : ""}
      ${(a.data || []).length ? `
        <div class="row wrap" style="gap:8px;margin-top:12px">
          ${a.data.map(d => `<span class="chip">${U.esc(d.label)} ${U.esc(d.value)}</span>`).join("")}
        </div>` : ""}`;
  }

  function threadHtml() {
    if (!state.thread.length) {
      return `<div class="qa-empty tiny dim">还没有提问。上面选一个常见问题，或者直接输入——我会带着你的训练数据回答。</div>`;
    }
    return state.thread.map(item => `
      <div class="qa-item">
        <div class="qa-q"><span class="qa-avatar">你</span><span>${U.esc(item.q)}</span></div>
        <div class="qa-a">
          <span class="qa-avatar ai">${Icon.get("spark")}</span>
          <div style="flex:1;min-width:0">
            ${answerMeta(item.a)}
            <div style="margin-top:10px">${answerBody(item.a)}</div>
            ${item.a.caveat ? `<p class="tiny dim" style="margin-top:10px">${U.esc(item.a.caveat)}</p>` : ""}
            ${item.a.llm && item.a.llm.failed ? `<p class="tiny" style="margin-top:6px;color:var(--warn)">大模型调用失败（${U.esc(item.a.llm.error || "未知原因")}），以上是本地规则引擎的回答。</p>` : ""}
          </div>
        </div>
      </div>`).join("");
  }

  function askHtml() {
    const pending = state.asking;
    const used = state.thread.reduce((n, it) => n + ((it.a.usage && it.a.usage.total) || 0), 0);
    return `
      <div class="card section qa-card">
        <div class="card-head"><h3>问教练</h3>
          <span class="dim">结合你的训练数据分析；填了 API Key 走大模型，否则本地方案同样能用</span>
          <div class="spacer"></div>
          ${state.thread.length ? `<button class="btn btn-sm btn-ghost" id="ckClear">清空（${state.thread.length}）</button>` : ""}
        </div>
        <div class="card-body">
          <div class="row wrap" style="gap:6px;margin-bottom:12px">
            ${PRESETS.map(q => `<button class="chip" data-ask="${U.esc(q)}" style="cursor:pointer">${U.esc(q)}</button>`).join("")}
          </div>

          <div class="qa-thread" id="ckThread">${threadHtml()}</div>
          ${pending ? `<div class="qa-item"><div class="qa-a">
            <span class="qa-avatar ai">${Icon.get("spark")}</span>
            <div class="tiny dim" style="padding-top:6px">正在分析你的数据…</div></div></div>` : ""}

          <div class="qa-compose">
            <textarea class="control qa-input" id="ckQuestion" rows="2"
              placeholder="问点什么，比如：这周该练什么？我的爬坡短板在哪？下个月比赛怎么减量？"></textarea>
            <div class="qa-compose-foot">
              <span class="tiny dim">Enter 发送 · Shift+Enter 换行${used ? ` · 本次会话 ${used} tokens` : ""}</span>
              <button class="btn btn-primary btn-sm" id="ckAsk" ${pending ? "disabled" : ""}>${Icon.get("spark")}${pending ? "分析中…" : "提问"}</button>
            </div>
          </div>
        </div>
      </div>`;
  }

  /* ---------------------------------------------------------------- 评估 */

  function reportHtml(r) {
    if (!r) return "";
    const risks = r.risks || [], findings = r.findings || [], gaps = r.gaps || [];
    return `
      <details class="section">
        <summary style="cursor:pointer;font-size:16px;font-weight:650">
          数据评估：${U.esc(r.verdict.headline)}
          <span class="chip" style="margin-left:8px">${r.grade} 级 / ${Math.round(r.score)} 分</span>
          <span class="tiny dim" style="margin-left:6px">置信度 ${r.confidence.score}%</span>
        </summary>
        <div class="card" style="margin-top:10px;padding:16px">
          <p class="tiny muted" style="line-height:1.8">${U.esc(r.verdict.body)}</p>
          <div class="grid g-2" style="margin-top:14px;gap:12px">
            ${risks.length ? `<div><div class="tiny" style="color:var(--bad);font-weight:600">风险</div>
              ${risks.map(x => `<p class="tiny muted" style="margin-top:5px;line-height:1.7">· ${U.esc(x.title)}：${U.esc(x.detail)}</p>`).join("")}</div>` : ""}
            ${findings.length ? `<div><div class="tiny" style="color:var(--warn);font-weight:600">留意</div>
              ${findings.map(x => `<p class="tiny muted" style="margin-top:5px;line-height:1.7">· ${U.esc(x.title)}：${U.esc(x.detail)}</p>`).join("")}</div>` : ""}
          </div>
          ${gaps.length ? `<div style="margin-top:10px">
            <div class="tiny dim">缺什么</div>
            ${gaps.map(g => `<p class="tiny dim" style="margin-top:3px">· ${U.esc(g.title)}</p>`).join("")}
          </div>` : ""}
        </div>
      </details>`;
  }

  /* ---------------------------------------------------------------- 渲染 */

  function render() {
    const r = state.report;
    host.innerHTML = `
      <div class="toolbar" style="margin-bottom:14px">
        <h2 style="font-size:17px;margin-right:6px">教练</h2>
        <span class="dim tiny">先定目标，再出课表；也可以直接问我</span>
      </div>
      ${reportHtml(r)}
      <div class="section">${formHtml()}</div>
      ${state.program ? programHtml(state.program) : `
        <div class="card section">${UI.empty("segment", "还没有计划",
          "上面选好目标、周期、每周天数，点「生成计划」。我不会在你没说目标之前先排课——同一个体能水平，备赛 5km 和备赛全马的排法完全相反。")}</div>`}
      ${askHtml()}`;

    Icon.hydrate(host);
    wire();
  }

  function wire() {
    const build = document.getElementById("ckBuild");
    if (build) build.addEventListener("click", generate);
    document.querySelectorAll("[data-week]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = Number(btn.dataset.week);
        state.openWeek = state.openWeek === idx ? -1 : idx;
        render();
      });
    });
    const askBtn = document.getElementById("ckAsk");
    const input = document.getElementById("ckQuestion");
    if (askBtn) askBtn.addEventListener("click", () => submitQuestion(input ? input.value : ""));
    if (input) {
      input.addEventListener("keydown", (e) => {
        // Enter 直接发，Shift+Enter 换行；输入法组字时不拦截
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !e.ctrlKey && !e.altKey) {
          e.preventDefault();
          submitQuestion(input.value);
        }
      });
      input.addEventListener("input", () => {
        input.style.height = "auto";
        input.style.height = `${Math.min(160, Math.max(52, input.scrollHeight))}px`;
      });
    }
    const clearBtn = document.getElementById("ckClear");
    if (clearBtn) clearBtn.addEventListener("click", () => { state.thread = []; render(); });
    document.querySelectorAll("[data-goal]").forEach(btn => {
      btn.addEventListener("click", () => {
        const sel = document.getElementById("ckGoal");
        if (!sel) return;
        sel.value = btn.dataset.goal;
        const label = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].textContent : "";
        UI.toast(`目标已切到「${label}」，点生成计划看课表`, "ok");
      });
    });
    document.querySelectorAll("[data-ask]").forEach(btn => {
      btn.addEventListener("click", () => submitQuestion(btn.dataset.ask));
    });
  }

  function collectIntake() {
    const val = (id) => document.getElementById(id);
    const cons = Array.from(document.querySelectorAll(".ckCons:checked")).map(x => x.value);
    return {
      goal: val("ckGoal") ? val("ckGoal").value : "health",
      horizon_weeks: Number(val("ckHorizon") ? val("ckHorizon").value : 13),
      days_per_week: Number(val("ckDays") ? val("ckDays").value : 4),
      session_minutes: Number(val("ckMinutes") ? val("ckMinutes").value : 60),
      level: val("ckLevel") ? val("ckLevel").value : "intermediate",
      constraints: cons,
      target_date: val("ckDate") ? val("ckDate").value : "",
    };
  }

  async function generate() {
    if (state.busy) return;
    state.busy = true;
    const status = document.getElementById("ckStatus");
    if (status) status.textContent = "生成中…";
    try {
      const intake = collectIntake();
      await api.saveIntake(intake);
      const res = await api.buildProgram({ intake });
      state.program = res.program;
      state.intake = res.program.intake;
      state.openWeek = 1;
      render();
      const el = document.querySelector(".section-head");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      UI.toast("生成失败：" + U.esc(err.message), "err");
      if (status) status.textContent = "";
    } finally {
      state.busy = false;
    }
  }

  async function submitQuestion(text) {
    const q = (text || "").trim();
    if (!q || state.asking) return;
    state.asking = true;
    const threadEl = document.getElementById("ckThread");
    if (threadEl) {
      const node = document.createElement("div");
      node.className = "qa-item";
      node.innerHTML = `<div class="qa-q"><span class="qa-avatar">你</span><span>${U.esc(q)}</span></div>
        <div class="qa-a"><span class="qa-avatar ai">${Icon.get("spark")}</span>
        <div class="tiny dim" style="padding-top:6px">正在分析你的数据…</div></div>`;
      threadEl.appendChild(node);
      node.scrollIntoView({ behavior: "smooth", block: "end" });
    }
    try {
      const res = await api.askCoach({
        question: q,
        intake: state.intake || collectIntake(),
        program: state.program || null,
      });
      state.thread.push({ q, a: res });
      state.asking = false;
      render();
      const items = document.querySelectorAll(".qa-item");
      if (items.length) items[items.length - 1].scrollIntoView({ behavior: "smooth", block: "end" });
      const input = document.getElementById("ckQuestion");
      if (input) input.focus();
    } catch (err) {
      state.asking = false;
      render();
      UI.toast("提问失败：" + (err.message || err), "err");
    }
  }

  async function load(root, params) {
    host = root;
    root.innerHTML = UI.spinner("读取你的数据…");
    try {
      const [report, meta] = await Promise.all([api.coach(), api.coachIntake()]);
      state.report = report;
      state.meta = meta;
      state.intake = meta.intake;
      state.program = null;
      state.thread = [];
      state.asking = false;
      render();
      document.getElementById("pageTitle").textContent = "教练";
      // 便于分享/调试：带 ?build=1 时自动生成
      if (params && params.build) await generate();
    } catch (err) {
      root.innerHTML = `<div class="card">${UI.empty("info", "加载失败", U.esc(err.message))}</div>`;
    }
  }

  window.PageCoach = { load };
})();
