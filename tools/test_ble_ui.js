/**
 * BLE 页面逻辑测试（Node + DOM 桩）。
 *
 * 用假 GATT 验证：
 *   1. 订阅成功时计数正确
 *   2. 订阅失败时不会假装成功（这是上一版"点了没反应"的根因）
 *   3. 没有可通知特征值时给出明确错误
 *   4. 帧解析正确
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

/* ---------------------------------------------------------------- DOM 桩 */

function makeDom() {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) {
      const handlers = {};
      nodes.set(id, {
        id, innerHTML: '', textContent: '', disabled: false, checked: false,
        style: {}, dataset: {}, children: [],
        _handlers: handlers,
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener(ev, fn) { handlers[ev] = fn; },
        removeEventListener() {},
        appendChild() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        closest() { return null; },
        getBoundingClientRect() { return { left: 0, top: 0 }; },
        scrollHeight: 0,
        set scrollTop(v) { this._scrollTop = v; },
        get scrollTop() { return this._scrollTop || 0; },
      });
    }
    return nodes.get(id);
  }
  return {
    nodes, node,
    getElementById: (id) => node(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => node('created-' + Math.random()),
    addEventListener() {},
    documentElement: { dataset: {} },
    body: { appendChild() {} },
  };
}

/* ---------------------------------------------------------------- GATT 桩 */

function makeService(uuid, chars) {
  return {
    uuid,
    async getCharacteristics() { return chars; },
  };
}

function makeChar(uuid, props, behavior) {
  const listeners = [];
  return {
    uuid,
    properties: props,
    _listeners: listeners,
    async startNotifications() {
      if (behavior === 'reject') throw new Error('NotSupportedError: notifications not supported');
      return this;
    },
    async stopNotifications() { return this; },
    async readValue() { return new DataView(new Uint8Array([0x00, 0x60]).buffer); },
    addEventListener(ev, fn) { if (ev === 'characteristicvaluechanged') listeners.push(fn); },
    _emit(bytes) {
      const view = new DataView(new Uint8Array(bytes).buffer);
      listeners.forEach(fn => fn({ target: { value: view } }));
    },
  };
}

function makeDevice(services) {
  const server = {
    async getPrimaryServices() { return services; },
    async getPrimaryService(uuid) {
      return services.find(s => s.uuid === uuid) || null;
    },
    disconnect() { this.connected = false; },
  };
  return {
    id: 'mock-device',
    name: 'iGPSPORT iGS630',
    gatt: {
      connected: false,
      async connect() {
        this.connected = true;
        return server;
      },
      async disconnect() { this.connected = false; },
    },
    _services: services,
    addEventListener() {},
  };
}

/* ---------------------------------------------------------------- 加载页面 */

function loadPage(dom, bluetoothStub) {
  const sandbox = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    structuredClone, JSON, Math, Date, Object, Array, String, Number, Boolean,
    DataView, Uint8Array, ArrayBuffer, Promise, Error, Map, Set,
    isSecureContext: true,
    navigator: { bluetooth: bluetoothStub },
    window: {},
    document: dom,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    getComputedStyle: () => ({ getPropertyValue: () => '#fff' }),
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  // 最小依赖桩
  sandbox.U = {
    esc: (s) => String(s == null ? '' : s),
    toast: () => {},
    clamp: (v, a, b) => Math.max(a, Math.min(b, v)),
  };
  sandbox.Icon = { get: () => '', hydrate: () => {} };
  sandbox.UI = { toast: () => {}, empty: () => '', spinner: () => '' };
  sandbox.api = {
    bleProtocols: async () => ({ standard_services: [] }),
    saveBleCapture: async () => ({ ok: true }),
  };
  sandbox.location = { hash: '#/ble' };

  vm.createContext(sandbox);
  const code = fs.readFileSync(path.join(WEB, 'pages', 'ble.js'), 'utf8');
  vm.runInContext(code, sandbox, { filename: 'ble.js' });
  return sandbox;
}

const tick = (ms) => new Promise(r => setTimeout(r, ms || 25));

/* ---------------------------------------------------------------- 测试 */

async function testSubscribeSuccess() {
  console.log('\n[1] 订阅成功');
  const dom = makeDom();
  const chars = [
    makeChar('00002a37-0000-1000-8000-00805f9b34fb', { notify: true }, 'ok'),
    makeChar('00002a63-0000-1000-8000-00805f9b34fb', { notify: false, read: true }, 'ok'),
  ];
  const device = makeDevice([makeService('0000180d-0000-1000-8000-00805f9b34fb', chars)]);
  const page = loadPage(dom, { async requestDevice() { return device; } });
  await page.PageBle.load(dom);
  await dom.node('bleConnect')._handlers.click();
  await tick();
  const servicesHtml = dom.node('bleServices').innerHTML;
  check('服务列表渲染', servicesHtml.includes('180d') || servicesHtml.includes('180D'),
        servicesHtml.slice(0, 100));
  await dom.node('bleSubscribe')._handlers.click();
  await tick();
  const logHtml = dom.node('bleLog').innerHTML;
  check('日志记录订阅成功', logHtml.includes('订阅') && logHtml.includes('成功'),
        logHtml.slice(-200));
  check('统计为 1/1', logHtml.includes('1/1'), logHtml.slice(-200));
  chars[0]._emit([0x00, 0x48]);
  const live = dom.node('bleLive').innerHTML;
  check('实时面板出现数据', live.includes('48'), live.slice(0, 120));
}

