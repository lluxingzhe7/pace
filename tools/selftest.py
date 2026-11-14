"""端到端自检：解析 -> 分析 -> 入库 -> API。

用法: python tools/selftest.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import analysis, analytics, parsers
from pace.store import open_store
from pace.summary_build import build_from_summary

FAILED: list[str] = []
PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def approx(a, b, tol) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def test_parsers() -> dict:
    print("\n[1] 解析器")
    base = os.path.join(ROOT, "sample-data")
    cases = {
        "run_trail.gpx": ("gpx", "running", 8_400),
        "run_park.gpx": ("gpx", "running", 5_200),
        "ride_loop.gpx": ("gpx", "cycling", 32_000),
        "walk_evening.gpx": ("gpx", "walking", 3_400),
        "trainer_ride.tcx": ("tcx", "cycling", 20_000),
        "road_ride.fit": ("fit", "road_cycling", 45_000),
    }
    parsed = {}
    for name, (fmt, _sport, expected_dist) in cases.items():
        path = os.path.join(base, name)
        with open(path, "rb") as fh:
            data = fh.read()
        t0 = time.time()
        rec = parsers.detect_and_parse(name, data)
        elapsed = time.time() - t0
        parsed[name] = rec
        check(f"{name} 格式识别", rec["source_format"] == fmt, f"得到 {rec['source_format']}")
        check(f"{name} 有点序列", isinstance(rec.get("points"), list) and len(rec["points"]) > 10,
              f"{len(rec.get('points') or [])} 点")
        check(f"{name} 解析耗时 < 3s", elapsed < 3.0, f"{elapsed:.2f}s")
        # 距离粗校验（合成路线 ~ 目标距离）
        if rec.get("points"):
            pts = rec["points"]
            d = analysis.analyze(pts, rec["sport"])["summary"]["distance_m"]
            check(f"{name} 距离量级", abs(d - expected_dist) / expected_dist < 0.25,
                  f"算出 {d:.0f}m，期望约 {expected_dist}m")

    # FIT 专项
    fit = parsed["road_ride.fit"]
    check("FIT 运动类型", fit["sport"] == "road_cycling", fit["sport"])
    check("FIT 有海拔", any(p.get("alt") is not None for p in fit["points"]))
    check("FIT 有心率", any(p.get("hr") is not None for p in fit["points"]))
    check("FIT 有功率", any(p.get("power") is not None for p in fit["points"]))
    check("FIT 时间戳合理", 1_700_000_000 < fit["start_time"] < 1_800_000_000, str(fit["start_time"]))

    # CSV
    csv_path = os.path.join(base, "activities.csv")
    with open(csv_path, "rb") as fh:
        rows = parsers.detect_and_parse("activities.csv", fh.read())
    check("CSV 多行", isinstance(rows, list) and len(rows) == 12, f"{len(rows) if isinstance(rows, list) else '?'} 行")
    check("CSV 运动类型归一", rows[0]["sport"] == "running", rows[0]["sport"])
    check("CSV 距离为米", rows[0]["distance_m"] > 1000, str(rows[0]["distance_m"]))
    return parsed


def test_analysis(parsed: dict) -> None:
    print("\n[2] 分析引擎")
    rec = parsed["run_trail.gpx"]
    result = analysis.analyze(rec["points"], "running", {"max": 190, "rest": 55})
    s = result["summary"]
    check("有距离", s["distance_m"] > 5000, str(s["distance_m"]))
    check("移动时间 <= 总时长", s["moving_seconds"] <= s["elapsed_seconds"] + 1,
          f"{s['moving_seconds']} vs {s['elapsed_seconds']}")
    check("爬升 > 0", s["elevation_gain_m"] > 10, str(s["elevation_gain_m"]))
    check("心率均值为正", s["avg_hr"] and 100 < s["avg_hr"] < 200, str(s["avg_hr"]))
    check("配速合理", s["pace_seconds_per_km"] and 200 < s["pace_seconds_per_km"] < 900,
          str(s["pace_seconds_per_km"]))
    check("训练负荷 > 0", s["training_load"] > 0, str(s["training_load"]))
    zones = s["hr_zones"]
    check("心率区间合计约等于移动时间",
          s["hr_zones"] and abs(sum(zones.values()) - s["moving_seconds"]) < s["moving_seconds"] * 0.1,
          f"{sum(zones.values()):.0f} vs {s['moving_seconds']:.0f}")

    st = result["streams"]
    check("流长度一致", len(st["t"]) == len(st["d"]) == len(st["alt"]) == len(st["hr"]) == len(st["speed"]),
          f"{[len(st[k]) for k in ('t','d','alt','hr','speed')]}")
    check("距离流单调", all(st["d"][i] <= st["d"][i + 1] + 0.01 for i in range(len(st["d"]) - 1)))
    check("速度非负", all(v >= 0 for v in st["speed"] if v is not None))
    check("路线已抽稀", 2 <= len(result["route"]) <= 900, str(len(result["route"])))

    best = s["best_efforts"]
    check("有最佳成绩", len(best) >= 1, str(best[:2]))
    if best:
        labels = {b["label"]: b for b in best}
        check("5km 最佳成绩存在", "5 km" in labels or s["distance_m"] < 5000)
        if "5 km" in labels:
            check("5km 用时 < 总时长", labels["5 km"]["seconds"] <= s["elapsed_seconds"] + 1)

    # 骑行
    ride = analysis.analyze(parsed["road_ride.fit"]["points"], "road_cycling")
    check("骑行均速合理", 10 < ride["summary"]["avg_speed_kmh"] < 70, str(ride["summary"]["avg_speed_kmh"]))
    check("骑行无跑步配速", ride["summary"]["pace_seconds_per_km"] is None)
    check("骑行 NP 已计算", ride["summary"]["np_power"] and ride["summary"]["np_power"] > 50,
          str(ride["summary"]["np_power"]))

    # 无 GPS 的汇总活动
    manual = build_from_summary({
        "sport": "pool_swimming", "start_time": 1_760_000_000,
        "distance_m": 1500, "moving_seconds": 1900, "elevation_m": 0,
        "avg_hr": 145, "max_hr": 168,
    })
    ms = manual["summary"]
    check("泳池配速 秒/100m", ms["pace_seconds_per_km"] and 100 < ms["pace_seconds_per_km"] < 200,
          str(ms["pace_seconds_per_km"]))
    check("汇总活动无流", manual["streams"] == {})


def test_store_and_api() -> None:
    print("\n[3] 存储与 API")
    # 不用系统临时目录：沙箱下只有工作区可写
    tmp = os.path.join(ROOT, ".selftest-tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    db = os.path.join(tmp, "test.db")
    store = open_store(db)
    base = os.path.join(ROOT, "sample-data")
    files = ["run_trail.gpx", "run_park.gpx", "trainer_ride.tcx", "road_ride.fit",
             "walk_evening.gpx", "activities.csv"]
    imported = 0
    for name in files:
        with open(os.path.join(base, name), "rb") as fh:
            data = fh.read()
        parsed = parsers.detect_and_parse(name, data)
        records = parsed if isinstance(parsed, list) else [parsed]
        import hashlib
        for rec in records:
            if "points" in rec:
                built = analysis.analyze(rec["points"], rec["sport"])
                aid, created = store.upsert_activity({
                    "source": "import", "file_hash": hashlib.sha256(data).hexdigest()
                    if len(records) == 1 else None,
                    "filename": name, "sport": rec["sport"],
                    "title": rec.get("title"), "summary": built["summary"],
                    "streams": built["streams"], "route": built["route"],
                })
            else:
                built = build_from_summary(rec)
                aid, created = store.upsert_activity({
                    "source": "csv", "filename": name, "sport": rec["sport"],
                    "title": rec.get("title"), "summary": built["summary"],
                    "external_id": f"csv:{rec['start_time']}:{rec['sport']}",
                })
            imported += 1 if created else 0

    check("导入活动数", store.count() == 17, f"{store.count()} 条")
    again = store.list_activities(limit=5)
    check("列表分页", len(again["items"]) == 5 and again["total"] >= 17, str(again["total"]))
    first = store.get_activity(again["items"][0]["id"], full=True)
    check("详情含流", bool(first["streams"].get("d")), "无流")

    # 去重
    with open(os.path.join(base, "run_trail.gpx"), "rb") as fh:
        data = fh.read()
    parsed = parsers.detect_and_parse("run_trail.gpx", data)
    built = analysis.analyze(parsed["points"], parsed["sport"])
    import hashlib
    _aid, created = store.upsert_activity({
        "source": "import", "file_hash": hashlib.sha256(data).hexdigest(),
        "filename": "run_trail.gpx", "sport": parsed["sport"], "title": "dup",
        "summary": built["summary"], "streams": built["streams"], "route": built["route"],
    })
    check("重复文件被识别", created is False)

    dash = analytics.dashboard(store, days=28, tz_offset=8)
    check("仪表盘有周桶", len(dash["weekly_buckets"]) == 12)
    check("仪表盘有热力图", len(dash["heatmap"]) == 182)
    check("有个人最好成绩", len(dash["personal_records"]) >= 1, str(len(dash["personal_records"])))
    check("体能曲线", len(dash["load_series"]) == 120 and dash["fitness"]["ctl"] >= 0)
    check("运动分布", len(dash["sport_breakdown"]) >= 3, str([s["family"] for s in dash["sport_breakdown"]]))

    # 分段：从第一条跑步活动建一个 1km 分段
    run = next(a for a in store.list_activities(limit=50)["items"] if a["sport"] == "running")
    full = store.get_activity(run["id"], full=True)
    seg_data = analytics.build_segment_from_activity(full, 0, max(2, len(full["route"]) // 2), "测试分段")
    sid = store.create_segment(seg_data)
    matched = analytics.rebuild_all_efforts(store)
    seg = store.get_segment(sid)
    check("分段创建", seg is not None and seg["distance_m"] > 100, str(seg and seg["distance_m"]))
    check("分段至少匹配一次", len(seg["efforts"]) >= 1, f"匹配 {matched} 次")

    store.close()

    # 启动真实服务验证 HTTP
    from pace.server import build_server
    httpd, app = build_server(db, os.path.join(ROOT, "web"), 0, auth_required=False)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    def get(path):
        with urllib.request.urlopen(base_url + path, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        health = get("/api/health")
        check("API 健康检查", health["ok"] is True and health["activities"] == 17, json.dumps(health))
        meta = get("/api/meta")
        check("API meta", "settings" in meta and meta["settings"]["hr_max"] == 190)
        d = get("/api/dashboard?days=28&tz=8")
        check("API dashboard", "period" in d and d["period_days"] == 28)
        acts = get("/api/activities?limit=3")
        check("API 活动列表", len(acts["items"]) == 3)
        detail = get(f"/api/activities/{acts['items'][0]['id']}")
        check("API 活动详情", "streams" in detail)
        prs = get("/api/prs")
        check("API PR", "records" in prs)
        segs = get("/api/segments")
        check("API 分段列表", len(segs["segments"]) >= 1)
        seg_detail = get(f"/api/segments/{sid}")
        check("API 分段详情", len(seg_detail["efforts"]) >= 1)

        # 上传接口（原始字节 + X-Filename）
        with open(os.path.join(base, "run_park.gpx"), "rb") as fh:
            payload = fh.read()
        req = urllib.request.Request(
            base_url + "/api/import", data=payload, method="POST",
            headers={"Content-Type": "application/gpx+xml", "X-Filename": "run_park.gpx"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            up = json.loads(resp.read().decode("utf-8"))
        check("API 上传", up["results"][0]["status"] == "ok", json.dumps(up)[:200])

        # 错误路径
        try:
            get("/api/activities/999999")
            check("API 404", False, "没有报错")
        except urllib.error.HTTPError as exc:
            check("API 404", exc.code == 404, str(exc.code))
    finally:
        httpd.shutdown()
        app.store.close()


def test_edge_cases() -> None:
    print("\n[4] 边界情况")
    # 单点 / 空
    for label, pts in (("空", []), ("单点", [{"t": 0, "lat": 30.0, "lon": 120.0}])):
        try:
            analysis.analyze(pts, "running")
            check(f"{label}应报错", False)
        except ValueError:
            check(f"{label}应报错", True)

    # 无坐标但有时间与心率
    pts = [{"t": i, "hr": 120 + i, "speed": 3.0} for i in range(600)]
    res = analysis.analyze(pts, "running")
    check("无 GPS 也能分析", res["summary"]["moving_seconds"] > 0 and not res["summary"]["has_gps"])

    # polyline 往返
    from pace import geo
    original = [(30.2465, 120.1520), (30.2470, 120.1530), (30.2481, 120.1544)]
    decoded = geo.decode_polyline(geo.encode_polyline(original))
    check("polyline 编解码", len(decoded) == 3 and approx(decoded[0][0], original[0][0], 1e-5),
          str(decoded[0]))

    # 心率区间边界
    from pace.analysis import _hr_zones
    z = _hr_zones([(0, 100), (10, 190)], {"max": 190, "rest": 55})
    check("心率区间有值", sum(z.values()) > 0)

    # 空 CSV
    try:
        parsers.parse_csv("")
        check("空 CSV 应报错", False)
    except parsers.ParseError:
        check("空 CSV 应报错", True)

    # 坏 FIT
    try:
        parsers.detect_and_parse("bad.fit", b"\x0e" + b"\x00" * 8 + b".BAD" + b"\x00" * 8)
        check("坏 FIT 应报错", False)
    except Exception:
        check("坏 FIT 应报错", True)


def main() -> int:
    print("=" * 60)
    print("PACE 自检")
    print("=" * 60)
    parsed = test_parsers()
    test_analysis(parsed)
    test_store_and_api()
    test_edge_cases()
    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
