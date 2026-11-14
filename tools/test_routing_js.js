/**
 * 路书路由逻辑测试（Node + 假 fetch / localStorage）。
 *
 * 验证：
 *   1. encoded polyline 解码（Valhalla 精度 1e6）
 *   2. 骑行/徒步走 Valhalla 对应 costing
 *   3. Valhalla 挂了自动退 OSRM，都挂了退直线且给原因
 *   4. POI 搜索：有高德 key 走高德，失败退 Photon；无 key 直接 Photon
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const WEB = path.join(__dirname, '..', 'web', 'js');
let PASSED = 0;
const FAILED = [];

function check(name, cond, detail) {
  if (cond) { PASSED++; console.log('  ok   ' + name); }
  else { FAILED.push(name + ' ' + (detail || '')); console.log('  FAIL ' + name + ' ' + (detail || '')); }
}

function loadRouting(fetchImpl, storage, settings) {
  const store = storage || {};
  const sandbox = {
    console,
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (t) => clearTimeout(t),
    AbortController,
    URLSearchParams,
    Math, JSON, Number, String, isFinite, Object, Array,
    fetch: fetchImpl,
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    },
  };
  sandbox.window = sandbox;
  // 高德 Key 现在只从本地服务的设置来（不再读 localStorage）
  sandbox.PaceApp = { settings: Object.assign({}, settings || {}) };
  vm.createContext(sandbox);
  const code = fs.readFileSync(path.join(WEB, 'routing.js'), 'utf8');
  vm.runInContext(code, sandbox);
  return { Routing: sandbox.Routing, store };
}

function jsonResponse(obj, status) {
  return {
    ok: (status || 200) < 400,
    status: status || 200,
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  };
}

/* Valhalla 的 shape 是精度 1e6 的 polyline：手写一段 3 个点的 */
function encode(pts, precision) {
  const factor = Math.pow(10, precision);
  const enc = (v) => {
    let value = v < 0 ? ~(v << 1) : (v << 1);
    let out = '';
    while (value >= 0x20) {
      out += String.fromCharCode((0x20 | (value & 0x1f)) + 63);
      value >>= 5;
    }
    out += String.fromCharCode(value + 63);
    return out;
  };
  let lastLat = 0, lastLon = 0, out = '';
  pts.forEach(([lat, lon]) => {
    const ilat = Math.round(lat * factor);
    const ilon = Math.round(lon * factor);
    out += enc(ilat - lastLat) + enc(ilon - lastLon);
    lastLat = ilat; lastLon = ilon;
  });
  return out;
}

