"""生成合成测试数据：GPX / TCX / FIT / CSV。

用于端到端验证，也可以当作导入格式的参考样例。
生成的轨迹是围绕某起点、带高程与心率的真实感路线。
"""
from __future__ import annotations

import math
import os
import random
import struct
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pace import geo
from pace.fitparse import FIT_EPOCH_OFFSET, crc16


def make_route(start_lat: float, start_lon: float, distance_m: float, *, seed: int = 1,
               loops: float = 2.0, speed_kmh: float = 11.5, spacing_m: float = 3.2,
               hill_scale: float = 1.0) -> list[dict]:
    """生成一条闭合/螺旋路线，总长度约等于 distance_m。

    做法：先在以米为单位的平面上画螺旋，保证每段弧长 ≈ spacing_m，
    最后换算成经纬度；时间戳按目标配速反推。这样距离、配速都自洽。
    """
    rng = random.Random(seed)
    loops = max(1.0, loops)
    spacing_m = max(1.0, spacing_m)
    # 周长 ≈ 每圈距离 / 圈数
    per_loop = distance_m / loops
    radius = per_loop / (2 * math.pi)
    # 螺旋总转角；点数由弧长决定
    theta_max = 2 * math.pi * loops
    n = max(60, int(distance_m / spacing_m))
    growth = 0.35  # 半径随角度增长，避免轨迹完全重叠

    xy = []
    for i in range(n):
        frac = i / (n - 1)
        th = frac * theta_max
        r = radius * (1.0 + growth * frac)
        xy.append((r * math.sin(th), r * math.cos(th)))

    # 缩放到精确总长
    planar = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in _pairs(xy))
    if planar > 0:
        k = distance_m / planar
        xy = [(x * k, y * k) for x, y in xy]

    lat0 = start_lat
    coslat = max(0.2, math.cos(math.radians(lat0)))
    meters_per_deg = 111_320.0
    points = []
    base_alt = 40.0
    for i, (x, y) in enumerate(xy):
        frac = i / (n - 1)
        th = frac * theta_max
        wobble = 0.015 * math.sin(th * 0.6 + 1.1) + 0.006 * math.sin(th * 2.1)
        east = x * (1 + wobble)
        north = y * (1 + wobble)
        lat = lat0 + north / meters_per_deg
        lon = start_lon + east / (meters_per_deg * coslat)
        hill = hill_scale * (28 * math.sin(frac * math.pi * 2.4) + 12 * math.sin(frac * math.pi * 7))
        alt = base_alt + hill + rng.uniform(-0.4, 0.4)
        hr = 138 + 26 * math.sin(frac * math.pi * 2.1) + rng.uniform(-4, 4)
        cad = 168 + 8 * math.sin(frac * math.pi * 5) + rng.uniform(-3, 3)
        power = 210 + 60 * math.sin(frac * math.pi * 2.2) + rng.uniform(-15, 15)
        points.append({
            "t_offset": i, "lat": lat, "lon": lon, "alt": alt,
            "hr": hr, "cad": cad, "power": power,
        })

    # 按实际空间距离重算时间轴
    cum = geo.cumulative_distance([(p["lat"], p["lon"]) for p in points])
    speed_ms = max(0.5, speed_kmh / 3.6)
    for p, d in zip(points, cum):
        p["t_offset"] = d / speed_ms
    return points


def _pairs(items):
    return list(zip(items[:-1], items[1:]))


