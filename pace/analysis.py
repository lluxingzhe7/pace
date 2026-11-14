"""活动分析引擎。

所有派生指标（距离、移动时间、均速、爬升、心率区间、训练负荷、最佳成绩）
都从原始轨迹点重算，保证 GPX / TCX / FIT / CSV 四种来源口径一致。

核心口径说明
------------
* 距离：GPS 逐点 haversine 累加；缺 GPS 时用点自带 dist / speed 回退
* 移动时间：间隔内平均速度超过运动阈值才计入；明显长于采样间隔且无位移的空档视为暂停
* 爬升：先平滑高程，再用滞回阈值累计，避免气压噪声虚高
* 配速：跑步/徒步 = 秒/公里，游泳 = 秒/100米，骑行 = 公里/小时
* 训练负荷：有心率用 TRIMP(Banister)，无心率用 RPE-时长模型
* 最佳成绩：1Hz 等距重采样后滑窗，O(n) 求各标准距离的最快用时
"""
from __future__ import annotations

import math
from typing import Iterable

from . import geo
from .parsers import sport_family

# ---------------------------------------------------------------- 参数

MOVING_SPEED_FLOOR_KMH = 1.5      # 低于此瞬时速度视为停住
PAUSE_GAP_SECONDS = 15.0          # 暂停判定阈值下限；采样稀疏时按 3 倍采样间隔放宽
ELEVATION_THRESHOLD = {"run": 1.5, "ride": 1.5, "swim": 0.0, "walk": 2.0, "other": 2.0}

DEFAULT_HR = {
    "rest": 55.0,
    "max": 190.0,
    "threshold": 168.0,
}

# 距离 -> 标签（跑步/骑行/游泳各自的标准成绩段）
BEST_EFFORT_DISTANCES = {
    "run": [
        (0.4, "400m"), (1_000, "1 km"), (1_609.34, "1 mi"), (5_000, "5 km"),
        (10_000, "10 km"), (21_097.5, "半马"), (42_195, "全马"),
    ],
    "ride": [
        (5_000, "5 km"), (10_000, "10 km"), (20_000, "20 km"),
        (40_000, "40 km"), (50_000, "50 km"), (100_000, "100 km"),
    ],
    "swim": [
        (400, "400 m"), (750, "750 m"), (1_500, "1.5 km"),
        (1_900, "1.9 km"), (3_800, "3.8 km"),
    ],
    "walk": [(1_000, "1 km"), (5_000, "5 km"), (10_000, "10 km")],
}


def family_of(sport: str) -> str:
    return sport_family(sport)


# ---------------------------------------------------------------- 主入口

