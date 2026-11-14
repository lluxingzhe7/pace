/* 路网吸附 + POI 搜索
   吸附：优先 Valhalla（bicycle/pedestrian 比 OSRM 的 driving 更贴近骑行/徒步），
   失败退回 OSRM；再失败退回直线，绝不阻塞制图。
   POI：有高德 Key 用高德（国内最准），没 Key 用 Photon（免 Key）。 */
(function () {
  const OSRM_URL = 'https://router.project-osrm.org/route/v1/driving/';
  const VALHALLA_URL = 'https://valhalla1.openstreetmap.de/route';
  const VALHALLA_HEIGHT = 'https://valhalla1.openstreetmap.de/height';
  const PHOTON_URL = 'https://photon.komoot.io/api/';
  const PHOTON_REVERSE = 'https://photon.komoot.io/reverse';
  const AMAP_SEARCH = 'https://restapi.amap.com/v3/place/text';
  const AMAP_REVERSE = 'https://restapi.amap.com/v3/geocode/regeo';
  const TIMEOUT_MS = 9000;

  const store = {
    get(key, fallback) {
      try {
        const v = localStorage.getItem(key);
        return (v === null || v === '') ? fallback : v;
      } catch (e) { return fallback; }
    },
    set(key, value) {
      try {
        if (value === null || value === undefined || value === '') localStorage.removeItem(key);
        else localStorage.setItem(key, String(value));
      } catch (e) { /* ignore */ }
    },
  };

  function osrmUrl() { return store.get('pace.routing.url', OSRM_URL); }
  function setOsrmUrl(u) { store.set('pace.routing.url', String(u || '').trim()); }
  let badAmapKey = '';

  function amapKey() {
    // 只认本地服务里的设置：浏览器 localStorage 可能还留着旧的/失效的 Key
    const fromApp = (window.PaceApp && window.PaceApp.settings
      && window.PaceApp.settings.amap_key) || '';
    return String(fromApp).trim();
  }

  /** 存到本地服务（跨浏览器、重启都还在） */
  async function setAmapKey(k) {
    const v = String(k || '').trim();
    if (window.PaceApp && window.PaceApp.settings) window.PaceApp.settings.amap_key = v;
    badAmapKey = '';            // 换了 Key 就重新试高德
    try {
      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amap_key: v }),
      });
    } catch (e) { /* 离线也不影响本地搜索 */ }
  }

  function withTimeout(timeout) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout || TIMEOUT_MS);
    return { signal: ctrl.signal, done: () => clearTimeout(timer) };
  }

  function straight(points) {
    return points.map(p => [p.lat, p.lon]);
  }

  /* ------------------------------------------------ 折线解码 */

  /** Google/Valhalla 的 encoded polyline（精度 1e6）→ [[lat, lon], ...] */
  function decodePolyline(str, precision) {
    const factor = Math.pow(10, precision == null ? 6 : precision);
    const coords = [];
    let index = 0, lat = 0, lon = 0;
    while (index < str.length) {
      let result = 0, shift = 0, b;
      do {
        b = str.charCodeAt(index++) - 63;
        result |= (b & 0x1f) << shift;
        shift += 5;
      } while (b >= 0x20);
      lat += (result & 1) ? ~(result >> 1) : (result >> 1);
      result = 0; shift = 0;
      do {
        b = str.charCodeAt(index++) - 63;
        result |= (b & 0x1f) << shift;
        shift += 5;
      } while (b >= 0x20);
      lon += (result & 1) ? ~(result >> 1) : (result >> 1);
      coords.push([lat / factor, lon / factor]);
    }
    return coords;
  }

  /* ------------------------------------------------ 吸附 */

  async function viaValhalla(points, costing) {
    const legs = await viaValhallaLegs(points, costing);
    return concatLegs(legs);
  }

  function concatLegs(legs) {
    const out = [];
    legs.forEach(leg => {
      const shape = leg.shape || [];
      if (out.length && shape.length) out.push(...shape.slice(1));
      else out.push(...shape);
    });
    return out;
  }

  async function viaValhallaLegs(points, costing) {
    // Valhalla 公共实例限制单次最多 10 个 location；多了就分段算（每段 9 个新点）
    const MAX = 10;
    if (points.length <= MAX) return await requestValhallaLegs(points, costing);
    const legs = [];
    for (let start = 0; start < points.length - 1; start += MAX - 1) {
      const chunk = points.slice(start, Math.min(points.length, start + MAX));
      if (chunk.length < 2) break;
      const part = await requestValhallaLegs(chunk, costing);
      // 接点由 concatLegs 统一去掉（每条后续 leg 的首点），这里不用再切片
      legs.push(...part);
    }
    return legs;
  }

  async function requestValhallaLegs(points, costing) {
    const t = withTimeout(TIMEOUT_MS + 4000);
    try {
      const res = await fetch(VALHALLA_URL, {
        method: 'POST',
        signal: t.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          locations: points.map(p => ({ lat: p.lat, lon: p.lon, type: 'break' })),
          costing,
          costing_options: costingOptions(costing),
          directions_options: { units: 'kilometers' },
        }),
      });
      if (!res.ok) throw new Error('Valhalla ' + res.status);
      const json = await res.json();
      const raw = (json.trip && json.trip.legs) || [];
      if (!raw.length) throw new Error('Valhalla 没有返回路线');
      return raw.map(leg => {
        const shape = decodePolyline(leg.shape || '', 6);
        let len = 0;
        for (let i = 1; i < shape.length; i++) len += metersBetween(shape[i - 1], shape[i]);
        return { shape, len };
      });
    } finally {
      t.done();
    }
  }

  async function viaOsrm(points) {
    const out = [];
    for (let i = 0; i < points.length - 1; i += 49) {
      const chunk = points.slice(i, Math.min(points.length, i + 50));
      const coords = chunk.map(p => `${p.lon.toFixed(6)},${p.lat.toFixed(6)}`).join(';');
      const t = withTimeout(TIMEOUT_MS);
      try {
        const res = await fetch(osrmUrl() + coords + '?overview=full&geometries=geojson&steps=false',
          { signal: t.signal });
        if (!res.ok) throw new Error('OSRM ' + res.status);
        const json = await res.json();
        if (json.code !== 'Ok' || !json.routes || !json.routes.length) {
          throw new Error(json.message || 'OSRM 算不出这段路');
        }
        const shape = json.routes[0].geometry.coordinates.map(c => [c[1], c[0]]);
        if (out.length && shape.length) shape.shift();
        out.push(...shape);
      } finally {
        t.done();
      }
    }
    return out;
  }

  const COSTING = { road: 'bicycle', walk: 'pedestrian' };

  /* 骑行偏好：尽量走大路、不上高速、少钻小路/土路。
     Valhalla 的 bicycle 本来就不会走 motorway，这里再把 living street / track 的权重压低，
     并把车型设成 Road（偏好铺装路面）。实测同一段路 7.72 km → 7.33 km，华文街这种小巷不再经过。 */
  const BICYCLE_OPTIONS = {
    bicycle_type: 'Road',
    use_roads: 0.9,
    use_living_streets: 0.1,
    avoid_bad_surfaces: 0.9,
    use_tracks: 0.05,
    use_hills: 0.4,
  };

  function costingOptions(costing) {
    return costing === 'bicycle' ? { bicycle: Object.assign({}, BICYCLE_OPTIONS) } : {};
  }

  function metersBetween(a, b) {
    const R = 6371008.8;
    const dLat = (b[0] - a[0]) * Math.PI / 180;
    const dLon = (b[1] - a[1]) * Math.PI / 180;
    const la1 = a[0] * Math.PI / 180;
    const la2 = b[0] * Math.PI / 180;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  async function oneLeg(a, b, costing) {
    const shape = await viaValhalla([a, b], costing);
    let len = 0;
    for (let i = 1; i < shape.length; i++) len += metersBetween(shape[i - 1], shape[i]);
    return { shape, len };
  }

  /** 相邻 15 米内的重复途经点直接去掉：重复点会让规划绕回原地（看起来像两条路） */
  function dedupeClose(list, minMeters) {
    const gap = minMeters == null ? 25 : minMeters;
    const out = [];
    for (const p of list) {
      const last = out[out.length - 1];
      if (last && metersBetween([last.lat, last.lon], [p.lat, p.lon]) < gap) continue;
      out.push(p);
    }
    return out;
  }

  /** profile: road（骑行）| walk（徒步）| straight（直线） */
  async function snap(points, profile) {
    const raw = (points || []).filter(p => p && isFinite(p.lat) && isFinite(p.lon));
    const list = dedupeClose(raw);
    const mode = profile || 'road';
    if (list.length < 2) {
      return { ok: false, coords: straight(list), reason: '至少两个途经点', provider: '' };
    }
    if (mode === 'straight') {
      return { ok: false, coords: straight(list), reason: '直线连接（不吸附路网）', provider: 'straight' };
    }
    try {
      const legs = await viaValhallaLegs(list, COSTING[mode] || 'bicycle');
      const detours = [];
      // 防绕路：某一段比直线长太多（骑行道封路/断点会绕一大圈），
      // 换徒步再算一次，明显更短就用徒步那段。
      for (let i = 0; i < legs.length; i++) {
        const a = list[i], b = list[i + 1];
        if (!a || !b) continue;
        const direct = metersBetween([a.lat, a.lon], [b.lat, b.lon]);
        if (direct <= 200) continue;
        if (legs[i].len <= direct * 2.2 + 900) continue;
        try {
          const alt = await oneLeg(a, b, 'pedestrian');
          if (alt.len < legs[i].len * 0.92) {
            detours.push({ leg: i + 1, direct: Math.round(direct),
              before: Math.round(legs[i].len), after: Math.round(alt.len) });
            legs[i] = alt;
          }
        } catch (e) { /* 徒步也失败就保留原路线 */ }
      }
      const coords = concatLegs(legs);
      if (coords.length < 2) throw new Error('Valhalla 路线为空');
      // 哪一段明显比直线远很多，提示用户（多半是锚点落在院子里/单行线）
      const detourLegs = [];
      legs.forEach((leg, i) => {
        const a = list[i], b = list[i + 1];
        if (!a || !b) return;
        const direct = metersBetween([a.lat, a.lon], [b.lat, b.lon]);
        if (direct > 800 && leg.len > direct * 2.2 + 1500) {
          detourLegs.push({ leg: i + 1, km: Math.round(leg.len / 100) / 10, directKm: Math.round(direct / 100) / 10 });
        }
      });
      return { ok: true, coords, reason: '', provider: 'valhalla', detours, detourLegs };
    } catch (errV) {
      try {
        const coords = await viaOsrm(list);
        return { ok: true, coords, reason: 'Valhalla 不通，已用 OSRM（驾车路网）', provider: 'osrm', detours: [] };
      } catch (errO) {
        return {
          ok: false,
          coords: straight(list),
          reason: (errO && errO.name === 'AbortError') ? '路网服务超时' : String((errO && errO.message) || errO),
          provider: '',
          detours: [],
        };
      }
    }
  }

  /* ------------------------------------------------ 海拔（Valhalla /height） */

  /** 从轨迹里均匀抽稀出 ≤ maxPts 个点，返回 [[lat, lon], ...] */
  function thin(points, maxPts) {
    const n = points.length;
    if (n <= maxPts) return points.slice();
    const step = (n - 1) / (maxPts - 1);
    const out = [];
    for (let i = 0; i < maxPts; i++) out.push(points[Math.round(i * step)]);
    return out;
  }

  /** 返回 {ok, profile: [[距离 m, 海拔 m], ...], reason} */
  async function elevation(points, maxPts) {
    const list = (points || []).filter(p => Array.isArray(p) && isFinite(p[0]) && isFinite(p[1]));
    if (list.length < 2) return { ok: false, profile: [], reason: '轨迹太短' };
    const picked = (maxPts && list.length > maxPts) ? thin(list, maxPts) : list;
    const t = withTimeout(12000);
    try {
      const res = await fetch(VALHALLA_HEIGHT, {
        method: 'POST',
        signal: t.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          shape: picked.map(p => ({ lat: p[0], lon: p[1] })),
          range: true,
        }),
      });
      if (!res.ok) throw new Error('海拔服务 ' + res.status);
      const json = await res.json();
      const rh = (json && json.range_height) || [];
      const prof = rh.filter(x => Array.isArray(x) && x[1] != null && isFinite(x[1]))
        .map(x => [Number(x[0]), Number(x[1])]);
      if (prof.length < 2) throw new Error('海拔服务没返回数据');
      return { ok: true, profile: prof, reason: '' };
    } catch (err) {
      return { ok: false, profile: [], reason: String((err && err.message) || err) };
    } finally {
      t.done();
    }
  }

  /** 和 backend _fill_alt_gaps 一样：把连续一小段掉到 0/1 m 的「没信号」补掉 */
  function fillAltGaps(values) {
    const vals = (values || []).map(Number);
    const n = vals.length;
    if (!n) return vals;
    const sorted = vals.slice().sort((a, b) => a - b);
    const gapLevel = sorted[Math.floor(n / 2)] > 5 ? 1 : 0;
    const out = vals.slice();
    let i = 0;
    while (i < n) {
      if (out[i] > gapLevel) { i++; continue; }
      let j = i;
      while (j < n && out[j] <= gapLevel) j++;
      if (j - i <= 30) {
        const before = i > 0 ? out[i - 1] : null;
        const after = j < n ? out[j] : null;
        if (before != null && after != null) {
          const span = j - i + 1;
          for (let k = i; k < j; k++) out[k] = before + (after - before) * ((k - i + 1) / span);
        } else if (after != null) {
          for (let k = i; k < j; k++) out[k] = after;
        } else if (before != null) {
          for (let k = i; k < j; k++) out[k] = before;
        }
      }
      i = j;
    }
    return out;
  }

  /** 从 [[距离 m, 海拔 m], ...] 里找爬坡段（和 backend routebook._climbs 同口径）：
      25 m 网格 → 前后 60 m 平均坡度 → 连续 ≥3% 合并（中间 <250 m 平路算同一段）
      → 长度 ≥300 m、爬升 ≥8 m 才算一段 → 按爬升量+坡度分级 */
  function climbs(profile) {
    const raw = (profile || []).filter(p => p && isFinite(p[0]) && isFinite(p[1]));
    if (raw.length < 20) return [];
    // 先做 9 点平滑：backend 对手绘/GPX 轨迹也是先 smooth(9) 再找爬坡，口径要对上
    const win = 9;
    const halfW = Math.floor(win / 2);
    const pairs = raw.map((p, i) => {
      const lo = Math.max(0, i - halfW);
      const hi = Math.min(raw.length - 1, i + halfW);
      let sum = 0;
      for (let k = lo; k <= hi; k++) sum += raw[k][1];
      return [p[0], sum / (hi - lo + 1)];
    });
    const step = 25;
    const start = pairs[0][0];
    const end = pairs[pairs.length - 1][0];
    if (end - start < 300) return [];
    const grid = [];
    let j = 0;
    for (let x = start; x <= end; x += step) {
      while (j < pairs.length - 2 && pairs[j + 1][0] < x) j++;
      const a = pairs[j], b = pairs[j + 1];
      const span = Math.max(1e-6, b[0] - a[0]);
      const t = Math.min(1, Math.max(0, (x - a[0]) / span));
      grid.push([x, a[1] + (b[1] - a[1]) * t]);
    }
    if (grid.length < 20) return [];
    const half = Math.max(1, Math.round(60 / step));
    const grades = grid.map((g, i) => {
      const lo = Math.max(0, i - half);
      const hi = Math.min(grid.length - 1, i + half);
      const dist = Math.max(1e-6, grid[hi][0] - grid[lo][0]);
      return (grid[hi][1] - grid[lo][1]) / dist * 100;
    });
    const runs = [];
    let i = 0;
    while (i < grades.length) {
      if (grades[i] < 3) { i++; continue; }
      let s0 = i, lastSteep = i;
      while (i + 1 < grades.length) {
        i++;
        if (grades[i] >= 3) lastSteep = i;
        else if (grid[i][0] - grid[lastSteep][0] > 250) break;
      }
      runs.push([s0, lastSteep]);
    }
    const out = [];
    runs.forEach(([s0, e0]) => {
      if (e0 <= s0) return;
      const startM = grid[s0][0], endM = grid[e0][0];
      const length = endM - startM;
      if (length < 300) return;
      const gain = grid[e0][1] - grid[s0][1];
      if (gain < 8) return;
      const grade = gain / length * 100;
      const score = gain * (1 + Math.max(0, grade) / 8);
      const label = score >= 900 ? 'HC' : score >= 500 ? '1 级' : score >= 260 ? '2 级' : score >= 120 ? '3 级' : '4 级';
      out.push({
        start_m: startM, end_m: endM, length_m: length,
        gain_m: gain, avg_grade_pct: grade, score, label,
      });
    });
    out.sort((a, b) => b.gain_m - a.gain_m);
    return out.slice(0, 5);
  }

  const CLIMB_LEVELS = ['HC', '1 级', '2 级', '3 级', '4 级'];

  /** 按等级统计：顺序固定（HC → 4 级），没有的等级不出现 */
  function climbLevelCounts(list) {
    const counts = {};
    (list || []).forEach(c => {
      const k = c && c.label;
      if (k) counts[k] = (counts[k] || 0) + 1;
    });
    return counts;
  }

  /** 一句话汇总：3 级 ×1 · 4 级 ×3（按等级高低排，不按出现顺序） */
  function climbLevelSummary(list) {
    const counts = climbLevelCounts(list);
    return CLIMB_LEVELS.filter(k => counts[k]).map(k => k + ' ×' + counts[k]).join(' · ');
  }

  /** 等级筛选：level 为 'all' 或具体等级（'HC' / '3 级' …），返回新数组 */
  function filterClimbs(list, level) {
    const arr = (list || []).filter(Boolean);
    if (!level || level === 'all') return arr.slice();
    return arr.filter(c => c.label === level);
  }

  /** 和 backend geo.elevation_gain 同口径：补 0 值 → 平滑 → 滞回阈值 */
  function climbStats(profile, threshold) {
    const thr = threshold == null ? 1.5 : threshold;
    const alts = fillAltGaps((profile || []).map(p => Number(p[1])));
    if (alts.length < 2) return { gain: 0, loss: 0, min: null, max: null };
    const w = 7;
    const half = Math.floor(w / 2);
    const smooth = alts.map((_, i) => {
      const lo = Math.max(0, i - half), hi = Math.min(alts.length - 1, i + half);
      let sum = 0;
      for (let k = lo; k <= hi; k++) sum += alts[k];
      return sum / (hi - lo + 1);
    });
    const run = (vals) => {
      let gain = 0;
      let ref = vals[0];
      for (let i = 1; i < vals.length; i++) {
        const d = vals[i] - ref;
        if (d >= thr) { gain += d; ref = vals[i]; }
        else if (d < -thr) { ref = vals[i]; }
      }
      return gain;
    };
    // 缺信号的 0 值已经在 fillAltGaps 里补过，最高/最低直接用
    return {
      gain: Math.round(run(smooth)),
      loss: Math.round(run(smooth.map(v => -v))),
      min: Math.round(Math.min.apply(null, alts)),
      max: Math.round(Math.max.apply(null, alts)),
    };
  }

  /* ------------------------------------------------ POI 搜索 */

  function amapReady() { return !!amapKey(); }

  function flattenAmapPoi(p) {
    const loc = String(p.location || '').split(',');
    const lon = Number(loc[0]), lat = Number(loc[1]);
    if (!isFinite(lat) || !isFinite(lon)) return null;
    return {
      name: p.name || '未命名地点',
      address: [p.cityname, p.adname, p.address].filter(Boolean).join(' '),
      lat, lon,
      source: 'amap',
    };
  }

  function flattenPhoton(f) {
    const c = (f.geometry && f.geometry.coordinates) || [];
    const p = f.properties || {};
    if (!isFinite(c[1]) || !isFinite(c[0])) return null;
    const name = p.name || p.street || p.city || '未命名地点';
    const extra = [p.street && p.name ? p.street : '', p.district || p.city, p.state, p.country]
      .filter(Boolean).join(' ');
    return {
      name,
      address: extra,
      lat: Number(c[1]), lon: Number(c[0]),
      source: 'photon',
    };
  }

  async function searchAmap(q, near) {
    const params = new URLSearchParams({
      key: amapKey(),
      keywords: q,
      offset: '10',
      page: '1',
      extensions: 'base',
    });
    if (near && isFinite(near.lat)) {
      params.set('location', `${near.lon},${near.lat}`);
      params.set('sortrule', 'distance');
    }
    const t = withTimeout(8000);
    try {
      const res = await fetch(AMAP_SEARCH + '?' + params.toString(), { signal: t.signal });
      const json = await res.json();
      if (json.status !== '1') {
        // 常见：INVALID_USER_KEY / USERKEY_PLAT_NOMATCH（Key 不是「Web服务」类型）
        throw new Error(`${json.info || '高德搜索失败'}${json.infocode ? `(${json.infocode})` : ''}`);
      }
      return (json.pois || []).map(flattenAmapPoi).filter(Boolean);
    } finally {
      t.done();
    }
  }

  async function searchPhoton(q, near) {
    // lang=de：Photon 没有德语翻译时会回落到 OSM 的本地名，所以能拿到「嘉禾望岗」这种中文名；
    // 用默认的 en 会返回 Jiahewanggang。
    const variants = queryVariants(q);
    for (const v of variants) {
      const params = new URLSearchParams({ q: v, limit: '10', lang: 'de' });
      if (near && isFinite(near.lat)) {
        params.set('lat', String(near.lat));
        params.set('lon', String(near.lon));
        params.set('zoom', '13');
        params.set('location_bias_scale', '0.25');
      }
      const t = withTimeout(9000);
      try {
        const res = await fetch(PHOTON_URL + '?' + params.toString(), { signal: t.signal });
        const json = await res.json();
        let items = (json.features || []).map(flattenPhoton).filter(Boolean);
        items = localFirst(dedupe(items), near);
        if (items.length) return items;
      } catch (err) {
        if (v === variants[variants.length - 1]) throw err;
      } finally {
        t.done();
      }
    }
    return [];
  }

  /** 「嘉禾地铁站」搜不到时，去掉「地铁站/公交站/广场」这类后缀再试 */
  function queryVariants(q) {
    const out = [];
    const push = (v) => {
      const s = String(v || '').trim();
      if (s && out.indexOf(s) < 0) out.push(s);
    };
    push(q);
    const stripped = String(q || '').replace(
      /(地铁站|公交站|火车站|高铁站|客运站|汽车站|机场|码头|广场|公园|大厦|小区|酒店|站)$/u, '',
    ).trim();
    push(stripped);
    return out;
  }

  function distTo(item, near) {
    if (!near || !isFinite(near.lat) || !isFinite(near.lon)) return 0;
    return metersBetween([near.lat, near.lon], [item.lat, item.lon]);
  }

  function dedupe(items) {
    const seen = new Set();
    const out = [];
    items.forEach(it => {
      const key = `${it.name}|${it.lat.toFixed(5)}|${it.lon.toFixed(5)}`;
      if (seen.has(key)) return;
      seen.add(key);
      out.push(it);
    });
    return out;
  }

  /** 同名地点全国都有：有「附近」参照时，把 150 km 外的结果剔掉（除非一个近的都没有） */
  function localFirst(items, near) {
    const sorted = items.slice().sort((a, b) => distTo(a, near) - distTo(b, near));
    if (!near || !isFinite(near.lat) || !isFinite(near.lon)) return sorted;
    const nearOnes = sorted.filter(it => distTo(it, near) <= 150000);
    return nearOnes.length ? nearOnes : sorted;
  }

  /** 手输坐标：「23.12,113.32」「113.32 23.12」都认 */
  function parseLatLon(text) {
    const m = String(text || '').match(/^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,，\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$/);
    if (!m) return null;
    const a = Number(m[1]);
    const b = Number(m[2]);
    if (!isFinite(a) || !isFinite(b)) return null;
    if (Math.abs(a) > 180 || Math.abs(b) > 180) return null;
    // 纬度不会超过 90：谁超过 90 谁就是经度（中国范围内一定是这个顺序）
    const lat = Math.abs(a) > 90 ? b : a;
    const lon = Math.abs(a) > 90 ? a : b;
    if (Math.abs(lat) > 90) return null;
    return { lat, lon, name: `${lat.toFixed(5)},${lon.toFixed(5)}` };
  }

  async function search(q, near) {
    const query = String(q || '').trim();
    if (!query) return { ok: true, items: [] };
    const direct = parseLatLon(query);
    if (direct) {
      return { ok: true, items: [{ name: '坐标点 ' + direct.name, address: '', lat: direct.lat, lon: direct.lon, source: 'coord' }] };
    }
    const useAmap = amapReady() && amapKey() !== badAmapKey;
    try {
      const items = useAmap ? await searchAmap(query, near) : await searchPhoton(query, near);
      return { ok: true, items };
    } catch (err) {
      // 高德失败（Key 类型不对/额度）就换 Photon 顶上，但把原因带回去让界面提示
      const detail = String((err && err.message) || err);
      const warning = `高德不可用：${detail}`;
      if (useAmap) {
        if (/USERKEY_PLAT_NOMATCH|INVALID_USER_KEY|INVALID_USER_DOMAIN/i.test(detail)) {
          badAmapKey = amapKey();      // 这个 Key 类型就不对，本次会话别再白跑
        }
        try {
          return { ok: true, items: await searchPhoton(query, near), warning };
        } catch (err2) { /* fallthrough */ }
      }
      return { ok: false, items: [], error: detail };
    }
  }

  /** 地图上点一个位置，尽量给它起个名字（行者会给途经点自动命名）
      有高德 Key 用高德；没有就用 Photon 反查，lang=de 同样能拿到中文名 */
  async function reverse(lat, lon) {
    if (amapReady()) {
      const params = new URLSearchParams({
        key: amapKey(), location: `${lon},${lat}`, extensions: 'base',
      });
      const t = withTimeout(6000);
      try {
        const res = await fetch(AMAP_REVERSE + '?' + params.toString(), { signal: t.signal });
        const json = await res.json();
        if (json.status === '1') {
          const rc = (json.regeocode || {}).addressComponent || {};
          const aois = (json.regeocode || {}).aois || [];
          const name = (aois[0] && aois[0].name) || rc.township || rc.district || '';
          if (name) return name;
        }
      } catch (e) { /* 落到 Photon */ } finally { t.done(); }
    }
    const t = withTimeout(6000);
    try {
      const url = `${PHOTON_REVERSE}?lat=${lat}&lon=${lon}&limit=1&lang=de`;
      const res = await fetch(url, { signal: t.signal });
      const json = await res.json();
      const p = ((json.features || [])[0] || {}).properties || {};
      return p.name || p.street || p.district || p.city || '';
    } catch (e) {
      return '';
    } finally {
      t.done();
    }
  }

  window.Routing = {
    snap,
    search,
    reverse,
    elevation,
    climbStats,
    climbs,
    CLIMB_LEVELS,
    climbLevelCounts,
    climbLevelSummary,
    filterClimbs,
    decodePolyline,
    metersBetween,
    parseLatLon,
    osrmUrl,
    setOsrmUrl,
    amapKey,
    setAmapKey,
    amapReady,
    DEFAULT_OSRM: OSRM_URL,
  };
})();
