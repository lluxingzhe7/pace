"""路书：把一条真实骑过的轨迹变成可照着骑的分段导航。

输出三类信息：
- cues  转向/路口提示（按方位变化识别，带里程、方位角、海拔）
- kms   每公里节点（用时、海拔、均速），给码表对表用
- climbs 爬坡段（长度、爬升、平均坡度、难度分）

坐标来自 route（抽稀到 ~900 点），距离/海拔/时间来自 streams（1Hz），
两者按总长度比例对齐——route 是抽稀的，直接累加会偏短。
"""
from __future__ import annotations

import math
from typing import Iterable

from . import geo

# 判定参数
TURN_MIN_DEG = 30.0        # 前后方位差超过这个角度才算一个转弯
TURN_MIN_SPACING_M = 200.0  # 两个提示之间的最小间距
LOOK_M = 150.0             # 取前后各 150 米平均方位，压掉 GPS 抖动
MAX_CUES = 90

DESCENT_MIN_DEG = -2.0


def _turn_word(delta: float) -> tuple[str, str]:
    """delta>0 顺时针 = 右转。"""
    if delta >= 135:
        return "u", "掉头"
    if delta >= 45:
        return "right", "右转"
    if delta <= -135:
        return "u", "掉头"
    if delta <= -45:
        return "left", "左转"
    return "slight", "稍向右" if delta > 0 else "稍向左"


def _bearing_between(route: list, cum: list, i: int, j: int) -> float | None:
    if i < 0 or j >= len(route) or j <= i:
        return None
    return geo.bearing(route[i][0], route[i][1], route[j][0], route[j][1])


def _index_at(cum: list, d: float) -> int:
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] < d:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _stream_at(streams: dict, key: str, d: float) -> float | None:
    """按距离在 streams 里取最近的值。"""
    ds = streams.get("d") or []
    vs = streams.get(key) or []
    if not ds or not vs:
        return None
    i = _index_at(ds, d)
    i = min(i, len(vs) - 1)
    return vs[i]


def _scale_climb(climb: dict) -> dict:
    gain = float(climb.get("gain_m") or 0)
    length = float(climb.get("length_m") or 0)
    grade = float(climb.get("avg_grade_pct") or 0)
    # 难度分：爬升为主，坡度加成；HC 约 100
    score = gain * (1.0 + max(0.0, grade) / 8.0)
    return {
        "start_m": round(float(climb.get("start_m") or 0), 1),
        "end_m": round(float(climb.get("end_m") or 0), 1),
        "length_m": round(length, 0),
        "gain_m": round(gain, 0),
        "avg_grade_pct": round(grade, 1),
        "score": round(score, 0),
        "label": _climb_label(score),
    }


def _climb_label(score: float) -> str:
    if score >= 900:
        return "HC"
    if score >= 500:
        return "1 级"
    if score >= 260:
        return "2 级"
    if score >= 120:
        return "3 级"
    return "4 级"


CLIMB_MIN_M = 300.0        # 一段爬坡至少这么长
CLIMB_MIN_GRADE = 3.0      # 平均坡度门槛（%）
CLIMB_GAP_M = 250.0        # 两段爬坡之间这么短的平路合并成一段
CLIMB_WINDOW_M = 120.0     # 算坡度的滑动窗口
PROFILE_MAX_POINTS = 240   # 海拔剖面抽稀到这么多点，够画图、也不卡


def _alt_from_points(points: list, cum: list) -> tuple[list, list] | None:
    """手绘/导入的路书没有 streams，但点里可能带海拔（GPX 的 <ele>）。

    单独抽一条 (距离, 海拔) 序列出来，后面 cues/kms/climbs/剖面都能用，
    省得每个地方再判断一次"有没有海拔"。
    """
    pairs = []
    for i, p in enumerate(points):
        if len(p) > 2 and p[2] is not None and i < len(cum):
            try:
                pairs.append((cum[i], float(p[2])))
            except (TypeError, ValueError):
                continue
    if len(pairs) < 20:
        return None
    d = [x[0] for x in pairs]
    alt = geo.smooth([x[1] for x in pairs], 9)
    return d, alt


