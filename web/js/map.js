/* 轨迹地图（Canvas，零依赖）
   - Web Mercator 投影，支持 OSM 瓦片叠加
   - 瓦片加载失败时自动降级为"抽象轨迹图"（网格 + 比例尺），离线也能看
   - 支持缩放、平移、起点/终点、分段高亮、回放指针

   坐标模型：世界像素 = 墨卡托(zoom0, 256px) * 2^zoom + offset
*/
(function () {
  const TILE = 256;
  const MAX_ZOOM = 18;   // 高德瓦片到 18 级为止，19 级会返回纯白
  const MIN_ZOOM = 1;

  function mercator(lat, lon) {
    const siny = Math.sin(Math.max(-85.05112878, Math.min(85.05112878, lat)) * Math.PI / 180);
    const x = (lon + 180) / 360;
    const y = 0.5 - Math.log((1 + siny) / (1 - siny)) / (4 * Math.PI);
    return { x: x * TILE, y: y * TILE };
  }

  /* 高德/腾讯的瓦片是 GCJ-02 火星坐标，GPS 是 WGS-84。
     不做转换的话轨迹会整体偏 100–600 米，看起来「不在路上」。 */
  function outOfChina(lat, lon) {
    return !(lon > 73.66 && lon < 135.05 && lat > 3.86 && lat < 53.55);
  }

  function wgs84ToGcj02(lat, lon) {
    if (outOfChina(lat, lon)) return [lat, lon];
    const a = 6378245.0;
    const ee = 0.00669342162296594323;
    const dLat = (x, y) => {
      let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
      ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
      ret += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
      ret += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
      return ret;
    };
    const dLon = (x, y) => {
      let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
      ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
      ret += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0;
      ret += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
      return ret;
    };
    let dLatV = dLat(lon - 105.0, lat - 35.0);
    let dLonV = dLon(lon - 105.0, lat - 35.0);
    const radLat = lat / 180.0 * Math.PI;
    let magic = Math.sin(radLat);
    magic = 1 - ee * magic * magic;
    const sqrtMagic = Math.sqrt(magic);
    dLatV = (dLatV * 180.0) / ((a * (1 - ee)) / (magic * sqrtMagic) * Math.PI);
    dLonV = (dLonV * 180.0) / (a / sqrtMagic * Math.cos(radLat) * Math.PI);
    return [lat + dLatV, lon + dLonV];
  }

  function unMercator(x, y) {
    const lon = x / TILE * 360 - 180;
    const n = Math.PI - 2 * Math.PI * (y / TILE);
    const lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
    return [lat, lon];
  }

  /* GCJ-02 → WGS-84：反函数没有解析解，用「猜一次再校正」迭代逼近，
     两次就够（误差 < 1 米），足够画线和导出。 */
  function gcj02ToWgs84(lat, lon) {
    if (outOfChina(lat, lon)) return [lat, lon];
    let wLat = lat, wLon = lon;
    for (let i = 0; i < 3; i++) {
      const [gLat, gLon] = wgs84ToGcj02(wLat, wLon);
      wLat += lat - gLat;
      wLon += lon - gLon;
    }
    return [wLat, wLon];
  }

  /* 两点间距离（米），算每公里标记用 */
  function metersBetween(a, b) {
    const R = 6371008.8;
    const dLat = (b[0] - a[0]) * Math.PI / 180;
    const dLon = (b[1] - a[1]) * Math.PI / 180;
    const la1 = a[0] * Math.PI / 180;
    const la2 = b[0] * Math.PI / 180;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  class TrackMap {
    /**
     * @param {HTMLElement} container
     * @param {{height?:number, tileUrl?:string, interactive?:boolean}} opts
     */
    constructor(container, opts) {
      this.container = container;
      this.opts = Object.assign({ height: 340, tileUrl: '', interactive: true }, opts || {});
      this.canvas = document.createElement('canvas');
      this.canvas.style.height = this.opts.height + 'px';
      this.ctx = this.canvas.getContext('2d');
      this.container.appendChild(this.canvas);

      this.route = [];
      this.proj = [];
      this.zoom = 13;
      this.offset = { x: 0, y: 0 };
      this.tileCache = new Map();
      this.tileFailed = new Set();
      this.tileErrors = 0;
      this.tileOk = 0;
      this.showTiles = !!this.opts.tileUrl;
      this.marker = null;
      this.segment = null;
      this.hoverPoint = null;
      this.dragging = false;
      this.waypoints = [];        // 路书途经点 [{lat, lon, name, tag, ...}]
      this.wpProj = [];
      this.dragWp = -1;
      this.kmMarks = false;
      this.focusPin = null;       // 搜索选中的地方：先在地图上标一下
      this.pinDrag = false;       // 正在拖这个临时点
      this._kmCache = null;

      this._resizeObs = new ResizeObserver(() => this.resize());
      this._resizeObs.observe(container);
      this._bind();
      this.resize();
    }

    /* ------------------------------------------------------------ 数据 */

    setRoute(points, opts) {
      this.route = (points || []).filter(p => Array.isArray(p) && isFinite(p[0]) && isFinite(p[1]));
      this.proj = this.route.map(p => {
        const ll = this.opts.gcj ? wgs84ToGcj02(p[0], p[1]) : [p[0], p[1]];
        return mercator(ll[0], ll[1]);
      });
      this._kmCache = null;
      if (!opts || opts.fit !== false) this.fit();
      else this.draw();
    }

    /** 把一组坐标（例如途经点）框到视野里 */
    fitPoints(latlngs) {
      const pts = (latlngs || []).filter(p => Array.isArray(p) && isFinite(p[0]) && isFinite(p[1]));
      if (!pts.length) return;
      const saved = this.proj;
      this.proj = pts.map(p => {
        const ll = this.opts.gcj ? wgs84ToGcj02(p[0], p[1]) : [p[0], p[1]];
        return mercator(ll[0], ll[1]);
      });
      this.fit();
      this.proj = saved;
      this.draw();
    }

    /** 路书途经点：画编号锚点，可拖动（需要 opts.onWaypointMove） */
    setWaypoints(list) {
      this.waypoints = (list || []).filter(w => w && isFinite(w.lat) && isFinite(w.lon))
        .map(w => Object.assign({}, w));
      this.wpProj = this.waypoints.map(w => {
        const ll = this.opts.gcj ? wgs84ToGcj02(w.lat, w.lon) : [w.lat, w.lon];
        return mercator(ll[0], ll[1]);
      });
      this.draw();
    }

    /** 每公里一个 K 标记（行者那套「路线公里 KM 标记」） */
    setKmMarks(on) {
      this.kmMarks = !!on;
      this.draw();
    }

    /** 选中的地点先钉一下：传 null 清掉 */
    setFocusPin(lat, lon) {
      if (lat == null || lon == null) {
        this.focusPin = null;
        this.draw();
        return;
      }
      const ll = this.opts.gcj ? wgs84ToGcj02(lat, lon) : [lat, lon];
      this.focusPin = { lat, lon, p: mercator(ll[0], ll[1]) };
      this.draw();
    }

    /** 把某个点放到画面中心（搜索选中时用） */
    focusLatLon(lat, lon, zoom) {
      this.setView(lat, lon, zoom == null ? 16 : zoom);
      this.setFocusPin(lat, lon);
    }

    /** 高亮某个位置（爬升点击时用）：只居中 + 红点，不动当前缩放，只重绘一次 */
    showPoint(lat, lon, zoom) {
      this.setView(lat, lon, zoom == null ? this.zoom : zoom, { draw: false });
      this.setFocusPin(lat, lon);
    }

    /** 跟着鼠标平移（剖面 hover 用）：保持缩放，单次重绘 */
    panToPoint(lat, lon) {
      this.setView(lat, lon, this.zoom, { draw: false });
      this.setFocusPin(lat, lon);
    }

    /** 按「画面宽度大致覆盖多少米」设置缩放（500 m / 1 km / 2 km …）
        和 zoomBy 一样要以屏幕中心为锚点换算偏移，否则一改缩放地图就跑到别处去了 */
    setSpanMeters(meters) {
      const span = Number(meters);
      if (!isFinite(span) || span <= 0) return;
      const width = Math.max(320, this.w);
      const mpp = span / width;
      const before = this.screenToWorld(this.w / 2, this.h / 2);
      const lat = this.screenToLatLon(this.w / 2, this.h / 2)[0];
      const z = Math.log2(156543.03392 * Math.cos(lat * Math.PI / 180) / mpp);
      this.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(z * 100) / 100));
      const after = this.screenToWorld(this.w / 2, this.h / 2);
      const scale = Math.pow(2, this.zoom);
      this.offset.x += (after.x - before.x) * scale;
      this.offset.y += (after.y - before.y) * scale;
      this.draw();
    }

    /** 改高度（全屏/大图时用） */
    setHeight(h) {
      this.opts.height = Math.max(220, Math.round(h));
      this.canvas.style.height = this.opts.height + 'px';
      this.resize();
    }

    /** 把指定经纬度放到画面中央（新建空白路书时用）
        opts.draw === false 时只改偏移，等调用方统一重绘一次（避免闪烁） */
    setView(lat, lon, zoom, opts) {
      if (zoom != null) this.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
      const ll = this.opts.gcj ? wgs84ToGcj02(lat, lon) : [lat, lon];
      const p = mercator(ll[0], ll[1]);
      const scale = Math.pow(2, this.zoom);
      this.offset = { x: this.w / 2 - p.x * scale, y: this.h / 2 - p.y * scale };
      if (!opts || opts.draw !== false) this.draw();
    }

    setTileUrl(url) {
      this.opts.tileUrl = url || '';
      this.showTiles = !!url;
      this.tileCache.clear();
      this.draw();
    }

    setMarker(index) {
      this.marker = index;
      this.draw();
    }

    setSegment(startIdx, endIdx) {
      this.segment = (startIdx == null) ? null : { start: startIdx, end: endIdx };
      this.draw();
    }

    /** 高亮一段（用于分段页），points 为 [lat,lon][] */
    setHighlight(points, color) {
      this.highlight = (points && points.length > 1)
        ? {
          points: points.map(p => {
            const ll = this.opts.gcj ? wgs84ToGcj02(p[0], p[1]) : [p[0], p[1]];
            return mercator(ll[0], ll[1]);
          }),
          color: color || '#fc4c02',
        }
        : null;
      this.draw();
    }

    /* ------------------------------------------------------------ 视图 */

    fit() {
      if (!this.proj.length) return;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      this.proj.forEach(p => {
        minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
        minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
      });
      const padX = 24, padY = 24;
      const availW = Math.max(20, this.w - padX * 2);
      const availH = Math.max(20, this.h - padY * 2);
      const spanX = Math.max(1e-9, maxX - minX);
      const spanY = Math.max(1e-9, maxY - minY);
      const zoomX = Math.log2(availW / spanX);
      const zoomY = Math.log2(availH / spanY);
      // 太小的范围别顶到最高级：留一点周边道路，否则满屏白瓦片
      const z = Math.max(MIN_ZOOM, Math.min(17, Math.min(zoomX, zoomY)));
      this.zoom = Math.round(z * 100) / 100;
      const cx = (minX + maxX) / 2;
      const cy = (minY + maxY) / 2;
      this.offset.x = this.w / 2 - cx * Math.pow(2, this.zoom);
      this.offset.y = this.h / 2 - cy * Math.pow(2, this.zoom);
      this.draw();
    }

    zoomBy(delta, anchorX, anchorY) {
      const before = this.screenToWorld(anchorX == null ? this.w / 2 : anchorX,
                                        anchorY == null ? this.h / 2 : anchorY);
      this.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, this.zoom + delta));
      const after = this.screenToWorld(anchorX == null ? this.w / 2 : anchorX,
                                       anchorY == null ? this.h / 2 : anchorY);
      this.offset.x += (after.x - before.x) * Math.pow(2, this.zoom);
      this.offset.y += (after.y - before.y) * Math.pow(2, this.zoom);
      this.draw();
    }

    screenToWorld(x, y) {
      const scale = Math.pow(2, this.zoom);
      return { x: (x - this.offset.x) / scale, y: (y - this.offset.y) / scale };
    }

    screenToLatLon(x, y) {
      const w = this.screenToWorld(x, y);
      const [lat, lon] = unMercator(w.x, w.y);
      return this.opts.gcj ? gcj02ToWgs84(lat, lon) : [lat, lon];
    }

    worldToScreen(p) {
      const scale = Math.pow(2, this.zoom);
      return { x: p.x * scale + this.offset.x, y: p.y * scale + this.offset.y };
    }

    resize() {
      const width = Math.max(120, this.container.clientWidth);
      const height = this.opts.height;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (this.canvas.width !== Math.round(width * dpr) || this.canvas.height !== Math.round(height * dpr)) {
        this.canvas.width = Math.round(width * dpr);
        this.canvas.height = Math.round(height * dpr);
        this.canvas.style.width = width + 'px';
        this.canvas.style.height = height + 'px';
      }
      this.w = width;
      this.h = height;
      this.dpr = dpr;
      if (this.proj.length && !this._fitted) { this._fitted = true; this.fit(); }
      else this.draw();
    }

    /* ------------------------------------------------------------ 绘制 */

    draw() {
      if (!this.ctx || !this.w) return;
      const ctx = this.ctx;
      const dpr = this.dpr || 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);

      const bgA = U.cssVar('--bg-elev-2') || '#171b22';
      ctx.fillStyle = bgA;
      ctx.fillRect(0, 0, this.w, this.h);

      if (this.showTiles) this._drawTiles();
      if (!this.showTiles || this.tileOk === 0) this._drawGrid();

      if (this.proj.length) {
        this._drawRoute();
        if (this.kmMarks) this._drawKmMarks();
        if (this.opts.showEndpoints !== false) this._drawEndpoints();
        if (this.highlight) this._drawHighlight();
        if (this.marker != null) this._drawMarker();
      } else if (this.opts.emptyHint !== false) {
        ctx.fillStyle = U.cssVar('--text-3');
        ctx.font = '13px ' + (U.cssVar('--font') || 'sans-serif');
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('该活动没有 GPS 轨迹', this.w / 2, this.h / 2 - 8);
        ctx.font = '11px ' + (U.cssVar('--font') || 'sans-serif');
        ctx.fillText('跑步机 / 泳池 / 手工记录的活动通常没有轨迹', this.w / 2, this.h / 2 + 14);
      }

      this._drawScale();

      if (this.tileErrors > 0 && this.tileOk === 0 && this.opts.tileUrl) {
        ctx.fillStyle = 'rgba(0,0,0,0.35)';
        ctx.fillRect(0, 0, this.w, this.h);
        ctx.fillStyle = 'rgba(255,255,255,0.78)';
        ctx.font = '11.5px ' + (U.cssVar('--font') || 'sans-serif');
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText('底图加载失败（可能没联网或被拦截），可在设置里换底图或改用离线', this.w / 2, 10);
      }

      if (this.wpProj.length) this._drawWaypoints();
      if (this.focusPin) this._drawFocusPin();
    }

    _drawGrid() {
      const ctx = this.ctx;
      const line = U.cssVar('--line') || '#262c35';
      ctx.strokeStyle = line;
      ctx.globalAlpha = 0.55;
      ctx.lineWidth = 1;
      const step = 48;
      for (let x = (this.offset.x % step); x < this.w; x += step) {
        ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, 0); ctx.lineTo(Math.round(x) + 0.5, this.h); ctx.stroke();
      }
      for (let y = (this.offset.y % step); y < this.h; y += step) {
        ctx.beginPath(); ctx.moveTo(0, Math.round(y) + 0.5); ctx.lineTo(this.w, Math.round(y) + 0.5); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    _drawTiles() {
      const z = Math.round(this.zoom);
      const n = Math.pow(2, z);
      const ctx = this.ctx;
      // 每块瓦片在屏幕上占多少像素：256 * 2^(zoom - z)。
      // 之前这里用了 2^zoom，整块地图只画出一块被放大 2^z 倍的瓦片，
      // 结果就是「一片纯色 + 一条线」，真实底图永远出不来。
      const tileScreen = TILE * Math.pow(2, this.zoom - z);

      // 由屏幕像素范围反推需要的瓦片行列
      const startTX = Math.floor((-this.offset.x) / tileScreen);
      const startTY = Math.floor((-this.offset.y) / tileScreen);
      const endTX = Math.ceil((this.w - this.offset.x) / tileScreen);
      const endTY = Math.ceil((this.h - this.offset.y) / tileScreen);

      for (let tx = startTX; tx <= endTX; tx++) {
        for (let ty = startTY; ty <= endTY; ty++) {
          if (ty < 0 || ty >= n) continue;
          const wrappedX = ((tx % n) + n) % n;
          const key = `${z}/${wrappedX}/${ty}`;
          if (this.tileFailed && this.tileFailed.has(key)) continue;
          const px = tx * tileScreen + this.offset.x;
          const py = ty * tileScreen + this.offset.y;
          const size = tileScreen;
          const img = this.tileCache.get(key);
          // 缓存里放的是 { img } 包装对象，直接把它塞给 drawImage 会抛
          // TypeError 并让整帧绘制中断（回放、联动高亮全都不动）。
          const loaded = img && img.img && img.img.complete && img.img.naturalWidth > 0;
          if (loaded) {
            ctx.drawImage(img.img, px, py, size + 0.6, size + 0.6);
            this.tileOk++;
          } else if (!img) {
            this._loadTile(key, z, wrappedX, ty);
          }
        }
      }
    }

    _loadTile(key, z, x, y) {
      const entry = { complete: false, naturalWidth: 0, img: null };
      this.tileCache.set(key, entry);
      if (this.tileCache.size > 400) {
        // 简单淘汰：删掉最早的 80 个
        let i = 0;
        for (const k of this.tileCache.keys()) {
          if (i++ > 80) break;
          this.tileCache.delete(k);
        }
      }
      const img = new Image();
      entry.img = img;
      img.crossOrigin = 'anonymous';
      img.onload = () => {
        entry.complete = true;
        entry.naturalWidth = img.naturalWidth;
        this.tileOk++;
        this._scheduleDraw();
      };
      img.onerror = () => {
        this.tileErrors++;
        if (this.tileFailed) this.tileFailed.add(key);
        this.tileCache.delete(key);
      };
      const url = this.opts.tileUrl
        .replace('{z}', z).replace('{x}', x).replace('{y}', y)
        .replace('{s}', ['a', 'b', 'c'][Math.abs(x + y) % 3]);
      img.src = url;
    }

    _scheduleDraw() {
      if (this._pending) return;
      this._pending = true;
      requestAnimationFrame(() => {
        this._pending = false;
        this.draw();
      });
    }

    _drawRoute() {
      const ctx = this.ctx;
      const points = this.proj.map(p => this.worldToScreen(p));
      if (points.length < 2) return;

      // 外圈白描边：浅色底图上深色光晕会发灰，白边更干净，深色底图上也清楚
      ctx.beginPath();
      points.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
      ctx.strokeStyle = 'rgba(255,255,255,0.88)';
      ctx.lineWidth = 7.5;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();

      ctx.beginPath();
      points.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
      const grad = ctx.createLinearGradient(0, 0, this.w, this.h);
      grad.addColorStop(0, U.cssVar('--accent') || '#fc4c02');
      grad.addColorStop(1, U.cssVar('--accent-2') || '#ff7a3d');
      ctx.strokeStyle = grad;
      ctx.lineWidth = 3.6;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();
    }

    _drawEndpoints() {
      const ctx = this.ctx;
      const first = this.worldToScreen(this.proj[0]);
      const last = this.worldToScreen(this.proj[this.proj.length - 1]);
      const drawDot = (p, color, label) => {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 6, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 2.5;
        ctx.strokeStyle = U.cssVar('--bg', '#0b0d10');
        ctx.stroke();
        if (label) {
          ctx.font = '600 9.5px ' + (U.cssVar('--font') || 'sans-serif');
          ctx.fillStyle = U.cssVar('--bg', '#0b0d10');
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillText(label, p.x, p.y + 0.5);
        }
      };
      drawDot(first, '#34d399', 'S');
      drawDot(last, '#fc4c02', 'E');
      if (this.opts.showVertices) {
        this.proj.forEach((p, i) => {
          if (i === 0 || i === this.proj.length - 1) return;
          const sp = this.worldToScreen(p);
          ctx.beginPath();
          ctx.arc(sp.x, sp.y, 3.6, 0, Math.PI * 2);
          ctx.fillStyle = '#ffffff';
          ctx.fill();
          ctx.lineWidth = 1.6;
          ctx.strokeStyle = 'rgba(0,0,0,.55)';
          ctx.stroke();
        });
      }
    }

    _drawHighlight() {
      const ctx = this.ctx;
      const pts = this.highlight.points.map(p => this.worldToScreen(p));
      ctx.beginPath();
      pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
      ctx.strokeStyle = this.highlight.color;
      ctx.lineWidth = 5;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.globalAlpha = 0.85;
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    _drawMarker() {
      const idx = U.clamp(this.marker, 0, this.proj.length - 1);
      const p = this.worldToScreen(this.proj[idx]);
      const ctx = this.ctx;
      // 光晕
      const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, 18);
      g.addColorStop(0, 'rgba(252,76,2,0.45)');
      g.addColorStop(1, 'rgba(252,76,2,0)');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(p.x, p.y, 18, 0, Math.PI * 2); ctx.fill();
      // 圆点
      ctx.beginPath(); ctx.arc(p.x, p.y, 5.5, 0, Math.PI * 2);
      ctx.fillStyle = '#fff'; ctx.fill();
      ctx.lineWidth = 2.5; ctx.strokeStyle = U.cssVar('--accent') || '#fc4c02'; ctx.stroke();
    }

    _drawKmMarks() {
      const ctx = this.ctx;
      const marks = this._kmPoints();
      if (!marks.length) return;
      ctx.font = '600 9.5px ' + (U.cssVar('--font') || 'sans-serif');
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      marks.forEach(m => {
        const s = this.worldToScreen(m.p);
        if (s.x < -12 || s.y < -12 || s.x > this.w + 12 || s.y > this.h + 12) return;
        ctx.beginPath();
        ctx.arc(s.x, s.y, 8, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(255,255,255,0.92)';
        ctx.fill();
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = 'rgba(0,0,0,0.35)';
        ctx.stroke();
        ctx.fillStyle = '#14181f';
        ctx.fillText('K' + m.km, s.x, s.y + 0.5);
      });
    }

    _kmPoints() {
      if (this._kmCache) return this._kmCache;
      const route = this.route || [];
      const out = [];
      if (route.length >= 2) {
        let acc = 0;
        let nextKm = 1000;
        for (let i = 1; i < route.length; i++) {
          const a = route[i - 1], b = route[i];
          const d = metersBetween(a, b);
          if (d <= 0) continue;
          while (nextKm <= acc + d) {
            const t = (nextKm - acc) / d;
            const lat = a[0] + (b[0] - a[0]) * t;
            const lon = a[1] + (b[1] - a[1]) * t;
            const ll = this.opts.gcj ? wgs84ToGcj02(lat, lon) : [lat, lon];
            out.push({ p: mercator(ll[0], ll[1]), km: nextKm / 1000 });
            nextKm += 1000;
          }
          acc += d;
        }
      }
      this._kmCache = out;
      return out;
    }

    _drawWaypoints() {
      const ctx = this.ctx;
      const n = this.wpProj.length;
      this.wpProj.forEach((p, i) => {
        const s = this.worldToScreen(p);
        const wp = this.waypoints[i] || {};
        const isStart = i === 0;
        const isEnd = n > 1 && i === n - 1;
        if (wp.show === false) {
          // 隐藏的途经点画小灰点：路线还看得懂，但不抢眼
          ctx.beginPath();
          ctx.arc(s.x, s.y, 4, 0, Math.PI * 2);
          ctx.fillStyle = 'rgba(120,128,140,0.85)';
          ctx.fill();
          return;
        }
        const color = isStart ? '#1f9d55' : (isEnd ? '#e8590c' : '#2f6feb');
        ctx.beginPath();
        ctx.arc(s.x, s.y, this.dragWp === i ? 13 : 11, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 2.5;
        ctx.strokeStyle = 'rgba(255,255,255,0.95)';
        ctx.stroke();
        ctx.font = '700 10px ' + (U.cssVar('--font') || 'sans-serif');
        ctx.fillStyle = '#fff';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(isStart ? 'S' : (isEnd ? 'E' : String(i)), s.x, s.y + 0.5);
      });
    }

    _drawFocusPin() {
      const ctx = this.ctx;
      const s = this.worldToScreen(this.focusPin.p);
      // 外圈脉冲感的光晕 + 中间的白心红针
      const g = ctx.createRadialGradient(s.x, s.y, 2, s.x, s.y, 26);
      g.addColorStop(0, 'rgba(232,57,12,0.35)');
      g.addColorStop(1, 'rgba(232,57,12,0)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(s.x, s.y, 26, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(s.x, s.y, 9, 0, Math.PI * 2);
      ctx.fillStyle = '#e8390c';
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = 'rgba(255,255,255,0.95)';
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(s.x, s.y, 3, 0, Math.PI * 2);
      ctx.fillStyle = '#fff';
      ctx.fill();
    }

    _hitWaypoint(x, y) {
      for (let i = 0; i < this.wpProj.length; i++) {
        const s = this.worldToScreen(this.wpProj[i]);
        if (Math.hypot(s.x - x, s.y - y) <= 14) return i;
      }
      return -1;
    }

    _drawScale() {
      const ctx = this.ctx;
      // 1 屏幕像素对应的米数（在中心纬度）
      const center = this.screenToWorld(this.w / 2, this.h / 2);
      const lat = Math.atan(Math.sinh((0.5 - center.y / TILE) * 2 * Math.PI)) * 180 / Math.PI;
      const metersPerWorldPx = 156543.03392 * Math.cos(lat * Math.PI / 180);
      const metersPerScreenPx = metersPerWorldPx / Math.pow(2, this.zoom);

      const candidates = [25, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000];
      let best = candidates[0];
      for (const c of candidates) {
        if (c / metersPerScreenPx <= 110) best = c;
      }
      const px = best / metersPerScreenPx;
      const x = 12, y = this.h - 14;
      ctx.strokeStyle = 'rgba(255,255,255,0.55)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x, y - 4); ctx.lineTo(x, y); ctx.lineTo(x + px, y); ctx.lineTo(x + px, y - 4);
      ctx.stroke();
      ctx.font = '10px ' + (U.cssVar('--font') || 'sans-serif');
      ctx.fillStyle = 'rgba(255,255,255,0.75)';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText(best >= 1000 ? (best / 1000) + ' km' : best + ' m', x, y - 6);
      this.metersPerScreenPx = metersPerScreenPx;
    }

    /* ------------------------------------------------------------ 交互 */

    _bind() {
      if (!this.opts.interactive) return;
      const canvas = this.canvas;

      canvas.addEventListener('pointerdown', (e) => {
        const rect = canvas.getBoundingClientRect();
        const lx = e.clientX - rect.left;
        const ly = e.clientY - rect.top;
        if (this.focusPin && typeof this.opts.onPinMoved === 'function'
            && Math.hypot(this.worldToScreen(this.focusPin.p).x - lx,
              this.worldToScreen(this.focusPin.p).y - ly) <= 18) {
          this.pinDrag = true;
          canvas.setPointerCapture(e.pointerId);
          return;
        }
        const hit = this._hitWaypoint(lx, ly);
        if (hit >= 0 && typeof this.opts.onWaypointMove === 'function') {
          // 点在锚点上：拖锚点，不平移地图
          this.dragWp = hit;
          this._downAt = { x: e.clientX, y: e.clientY };
          canvas.setPointerCapture(e.pointerId);
          this.draw();
          return;
        }
        this.dragging = true;
        this._downAt = { x: e.clientX, y: e.clientY };
        this.dragStart = { x: e.clientX, y: e.clientY, ox: this.offset.x, oy: this.offset.y };
        canvas.setPointerCapture(e.pointerId);
      });
      canvas.addEventListener('pointermove', (e) => {
        if (this.pinDrag && this.focusPin) {
          const rect = canvas.getBoundingClientRect();
          const ll = this.screenToLatLon(e.clientX - rect.left, e.clientY - rect.top);
          this.focusPin.lat = ll[0];
          this.focusPin.lon = ll[1];
          const g = this.opts.gcj ? wgs84ToGcj02(ll[0], ll[1]) : ll;
          this.focusPin.p = mercator(g[0], g[1]);
          this._scheduleDraw();
          return;
        }
        if (this.dragWp >= 0) {
          const rect = canvas.getBoundingClientRect();
          const ll = this.screenToLatLon(e.clientX - rect.left, e.clientY - rect.top);
          const wp = this.waypoints[this.dragWp];
          if (wp) { wp.lat = ll[0]; wp.lon = ll[1]; }
          const g = this.opts.gcj ? wgs84ToGcj02(ll[0], ll[1]) : ll;
          this.wpProj[this.dragWp] = mercator(g[0], g[1]);
          this._kmCache = null;
          this._scheduleDraw();
          return;
        }
        if (!this.dragging) return;
        this.offset.x = this.dragStart.ox + (e.clientX - this.dragStart.x);
        this.offset.y = this.dragStart.oy + (e.clientY - this.dragStart.y);
        this._scheduleDraw();
      });
      const end = () => { this.dragging = false; };
      canvas.addEventListener('pointercancel', (e) => {
        end();
        if (this.dragWp >= 0) { this.dragWp = -1; this.draw(); }
      });
      canvas.addEventListener('pointerup', (e) => {
        const down = this._downAt;
        this.dragging = false;
        if (this.pinDrag) {
          this.pinDrag = false;
          const fp = this.focusPin;
          if (fp && typeof this.opts.onPinMoved === 'function') this.opts.onPinMoved(fp.lat, fp.lon);
          return;
        }
        if (this.dragWp >= 0) {
          const idx = this.dragWp;
          const wp = this.waypoints[idx];
          this.dragWp = -1;
          this.draw();
          if (wp && typeof this.opts.onWaypointMove === 'function') {
            this.opts.onWaypointMove(idx, wp.lat, wp.lon);
          }
          this._downAt = null;
          return;
        }
        // 拖拽地图不算点击，只有真正「点一下」才加点
        if (down && Math.abs(e.clientX - down.x) < 4 && Math.abs(e.clientY - down.y) < 4
            && typeof this.opts.onClick === 'function') {
          const rect = canvas.getBoundingClientRect();
          const ll = this.screenToLatLon(e.clientX - rect.left, e.clientY - rect.top);
          this.opts.onClick(ll[0], ll[1]);
        }
      });

      canvas.addEventListener('wheel', (e) => {
        e.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const delta = e.deltaY < 0 ? 0.5 : -0.5;
        this.zoomBy(delta, e.clientX - rect.left, e.clientY - rect.top);
      }, { passive: false });

      // 双击放大
      canvas.addEventListener('dblclick', (e) => {
        const rect = canvas.getBoundingClientRect();
        this.zoomBy(1, e.clientX - rect.left, e.clientY - rect.top);
      });
    }

    destroy() {
      this._resizeObs.disconnect();
      this.canvas.remove();
      this.tileCache.clear();
    }
  }

  /* ---------------------------------------------------------------- 缩略图
     列表卡片里的迷你地图：和详情页共用一套瓦片/投影逻辑，但只画一次、
     不响应交互。方块小，只取中心附近的少量瓦片，几十张卡片也扛得住。 */
  const thumbTiles = new Map();
  const thumbFailed = new Set();
  let thumbSeq = 0;

  function thumbTile(key, z, x, y, url, onload) {
    if (thumbFailed.has(key)) return null;
    const hit = thumbTiles.get(key);
    if (hit) {
      if (hit.complete && hit.naturalWidth) return hit;
      hit._waiters.push(onload);
      return null;
    }
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img._waiters = [onload];
    img.onload = () => {
      img._waiters.splice(0).forEach(fn => fn());
    };
    img.onerror = () => {
      thumbFailed.add(key);
      thumbTiles.delete(key);
    };
    img.src = url.replace('{z}', z).replace('{x}', x).replace('{y}', y)
      .replace('{s}', ['a', 'b', 'c'][Math.abs(x + y) % 3]);
    thumbTiles.set(key, img);
    if (thumbTiles.size > 600) {
      let i = 0;
      for (const k of thumbTiles.keys()) { if (i++ > 120) break; thumbTiles.delete(k); }
    }
    return null;
  }

  /**
   * @param {HTMLCanvasElement} canvas
   * @param {number[][]} points [[lat,lon],...]
   * @param {{tileUrl?:string, gcj?:boolean, color?:string}} opts
   */
  function renderThumb(canvas, points, opts) {
    const pts = (points || []).filter(p => Array.isArray(p) && isFinite(p[0]) && isFinite(p[1]));
    if (!canvas || pts.length < 2) return false;
    const o = Object.assign({ tileUrl: '', gcj: false, color: '#fc4c02' }, opts || {});
    const w = Math.max(40, canvas.clientWidth || 240);
    const h = Math.max(30, canvas.clientHeight || 110);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    const proj = pts.map(p => {
      const ll = o.gcj ? wgs84ToGcj02(p[0], p[1]) : [p[0], p[1]];
      return mercator(ll[0], ll[1]);
    });

    // 同 fit()：按包围盒取整度缩放
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    proj.forEach(p => {
      minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
      minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
    });
    const pad = 10;
    const zoom = Math.max(MIN_ZOOM, Math.min(16,
      Math.min(Math.log2(Math.max(20, w - pad * 2) / Math.max(1e-9, maxX - minX)),
               Math.log2(Math.max(20, h - pad * 2) / Math.max(1e-9, maxY - minY)))));
    const scale = Math.pow(2, zoom);
    const offX = w / 2 - ((minX + maxX) / 2) * scale;
    const offY = h / 2 - ((minY + maxY) / 2) * scale;

    const paint = () => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = U.cssVar('--bg-elev-2') || '#171b22';
      ctx.fillRect(0, 0, w, h);

      if (o.tileUrl) {
        const z = Math.round(zoom);
        const n = Math.pow(2, z);
        const tileScreen = TILE * Math.pow(2, zoom - z);
        const sTX = Math.floor(-offX / tileScreen), eTX = Math.ceil((w - offX) / tileScreen);
        const sTY = Math.floor(-offY / tileScreen), eTY = Math.ceil((h - offY) / tileScreen);
        for (let tx = sTX; tx <= eTX; tx++) {
          for (let ty = sTY; ty <= eTY; ty++) {
            if (ty < 0 || ty >= n) continue;
            const wx = ((tx % n) + n) % n;
            const img = thumbTile(`${z}/${wx}/${ty}`, z, wx, ty, o.tileUrl, paint);
            if (img) ctx.drawImage(img, tx * tileScreen + offX, ty * tileScreen + offY,
              tileScreen + 0.6, tileScreen + 0.6);
          }
        }
      }

      // 轨迹
      const scr = proj.map(p => ({ x: p.x * scale + offX, y: p.y * scale + offY }));
      ctx.beginPath();
      scr.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
      ctx.strokeStyle = 'rgba(0,0,0,0.45)';
      ctx.lineWidth = 4.5;
      ctx.lineJoin = ctx.lineCap = 'round';
      ctx.stroke();
      ctx.strokeStyle = o.color;
      ctx.lineWidth = 2.4;
      ctx.stroke();

      // 起点/终点
      const dot = (p, fill) => {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3.6, 0, Math.PI * 2);
        ctx.fillStyle = fill;
        ctx.fill();
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = 'rgba(0,0,0,0.5)';
        ctx.stroke();
      };
      dot(scr[0], '#34d399');
      dot(scr[scr.length - 1], o.color);
    };

    paint();
    // 底图没网时退回深色底 + 轨迹，仍然是可辨认的形状
    canvas.dataset.thumb = String(++thumbSeq);
    return !thumbFailed.size || o.tileUrl === '';
  }

  window.TrackMap = TrackMap;
  window.MapThumb = { render: renderThumb };
  window.MapCoords = { toWgs: gcj02ToWgs84, toGcj: wgs84ToGcj02, mercator, unMercator };
})();