def analyze(points: list[dict], sport: str = "generic", options: dict | None = None) -> dict:
    """把轨迹点序列分析为完整指标 + 1Hz 数据流。"""
    opts = dict(DEFAULT_HR)
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})

    family = family_of(sport)
    clean = _clean_points(points)
    if len(clean) < 2:
        raise ValueError("有效轨迹点不足（至少需要 2 个带时间戳的点）")

    times = [p["t"] for p in clean]
    n = len(clean)

    # ---- 距离数组
    latlngs = [(p["lat"], p["lon"]) for p in clean if p["lat"] is not None and p["lon"] is not None]
    has_gps = len(latlngs) >= max(2, int(n * 0.5))
    if has_gps:
        gps_index = [i for i, p in enumerate(clean) if p["lat"] is not None and p["lon"] is not None]
        cum_gps = geo.cumulative_distance([(clean[i]["lat"], clean[i]["lon"]) for i in gps_index])
        cum = [0.0] * n
        j = 0
        for i in gps_index:
            cum[i] = cum_gps[j]
            j += 1
        # 非 GPS 点用相邻值填充（保持单调）
        filled = []
        last = 0.0
        for i in range(n):
            if clean[i]["lat"] is None or clean[i]["lon"] is None:
                last = max(last, _interp_neighbor(cum, i))
            else:
                last = cum[i]
            filled.append(last)
        # 强制单调
        for i in range(1, n):
            if filled[i] < filled[i - 1]:
                filled[i] = filled[i - 1]
        cum = filled
    else:
        cum = _distance_from_fields(clean)
        has_gps = False

    # ---- 时间与移动时间
    total_elapsed = max(0.0, times[-1] - times[0])
    moving_seconds, stopped_seconds = _moving_time(clean, times, family)

    # ---- 速度序列
    speeds = _segment_speeds(clean, times, cum)  # km/h，长度 n-1
    if not speeds:
        speeds = [0.0]
    max_speed = _robust_max_speed(speeds, _sample_interval(times))

    # ---- 高程
    alts = [p["alt"] for p in clean]
    alt_valid = _fill_altitudes(alts)
    threshold = ELEVATION_THRESHOLD.get(family, 2.0)
    elevation_gain = geo.elevation_gain(alt_valid, threshold) if any(a is not None for a in alts) else 0.0
    elevation_loss = geo.elevation_loss(alt_valid, threshold) if any(a is not None for a in alts) else 0.0
    alt_min = min((a for a in alt_valid if a is not None), default=None)
    alt_max = max((a for a in alt_valid if a is not None), default=None)

    # ---- 心率
    hrs = [p["hr"] for p in clean]
    hr_valid = [(t, h) for p, t, h in zip(clean, times, hrs) if h is not None and 20 <= h <= 260]
    avg_hr = None
    max_hr = None
    zones: dict[str, float] = {}
    if hr_valid and moving_seconds > 60:
        # 以运动时间加权，停住时的心率不计入
        weights = []
        for idx, (t, h) in enumerate(hr_valid):
            nxt = hr_valid[idx + 1][0] if idx + 1 < len(hr_valid) else t + 1
            weights.append(min(30.0, max(0.0, nxt - t)))
        avg_hr = geo.weighted_mean([h for _, h in hr_valid], weights)
        max_hr = max(h for _, h in hr_valid)
        zones = _hr_zones(hr_valid, opts)

    # ---- 功率
    powers = [p["power"] for p in clean if p.get("power") is not None]
    avg_power = sum(powers) / len(powers) if powers else None
    np_power = _normalized_power([p["power"] for p in clean]) if len(powers) > 30 else None
    max_power = max(powers) if powers else None

    # ---- 汇总速度/配速
    distance_m = cum[-1]
    avg_speed_kmh = (distance_m / 1000.0) / (moving_seconds / 3600.0) if moving_seconds > 0 else 0.0
    pace = _pace_from(distance_m, moving_seconds, family)

    # ---- 训练负荷
    load, load_method = training_load(
        moving_seconds, avg_hr=avg_hr, sport_family=family,
        distance_m=distance_m, elevation_gain=elevation_gain,
        hr_max=float(opts["max"]), hr_rest=float(opts["rest"]),
    )

    # ---- 1Hz 数据流 + 最佳成绩/爬坡
    streams = _build_streams(clean, times, cum, alt_valid, family, max_points=2400)
    best = _best_efforts(clean, times, cum, family) if distance_m > 200 else []
    climbs = _detect_climbs(cum, alt_valid, family) if distance_m > 1000 else []

    # ---- 轨迹（抽稀到 900 点做地图折线）
    route_idx = geo.resample_indices(n, 900)
    route = [
        [round(clean[i]["lat"], 6), round(clean[i]["lon"], 6)]
        for i in route_idx if clean[i]["lat"] is not None and clean[i]["lon"] is not None
    ]
    center = None
    bbox = None
    if route:
        lats = [p[0] for p in route]
        lons = [p[1] for p in route]
        center = [round(sum(lats) / len(lats), 6), round(sum(lons) / len(lons), 6)]
        bbox = [min(lats), min(lons), max(lats), max(lons)]

    summary = {
        "start_time": float(times[0]),
        "distance_m": round(distance_m, 1),
        "elapsed_seconds": round(total_elapsed, 1),
        "moving_seconds": round(moving_seconds, 1),
        "stopped_seconds": round(stopped_seconds, 1),
        "avg_speed_kmh": round(avg_speed_kmh, 2),
        "max_speed_kmh": round(max_speed, 2),
        "pace_seconds_per_km": round(pace, 1) if pace else None,
        "elevation_gain_m": round(elevation_gain, 1),
        "elevation_loss_m": round(elevation_loss, 1),
        "elevation_min_m": round(alt_min, 1) if alt_min is not None else None,
        "elevation_max_m": round(alt_max, 1) if alt_max is not None else None,
        "avg_hr": round(avg_hr, 1) if avg_hr else None,
        "max_hr": round(max_hr, 1) if max_hr else None,
        "avg_power": round(avg_power, 1) if avg_power else None,
        "np_power": round(np_power, 1) if np_power else None,
        "max_power": round(max_power, 1) if max_power else None,
        "hr_zones": {k: round(v, 1) for k, v in zones.items()},
        "training_load": round(load, 1),
        "load_method": load_method,
        "has_gps": has_gps,
        "point_count": n,
        "best_efforts": best,
        "climbs": climbs,
        "center": center,
        "bbox": bbox,
    }
    return {"summary": summary, "streams": streams, "route": route}


