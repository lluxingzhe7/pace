/* 通用工具：格式化、DOM、时间、颜色。无依赖。 */
(function () {
  const FAMILY_LABEL = { run: '跑步', ride: '骑行', swim: '游泳', walk: '徒步', other: '其他' };
  const SPORT_LABEL = {
    running: '跑步', trail_running: '越野跑', treadmill_running: '跑步机', track_running: '场地跑',
    cycling: '骑行', road_cycling: '公路骑行', indoor_cycling: '室内骑行',
    mountain_biking: '山地骑行', gravel_cycling: '砾石骑行', cyclocross: '公路越野',
    e_bike_ride: '电助力骑行',
    swimming: '游泳', pool_swimming: '泳池游泳', open_water_swimming: '公开水域',
    walking: '步行', hiking: '徒步', rowing: '划船', skiing: '滑雪',
    workout: '力量训练', generic: '其他',
  };

  const pad = (n) => String(n).padStart(2, '0');

  function parseDate(ts) {
    return ts ? new Date(ts * 1000) : new Date();
  }

  /** 时长：<1h -> 12:34；>=1h -> 1:02:03 */
  function dur(seconds, opts) {
    if (seconds == null || !isFinite(seconds)) return '—';
    seconds = Math.max(0, Math.round(seconds));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (opts && opts.clock) {
      return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
    }
    if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
    if (m > 0) return `${m}:${pad(s)}`;
    return `0:${pad(s)}`;
  }

  /** 友好时长：1小时22分 / 45分钟 */
  function durHuman(seconds) {
    if (seconds == null || !isFinite(seconds)) return '—';
    const total = Math.round(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.round((total % 3600) / 60);
    if (h > 0) return m > 0 ? `${h}小时${m}分` : `${h}小时`;
    if (m > 0) return `${m}分钟`;
    return `${total}秒`;
  }

  /** 距离：米 -> "5.24" */
  function km(meters, digits) {
    if (meters == null || !isFinite(meters)) return '—';
    const d = digits == null ? (meters < 10000 ? 2 : 1) : digits;
    return (meters / 1000).toFixed(d);
  }

  function distLabel(meters, digits) {
    return `${km(meters, digits)}<small>km</small>`;
  }

  /**
   * 配速/速度。family: run/ride/swim/walk/other
   * 跑步与徒步：分:秒 / 公里；游泳：分:秒 / 100米；骑行：km/h
   */
  function pace(secondsPerUnit, family) {
    if (secondsPerUnit == null || !isFinite(secondsPerUnit) || secondsPerUnit <= 0) return '—';
    const s = Math.round(secondsPerUnit);
    return `${Math.floor(s / 60)}:${pad(s % 60)}`;
  }

  function paceFromSpeed(kmh, family) {
    if (!kmh || !isFinite(kmh) || kmh <= 0) return '—';
    if (family === 'ride' || family === 'other') return `${kmh.toFixed(1)}`;
    const secPerKm = 3600 / kmh;
    if (family === 'swim') return pace(secPerKm / 10, family);
    return pace(secPerKm, family);
  }

  function paceUnit(family) {
    if (family === 'ride' || family === 'other') return 'km/h';
    if (family === 'swim') return '/100m';
    return '/km';
  }

  function paceLabel(secondsPerKm, family) {
    if (family === 'ride' || family === 'other') {
      if (!secondsPerKm) return '—';
      return paceFromSpeed(3600 / secondsPerKm, family);
    }
    if (family === 'swim') return pace(secondsPerKm / 10, family);
    return pace(secondsPerKm, family);
  }

  function num(value, digits) {
    if (value == null || !isFinite(value)) return '—';
    return Number(value).toLocaleString('zh-CN', {
      minimumFractionDigits: digits == null ? 0 : digits,
      maximumFractionDigits: digits == null ? 0 : digits,
    });
  }

  function elev(meters) {
    if (meters == null || !isFinite(meters)) return '—';
    return `${Math.round(meters)}`;
  }

  /** 日期：今天 / 昨天 / 10月4日 周六 */
  function dateLabel(ts) {
    if (!ts) return '—';
    const d = parseDate(ts);
    const now = new Date();
    const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diffDays = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
    const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (diffDays === 0) return `今天 ${hhmm}`;
    if (diffDays === 1) return `昨天 ${hhmm}`;
    if (diffDays < 7) return `${diffDays} 天前 ${hhmm}`;
    const weekday = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()];
    const sameYear = d.getFullYear() === now.getFullYear();
    return sameYear
      ? `${d.getMonth() + 1}月${d.getDate()}日 ${weekday} ${hhmm}`
      : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${hhmm}`;
  }

  function dateShort(ts) {
    const d = parseDate(ts);
    return `${d.getMonth() + 1}/${d.getDate()}`;
  }

  function dateISO(ts) {
    const d = parseDate(ts);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function timeOfDay(ts) {
    const d = parseDate(ts);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function weekday(ts) {
    return ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][parseDate(ts).getDay()];
  }

  /** n 天前的 0 点（本地） */
  function startOfDay(daysAgo) {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - (daysAgo || 0));
    return d.getTime() / 1000;
  }

  function esc(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function el(html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  function debounce(fn, ms) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), ms || 220);
    };
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function family(sport) {
    if (!sport) return 'other';
    if (sport.startsWith('run')) return 'run';
    if (sport.includes('cycl') || sport.includes('bik') || sport === 'ebike') return 'ride';
    if (sport.includes('swim')) return 'swim';
    if (sport === 'walking' || sport === 'hiking') return 'walk';
    return 'other';
  }

  function familyLabel(f) { return FAMILY_LABEL[f] || '其他'; }
  function sportLabel(s) { return SPORT_LABEL[s] || s || '其他'; }

  function familyColor(f) {
    const map = { run: 'var(--run)', ride: 'var(--ride)', swim: 'var(--swim)', walk: 'var(--walk)', other: 'var(--other)' };
    return map[f] || map.other;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /** 训练状态的中文描述 */
  function trainingStatus(key) {
    return ({
      insufficient: { label: '数据不足', tone: 'dim' },
      fresh: { label: '状态充沛', tone: 'good' },
      race_ready: { label: '比赛状态', tone: 'good' },
      neutral: { label: '维持中', tone: 'dim' },
      productive: { label: '高效训练', tone: 'good' },
      overreaching: { label: '负荷偏高', tone: 'bad' },
    })[key] || { label: '—', tone: 'dim' };
  }

  function deltaPct(value) {
    if (value == null) return '<span class="delta flat">—</span>';
    if (Math.abs(value) < 0.05) return '<span class="delta flat">持平</span>';
    const cls = value > 0 ? 'up' : 'down';
    const arrow = value > 0 ? '↑' : '↓';
    return `<span class="delta ${cls}">${arrow}${Math.abs(value).toFixed(0)}%</span>`;
  }

  /** 解析查询串 / 设置路由参数 */
  function query() {
    const out = {};
    const src = location.hash.split('?')[1] || '';
    src.split('&').forEach(pair => {
      if (!pair) return;
      const [k, v] = pair.split('=');
      out[decodeURIComponent(k)] = decodeURIComponent(v || '');
    });
    return out;
  }

  function tzOffset() {
    return -new Date().getTimezoneOffset() / 60;
  }

  function todayRange() {
    return { from: startOfDay(0), to: startOfDay(-1) };
  }

  function isEmpty(v) {
    return v == null || (Array.isArray(v) && v.length === 0) ||
      (typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0);
  }

  function sum(arr, fn) {
    return arr.reduce((acc, x) => acc + (fn ? fn(x) : x), 0);
  }

  function groupBy(arr, fn) {
    const out = new Map();
    arr.forEach(x => {
      const k = fn(x);
      if (!out.has(k)) out.set(k, []);
      out.get(k).push(x);
    });
    return out;
  }

  function niceMax(v) {
    if (!isFinite(v) || v <= 0) return 1;
    const exp = Math.floor(Math.log10(v));
    const base = Math.pow(10, exp);
    const n = v / base;
    const mult = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return mult * base;
  }

  function fmtMinutes(seconds) {
    const m = Math.round(seconds / 60);
    return m >= 60 ? `${(m / 60).toFixed(1)}h` : `${m}m`;
  }

  window.U = {
    pad, dur, durHuman, km, distLabel, pace, paceFromSpeed, paceLabel, paceUnit,
    num, elev, dateLabel, dateShort, dateISO, timeOfDay, weekday, startOfDay,
    esc, el, $, $$, debounce, clamp, family, familyLabel, sportLabel, familyColor,
    cssVar, trainingStatus, deltaPct, query, tzOffset, todayRange, isEmpty, sum,
    groupBy, niceMax, fmtMinutes, parseDate,
    FAMILY_LABEL, SPORT_LABEL,
  };
})();
