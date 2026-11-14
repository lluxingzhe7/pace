/**
 * 无头浏览器探针：用 Edge + CDP 检查真实 DOM / 交互 / 控制台错误，并可截图。
 *
 * 用法（先起 Edge，再跑探针）：
 *   msedge.exe --headless=new --remote-debugging-port=9333 --user-data-dir=.edge-profile about:blank
 *   node tools/ui_probe.mjs --port 9333 --url http://127.0.0.1:8765/ --steps steps.json --out .ui
 *
 * steps.json 是数组，每一项：
 *   {"eval": "<js 表达式>"}   在页面里执行，结果收集进报告
 *   {"wait": 800}             等待毫秒
 *   {"shot": "name"}          截图到 <out>/<name>.png
 *   {"navigate": "url"}       跳转并等 load
 *
 * 结束时打印控制台错误、异常和每步 eval 结果，任何异常退出码为 1。
 */
import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

const args = {};
for (let i = 2; i < process.argv.length; i += 2) {
  const k = process.argv[i].replace(/^--/, '');
  args[k] = process.argv[i + 1];
}
const port = Number(args.port || 9333);
const url = args.url || 'http://127.0.0.1:8765/';
const outDir = args.out || '.ui';
const stepsFile = args.steps;

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function readUrl(p) {
  return `http://127.0.0.1:${port}${p}`;
}

async function targets() {
  const res = await fetch(readUrl('/json/list'));
  return res.json();
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.handlers = new Map();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
      } else if (msg.method) {
        (this.handlers.get(msg.method) || []).forEach(fn => fn(msg.params));
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, 30000);
    });
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
}

const errors = [];
const logs = [];

function fmtRemote(arg) {
  if (arg.value !== undefined) return String(arg.value);
  if (arg.description) return arg.description;
  return arg.type;
}

async function main() {
  const list = await targets();
  let page = list.find(t => t.type === 'page');
  if (!page) throw new Error('没有可用的页面目标');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', () => rej(new Error('无法连接 CDP')), { once: true });
  });
  const cdp = new CDP(ws);
  cdp.on('Runtime.exceptionThrown', (p) => {
    errors.push(p.exceptionDetails.exception?.description || p.exceptionDetails.text);
  });
  cdp.on('Runtime.consoleAPICalled', (p) => {
    const text = (p.args || []).map(fmtRemote).join(' ');
    logs.push(`[${p.type}] ${text}`);
    if (p.type === 'error') errors.push(text);
  });
  cdp.on('Log.entryAdded', (p) => {
    if (p.entry.level === 'error') errors.push(`${p.entry.source}: ${p.entry.text}`);
  });
  await cdp.send('Runtime.enable');
  await cdp.send('Log.enable');
  await cdp.send('Page.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: Number(args.width || 1440), height: Number(args.height || 900),
    deviceScaleFactor: 1, mobile: false,
  });

  const loaded = new Promise((res) => cdp.on('Page.loadEventFired', res));
  await cdp.send('Page.navigate', { url });
  await Promise.race([loaded, sleep(8000)]);
  await sleep(600);

  const report = { url, steps: [], errors, logs };
  let steps = [];
  if (stepsFile) {
    steps = JSON.parse(await (await import('node:fs/promises')).readFile(stepsFile, 'utf8'));
  }
  mkdirSync(outDir, { recursive: true });
  for (const step of steps) {
    if (step.eval) {
      const r = await cdp.send('Runtime.evaluate', {
        expression: step.eval, awaitPromise: true, returnByValue: true,
      });
      if (r.exceptionDetails) {
        errors.push(`eval 失败: ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}`);
        report.steps.push({ eval: step.eval, error: true });
      } else {
        report.steps.push({ eval: step.eval, value: r.result.value });
      }
    } else if (step.wait) {
      await sleep(step.wait);
    } else if (step.navigate) {
      const p = new Promise((res) => cdp.on('Page.loadEventFired', res));
      await cdp.send('Page.navigate', { url: step.navigate });
      await Promise.race([p, sleep(8000)]);
      await sleep(500);
    } else if (step.viewport) {
      const [vw, vh] = step.viewport;
      await cdp.send('Emulation.setDeviceMetricsOverride', {
        width: vw, height: vh, deviceScaleFactor: 2, mobile: true,
      });
      await sleep(600);
      report.steps.push({ viewport: [vw, vh] });
    } else if (step.shot) {
      const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
      const file = join(outDir, `${step.shot}.png`);
      writeFileSync(file, Buffer.from(shot.data, 'base64'));
      report.steps.push({ shot: file });
    } else if (step.move) {
      const [mx, my] = step.move;
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: mx, y: my, button: 'none' });
      await sleep(60);
      report.steps.push({ move: [mx, my] });
    } else if (step.click) {
      // 真实鼠标点击：地图这类靠 pointer 事件的地方必须走 CDP，不能 dispatchEvent
      const [x, y] = step.click;
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none' });
      await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
      await sleep(40);
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
      await sleep(120);
      report.steps.push({ click: [x, y] });
    } else if (step.dragEval) {
      // 起点坐标由页面算出（例如锚点在画布上的位置），再用真实鼠标拖动
      const [expr, dx, dy] = step.dragEval;
      const r = await cdp.send('Runtime.evaluate', {
        expression: expr, awaitPromise: true, returnByValue: true,
      });
      const start = r.result.value || [0, 0];
      const [x1, y1] = start;
      const x2 = x1 + (dx || 0);
      const y2 = y1 + (dy || 0);
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x1, y: y1, button: 'none' });
      await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: x1, y: y1, button: 'left', clickCount: 1 });
      for (let i = 1; i <= 8; i++) {
        await cdp.send('Input.dispatchMouseEvent', {
          type: 'mouseMoved', button: 'left',
          x: Math.round(x1 + (x2 - x1) * i / 8), y: Math.round(y1 + (y2 - y1) * i / 8),
        });
        await sleep(35);
      }
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: x2, y: y2, button: 'left', clickCount: 1 });
      await sleep(150);
      report.steps.push({ dragEval: [x1, y1, x2, y2] });
    } else if (step.drag) {
      const [x1, y1, x2, y2] = step.drag;
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x1, y: y1, button: 'none' });
      await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: x1, y: y1, button: 'left', clickCount: 1 });
      for (let i = 1; i <= 6; i++) {
        await cdp.send('Input.dispatchMouseEvent', {
          type: 'mouseMoved', button: 'left',
          x: Math.round(x1 + (x2 - x1) * i / 6), y: Math.round(y1 + (y2 - y1) * i / 6),
        });
        await sleep(40);
      }
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: x2, y: y2, button: 'left', clickCount: 1 });
      await sleep(150);
      report.steps.push({ drag: [x1, y1, x2, y2] });
    }
  }
  ws.close();
  console.log(JSON.stringify(report, null, 2));
  if (errors.length) process.exitCode = 1;
  // 让浏览器自己退出，避免留下后台进程
  try { await fetch(readUrl('/json/close/' + page.id)); } catch { /* 忽略 */ }
}

main().catch((err) => {
  console.error('探针失败:', err.message);
  process.exit(2);
});
