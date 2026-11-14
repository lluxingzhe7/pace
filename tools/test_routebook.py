"""路书（pace/routebook.py）测试：转弯识别、每公里节点、爬坡分级。"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import routebook

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


def straight_line(n=300, meters=12000.0, lat0=23.1, lon0=113.3):
    """正东方向直线，n 个点，总长 meters。"""
    pts = []
    for i in range(n):
        frac = i / (n - 1)
        lon = lon0 + (frac * meters) / (111320.0 * math.cos(math.radians(lat0)))
        pts.append([lat0, lon])
    return pts


def streams_for(n, meters, *, alt_fn=None, speed=25.0):
    d = [meters * i / (n - 1) for i in range(n)]
    t = [x / (speed / 3.6) for x in d]
    alt = [alt_fn(i, n) if alt_fn else 10.0 for i in range(n)]
    return {"d": d, "t": t, "alt": alt, "speed": [speed] * n}


def l_shape(m=6000.0, per_leg=60):
    """先向东走 m/2，再向北走 m/2：中间一个 90° 左转。"""
    lat0, lon0 = 23.1, 113.3
    pts = []
    half = m / 2
    for i in range(per_leg + 1):
        f = i / per_leg
        pts.append([lat0, lon0 + (f * half) / (111320.0 * math.cos(math.radians(lat0)))])
    for i in range(1, per_leg + 1):
        f = i / per_leg
        pts.append([lat0 + (f * half) / 110540.0,
                    lon0 + half / (111320.0 * math.cos(math.radians(lat0)))])
    return pts


def test_basic_contract():
    print("\n[1] 基本契约")
    pts = straight_line()
    st = streams_for(len(pts), 12000.0)
    rb = routebook.build(pts, st)
    check("ok 为 True", rb["ok"] is True, str(rb.get("notes")))
    check("total_m 与轨迹长度一致", abs(rb["total_m"] - 12000) < 300, str(rb["total_m"]))
    check("输出字段齐全",
          all(k in rb for k in ("cues", "kms", "climbs", "turn_count", "climb_count",
                                "climb_gain", "notes")), str(sorted(rb)))
    check("kms 每公里一个", len(rb["kms"]) == 11, str(len(rb["kms"])))
    check("kms 里程递增",
          all(b["d"] > a["d"] for a, b in zip(rb["kms"], rb["kms"][1:])), "order")
    check("kms 带时间与海拔", all("t" in k and "alt" in k for k in rb["kms"]), "fields")
    check("直线没有转向提示", rb["turn_count"] == 0, str(rb["turn_count"]))
    check("直线时的说明可读", rb["notes"] and "转弯" in rb["notes"][0], str(rb["notes"]))


def test_turn_detection():
    print("\n[2] 转弯识别")
    pts = l_shape()
    st = streams_for(len(pts), 6000.0)
    rb = routebook.build(pts, st)
    check("识别到一个转向点", rb["turn_count"] == 1, str(rb["cues"]))
    cue = rb["cues"][0]
    check("方向为左转（向东转北 = 逆时针）", cue["dir"] == "左转", str(cue))
    check("里程在两段之间", 2000 < cue["d"] < 4000, str(cue["d"]))
    check("带方位角", 0 <= cue["heading"] <= 360, str(cue["heading"]))
    check("带海拔", cue["alt"] is not None, str(cue))


def test_filters():
    print("\n[3] 噪声过滤")
    # 起步 300 米内的抖动不算路口
    pts = straight_line(n=120, meters=1000.0)
    pts[1] = [pts[1][0] + 0.004, pts[1][1]]      # 刚出发就"转弯"
    st = streams_for(len(pts), 1000.0)
    rb = routebook.build(pts, st)
    check("起步抖动不产生提示", rb["turn_count"] == 0, str(rb["cues"]))

    # 停车状态下的方位变化不算路口
    pts = l_shape(m=6000.0)
    st = streams_for(len(pts), 6000.0, speed=25.0)
    st["speed"] = [1.0] * len(pts)                # 全程推车
    rb = routebook.build(pts, st)
    check("停车时不报路口", rb["turn_count"] == 0, str(rb["cues"]))

    # 提示间距 200 米：连续小抖动只留一个
    check("提示间距至少 200 米",
          all(b["d"] - a["d"] >= routebook.TURN_MIN_SPACING_M - 1
              for a, b in zip(rb["cues"], rb["cues"][1:])) if len(rb["cues"]) > 1 else True,
          "spacing")
    check("提示数不超过上限",
          len(rb["cues"]) <= routebook.MAX_CUES, str(len(rb["cues"])))


def test_climbs():
    print("\n[4] 爬坡")
    pts = straight_line(n=400, meters=16000.0)
    # 中间 4 公里连续爬 160 米 = 4%
    def alt(i, n):
        d = 16000.0 * i / (n - 1)
        if 6000 <= d <= 10000:
            return 10 + (d - 6000) / 4000 * 160
        return 10 + (160 if d > 10000 else 0)

    st = streams_for(len(pts), 16000.0, alt_fn=alt)
    rb = routebook.build(pts, st)
    check("识别到一段爬坡", rb["climb_count"] >= 1, str(rb["climbs"]))
    if rb["climbs"]:
        c = rb["climbs"][0]
        check("爬升量接近 160 米", 120 <= c["gain_m"] <= 200, str(c["gain_m"]))
        check("平均坡度接近 4%", 3.0 <= c["avg_grade_pct"] <= 5.0, str(c["avg_grade_pct"]))
        check("坡度分级有标签", c["label"] in ("HC", "1 级", "2 级", "3 级", "4 级"), c["label"])
    check("climb_gain 与爬坡段一致",
          abs(rb["climb_gain"] - sum(c["gain_m"] for c in rb["climbs"])) < 1,
          str(rb["climb_gain"]))
    check("平地不误报爬坡", routebook.build(straight_line(), streams_for(300, 12000))["climb_count"] == 0,
          "flat")


def test_degenerate():
    print("\n[5] 异常输入")
    rb = routebook.build([], {})
    check("空轨迹 ok=False", rb["ok"] is False, str(rb))
    check("空轨迹有说明", rb["notes"], str(rb["notes"]))
    check("空轨迹不抛异常", rb["cues"] == [] and rb["climbs"] == [], "empty")
    rb2 = routebook.build([[23.1, 113.3], [23.1, 113.3]], {})
    check("重合点 ok=False", rb2["ok"] is False, str(rb2["notes"]))


def test_store():
    print("\n[6] 路书存储")
    from pace import store as store_mod
    db_path = os.path.join(ROOT, ".tmp-test-routebook.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    st = store_mod.Store(db_path)
    try:
        pts = straight_line(41, 8000.0)
        bid = st.create_routebook({"name": "T1", "points": pts,
                                   "distance_m": 8000.0, "source": "manual"})
        check("新建返回 id", isinstance(bid, int) and bid > 0, str(bid))
        row = st.get_routebook(bid)
        check("存了点数", row and row["point_count"] == len(pts), str(row and row["point_count"]))
        check("距离保留", row and abs(row["distance_m"] - 8000.0) < 1, str(row and row["distance_m"]))
        check("points 往返一致",
              row and len(row["points"]) == len(pts)
              and abs(row["points"][0][0] - pts[0][0]) < 1e-9, "points")
        lst = st.list_routebooks()
        check("列表 1 条", len(lst) == 1, str(len(lst)))
        check("列表不带 points", "points" not in lst[0], str(list(lst[0].keys())))
        check("列表带预览点", 2 <= len(lst[0]["preview"]) <= 60, str(len(lst[0]["preview"])))
        check("列表带起终点", lst[0]["start"] and lst[0]["end"], "se")
        check("改名返回 True", st.update_routebook(bid, {"name": "T2"}) is True)
        check("改名生效", st.get_routebook(bid)["name"] == "T2", "name")
        check("换点同步点数", st.update_routebook(bid, {"points": pts[:5]}) is True
              and st.get_routebook(bid)["point_count"] == 5, "count")
        check("未知字段不算更新", st.update_routebook(bid, {"nope": 1}) is False)
        check("删不存在返回 False", st.delete_routebook(987654) is False)
        check("删除返回 True", st.delete_routebook(bid) is True)
        check("删后取不到", st.get_routebook(bid) is None)
        check("删后列表为空", st.list_routebooks() == [])
    finally:
        st.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)


def test_export():
    print("\n[7] 导出 GPX 与抽稀")
    import xml.etree.ElementTree as ET
    pts = straight_line(50, 5000.0)
    xml = routebook.to_gpx("测试<&>线", pts)
    check("是 XML 声明开头", xml.startswith("<?xml"), xml[:20])
    check("点数一致", xml.count("<trkpt") == 50, str(xml.count("<trkpt")))
    check("名字做了转义", "测试&lt;&amp;&gt;线" in xml, "esc")
    check("不带时间戳", "<time>" not in xml, "time")
    root = ET.fromstring(xml)
    tps = root.findall(".//{http://www.topografix.com/GPX/1/1}trkpt")
    check("XML 可解析", len(tps) == 50, str(len(tps)))
    check("坐标对得上",
          abs(float(tps[0].get("lat")) - pts[0][0]) < 1e-6, "lat")
    check("单点也能导出", routebook.to_gpx("x", pts[:1]).count("<trkpt") == 1, "one")
    check("空点不抛", routebook.to_gpx("x", []) != "", "empty")

    line = straight_line(200, 20000.0)
    simp = routebook.simplify(line, tolerance_m=8.0)
    check("直线抽稀到两点", len(simp) == 2, str(len(simp)))
    check("抽稀保留端点",
          abs(simp[0][0] - line[0][0]) < 1e-9 and abs(simp[0][1] - line[0][1]) < 1e-9
          and abs(simp[-1][1] - line[-1][1]) < 1e-9, "ends")
    corner = [[23.1, 113.3], [23.1, 113.4], [23.2, 113.4]]
    check("拐点不丢", len(routebook.simplify(corner, tolerance_m=8.0)) == 3, "corner")
    check("空输入返回空", routebook.simplify([], tolerance_m=8.0) == [])
    check("单点原样返回", len(routebook.simplify([[1.0, 2.0]], tolerance_m=8.0)) == 1, "one")


def test_waypoints():
    print("\n[9] 途经点")
    from pace import store as store_mod
    db_path = os.path.join(ROOT, ".tmp-test-waypoints.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    st = store_mod.Store(db_path)
    try:
        pts = straight_line(30, 6000.0)
        wps = [{"lat": pts[0][0], "lon": pts[0][1], "name": "起点",
                "tag": "打卡点", "note": "", "show": True},
               {"lat": pts[15][0], "lon": pts[15][1], "name": "补给",
                "tag": "食物补给", "note": "有水和面", "show": True}]
        bid = st.create_routebook({"name": "带途经点", "points": pts,
                                   "waypoints": wps, "distance_m": 6000.0})
        row = st.get_routebook(bid)
        check("途经点存进去", len(row["waypoints"]) == 2, str(len(row["waypoints"])))
        check("途经点字段完整",
              row["waypoints"][1]["tag"] == "食物补给"
              and row["waypoints"][1]["note"] == "有水和面", str(row["waypoints"][1]))
        lst = st.list_routebooks()
        check("列表带途经点数", lst[0]["waypoint_count"] == 2, str(lst[0].get("waypoint_count")))
        check("列表不带途经点正文", "waypoints" not in lst[0], str(list(lst[0].keys())))
        st.update_routebook(bid, {"waypoints": []})
        check("途经点可清空", st.get_routebook(bid)["waypoints"] == [], "clear")

        xml = routebook.to_gpx("带点路书", pts[:5], waypoints=wps[:1])
        check("GPX 写了 wpt", xml.count("<wpt ") == 1, str(xml.count("<wpt ")))
        check("wpt 带名称", "<name>起点</name>" in xml, "name")
        check("wpt 带标签", "<sym>打卡点</sym>" in xml, "sym")
        check("没有途经点时只有轨迹", "<wpt " not in routebook.to_gpx("x", pts[:3]), "none")
    finally:
        st.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)


def test_profile():
    print("\n[8] 海拔剖面")
    pts = straight_line(400, 16000.0)

    def alt(i, n):
        d = 16000.0 * i / (n - 1)
        return 20 + (d / 16000.0) * 400        # 全程缓上，20 → 420 m

    st = streams_for(len(pts), 16000.0, alt_fn=alt)
    rb = routebook.build(pts, st)
    prof = rb["profile"]
    check("有剖面点", len(prof) >= 3, str(len(prof)))
    check("剖面不超过上限", len(prof) <= routebook.PROFILE_MAX_POINTS, str(len(prof)))
    check("剖面里程递增",
          all(b[0] > a[0] for a, b in zip(prof, prof[1:])), "order")
    check("剖面覆盖全程", prof[-1][0] > 14000, str(prof[-1][0]))
    check("最高/最低海拔", rb["alt_min"] is not None and rb["alt_max"] is not None,
          f"{rb['alt_min']}/{rb['alt_max']}")
    check("海拔范围合理", 15 <= (rb["alt_min"] or 0) <= 30
          and 400 <= (rb["alt_max"] or 0) <= 460, str((rb["alt_min"], rb["alt_max"])))
    check("剖面累计爬升接近 400 m",
          380 <= rb["climb_gain_total"] <= 420, str(rb["climb_gain_total"]))
    check("没海拔时累计爬升为 0",
          routebook.build(pts, {})["climb_gain_total"] == 0, "empty-gain")

    # 点里带海拔（GPX 的 <ele>）也要能出剖面
    def alt_ramp(i, n):
        d = 16000.0 * i / (n - 1)
        if 6000 <= d <= 10000:
            return 20 + (d - 6000) / 4000 * 160      # 4 km 爬 160 m ≈ 4%
        return 20 + (160 if d > 10000 else 0)

    pts_ele = [[p[0], p[1], round(alt_ramp(i, len(pts)), 1)] for i, p in enumerate(pts)]
    rb2 = routebook.build(pts_ele, {})
    check("无 streams 也能从点出剖面", len(rb2["profile"]) >= 3, str(len(rb2["profile"])))
    check("点海拔带动转向海拔", all(c["alt"] is not None for c in rb2["cues"]) or not rb2["cues"],
          "cue alt")
    check("点海拔能算爬坡", rb2["climb_count"] >= 1, str(rb2["climbs"]))

    # 起步/隧道的 0 海拔不该把剖面拉到底
    pts_z = straight_line(300, 12000.0)
    st_z = streams_for(len(pts_z), 12000.0)
    st_z["alt"] = [0.0] * 4 + [20.0] * 146 + [0.0] * 2 + [45.0] * 148
    rb_z = routebook.build(pts_z, st_z)
    check("0 海拔被补掉", rb_z["alt_min"] >= 15, str(rb_z["alt_min"]))
    check("补完仍在合理范围", rb_z["alt_max"] <= 50, str(rb_z["alt_max"]))

    # 没有海拔就不给假数据
    rb3 = routebook.build(pts, {})
    check("没海拔时剖面为空", rb3["profile"] == [], str(len(rb3["profile"])))
    check("没海拔时给出说明",
          any("海拔" in n for n in rb3["notes"]), str(rb3["notes"]))

    # GPX 导出带 ele，往返一致
    import xml.etree.ElementTree as ET
    xml = routebook.to_gpx("带海拔", pts_ele[:10])
    check("GPX 写了 ele", xml.count("<ele>") == 10, str(xml.count("<ele>")))
    root = ET.fromstring(xml)
    ns = "{http://www.topografix.com/GPX/1/1}"
    eles = [float(t.find(ns + "ele").text) for t in root.findall(".//" + ns + "trkpt")]
    check("ele 数值对得上", abs(eles[0] - pts_ele[0][2]) < 0.2, str(eles[0]))
    check("没海拔的点不写 ele",
          routebook.to_gpx("无海拔", pts[:5]).count("<ele>") == 0, "no-ele")


def main():
    test_basic_contract()
    test_turn_detection()
    test_filters()
    test_climbs()
    test_degenerate()
    test_store()
    test_export()
    test_waypoints()
    test_profile()
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