async function main() {
  console.log('\n[1] polyline 解码');
  {
    const { Routing } = loadRouting(async () => jsonResponse({}));
    const pts = [[23.1, 113.3], [23.12, 113.33]];
    const shape = encode(pts, 6);
    const back = Routing.decodePolyline(shape, 6);
    check('点数对得上', back.length === 2, String(back.length));
    check('纬度对得上', Math.abs(back[0][0] - 23.1) < 1e-6, JSON.stringify(back[0]));
    check('经度对得上', Math.abs(back[1][1] - 113.33) < 1e-6, JSON.stringify(back[1]));
  }

  console.log('\n[2] 吸附：Valhalla 优先');
  {
    const calls = [];
    const { Routing } = loadRouting(async (url, opts) => {
      calls.push({ url, opts });
      if (String(url).includes('valhalla')) {
        const body = JSON.parse(opts.body);
        return jsonResponse({
          trip: { legs: [{ shape: encode([[23.1, 113.3], [23.12, 113.33]], 6) }] },
          costing: body.costing,
        });
      }
      throw new Error('不该走 OSRM');
    });
    const res = await Routing.snap([{ lat: 23.1, lon: 113.3 }, { lat: 23.12, lon: 113.33 }], 'road');
    check('ok', res.ok === true, JSON.stringify(res));
    check('provider=valhalla', res.provider === 'valhalla', res.provider);
    check('骑行用 bicycle', JSON.parse(calls[0].opts.body).costing === 'bicycle',
      JSON.parse(calls[0].opts.body).costing);
    const walk = await Routing.snap([{ lat: 23.1, lon: 113.3 }, { lat: 23.12, lon: 113.33 }], 'walk');
    check('徒步用 pedestrian', walk.ok && JSON.parse(calls[1].opts.body).costing === 'pedestrian',
      JSON.parse(calls[1].opts.body).costing);
  }

  console.log('\n[3] 吸附：降到 OSRM / 直线');
  {
    const { Routing } = loadRouting(async (url) => {
      if (String(url).includes('valhalla')) throw new Error('valhalla down');
      return jsonResponse({
        code: 'Ok',
        routes: [{ geometry: { coordinates: [[113.3, 23.1], [113.33, 23.12]] } }],
      });
    });
    const res = await Routing.snap([{ lat: 23.1, lon: 113.3 }, { lat: 23.12, lon: 113.33 }], 'road');
    check('OSRM 兜底成功', res.ok && res.provider === 'osrm', JSON.stringify(res));

    const { Routing: R2 } = loadRouting(async () => { throw new Error('offline'); });
    const fail = await R2.snap([{ lat: 23.1, lon: 113.3 }, { lat: 23.12, lon: 113.33 }], 'road');
    check('全挂时退直线', !fail.ok && fail.coords.length === 2, JSON.stringify(fail));
    check('退直线有原因', !!fail.reason, fail.reason);
    check('两点以内不吸附',
      (await R2.snap([{ lat: 23.1, lon: 113.3 }], 'road')).ok === false, 'single');
    const st = await R2.snap([{ lat: 1, lon: 2 }, { lat: 3, lon: 4 }], 'straight');
    check('直线模式不请求', st.provider === 'straight' && st.coords.length === 2, st.provider);
  }

  console.log('\n[4] POI 搜索');
  {
    const calls = [];
    const { Routing } = loadRouting(async (url) => {
      calls.push(String(url));
      if (String(url).includes('amap')) {
        return jsonResponse({ status: '1', pois: [{ name: '天河体育中心', location: '113.32,23.14', cityname: '广州市', adname: '天河区', address: '体育东路' }] });
      }
      return jsonResponse({ features: [] });
    }, {}, { amap_key: 'test-key' });
    const res = await Routing.search('天河', { lat: 23.13, lon: 113.32 });
    check('有 key 走高德', calls[0].includes('amap'), calls[0]);
    check('结果解出坐标', res.items.length === 1 && Math.abs(res.items[0].lon - 113.32) < 1e-6,
      JSON.stringify(res.items));

    const calls2 = [];
    const { Routing: R2 } = loadRouting(async (url) => {
      calls2.push(String(url));
      if (String(url).includes('amap')) return jsonResponse({ status: '0', info: 'INVALID_USER_KEY' });
      return jsonResponse({ features: [{ geometry: { coordinates: [113.3, 23.1] }, properties: { name: 'Tianhe', city: 'Guangzhou', country: 'China' } }] });
    }, {}, { amap_key: 'bad' });
    const res2 = await R2.search('天河');
    check('高德失败退 Photon', calls2.some(u => u.includes('photon')) && res2.items.length === 1,
      JSON.stringify(calls2));
    check('Photon 结果可读', res2.items[0] && res2.items[0].name === 'Tianhe', JSON.stringify(res2.items[0]));

    const { Routing: R3 } = loadRouting(async () => jsonResponse({ features: [] }));
    const res3 = await R3.search('  ');
    check('空关键词不请求', res3.items.length === 0, JSON.stringify(res3));
  }

  console.log('\n[5] 海拔与爬升');
  {
    const { Routing } = loadRouting(async (url) => {
      if (String(url).includes('height')) {
        // 40 个点，海拔从 10 缓升到 110
        const pts = [];
        for (let i = 0; i < 40; i++) pts.push([i * 200, 10 + i * 2.5]);
        return jsonResponse({ shape: [], range_height: pts });
      }
      return jsonResponse({});
    });
    const route = [];
    for (let i = 0; i < 40; i++) route.push([23.1 + i * 0.001, 113.3]);
    const res = await Routing.elevation(route, 320);
    check('海拔接口 ok', res.ok === true, JSON.stringify(res.reason));
    check('剖面点数一致', res.profile.length === 40, String(res.profile.length));
    const st = Routing.climbStats(res.profile);
    check('累计爬升接近 97 m（平滑后略少）', st.gain >= 80 && st.gain <= 105, String(st.gain));
    check('最高/最低海拔',
      st.max === 108 && st.min === 10, JSON.stringify({ max: st.max, min: st.min }));

    const flat = [];
    for (let i = 0; i < 40; i++) flat.push([i * 200, 20 + (i % 2 ? 0.8 : -0.8)]);
    const flatStats = Routing.climbStats(flat);
    check('平坦的小抖动不算爬升', flatStats.gain === 0, String(flatStats.gain));

    const { Routing: R2 } = loadRouting(async () => { throw new Error('height down'); });
    const bad = await R2.elevation(route, 320);
    check('海拔服务挂了不抛异常', bad.ok === false && bad.profile.length === 0, JSON.stringify(bad));
    check('海拔失败有原因', !!bad.reason, bad.reason);
  }

  console.log('\n[6] 防绕路');
  {
    const calls = [];
    const { Routing } = loadRouting(async (url, opts) => {
      const body = JSON.parse(opts.body);
      calls.push(body.costing);
      const a = body.locations[0], b = body.locations[1];
      if (body.locations.length === 2 && body.costing === 'bicycle') {
        // 故意给一条 30 km 的绕路（直线上只有 ~1.4 km）
        const shape = [];
        for (let i = 0; i < 30; i++) {
          shape.push([a.lat + i * 0.01, a.lon + i * 0.01]);
        }
        return jsonResponse({ trip: { legs: [{ shape: encode(shape, 6) }] } });
      }
      return jsonResponse({ trip: { legs: [{ shape: encode([[a.lat, a.lon], [b.lat, b.lon]], 6) }] } });
    });
    const pts = [{ lat: 23.10, lon: 113.30 }, { lat: 23.11, lon: 113.31 }];
    const res = await Routing.snap(pts, 'road');
    check('识别到绕路并换成徒步', res.detours.length === 1, JSON.stringify(res.detours));
    check('换了之后更短', res.detours[0].after < res.detours[0].before,
      JSON.stringify(res.detours[0]));
    check('确实请求了 pedestrian', calls.includes('pedestrian'), JSON.stringify(calls));
    let len = 0;
    for (let i = 1; i < res.coords.length; i++) len += Routing.metersBetween(res.coords[i - 1], res.coords[i]);
    check('最终路线接近直线', len < 2500, String(Math.round(len)));
  }

  console.log('\n[7] 中文搜索');
  {
    // Photon 默认 lang=en 会返回英文名；我们用 lang=de 让 OSM 回落到本地中文名
    const calls = [];
    const { Routing } = loadRouting(async (url) => {
      calls.push(String(url));
      const u = String(url);
      if (u.includes('photon')) {
        if (u.includes(encodeURIComponent('地铁站')) || u.includes('%E5%9C%B0%E9%93%81%E7%AB%99')) {
          return jsonResponse({ features: [] });          // 「嘉禾地铁站」查不到
        }
        return jsonResponse({ features: [{
          geometry: { coordinates: [113.2837, 23.2406] },
          properties: { name: '嘉禾望岗', city: 'Guangzhou', district: '嘉禾街道', country: 'China' },
        }] });
      }
      return jsonResponse({});
    });
    const res = await Routing.search('嘉禾地铁站');
    check('中文查询有结果', res.ok && res.items.length === 1, JSON.stringify(res));
    check('返回中文名', res.items[0] && res.items[0].name === '嘉禾望岗', JSON.stringify(res.items[0]));
    check('Photon 用 lang=de', calls.some(u => u.includes('lang=de')), JSON.stringify(calls.slice(0, 2)));
    check('去掉了「地铁站」后缀再搜',
      calls.some(u => u.includes(encodeURIComponent('嘉禾')) || u.includes('%E5%98%89%E7%A6%BE')),
      JSON.stringify(calls));

    const coord = await Routing.search('23.12,113.32');
    check('直接输坐标也能用', coord.items.length === 1 && Math.abs(coord.items[0].lat - 23.12) < 1e-6,
      JSON.stringify(coord.items));
    const coord2 = Routing.parseLatLon('113.32 23.12');
    check('坐标顺序自动判断', coord2 && Math.abs(coord2.lat - 23.12) < 1e-6, JSON.stringify(coord2));
    check('不是坐标就不乱认', Routing.parseLatLon('天河体育中心') === null, 'no-coord');

    // 反查：没有高德 Key 时用 Photon，也要中文
    const { Routing: R2 } = loadRouting(async (url) => {
      if (String(url).includes('reverse')) {
        return jsonResponse({ features: [{ properties: { name: '嘉禾望岗 C出入口', district: '嘉禾街道' } }] });
      }
      return jsonResponse({});
    });
    const name = await R2.reverse(23.24, 113.28);
    check('点地图能自动取中文名', name === '嘉禾望岗 C出入口', name);
  }

  console.log('\n[8] 高德 Key 类型不对');
  {
    const calls = [];
    const { Routing } = loadRouting(async (url) => {
      const u = String(url);
      calls.push(u);
      if (u.includes('amap')) {
        return jsonResponse({ status: '0', info: 'USERKEY_PLAT_NOMATCH', infocode: '10009' });
      }
      return jsonResponse({ features: [{
        geometry: { coordinates: [113.28, 23.24] },
        properties: { name: '嘉禾望岗', city: 'Guangzhou' },
      }] });
    }, {}, { amap_key: 'bad-key' });
    const res = await Routing.search('嘉禾地铁站', { lat: 23.2, lon: 113.3 });
    check('自动降级到 Photon', res.ok && res.items.length === 1, JSON.stringify(res));
    check('把高德错误带回来', /USERKEY_PLAT_NOMATCH/.test(res.warning || ''), res.warning);
    const amapCalls1 = calls.filter(u => u.includes('amap')).length;
    await Routing.search('天河体育中心', { lat: 23.2, lon: 113.3 });
    const amapCalls2 = calls.filter(u => u.includes('amap')).length;
    check('坏 Key 本次会话不再重复请求', amapCalls2 === amapCalls1, `${amapCalls1} -> ${amapCalls2}`);
  }

  console.log('\n[9] 骑行偏好：走大路');
  {
    const bodies = [];
    const { Routing } = loadRouting(async (url, opts) => {
      if (String(url).includes('valhalla')) {
        bodies.push(JSON.parse(opts.body));
        return jsonResponse({ trip: { legs: [{ shape: encode([[23.1, 113.3], [23.12, 113.33]], 6) }] } });
      }
      throw new Error('不该走 OSRM');
    });
    await Routing.snap([{ lat: 23.1, lon: 113.3 }, { lat: 23.12, lon: 113.33 }], 'road');
    const bike = (bodies[0].costing_options || {}).bicycle || {};
    check('带上了骑行偏好参数', !!bodies[0].costing_options, JSON.stringify(bodies[0]).slice(0, 80));
    check('公路车型（偏好铺装大路）', bike.bicycle_type === 'Road', String(bike.bicycle_type));
    check('少走生活街区小路', bike.use_living_streets <= 0.1, String(bike.use_living_streets));
    check('尽量用主路', bike.use_roads >= 0.9, String(bike.use_roads));
    check('少走土路/小径', (bike.use_tracks || 0) <= 0.1 && bike.avoid_bad_surfaces >= 0.8,
      JSON.stringify({ tracks: bike.use_tracks, bad: bike.avoid_bad_surfaces }));

    const walk = await Routing.snap([{ lat: 23.1, lon: 113.3 }, { lat: 23.12, lon: 113.33 }], 'walk');
    const ped = (bodies[bodies.length - 1].costing_options || {}).bicycle;
    check('徒步不带骑行参数', !ped, JSON.stringify(ped));
  }

  console.log('\n[10] 超过 10 个点 / 重复点');
  {
    const bodies = [];
    const { Routing } = loadRouting(async (url, opts) => {
      if (String(url).includes('valhalla')) {
        const body = JSON.parse(opts.body);
        bodies.push(body);
        const locs = body.locations;
        const shape = locs.map(l => [l.lat, l.lon]);
        return jsonResponse({ trip: { legs: locs.slice(1).map((l, i) => ({ shape: encode([shape[i], shape[i + 1]], 6) })) } });
      }
      throw new Error('不该降级');
    });
    const pts = [];
    for (let i = 0; i < 12; i++) pts.push({ lat: 23.10 + i * 0.01, lon: 113.30 + i * 0.01 });
    const res = await Routing.snap(pts, 'road');
    check('12 个点自动分段', bodies.length === 2, `requests=${bodies.length}`);
    check('每段不超过 10 个点', bodies.every(b => b.locations.length <= 10),
      JSON.stringify(bodies.map(b => b.locations.length)));
    check('分段算完还是连贯的', res.ok && res.coords.length >= 12, String(res.coords.length));

    const dup = [{ lat: 23.1, lon: 113.3 }, { lat: 23.1, lon: 113.3001 },
      { lat: 23.2, lon: 113.4 }, { lat: 23.2, lon: 113.4001 }];
    const calls = [];
    const { Routing: R2 } = loadRouting(async (url, opts) => {
      if (String(url).includes('valhalla')) {
        const body = JSON.parse(opts.body);
        calls.push(body.locations.length);
        return jsonResponse({ trip: { legs: [{ shape: encode([[23.1, 113.3], [23.2, 113.4]], 6) }] } });
      }
      throw new Error('不该降级');
    });
    await R2.snap(dup, 'road');
    check('重复点先去重再规划', calls[0] === 2, `locations=${calls[0]}`);
  }

  console.log('\n[11] 绕行提示');
  {
    // 第一段故意给一条比直线长 5 倍的路线，应该报 detourLegs
    const { Routing } = loadRouting(async (url, opts) => {
      const body = JSON.parse(opts.body);
      const a = body.locations[0], b = body.locations[1];
      const long = [];
      for (let i = 0; i <= 20; i++) long.push([a.lat + i * 0.004, a.lon + i * 0.004]);
      const shape = bodies_index === 0 ? long : [a, b];
      bodies_index++;
      return jsonResponse({ trip: { legs: [{ shape: encode(shape, 6) }] } });
    });
    var bodies_index = 0;
    const res = await Routing.snap([{ lat: 23.10, lon: 113.30 }, { lat: 23.11, lon: 113.31 }], 'road');
    check('报出绕行段', (res.detourLegs || []).length === 1, JSON.stringify(res.detourLegs));
    check('带上直线/实际里程', res.detourLegs[0].directKm > 0 && res.detourLegs[0].km > res.detourLegs[0].directKm,
      JSON.stringify(res.detourLegs[0]));
  }

  console.log('\n[12] 爬坡识别');
  {
    const { Routing } = loadRouting(async () => jsonResponse({}));
    // 10 公里，中间 2 公里爬 100 m（5%）
    const prof = [];
    for (let d = 0; d <= 10000; d += 25) {
      let alt = 20;
      if (d >= 4000 && d <= 6000) alt = 20 + (d - 4000) * 0.05;
      else if (d > 6000) alt = 120;
      prof.push([d, alt]);
    }
    const cs = Routing.climbs(prof);
    check('识别出一段爬坡', cs.length === 1, JSON.stringify(cs));
    check('长度约 2 km', Math.abs(cs[0].length_m - 2000) < 120, String(cs[0] && cs[0].length_m));
    check('坡度约 5%', Math.abs(cs[0].avg_grade_pct - 5) < 0.6, String(cs[0] && cs[0].avg_grade_pct));
    check('等级按爬升量分级（100 m / 5% → 3 级）', cs[0].label === '3 级', cs[0].label);
    const flat = [];
    for (let d = 0; d <= 10000; d += 25) flat.push([d, 20]);
    check('平路没有爬坡', Routing.climbs(flat).length === 0, JSON.stringify(Routing.climbs(flat)));
  }

  console.log('\n[13] 爬坡等级筛选（详情页）');
  {
    const { Routing } = loadRouting(async () => jsonResponse({}));
    const list = [
      { label: '4 级', gain_m: 12 },
      { label: '3 级', gain_m: 100 },
      { label: '3 级', gain_m: 130 },
      { label: 'HC', gain_m: 1200 },
    ];
    check('等级顺序固定 HC → 4 级', Routing.CLIMB_LEVELS.join(',') === 'HC,1 级,2 级,3 级,4 级',
      Routing.CLIMB_LEVELS.join(','));
    const counts = Routing.climbLevelCounts(list);
    check('按等级计数', counts['3 级'] === 2 && counts['HC'] === 1, JSON.stringify(counts));
    check('汇总按等级高低排', Routing.climbLevelSummary(list) === 'HC ×1 · 3 级 ×2 · 4 级 ×1',
      Routing.climbLevelSummary(list));
    check('空列表汇总为空', Routing.climbLevelSummary([]) === '', Routing.climbLevelSummary([]));
    check('全部不过滤', Routing.filterClimbs(list, 'all').length === 4);
    check('按等级筛选', Routing.filterClimbs(list, '3 级').length === 2);
    check('没出现过的等级筛出 0 段', Routing.filterClimbs(list, '1 级').length === 0);
    const copy = Routing.filterClimbs(list, 'all');
    copy.pop();
    check('筛选返回新数组，不动原列表', list.length === 4, String(list.length));
    const prof = [];
    for (let d = 0; d <= 8000; d += 25) {
      let alt = 20;
      if (d >= 2000 && d <= 5000) alt = 20 + (d - 2000) * 0.04;
      else if (d > 5000) alt = 140;
      prof.push([d, alt]);
    }
    const cs = Routing.climbs(prof);
    check('climbs 的等级都在已知等级里', cs.length > 0 && cs.every(c => Routing.CLIMB_LEVELS.includes(c.label)),
      JSON.stringify(cs.map(c => c.label)));
  }

  console.log('\n' + '='.repeat(56));
  if (FAILED.length) {
    console.log(`失败 ${FAILED.length} 项 / 通过 ${PASSED} 项`);
    FAILED.forEach(f => console.log('  - ' + f));
    process.exit(1);
  }
  console.log(`全部通过（${PASSED} 项）`);
}

main().catch((err) => { console.error(err); process.exit(1); });