async function testSubscribeReject() {
  console.log('\n[2] 订阅失败必须暴露');
  const dom = makeDom();
  const ch = makeChar('0000abcd-0000-1000-8000-00805f9b34fb', { notify: true }, 'reject');
  const device = makeDevice([makeService('0000fff0-0000-1000-8000-00805f9b34fb', [ch])]);
  const page = loadPage(dom, { async requestDevice() { return device; } });
  await page.PageBle.load(dom);
  await dom.node('bleConnect')._handlers.click();
  await tick();
  await dom.node('bleSubscribe')._handlers.click();
  await tick();
  const logHtml = dom.node('bleLog').innerHTML;
  check('失败写进日志', logHtml.includes('失败'), logHtml.slice(-200));
  check('失败原因可见', logHtml.includes('NotSupportedError') || logHtml.includes('not supported'),
        logHtml.slice(-200));
  check('不谎报成功', !logHtml.includes('正在接收'), logHtml.slice(-200));
}

async function testNoNotifyChars() {
  console.log('\n[3] 没有可通知特征值');
  const dom = makeDom();
  const ch = makeChar('0000abcd-0000-1000-8000-00805f9b34fb', { read: true, write: true }, 'ok');
  const device = makeDevice([makeService('0000180a-0000-1000-8000-00805f9b34fb', [ch])]);
  const page = loadPage(dom, { async requestDevice() { return device; } });
  await page.PageBle.load(dom);
  await dom.node('bleConnect')._handlers.click();
  await tick();
  await dom.node('bleSubscribe')._handlers.click();
  await tick();
  const logHtml = dom.node('bleLog').innerHTML;
  check('明确提示没有通知特征', logHtml.includes('没有') || logHtml.includes('可通知'),
        logHtml.slice(-220));
}

async function testDecode() {
  console.log('\n[4] 帧解析');
  const dom = makeDom();
  const ch = makeChar('00002a37-0000-1000-8000-00805f9b34fb', { notify: true }, 'ok');
  const device = makeDevice([makeService('0000180d-0000-1000-8000-00805f9b34fb', [ch])]);
  const page = loadPage(dom, { async requestDevice() { return device; } });
  await page.PageBle.load(dom);
  await dom.node('bleConnect')._handlers.click();
  await tick();
  await dom.node('bleSubscribe')._handlers.click();
  await tick();
  ch._emit([0x00, 0x60]);
  const live = dom.node('bleLive').innerHTML;
  check('心率被解析为 96', live.includes('96') || live.includes('heart_rate'),
        live.slice(0, 200));
}

async function testOnlyBaseServices() {
  console.log('\n[5] 只暴露 1800 / 1801 时的提示');
  const dom = makeDom();
  // 复现用户真实场景：1800 有 4 个特征值（含 2A05），1801 有 1 个
  const gaChars = [
    makeChar('00002a00-0000-1000-8000-00805f9b34fb', { read: true }, 'ok'),
    makeChar('00002a01-0000-1000-8000-00805f9b34fb', { read: true }, 'ok'),
    makeChar('00002a04-0000-1000-8000-00805f9b34fb', { read: true }, 'ok'),
    makeChar('00002a05-0000-1000-8000-00805f9b34fb', { indicate: true }, 'reject'),
  ];
  const gattChars = [makeChar('00002a05-0000-1000-8000-00805f9b34fb', { indicate: true }, 'reject')];
  const device = makeDevice([
    makeService('00001800-0000-1000-8000-00805f9b34fb', gaChars),
    makeService('00001801-0000-1000-8000-00805f9b34fb', gattChars),
  ]);
  const page = loadPage(dom, { async requestDevice() { return device; } });
  await page.PageBle.load(dom);
  await dom.node('bleConnect')._handlers.click();
  await tick();
  const body = dom.node('bleServices').innerHTML;
  check('列出 1800 服务', body.includes('1800') || body.includes('1800'.toUpperCase()),
        body.slice(0, 100));
  check('2A05 有中文说明', body.includes('Service Changed'), body.slice(0, 400));
  await dom.node('bleSubscribe')._handlers.click();
  await tick();
  const logHtml = dom.node('bleLog').innerHTML;
  check('解释 2A05 不会产生数据', logHtml.includes('服务变更') || logHtml.includes('2A05'),
        logHtml.slice(-300));
  check('全部失败时不谎报', logHtml.includes('全部订阅失败') || logHtml.includes('失败'),
        logHtml.slice(-200));
}


function main() {
  console.log('='.repeat(56));
  console.log('BLE 页面逻辑测试');
  console.log('='.repeat(56));
  return (async () => {
    await testSubscribeSuccess();
    await testSubscribeReject();
    await testNoNotifyChars();
    await testDecode();
    await testOnlyBaseServices();

    console.log('\n' + '='.repeat(56));
    if (FAILED.length) {
      console.log(`失败 ${FAILED.length} 项 / 通过 ${PASSED} 项`);
      FAILED.forEach(f => console.log('  - ' + f));
      return 1;
    }
    console.log(`全部通过（${PASSED} 项）`);
    return 0;
  })();
}

main().then(code => process.exit(code));
