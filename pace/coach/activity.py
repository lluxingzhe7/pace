"""单次活动的专业分析报告。

参考佳明"活动详情"的信息结构（执行评分、分段、效率、训练效果、数据质量），
但所有指标都由本项目的原始流数据现算，不做黑盒打分。

诚实原则
--------
* 没有心率 → 不编"有氧效率"，直接标注该维度缺失
* 活动太短（< 20 分钟）→ 不算心率漂移，因为前 10 分钟本来就在爬升
* 没有功率 → 不算 NP/FTP 相关结论，只给心率与配速口径
* 数据可疑（GPS 跳点、心率尖刺）→ 单独列出，并降低整体置信度
"""
from __future__ import annotations

import math

from .. import geo
from ..parsers import sport_family, sport_label

SPLIT_UNIT = {"run": 1000.0, "walk": 1000.0, "ride": 5000.0, "swim": 100.0, "other": 1000.0}
GRADE_BANDS = [(88, "A"), (76, "B"), (62, "C"), (48, "D"), (0, "E")]


def _pace_seconds(elapsed: float, distance_m: float, family: str) -> float | None:
    if elapsed <= 0 or distance_m <= 0:
        return None
    if family in ("run", "walk"):
        return elapsed / (distance_m / 1000.0)
    if family == "swim":
        return elapsed / (distance_m / 100.0)
    return None


def _fmt_pace(seconds: float | None, family: str) -> str:
    if not seconds or seconds <= 0:
        return "—"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _grade(score: float) -> str:
    for threshold, letter in GRADE_BANDS:
        if score >= threshold:
            return letter
    return "E"


# ---------------------------------------------------------------- 专项指标
#
# 口径对齐常见专业软件（WKO / TrainingPeaks / 佳明 Connect / Strava）：
#   骑行 NP/IF/TSS/VI、功率区间；跑步配速稳定性、坡度修正配速(GAP)、步频；
#   游泳 配速/100m、划频、SWOLF。取不到数据就标 ok=False，绝不填假数。

POWER_ZONES = [
    ("z1", "Z1 恢复", 0.00, 0.55),
    ("z2", "Z2 耐力", 0.55, 0.75),
    ("z3", "Z3 节奏", 0.75, 0.90),
    ("z4", "Z4 阈值", 0.90, 1.05),
    ("z5", "Z5 VO2max", 1.05, 1.20),
    ("z6", "Z6 无氧", 1.20, 1.50),
    ("z7", "Z7 神经", 1.50, 99.0),
]


def _norm_power(pwr: list[float], sample_dt: float = 1.0) -> float | None:
    """标准化功率：30 秒滑动均值后取四次方均根（Coggan 口径）。"""
    values = [max(0.0, float(v)) for v in pwr if v is not None]
    if len(values) < 60:
        return None
    window = max(1, int(round(30.0 / max(0.01, sample_dt))))
    if window > len(values):
        return None
    run = sum(values[:window])
    acc = 0.0
    count = 0
    for i in range(window, len(values) + 1):
        if i > window:
            run += values[i - 1] - values[i - window - 1]
        acc += (run / window) ** 4
        count += 1
    return (acc / count) ** 0.25


def _power_zones(pwr: list[float], ftp: float, sample_dt: float = 1.0) -> dict:
    zones = {k: 0.0 for k, _l, _lo, _hi in POWER_ZONES}
    if not ftp:
        return {}
    for v in pwr:
        if v is None:
            continue
        ratio = float(v) / ftp
        for key, _label, lo, hi in POWER_ZONES:
            if lo <= ratio < hi:
                zones[key] += sample_dt
                break
    return {k: round(v, 1) for k, v in zones.items()}


def _cv(values: list[float]) -> float | None:
    """变异系数（%）：衡量配速/功率有多稳。"""
    vals = [v for v in values if v]
    if len(vals) < 3:
        return None
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return None
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return round(math.sqrt(var) / mean * 100, 1)


def _item(key, label, value, unit="", hint="", ok=True) -> dict:
    return {"key": key, "label": label, "value": value, "unit": unit, "hint": hint, "ok": ok}


