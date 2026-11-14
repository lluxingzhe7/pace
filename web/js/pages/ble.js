/* Web Bluetooth 采集面板（带完整诊断）。

   设计教训：
   1. 上一版在订阅特征值时把异常全部吞掉，导致"点了没反应"。
   2. 只暴露 1800/1801（Generic Access / Generic Attribute）说明设备没有
      把运动数据服务开放给浏览器——这是协议层的问题，不是代码能绕过的。
      本页要把这个判断直接告诉用户，而不是让人反复点按钮。

   页面会依次给出：设备名 → 服务列表 → 每个特征值的属性 → 订阅/读取结果。
*/
(function () {
  const state = {
    device: null,
    server: null,
    services: [],       // [{ uuid, chars: [{uuid, props, notifyOk, error, reading}] }]
    events: [],
    active: 0,
    supported: false,
    logs: [],
    deviceInfo: {},
    scanRounds: 0,
    knownDevice: null,
  };
  let host = null;

  // 标准服务短 UUID -> 说明
  const KNOWN_SERVICES = {
    '1800': 'Generic Access（设备名/外观）',
    '1801': 'Generic Attribute（服务变更）',
    '180a': 'Device Information（厂商/型号）',
    '180d': 'Heart Rate（心率）',
    '1816': 'Cycling Speed and Cadence（速度/踏频）',
    '1818': 'Cycling Power（功率）',
    '1826': 'Fitness Machine（FTMS）',
    '180f': 'Battery（电量）',
    'fe59': 'Nordic DFU（固件升级）',
  };

  const KNOWN_CHARS = {
    '2a00': 'Device Name', '2a01': 'Appearance', '2a04': 'Peripheral Preferred Params',
    '2a05': 'Service Changed', '2a19': 'Battery Level', '2a24': 'Model Number',
    '2a25': 'Serial Number', '2a26': 'Firmware Revision', '2a27': 'Hardware Revision',
    '2a28': 'Software Revision', '2a29': 'Manufacturer Name',
    '2a37': 'Heart Rate Measurement', '2a38': 'Body Sensor Location',
    '2a5b': 'CSC Measurement', '2a5c': 'CSC Feature',
    '2a63': 'Cycling Power Measurement', '2a65': 'Cycling Power Feature',
    '2ad2': 'Indoor Bike Data', '2ad9': 'Fitness Machine Control Point',
  };

  function supported() {
    return typeof navigator !== 'undefined' && 'bluetooth' in navigator && isSecureContext;
  }

  function short(uuid) {
    return (uuid || '').slice(4, 8).toLowerCase();
  }

  function log(msg, kind) {
    state.logs.push({ at: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
                      msg, kind: kind || 'info' });
    if (state.logs.length > 300) state.logs.shift();
    renderLog();
  }

  function renderLog() {
    const box = document.getElementById('bleLog');
    if (!box) return;
    const color = { info: 'var(--text-3)', ok: 'var(--good)', warn: 'var(--warn)',
                    err: 'var(--bad)' };
    box.innerHTML = state.logs.slice(-50).map(l =>
      `<div class="mono tiny" style="color:${color[l.kind] || color.info}">
        ${l.at} ${U.esc(l.msg)}</div>`).join('');
    box.scrollTop = box.scrollHeight;
  }

  function render() {
    const onlyBase = state.services.length > 0 &&
      state.services.every(s => ['1800', '1801'].includes(short(s.uuid)));
    const known = state.knownDevice;
    const verdict = known && known.verdict === 'unsupported'
      ? `<div class="result-row" style="margin-top:12px;border:1px solid color-mix(in srgb, var(--bad) 45%, transparent)">
           <span style="color:var(--bad)">${Icon.get('x')}</span>
           <div>
             <b style="color:var(--bad)">${U.esc(known.name)}：浏览器直读不可行</b>
             <p class="tiny muted" style="margin-top:4px;line-height:1.7">${U.esc(known.reason || '')}</p>
             <p class="tiny dim" style="margin-top:6px">可行路径：
               ${(known.working_paths || []).map(p => ({
                 intervals: '手机 App → Intervals.icu → 本页同步',
                 usb_fit: '数据线拷 FIT 到「数据源」',
               })[p] || p).map(U.esc).join(' ／ ')}
             </p>
           </div>
         </div>`
      : '';
    host.innerHTML = `
      <div class="card">
        <div class="card-head">
          <h3>蓝牙采集</h3>
          <span class="dim">Web Bluetooth · 需 Chrome/Edge · https 或 localhost</span>
          <div class="spacer"></div>
          <span class="chip" id="bleState">${state.device ? '已连接' : '未连接'}</span>
        </div>
        <div class="card-body">
          ${state.supported ? '' : `
            <div class="result-row" style="margin-bottom:12px">
              <span class="err">${Icon.get('info')}</span>
              <span class="muted">当前环境不支持 Web Bluetooth。需要 Chromium 内核浏览器，
                且页面地址是 localhost 或 https。</span>
            </div>`}

          ${state.device ? `
            <div class="result-row" style="margin-bottom:12px">
              <span class="dim">${Icon.get('link')}</span>
              <span><b>${U.esc(state.deviceInfo.name || state.device.name || '未命名设备')}</b></span>
              <span class="mono tiny dim">${U.esc(state.device.id)}</span>
              ${state.deviceInfo.manufacturer ? `<span class="chip">${U.esc(state.deviceInfo.manufacturer)}</span>` : ''}
            </div>` : ''}

          ${verdict}

          <div class="row" style="gap:8px;flex-wrap:wrap">
            <button class="btn btn-primary btn-sm" id="bleConnect" ${state.supported ? '' : 'disabled'}>
              ${Icon.get('link')}${state.device ? '重新连接' : '连接设备'}
            </button>
            <button class="btn btn-sm" id="bleSubscribe" ${state.services.length ? '' : 'disabled'}>
              ${Icon.get('play')}开始接收
            </button>
            <button class="btn btn-sm" id="bleStop" disabled>${Icon.get('pause')}停止</button>
            <button class="btn btn-sm" id="bleRescan" ${state.server ? '' : 'disabled'}>
              ${Icon.get('spark')}重新扫描服务
            </button>
            <button class="btn btn-sm" id="bleSave" ${state.events.length ? '' : 'disabled'}>
              ${Icon.get('download')}保存抓包
            </button>
            <button class="btn btn-sm btn-ghost" id="bleClear">清空日志</button>
          </div>

          ${onlyBase ? `
            <div class="result-row" style="margin-top:12px;border:1px solid color-mix(in srgb, var(--warn) 45%, transparent)">
              <span style="color:var(--warn)">${Icon.get('info')}</span>
              <div>
                <b style="color:var(--warn)">只发现了基础服务（1800 / 1801）</b>
                <p class="tiny muted" style="margin-top:4px;line-height:1.7">
                  ${state.deviceInfo.name ? `当前设备自称「<b>${U.esc(state.deviceInfo.name)}</b>」。<br>` : ''}
                  1800/1801 是蓝牙协议自带的基础服务，<b>不包含任何运动数据</b>。
                  说明设备没有把数据通道开放给浏览器。按可能性排序：
                  <br><b>1.</b> 选错了设备（附近的传感器/耳机也会出现在列表里，先看上面的设备名）
                  <br><b>2.</b> 码表已被手机 App 占用——同一时间只允许一个连接，先把手机蓝牙关掉再试
                  <br><b>3.</b> 码表的 BLE 只用于设置/固件，运动数据走蓝牙经典（SPP）或只存成 FIT 文件
                  <br><b>4.</b> 需要先在系统蓝牙里配对（配对后部分设备才会开放私有服务）
                </p>
                <div class="row" style="margin-top:8px;gap:8px">
                  <button class="btn btn-sm" id="bleCopyTree">复制完整 GATT 树</button>
                </div>
              </div>
            </div>` : ''}

          <div class="grid g-2" style="margin-top:14px;align-items:start">
            <div>
              <div class="tiny dim" style="margin-bottom:6px">服务与特征值（诊断）</div>
              <div id="bleServices" class="ble-scroll"></div>
            </div>
            <div>
              <div class="tiny dim" style="margin-bottom:6px">运行日志</div>
              <div id="bleLog" class="ble-scroll mono"></div>
            </div>
          </div>

          <div id="bleLive" style="margin-top:14px"></div>

          <details style="margin-top:14px">
            <summary class="tiny dim" style="cursor:pointer">还是不行？三条替代路径</summary>
            <div class="tiny muted" style="margin-top:8px;line-height:1.9">
              <b style="color:var(--text)">A. 手机 App → Intervals.icu（推荐）</b><br>
              手机上 iGPSPORT App 正常记录并上传 → 在 iGPSPORT Ride（国际版）里绑定
              Intervals.icu → 电脑 PACE「数据源」填 Athlete ID + API Key → 同步。
              无线、走官方接口、不用抓包。<br>
              <b style="color:var(--text)">B. 数据线拷 FIT</b><br>
              码表连电脑选「文件传输」→ PACE「数据源」自动识别设备目录。<br>
              <b style="color:var(--text)">C. 另配标准传感器</b><br>
              速度/踏频/心率传感器多为标准 1816/180D 服务，浏览器可以直接读。
              这也是目前 Web Bluetooth 唯一能稳定拿到实时数据的路径。
            </div>
          </details>
        </div>
      </div>`;

    Icon.hydrate(host);
    const on = (id, fn) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('click', fn);
    };
    on('bleConnect', connect);
    on('bleSubscribe', subscribeAll);
    on('bleStop', stopAll);
    on('bleRescan', () => enumerateServices(true));
    on('bleSave', saveCapture);
    on('bleClear', () => { state.logs = []; renderLog(); });
    on('bleCopyTree', copyTree);
    renderServices();
    renderLog();
    renderLive();
  }

  function gattTree() {
    return {
      device: state.device ? { name: state.device.name, id: state.device.id } : null,
      info: state.deviceInfo,
      services: state.services.map(s => ({
        uuid: s.uuid,
        known: KNOWN_SERVICES[short(s.uuid)] || null,
        chars: s.chars.map(c => ({
          uuid: c.uuid,
          known: KNOWN_CHARS[short(c.uuid)] || null,
          properties: c.props,
          notifyOk: c.notifyOk,
          error: c.error,
        })),
      })),
    };
  }

  async function copyTree() {
    const text = JSON.stringify(gattTree(), null, 2);
    try {
      await navigator.clipboard.writeText(text);
      UI.toast('GATT 树已复制，可直接粘贴给我', 'ok');
      log('GATT 树已复制到剪贴板', 'ok');
    } catch (err) {
      log('剪贴板不可用，请从控制台复制（已打印）', 'warn');
      console.log(text);
    }
  }

  /* ------------------------------------------------------------ 连接 */

  async function connect() {
    if (!supported()) return;
    log('请求选择设备…');
    state.device = null;
    state.server = null;
    state.services = [];
    state.deviceInfo = {};
    try {
      const device = await navigator.bluetooth.requestDevice({
        acceptAllDevices: true,
        optionalServices: [
          'heart_rate', 'cycling_speed_and_cadence', 'cycling_power',
          'fitness_machine', 'battery_service', 'device_information',
          'generic_access', 'generic_attribute',
        ],
      });
      state.device = device;
      log(`已选择：${device.name || '未命名'} (${device.id})`, 'ok');
      device.addEventListener('gattserverdisconnected', () => {
        state.device = null;
        state.server = null;
        state.services = [];
        state.active = 0;
        log('设备断开', 'warn');
        render();
      });

      log('连接 GATT…');
      state.server = await device.gatt.connect();
      log('GATT 已连接', 'ok');
      await enumerateServices();
      await readDeviceInfo();
      render();
    } catch (err) {
      log(`连接失败：${err.name} ${err.message}`, 'err');
      if (err.name !== 'NotFoundError') {
        UI.toast('连接失败：' + U.esc(err.message), 'err');
      }
      render();
    }
  }

  async function enumerateServices(force) {
    if (!state.server) return;
    if (force) {
      state.scanRounds += 1;
      state.services = [];
      log(`第 ${state.scanRounds + 1} 次扫描服务…`, 'info');
    }
    let services = [];
    try {
      services = await state.server.getPrimaryServices();
      log(`枚举到 ${services.length} 个主服务`, services.length ? 'ok' : 'warn');
    } catch (err) {
      log(`枚举服务失败：${err.name} ${err.message}`, 'err');
      return;
    }
    if (!services.length) {
      log('没有拿到任何服务。设备可能只广播、不提供 GATT，或需要先唤醒。', 'warn');
      return;
    }

    // 去重（重新扫描时可能重复）
    const seen = new Set();
    for (const svc of services) {
      const key = svc.uuid;
      if (seen.has(key)) continue;
      seen.add(key);
      const node = { uuid: svc.uuid, chars: [] };
      const label = KNOWN_SERVICES[short(svc.uuid)];
      try {
        const chars = await svc.getCharacteristics();
        for (const ch of chars) {
          const props = ch.properties || {};
          node.chars.push({
            uuid: ch.uuid,
            props: {
              notify: !!props.notify, indicate: !!props.indicate,
              read: !!props.read, write: !!props.write,
              writeWithoutResponse: !!props.writeWithoutResponse,
            },
            obj: ch,
            notifyOk: false,
            error: null,
          });
        }
        log(`服务 ${short(svc.uuid).toUpperCase()}${label ? '（' + label + '）' : ''}：${chars.length} 个特征值`, 'info');
      } catch (err) {
        node.error = err.message;
        log(`服务 ${short(svc.uuid).toUpperCase()} 读取特征值失败：${err.message}`, 'warn');
      }
      state.services.push(node);
    }

    const onlyBase = state.services.length > 0 &&
      state.services.every(s => ['1800', '1801'].includes(short(s.uuid)));
    if (onlyBase) {
      log('注意：只发现基础服务，没有运动数据服务。详见页面提示。', 'warn');
    }
  }

  /** 从 1800 里读设备名与外观，确认连的到底是哪台设备。 */
  async function readDeviceInfo() {
    const ga = state.services.find(s => short(s.uuid) === '1800');
    const di = state.services.find(s => short(s.uuid) === '180a');
    for (const svc of [ga, di]) {
      if (!svc) continue;
      for (const ch of svc.chars) {
        const id = short(ch.uuid);
        if (!ch.props.read) continue;
        if (!['2a00', '2a24', '2a29', '2a25', '2a26'].includes(id)) continue;
        try {
          const view = await ch.obj.readValue();
          const bytes = new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
          const text = new TextDecoder().decode(bytes).replace(/\0/g, '').trim();
          if (!text) continue;
          if (id === '2a00') state.deviceInfo.name = text;
          if (id === '2a29') state.deviceInfo.manufacturer = text;
          if (id === '2a24') state.deviceInfo.model = text;
          if (id === '2a26') state.deviceInfo.firmware = text;
          log(`读取 ${KNOWN_CHARS[id] || id} = ${text}`, 'ok');
        } catch (err) {
          log(`读取 ${KNOWN_CHARS[id] || id} 失败：${err.name}`, 'info');
        }
      }
    }
    // 查设备档案，直接给出"行不行"的结论
    const name = state.deviceInfo.name || (state.device && state.device.name);
    if (name) {
      try {
        const res = await api.lookupBleDevice(name);
        if (res && res.device) {
          state.knownDevice = res.device;
          log(`命中设备档案：${res.device.name}（判定：${res.device.verdict === 'unsupported' ? '浏览器直读不可行' : '可用'}）`,
              res.device.verdict === 'unsupported' ? 'warn' : 'ok');
          const dataSvcs = (res.device.observed && res.device.observed.data_services_found) || [];
          if (res.device.verdict === 'unsupported' && !dataSvcs.length) {
            log('该型号只有 GAP/GATT 基础服务，运动数据不走标准 BLE。建议改用 Intervals.icu 或数据线。', 'err');
          }
        }
      } catch (e) { /* 档案查询失败不影响连接 */ }
    }
  }

  function propsText(p) {
    const tags = [];
    if (p.notify) tags.push('notify');
    if (p.indicate) tags.push('indicate');
    if (p.read) tags.push('read');
    if (p.write) tags.push('write');
    if (p.writeWithoutResponse) tags.push('writeNR');
    return tags.join(', ') || '无';
  }

  function renderServices() {
    const box = document.getElementById('bleServices');
    if (!box) return;
    if (!state.services.length) {
      box.innerHTML = '<p class="tiny dim">尚未连接或没有发现服务。</p>';
      return;
    }
    box.innerHTML = state.services.map(svc => `
      <div style="margin-bottom:10px;padding:8px 10px;background:var(--bg-elev-2);border-radius:8px">
        <div class="mono tiny" style="color:var(--accent-2)">
          ${short(svc.uuid).toUpperCase()} · ${U.esc(svc.uuid)}
        </div>
        ${KNOWN_SERVICES[short(svc.uuid)] ? `<div class="tiny dim">${U.esc(KNOWN_SERVICES[short(svc.uuid)])}</div>` : ''}
        ${svc.chars.length ? svc.chars.map(ch => `
          <div class="row between" style="gap:8px;padding:3px 0">
            <span class="mono tiny">${short(ch.uuid).toUpperCase()}</span>
            <span class="tiny dim" style="flex:1">${U.esc(KNOWN_CHARS[short(ch.uuid)] || '')}</span>
            <span class="tiny" style="color:var(--text-3)">${propsText(ch.props)}</span>
            <span class="chip ${ch.notifyOk ? 'chip-accent' : ''}">
              ${ch.notifyOk ? '接收中' : (ch.error ? '失败' : '—')}
            </span>
          </div>
          ${ch.error ? `<div class="tiny" style="color:var(--bad)">${U.esc(ch.error)}</div>` : ''}
        `).join('') : '<div class="tiny dim">（无法读取特征值）</div>'}
      </div>`).join('');
  }

  function renderLive() {
    const box = document.getElementById('bleLive');
    if (!box) return;
    if (!state.events.length) {
      box.innerHTML = `<p class="tiny dim">还没有收到数据。订阅成功后，操作设备（踩踏/开始骑行）才会产生通知。</p>`;
      return;
    }
    const recent = state.events.slice(-10).reverse();
    box.innerHTML = `
      <div class="tiny dim" style="margin-bottom:6px">实时帧（共 ${state.events.length} 条）</div>
      ${recent.map(e => `
        <div class="result-row" style="justify-content:space-between">
          <span class="mono tiny dim">${short(e.uuid).toUpperCase()}</span>
          <span class="mono tiny">${U.esc(e.hex)}</span>
          <span class="tiny" style="color:var(--accent-2)">${e.decoded ? U.esc(JSON.stringify(e.decoded)) : '未知格式'}</span>
        </div>`).join('')}`;
  }

  /* ------------------------------------------------------------ 订阅 */

  async function subscribeAll() {
    if (!state.server || !state.services.length) {
      log('还没连接或没有服务，无法订阅', 'err');
      return;
    }
    const btn = document.getElementById('bleSubscribe');
    if (btn) { btn.disabled = true; btn.textContent = '订阅中…'; }
    let attempted = 0;
    let ok = 0;

    for (const svc of state.services) {
      for (const ch of svc.chars) {
        if (!ch.props.notify && !ch.props.indicate) continue;
        attempted++;
        try {
          await ch.obj.startNotifications();
          ch.notifyOk = true;
          ch.error = null;
          ok++;
          ch.obj.addEventListener('characteristicvaluechanged', (ev) => onFrame(ch.uuid, ev.target.value));
          log(`订阅 ${short(ch.uuid).toUpperCase()} 成功`, 'ok');
        } catch (err) {
          ch.notifyOk = false;
          ch.error = `${err.name}: ${err.message}`;
          log(`订阅 ${short(ch.uuid).toUpperCase()} 失败：${ch.error}`, 'err');
        }
      }
    }

    state.active = ok;
    if (btn) { btn.disabled = false; btn.textContent = '开始接收'; }

    // 针对 2A05 的特殊说明：它只是"服务变更"通知，本来就没有数据
    const onlyServiceChanged = attempted > 0 && ok === 0 &&
      state.services.every(s => s.chars.every(c => !c.notifyOk)) &&
      state.services.some(s => s.chars.some(c => short(c.uuid) === '2a05'));
    if (onlyServiceChanged) {
      log('说明：2A05 是「服务变更」通知，就算订阅成功也不会产生运动数据。'
        + '它失败通常是设备不接受未配对的 CCCD 写入，与本项目无关。', 'warn');
    }

    if (attempted === 0) {
      log('设备没有暴露任何可通知的特征值。这个设备的协议可能是私有的，'
        + '或者数据只在厂商 App 里通过加密通道传输。', 'err');
      UI.toast('没有可订阅的通知特征值', 'warn');
    } else if (ok === 0) {
      log(`尝试了 ${attempted} 个特征值，全部订阅失败。`, 'err');
      UI.toast('订阅全部失败，看右侧日志', 'err');
    } else {
      log(`订阅完成：${ok}/${attempted} 个特征值正在接收`, 'ok');
      UI.toast(`已订阅 ${ok} 个特征值，去操作设备`, 'ok');
      const stop = document.getElementById('bleStop');
      if (stop) stop.disabled = false;
    }
    render();
  }

  async function stopAll() {
    for (const svc of state.services) {
      for (const ch of svc.chars) {
        if (!ch.notifyOk) continue;
        try {
          await ch.obj.stopNotifications();
          ch.notifyOk = false;
          log(`停止 ${short(ch.uuid).toUpperCase()}`, 'info');
        } catch (e) { /* 忽略 */ }
      }
    }
    state.active = 0;
    renderServices();
    log('已停止接收', 'info');
  }

  function onFrame(uuid, view) {
    const bytes = new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
    const hex = Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
    const decoded = decodeLocal(uuid, bytes);
    state.events.push({ uuid, hex, decoded, at: Date.now() });
    if (state.events.length > 3000) state.events.shift();
    renderLive();
    const save = document.getElementById('bleSave');
    if (save) save.disabled = false;
  }

  /** 与后端 pace/ble.py 同构的最小解析器（前端直接展示用）。 */
  function decodeLocal(uuid, bytes) {
    const s = short(uuid);
    try {
      if (s === '2a37' && bytes.length >= 2) {
        return (bytes[0] & 1)
          ? { heart_rate: bytes[1] | (bytes[2] << 8) }
          : { heart_rate: bytes[1] };
      }
      if (s === '2a5b' && bytes.length >= 1) {
        const flags = bytes[0];
        let idx = 1;
        const out = {};
        if (flags & 1 && bytes.length >= idx + 6) {
          out.wheel_revs = bytes[idx] | (bytes[idx + 1] << 8) | (bytes[idx + 2] << 16) | (bytes[idx + 3] << 24);
          out.wheel_evt_s = ((bytes[idx + 4] | (bytes[idx + 5] << 8)) / 1024).toFixed(2);
          idx += 6;
        }
        if (flags & 2 && bytes.length >= idx + 4) {
          out.crank_revs = bytes[idx] | (bytes[idx + 1] << 8);
          out.crank_evt_s = ((bytes[idx + 2] | (bytes[idx + 3] << 8)) / 1024).toFixed(2);
        }
        return Object.keys(out).length ? out : null;
      }
      if (s === '2a63' && bytes.length >= 4) {
        return { power_w: bytes[2] | (bytes[3] << 8) };
      }
      if (s === '2ad2' && bytes.length >= 4) {
        const flags = bytes[0] | (bytes[1] << 8);
        let idx = 2;
        const out = {};
        if (flags & 1) { out.speed_kmh = ((bytes[idx] | (bytes[idx + 1] << 8)) / 100).toFixed(2); idx += 2; }
        if (flags & 4) { out.cadence_rpm = (bytes[idx] | (bytes[idx + 1] << 8)) / 2; idx += 2; }
        return Object.keys(out).length ? out : null;
      }
    } catch (e) { return null; }
    return null;
  }

  async function saveCapture() {
    if (!state.events.length) return;
    const services = state.services.map(s => ({
      uuid: s.uuid,
      chars: s.chars.map(c => ({ uuid: c.uuid, props: c.props, notifyOk: c.notifyOk })),
    }));
    try {
      await api.saveBleCapture({
        device_name: state.device && state.device.name,
        device_id: state.device && state.device.id,
        services,
        events: state.events.slice(-1000),
      });
      UI.toast('抓包已保存', 'ok');
      log('抓包已保存到 data/ble-captures.json', 'ok');
    } catch (err) {
      log(`保存失败：${err.message}`, 'err');
      UI.toast('保存失败：' + U.esc(err.message), 'err');
    }
  }

  async function load(root) {
    host = root;
    state.supported = supported();
    state.logs = [];
    render();
    log(state.supported ? '浏览器支持 Web Bluetooth' : '浏览器不支持 Web Bluetooth',
        state.supported ? 'ok' : 'err');
    log('提示：先在码表上开机并解锁，手机上的厂商 App 建议先断开，避免独占连接', 'info');
  }

  window.PageBle = { load };
})();
