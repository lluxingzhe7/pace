"""Strava 直连测试：假 OAuth 服务器 + 假 API，验证整条链路。

不联网。覆盖：授权换取、token 刷新、活动列表、逐点流、室内无 GPS、入库去重。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import strava_api
from pace.devices import SyncEngine
from pace.devices.sources import StravaSource, SyncState
from pace.importer import Importer
from pace.store import open_store

PASSED = 0
FAILED: list[str] = []
TMP = os.path.join(ROOT, ".strava-test")

START = 1_793_000_000
GPS_ID = 111
INDOOR_ID = 222


class FakeStrava(BaseHTTPRequestHandler):
    token_calls = 0
    refresh_calls = 0
    stream_calls = 0

    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        grant = (form.get("grant_type") or [""])[0]
        if grant == "authorization_code":
            FakeStrava.token_calls += 1
        elif grant == "refresh_token":
            FakeStrava.refresh_calls += 1
        self._send(200, {
            "access_token": f"access-{int(time.time())}",
            "refresh_token": "refresh-1",
            "expires_at": int(time.time()) + 3600,
            "athlete": {"id": 999, "firstname": "测试", "lastname": "骑手"},
        })

    def do_GET(self):
        if self.path.startswith("/api/v3/athlete/activities"):
            return self._send(200, [
                {
                    "id": GPS_ID, "name": "晨骑", "type": "Ride",
                    "sport_type": "Ride", "start_date": "2026-11-05T07:00:00Z",
                    "distance": 25000.0, "moving_time": 3600, "elapsed_time": 3700,
                    "total_elevation_gain": 120.0, "average_heartrate": 142.5,
                    "max_heartrate": 171.0, "average_watts": 180.0,
                },
                {
                    "id": INDOOR_ID, "name": "骑行台", "type": "VirtualRide",
                    "sport_type": "VirtualRide", "start_date": "2026-11-03T19:00:00Z",
                    "distance": 12000.0, "moving_time": 1800, "elapsed_time": 1800,
                    "total_elevation_gain": 0.0, "average_heartrate": 135.0,
                },
            ])
        if "/streams" in self.path:
            FakeStrava.stream_calls += 1
            aid = int(self.path.split("/activities/")[1].split("/")[0])
            n = 120
            base = {
                "time": {"data": list(range(n))},
                "distance": {"data": [i * (25000 / n if aid == GPS_ID else 12000 / n) for i in range(n)]},
                "heartrate": {"data": [130 + (i % 30) for i in range(n)]},
                "watts": {"data": [150 + (i % 40) for i in range(n)]},
                "altitude": {"data": [40 + (i % 10) for i in range(n)]},
                "velocity_smooth": {"data": [6.5 + (i % 5) * 0.2 for i in range(n)]},
            }
            if aid == GPS_ID:
                base["latlng"] = {"data": [[30.25 + i * 1e-5, 120.15 + i * 1e-5] for i in range(n)]}
            return self._send(200, base)
        if self.path.startswith("/api/v3/athlete"):
            return self._send(200, {"id": 999, "firstname": "测试", "lastname": "骑手"})
        self._send(404, {"error": "not found"})


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def main() -> int:
    print("=" * 58)
    print("Strava 直连测试")
    print("=" * 58)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeStrava)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    # 让客户端指向假服务器
    strava_api.TOKEN_URL = f"{base}/oauth/token"
    strava_api.API_BASE = f"{base}/api/v3"

    print("\n[1] OAuth 与 token")
    tokens = strava_api.exchange_code("cid", "secret", "code-1")
    check("换取 token", tokens["access_token"].startswith("access-"), str(tokens)[:80])
    check("记录 athlete", tokens["athlete_name"] == "测试 骑手", tokens.get("athlete_name"))
    url = strava_api.auth_url("cid", "http://localhost:8765/api/strava/callback")
    check("授权 URL 含 scope", "activity%3Aread_all" in url or "activity:read_all" in url, url[:120])
    check("授权 URL 含回调", "redirect_uri" in url)

    print("\n[2] 拉取与入库")
    store = open_store(os.path.join(TMP, "t.db"))
    saved = {}

    def persist(conf):
        saved.update(conf)

    src = StravaSource({
        "client_id": "cid", "client_secret": "secret", "days": 30,
        "tokens": dict(tokens),
    }, SyncState(os.path.join(TMP, "state.json")), persist=persist)

    check("状态已连接", src.status()["available"] is True)
    items = src.scan()
    check("拿到 2 条活动", len(items) == 2, str(len(items)))
    names = sorted(i.name for i in items)
    check("文件名带 ID", all(".json" in n for n in names), str(names))

    importer = Importer(store, lambda: {"max": 190, "rest": 55})
    imported = 0
    for item in items:
        outcome = importer.import_file(item.name, item.data)
        imported += len(outcome.get("imported") or [])
        if outcome.get("errors"):
            print("    导入错误:", outcome["errors"])
        if outcome.get("imported"):
            src.mark_done(item)      # SyncEngine 在真实流程里会做这一步
    check("两条都入库", imported == 2, str(imported))
    check("数据库 2 条", store.count() == 2, str(store.count()))

    rows = store.conn.execute(
        "SELECT id, external_id, distance_m, has_gps, point_count FROM activities ORDER BY id"
    ).fetchall()
    ids = {r["external_id"] for r in rows}
    check("外部 ID 正确", ids == {f"strava:{GPS_ID}", f"strava:{INDOOR_ID}"}, str(ids))
    gps = next((r for r in rows if r["has_gps"]), None)
    indoor = next((r for r in rows if not r["has_gps"]), None)
    check("GPS 活动有轨迹", gps is not None and gps["point_count"] > 50,
          str(gps and gps["point_count"]))
    check("室内活动也导入", indoor is not None, "无室内活动")
    if indoor:
        check("室内活动有距离", indoor["distance_m"] > 10000, str(indoor["distance_m"]))

    # 逐点数据是否完整
    act = store.get_activity(gps["id"] if gps else 1, full=True)
    st = act.get("streams") or {}
    check("有逐点心率", bool(st.get("hr")), str(len(st.get("hr") or [])))
    check("有逐点功率", bool(st.get("pwr")), str(len(st.get("pwr") or [])))
    check("有逐点距离", bool(st.get("d")), str(len(st.get("d") or [])))

    # 二次扫描应全部去重
    again = src.scan()
    check("二次扫描无新活动", len(again) == 0, str(len(again)))

    print("\n[3] token 刷新")
    expired = dict(tokens)
    expired["expires_at"] = int(time.time()) - 10
    src2 = StravaSource({"client_id": "cid", "client_secret": "secret",
                         "tokens": expired}, SyncState(os.path.join(TMP, "s2.json")),
                        persist=persist)
    before = FakeStrava.refresh_calls
    tok = src2.access_token()
    check("过期后自动刷新", FakeStrava.refresh_calls > before, str(FakeStrava.refresh_calls))
    check("刷新后可用", bool(tok), tok)
    check("刷新结果被持久化", bool(saved.get("tokens", {}).get("access_token")),
          str(list(saved.keys())))

    print("\n[4] 同步引擎集成")
    engine = SyncEngine(store, os.path.join(TMP, "engine-state.json"),
                        importer.import_file)
    configs = engine.configs()
    for cfg in configs:
        if cfg["id"] == "strava":
            cfg["enabled"] = True
            cfg["config"] = {"client_id": "cid", "client_secret": "secret",
                             "tokens": dict(tokens), "days": 30}
        else:
            cfg["enabled"] = False
    engine.set_configs(configs)
    # 清掉上一轮的入库记录，让同步重新拉一次
    store.conn.execute("DELETE FROM activities")
    store.conn.commit()
    r = engine.sync(source_ids=["strava"])
    check("引擎同步导入 2 条", r["imported"] == 2, json.dumps(r)[:200])

    store.close()
    httpd.shutdown()
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
