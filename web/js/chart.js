/* Canvas 图表引擎（零依赖）
   支持：折线/面积/柱状、双 Y 轴、十字线、悬浮提示、响应式、DPR 适配。
   用法：
     const c = new Chart(canvas, {
       type: 'line',
       xLabels: [...],          // 或用一个 series 的 x 值
       series: [{ key:'hr', label:'心率', data:[...], color:'var(--run)', axis:0, fill:true }],
       axes: [{ unit:'bpm', min:null, max:null, side:'left' }],
       height: 180,
       onHover: (i, x, y) => {},
     });
     c.setData({...}); c.destroy();
*/
(function () {
  const DPR = () => Math.min(window.devicePixelRatio || 1, 2.5);

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function resolveColor(c) {
    if (!c) return cssVar('--accent', '#fc4c02');
    if (c.startsWith('var(')) {
      const name = c.slice(4, -1).trim();
      return cssVar(name, '#fc4c02');
    }
    return c;
  }

  function withAlpha(color, alpha) {
    if (!color) return color;
    if (color.startsWith('#')) {
      const hex = color.length === 4
        ? '#' + color[1] + color[1] + color[2] + color[2] + color[3] + color[3]
        : color;
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      return `rgba(${r},${g},${b},${alpha})`;
    }
    if (color.startsWith('rgb')) {
      return color.replace(/rgba?\(([^)]+)\)/, (m, inner) => {
        const parts = inner.split(',').map(s => s.trim());
        return `rgba(${parts[0]},${parts[1]},${parts[2]},${alpha})`;
      });
    }
    return color;
  }

  function niceTicks(min, max, count) {
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    const start = Math.floor(min / step) * step;
    const end = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = start; v <= end + step * 0.5; v += step) {
      ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    }
    return { ticks, min: start, max: end, step };
  }

  function formatTick(v, unit) {
    if (Math.abs(v) >= 10000) return (v / 1000).toFixed(0) + 'k';
    if (unit === 'sec') {
      const s = Math.round(v);
      return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }
    const abs = Math.abs(v);
    if (abs >= 100) return String(Math.round(v));
    if (abs >= 10) return (Math.round(v * 10) / 10).toString();
    return (Math.round(v * 100) / 100).toString();
  }

  class Chart {
    constructor(canvas, opts) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.opts = Object.assign({ type: 'line', height: 180, xLabels: null, series: [], axes: [{}], padding: null }, opts || {});
      this.hoverIndex = -1;
      this.anim = null;
      this._boundMove = this._onMove.bind(this);
      this._boundLeave = this._onLeave.bind(this);
      this._boundClick = this._onClick.bind(this);
      canvas.addEventListener('pointermove', this._boundMove);
      canvas.addEventListener('pointerleave', this._boundLeave);
      canvas.addEventListener('click', this._boundClick);
      this._resizeObserver = new ResizeObserver(() => this.resize());
      this._resizeObserver.observe(canvas.parentElement || canvas);
      this.resize();
      this._animate();
    }

    setData(opts) {
      Object.assign(this.opts, opts);
      this.hoverIndex = -1;
      this._progress = 0;
      this._animate();
      this.draw();
    }

    destroy() {
      cancelAnimationFrame(this._raf);
      this.canvas.removeEventListener('pointermove', this._boundMove);
      this.canvas.removeEventListener('pointerleave', this._boundLeave);
      this.canvas.removeEventListener('click', this._boundClick);
      this._resizeObserver.disconnect();
    }

    resize() {
      const parent = this.canvas.parentElement;
      if (!parent) return;
      const width = Math.max(120, parent.clientWidth);
      const height = this.opts.height;
      const dpr = DPR();
      if (this.canvas.width !== Math.round(width * dpr) || this.canvas.height !== Math.round(height * dpr)) {
        this.canvas.width = Math.round(width * dpr);
        this.canvas.height = Math.round(height * dpr);
        this.canvas.style.width = width + 'px';
        this.canvas.style.height = height + 'px';
      }
      this.w = width;
      this.h = height;
      this.dpr = dpr;
      this.draw();
    }

    _animate() {
      cancelAnimationFrame(this._raf);
      this._progress = 0;
      const step = (t) => {
        this._progress = Math.min(1, (this._progress || 0) + 0.09);
        this.draw();
        if (this._progress < 1) this._raf = requestAnimationFrame(step);
      };
      this._raf = requestAnimationFrame(step);
    }

    /* ---------------------------------------------------------- 布局计算 */

    _layout() {
      const o = this.opts;
      const pad = o.padding || { top: 14, right: 12, bottom: 22, left: 40 };
      const axes = (o.axes && o.axes.length ? o.axes : [{}]).map(a => Object.assign({ side: 'left' }, a));
      const hasRight = axes.some((a, i) => i > 0 && a.side === 'right');

      // 计算每个轴的取值范围
      axes.forEach((axis, ai) => {
        const values = [];
        o.series.forEach(s => {
          if ((s.axis || 0) !== ai) return;
          (s.data || []).forEach(v => { if (v != null && isFinite(v)) values.push(+v); });
        });
        let min = axis.min != null ? axis.min : (values.length ? Math.min(...values) : 0);
        let max = axis.max != null ? axis.max : (values.length ? Math.max(...values) : 1);
        if (axis.zero) min = Math.min(0, min);
        if (axis.pad) {
          const span = Math.max(1e-6, max - min);
          min -= span * axis.pad;
          max += span * axis.pad;
        }
        if (min === max) { max = min + 1; }
        const n = axis.ticks || 4;
        const nice = axis.integer ? null : niceTicks(min, max, n);
        axis._ticks = nice ? nice.ticks : null;
        axis._min = nice ? nice.min : (axis.integer ? Math.floor(min) : min);
        axis._max = nice ? nice.max : (axis.integer ? Math.ceil(max) : max);
        if (axis.integer) {
          const step = Math.max(1, Math.ceil((axis._max - axis._min) / n));
          const ticks = [];
          for (let v = Math.floor(axis._min); v <= axis._max; v += step) ticks.push(v);
          axis._ticks = ticks;
          axis._min = ticks[0] || 0;
          axis._max = ticks[ticks.length - 1] || 1;
        }
      });

      const plot = {
        x: pad.left,
        y: pad.top,
        w: Math.max(10, this.w - pad.left - pad.right - (hasRight ? 34 : 0)),
        h: Math.max(10, this.h - pad.top - pad.bottom),
      };
      plot.right = plot.x + plot.w;
      plot.bottom = plot.y + plot.h;

      const n = this._pointCount();
      plot.n = n;
      return { axes, plot, pad, hasRight };
    }

    _pointCount() {
      const o = this.opts;
      if (o.xValues && o.xValues.length) return o.xValues.length;
      if (o.xLabels && o.xLabels.length) return o.xLabels.length;
      let max = 0;
      o.series.forEach(s => { if (s.data) max = Math.max(max, s.data.length); });
      return max;
    }

    _xAt(i, plot) {
      const o = this.opts;
      const n = Math.max(1, plot.n - 1);
      if (o.xValues && o.xValues.length) {
        let lo = o.xMin, hi = o.xMax;
        if (lo == null) lo = o.xValues[0];
        if (hi == null) hi = o.xValues[o.xValues.length - 1];
        const t = (o.xValues[Math.min(i, o.xValues.length - 1)] - lo) / Math.max(1e-9, hi - lo);
        return plot.x + t * plot.w;
      }
      return plot.x + (i / n) * plot.w;
    }

    _yAt(v, axis, plot) {
      const t = (v - axis._min) / Math.max(1e-9, axis._max - axis._min);
      const clamped = this.opts.invertY ? 1 - t : t;
      return plot.bottom - clamped * plot.h;
    }

    /* ---------------------------------------------------------- 绘制 */

    draw() {
      if (!this.ctx || !this.w) return;
      const ctx = this.ctx;
      const o = this.opts;
      const { axes, plot, pad, hasRight } = this._layout();
      const text2 = cssVar('--text-2', '#a8b1bd');
      const text3 = cssVar('--text-3', '#6d7684');
      const line = cssVar('--line-soft', '#1c2128');
      const lineStrong = cssVar('--line', '#262c35');

      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);

      // 网格与 Y 轴刻度
      const primary = axes[0];
      const ticks = primary._ticks || [];
      ctx.font = '10px ' + (cssVar('--font') || 'sans-serif');
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ticks.forEach(t => {
        const y = this._yAt(t, primary, plot);
        ctx.strokeStyle = line;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(plot.x, Math.round(y) + 0.5);
        ctx.lineTo(plot.right, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.fillStyle = text3;
        ctx.fillText(formatTick(t, primary.unit), plot.x - 7, y);
      });

      // 右轴刻度
      if (hasRight) {
        const right = axes[1];
        ctx.textAlign = 'left';
        (right._ticks || []).forEach(t => {
          const y = this._yAt(t, right, plot);
          ctx.fillStyle = text3;
          ctx.fillText(formatTick(t, right.unit), plot.right + 7, y);
        });
      }

      // 柱状
      const barSeries = o.series.filter(s => s.type === 'bar');
      if (barSeries.length) {
        const n = plot.n;
        const groupW = plot.w / Math.max(1, n);
        const barW = Math.max(1, Math.min(14, groupW * 0.62));
        barSeries.forEach((s) => {
          const axis = axes[s.axis || 0];
          const color = resolveColor(s.color);
          // 柱状图基线：0 在范围内就用 0，否则贴底
          const zero = Math.min(Math.max(0, axis._min), axis._max);
          const y0 = this._yAt(zero, axis, plot);
          s.data.forEach((v, i) => {
            if (v == null || !isFinite(v)) return;
            const x = this._xAt(i, plot);
            const y = this._yAt(v, axis, plot);
            const h = Math.max(0.8, Math.abs(y0 - y));
            ctx.fillStyle = withAlpha(color, s.opacity == null ? 0.5 : s.opacity);
            ctx.fillRect(x - barW / 2, Math.min(y, y0), barW, h);
          });
        });
      }

      // 折线 / 面积
      const progress = this._progress == null ? 1 : this._progress;
      o.series.filter(s => s.type !== 'bar').forEach((s, si) => {
        const axis = axes[s.axis || 0];
        const color = resolveColor(s.color);
        const pts = [];
        (s.data || []).forEach((v, i) => {
          if (v == null || !isFinite(v)) { pts.push(null); return; }
          pts.push([this._xAt(i, plot), this._yAt(v, axis, plot)]);
        });
        if (!pts.length) return;

        // 面积
        if (s.fill) {
          ctx.beginPath();
          let started = false;
          const baseY = this._yAt(Math.max(axis._min, Math.min(0, axis._max)), axis, plot);
          pts.forEach((p, i) => {
            if (!p) return;
            if (!started) { ctx.moveTo(p[0], baseY); ctx.lineTo(p[0], p[1]); started = true; }
            else ctx.lineTo(p[0], p[1]);
          });
          for (let i = pts.length - 1; i >= 0; i--) {
            if (pts[i]) { ctx.lineTo(pts[i][0], baseY); break; }
          }
          ctx.closePath();
          const grad = ctx.createLinearGradient(0, plot.y, 0, plot.bottom);
          grad.addColorStop(0, withAlpha(color, s.fillOpacity == null ? 0.28 : s.fillOpacity));
          grad.addColorStop(1, withAlpha(color, 0));
          ctx.fillStyle = grad;
          ctx.fill();
        }

        // 折线（用裁剪做进度动画）
        ctx.save();
        ctx.beginPath();
        ctx.rect(plot.x, plot.y - 2, plot.w * progress, plot.h + 4);
        ctx.clip();
        ctx.beginPath();
        let started = false;
        pts.forEach(p => {
          if (!p) { started = false; return; }
          if (!started) { ctx.moveTo(p[0], p[1]); started = true; }
          else ctx.lineTo(p[0], p[1]);
        });
        ctx.strokeStyle = color;
        ctx.lineWidth = s.width || 1.7;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        if (s.dash) ctx.setLineDash(s.dash);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.restore();
      });

      // X 轴标签
      if (o.xLabels && o.xLabels.length) {
        const labels = o.xLabels;
        const maxLabels = Math.max(2, Math.floor(plot.w / 74));
        const stride = Math.max(1, Math.ceil(labels.length / maxLabels));
        ctx.fillStyle = text3;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        for (let i = 0; i < labels.length; i += stride) {
          const x = this._xAt(i, plot);
          if (x < plot.x + 8 || x > plot.right - 8) continue;
          ctx.fillText(labels[i], x, plot.bottom + 6);
        }
      } else if (o.xUnit) {
        ctx.fillStyle = text3;
        ctx.textAlign = 'right';
        ctx.textBaseline = 'top';
        ctx.fillText(o.xUnit, plot.right, plot.bottom + 6);
      }

      // 十字线
      if (this.hoverIndex >= 0 && this.hoverIndex < plot.n) {
        const x = this._xAt(this.hoverIndex, plot);
        ctx.strokeStyle = withAlpha(cssVar('--text-3', '#6d7684'), 0.55);
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, plot.y);
        ctx.lineTo(Math.round(x) + 0.5, plot.bottom);
        ctx.stroke();
        ctx.setLineDash([]);

        o.series.forEach(s => {
          if (s.type === 'bar') return;
          const v = (s.data || [])[this.hoverIndex];
          if (v == null || !isFinite(v)) return;
          const axis = axes[s.axis || 0];
          const y = this._yAt(v, axis, plot);
          ctx.beginPath();
          ctx.arc(x, y, 3.4, 0, Math.PI * 2);
          ctx.fillStyle = resolveColor(s.color);
          ctx.fill();
          ctx.strokeStyle = cssVar('--bg-elev', '#12151a');
          ctx.lineWidth = 2;
          ctx.stroke();
        });
      }

      // 图例（右上角，避免压住左轴刻度）
      if (o.legend !== false && o.series.length > 1 && o.series.some(s => s.label)) {
        const items = o.series.filter(s => s.label).map(s => ({
          label: s.label, color: resolveColor(s.color),
        }));
        ctx.font = '10px ' + (cssVar('--font') || 'sans-serif');
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        const widths = items.map(it => 13 + ctx.measureText(it.label).width);
        let lx = plot.right - widths.reduce((a, b) => a + b, 0) - (items.length - 1) * 12;
        lx = Math.max(plot.x + 4, lx);
        const ly = pad.top / 2 + 1;
        items.forEach((it, i) => {
          ctx.fillStyle = it.color;
          ctx.fillRect(lx, ly - 1.5, 10, 3);
          ctx.fillStyle = text2;
          ctx.fillText(it.label, lx + 13, ly);
          lx += widths[i] + 12;
        });
      }

      this._plot = plot;
      this._axes = axes;
    }

    /* ---------------------------------------------------------- 交互 */

    _indexFromEvent(e) {
      if (!this._plot) return -1;
      const rect = this.canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const p = this._plot;
      if (x < p.x - 6 || x > p.right + 6) return -1;
      if (this.opts.xValues && this.opts.xValues.length) {
        const lo = this.opts.xMin == null ? this.opts.xValues[0] : this.opts.xMin;
        const hi = this.opts.xMax == null ? this.opts.xValues[this.opts.xValues.length - 1] : this.opts.xMax;
        const target = lo + ((x - p.x) / p.w) * (hi - lo);
        let best = 0, bestD = Infinity;
        this.opts.xValues.forEach((v, i) => {
          const d = Math.abs(v - target);
          if (d < bestD) { bestD = d; best = i; }
        });
        return best;
      }
      return U.clamp(Math.round(((x - p.x) / p.w) * (p.n - 1)), 0, p.n - 1);
    }

    _onMove(e) {
      const idx = this._indexFromEvent(e);
      if (idx !== this.hoverIndex) {
        this.hoverIndex = idx;
        this.draw();
      }
      if (idx >= 0 && this.opts.onHover) {
        const rect = this.canvas.getBoundingClientRect();
        this.opts.onHover(idx, e.clientX, e.clientY, rect);
      } else if (idx < 0 && this.opts.onHover) {
        this.opts.onHover(-1, e.clientX, e.clientY);
      } else if (idx >= 0 && this.opts.tooltip) {
        showTip(e.clientX, e.clientY, this.opts.tooltip(idx));
      }
    }

    _onLeave() {
      this.hoverIndex = -1;
      this.draw();
      hideTip();
      if (this.opts.onHover) this.opts.onHover(-1);
    }

    _onClick(e) {
      if (!this.opts.onClick) return;
      const idx = this._indexFromEvent(e);
      if (idx >= 0) this.opts.onClick(idx);
    }
  }

  /* ---------------------------------------------------------- 全局提示框 */

  let tipEl = null;
  function tipNode() {
    if (!tipEl) tipEl = document.getElementById('chartTip');
    return tipEl;
  }

  function showTip(x, y, html) {
    const node = tipNode();
    if (!node || !html) return;
    node.innerHTML = html;
    node.hidden = false;
    const rect = node.getBoundingClientRect();
    const left = U.clamp(x, rect.width / 2 + 8, window.innerWidth - rect.width / 2 - 8);
    node.style.left = left + 'px';
    node.style.top = y + 'px';
  }

  function hideTip() {
    const node = tipNode();
    if (node) node.hidden = true;
  }

  window.Chart = Chart;
  window.chartUtil = { cssVar, resolveColor, withAlpha, niceTicks, formatTick, showTip, hideTip };
})();