def _fill_alt_gaps(values: list) -> list:
    """把「连续一小段掉到 0（或接近 0）」补掉：码表起步、过隧道时常常记 0。

    只在画剖面时用；爬坡判定仍然用原始值，避免把没信号的地方算成下坡。
    整条轨迹本身就在海平面附近时（中位数 <5 m），不去动这些值。
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if not n:
        return vals
    ordered = sorted(vals)
    gap_level = 1.0 if ordered[n // 2] > 5.0 else 0.0
    # 码表的气压高度在隧道/桥下会连续掉到 0 再跳回来（这里见过 ~25 秒的），
    # 30 个采样以内都按「没信号」处理；再长就当成真的下坡。
    max_gap_run = 30
    out = list(vals)
    i = 0
    while i < n:
        if out[i] > gap_level:
            i += 1
            continue
        j = i
        while j < n and out[j] <= gap_level:
            j += 1
        if j - i <= max_gap_run:
            before = out[i - 1] if i > 0 else None
            after = out[j] if j < n else None
            if before is not None and after is not None:
                span = j - i + 1
                for k in range(i, j):
                    out[k] = before + (after - before) * ((k - i + 1) / span)
            elif after is not None:
                out[i:j] = [after] * (j - i)
            elif before is not None:
                out[i:j] = [before] * (j - i)
        i = j
    return out


def _profile(streams: dict, points: list, cum: list) -> tuple[list, float | None, float | None]:
    """海拔剖面：优先用活动的 streams，没有再用点里的海拔。"""
    ds = streams.get("d") or []
    alts = streams.get("alt") or []
    pairs: list[tuple[float, float]] = []
    if ds and len(alts) == len(ds) and any(a is not None for a in alts):
        idx_keep = [i for i in range(len(ds)) if alts[i] is not None]
        raw_d = [float(ds[i]) for i in idx_keep]
        raw_a = geo.smooth(_fill_alt_gaps([float(alts[i]) for i in idx_keep]), 5)
        pairs = list(zip(raw_d, raw_a))
    else:
        raw = _alt_from_points(points, cum)
        if raw:
            pairs = list(zip(raw[0], _fill_alt_gaps(raw[1])))
    if len(pairs) < 2:
        return [], None, None
    idx = geo.resample_indices(len(pairs), PROFILE_MAX_POINTS)
    prof = [[round(float(pairs[i][0]), 0), round(float(pairs[i][1]), 1)] for i in idx]
    alts_only = [p[1] for p in prof]
    return prof, round(min(alts_only), 1), round(max(alts_only), 1)


def _climbs(streams: dict, route_m: float) -> list[dict]:
    """按「局部坡度」切爬坡段。

    不用全段单调性判断：先平地再爬 4 公里时，整段平均坡度只有 1%，
    单调法会把爬坡吞掉。这里先算滑动窗口坡度，再把连续 ≥3% 的区间拼起来。
    """
    d = [x for x in (streams.get("d") or [])]
    alt = [x for x in (streams.get("alt") or [])]
    n = min(len(d), len(alt))
    d, alt = d[:n], alt[:n]
    if n < 30:
        return []
    # 海拔缺失的点先丢掉
    pairs = [(d[i], alt[i]) for i in range(n) if alt[i] is not None and d[i] is not None]
    if len(pairs) < 30:
        return []
    dd = [p[0] for p in pairs]
    aa = [p[1] for p in pairs]
    if dd[-1] - dd[0] < CLIMB_MIN_M:
        return []

    # 每 25 米一个采样点，坡度取前后各 CLIMB_WINDOW_M/2 的差，
    # 相当于「这 120 米平均陡不陡」，不会被单点海拔抖动带偏。
    step = 25.0
    grid = []
    x = dd[0]
    j = 0
    while x <= dd[-1]:
        while j < len(dd) - 2 and dd[j + 1] < x:
            j += 1
        span = dd[j + 1] - dd[j]
        frac = 0.0 if span <= 0 else (x - dd[j]) / span
        grid.append((x, aa[j] + (aa[j + 1] - aa[j]) * frac))
        x += step
    if len(grid) < 20:
        return []

    half = max(1, int(round(CLIMB_WINDOW_M / 2 / step)))
    grades = []
    for i in range(len(grid)):
        lo, hi = max(0, i - half), min(len(grid) - 1, i + half)
        dist = grid[hi][0] - grid[lo][0]
        grades.append(((grid[hi][1] - grid[lo][1]) / dist * 100.0) if dist > 1 else 0.0)

    # 连续 ≥3% 的区间，允许中间夹一小段缓平路
    runs = []
    i = 0
    while i < len(grades):
        if grades[i] < CLIMB_MIN_GRADE:
            i += 1
            continue
        start = i
        last_steep = i
        while i + 1 < len(grades):
            i += 1
            if grades[i] >= CLIMB_MIN_GRADE:
                last_steep = i
            elif grid[i][0] - grid[last_steep][0] > CLIMB_GAP_M:
                break
        runs.append((start, last_steep))

    climbs = []
    for start, end in runs:
        if end <= start:
            continue
        start_m = grid[start][0]
        end_m = grid[end][0]
        length = end_m - start_m
        if length < CLIMB_MIN_M:
            continue
        gain = grid[end][1] - grid[start][1]
        if gain < 8:
            continue
        climbs.append(_scale_climb({
            "start_m": start_m,
            "end_m": end_m,
            "length_m": length,
            "gain_m": gain,
            "avg_grade_pct": gain / length * 100.0,
        }))
    climbs.sort(key=lambda c: -c["gain_m"])
    return climbs[:5]


def _alt_values(streams: dict, points: list, fill: bool = True) -> list:
    """原始海拔序列（不平滑）：streams 优先，其次点里的 ele。"""
    ds = streams.get("d") or []
    alts = streams.get("alt") or []
    if ds and len(alts) == len(ds):
        vals = [float(a) for a in alts if a is not None]
        if len(vals) >= 2:
            return _fill_alt_gaps(vals) if fill else vals
    out = []
    for p in (points or []):
        if len(p) > 2 and p[2] is not None:
            try:
                out.append(float(p[2]))
            except (TypeError, ValueError):
                continue
    if not out:
        return []
    return _fill_alt_gaps(out) if fill else out


def _total_gain(alts: list, threshold: float = 1.5) -> float:
    """累计爬升：平滑 + 滞回阈值（和 geo.elevation_gain 同一口径）。

    不能用「每个正向差都加」：缓坡每步只涨 1 米，会被 1.5 米阈值全吃掉。
    """
    vals = [float(a) for a in (alts or [])]
    if len(vals) < 2:
        return 0.0
    sm = geo.smooth(vals, 7)
    gain = 0.0
    ref = sm[0]
    for v in sm[1:]:
        delta = v - ref
        if delta >= threshold:
            gain += delta
            ref = v
        elif delta < -threshold:
            ref = v
    return round(gain, 0)


def build(route: Iterable, streams: dict, *, total_m: float | None = None,
          max_cues: int = MAX_CUES) -> dict:
    """route: [[lat, lon], ...]；streams: {d, t, alt, speed}。"""
    pts = [p for p in (route or []) if p and p[0] is not None and p[1] is not None]
    result = {"ok": False, "total_m": 0, "cues": [], "kms": [], "climbs": [],
              "notes": []}
    if len(pts) < 3:
        result["notes"].append("没有足够的坐标点，无法生成路书。")
        return result

    cum = geo.cumulative_distance([(p[0], p[1]) for p in pts])
    route_total = cum[-1]
    if route_total <= 1:
        result["notes"].append("轨迹长度为 0，无法生成路书。")
        return result

    stream_total = float(streams.get("d")[-1]) if streams.get("d") else route_total

    # 手绘/导入的路书没有 streams，但点里可能带海拔：补一条 (d, alt)，
    # 后面的转向海拔、公里节点、爬坡和剖面就都能用。
    # 累计爬升要拿「补 streams 之前」的原始海拔算，否则会平滑两次、数值被压小
    raw_alts = _alt_values(streams, pts)

    if not (streams.get("alt") or []):
        pts_alt = _alt_from_points(pts, cum)
        if pts_alt:
            streams = dict(streams)
            streams["d"], streams["alt"] = pts_alt
            stream_total = float(pts_alt[0][-1]) if pts_alt[0] else route_total

    scale = (float(total_m) / route_total) if total_m else (stream_total / route_total)
    total = float(total_m) if total_m else stream_total

    # ---------------- 转向提示
    cues: list[dict] = []
    last_d = -1e9
    n = len(pts)
    step = max(1, int(40.0 / max(1e-6, route_total / max(1, n - 1))))
    for i in range(1, n - 1, step):
        d_here = cum[i] * scale
        if d_here - last_d < TURN_MIN_SPACING_M:
            continue
        back = _index_at(cum, max(0.0, cum[i] - LOOK_M))
        fwd = _index_at(cum, min(route_total, cum[i] + LOOK_M))
        if back >= i or fwd <= i:
            continue
        b1 = _bearing_between(pts, cum, back, i)
        b2 = _bearing_between(pts, cum, i, fwd)
        if b1 is None or b2 is None:
            continue
        delta = ((b2 - b1 + 540.0) % 360.0) - 180.0
        if abs(delta) < TURN_MIN_DEG:
            continue
        kind, word = _turn_word(delta)
        if kind == "slight":
            continue
        if d_here < 300 or d_here > total - 300:
            continue                      # 起步/收尾的 GPS 抖动不算路口
        speed = _stream_at(streams, "speed", d_here)
        if speed is not None and speed < 3:
            continue                      # 停下来拍照/等人的位置没有转向意义
        cues.append({
            "d": round(d_here, 0),
            "kind": kind,
            "dir": word,
            "heading": round(b2, 0),
            "delta": round(delta, 0),
            "alt": _stream_at(streams, "alt", d_here),
            "t": _stream_at(streams, "t", d_here),
            "text": f"{word}（方位 {round(b2 / 10) * 10:g}°）",
        })
        last_d = d_here
        if len(cues) >= max_cues:
            break

    # ---------------- 每公里节点
    kms = []
    km = 1000.0
    while km < total:
        kms.append({
            "d": round(km, 0),
            "t": _stream_at(streams, "t", km),
            "alt": _stream_at(streams, "alt", km),
            "speed": _stream_at(streams, "speed", km),
        })
        km += 1000.0

    climbs = _climbs(streams, total)
    profile, alt_min, alt_max = _profile(streams, pts, cum)
    if raw_alts:
        alt_min = round(min(raw_alts), 1)
        alt_max = round(max(raw_alts), 1)

    result.update({
        "ok": True,
        "total_m": round(total, 0),
        "cues": cues,
        "kms": kms,
        "climbs": climbs,
        "profile": profile,
        "alt_min": alt_min,
        "alt_max": alt_max,
        "climb_gain_total": _total_gain(raw_alts),
        "turn_count": len(cues),
        "climb_count": len(climbs),
        "climb_gain": round(sum(c["gain_m"] for c in climbs), 0),
        "descent_count": len([c for c in climbs if c["avg_grade_pct"] < 0]),
    })
    if not cues:
        result["notes"].append("这条路线几乎没有转弯，可能是一段直路或折返。")
    if not climbs:
        result["notes"].append("没有识别到 ≥300 米、平均坡度 ≥3% 的爬坡。")
    if not profile:
        result["notes"].append("这条路线没有海拔数据（手绘点没有高度，导入 GPX 时要带 <ele>）。")
    return result


def to_gpx(name: str, points: Iterable, *, sport: str = "cycling",
           waypoints: Iterable | None = None) -> str:
    """把路书导出成标准 GPX：轨迹 + 途经点（wpt 航点，码表能显示/导航）。

    途经点学行者：名称、功能标签放 <sym>、描述放 <desc>。
    """
    import html

    pts = []
    for p in (points or []):
        if not p or p[0] is None or p[1] is None:
            continue
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            continue
        ele = None
        if len(p) > 2 and p[2] is not None:
            try:
                ele = float(p[2])
            except (TypeError, ValueError):
                ele = None
        pts.append((lat, lon, ele))
    title = html.escape(str(name or "路书"))
    stype = html.escape(str(sport or "cycling"))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="PACE" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <metadata><name>{title}</name></metadata>",
    ]
    for w in (waypoints or []):
        if not isinstance(w, dict):
            continue
        try:
            wlat, wlon = float(w.get("lat")), float(w.get("lon"))
        except (TypeError, ValueError):
            continue
        wname = html.escape(str(w.get("name") or ""))
        wtag = html.escape(str(w.get("tag") or ""))
        wnote = html.escape(str(w.get("note") or ""))
        lines.append(f'  <wpt lat="{wlat:.7f}" lon="{wlon:.7f}">')
        if wname:
            lines.append(f"    <name>{wname}</name>")
        if wtag:
            lines.append(f"    <sym>{wtag}</sym>")
        if wnote:
            lines.append(f"    <desc>{wnote}</desc>")
        lines.append("  </wpt>")
    lines.append(f"  <trk><name>{title}</name><type>{stype}</type><trkseg>")
    for lat, lon, ele in pts:
        if ele is None:
            lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"></trkpt>')
        else:
            lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"><ele>{ele:.1f}</ele></trkpt>')
    lines += ["  </trkseg></trk>", "</gpx>"]
    return "\n".join(lines)


def _seg_dist_m(p, a, b) -> float:
    """点到线段的近似距离（局部等距投影，几十公里内误差可忽略）。"""
    lat0 = math.radians((a[0] + b[0]) / 2.0)
    kx = 111_320.0 * math.cos(lat0)
    ky = 110_540.0
    ax, ay = (a[1]) * kx, a[0] * ky
    bx, by = (b[1]) * kx, b[0] * ky
    px, py = (p[1]) * kx, p[0] * ky
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(points: Iterable, *, tolerance_m: float = 8.0) -> list:
    """按垂直距离抽稀折线（Douglas-Peucker），编辑时用来压点数。"""
    pts = [(float(p[0]), float(p[1])) for p in (points or [])
           if p and p[0] is not None and p[1] is not None]
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        worst, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _seg_dist_m(pts[k], pts[i], pts[j])
            if d > worst:
                worst, idx = d, k
        if idx > 0 and worst > tolerance_m:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(pts, keep) if k]
