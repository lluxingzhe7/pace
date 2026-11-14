"""设备接入层测试：文件夹同步、USB 探测、Intervals 拉取、教练 API。

不联网：Intervals 用本地假服务器模拟，USB 用假目录模拟。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import analysis, parsers
from pace.devices import SyncEngine
from pace.devices.sources import FolderSource, SyncState, UsbSource
from pace.importer import Importer
from pace.store import open_store

PASSED = 0
FAILED: list[str] = []
TMP = os.path.join(ROOT, ".device-test")


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def gpx_file(shift: float = 0.0) -> bytes:
    """可微调内容的 GPX，避免两次生成完全相同的文件哈希。"""
    import re
    text = open(os.path.join(ROOT, "sample-data", "run_park.gpx"), encoding="utf-8").read()
    if shift:
        match = re.search(r'lat="([\d.]+)"', text)
        if match:
            new_lat = float(match.group(1)) + shift
            text = text[:match.start(1)] + f"{new_lat:.7f}" + text[match.end(1):]
    return text.encode("utf-8")


def fit_file() -> bytes:
    return open(os.path.join(ROOT, "sample-data", "road_ride.fit"), "rb").read()


# ---------------------------------------------------------------- 文件夹

def test_folder():
    print("\n[1] 文件夹同步")
    watch = os.path.join(TMP, "huawei")
    os.makedirs(os.path.join(watch, "motion"), exist_ok=True)
    open(os.path.join(watch, "motion", "run1.gpx"), "wb").write(gpx_file())
    open(os.path.join(watch, "motion", "ride1.fit"), "wb").write(fit_file())
    # Strava 批量导出常见 .fit.gz
    import gzip
    open(os.path.join(watch, "motion", "ride2.fit.gz"), "wb").write(gzip.compress(fit_file()))
    open(os.path.join(watch, "readme.txt"), "w").write("ignore me")

    from pace.devices.sources import is_supported
    check("识别 .fit", is_supported("a.fit"))
    check("识别 .fit.gz", is_supported("a.fit.gz"))
    check("忽略 .txt", not is_supported("a.txt"))

    db = os.path.join(TMP, "a.db")
    store = open_store(db)
    engine = SyncEngine(store, os.path.join(TMP, "a-state.json"),
                        Importer(store, lambda: {}).import_file)

    configs = engine.configs()
    for cfg in configs:
        if cfg["id"] == "huawei_export":
            cfg["enabled"] = True
            cfg["config"]["path"] = watch
        else:
            cfg["enabled"] = False
    engine.set_configs(configs)

    status = engine.status()
    hw = next(s for s in status["sources"] if s["id"] == "huawei_export")
    check("识别目录", hw["available"] is True)
    check("统计活动文件", hw["files_pending"] == 3, str(hw["files_pending"]))

    r1 = engine.sync()
    # run1.gpx 新建；ride1.fit 新建；ride2.fit.gz 是同一次骑行 → 合并
    check("首次同步新建 2 条", r1["imported"] == 2, str(r1["imported"]))
    check(".gz 与 .fit 合并为一条", r1.get("merged") == 1, str(r1.get("merged")))
    check("数据库有 2 条", store.count() == 2, str(store.count()))

    r2 = engine.sync()
    check("二次同步不重复导入", r2["imported"] == 0, str(r2["imported"]))
    check("数据库仍为 2 条", store.count() == 2, str(store.count()))

    # 新增一个内容不同的文件
    open(os.path.join(watch, "motion", "run2.gpx"), "wb").write(gpx_file(shift=0.002))
    r3 = engine.sync()
    check("新文件被增量导入", r3["imported"] == 1, str(r3["imported"]))
    check("数据库变 3 条", store.count() == 3, str(store.count()))
    store.close()


def test_usb():
    print("\n[2] USB 设备探测")
    fake = os.path.join(TMP, "usb", "iGPSPORT")
    os.makedirs(os.path.join(fake, "Activities"), exist_ok=True)
    open(os.path.join(fake, "Activities", "2026-11-01.fit"), "wb").write(fit_file())
    open(os.path.join(fake, "Activities", "2026-11-02.fit"), "wb").write(fit_file())

    dev_root = os.path.join(TMP, "usb")
    src = UsbSource({"path": dev_root}, SyncState(os.path.join(TMP, "usb-state.json")))
    check("识别 iGPSPORT 目录", src._looks_like_device(fake) is True)
    plain = os.path.join(TMP, "usb", "plain")
    os.makedirs(plain, exist_ok=True)
    check("普通目录不误判", src._looks_like_device(plain) is False)

    items = src.scan()
    check("扫描到活动文件", len(items) >= 1, str(len(items)))
    if items:
        check("文件名保留", items[0].name.endswith(".fit"), items[0].name)
        check("内容完整", len(items[0].data) > 1000, str(len(items[0].data)))

    st = src.status()
    check("status 可序列化", isinstance(st, dict) and "hint" in st)


# ---------------------------------------------------------------- Intervals

class FakeIntervals(BaseHTTPRequestHandler):
    activities = [
        {"id": "i111", "type": "Ride", "start_date_local": "2026-11-10T07:00:00",
         "file_type": "fit"},
    ]
    forbidden = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        if "/activities" in self.path:
            body = json.dumps(self.activities).encode()
            self._send(200, body, "application/json")
        elif self.path.endswith("/file"):
            if self.forbidden:
                self._send(403, b'{"error":"strava data"}', "application/json")
            else:
                self._send(200, fit_file(), "application/octet-stream")
        else:
            self._send(404, b"{}", "application/json")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_intervals():
    print("\n[3] Intervals.icu 拉取")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeIntervals)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    from pace.devices.sources import IntervalsSource
    src = IntervalsSource({"athlete_id": "i123", "api_key": "k",
                           "days": 30}, SyncState(os.path.join(TMP, "iv-state.json")))
    src.API = f"http://127.0.0.1:{port}/api/v1"
    try:
        test = src.test()
        check("连接测试通过", test.get("ok") is True, json.dumps(test))
        items = src.scan()
        check("拉到活动文件", len(items) == 1, str(len(items)))
        if items:
            check("文件名含 FIT", items[0].name.endswith(".fit"), items[0].name)
            check("内容是 FIT", items[0].data[8:12] == b".FIT", str(items[0].data[:14]))
            src.mark_done(items[0])
            check("已标记不重复", len(src.scan()) == 0)
            check("状态已持久化", "i111" in src.state.data["intervals"])

        # Strava 来源的活动：文件 403，应被记录而不是静默丢弃
        FakeIntervals.forbidden = True
        src2 = IntervalsSource({"athlete_id": "0", "api_key": "k", "days": 30},
                               SyncState(os.path.join(TMP, "iv2-state.json")))
        src2.API = src.API
        check("403 时不抛错", src2.scan() == [])
        check("403 被记录", len(src2.last_errors) == 1, str(src2.last_errors))
        if src2.last_errors:
            err = src2.last_errors[0]
            check("错误含活动 ID", err["activity_id"] == "i111", str(err))
            check("说明是 Strava 限制", "Strava" in err["reason"], err["reason"])
        FakeIntervals.forbidden = False
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------- 服务器 API

def test_api():
    print("\n[4] 服务器 API")
    from pace.server import build_server
    db = os.path.join(TMP, "api.db")
    store = open_store(db)
    store.close()
    httpd, app = build_server(db, os.path.join(ROOT, "web"), 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=20) as r:
            return json.loads(r.read())

    def post(path, payload):
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def delete(path):
        req = urllib.request.Request(base + path, method="DELETE")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    try:
        status = get("/api/sources")
        check("GET /api/sources", "sources" in status and len(status["sources"]) >= 3,
              str(len(status.get("sources", []))))

        # 配好文件夹源并同步
        configs = status["sources"]
        for cfg in configs:
            cfg["enabled"] = cfg["id"] in ("huawei_export",)
            if cfg["id"] == "huawei_export":
                cfg["config"]["path"] = os.path.join(TMP, "huawei")
        saved = post("/api/sources", {"sources": configs})
        check("PUT /api/sources", "sources" in saved)

        r = post("/api/sources/sync", {"sources": ["huawei_export"]})
        check("POST sync 导入", r["imported"] >= 1, json.dumps(r)[:200])

        # 列表卡片要画缩略轨迹
        listed = get("/api/activities")
        item = listed["items"][0]
        check("列表带轨迹缩略", bool(item.get("route_preview")) and len(item["route_preview"]) >= 2,
              str(len(item.get("route_preview") or [])))
        check("列表带统计汇总", "stats" in listed and listed["stats"]["distance_m"] > 0,
              json.dumps(listed.get("stats"))[:120])

        # 删除后必须能重新导入（同步状态要一起清掉）
        before = listed["total"]
        delete(f"/api/activities/{item['id']}")
        check("删除生效", get("/api/activities")["total"] == before - 1,
              f"{before} -> {get('/api/activities')['total']}")
        again = post("/api/sources/sync", {"sources": ["huawei_export"]})
        check("删除后同一文件能重新导入", again["imported"] >= 1, json.dumps(again)[:200])
        check("重新导入后数量恢复", get("/api/activities")["total"] == before,
              str(get("/api/activities")["total"]))

        report = get("/api/coach?tz=8")
        check("GET /api/coach", "verdict" in report and "grade" in report)
        check("报告含风险/缺口", "risks" in report and "gaps" in report)
        check("报告可 JSON 序列化", isinstance(report["confidence"], dict))

        plan = get("/api/coach/plan?tz=8&goal=health&days=3")
        check("GET /api/coach/plan", "plan" in plan and len(plan["plan"]["sessions"]) == 7)
        check("计划有护栏", len(plan["plan"]["guardrails"]) >= 3)
    finally:
        httpd.shutdown()
        app.store.close()


def test_low_data_coach():
    print("\n[5] 数据少时的教练输出")
    from pace.coach import evaluate
    db = os.path.join(TMP, "empty.db")
    store = open_store(db)
    report = evaluate.evaluate(store, tz_offset=8)
    check("空库也能返回报告", report["verdict"]["tone"] == "insufficient",
          report["verdict"]["tone"])
    check("明确说分数上限", report["score"] <= 40, str(report["score"]))
    store.close()


def main() -> int:
    print("=" * 58)
    print("设备接入测试")
    print("=" * 58)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    test_folder()
    test_usb()
    test_intervals()
    test_api()
    test_low_data_coach()

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 58)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