def write_gpx(path: str, points: list[dict], start: datetime, title: str = "晨跑",
              sport: str = "running") -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="PACE test generator" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        '  <metadata><name>%s</name><time>%s</time></metadata>' % (title, start.isoformat()),
        '  <trk><name>%s</name><type>%s</type><trkseg>' % (title, sport),
    ]
    for p in points:
        t = (start + timedelta(seconds=p["t_offset"])).isoformat()
        lines.append(
            '    <trkpt lat="%.7f" lon="%.7f"><ele>%.2f</ele><time>%s</time>'
            '<extensions><gpxtpx:TrackPointExtension>'
            '<gpxtpx:hr>%d</gpxtpx:hr><gpxtpx:cad>%d</gpxtpx:cad>'
            '</gpxtpx:TrackPointExtension></extensions></trkpt>'
            % (p["lat"], p["lon"], p["alt"], t, round(p["hr"]), round(p["cad"]))
        )
    lines += ["  </trkseg></trk>", "</gpx>"]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def write_tcx(path: str, points: list[dict], start: datetime, sport: str = "Biking") -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/'
        'TrainingCenterDatabase/v2">',
        '  <Activities><Activity Sport="%s"><Id>%s</Id>' % (sport, start.isoformat()),
        '    <Lap StartTime="%s">' % start.isoformat(),
        '      <TotalTimeSeconds>%d</TotalTimeSeconds>' % len(points),
        '      <Track>',
    ]
    for p in points:
        t = (start + timedelta(seconds=p["t_offset"])).isoformat()
        lines.append(
            '        <Trackpoint><Time>%s</Time>'
            '<Position><LatitudeDegrees>%.7f</LatitudeDegrees>'
            '<LongitudeDegrees>%.7f</LongitudeDegrees></Position>'
            '<AltitudeMeters>%.2f</AltitudeMeters>'
            '<HeartRateBpm><Value>%d</Value></HeartRateBpm>'
            '<Cadence>%d</Cadence><Watts>%d</Watts></Trackpoint>'
            % (t, p["lat"], p["lon"], p["alt"], round(p["hr"]), round(p["cad"]), round(p["power"]))
        )
    lines += [
        '      </Track>',
        '    </Lap>',
        '  </Activity></Activities></TrainingCenterDatabase>',
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------- FIT 写入

def _fit_header(data_size: int) -> bytes:
    # header_size=14, protocol=2, profile=2100, data_size, ".FIT"
    head = struct.pack("<BBHI4s", 14, 2, 2100, data_size, b".FIT")
    return head + struct.pack(">H", crc16(head))


def _def_msg(local: int, global_num: int, fields: list[tuple[int, int, int]]) -> bytes:
    # 定义消息头 + 保留字节 + 架构 0(小端) + 全局消息号(小端)
    out = bytearray([0x40 | local])
    out.append(0)                     # 保留字节，规范要求存在
    out.append(0)                     # 架构：0 = 小端
    out += struct.pack("<H", global_num)
    out.append(len(fields))
    for num, size, base in fields:
        out += bytes([num, size, base])
    return bytes(out)


def write_fit(path: str, points: list[dict], start: datetime, *, sport: int = 2,
              sub_sport: int = 7) -> None:
    """写一个最小但结构完整的 FIT：record + session。"""
    fit_start = int(start.timestamp()) - FIT_EPOCH_OFFSET
    body = bytearray()

    # record 消息定义（全局 20）
    rec_fields = [
        (253, 4, 0x86),  # timestamp uint32
        (0, 4, 0x85),    # position_lat sint32
        (1, 4, 0x85),    # position_long
        (2, 2, 0x84),    # altitude uint16
        (3, 1, 0x02),    # heart_rate uint8
        (4, 1, 0x02),    # cadence uint8
        (6, 2, 0x84),    # speed uint16
        (7, 2, 0x84),    # power uint16
    ]
    body += _def_msg(0, 20, rec_fields)

    last_ts = None
    prev = None
    for p in points:
        ts = fit_start + int(p["t_offset"])
        lat = int(round(p["lat"] / (180.0 / 2147483648.0)))
        lon = int(round(p["lon"] / (180.0 / 2147483648.0)))
        alt = int(round((p["alt"] + 500.0) * 5))
        if prev is not None and p["t_offset"] > prev["t_offset"]:
            step = geo.haversine(prev["lat"], prev["lon"], p["lat"], p["lon"])
            speed = step / (p["t_offset"] - prev["t_offset"])
        else:
            speed = 8.0
        speed = max(0.3, min(30.0, speed))
        speed_cm_s = int(round(speed * 1000))
        hr = max(30, min(250, int(round(p["hr"]))))
        cad = max(0, min(250, int(round(p["cad"]))))
        power = max(0, min(2000, int(round(p["power"]))))
        body.append(0x00)  # 数据消息头，本地类型 0
        body += struct.pack("<IiiHBBHH", ts, lat, lon, alt, hr, cad, speed_cm_s, power)
        last_ts = ts
        prev = p

    # session 定义 + 数据
    start_ts = fit_start
    elapsed_ms = int(points[-1]["t_offset"] * 1000)
    distance_cm = int(round(points and _route_distance(points) * 100))
    sen_fields = [
        (253, 4, 0x86),  # timestamp
        (2, 4, 0x86),    # start_time
        (5, 1, 0x00),    # sport
        (6, 1, 0x00),    # sub_sport
        (7, 4, 0x86),    # total_elapsed_time (ms)
        (8, 4, 0x86),    # total_timer_time
        (9, 4, 0x86),    # total_distance (cm)
        (16, 1, 0x02),   # avg_heart_rate
        (17, 1, 0x02),   # max_heart_rate
        (20, 2, 0x84),   # avg_power
    ]
    body += _def_msg(1, 18, sen_fields)
    avg_hr = int(sum(p["hr"] for p in points) / len(points))
    max_hr = int(max(p["hr"] for p in points))
    avg_pw = int(sum(p["power"] for p in points) / len(points))
    body.append(0x01)
    body += struct.pack("<IIBBIIIBHHH", last_ts, start_ts, sport, sub_sport,
                        elapsed_ms, elapsed_ms, distance_cm, avg_hr, max_hr, avg_pw, 0)

    data = _fit_header(len(body)) + bytes(body) + struct.pack("<H", crc16(bytes(body)))
    with open(path, "wb") as fh:
        fh.write(data)


def _route_distance(points: list[dict]) -> float:
    return geo.cumulative_distance([(p["lat"], p["lon"]) for p in points])[-1]


def write_csv(path: str, start: datetime) -> None:
    """Strava 风格的 activities.csv，日期从 start 往前每 4 天一条。"""
    rows = [
        "Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Moving Time,"
        "Distance,Elevation Gain,Average Heart Rate,Max Heart Rate,Calories",
    ]
    names = ["滨江夜跑", "周末骑行", "泳池训练", "公园慢跑", "越野拉练"]
    types = ["Run", "Ride", "Swim", "Run", "TrailRun"]
    for i in range(12):
        d = start - timedelta(days=i * 4)
        dist = [5200, 32000, 1500, 4600, 12500][i % 5] * (1 + (i % 3) * 0.08)
        moving = dist / [2.9, 7.5, 0.95, 2.7, 2.2][i % 5]
        rows.append(
            f"{900000 + i},{d.strftime('%Y-%m-%d %H:%M:%S')},{names[i % 5]},"
            f"{types[i % 5]},{int(moving * 1.08)},{int(moving)},"
            f"{dist:.1f},{int(dist * 0.012)},{int(140 + i % 5 * 3)},{int(165 + i % 4 * 4)},"
            f"{int(moving * 0.16)}"
        )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(rows))


