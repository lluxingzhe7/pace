"""Strava 网页数据测试：内嵌 JSON 提取、摘要解析、streams 转 GPX。
不联网，全部基于本地构造的页面。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import strava_web
from pace.parsers import ParseError

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


def sample_page() -> str:
    payload = {
        "props": {
            "pageProps": {
                "activity": {
                    "id": 12345678901,
                    "name": "Morning Run",
                    "type": "Run",
                    "sport_type": "TrailRun",
                    "start_date": "2026-11-08T06:30:00Z",
                    "distance": 10320.5,
                    "moving_time": 3180,
                    "elapsed_time": 3300,
                    "total_elevation_gain": 184.2,
                    "average_heartrate": 152.3,
                    "max_heartrate": 178,
                    "description": "轻松跑",
                },
                "streams": {
                    "time": {"data": [0, 1, 2, 3, 4]},
                    "latlng": {"data": [[30.25, 120.15], [30.2501, 120.1501],
                                        [30.2502, 120.1502], [30.2503, 120.1503],
                                        [30.2504, 120.1504]]},
                    "altitude": {"data": [40, 41, 42, 43, 44]},
                    "heartrate": {"data": [140, 145, 150, 155, 160]},
                },
            }
        }
    }
    return (
        "<!DOCTYPE html><html><head><title>Activity</title></head><body>"
        "<script>window.__NEXT_DATA__ = " + json.dumps(payload) + ";</script>"
        "</body></html>"
    )


def sample_list_page() -> str:
    acts = [
        {"id": 1, "name": "Run A", "type": "Run", "start_date": "2026-11-07T07:00:00Z",
         "distance": 5000, "moving_time": 1500, "total_elevation_gain": 20,
         "average_heartrate": 148, "max_heartrate": 170},
        {"id": 2, "name": "Ride B", "type": "Ride", "start_date": "2026-11-06T08:00:00Z",
         "distance": 30000, "moving_time": 3600, "total_elevation_gain": 120,
         "average_heartrate": 135, "max_heartrate": 160},
    ]
    payload = {"props": {"pageProps": {"activities": acts}}}
    return ("<html><body><script>window.__NEXT_DATA__ = "
            + json.dumps(payload) + ";</script></body></html>")


def sample_streams() -> dict:
    return {
        "time": {"data": [0, 1, 2, 3]},
        "latlng": {"data": [[30.30, 120.20], [30.3001, 120.2001],
                            [30.3002, 120.2002], [30.3003, 120.2003]]},
        "velocity_smooth": {"data": [3.1, 3.2, 3.0, 3.3]},
        "heartrate": {"data": [130, 135, 140, 145]},
    }


def test_extract():
    print("\n[1] 内嵌 JSON 提取")
    page = sample_page()
    blobs = strava_web.extract_json_blobs(page)
    check("提取到 JSON 块", len(blobs) >= 1, str(len(blobs)))
    act = strava_web.find_activity_payload(blobs)
    check("定位到活动对象", act is not None and act.get("id") == 12345678901)
    check("保留原始字段", act and act.get("distance") == 10320.5)


def test_parse_page():
    print("\n[2] 活动详情解析")
    rec = strava_web.parse_saved_activity_page(sample_page())
    check("运动类型归一", rec["sport"] == "trail_running", rec["sport"])
    check("标题", rec["title"] == "Morning Run")
    check("起始时间", rec["start_time"] > 1_700_000_000, str(rec["start_time"]))
    check("有轨迹点", len(rec["points"]) == 5, str(len(rec["points"])))
    check("点含心率", rec["points"][0]["hr"] == 140)
    check("摘要距离", rec["summary"]["total_distance"] == 10320.5)
    check("外部 ID", rec["external_id"] == "strava:12345678901")

    bad = "<html><body>没有 JSON</body></html>"
    try:
        strava_web.parse_saved_activity_page(bad)
        check("空页面应报错", False)
    except ParseError:
        check("空页面应报错", True)


def test_parse_list():
    print("\n[3] 列表页解析")
    rows = strava_web.parse_saved_activity_list(sample_list_page())
    check("解析出 2 条", len(rows) == 2, str(len(rows)))
    check("类型正确", rows[0]["sport"] == "running" and rows[1]["sport"] == "cycling",
          f"{rows[0]['sport']},{rows[1]['sport']}")
    check("距离为米", rows[0]["distance_m"] == 5000)


def test_streams():
    print("\n[4] Streams 解析与 GPX 导出")
    raw = json.dumps(sample_streams())
    rec = strava_web.parse_saved_streams(raw, start_time=1_760_000_000, sport="ride")
    check("streams 点数", len(rec["points"]) == 4, str(len(rec["points"])))
    check("streams 类型", rec["sport"] == "cycling", rec["sport"])
    check("时间戳递增", rec["points"][1]["t"] - rec["points"][0]["t"] == 1.0)

    try:
        strava_web.parse_saved_streams(raw)
        check("缺时间应报错", False)
    except ParseError:
        check("缺时间应报错", True)

    gpx = strava_web.to_gpx(rec)
    check("GPX 有 trkpt", gpx.count("<trkpt") == 4, str(gpx.count("<trkpt")))
    check("GPX 合法头", gpx.startswith("<?xml") and "</gpx>" in gpx)

    # 导出后能被自家 GPX 解析器读回（往返一致性）
    from pace import parsers, analysis
    back = parsers.parse_gpx(gpx)
    check("回读运动类型", back["sport"] == "cycling", back["sport"])
    check("回读点数", len(back["points"]) == 4)
    result = analysis.analyze(back["points"], back["sport"])
    check("回读可分析", result["summary"]["distance_m"] > 0, str(result["summary"]["distance_m"]))


def test_guards():
    print("\n[5] 安全护栏")
    reader = strava_web.StravaPublicReader(allow_network=False)
    try:
        reader.fetch("https://www.strava.com/activities/1")
        check("默认禁用网络", False)
    except strava_web.ScrapeDisabled:
        check("默认禁用网络", True)

    reader2 = strava_web.StravaPublicReader(allow_network=True, cookie="")
    try:
        reader2.fetch("https://www.strava.com/activities/1")
        check("无 Cookie 拒绝", False)
    except strava_web.ScrapeDisabled:
        check("无 Cookie 拒绝", True)

    reader3 = strava_web.StravaPublicReader(allow_network=True, cookie="x", min_interval=0.01)
    check("间隔下限被抬高", reader3.min_interval >= strava_web.MIN_INTERVAL,
          str(reader3.min_interval))

    # 文件名推断时间
    ts = strava_web._guess_start_from_name("2026-11-08T06-30-00_streams.json")
    check("文件名推断时间", ts is not None and ts > 1_700_000_000)


def main() -> int:
    print("=" * 56)
    print("Strava 网页数据自检（离线）")
    print("=" * 56)
    test_extract()
    test_parse_page()
    test_parse_list()
    test_streams()
    test_guards()
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
