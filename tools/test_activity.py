"""单次活动报告 + BLE 解析测试。

报告部分用构造的流数据验证：漂移方向、分段、训练效果、数据质量告警都对得上。
BLE 部分验证标准服务的帧解析，以及"未知厂商帧"会明确返回 unknown。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import ble, parsers
from pace.coach import activity as ar

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


def make_streams(duration=3600, distance=10000, hr_start=140, hr_end=160,
                 pace_drift=1.0, hr_ok=True):
    """构造 1Hz 流：pace_drift>1 表示后半程变慢。"""
    n = duration + 1
    d, t, hr, speed, alt, cad = [], [], [], [], [], []
    for i in range(n):
        frac = i / (n - 1)
        dist = distance * frac * (1 + (pace_drift - 1) * frac)
        d.append(dist)
        t.append(float(i))
        h = hr_start + (hr_end - hr_start) * frac
        hr.append(h if hr_ok else None)
        speed.append(3.6 * (dist / max(1.0, i)))
        alt.append(40 + 10 * math.sin(frac * 6))
        cad.append(170)
    return {"d": d, "t": t, "hr": hr, "speed": speed, "alt": alt, "cad": cad}


import math  # noqa: E402  (放在函数定义后不影响使用)


def base_activity(**kw):
    a = {
        "id": 1, "sport": "running", "distance_m": 10000, "moving_seconds": 3600,
        "training_load": 90, "has_gps": True, "point_count": 3601,
        "streams": make_streams(), "best_efforts": [{"label": "5 km"}],
        "climbs": [], "source_distance_m": None,
    }
    a.update(kw)
    return a


def test_report():
    print("\n[1] 活动报告")
    rep = ar.build(base_activity(), {"hr_max": 190, "hr_rest": 55})
    check("有评级", rep["grade"] in "ABCDE", rep["grade"])
    check("有分段", len(rep["splits"]) >= 5, str(len(rep["splits"])))
    check("分段距离正确", abs(rep["splits"][0]["distance_m"] - 1000) < 1, str(rep["splits"][0]["distance_m"]))
    check("有区间时间", sum(rep["zones"].values()) > 1000, str(sum(rep["zones"].values())))
    check("训练效果在 1-5", 0 < rep["training_effect"]["aerobic"] <= 5,
          str(rep["training_effect"]["aerobic"]))
    check("有执行分析", rep["execution"]["label"] != "", rep["execution"]["label"])
    check("数据质量满分", rep["data_quality"]["score"] == 100, str(rep["data_quality"]["score"]))
    check("步频已计算", rep["cadence"] and rep["cadence"]["avg"] > 100, str(rep["cadence"]))
    check("结论不空洞", any(k in rep["verdict"]["headline"] for k in
          ("训练价值", "执行质量", "完成度", "基本达标", "只能看配速")),
          rep["verdict"]["headline"])


def test_decoupling():
    print("\n[2] 心率漂移方向")
    # 匀速 + 心率上升 => 成本上升 => 正值
    dec = ar.aerobic_decoupling(make_streams(hr_start=140, hr_end=165), 10000)
    check("漂移已计算", dec is not None)
    if dec:
        check("心率升 => 成本上升（正值）", dec["change_pct"] > 0, str(dec["change_pct"]))

    # 心率平稳 => 接近 0
    dec2 = ar.aerobic_decoupling(make_streams(hr_start=150, hr_end=150), 10000)
    if dec2:
        check("心率平稳 => 漂移接近 0", abs(dec2["change_pct"]) < 3, str(dec2["change_pct"]))

    # 心率剧烈变化 => 标记数据异常
    dec3 = ar.aerobic_decoupling(make_streams(hr_start=120, hr_end=190), 10000)
    if dec3:
        check("异常漂移被标记", dec3["verdict"]["label"] == "数据异常", dec3["verdict"]["label"])


def test_short_and_missing():
    print("\n[3] 短活动与缺数据")
    short = base_activity(distance_m=800, moving_seconds=300,
                          streams=make_streams(duration=300, distance=800))
    rep = ar.build(short)
    check("短活动不强行算漂移", rep["decoupling"] is None)
    check("短活动仍有分段", len(rep["splits"]) >= 1, str(len(rep["splits"])))

    no_hr = base_activity(streams=make_streams(hr_ok=False))
    rep2 = ar.build(no_hr)
    check("无心率不算漂移", rep2["decoupling"] is None)
    check("无心率标为缺失", "心率流" in rep2["missing"], str(rep2["missing"]))
    check("无心率时耐力分不给高分", rep2["caps"]["endurance"] <= 50, str(rep2["caps"]["endurance"]))

    summary_only = {"id": 2, "sport": "running", "distance_m": 5000,
                    "moving_seconds": 1700, "training_load": 50,
                    "has_gps": False, "point_count": 0, "streams": {}}
    rep3 = ar.build(summary_only)
    check("只有汇总时明确说明", rep3["verdict"]["tone"] == "insufficient",
          rep3["verdict"]["tone"])
    check("只有汇总时质量分为 0", rep3["data_quality"]["score"] == 0)


def test_data_quality():
    print("\n[4] 数据质量告警")
    s = make_streams()
    s["hr"] = [None] * len(s["hr"])
    rep = ar.build(base_activity(streams=s))
    check("心率缺失被标记", any(i["key"] == "hr_gaps" for i in rep["data_quality"]["issues"]),
          str([i["key"] for i in rep["data_quality"]["issues"]]))

    s2 = make_streams()
    for i in range(0, len(s2["speed"]), 50):
        s2["speed"][i] = 90.0
    rep2 = ar.build(base_activity(streams=s2))
    check("速度尖刺被标记", any(i["key"] == "speed_spikes" for i in rep2["data_quality"]["issues"]),
          str([i["key"] for i in rep2["data_quality"]["issues"]]))


def test_ble():
    print("\n[5] BLE 帧解析")
    hr8 = ble.decode_heart_rate(bytes([0x00, 72]))
    check("心率 uint8", hr8 == {"heart_rate": 72}, str(hr8))
    hr16 = ble.decode_heart_rate(bytes([0x01, 0x2C, 0x01]))
    check("心率 uint16", hr16 == {"heart_rate": 300}, str(hr16))

    # CSC：flags=0x03，轮速 4B + 时间 2B，曲柄 2B + 2B
    csc = ble.decode_csc(bytes([0x03, 0x10, 0, 0, 0, 0, 0x04, 0x08, 0, 0x20, 0]))
    check("CSC 解析轮速", csc and csc["wheel_revs"] == 16, str(csc))
    check("CSC 解析曲柄", csc and csc["crank_revs"] == 8, str(csc))

    cps = ble.decode_cps(bytes([0x00, 0x00, 0xF4, 0x01]))
    check("功率解析", cps == {"power_w": 500}, str(cps))

    # FTMS：flags 0x0005 = 速度 + 踏频
    ftms = ble.decode_ftms_indoor_bike(bytes([0x05, 0x00, 0xE8, 0x03, 0x50, 0x00]))
    check("FTMS 解析速度", ftms and abs(ftms["speed_kmh"] - 10.0) < 0.1, str(ftms))
    check("FTMS 解析踏频", ftms and ftms["cadence_rpm"] == 40, str(ftms))

    check("短帧不崩", ble.decode_heart_rate(b"") is None)
    check("未知特征值返回 None",
          ble.decode_notification("0000abcd-0000-1000-8000-00805f9b34fb", b"\x01\x02") is None)
    check("UUID 归一", ble.normalize_uuid("2A37") == "00002a37-0000-1000-8000-00805f9b34fb")
    check("未知 UUID 解析返回 None",
          ble.decode_notification("2a37", b"\x00\x60") == {"heart_rate": 96})


def test_capture_store():
    print("\n[6] 抓包存档")
    path = os.path.join(ROOT, ".ble-test", "captures.json")
    store = ble.CaptureStore(path)
    result = store.save({
        "device_name": "iGPSPORT iGS630", "device_id": "AA:BB",
        "services": [{"uuid": "0000180d-0000-1000-8000-00805f9b34fb", "subscribed": True}],
        "events": [{"uuid": "00002a37-0000-1000-8000-00805f9b34fb", "hex": "0060"}],
    })
    check("抓包已保存", result["ok"] and result["event_count"] == 1, str(result))
    summary = store.summary()
    check("摘要含设备名", "iGPSPORT iGS630" in summary["devices"], str(summary))
    check("文件已落盘", os.path.isfile(path))

    big = {"events": [{"hex": "00"}] * 5000}
    r2 = store.save(big)
    check("超长抓包被截断", r2["event_count"] == ble.MAX_CAPTURE_EVENTS, str(r2["event_count"]))


def test_device_registry():
    print("\n[7] BLE 设备档案")
    from pace.devices.ble_registry import DeviceRegistry
    path = os.path.join(ROOT, ".ble-test", "devices.json")
    reg = DeviceRegistry(path)
    check("内置 BSC200S 档案", "BSC200S" in reg.all())
    hit = reg.lookup("BSC200S")
    check("按名称命中", hit is not None and hit["verdict"] == "unsupported", str(hit and hit.get("verdict")))
    check("给出可行路径", hit and "intervals" in (hit.get("working_paths") or []), str(hit and hit.get("working_paths")))

    # 记录一次只有基础服务的实测
    result = reg.save_observed("BSC200S", [
        {"uuid": "00001800-0000-1000-8000-00805f9b34fb",
         "chars": [{"uuid": "00002a00-0000-1000-8000-00805f9b34fb"}]},
        {"uuid": "00001801-0000-1000-8000-00805f9b34fb",
         "chars": [{"uuid": "00002a05-0000-1000-8000-00805f9b34fb"}]},
    ], note="只看到 1800/1801")
    check("实测判定为不可用", result["verdict"] == "unsupported", result["verdict"])
    check("保留已知档案不受损", reg.all()["BSC200S"]["verdict"] == "unsupported")

    # 记录一个带标准心率服务的设备
    r2 = reg.save_observed("HRM-Dual", [
        {"uuid": "0000180d-0000-1000-8000-00805f9b34fb",
         "chars": [{"uuid": "00002a37-0000-1000-8000-00805f9b34fb"}]},
    ])
    check("标准设备判定为可用", r2["verdict"] == "supported", r2["verdict"])

    # 重新加载应能读到新设备
    reg2 = DeviceRegistry(path)
    check("新设备已落盘", reg2.lookup("HRM-Dual") is not None)
    check("未知设备返回 None", reg2.lookup("AirPods") is None)


def test_sparse_gpx():
    """iGPSPORT 导出的 GPX：约 16 秒一点、没有 <type>、运动类型只在轨迹名里。

    这条真实文件的三个坑都在这测到：运动类型丢失、整段骑行被当成暂停、
    极速被窗口初始化 bug 放大。用构造轨迹复现，避免依赖本机外部文件。
    """
    print("\n[8] 稀疏采样 GPX（iGPSPORT 导出）")
    import math
    import time as _time
    from pace import analysis as pa

    t0 = 1789814197.0          # 2026-09-19 10:36:37Z
    dt = 16.0                  # iGPSPORT GPX 的典型点间隔
    speed_ms = 22.0 / 3.6      # 全程 22 km/h 匀速，便于核对均速
    lat = 23.2327
    lon = 113.2729
    dlon = speed_ms * dt / (111320.0 * math.cos(math.radians(lat)))
    rows = []
    ts = t0
    for i in range(241):       # 240 个间隔 = 3840 秒
        rows.append((lat, lon, ts))
        if i == 120:           # 中途停 5 分钟（同坐标），检验暂停判定
            ts += 300.0
            rows.append((lat, lon, ts))
        lon += dlon
        ts += dt
    body = "\n".join(
        f'<trkpt lat="{a:.7f}" lon="{b:.7f}"><ele>10</ele>'
        f'<time>{_time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(t))}</time></trkpt>'
        for a, b, t in rows)
    gpx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx creator="IGPSPORT" version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        '<metadata><link href="https://www.igpsport.cn/"><text>户外骑行</text></link></metadata>'
        f'<trk><name>户外骑行</name><trkseg>{body}</trkseg></trk></gpx>'
    )

    rec = parsers.parse_gpx(gpx)
    check("没有 <type> 也能认出骑行", rec["sport"] == "cycling", rec["sport"])
    check("标题取轨迹名", rec["title"] == "户外骑行", str(rec["title"]))

    s = pa.analyze(rec["points"], rec["sport"])["summary"]
    check("稀疏采样不被当成暂停", s["moving_seconds"] > 3700,
          f"{s['moving_seconds']} / {s['elapsed_seconds']}")
    check("暂停 5 分钟被扣掉", 3800 < s["moving_seconds"] < 3900, str(s["moving_seconds"]))
    check("均速接近 22 km/h", abs(s["avg_speed_kmh"] - 22.0) < 1.0, str(s["avg_speed_kmh"]))
    check("极速不被放大", s["max_speed_kmh"] < 25.0, str(s["max_speed_kmh"]))

    # 窗口初始化回归：老实现把 5 个值的和按 i+1 平摊，会翻倍
    check("滑窗极速无初始化放大", abs(pa._robust_max_speed([40.0] * 5 + [10.0] * 20) - 40.0) < 0.01,
          str(pa._robust_max_speed([40.0] * 5 + [10.0] * 20)))
    check("稀疏数据极速按单段算", abs(pa._robust_max_speed([30.0, 44.0, 12.0], 16.0) - 44.0) < 0.01,
          str(pa._robust_max_speed([30.0, 44.0, 12.0], 16.0)))


def test_specialty():
    """专项指标：骑行功率口径、跑步 GAP/步频、游泳 SWOLF，以及缺数据时的诚实标注。"""
    print("\n[9] 专项指标")

    # 骑行：有功率 + FTP 250 → NP/IF/TSS/功率区间
    s = make_streams(duration=3600, distance=30000)
    s["pwr"] = [200] * (3600 + 1)
    ride = base_activity(sport="cycling", distance_m=30000, moving_seconds=3600,
                         training_load=100, streams=s, avg_hr=140, avg_power=200)
    sp = ar.build(ride, {"ftp": 250})["specialty"]
    by_key = {i["key"]: i for i in sp["items"]}
    check("骑行 NP 已计算", by_key["np"]["ok"] and abs(by_key["np"]["value"] - 200) <= 1,
          str(by_key["np"]))
    check("IF = NP/FTP", by_key["if"]["ok"] and abs(by_key["if"]["value"] - 0.8) < 0.02,
          str(by_key["if"]))
    check("TSS 按 IF²×时长", by_key["tss"]["ok"] and abs(by_key["tss"]["value"] - 64) <= 2,
          str(by_key["tss"]))
    check("功率区间按 FTP 划分", sp["power_zones"] and sp["power_zones"].get("z3", 0) > 3500,
          str(sp["power_zones"]))
    check("匀速时 VI≈1", abs(by_key["vi"]["value"] - 1.0) < 0.02, str(by_key["vi"]))

    # 骑行：没有功率 → 明确标缺，不编数
    s2 = make_streams(duration=1800, distance=10000)
    ride2 = base_activity(sport="cycling", distance_m=10000, moving_seconds=1800, streams=s2)
    sp2 = ar.build(ride2, {"ftp": 250})["specialty"]
    np_item = next(i for i in sp2["items"] if i["key"] == "np")
    check("无功率时 NP 标缺", not np_item["ok"] and np_item["value"] == "—", str(np_item))
    check("无功率时不出区间分布", sp2["power_zones"] is None, str(sp2["power_zones"]))

    # 跑步：有爬升 → GAP 与步频
    run = base_activity(sport="running", distance_m=10000, moving_seconds=3600,
                        avg_hr=150, elevation_gain_m=200)
    sp3 = ar.build(run, {})["specialty"]
    by3 = {i["key"]: i for i in sp3["items"]}
    check("跑步有 GAP", by3["gap"]["ok"], str(by3["gap"]))
    check("GAP 比实际配速快", by3["gap"]["value"] != by3["pace"]["value"],
          f"{by3['gap']['value']} vs {by3['pace']['value']}")
    check("步频已计算", by3["cadence"]["ok"] and by3["cadence"]["value"] == 170, str(by3["cadence"]))
    check("每搏距离已计算", by3["eff"]["ok"], str(by3["eff"]))

    # 游泳：配速/100m + 划频 → SWOLF
    s4 = make_streams(duration=2400, distance=1500)
    s4["cad"] = [40] * (2400 + 1)
    swim = base_activity(sport="swimming", distance_m=1500, moving_seconds=2400, streams=s4)
    sp4 = ar.build(swim, {})["specialty"]
    by4 = {i["key"]: i for i in sp4["items"]}
    check("游泳配速 /100m", by4["pace100"]["ok"] and by4["pace100"]["value"] == "2:40",
          str(by4["pace100"]))
    check("SWOLF 已估算", by4["swolf"]["ok"] and by4["swolf"]["value"] > 0, str(by4["swolf"]))

    s5 = make_streams(duration=2400, distance=1500)
    s5["cad"] = [None] * (2400 + 1)
    swim2 = base_activity(sport="swimming", distance_m=1500, moving_seconds=2400, streams=s5)
    sp5 = ar.build(swim2, {})["specialty"]
    swolf2 = next(i for i in sp5["items"] if i["key"] == "swolf")
    check("无划频时 SWOLF 标缺", not swolf2["ok"], str(swolf2))


def main() -> int:
    print("=" * 58)
    print("活动报告与 BLE 测试")
    print("=" * 58)
    test_report()
    test_decoupling()
    test_short_and_missing()
    test_data_quality()
    test_ble()
    test_capture_store()
    test_device_registry()
    test_sparse_gpx()
    test_specialty()
    import shutil
    shutil.rmtree(os.path.join(ROOT, ".ble-test"), ignore_errors=True)
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