def generate_all(out_dir: str, count: int = 6) -> list[str]:
    """生成一组覆盖不同运动类型与格式的样例文件。"""
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(42)
    # 杭州西湖附近
    base_lat, base_lon = 30.2465, 120.1520
    # 相对"现在"生成，保证最近 28 天里有一定训练量
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # 各活动的天数偏移：覆盖本周、上月、季度
    day_offsets = [0, 2, 4, 7, 9, 12, 16, 21, 27, 34, 45, 60]
    written = []

    plans = [
        ("run_trail.gpx", "run", 8_400, 1.0, 1.8, 10.2),
        ("run_park.gpx", "run", 5_200, 2.0, 1.2, 11.4),
        ("ride_loop.gpx", "ride", 32_000, 3.0, 1.0, 27.0),
        ("run_long.gpx", "run", 15_800, 4.0, 2.4, 10.8),
        ("walk_evening.gpx", "walk", 3_400, 5.0, 1.0, 5.2),
    ]
    for idx, (name, kind, dist, seed, loops, speed) in enumerate(plans):
        pts = make_route(base_lat + idx * 0.004, base_lon + idx * 0.003, dist,
                         seed=int(seed * 10), loops=loops, speed_kmh=speed)
        start = (now - timedelta(days=day_offsets[idx], hours=rng.randint(0, 5))).replace(minute=0)
        path = os.path.join(out_dir, name)
        sport = {"run": "running", "ride": "cycling", "walk": "walking"}[kind]
        title = {"run": ["西湖越野", "公园晨跑", "长距离拉练"][idx % 3],
                 "ride": "环湖骑行", "walk": "江边徒步"}[kind]
        write_gpx(path, pts, start, title=title, sport=sport)
        written.append(path)

    # TCX：骑行台
    pts = make_route(base_lat + 0.02, base_lon + 0.02, 20_000, seed=77, loops=1.0, speed_kmh=30.0)
    tcx_path = os.path.join(out_dir, "trainer_ride.tcx")
    write_tcx(tcx_path, pts, now - timedelta(days=6), sport="Biking")
    written.append(tcx_path)

    # FIT：公路骑行
    pts = make_route(base_lat + 0.03, base_lon + 0.01, 45_000, seed=91, loops=1.3, speed_kmh=32.0)
    fit_path = os.path.join(out_dir, "road_ride.fit")
    write_fit(fit_path, pts, now - timedelta(days=3), sport=2, sub_sport=7)
    written.append(fit_path)

    # CSV：批量摘要（散落在过去 45 天）
    csv_path = os.path.join(out_dir, "activities.csv")
    write_csv(csv_path, now - timedelta(days=4))
    written.append(csv_path)

    return written


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "sample-data"
    files = generate_all(target)
    for f in files:
        print(f, os.path.getsize(f), "bytes")
