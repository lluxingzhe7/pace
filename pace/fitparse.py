"""FIT 二进制解析器（Garmin / Wahoo / Coros / Suunto / 各类码表导出的 .fit）。

不依赖第三方库。实现 FIT 协议读取子集：文件头、定义消息、数据消息、
压缩时间戳记录头、开发者字段跳过，并校验文件头 CRC。

统一输出结构与 GPX/TCX 解析器一致，见 parsers.py。

FIT 时间戳：自 1989-12-31T00:00:00Z 起的秒数（不是 Unix 时间）。
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

FIT_EPOCH_OFFSET = 631065600  # 1989-12-31T00:00:00Z 到 Unix epoch 的秒数

# 基础类型 -> (类别, 无效值, 字节宽度)。类别: u 无符号, i 有符号, f 浮点, s 字符串
_BASE: dict[int, tuple[str, int | None, int | None]] = {
    0x00: ("u", 0xFF, 1),            # enum
    0x01: ("i", 0x7F, 1),            # sint8
    0x02: ("u", 0xFF, 1),            # uint8
    0x83: ("i", 0x7FFF, 2),          # sint16
    0x84: ("u", 0xFFFF, 2),          # uint16
    0x85: ("i", 0x7FFFFFFF, 4),      # sint32
    0x86: ("u", 0xFFFFFFFF, 4),      # uint32
    0x07: ("s", None, None),         # string
    0x88: ("f", None, 4),            # float32
    0x89: ("f", None, 8),            # float64
    0x0A: ("u", 0, 1),               # uint8z
    0x8B: ("u", 0, 2),               # uint16z
    0x8C: ("u", 0, 4),               # uint32z
    0x0D: ("u", 0xFF, 1),            # byte
    0x8E: ("i", 0x7FFFFFFFFFFFFFFF, 8),  # sint64
    0x8F: ("u", 0xFFFFFFFFFFFFFFFF, 8),  # uint64
}

MESG_SESSION = 18
MESG_RECORD = 20
MESG_ACTIVITY = 34

SPORT_MAP = {
    0: "generic", 1: "running", 2: "cycling", 3: "transition", 4: "fitness_equipment",
    5: "swimming", 6: "basketball", 11: "walking", 17: "hiking", 21: "rowing",
    25: "skiing", 35: "triathlon", 53: "paddling",
}

# sport -> {sub_sport 枚举值: 归一化运动名}
SUB_SPORT_MAP = {
    1: {1: "treadmill_running", 3: "trail_running", 4: "track_running"},
    2: {6: "indoor_cycling", 7: "road_cycling", 8: "mountain_biking", 11: "cyclocross",
        15: "gravel_cycling"},
    5: {17: "pool_swimming", 18: "open_water_swimming"},
}

SUB_NAMES = {
    0: "generic", 1: "treadmill", 2: "street", 3: "trail", 4: "track", 5: "spin",
    6: "indoor", 7: "road", 8: "mountain", 9: "downhill", 10: "recumbent",
    11: "cyclocross", 12: "hand", 13: "track_cycling", 14: "indoor_cycling", 15: "gravel",
    17: "lap", 18: "open_water", 19: "flexibility_training",
}


class FitError(Exception):
    pass


_TABLE: list[int] | None = None


def _crc_table() -> list[int]:
    global _TABLE
    if _TABLE is None:
        tbl = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
            tbl.append(c)
        _TABLE = tbl
    return _TABLE


def crc16(data: bytes, crc: int = 0) -> int:
    table = _crc_table()
    for byte in data:
        crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
    return crc


@dataclass
class Def:
    global_num: int
    big_endian: bool
    fields: list[tuple[int, int, int]] = field(default_factory=list)  # (num, size, base)


@dataclass
class Point:
    t: float | None = None
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    hr: float | None = None
    cad: float | None = None
    power: float | None = None
    speed: float | None = None
    temp: float | None = None
    dist: float | None = None


@dataclass
class Session:
    sport: int = 0
    sub_sport: int | None = None
    start: float | None = None
    elapsed: float | None = None
    timer: float | None = None
    distance: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_power: float | None = None
    np_power: float | None = None
    calories: float | None = None
    ascent: float | None = None


def parse_fit(data: bytes, filename: str = "") -> dict:
    """解析 FIT 字节流，返回 {sport, sub_sport, start_time, points, summary, source_format}。"""
    if len(data) < 14:
        raise FitError("文件太短，不是有效的 FIT 文件")
    header_size = data[0]
    if header_size not in (12, 14):
        raise FitError(f"未知的 FIT 文件头长度 {header_size}")
    if data[8:12] != b".FIT":
        raise FitError("缺少 .FIT 签名")
    data_size = struct.unpack("<I", data[4:8])[0]
    if header_size + data_size > len(data):
        raise FitError("FIT 数据区被截断")

    pos = header_size
    end = header_size + data_size
    defs: dict[int, Def] = {}
    points: list[Point] = []
    session = Session()
    last_ts: int | None = None

    while pos < end:
        rec = data[pos]
        pos += 1
        is_def = bool(rec & 0x40)
        dev_flag = bool(rec & 0x20)

        if is_def:
            # 定义消息：记录头 + 保留字节 + 架构 + 全局消息号(2B) + 字段数 + 字段定义
            if pos + 5 > end:
                break
            local = rec & 0x0F
            pos += 1                      # 保留字节（真实设备会写，规范要求跳过）
            arch = data[pos]
            pos += 1
            big_endian = arch == 1
            order = "big" if big_endian else "little"
            gnum = int.from_bytes(data[pos:pos + 2], order)
            pos += 2
            nfields = data[pos]
            pos += 1
            fields = []
            for _ in range(nfields):
                if pos + 3 > end:
                    break
                fields.append((data[pos], data[pos + 1], data[pos + 2]))
                pos += 3
            if dev_flag and pos < end:
                ndev = data[pos]
                pos += 1 + ndev * 3
            # 开发者字段定义（字段号 0..255 之外）不参与解析
            defs[local] = Def(gnum, big_endian, fields)
            continue

        compressed = bool(rec & 0x80)
        if compressed:
            # 压缩时间戳头：bit7=1，bit5-6=时间偏移，bit0-4=本地消息类型
            local = rec & 0x1F
            time_offset = (rec >> 5) & 0x03
        else:
            local = rec & 0x0F
            time_offset = None

        d = defs.get(local)
        if d is None:
            break

        chunks: list[tuple[int, bytes, int]] = []  # (字段号, 原始字节, 基础类型)
        truncated = False
        for num, size, base in d.fields:
            chunk = data[pos:pos + size]
            if len(chunk) < size:
                truncated = True
                break
            chunks.append((num, chunk, base))
            pos += size
        if truncated:
            break

        ts_raw: int | None = None
        values: dict[int, object] = {}
        for num, chunk, base in chunks:
            value, raw_int = _decode(chunk, base, d.big_endian)
            if num == 253 and raw_int is not None:
                ts_raw = raw_int
            values[num] = value
        if ts_raw is not None:
            ts: int | None = ts_raw
            last_ts = ts_raw
        elif compressed and last_ts is not None:
            candidate = (last_ts & ~0x1F) + time_offset
            if candidate < last_ts:
                candidate += 0x20
            ts = candidate
            last_ts = candidate
        else:
            ts = None

        if d.global_num == MESG_RECORD:
            pt = _record_point(values, ts)
            if pt is not None:
                points.append(pt)
        elif d.global_num == MESG_SESSION:
            _apply_session(session, values, ts)

    real_points = [p for p in points if p.t is not None]
    real_points.sort(key=lambda p: p.t)
    if not real_points:
        raise FitError("FIT 中没有可用的轨迹记录点")

    start = session.start
    if start is None:
        start = real_points[0].t
    sport, sub = _sport_name(session.sport, session.sub_sport)

    summary = {
        "total_elapsed": session.elapsed,
        "total_timer": session.timer,
        "total_distance": session.distance,
        "avg_hr": session.avg_hr,
        "max_hr": session.max_hr,
        "avg_power": session.avg_power,
        "np_power": session.np_power,
        "calories": session.calories,
        "ascent": session.ascent,
    }
    return {
        "sport": sport,
        "sub_sport": sub,
        "title": None,
        "start_time": float(start),
        "points": [
            {"t": p.t, "lat": p.lat, "lon": p.lon, "alt": p.alt, "hr": p.hr,
             "cad": p.cad, "power": p.power, "speed": p.speed, "dist": p.dist}
            for p in real_points
        ],
        "summary": {k: v for k, v in summary.items() if v is not None},
        "source_format": "fit",
    }


def _decode(chunk: bytes, base: int, big_endian: bool) -> tuple[object, int | None]:
    """把原始字节解码为字段值，同时返回未符号化的整数（用于时间戳）。"""
    kind, invalid, width = _BASE.get(base, ("u", None, None))
    order = "big" if big_endian else "little"

    if kind == "s":
        return chunk.split(b"\x00", 1)[0].decode("utf-8", "replace").strip() or None, None
    if kind == "f":
        fmt = (">" if big_endian else "<") + ("f" if len(chunk) == 4 else "d")
        try:
            value = struct.unpack(fmt, chunk)[0]
        except struct.error:
            return None, None
        if math.isnan(value) or math.isinf(value):
            return None, None
        return value, None

    raw = int.from_bytes(chunk, order)
    if invalid is not None and raw == invalid:
        return None, None
    if kind == "i":
        return raw - (1 << (len(chunk) * 8)) if raw >= (1 << (len(chunk) * 8 - 1)) else raw, raw
    return raw, raw


def _semicircles(value: object) -> float | None:
    if not isinstance(value, int):
        return None
    degrees = value * (180.0 / 2147483648.0)
    if not (-90.0 <= degrees <= 90.0):
        return None
    return degrees


def _scaled(value: object, scale: float, offset: float) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value) / scale - offset


def _record_point(values: dict, ts: int | None) -> Point | None:
    lat = _semicircles(values.get(0))
    lon = _semicircles(values.get(1))
    if lat is None or lon is None:
        lat = lon = None
    alt = _scaled(values.get(2), 5.0, 500.0)
    if alt is None:
        alt = _scaled(values.get(78), 5.0, 500.0)
    speed = _scaled(values.get(6), 1000.0, 0.0)
    if speed is None:
        speed = _scaled(values.get(73), 1000.0, 0.0)
    cad = values.get(4)
    if isinstance(cad, (int, float)) and not (0 <= cad <= 254):
        cad = None
    hr = values.get(3)
    if isinstance(hr, (int, float)) and not (20 <= hr <= 260):
        hr = None
    power = values.get(7)
    if isinstance(power, (int, float)) and not (0 <= power <= 3000):
        power = None
    temp = values.get(13)
    if isinstance(temp, (int, float)) and not (-40 <= temp <= 60):
        temp = None
    dist = _scaled(values.get(5), 100.0, 0.0)

    if lat is None and hr is None and speed is None and power is None:
        return None
    return Point(
        t=(float(ts) + FIT_EPOCH_OFFSET) if ts is not None else None,
        lat=lat, lon=lon, alt=alt, hr=hr,
        cad=float(cad) if isinstance(cad, (int, float)) else None,
        power=float(power) if isinstance(power, (int, float)) else None,
        speed=speed, temp=temp, dist=dist,
    )


def _sport_name(sport: int, sub: int | None) -> tuple[str, str | None]:
    base = SPORT_MAP.get(sport, "generic")
    if sub is not None:
        mapped = SUB_SPORT_MAP.get(sport, {}).get(sub)
        if mapped:
            return mapped, SUB_NAMES.get(sub)
        name = SUB_NAMES.get(sub)
        if name and base == "generic":
            return name, name
    return base, None


def _apply_session(s: Session, v: dict, ts: int | None) -> None:
    start = v.get(2)
    if isinstance(start, int):
        s.start = float(start) + FIT_EPOCH_OFFSET
    elif ts is not None and s.start is None:
        s.start = float(ts) + FIT_EPOCH_OFFSET
    if isinstance(v.get(5), int):
        s.sport = int(v[5])
    if isinstance(v.get(6), int):
        s.sub_sport = int(v[6])
    s.elapsed = _scaled(v.get(7), 1000.0, 0.0) or s.elapsed
    s.timer = _scaled(v.get(8), 1000.0, 0.0) or s.timer
    s.distance = _scaled(v.get(9), 100.0, 0.0) or s.distance

    for num, attr in ((16, "avg_hr"), (17, "max_hr"), (20, "avg_power"),
                      (34, "np_power"), (11, "calories"), (22, "ascent")):
        value = v.get(num)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(s, attr, float(value))
