"""地理与信号处理工具。纯标准库实现。

坐标约定：所有函数使用 (lat, lon) 顺序，与 GPX/TCX/FIT 一致。
"""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6371008.8


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点球面距离（米）。"""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def cumulative_distance(latlngs: list[tuple[float, float]]) -> list[float]:
    """累计距离数组（米），长度与输入一致，首项为 0。"""
    out = [0.0]
    for i in range(1, len(latlngs)):
        out.append(out[-1] + haversine(*latlngs[i - 1], *latlngs[i]))
    return out


def smooth(values: list[float], window: int = 5) -> list[float]:
    """居中移动平均。窗口为奇数时对称，偶数时向后偏移。"""
    n = len(values)
    if n == 0 or window <= 1:
        return list(values)
    half = window // 2
    out = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        chunk = values[lo:hi]
        out.append(sum(chunk) / len(chunk))
    return out


def smooth_adaptive(values: list[float], window: int = 5, max_jump: float | None = None) -> list[float]:
    """平滑并可选地抑制毛刺（与邻点差异过大视为噪声，保留趋势）。"""
    sm = smooth(values, window)
    if max_jump is None:
        return sm
    return [v if abs(v - s) <= max_jump else s for v, s in zip(values, sm)]


def elevation_gain(altitudes: list[float], threshold: float = 1.5, smooth_window: int = 7) -> float:
    """累计爬升（米）。带平滑与滞回阈值，避免气压噪声累积虚高。

    threshold 是"确认一次爬升所需的最小净变化"，向下变化不扣减。
    """
    if len(altitudes) < 2:
        return 0.0
    sm = smooth(altitudes, smooth_window)
    gain = 0.0
    ref = sm[0]
    for v in sm[1:]:
        d = v - ref
        if d >= threshold:
            gain += d
            ref = v
        elif d < -threshold:
            ref = v
    return gain


def elevation_loss(altitudes: list[float], threshold: float = 1.5, smooth_window: int = 7) -> float:
    return elevation_gain([-a for a in altitudes], threshold, smooth_window)


def percentile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数，输入需已排序。q ∈ [0,1]。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * max(0.0, min(1.0, q))
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def weighted_mean(values: list[float], weights: list[float]) -> float:
    total_w = sum(weights)
    if total_w <= 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total_w


def encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    """Google Encoded Polyline，用于在数据库里紧凑存储轨迹。"""
    def _round(v: float) -> int:
        return int(math.floor(v * (10 ** precision) + 0.5))

    def _encode_signed(value: int) -> str:
        value = ~(value << 1) if value < 0 else (value << 1)
        chunks = []
        while value >= 0x20:
            chunks.append(chr((0x20 | (value & 0x1F)) + 63))
            value >>= 5
        chunks.append(chr(value + 63))
        return "".join(chunks)

    last_lat = 0
    last_lon = 0
    out = []
    for lat, lon in points:
        rlat, rlon = _round(lat), _round(lon)
        out.append(_encode_signed(rlat - last_lat))
        out.append(_encode_signed(rlon - last_lon))
        last_lat, last_lon = rlat, rlon
    return "".join(out)


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    lat = lon = 0
    index = 0
    factor = float(10 ** precision)
    length = len(encoded)

    def _next_delta() -> int:
        nonlocal index
        result = 0
        shift = 0
        while True:
            if index >= length:
                raise ValueError("polyline 数据被截断")
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        return ~(result >> 1) if (result & 1) else (result >> 1)

    while index < length:
        lat += _next_delta()
        lon += _next_delta()
        points.append((lat / factor, lon / factor))
    return points


def zoom_to_fit_span(lat_span: float, lon_span: float, width_px: int, height_px: int,
                     padding: float = 0.10) -> float:
    """给定经纬跨度与画布尺寸，返回最佳 slippy-map 缩放级别。"""
    lon_span = max(lon_span, 1e-6)
    lat_span = max(lat_span, 1e-6)
    usable_w = width_px * (1 - 2 * padding)
    usable_h = height_px * (1 - 2 * padding)
    # 每度经度在 zoom0 占 256px
    zoom_x = math.log2(usable_w / (lon_span * 256 / 360))
    # 墨卡托 y 尺度随纬度变化
    lat_scale = max(math.cos(math.radians(45)), 0.1)
    zoom_y = math.log2(usable_h / (lat_span * 256 / 360 / lat_scale))
    return max(0.0, min(19.0, min(zoom_x, zoom_y)))


def lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    """Web 墨卡托，单位为世界像素（zoom 0，256px）。"""
    x = (lon + 180.0) / 360.0 * 256.0
    siny = math.sin(math.radians(max(-85.05112878, min(85.05112878, lat))))
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * 256.0
    return x, y


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """方位角，0=北，顺时针，单位度。"""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def resample_indices(n: int, max_points: int) -> list[int]:
    """把 n 个点均匀抽稀到不超过 max_points 个索引（保留首尾）。"""
    if n <= max_points or max_points <= 2:
        return list(range(n))
    step = (n - 1) / (max_points - 1)
    return sorted({int(round(i * step)) for i in range(max_points)})