def _mean_cad(streams: dict) -> float | None:
    """平均踏频/划频（次/分）。骑行与游泳都用 cad 流。"""
    cad = [float(v) for v in (streams.get("cad") or []) if v]
    if len(cad) < 20:
        return None
    return round(sum(cad) / len(cad), 1)


def specialty(activity: dict, streams: dict, family: str, splits: list[dict],
              settings: dict | None = None) -> dict:
    """按运动类型给出专项指标。缺数据时 ok=False，由前端明确显示「无数据」。"""
    settings = settings or {}
    ftp = float(settings.get("ftp") or 200)
    threshold_pace = float(settings.get("threshold_pace_s") or 300)
    moving = float(activity.get("moving_seconds") or 0)
    elapsed = float(activity.get("elapsed_seconds") or 0)
    dist = float(activity.get("distance_m") or 0)
    gain = float(activity.get("elevation_gain_m") or 0)
    avg_hr = activity.get("avg_hr")
    pwr = streams.get("pwr") or []
    t = streams.get("t") or []
    sample_dt = (t[1] - t[0]) if len(t) > 1 else 1.0
    moving_pct = round(moving / elapsed * 100, 1) if elapsed > 0 else None
    vam = round(gain / (moving / 3600.0), 1) if moving > 600 and gain > 20 else None

    items = []
    zones = None

    if family == "ride":
        speeds = [s.get("speed_kmh") for s in splits if s.get("speed_kmh")]
        cruise = sorted(speeds)[len(speeds) // 2] if len(speeds) >= 2 else None
        items.append(_item("avg_speed", "平均速度", round(activity.get("avg_speed_kmh") or 0, 1), "km/h"))
        items.append(_item("max_speed", "最大速度", round(activity.get("max_speed_kmh") or 0, 1), "km/h"))
        items.append(_item("cruise", "巡航速度", round(cruise, 1) if cruise else "—", "km/h",
                           "取各 5 km 分段速度的中位数", cruise is not None))
        np_w = _norm_power(pwr, sample_dt) if pwr else None
        if np_w:
            intensity = np_w / ftp
            tss = (intensity ** 2) * (moving / 3600.0) * 100
            avg_p = activity.get("avg_power") or (sum(pwr) / len(pwr) if pwr else None)
            items.append(_item("np", "标准化功率 NP", round(np_w), "W", "30 秒滑窗四次方均根"))
            items.append(_item("if", "强度系数 IF", round(intensity, 2), "", f"NP ÷ FTP（FTP {ftp:.0f} W）"))
            items.append(_item("tss", "训练压力 TSS", round(tss), "", "IF² × 时长，100 ≈ 阈值骑 1 小时"))
            items.append(_item("vi", "变异性指数 VI", round(np_w / avg_p, 2) if avg_p else "—", "",
                               "1.0 左右说明功率平稳；>1.15 起伏大", bool(avg_p)))
            zones = _power_zones(pwr, ftp, sample_dt)
        else:
            for key, label, hint in (
                ("np", "标准化功率 NP", "需要功率计或骑行台"),
                ("if", "强度系数 IF", "需要功率数据与 FTP"),
                ("tss", "训练压力 TSS", "需要功率数据"),
                ("vi", "变异性指数 VI", "需要功率数据"),
            ):
                items.append(_item(key, label, "—", "", hint, False))
        if avg_hr and activity.get("avg_power"):
            items.append(_item("pwhr", "功率心率比", round(activity["avg_power"] / avg_hr, 2), "W/bpm",
                               "同等心率下功率越高，有氧能力越好"))
        else:
            items.append(_item("pwhr", "功率心率比", "—", "", "需要功率与心率同时存在", False))
        items.append(_item("vam", "爬升率 VAM", vam if vam else "—", "m/h",
                           "每小时累计爬升，爬坡能力的粗指标", vam is not None))
        items.append(_item("speed_cv", "速度变异", _cv(speeds) if speeds else "—", "%",
                           "分段速度的变异系数，越小越稳", bool(speeds)))
        cad_r = _mean_cad(streams)
        items.append(_item("cadence", "平均踏频", cad_r if cad_r else "—", "rpm",
                           "平路常用 80–95；过低易伤膝，过高影响踩踏效率", bool(cad_r)))

    elif family in ("run", "walk"):
        paces = [s.get("pace_s") for s in splits if s.get("pace_s")]
        best = min(paces) if paces else None
        worst = max(paces) if paces else None
        items.append(_item("pace", "平均配速", _fmt_pace(_pace_seconds(moving, dist, family), family) or "—", "/km"))
        items.append(_item("best_km", "最快分段", _fmt_pace(best, family) if best else "—", "/km",
                           "分段最快的一公里", best is not None))
        items.append(_item("worst_km", "最慢分段", _fmt_pace(worst, family) if worst else "—", "/km",
                           "分段最慢的一公里", worst is not None))
        items.append(_item("pace_cv", "配速变异", _cv(paces) if paces else "—", "%",
                           "越小说明配速越稳；比赛通常 <5%", bool(paces)))
        if gain > 20 and dist > 1000:
            # 经验换算：每爬升 1 m 约等于多跑 8 m 平路
            gap = moving / ((dist + 8 * gain) / 1000.0)
            items.append(_item("gap", "坡度修正配速 GAP", _fmt_pace(gap, family), "/km",
                               "按每 1 m 爬升折算 8 m 平路，用于比较起伏路线"))
        else:
            items.append(_item("gap", "坡度修正配速 GAP", "—", "/km", "路线几乎无爬升，无需修正", False))
        cad = _cadence(streams, family)
        items.append(_item("cadence", "平均步频", round(cad["avg"]) if cad else "—", "spm",
                           "跑步常用 170–180；过低常见于疲劳或步幅过大", bool(cad)))
        if avg_hr and dist > 0 and moving > 0:
            items.append(_item("eff", "每搏距离", round(dist / (avg_hr * moving / 60.0), 2), "m/beat",
                               "速度 ÷ 心率，同心率下越高越省力"))
        else:
            items.append(_item("eff", "每搏距离", "—", "m/beat", "需要心率数据", False))
        items.append(_item("vam", "爬升率 VAM", vam if vam else "—", "m/h",
                           "每小时累计爬升", vam is not None))

    elif family == "swim":
        pace100 = _pace_seconds(moving, dist, "swim")
        items.append(_item("pace100", "配速 /100m", _fmt_pace(pace100, "swim") if pace100 else "—", "/100m"))
        lengths = dist / 25.0
        items.append(_item("lengths", "折合趟数", round(lengths), "趟（25 m）",
                           "按 25 米池折算，便于核对训练量"))
        cad = _mean_cad(streams)
        items.append(_item("stroke_rate", "划频", cad if cad else "—", "次/分",
                           "需要手表记录划水次数", bool(cad)))
        if pace100 and cad:
            # SWOLF(25m) = 25m 用时 + 每 25m 划水次数
            sec25 = pace100 / 4.0
            strokes25 = cad / 60.0 * sec25
            items.append(_item("swolf", "SWOLF（估算）", round(sec25 + strokes25), "",
                               "25 m 用时 + 划水次数，越小越省力"))
        else:
            items.append(_item("swolf", "SWOLF（估算）", "—", "", "需要配速与划频同时存在", False))
        items.append(_item("vam", "每小时距离", round(dist / (moving / 3600.0)) if moving else "—", "m/h"))

    else:
        items.append(_item("note", "运动类型", "未归类", "", "在活动详情里改成骑行/跑步/游泳可得到专项指标", False))

    items.append(_item("moving_pct", "移动时间占比", moving_pct if moving_pct is not None else "—", "%",
                       "总时长里有氧骑/跑的比例，越低说明停得越多", moving_pct is not None))

    return {"family": family, "items": items, "power_zones": zones,
            "ftp": ftp if family == "ride" else None,
            "threshold_pace_s": threshold_pace if family in ("run", "walk") else None}


# ---------------------------------------------------------------- 分段

def compute_splits(activity: dict) -> list[dict]:
    """按标准距离切分，返回每段用时、配速/速度、心率、爬升、步频。"""
    streams = activity.get("streams") or {}
    d = streams.get("d") or []
    t = streams.get("t") or []
    if len(d) < 3 or len(t) < 3:
        return []
    family = sport_family(activity.get("sport") or "generic")
    unit = SPLIT_UNIT.get(family, 1000.0)
    total = d[-1]
    if total <= 0:
        return []

    hr = streams.get("hr") or []
    alt = streams.get("alt") or []
    cad = streams.get("cad") or []

    splits = []
    index = 0
    start_d, start_t = 0.0, 0.0
    while start_d < total - unit * 0.4:
        target = min(start_d + unit, total)
        # 二分定位 target 距离对应的索引
        lo, hi = index, len(d) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if d[mid] < target:
                lo = mid + 1
            else:
                hi = mid
        end_idx = lo
        span_d = max(1e-6, d[end_idx] - d[index])
        ratio = (target - d[index]) / span_d
        end_t = t[index] + (t[end_idx] - t[index]) * max(0.0, min(1.0, ratio))
        elapsed = max(0.0, end_t - start_t)
        seg_hr = [hr[k] for k in range(index, end_idx + 1) if k < len(hr) and hr[k]]
        seg_alt = [alt[k] for k in range(index, end_idx + 1) if k < len(alt) and alt[k] is not None]
        seg_cad = [cad[k] for k in range(index, end_idx + 1) if k < len(cad) and cad[k]]
        gain = geo.elevation_gain(seg_alt, 1.0, 5) if len(seg_alt) >= 3 else 0.0
        speed = (target - start_d) / elapsed * 3.6 if elapsed > 0 else 0.0
        splits.append({
            "index": len(splits) + 1,
            "distance_m": round(target - start_d, 1),
            "cumulative_m": round(target, 1),
            "elapsed_s": round(elapsed, 1),
            "pace_s": round(_pace_seconds(elapsed, target - start_d, family) or 0, 1) or None,
            "speed_kmh": round(speed, 2),
            "avg_hr": round(sum(seg_hr) / len(seg_hr), 1) if seg_hr else None,
            "max_hr": round(max(seg_hr), 1) if seg_hr else None,
            "gain_m": round(gain, 1),
            "cadence": round(sum(seg_cad) / len(seg_cad), 1) if seg_cad else None,
            "is_partial": (target - start_d) < unit * 0.9,
        })
        index = end_idx
        start_d, start_t = target, end_t
        if index >= len(d) - 1:
            break
    return splits


# ---------------------------------------------------------------- 效率

def aerobic_decoupling(streams: dict, split_at: float) -> dict | None:
    """心率漂移（有氧解耦）。

    指标：心脏成本 = 心率 × 每米耗时（bpm·秒/米）。
    同样每米耗时下心率越高，成本越大；成本上涨 = 有氧解耦。

    做法：按固定距离（总长的 1/80）重采样，比较前后半程的**中位数**。
    用中位数而不是均值：瞬时心率波动大，均值会被个别点带偏。
    用距离采样而不是时间采样：避免心率延迟造成的假漂移。

    正值 = 后半程成本更高（疲劳 / 有氧耐力不足）
    负值 = 后半程更经济（状态好或前程保守）
    """
    d = streams.get("d") or []
    t = streams.get("t") or []
    hr = streams.get("hr") or []
    n = min(len(d), len(t), len(hr))
    if n < 60 or split_at <= 200:
        return None
    valid = sum(1 for h in hr[:n] if h and h >= 60)
    if valid / n < 0.6:
        return None

    samples = 80
    step = split_at / samples
    costs: list[tuple[float, float]] = []   # (距离, 心脏成本)
    idx = 0
    last_t = t[0]
    for k in range(1, samples):
        target = k * step
        while idx < n - 1 and d[idx + 1] < target:
            idx += 1
        if idx >= n - 1:
            break
        span = d[idx + 1] - d[idx]
        frac = (target - d[idx]) / span if span > 0 else 0.0
        frac = max(0.0, min(1.0, frac))
        time_at = t[idx] + (t[idx + 1] - t[idx]) * frac
        # 心率也做线性插值，减少采样噪声
        h0, h1 = hr[idx], hr[idx + 1]
        if h0 and h1:
            h = h0 + (h1 - h0) * frac
        else:
            h = h0 or h1
        dsec = time_at - last_t
        if h and h >= 60 and dsec > 0 and step > 0:
            costs.append((target, (dsec / step) * h))
        last_t = time_at

    if len(costs) < 20:
        return None
    mid = split_at / 2.0
    first = sorted(c for dd, c in costs if dd <= mid)
    second = sorted(c for dd, c in costs if dd > mid)
    if len(first) < 8 or len(second) < 8:
        return None

    def median(values: list[float]) -> float:
        m = len(values) // 2
        return values[m] if len(values) % 2 else (values[m - 1] + values[m]) / 2

    a, b = median(first), median(second)
    if a <= 0:
        return None
    change = (b - a) / a * 100.0
    return {
        "first_half": round(a, 1),
        "second_half": round(b, 1),
        "change_pct": round(change, 1),
        "unit": "bpm·s/m",
        "verdict": _decoupling_verdict(change),
    }


def _decoupling_verdict(change: float) -> dict:
    """把漂移百分比翻译成人话。超出 ±20% 视为数据问题而非生理结论。"""
    if abs(change) > 20:
        return {
            "tone": "warn",
            "label": "数据异常",
            "detail": f"心脏成本变化 {change:+.1f}%，超出正常生理范围（±20%）。"
                      "通常是心率带接触不良、数据来自不同设备，或这是合成/演示数据。"
                      "不要据此判断有氧能力。",
        }
    if change <= -3:
        return {"tone": "good", "label": "后程更经济",
                "detail": "同样配速下后半程心率更低，说明状态好或前程保守。"}
    if change < 3:
        return {"tone": "good", "label": "成本稳定",
                "detail": "前后半程「心率×配速」基本一致，有氧控制良好。"}
    if change < 8:
        return {"tone": "fair", "label": "轻微漂移",
                "detail": "后半程成本小幅上升，属于正常疲劳，注意补给与节奏。"}
    return {"tone": "warn", "label": "明显漂移",
            "detail": "同样配速下后半程心率明显更高，提示有氧耐力不足或前程偏快。"}


def zone_times(streams: dict, hr_max: float, hr_rest: float) -> dict:
    """从逐点心率流统计精确的区间时间（优于用活动均值近似）。"""
    t = streams.get("t") or []
    hr = streams.get("hr") or []
    out = {"z1": 0.0, "z2": 0.0, "z3": 0.0, "z4": 0.0, "z5": 0.0}
    if len(t) < 3 or len(hr) < 3:
        return out
    reserve = max(1.0, hr_max - hr_rest)
    for i in range(len(t) - 1):
        h = hr[i]
        if not h:
            continue
        dt = max(0.0, min(30.0, t[i + 1] - t[i]))
        pct = (h - hr_rest) / reserve
        key = "z1" if pct < 0.60 else "z2" if pct < 0.70 else "z3" if pct < 0.80 \
            else "z4" if pct < 0.90 else "z5"
        out[key] += dt
    return {k: round(v, 1) for k, v in out.items()}


# ---------------------------------------------------------------- 训练效果

def training_effect(activity: dict) -> dict:
    """用 TRIMP 与区间分布估算"有氧/无氧训练效果"（1.0–5.0）。

    这是公开的 TRIMP 模型映射，不是 Garmin 的 Firstbeat 算法，
    结论只用于同一运动员的纵向对比，不可跨人比较。
    """
    load = float(activity.get("training_load") or 0)
    zones = activity.get("_zone_times") or {}
    total = sum(zones.values()) or 0
    high = (zones.get("z4", 0) + zones.get("z5", 0)) / total if total else 0
    # 有氧：TRIMP 对数映射
    aerobic = 0.0
    if load > 0:
        aerobic = min(5.0, 1.0 + math.log10(1 + load / 20.0) * 3.2)
    # 无氧：高强度时间占比
    anaerobic = min(5.0, 0.6 + high * 8.0) if total else 0.0
    label = "无训练效果"
    if aerobic >= 4.0:
        label = "显著提升有氧能力"
    elif aerobic >= 3.0:
        label = "保持并小幅提升"
    elif aerobic >= 2.0:
        label = "维持有氧基础"
    elif aerobic > 0:
        label = "恢复性刺激"
    return {
        "aerobic": round(aerobic, 1),
        "anaerobic": round(anaerobic, 1) if anaerobic else None,
        "label": label,
        "basis": "TRIMP + 心率区间占比（非 Firstbeat）",
    }


# ---------------------------------------------------------------- 数据质量

def data_quality(activity: dict) -> dict:
    """列出可疑数据，避免用户拿脏数据下结论。"""
    streams = activity.get("streams") or {}
    issues: list[dict] = []
    d = streams.get("d") or []
    hr = streams.get("hr") or []
    speed = streams.get("speed") or []
    n = max(len(d), len(hr), len(speed))

    if n == 0:
        issues.append({"key": "no_streams", "level": "high",
                       "text": "没有逐点数据，只能给汇总指标。"})
        return {"issues": issues, "score": 0, "level": "low"}

    hr_missing = sum(1 for v in hr if not v) / len(hr) if hr else 1.0
    if hr and hr_missing > 0.3:
        issues.append({"key": "hr_gaps", "level": "medium",
                       "text": f"{hr_missing * 100:.0f}% 的时间点没有心率，区间与漂移结果不可靠。"})

    if speed:
        vals = [v for v in speed if v is not None]
        if vals:
            avg = sum(vals) / len(vals)
            spikes = sum(1 for v in vals if v > max(45.0, avg * 2.2))
            if spikes / len(vals) > 0.01:
                issues.append({"key": "speed_spikes", "level": "medium",
                               "text": f"{spikes} 个速度尖刺（>{max(45.0, avg * 2.2):.0f} km/h），可能是 GPS 跳点。"})

    if hr:
        jumps = sum(1 for i in range(1, len(hr))
                    if hr[i] and hr[i - 1] and abs(hr[i] - hr[i - 1]) > 25)
        if jumps > max(3, len(hr) * 0.01):
            issues.append({"key": "hr_jumps", "level": "medium",
                           "text": f"{jumps} 次心率突变（>25 bpm/秒），可能佩戴松动或数据异常。"})

    if d and len(d) > 2:
        covered = d[-1]
        reported = activity.get("source_distance_m") or 0
        if reported and abs(covered - reported) / reported > 0.08:
            issues.append({"key": "distance_mismatch", "level": "low",
                           "text": f"轨迹累计 {covered / 1000:.2f} km 与设备记录 "
                                   f"{reported / 1000:.2f} km 相差超过 8%。"})

    penalty = sum(30 if i["level"] == "high" else 14 if i["level"] == "medium" else 6
                  for i in issues)
    score = max(0, 100 - penalty)
    level = "high" if score >= 85 else "medium" if score >= 60 else "low"
    return {"issues": issues, "score": score, "level": level}


# ---------------------------------------------------------------- 主报告

def build(activity: dict, settings: dict | None = None) -> dict:
    """生成单次活动报告。activity 需包含 streams / route / 汇总字段。"""
    settings = settings or {}
    hr_max = float(settings.get("hr_max") or 190)
    hr_rest = float(settings.get("hr_rest") or 55)
    family = sport_family(activity.get("sport") or "generic")
    streams = activity.get("streams") or {}

    splits = compute_splits(activity)
    zones = zone_times(streams, hr_max, hr_rest)
    activity_for_te = dict(activity)
    activity_for_te["_zone_times"] = zones
    te = training_effect(activity_for_te)
    quality = data_quality(activity)

    total = float(activity.get("distance_m") or 0)
    moving = float(activity.get("moving_seconds") or 0)
    decoupling = None
    if moving >= 1200 and total > 0:
        decoupling = aerobic_decoupling(streams, total)

    # ---- 执行分析：前后半程
    execution = _execution(activity, splits, family)
    # ---- 配速/速度稳定性
    consistency = _consistency(splits)
    # ---- 步频（仅跑步）
    cadence = _cadence(streams, family)
    # ---- 爬坡：优先用导入时算好的；合并过的活动那份会过期，就用流数据重算
    climbs = activity.get("climbs") or []
    if not climbs and family in ("ride", "run") and streams.get("d") and streams.get("alt"):
        try:
            from ..routebook import _climbs
            climbs = _climbs(streams, total)
        except Exception:
            climbs = []

    caps = {
        "endurance": 0.0, "speed": 0.0, "execution": 0.0,
        "data": float(quality["score"]), "load": 0.0,
    }
    caps["load"] = min(100.0, float(activity.get("training_load") or 0) / 1.8)
    caps["execution"] = execution.get("score") or 50
    if decoupling and abs(decoupling["change_pct"]) <= 20:
        caps["endurance"] = max(0.0, min(100.0, 100 - max(0.0, decoupling["change_pct"]) * 8))
    elif family in ("run", "ride") and moving >= 1200:
        # 数据异常或没有心率：不给虚假的高分，按"无法判断"记 45
        caps["endurance"] = 45.0
    caps["speed"] = _speed_capacity(activity, consistency)
    overall = round(sum(caps.values()) / len(caps), 1)
    grade = _grade(overall)

    return {
        "activity_id": activity.get("id"),
        "sport": activity.get("sport"),
        "sport_label": sport_label(activity.get("sport") or "generic"),
        "family": family,
        "grade": grade,
        "score": overall,
        "caps": {k: round(v, 1) for k, v in caps.items()},
        "verdict": _verdict(activity, overall, grade, quality, decoupling, execution, family),
        "splits": splits,
        "split_unit_m": SPLIT_UNIT.get(family, 1000.0),
        "zones": zones,
        "specialty": specialty(activity, streams, family, splits, settings),
        "training_effect": te,
        "decoupling": decoupling,
        "execution": execution,
        "consistency": consistency,
        "cadence": cadence,
        "climbs": climbs[:5],
        "data_quality": quality,
        "missing": _missing(activity, family),
    }


def _execution(activity: dict, splits: list[dict], family: str) -> dict:
    if len(splits) < 2:
        return {"score": 50, "label": "分段不足", "detail": "活动太短，无法做前后半程对比。"}
    half = len(splits) // 2
    first = splits[:half]
    second = splits[half:]
    key = "speed_kmh" if family in ("ride", "other") else "pace_s"
    if family in ("ride", "other"):
        a = sum(s["speed_kmh"] for s in first) / len(first)
        b = sum(s["speed_kmh"] for s in second) / len(second)
        change = (b - a) / a * 100 if a else 0
        faster_later = change > 1
    else:
        a = sum(s["pace_s"] or 0 for s in first) / len(first)
        b = sum(s["pace_s"] or 0 for s in second) / len(second)
        change = (a - b) / a * 100 if a else 0     # 正 = 后半程更快
        faster_later = change > 1

    if change > 3:
        label, tone = "后程加速", "good"
        detail = f"后半程比前半程快 {change:.1f}%，配速分配克制，是比赛能力好的信号。"
        score = 88
    elif change > -2:
        label, tone = "匀速", "good"
        detail = f"前后半程差 {abs(change):.1f}%，配速控制稳定。"
        score = 80
    elif change > -6:
        label, tone = "轻微掉速", "fair"
        detail = f"后半程慢 {abs(change):.1f}%，属于正常疲劳范围。"
        score = 65
    else:
        label, tone = "明显掉速", "warn"
        detail = f"后半程慢 {abs(change):.1f}%，通常是前程偏快或耐力不足。"
        score = 45
    return {"score": score, "label": label, "tone": tone, "detail": detail,
            "change_pct": round(change, 1), "faster_later": faster_later}


def _consistency(splits: list[dict]) -> dict:
    paces = [s["pace_s"] for s in splits if s.get("pace_s") and not s.get("is_partial")]
    speeds = [s["speed_kmh"] for s in splits if s.get("speed_kmh") and not s.get("is_partial")]
    values = paces or speeds
    if len(values) < 3:
        return {"cv": None, "label": "—", "best": None, "worst": None}
    mean = sum(values) / len(values)
    sd = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    cv = sd / mean if mean else 0
    if cv < 0.04:
        label = "非常稳定"
    elif cv < 0.08:
        label = "较稳定"
    elif cv < 0.15:
        label = "有波动"
    else:
        label = "波动很大"
    order = values
    best = min(order) if paces else max(order)
    worst = max(order) if paces else min(order)
    return {"cv": round(cv * 100, 1), "label": label, "best": best, "worst": worst}


def _cadence(streams: dict, family: str) -> dict | None:
    cad = [v for v in (streams.get("cad") or []) if v]
    if family != "run" or len(cad) < 30:
        return None
    avg = sum(cad) / len(cad)
    verdict = ("偏慢，可能存在跨步过大" if avg < 160 else
               "正常范围" if avg < 178 else "偏快，注意经济性")
    return {"avg": round(avg, 1), "min": round(min(cad), 1), "max": round(max(cad), 1),
            "verdict": verdict}


def _speed_capacity(activity: dict, consistency: dict) -> float:
    """速度能力：由最佳成绩段（best_efforts）与整体稳定性粗估。"""
    best = activity.get("best_efforts") or []
    score = 50.0
    if best:
        score += min(25.0, len(best) * 4)
    cv = consistency.get("cv")
    if cv is not None:
        score += max(-15.0, min(15.0, (10 - cv) * 1.2))
    return max(0.0, min(100.0, score))


def _missing(activity: dict, family: str) -> list[str]:
    out = []
    streams = activity.get("streams") or {}
    hr = [v for v in (streams.get("hr") or []) if v]
    if not hr:
        out.append("心率流")
    if family in ("run", "ride"):
        cad = [v for v in (streams.get("cad") or []) if v]
        if not cad:
            out.append("步频/踏频流")
    if family in ("ride", "other") and not activity.get("avg_power"):
        out.append("功率")
    if not activity.get("has_gps"):
        out.append("GPS 轨迹")
    return out


def _verdict(activity, score, grade, quality, decoupling, execution, family) -> dict:
    """一句话结论：先讲最重要的事实，再讲下一步。"""
    name = sport_label(activity.get("sport") or "generic")
    total_km = (activity.get("distance_m") or 0) / 1000
    moving = float(activity.get("moving_seconds") or 0)
    streams = activity.get("streams") or {}

    # 只有汇总数字：先说清楚"做不了过程分析"，别用质量分去误导
    if not streams.get("t") or activity.get("point_count", 0) == 0:
        return {
            "tone": "insufficient",
            "headline": "只有汇总数字，做不了过程分析",
            "body": f"{name} {total_km:.2f} km / {int(moving // 60)} 分钟。"
                    "这条活动没有逐点数据（CSV 或手工记录），距离和时长可信，"
                    "但配速漂移、区间分布、心率效率这些都没法算。",
        }
    if quality["level"] == "low":
        return {
            "tone": "warning",
            "headline": "这份数据不够干净，先别当结论",
            "body": quality["issues"][0]["text"] if quality["issues"] else "数据质量偏低。",
        }

    # 没有心率：明确说"耐力维度无法判断"
    has_hr = any(streams.get("hr") or [])
    if not has_hr:
        return {
            "tone": "fair",
            "headline": "没有心率，这堂课只能看配速",
            "body": f"{name} {total_km:.2f} km / {int(moving // 60)} 分钟。"
                    f"{execution['label']}：{execution['detail']}"
                    " 但没有心率就无法判断有氧效率、漂移和区间分布，"
                    "这几项在本页会显示为缺失而不是 0 分。",
        }

    parts = [f"{name} {total_km:.2f} km / {int(moving // 60)} 分钟"]
    if decoupling:
        label = decoupling["verdict"]["label"]
        if label == "数据异常":
            # 数据异常时不把它和"匀速/掉速"并列，否则读者会误以为这是能力结论
            body = " · ".join(parts) + "。"
            body += ("心率数据前后差异超出正常范围，本次不评价有氧效率；"
                     f"{execution['label']}：{execution['detail']}")
            return {"tone": "warning", "headline": "数据有疑点，这堂课的结论要打折", "body": body}
        parts.append(label)
    parts.append(execution["label"])
    body = " · ".join(parts) + "。"
    body += execution["detail"]

    tone = "good" if score >= 76 else "fair" if score >= 62 else "poor"
    headline = {
        "A": "执行质量很高，这堂课可以当作模板",
        "B": "完成度良好，细节还能再抠",
        "C": "基本达标，但有几个明显短板",
        "D": "这堂课的训练价值打了折扣",
        "E": "数据或执行存在明显问题",
    }[grade]
    return {"tone": tone, "headline": headline, "body": body}
