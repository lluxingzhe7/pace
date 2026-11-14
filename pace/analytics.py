"""聚合分析与分段（Segment）匹配。

仪表盘的所有统计都在这里从 activities 表计算：
周/月汇总、训练负荷趋势（CTL/ATL/TSB）、运动类型分布、日历热力图、个人最好成绩。

分段匹配采用"端点邻近 + 窗口内路径距离对齐"的启发式算法，
不需要上传原始轨迹给任何第三方。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from . import geo
from .parsers import sport_family, sport_label

# ---------------------------------------------------------------- 时间工具


class TZ:
    """轻量时区包装，只做"本地日"对齐，避免 dateutil 依赖。"""

    def __init__(self, offset_hours: float = 8.0):
        self.offset = float(offset_hours)
        self.tz = timezone(timedelta(hours=self.offset))

    def local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.tz)

    def day_key(self, ts: float) -> str:
        return self.local(ts).strftime("%Y-%m-%d")

    def start_of_day(self, ts: float) -> float:
        d = self.local(ts).replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def start_of_week(self, ts: float) -> float:
        """周一为一周起点。"""
        d = self.local(ts)
        d = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def start_of_month(self, ts: float) -> float:
        d = self.local(ts).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def now(self) -> float:
        return datetime.now(timezone.utc).timestamp()


def _bucket_stats(items: list[dict]) -> dict:
    distance = sum(i["distance_m"] for i in items)
    moving = sum(i["moving_seconds"] for i in items)
    gain = sum(i["elevation_gain_m"] for i in items)
    load = sum(i["training_load"] for i in items)
    return {
        "count": len(items),
        "distance_m": round(distance, 1),
        "moving_seconds": round(moving, 1),
        "elevation_gain_m": round(gain, 1),
        "training_load": round(load, 1),
    }


def _delta(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None if current <= 0 else 100.0
    return round((current - previous) / previous * 100.0, 1)


# ---------------------------------------------------------------- 仪表盘

def dashboard(store, *, days: int = 28, tz_offset: float = 8.0, sport: str | None = None) -> dict:
    tz = TZ(tz_offset)
    now = tz.now()
    today_start = tz.start_of_day(now)
    period_start = today_start - timedelta(days=days - 1).total_seconds()
    prev_start = period_start - timedelta(days=days).total_seconds()

    rows = store.all_activity_summaries(date_from=min(prev_start, period_start) - 86400 * 400)
    if sport and sport != "all":
        key = str(sport).strip().lower()
        if key in ("run", "ride", "swim", "walk", "other"):
            rows = [r for r in rows if sport_family(r["sport"]) == key]
        else:
            rows = [r for r in rows if r["sport"] == sport]

    current = [r for r in rows if period_start <= r["start_time"] <= now]
    previous = [r for r in rows if prev_start <= r["start_time"] < period_start]

    cur_stats = _bucket_stats(current)
    prev_stats = _bucket_stats(previous)

    # 本周（自然周）
    week_start = tz.start_of_week(now)
    this_week = [r for r in rows if r["start_time"] >= week_start]
    week_stats = _bucket_stats(this_week)

    # 月
    month_start = tz.start_of_month(now)
    this_month = [r for r in rows if r["start_time"] >= month_start]
    month_stats = _bucket_stats(this_month)

    # 周桶（最近 12 周，用于柱状图）
    buckets: list[dict] = []
    for w in range(11, -1, -1):
        start = week_start - timedelta(weeks=w).total_seconds()
        end = start + timedelta(weeks=1).total_seconds()
        items = [r for r in rows if start <= r["start_time"] < end]
        stats = _bucket_stats(items)
        stats["label"] = tz.local(start).strftime("%m/%d")
        stats["start"] = start
        buckets.append(stats)

    # 日历热力图（最近 182 天，按本地日聚合）
    heat_days = 182
    heat_start = tz.start_of_day(now) - timedelta(days=heat_days - 1).total_seconds()
    daily: dict[str, dict] = {}
    for r in rows:
        if r["start_time"] < heat_start:
            continue
        key = tz.day_key(r["start_time"])
        node = daily.setdefault(key, {"date": key, "distance_m": 0.0, "moving_seconds": 0.0,
                                      "training_load": 0.0, "count": 0, "sports": {}})
        node["distance_m"] += r["distance_m"]
        node["moving_seconds"] += r["moving_seconds"]
        node["training_load"] += r["training_load"]
        node["count"] += 1
        node["sports"][r["sport"]] = node["sports"].get(r["sport"], 0) + 1

    heatmap = []
    for i in range(heat_days):
        ts = heat_start + i * 86400
        key = tz.day_key(ts)
        node = daily.get(key) or {"date": key, "distance_m": 0.0, "moving_seconds": 0.0,
                                  "training_load": 0.0, "count": 0, "sports": {}}
        heatmap.append({
            "date": key,
            "distance_m": round(node["distance_m"], 1),
            "moving_seconds": round(node["moving_seconds"], 1),
            "training_load": round(node["training_load"], 1),
            "count": node["count"],
        })

    # 运动类型分布（近 90 天）
    since = today_start - timedelta(days=90).total_seconds()
    recent = [r for r in rows if r["start_time"] >= since]
    by_sport: dict[str, dict] = {}
    for r in recent:
        fam = sport_family(r["sport"])
        node = by_sport.setdefault(fam, {"family": fam, "count": 0, "distance_m": 0.0,
                                         "moving_seconds": 0.0, "training_load": 0.0})
        node["count"] += 1
        node["distance_m"] += r["distance_m"]
        node["moving_seconds"] += r["moving_seconds"]
        node["training_load"] += r["training_load"]
    for node in by_sport.values():
        node["distance_m"] = round(node["distance_m"], 1)
        node["moving_seconds"] = round(node["moving_seconds"], 1)
        node["training_load"] = round(node["training_load"], 1)
        node["label"] = {"run": "跑步", "ride": "骑行", "swim": "游泳", "walk": "徒步/步行",
                         "other": "其他"}.get(node["family"], node["family"])

    # 训练负荷趋势
    load_series, fitness = fitness_fatigue(rows, now=now, tz=tz)

    # 近期最佳成绩
    prs = personal_records(store, limit=6)

    # 连续训练天数（streak）：从今天往前数，允许今天还没练
    streak = 0
    cursor = today_start
    keys_with_activity = {tz.day_key(r["start_time"]) for r in rows}
    if tz.day_key(now) not in keys_with_activity:
        cursor -= 86400
    while tz.day_key(cursor) in keys_with_activity:
        streak += 1
        cursor -= 86400

    return {
        "generated_at": now,
        "tz_offset": tz.offset,
        "period_days": days,
        "period": cur_stats,
        "previous": prev_stats,
        "delta": {
            "distance_pct": _delta(cur_stats["distance_m"], prev_stats["distance_m"]),
            "moving_pct": _delta(cur_stats["moving_seconds"], prev_stats["moving_seconds"]),
            "elevation_pct": _delta(cur_stats["elevation_gain_m"], prev_stats["elevation_gain_m"]),
            "load_pct": _delta(cur_stats["training_load"], prev_stats["training_load"]),
        },
        "week": week_stats,
        "month": month_stats,
        "weekly_buckets": buckets,
        "heatmap": heatmap,
        "sport_breakdown": sorted(by_sport.values(), key=lambda x: -x["distance_m"]),
        "load_series": load_series,
        "fitness": fitness,
        "personal_records": prs,
        "streak_days": streak,
        "activity_count": len(rows),
    }


def fitness_fatigue(rows: list[dict], *, now: float, tz: TZ, days: int = 120) -> tuple[list[dict], dict]:
    """训练负荷的三条曲线。

    CTL（体能）= 42 天指数移动平均
    ATL（疲劳）= 7 天指数移动平均
    TSB（状态）= CTL - ATL
    """
    by_day: dict[str, float] = {}
    for r in rows:
        key = tz.day_key(r["start_time"])
        by_day[key] = by_day.get(key, 0.0) + float(r["training_load"] or 0.0)

    series: list[dict] = []
    ctl = atl = 0.0
    start_day = tz.start_of_day(now) - timedelta(days=days - 1).total_seconds()
    for i in range(days):
        ts = start_day + i * 86400
        key = tz.day_key(ts)
        load = by_day.get(key, 0.0)
        ctl = ctl + (load - ctl) * (1 - math.exp(-1 / 42))
        atl = atl + (load - atl) * (1 - math.exp(-1 / 7))
        series.append({
            "date": key,
            "load": round(load, 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(ctl - atl, 1),
        })
    last = series[-1] if series else {"ctl": 0, "atl": 0, "tsb": 0}
    return series, {
        "ctl": last["ctl"],
        "atl": last["atl"],
        "tsb": last["tsb"],
        "status": _training_status(last["tsb"], last["ctl"]),
    }


def _training_status(tsb: float, ctl: float) -> str:
    if ctl < 5:
        return "insufficient"
    if tsb > 15:
        return "fresh"
    if tsb > 5:
        return "race_ready"
    if tsb > -10:
        return "neutral"
    if tsb > -25:
        return "productive"
    return "overreaching"


# ---------------------------------------------------------------- 统计工具


def daily_load_series(rows: list[dict], *, now: float, tz: TZ, days: int = 42) -> list[float]:
    """按本地日返回每日训练负荷，长度 days，最后一项是今天。"""
    by_day: dict[str, float] = {}
    for r in rows:
        key = tz.day_key(r["start_time"])
        by_day[key] = by_day.get(key, 0.0) + float(r["training_load"] or 0.0)
    out = []
    start = tz.start_of_day(now) - timedelta(days=days - 1).total_seconds()
    for i in range(days):
        out.append(by_day.get(tz.day_key(start + i * 86400), 0.0))
    return out


def round_times(rows: list[dict], *, now: float, tz: TZ) -> int:
    """最近 28 天里出现过的训练次数（同一天多次按一次算）。"""
    since = tz.start_of_day(now) - timedelta(days=27).total_seconds()
    return len({tz.day_key(r["start_time"]) for r in rows if r["start_time"] >= since})


def pct_change(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None if current <= 0 else 100.0
    return round((current - previous) / previous * 100.0, 1)


def sport_family_mix(rows: list[dict], *, now: float, tz: TZ, days: int = 56) -> dict:
    """各运动族的时长占比，用于判断训练是否单一。"""
    since = tz.start_of_day(now) - timedelta(days=days - 1).total_seconds()
    buckets: dict[str, float] = {}
    for r in rows:
        if r["start_time"] < since:
            continue
        fam = sport_family(r["sport"])
        buckets[fam] = buckets.get(fam, 0.0) + float(r["moving_seconds"] or 0)
    total = sum(buckets.values()) or 1.0
    return {k: round(v / total * 100, 1) for k, v in buckets.items()}


def load_ratio(rows: list[dict], *, now: float, tz: TZ) -> dict:
    """7 天负荷 / 前 28 天周均负荷（ACWR 变体）。>1.5 通常意味着加量过快。"""
    daily = daily_load_series(rows, now=now, tz=tz, days=28)
    acute = sum(daily[-7:])
    chronic = sum(daily) / 4.0
    ratio = (acute / chronic) if chronic > 0 else None
    return {
        "acute_7d": round(acute, 1),
        "chronic_weekly": round(chronic, 1),
        "ratio": round(ratio, 2) if ratio is not None else None,
    }


def monotony_and_strain(daily: list[float]) -> dict:
    """Foster 单调性 / 训练应变。

    monotony = 14 天日均负荷 / 日均负荷标准差
    strain   = 14 天总负荷 × monotony
    没有波动（每天都练）会让 monotony 偏高，是过度训练的经典信号。
    """
    window = [x for x in daily[-14:]]
    if len(window) < 7:
        return {"monotony": None, "strain": None}
    mean = sum(window) / len(window)
    if mean <= 0:
        return {"monotony": 0.0, "strain": 0.0}
    var = sum((x - mean) ** 2 for x in window) / len(window)
    sd = var ** 0.5
    monotony = (mean / sd) if sd > 0 else 5.0
    return {"monotony": round(monotony, 2), "strain": round(sum(window) * monotony, 1)}

    if ctl < 5:
        return "insufficient"
    if tsb > 15:
        return "fresh"
    if tsb > 5:
        return "race_ready"
    if tsb > -10:
        return "neutral"
    if tsb > -25:
        return "productive"
    return "overreaching"


# ---------------------------------------------------------------- 个人最好成绩

def personal_records(store, *, limit: int = 20) -> list[dict]:
    """跨活动的标准距离最好成绩（每个距离只取最快一次）。"""
    rows = store.conn.execute(
        "SELECT id, sport, start_time, best_efforts, title FROM activities "
        "WHERE best_efforts IS NOT NULL AND best_efforts != '[]' ORDER BY start_time DESC"
    ).fetchall()
    import json
    best: dict[tuple[str, float], dict] = {}
    for row in rows:
        try:
            efforts = json.loads(row["best_efforts"] or "[]")
        except json.JSONDecodeError:
            continue
        family = sport_family(row["sport"])
        for eff in efforts:
            key = (family, float(eff["distance_m"]))
            record = {
                "family": family,
                "sport": row["sport"],
                "sport_label": sport_label(row["sport"]),
                "distance_m": float(eff["distance_m"]),
                "label": eff["label"],
                "seconds": float(eff["seconds"]),
                "pace_seconds": eff.get("pace_seconds"),
                "activity_id": row["id"],
                "title": row["title"],
                "date": row["start_time"],
            }
            prev = best.get(key)
            if prev is None or record["seconds"] < prev["seconds"]:
                best[key] = record
    out = sorted(best.values(), key=lambda r: (r["family"], r["distance_m"]))
    # 同一距离族只保留最靠前的几条给仪表盘
    return out[:limit] if limit else out


# ---------------------------------------------------------------- 活动间对比

def compare_activities(store, ids: list[int], *, tz_offset: float = 8.0) -> dict:
    items = []
    for aid in ids[:6]:
        act = store.get_activity(aid, full=True)
        if act:
            items.append(act)
    if not items:
        raise ValueError("没有找到可对比的活动")

    streams = []
    for act in items:
        st = act.get("streams") or {}
        streams.append({
            "id": act["id"],
            "title": act.get("title"),
            "sport": act["sport"],
            "distance_m": act["distance_m"],
            "moving_seconds": act["moving_seconds"],
            "elevation_gain_m": act["elevation_gain_m"],
            "avg_hr": act["avg_hr"],
            "pace_s_per_km": act["pace_s_per_km"],
            "series": {
                "d": st.get("d", []),
                "t": st.get("t", []),
                "hr": st.get("hr", []),
                "speed": st.get("speed", []),
                "alt": st.get("alt", []),
                "pace": st.get("pace", []),
            },
        })
    return {"activities": items, "series": streams}


# ---------------------------------------------------------------- 分段

def build_segment_from_activity(activity: dict, start_index: int, end_index: int,
                                name: str | None = None) -> dict:
    """从某条活动的点区间创建分段定义。"""
    route = activity.get("route") or []
    streams = activity.get("streams") or {}
    if not route:
        raise ValueError("该活动没有 GPS 轨迹，无法创建分段")
    n = len(route)
    start_index = max(0, min(int(start_index), n - 2))
    end_index = max(start_index + 1, min(int(end_index), n - 1))

    chunk = route[start_index:end_index + 1]
    latlngs = [(p[0], p[1]) for p in chunk]
    cum = geo.cumulative_distance(latlngs)
    distance = cum[-1]
    if distance < 50:
        raise ValueError("分段太短（< 50 米）")

    # 用流数据估算爬升（按距离比例抽样高程）
    alt_series = streams.get("alt") or []
    d_series = streams.get("d") or []
    gains = 0.0
    if alt_series and d_series and len(alt_series) == len(d_series):
        d0 = d_series[0]
        window = [
            (dd, aa) for dd, aa in zip(d_series, alt_series)
            if aa is not None and (cum[0] - 5) <= (dd - d0) <= (distance + 5)
        ]
        if window:
            gains = geo.elevation_gain([a for _, a in window], 1.0, 7)

    grade = (gains / distance * 100.0) if distance > 0 else 0.0
    return {
        "name": name or f"分段 {activity['id']}-{start_index}",
        "sport_family": sport_family(activity["sport"]),
        "distance_m": round(distance, 1),
        "elevation_gain_m": round(gains, 1),
        "avg_grade_pct": round(grade, 1),
        "start_lat": latlngs[0][0], "start_lon": latlngs[0][1],
        "end_lat": latlngs[-1][0], "end_lon": latlngs[-1][1],
        "polyline": geo.encode_polyline(_thin(latlngs, 160)),
        "source_activity_id": activity["id"],
    }


def _thin(points: list[tuple[float, float]], max_points: int) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    idx = geo.resample_indices(len(points), max_points)
    return [points[i] for i in idx]


def match_segment(segment: dict, activity: dict, *, tolerance_m: float = 45.0) -> dict | None:
    """在活动轨迹里寻找与分段起点/终点都吻合、且路径长度接近的窗口。

    复杂度：路线累计距离预计算一次，之后线性扫描 + 邻域二分。
    """
    route = activity.get("route") or []
    streams = activity.get("streams") or {}
    if len(route) < 2:
        return None
    if sport_family(activity["sport"]) != segment.get("sport_family"):
        return None

    s_lat, s_lon = segment.get("start_lat"), segment.get("start_lon")
    e_lat, e_lon = segment.get("end_lat"), segment.get("end_lon")
    if None in (s_lat, s_lon, e_lat, e_lon):
        return None

    target = float(segment.get("distance_m") or 0)
    if target <= 0:
        return None

    # 预计算路线累计距离
    cum_route = geo.cumulative_distance([(p[0], p[1]) for p in route])
    total_route = cum_route[-1]
    if total_route < target * 0.9:
        return None

    n = len(route)
    tol_len = max(60.0, target * 0.12)
    best: dict | None = None
    best_score = 1e18

    for i in range(n - 1):
        if geo.haversine(s_lat, s_lon, route[i][0], route[i][1]) > tolerance_m:
            continue
        # 二分找到累计距离为 cum_route[i] + target 的位置
        ideal = cum_route[i] + target
        j = _lower_bound(cum_route, ideal)
        for candidate in (j - 1, j, j + 1):
            if candidate <= i or candidate >= n:
                continue
            path_len = cum_route[candidate] - cum_route[i]
            if abs(path_len - target) > tol_len:
                continue
            end_dist = geo.haversine(e_lat, e_lon, route[candidate][0], route[candidate][1])
            if end_dist > tolerance_m:
                continue
            score = end_dist + abs(path_len - target) * 0.35 + _direction_penalty(
                route, i, candidate, s_lat, s_lon, e_lat, e_lon)
            if score < best_score:
                best_score = score
                best = {"start_index": i, "end_index": candidate, "distance_m": path_len}
        if best is not None and best["start_index"] == i:
            break

    if not best:
        return None

    # 用 1Hz 流数据把路线序号换算成精确时间
    t_series = streams.get("t") or []
    d_series = streams.get("d") or []
    if not t_series or not d_series:
        return None

    start_offset = _interp_time_from_route(route, cum_route, t_series, d_series, best["start_index"])
    end_offset = _interp_time_from_route(route, cum_route, t_series, d_series, best["end_index"])
    elapsed = end_offset - start_offset
    if elapsed <= 0:
        return None

    hr_series = streams.get("hr") or []
    avg_hr = None
    if hr_series:
        lo = max(0, int(start_offset))
        hi = min(len(hr_series), int(end_offset) + 1)
        window = [h for h in hr_series[lo:hi] if h]
        if window:
            avg_hr = round(sum(window) / len(window), 1)

    best.update({
        "start_offset_s": round(start_offset, 1),
        "elapsed_s": round(elapsed, 1),
        "avg_hr": avg_hr,
        "avg_speed_kmh": round(best["distance_m"] / elapsed * 3.6, 2),
    })
    return best


def _lower_bound(values: list[float], target: float) -> int:
    lo, hi = 0, len(values) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if values[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _interp_time_from_route(route: list, cum_route: list[float], t_series: list[float],
                            d_series: list[float], index: int) -> float:
    """路线点序号 -> 该点累计距离 -> 在时间流中插值出秒数。"""
    if index <= 0:
        return float(t_series[0]) if t_series else 0.0
    if index >= len(cum_route):
        return float(t_series[-1]) if t_series else 0.0
    total = cum_route[-1]
    frac = cum_route[index] / total if total > 0 else 0.0
    target_d = frac * d_series[-1]
    return _interp_stream(t_series, d_series, target_d)


def _interp_stream(times: list[float], dists: list[float], target_d: float) -> float:
    if not times or not dists:
        return 0.0
    if target_d <= dists[0]:
        return float(times[0])
    if target_d >= dists[-1]:
        return float(times[-1])
    lo, hi = 0, len(dists) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if dists[mid] <= target_d:
            lo = mid
        else:
            hi = mid
    span = dists[hi] - dists[lo]
    frac = (target_d - dists[lo]) / span if span else 0.0
    return float(times[lo] + (times[hi] - times[lo]) * frac)


def _direction_penalty(route, i, j, s_lat, s_lon, e_lat, e_lon) -> float:
    """活动窗口方向与分段起终点方向差太多则加分（惩罚）。"""
    seg_bearing = geo.bearing(s_lat, s_lon, e_lat, e_lon)
    act_bearing = geo.bearing(route[i][0], route[i][1], route[j][0], route[j][1])
    diff = abs((seg_bearing - act_bearing + 180) % 360 - 180)
    return diff * 0.5


def rebuild_all_efforts(store, sport_family_filter: str | None = None) -> int:
    """把所有已有活动与所有分段重新匹配，返回新增/更新条数。"""
    segments = store.list_segments()
    if not segments:
        return 0
    acts = store.conn.execute(
        "SELECT id, sport FROM activities WHERE has_gps=1"
    ).fetchall()
    matched = 0
    for seg in segments:
        seg_full = store.get_segment(seg["id"])
        if not seg_full:
            continue
        for row in acts:
            if sport_family_filter and sport_family(row["sport"]) != sport_family_filter:
                continue
            if sport_family(row["sport"]) != seg_full.get("sport_family"):
                continue
            activity = store.get_activity(row["id"], full=True)
            if not activity:
                continue
            effort = match_segment(seg_full, activity)
            if effort:
                store.add_effort(seg["id"], activity["id"], effort)
                matched += 1
    store.refresh_effort_bests()
    return matched


def auto_suggest_segments(store, *, min_repeats: int = 3, tolerance_m: float = 120.0) -> list[dict]:
    """找出反复经过的起终点组合，建议创建分段。

    对每条有 GPS 的活动取若干等分锚点，按 (起点, 终点) 聚类，
    出现次数 >= min_repeats 的组合即为候选。
    """
    acts = store.conn.execute(
        "SELECT id, sport FROM activities WHERE has_gps=1 ORDER BY start_time DESC LIMIT 300"
    ).fetchall()
    buckets: dict[tuple, list] = {}
    for row in acts:
        act = store.get_activity(row["id"], full=True)
        route = (act or {}).get("route") or []
        if len(route) < 40:
            continue
        fam = sport_family(row["sport"])
        n = len(route)
        # 取 25% / 50% / 75% / 100% 作为候选终点
        for end_frac in (0.25, 0.5, 0.75, 1.0):
            end_idx = min(n - 1, int(n * end_frac))
            end = route[end_idx]
            for start_frac in (0.0, 0.25, 0.5):
                start_idx = int(n * start_frac)
                if start_idx >= end_idx - 5:
                    continue
                start = route[start_idx]
                key = (fam, round(start[0], 2), round(start[1], 2),
                       round(end[0], 2), round(end[1], 2))
                buckets.setdefault(key, []).append({"activity_id": row["id"], "sport": row["sport"],
                                                    "start_idx": start_idx, "end_idx": end_idx,
                                                    "start": start, "end": end})
    suggestions = []
    for key, items in buckets.items():
        if len(items) < min_repeats:
            continue
        first = items[0]
        suggestions.append({
            "family": key[0],
            "start": first["start"],
            "end": first["end"],
            "count": len(items),
            "activity_ids": [i["activity_id"] for i in items][:20],
            "suggested_name": f"{sport_label(first['sport'])}路线 {len(items)} 次",
        })
    suggestions.sort(key=lambda s: -s["count"])
    return suggestions[:12]