# ---------------------------------------------------------------- 点清洗

def _clean_points(points: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    for raw in points:
        t = raw.get("t")
        if t is None:
            continue
        try:
            t = float(t)
        except (TypeError, ValueError):
            continue
        item = {"t": t}
        for key in ("lat", "lon", "alt", "hr", "cad", "power", "speed", "dist"):
            value = raw.get(key)
            if value is None:
                item[key] = None
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                item[key] = None
                continue
            item[key] = value if math.isfinite(value) else None
        # 坐标合法性
        if item["lat"] is not None and not (-90 <= item["lat"] <= 90):
            item["lat"] = None
        if item["lon"] is not None and not (-180 <= item["lon"] <= 180):
            item["lon"] = None
        if (item["lat"] is None) != (item["lon"] is None):
            item["lat"] = item["lon"] = None
        if abs(item["lat"] or 0) < 1e-7 and abs(item["lon"] or 0) < 1e-7:
            item["lat"] = item["lon"] = None
        out.append(item)

    out.sort(key=lambda p: p["t"])
    # 去重时间戳（保留信息更全的一条）
    deduped: list[dict] = []
    for item in out:
        if deduped and abs(item["t"] - deduped[-1]["t"]) < 1e-6:
            if _richness(item) > _richness(deduped[-1]):
                deduped[-1] = item
            continue
        deduped.append(item)
    return deduped


def _richness(p: dict) -> int:
    return sum(1 for k in ("lat", "alt", "hr", "cad", "power", "speed", "dist") if p.get(k) is not None)


def _interp_neighbor(values: list[float], index: int) -> float:
    for offset in range(1, len(values)):
        left = index - offset
        if left >= 0 and values[left] > 0:
            return values[left]
        right = index + offset
        if right < len(values) and values[right] > 0:
            return values[right]
    return 0.0


def _distance_from_fields(points: list[dict]) -> list[float]:
    """无 GPS（例如泳池游泳）时用文件自带的累计距离或速度积分。"""
    if any(p.get("dist") is not None for p in points):
        vals = [p["dist"] or 0.0 for p in points]
        for i in range(1, len(vals)):
            if vals[i] < vals[i - 1]:
                vals[i] = vals[i - 1]
        return vals
    cum = [0.0]
    for i in range(1, len(points)):
        dt = max(0.0, points[i]["t"] - points[i - 1]["t"])
        speed = points[i].get("speed") or points[i - 1].get("speed") or 0.0
        cum.append(cum[-1] + speed * dt)
    return cum


def _sample_interval(times: list[float]) -> float:
    """采样间隔（秒，取中位数）。1Hz 的 FIT 是 1.0，iGPSPORT 导出的 GPX 约 16。"""
    gaps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    if not gaps:
        return 0.0
    return gaps[len(gaps) // 2]


def _moving_time(points: list[dict], times: list[float], family: str) -> tuple[float, float]:
    floor_ms = MOVING_SPEED_FLOOR_KMH / 3.6
    if family == "swim":
        floor_ms = 0.3 / 3.6
    # 阈值跟着采样间隔走：16 秒一点的文件若死守 15 秒，整段骑行都会被判成暂停
    gap_limit = max(PAUSE_GAP_SECONDS, _sample_interval(times) * 3.0)
    moving = 0.0
    stopped = 0.0
    for i in range(1, len(points)):
        dt = times[i] - times[i - 1]
        if dt <= 0:
            continue
        implied = _implied_speed(points[i - 1], points[i])
        if dt > gap_limit:
            # 空档远长于本活动采样间隔：这段时间有没有位移才是判据。
            # 真停住（等红灯、补给）位移接近 0 记暂停；只是导出稀疏的继续骑记运动。
            if implied is not None and implied >= floor_ms:
                moving += dt
            else:
                stopped += dt
            continue
        # 优先用瞬时速度，其次由位置差推算
        speed = points[i].get("speed") or points[i - 1].get("speed")
        if speed is None:
            speed = implied
        if speed is not None and speed >= floor_ms:
            moving += dt
        else:
            stopped += dt
    return moving, stopped


def _implied_speed(a: dict, b: dict) -> float | None:
    if a.get("lat") is None or b.get("lat") is None:
        return None
    dt = b["t"] - a["t"]
    if dt <= 0:
        return None
    return geo.haversine(a["lat"], a["lon"], b["lat"], b["lon"]) / dt


def _segment_speeds(points: list[dict], times: list[float], cum: list[float]) -> list[float]:
    """每段平均速度 km/h。优先累计距离差，其次瞬时速度。"""
    speeds = []
    for i in range(1, len(points)):
        dt = times[i] - times[i - 1]
        if dt <= 0:
            continue
        d = cum[i] - cum[i - 1]
        if d > 0:
            speed = d / dt
        else:
            instant = points[i].get("speed") or points[i - 1].get("speed") or 0.0
            speed = float(instant)
        speeds.append(speed * 3.6)
    return speeds


def _robust_max_speed(speeds: list[float], sample_dt: float = 1.0) -> float:
    """取若干连续分段速度的均值最大值，抑制 GPS 跳点造成的假极速。

    窗口按时间折算：默认 5 秒（1Hz 数据即 5 个分段）。采样稀疏时窗口收窄，
    否则 16 秒一点的数据会把 80 秒平掉、反而低估真实极速。
    早期版本初始窗口取了 5 个值却按 ``i+1`` 平摊，起点附近有快段时极速会被放大 2 倍以上。
    """
    if not speeds:
        return 0.0
    width = 5
    if sample_dt and sample_dt > 1.0:
        width = max(1, int(round(5.0 / sample_dt)))
    width = min(width, len(speeds))
    if width <= 1:
        return max(speeds)
    window = sum(speeds[:width])
    best = window / width
    for i in range(width, len(speeds)):
        window += speeds[i] - speeds[i - width]
        best = max(best, window / width)
    return best


def _fill_altitudes(alts: list[float | None]) -> list[float | None]:
    """线性插值填补缺失高程；首尾用最近值外推。"""
    known = [(i, a) for i, a in enumerate(alts) if a is not None]
    if not known:
        return [None] * len(alts)
    out: list[float | None] = list(alts)
    for idx in range(len(out)):
        if out[idx] is not None:
            continue
        prev = next(((i, a) for i, a in reversed(known) if i < idx), None)
        nxt = next(((i, a) for i, a in known if i > idx), None)
        if prev and nxt:
            span = nxt[0] - prev[0]
            frac = (idx - prev[0]) / span if span else 0
            out[idx] = prev[1] + (nxt[1] - prev[1]) * frac
        elif prev:
            out[idx] = prev[1]
        elif nxt:
            out[idx] = nxt[1]
    return out


def _hr_zones(hr_series: list[tuple[float, float]], opts: dict) -> dict[str, float]:
    hr_max = float(opts["max"])
    hr_rest = float(opts["rest"])
    reserve = max(1.0, hr_max - hr_rest)
    bounds = [0.60, 0.70, 0.80, 0.90]
    zones = {"z1": 0.0, "z2": 0.0, "z3": 0.0, "z4": 0.0, "z5": 0.0}
    for idx, (t, hr) in enumerate(hr_series):
        nxt = hr_series[idx + 1][0] if idx + 1 < len(hr_series) else t + 1
        dt = min(30.0, max(0.0, nxt - t))
        if dt <= 0:
            continue
        pct = (hr - hr_rest) / reserve
        if pct < bounds[0]:
            zones["z1"] += dt
        elif pct < bounds[1]:
            zones["z2"] += dt
        elif pct < bounds[2]:
            zones["z3"] += dt
        elif pct < bounds[3]:
            zones["z4"] += dt
        else:
            zones["z5"] += dt
    return zones


def _normalized_power(power_series: list[float | None]) -> float | None:
    """NP = 30 秒滑窗均值的四次方平均的四次方根。"""
    values = [p for p in power_series if p is not None]
    if len(values) < 30:
        return None
    window = sum(values[:30]) / 30.0
    fourths = [window ** 4]
    for i in range(30, len(values)):
        window += (values[i] - values[i - 30]) / 30.0
        fourths.append(window ** 4)
    return (sum(fourths) / len(fourths)) ** 0.25


def _pace_from(distance_m: float, seconds: float, family: str) -> float | None:
    if distance_m <= 0 or seconds <= 0:
        return None
    if family in ("run", "walk"):
        return seconds / (distance_m / 1000.0)          # 秒/公里
    if family == "swim":
        return seconds / (distance_m / 100.0)           # 秒/100米
    return None


def training_load(seconds: float, *, avg_hr: float | None, sport_family: str,
                  distance_m: float, elevation_gain: float,
                  hr_max: float = 190.0, hr_rest: float = 55.0) -> tuple[float, str]:
    """训练负荷（相对努力度）。

    有心率: TRIMP = 时长(min) × HRr × 0.64·e^(1.92·HRr)，HRr 为心率储备比
    无心率: 时长 × 强度系数（跑步按配速、骑行按速度、其他按爬升）
    """
    minutes = seconds / 60.0
    if minutes <= 0:
        return 0.0, "none"

    if avg_hr and hr_max > hr_rest:
        hrr = max(0.0, min(1.0, (avg_hr - hr_rest) / (hr_max - hr_rest)))
        trimp = minutes * hrr * 0.64 * math.exp(1.92 * hrr)
        return round(trimp, 1), "trimp"

    # 无心率回退：用速度/爬升估算强度
    if sport_family == "run":
        pace = seconds / (distance_m / 1000.0) if distance_m > 0 else 0
        intensity = 1.0
        if pace:
            intensity = max(0.6, min(1.6, 330.0 / max(pace, 1.0)))
    elif sport_family == "ride":
        speed = (distance_m / 1000.0) / (seconds / 3600.0) if seconds > 0 else 0
        intensity = max(0.6, min(1.6, speed / 25.0))
    else:
        intensity = 1.0
    climb_bonus = elevation_gain / 100.0 * 0.5
    return round(minutes * intensity * 0.9 + climb_bonus, 1), "estimated"


# ---------------------------------------------------------------- 1Hz 流

def _build_streams(points: list[dict], times: list[float], cum: list[float],
                   alt_filled: list[float | None], family: str, max_points: int = 2400) -> dict:
    """把不规则采样重采样为等距序列（默认 1Hz，过长时自动降采样）。"""
    duration = times[-1] - times[0]
    if duration <= 0:
        return {}
    hz = 1.0
    est = duration * hz
    if est > max_points:
        hz = max(0.02, max_points / duration)
    count = max(2, int(duration * hz) + 1)

    idx = 0
    n = len(points)
    streams = {"t": [], "d": [], "alt": [], "hr": [], "speed": [], "cad": [], "pwr": []}
    raw_speed: list[float | None] = []
    for k in range(count):
        target = times[0] + k / hz
        while idx + 1 < n and times[idx + 1] <= target:
            idx += 1
        j = min(idx + 1, n - 1)
        t0, t1 = times[idx], times[j]
        frac = (target - t0) / (t1 - t0) if t1 > t0 else 0.0
        frac = max(0.0, min(1.0, frac))
        streams["t"].append(round(k / hz, 2))
        streams["d"].append(round(_lerp(cum[idx], cum[j], frac), 1))
        streams["alt"].append(_round_opt(_lerp_opt(alt_filled[idx], alt_filled[j], frac), 1))
        streams["hr"].append(_round_opt(_lerp_opt(points[idx].get("hr"), points[j].get("hr"), frac), 1))
        raw_speed.append(_lerp_opt(points[idx].get("speed"), points[j].get("speed"), frac))
        streams["cad"].append(_round_opt(_lerp_opt(points[idx].get("cad"), points[j].get("cad"), frac), 1))
        streams["pwr"].append(_round_opt(_lerp_opt(points[idx].get("power"), points[j].get("power"), frac), 1))

    # 速度流：优先用设备记录的瞬时速度（m/s），否则由距离差推算，最后统一转 km/h
    if len([v for v in raw_speed if v is not None]) >= count * 0.8:
        base = [float(v) if v is not None else 0.0 for v in raw_speed]
    else:
        base = [0.0]
        for k in range(1, count):
            base.append(max(0.0, (streams["d"][k] - streams["d"][k - 1]) * hz))
    streams["speed"] = [round(v * 3.6, 2) for v in geo.smooth(base, 5)]

    # 配速流（跑步/徒步用 秒/公里）
    if family in ("run", "walk"):
        pace = []
        for v in streams["speed"]:
            if v and v > 0.5:
                pace.append(round(3600.0 / v, 1))
            else:
                pace.append(None)
        streams["pace"] = pace
    return streams


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


def _lerp_opt(a: float | None, b: float | None, f: float) -> float | None:
    if a is None and b is None:
        return None
    if a is None:
        return b
    if b is None:
        return a
    return a + (b - a) * f


def _round_opt(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


# ---------------------------------------------------------------- 最佳成绩

def _best_efforts(points: list[dict], times: list[float], cum: list[float], family: str) -> list[dict]:
    """滑窗求标准距离的最快用时。要求该窗口内确实移动过（排除下坡滑行等）。"""
    targets = BEST_EFFORT_DISTANCES.get(family)
    if not targets:
        return []
    total = cum[-1]
    results: list[dict] = []
    for dist, label in targets:
        if total < dist * 0.98:
            continue
        best_time = None
        best_start = None
        j = 0
        n = len(cum)
        for i in range(n):
            if j < i:
                j = i
            while j < n and cum[j] - cum[i] < dist:
                j += 1
            if j >= n:
                break
            dt = times[j] - times[i]
            if dt <= 0:
                continue
            if best_time is None or dt < best_time:
                best_time = dt
                best_start = cum[i]
        if best_time and best_time > 5:
            pace = best_time / (dist / 1000.0) if family != "swim" else best_time / (dist / 100.0)
            results.append({
                "distance_m": round(dist, 1),
                "label": label,
                "seconds": round(best_time, 1),
                "pace_seconds": round(pace, 1),
                "start_distance_m": round(best_start or 0.0, 1),
            })
    return results


def _detect_climbs(cum: list[float], alt: list[float | None], family: str) -> list[dict]:
    """识别持续爬升段：>= 300m 且平均坡度 >= 3%。"""
    if family not in ("run", "ride", "walk"):
        return []
    if not alt or all(a is None for a in alt):
        return []
    values = [a if a is not None else 0.0 for a in alt]
    sm = geo.smooth(values, 15)
    climbs = []
    i = 0
    n = len(sm)
    while i < n:
        # 找爬升起点
        j = i + 1
        while j < n and sm[j] < sm[j - 1]:
            j += 1
        if j >= n:
            break
        start = j - 1
        k = start + 1
        peak = start
        while k < n and sm[k] >= sm[peak] - 1.0:
            if sm[k] > sm[peak]:
                peak = k
            k += 1
        if peak > start:
            gain = sm[peak] - sm[start]
            dist = cum[peak] - cum[start]
            if dist >= 300 and gain >= 8 and (gain / dist) >= 0.03:
                climbs.append({
                    "start_m": round(cum[start], 1),
                    "end_m": round(cum[peak], 1),
                    "length_m": round(dist, 1),
                    "gain_m": round(gain, 1),
                    "avg_grade_pct": round(gain / dist * 100, 1),
                })
        i = peak + 1
        if len(climbs) >= 8:
            break
    climbs.sort(key=lambda c: -c["gain_m"])
    return climbs[:5]
