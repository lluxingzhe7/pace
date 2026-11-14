"""导入链路端到端测试：覆盖全部格式 + Strava 网页存档。

用法: python tools/test_imports.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import analysis, parsers, strava_web
from pace.store import open_store
from pace.summary_build import build_from_summary
from pace.importer import Importer
from pace.importer import strava_id_from_json as _strava_id_from_json

PASSED = 0
FAILED: list[str] = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def strava_html() -> str:
    start = int(time.time()) - 86400 * 3
    from datetime import datetime, timezone
    iso = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "props": {"pageProps": {
            "activity": {
                "id": 99887766, "name": "Strava 存档测试", "type": "Ride",
                "sport_type": "Ride", "start_date": iso,
                "distance": 21000.0, "moving_time": 3000, "elapsed_time": 3200,
                "total_elevation_gain": 160.0, "average_heartrate": 142,
                "max_heartrate": 171,
            },
            "streams": {
                "time": {"data": list(range(300))},
                "latlng": {"data": [[30.25 + i * 1e-5, 120.15 + i * 1e-5] for i in range(300)]},
                "heartrate": {"data": [130 + (i % 40) for i in range(300)]},
                "altitude": {"data": [40 + (i % 15) for i in range(300)]},
            },
        }}
    }
    return ("<html><head><title>x</title></head><body>"
            f"<script>window.__NEXT_DATA__ = {json.dumps(payload)};</script></body></html>")


def strava_json() -> str:
    start = int(time.time()) - 86400 * 5
    from datetime import datetime, timezone
    iso = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({
        "activity": {
            "id": 55667788, "name": "Strava JSON 测试", "type": "Run",
            "start_date": iso, "distance": 8000, "moving_time": 2400,
            "total_elevation_gain": 55, "average_heartrate": 150, "max_heartrate": 178,
        },
        "streams": {
            "time": {"data": list(range(240))},
            "latlng": {"data": [[30.26 + i * 1e-5, 120.16 + i * 1e-5] for i in range(240)]},
            "heartrate": {"data": [140 + (i % 30) for i in range(240)]},
        },
    })


def _ride_gpx(with_hr: bool, tag: str = "") -> bytes:
    """同一趟骑行的两份文件：一份纯轨迹（GPX 导出），一份带心率（FIT 导出等价物）。"""
    pts = []
    t0 = 1793929478
    for i in range(240):
        lat = 23.10 + i * 0.001
        lon = 113.20 + i * 0.001
        ts = t0 + i * 15
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        ext = ""
        if with_hr:
            ext = (f"<extensions><gpxtpx:TrackPointExtension>"
                   f"<gpxtpx:hr>{138 + (i % 7)}</gpxtpx:hr>"
                   f"<gpxtpx:cad>{84 + (i % 5)}</gpxtpx:cad>"
                   f"</gpxtpx:TrackPointExtension></extensions>")
        pts.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>20</ele>'
                   f'<time>{stamp}</time>{ext}</trkpt>')
    ns = (' xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1"'
          if with_hr else "")
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<gpx version="1.1" creator="TEST{tag}"{ns} '
            f'xmlns="http://www.topografix.com/GPX/1/1">'
            f'<trk><name>合并测试{tag}</name><trkseg>{"".join(pts)}</trkseg></trk></gpx>'
            ).encode("utf-8")


def _ride_tcx_hr_only() -> bytes:
    """只有心率/功率、没有坐标的 TCX（等价于 App 导出的「传感器版」文件）。"""
    t0 = 1793929478
    pts = []
    for i in range(240):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + i * 15))
        pts.append(
            "<Trackpoint>"
            f"<Time>{stamp}</Time>"
            f"<HeartRateBpm><Value>{140 + (i % 6)}</Value></HeartRateBpm>"
            f"<Cadence>{85 + (i % 4)}</Cadence>"
            f"<Extensions><TPX><Watts>{150 + (i % 30)}</Watts></TPX></Extensions>"
            "</Trackpoint>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
            '<Activities><Activity Sport="Biking"><Id>2026-11-06T09:44:38Z</Id><Lap><Track>'
            + "".join(pts)
            + "</Track></Lap></Activity></Activities></TrainingCenterDatabase>").encode("utf-8")


def test_merge_keeps_gps() -> None:
    """带轨迹的 GPX + 不带轨迹的传感器文件：合并后既要有地图，也要有心率。"""
    print("\n[合并] 轨迹与传感器分别在两个文件里")
    tmp = os.path.join(ROOT, ".import-test", "merge2")
    os.makedirs(tmp, exist_ok=True)
    store = open_store(os.path.join(tmp, "m.db"))
    try:
        imp = Importer(store, lambda: {})

        def one(name, data):
            res = imp.import_file(name, data)
            return (res.get("imported") or [{}])[0] if res.get("imported") else res

        gps = one("ride-gps.gpx", _ride_gpx(False))
        check("先导入的 GPX 有轨迹", store.get_activity(gps["id"], full=True)["has_gps"])

        sensors = one("ride-hr.tcx", _ride_tcx_hr_only())
        check("传感器文件合并进同一条", sensors.get("merged") is True, str(sensors)[:140])
        check("仍然只有一条记录", store.count() == 1, str(store.count()))

        merged = store.get_activity(gps["id"], full=True)
        check("合并后轨迹还在", bool(merged["has_gps"]) and len(merged.get("route") or []) > 10,
              f"gps={merged['has_gps']} route={len(merged.get('route') or [])}")
        check("合并后有心率", merged.get("avg_hr") is not None, str(merged.get("avg_hr")))
        check("合并后有功率", merged.get("avg_power") is not None, str(merged.get("avg_power")))
        check("合并后有功率区间/心率区间", bool(merged.get("hr_zones")), str(merged.get("hr_zones"))[:80])
        check("合并后流里含心率", any(v is not None for v in (merged.get("streams") or {}).get("hr", [])), "")
    finally:
        store.close()


def test_merge() -> None:
    """同一趟运动的 GPX + FIT 应该合成一条，而不是两条。"""
    print("\n[合并] 同一次运动的两种格式")
    tmp = os.path.join(ROOT, ".import-test", "merge")
    os.makedirs(tmp, exist_ok=True)
    store = open_store(os.path.join(tmp, "merge.db"))
    try:
        imp = Importer(store, lambda: {})

        def one(name, data):
            res = imp.import_file(name, data)
            return (res.get("imported") or [{}])[0] if res.get("imported") else res

        plain = one("ride-plain.gpx", _ride_gpx(False))
        check("GPX 首次新建", plain["created"] and not plain.get("merged"), str(plain)[:120])
        check("GPX 没有心率", not plain["has_hr"])

        rich = one("ride-rich.gpx", _ride_gpx(True))
        check("FIT(带心率) 合并进同一条", rich.get("merged") is True, str(rich)[:160])
        check("没有产生重复记录", store.count() == 1, str(store.count()))
        merged = store.get_activity(rich["id"], full=True)
        check("合并后有心率", merged.get("avg_hr") is not None, str(merged.get("avg_hr")))
        check("合并后保留踏频", bool((merged.get("streams") or {}).get("cad")), "")
        check("合并保留原 id", rich["id"] == plain["id"], f"{rich['id']} vs {plain['id']}")

        # 再把最初那份 GPX 传一遍：指纹已被记住，不该新增，也不该把心率冲掉
        one("ride-plain.gpx", _ride_gpx(False))
        check("旧文件重传不新增", store.count() == 1, str(store.count()))
        kept = store.get_activity(plain["id"], full=True)
        check("重传低配文件不冲掉心率", kept.get("avg_hr") is not None, str(kept.get("avg_hr")))

        # 起点晚 3 小时的另一次骑行不该被合并
        late_pts = []
        for i in range(240):
            lat = 30.10 + i * 0.001
            lon = 120.20 + i * 0.001
            ts = 1793929478 + 10800 + i * 15
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
            late_pts.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>20</ele>'
                            f'<time>{stamp}</time></trkpt>')
        late = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><gpx version=\"1.1\" creator=\"T\" "
                "xmlns=\"http://www.topografix.com/GPX/1/1\"><trk><name>另一趟</name><trkseg>"
                + "".join(late_pts) + "</trkseg></trk></gpx>").encode("utf-8")
        third = one("ride-late.gpx", late)
        check("不同时间的活动不被合并", third["created"] and not third.get("merged"), str(third)[:120])
        check("两条活动并存", store.count() == 2, str(store.count()))
    finally:
        store.close()


def main() -> int:
    print("=" * 56)
    print("导入链路端到端测试")
    print("=" * 56)

    tmp = os.path.join(ROOT, ".import-test")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    db = os.path.join(tmp, "t.db")
    store = open_store(db)

    files = {
        "run.gpx": None, "ride.tcx": None, "ride.fit": None, "list.csv": None,
        "strava.html": strava_html().encode("utf-8"),
        "strava.json": strava_json().encode("utf-8"),
        "streams.json": json.dumps({
            "time": {"data": list(range(120))},
            "latlng": {"data": [[30.3 + i * 1e-5, 120.2 + i * 1e-5] for i in range(120)]},
        }).encode("utf-8"),
    }
    for name in ("run_trail.gpx", "trainer_ride.tcx", "road_ride.fit", "activities.csv"):
        src = os.path.join(ROOT, "sample-data", name)
        with open(src, "rb") as fh:
            files[name] = fh.read()

    imported = 0
    errors = []
    for name, data in files.items():
        if data is None:
            continue
        print(f"\n[{name}]")
        try:
            parsed = parsers.detect_and_parse(name, data)
        except Exception:
            parsed = None
        if parsed is None:
            if name.endswith(".html"):
                parsed = strava_web.parse_saved_activity_page(data.decode("utf-8"))
            else:
                try:
                    parsed = strava_web.parse_strava_json(data)
                except parsers.ParseError:
                    parsed = None
        if parsed is None:
            # 纯 streams 无起始时间：应当被明确拒绝，而不是静默入库
            check("纯 streams 缺时间被拒绝", name == "streams.json", name)
            continue
        records = parsed if isinstance(parsed, list) else [parsed]
        for rec in records:
            try:
                if rec.get("points"):
                    built = analysis.analyze(rec["points"], rec.get("sport", "generic"))
                    check(f"{name} 有点可分析", built["summary"]["distance_m"] > 0,
                          str(built["summary"]["distance_m"]))
                else:
                    normalized = dict(rec)
                    normalized.setdefault("distance_m", (rec.get("summary") or {}).get("total_distance"))
                    normalized.setdefault("moving_seconds", (rec.get("summary") or {}).get("total_timer"))
                    built = build_from_summary(normalized)
                    check(f"{name} 摘要可构建", built["summary"]["distance_m"] > 0,
                          str(built["summary"]["distance_m"]))
                store.upsert_activity({
                    "source": rec.get("source_format", "import"),
                    # 与 server._persist 一致：普通 JSON 也补 Strava ID
                    "external_id": rec.get("external_id") or _strava_id_from_json(data),
                    "file_hash": None,
                    "filename": name,
                    "sport": rec.get("sport", "generic"),
                    "title": rec.get("title"),
                    "summary": built["summary"],
                    "streams": built.get("streams") or {},
                    "route": built.get("route") or [],
                })
                imported += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")
                check(f"{name} 入库", False, str(exc))

    print()
    check("累计导入", imported >= 10, f"{imported} 条")
    check("无异常", not errors, "; ".join(errors[:2]))
    total = store.count()
    check("数据库计数", total >= 10, str(total))

    # Strava 活动应保留 external_id 并可去重
    rows = store.conn.execute(
        "SELECT sport, title, external_id FROM activities WHERE external_id LIKE 'strava:%'"
    ).fetchall()
    check("Strava 活动入库", len(rows) >= 2, str(len(rows)))
    if rows:
        sports = {r["sport"] for r in rows}
        check("Strava 类型正确", sports <= {"cycling", "running"}, str(sports))

    # 重复导入同一外部 ID 不应新增
    before = store.count()
    store.upsert_activity({
        "source": "strava-html", "external_id": "strava:99887766", "filename": "dup",
        "sport": "cycling", "title": "dup", "summary": build_from_summary({
            "sport": "cycling", "start_time": time.time() - 86400, "distance_m": 1000,
            "moving_seconds": 100,
        })["summary"],
    })
    check("外部 ID 去重", store.count() == before, f"{store.count()} vs {before}")

    store.close()
    test_merge()
    test_merge_keeps_gps()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 56)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
