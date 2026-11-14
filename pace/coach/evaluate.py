"""训练教练分析引擎。

设计原则（比算法更important）
----------------------------
1. **只对数据能证明的事下结论**。没有心率/功率就说没证据，绝不外推。
2. **每条结论都带证据**。报告里每个判断都附具体数字和日期，用户能自己核对。
3. **不做无条件鼓励**。verdict 可能是"练得不错"，也可能是"这段时间基本没练"、
   "负荷虚高但质量低"、"数据量不足以判断"。
4. **明确列出不知道的东西**。缺睡眠、缺 HRV、缺力量训练——这些直接影响建议可信度。

输出为纯 dict，方便 API 序列化、也方便单元测试断言。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import analytics
from ..parsers import sport_family, sport_label

# 判断阈值集中在这里，便于解释与调整
THRESHOLDS = {
    "ctl_low": 15.0,          # CTL 低于此值视为基础薄弱
    "ctl_good": 40.0,
    "acwr_high": 1.35,
    "acwr_very_high": 1.6,
    "acwr_low": 0.8,
    "monotony_high": 2.0,
    "tsb_overreached": -28.0,
    "tsb_fresh": 12.0,
    "long_run_ratio": 0.25,    # 长距离至少占周跑量 25%
    "easy_ratio_min": 0.70,    # 有氧低强度至少 70%
    "hard_ratio_max": 0.30,
    "decoupling_warn": 8.0,    # 心率漂移 > 8% 提示有氧耐力不足
    "cadence_low": 160,        # 跑步平均步频低于此值提示步幅过大
    "volume_drop": -25.0,      # 周量下降超过 25% 视为掉量
    "min_runs_for_analysis": 6,
}


def _fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime("%m-%d")


def _fmt_minutes(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"{m // 60}小时{m % 60}分" if m >= 60 else f"{m}分钟"


def _grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    if score >= 35:
        return "D"
    return "E"


def _evidence(text: str, strength: str = "medium") -> dict:
    return {"text": text, "strength": strength}


# ---------------------------------------------------------------- 数据清单

def data_inventory(rows: list[dict], now: float, tz) -> dict:
    """统计有哪些维度可用，决定分析的置信度。"""
    recent = [r for r in rows if r["start_time"] >= tz.start_of_day(now) - 56 * 86400]
    with_hr = [r for r in recent if r.get("avg_hr")]
    with_gps = [r for r in recent if r.get("has_gps")]
    with_power = [r for r in recent if r.get("avg_power")]
    return {
        "activities_total": len(rows),
        "activities_56d": len(recent),
        "with_heart_rate": len(with_hr),
        "with_gps": len(with_gps),
        "with_power": len(with_power),
        "has_power": len(with_power) >= 3,
        "has_hr": len(with_hr) >= 3,
        "has_gps": len(with_gps) >= 3,
        "sufficient_for_load": len(recent) >= THRESHOLDS["min_runs_for_analysis"],
        "missing": [],
    }


# ---------------------------------------------------------------- 主报告

def evaluate(store, *, tz_offset: float = 8.0, now: float | None = None,
             days: int = 56) -> dict:
    """生成一次完整评估。所有结论都能追溯到具体活动与数字。"""
    tz = analytics.TZ(tz_offset)
    now = now or tz.now()
    rows = store.all_activity_summaries()
    rows.sort(key=lambda r: r["start_time"])

    inventory = data_inventory(rows, now, tz)
    settings = store.get_settings()
    hr_max = float(settings.get("hr_max") or 190)
    hr_rest = float(settings.get("hr_rest") or 55)

    since = tz.start_of_day(now) - (days - 1) * 86400
    recent = [r for r in rows if since <= r["start_time"] <= now]
    prev_start = since - days * 86400
    previous = [r for r in rows if prev_start <= r["start_time"] < since]

    daily = analytics.daily_load_series(rows, now=now, tz=tz, days=28)
    mono = analytics.monotony_and_strain(daily)
    ratio = analytics.load_ratio(rows, now=now, tz=tz)
    series, fitness = analytics.fitness_fatigue(rows, now=now, tz=tz)
    mix = analytics.sport_family_mix(rows, now=now, tz=tz)

    findings: list[dict] = []
    strengths: list[dict] = []
    risks: list[dict] = []
    gaps: list[dict] = []

    # ---- 1. 数据充分性（先判断能不能分析）
    if not inventory["sufficient_for_load"]:
        gaps.append({
            "key": "low_data",
            "title": "数据量不足以做趋势判断",
            "detail": f"最近 8 周只有 {inventory['activities_56d']} 条记录。"
                      f"低于 {THRESHOLDS['min_runs_for_analysis']} 条时，负荷曲线和趋势都不可靠。",
            "impact": "high",
        })

    # ---- 2. 训练量趋势
    cur_sec = sum(r["moving_seconds"] or 0 for r in recent)
    prev_sec = sum(r["moving_seconds"] or 0 for r in previous)
    cur_km = sum(r["distance_m"] or 0 for r in recent) / 1000
    prev_km = sum(r["distance_m"] or 0 for r in previous) / 1000
    change = analytics.pct_change(cur_sec, prev_sec)

    if prev_sec > 0 and change is not None:
        if change <= THRESHOLDS["volume_drop"]:
            risks.append({
                "key": "volume_drop",
                "title": "训练量明显下降",
                "detail": f"近 {days // 7} 周总时长 {_fmt_minutes(cur_sec)}，"
                          f"比前一个周期的 {_fmt_minutes(prev_sec)} 少 {abs(change):.0f}%。",
                "evidence": [f"{_fmt_date(since)} 起 {len(recent)} 次 / 此前 {len(previous)} 次"],
                "impact": "medium",
            })
        elif change >= 60:
            risks.append({
                "key": "volume_spike",
                "title": "训练量上升过快",
                "detail": f"近 {days // 7} 周总时长比上一周期增加 {change:.0f}%。"
                          "一周内超过 20% 的增幅就会明显抬高受伤与过训风险。",
                "evidence": [f"{_fmt_date(since)} 起 {_fmt_minutes(cur_sec)} vs {_fmt_minutes(prev_sec)}"],
                "impact": "high",
            })

    # ---- 3. 负荷结构
    if mono["monotony"] is not None:
        if mono["monotony"] > THRESHOLDS["monotony_high"]:
            risks.append({
                "key": "monotony",
                "title": "训练日负荷太平，缺少波动",
                "detail": f"单调性指数 {mono['monotony']:.2f}（>2.0 偏高）。"
                          "每天都练差不多强度，比「练一休一」更容易积累疲劳。",
                "evidence": [f"14 天训练应变 {mono['strain']:.0f}"],
                "impact": "medium",
            })
        else:
            strengths.append({
                "key": "load_variation",
                "title": "负荷有起伏",
                "detail": f"单调性 {mono['monotony']:.2f}，强度与休息日有区分，这是可持续的节奏。",
                "impact": "low",
            })

    if ratio["ratio"] is not None:
        acwr = ratio["ratio"]
        if acwr > THRESHOLDS["acwr_very_high"]:
            risks.append({
                "key": "acwr_very_high",
                "title": "急性负荷远超慢性负荷",
                "detail": f"7 天负荷 {ratio['acute_7d']:.0f}，是 28 天周均 "
                          f"{ratio['chronic_weekly']:.0f} 的 {acwr:.2f} 倍。"
                          "这个比例通常对应伤病高发期，不是值得骄傲的「练得多」。",
                "evidence": [f"ACWR {acwr:.2f}（建议区间 0.8–1.3）"],
                "impact": "high",
            })
        elif acwr < THRESHOLDS["acwr_low"]:
            findings.append({
                "key": "acwr_low",
                "title": "负荷在回落",
                "detail": f"ACWR {acwr:.2f}，低于 0.8。如果这是赛前减量或主动恢复，合理；"
                          "如果不是，说明训练连续性在丢。",
                "impact": "low",
            })

    # ---- 4. 体能与状态
    ctl = fitness.get("ctl", 0)
    tsb = fitness.get("tsb", 0)
    if ctl < THRESHOLDS["ctl_low"]:
        findings.append({
            "key": "ctl_low",
            "title": "有氧基础偏薄",
            "detail": f"CTL（42 天负荷均值）只有 {ctl:.0f}。这个阶段谈配速目标意义不大，"
                      "先把连续 8–10 周、每周 3–4 次的有氧打底做起来。",
            "impact": "medium",
        })
    elif ctl >= THRESHOLDS["ctl_good"]:
        strengths.append({
            "key": "ctl_good",
            "title": "有氧基础扎实",
            "detail": f"CTL {ctl:.0f}，说明过去一个多月负荷稳定积累，"
                      "可以开始安排质量课（阈值/间歇）。",
            "impact": "low",
        })
    if tsb < THRESHOLDS["tsb_overreached"]:
        risks.append({
            "key": "overreached",
            "title": "疲劳明显堆积",
            "detail": f"TSB（状态）{tsb:.0f}，低于 −28。这已经是「该减量」的信号，"
                      "继续硬顶只会把疲劳带进下一次比赛。",
            "impact": "high",
        })
    elif tsb > THRESHOLDS["tsb_fresh"] and ctl > 20:
        strengths.append({
            "key": "fresh",
            "title": "状态新鲜",
            "detail": f"TSB +{tsb:.0f}，近期减量生效，适合安排测试或比赛。",
            "impact": "low",
        })

    # ---- 5. 强度分布（需要心率）
    if inventory["has_hr"]:
        zone_share = _zone_share(rows, since, now, hr_max, hr_rest)
        if zone_share:
            easy = zone_share.get("z1", 0) + zone_share.get("z2", 0)
            tempo = zone_share.get("z3", 0)
            hard = zone_share.get("z4", 0) + zone_share.get("z5", 0)
            quality = tempo + hard          # Z3 及以上的总刺激量
            if easy < THRESHOLDS["easy_ratio_min"] * 100:
                risks.append({
                    "key": "too_hard",
                    "title": "低强度占比不足",
                    "detail": f"Z1+Z2 只占 {easy:.0f}%，低于 70%。大量中等强度会让"
                              "有氧收益打折、恢复变慢，这是最常见的「练得勤但不见涨」。",
                    "evidence": [f"近 8 周心率区间分布：Z1 {zone_share['z1']:.0f}% · "
                                 f"Z2 {zone_share['z2']:.0f}% · Z3 {zone_share['z3']:.0f}% · "
                                 f"Z4 {zone_share['z4']:.0f}% · Z5 {zone_share['z5']:.0f}%"],
                    "impact": "high",
                })
            elif hard > THRESHOLDS["hard_ratio_max"] * 100:
                risks.append({
                    "key": "too_hard_short",
                    "title": "高强度比例偏高",
                    "detail": f"Z4+Z5 占 {hard:.0f}%，超过 30%。频繁高强度会挤压"
                              "有氧基础，也更容易在小伤上反复。",
                    "evidence": [f"Z4 {zone_share['z4']:.0f}% + Z5 {zone_share['z5']:.0f}%"],
                    "impact": "medium",
                })
            elif quality < 8 and easy > 85:
                findings.append({
                    "key": "no_quality",
                    "title": "几乎没有质量课",
                    "detail": f"Z3 及以上只有 {quality:.0f}%（Z4+Z5 仅 {hard:.0f}%）。"
                              "全是慢跑只能维持有氧，不会让你变快；"
                              "每周至少需要 1 次节奏跑或阈值跑。",
                    "impact": "medium",
                })
            elif quality < 15:
                findings.append({
                    "key": "low_quality",
                    "title": "质量课偏少",
                    "detail": f"Z3 及以上占 {quality:.0f}%，低于 15%。"
                              "如果目标是提高成绩，建议每周安排一次 20–30 分钟的节奏/阈值训练。",
                    "impact": "low",
                })
            else:
                strengths.append({
                    "key": "distribution_ok",
                    "title": "强度分布基本合理",
                    "detail": f"低强度 {easy:.0f}%、质量区 {quality:.0f}%"
                              "（Z3 及以上），接近主流的极化/金字塔区间。",
                    "impact": "low",
                })
    else:
        gaps.append({
            "key": "no_hr",
            "title": "缺少心率数据",
            "detail": "没有足够的心率记录，无法判断强度分布、有氧效率与恢复状态。"
                      "建议跑步/骑行时佩戴光电心率或心率带。",
            "impact": "high",
        })

    # ---- 6. 跑步专项
    run_rows = [r for r in recent if sport_family(r["sport"]) == "run"]
    if len(run_rows) >= 4:
        findings.extend(_running_findings(run_rows, now, tz, hr_max, hr_rest))

    # ---- 7. 运动单一性
    if mix:
        dominant = max(mix, key=mix.get)
        if mix[dominant] > 92 and len(mix) > 1:
            findings.append({
                "key": "single_sport",
                "title": "训练几乎只有一种运动",
                "detail": f"{_family_cn(dominant)}占 {mix[dominant]:.0f}%。"
                          "加入 1 次力量训练会直接改善跑姿稳定性和抗伤能力，"
                          "这在数据里看不出来，但教练普遍会先加这一项。",
                "impact": "low",
            })

    # ---- 8. 质量与量的匹配
    prs = analytics.personal_records(store, limit=0)
    if prs and ctl > 10:
        best_ratio = _quality_vs_volume(prs, rows, now, tz)
        if best_ratio is not None and best_ratio < 0.75:
            risks.append({
                "key": "quantity_over_quality",
                "title": "跑量不小，但速度能力没有跟上",
                "detail": "把最近 8 周的跑步量和你的最佳成绩放在一起看，"
                          "成绩水平相对跑量偏低。这通常意味着训练偏「堆量」，"
                          "缺少节奏跑/间歇这类速度刺激。",
                "evidence": [f"相对水平指数 {best_ratio:.2f}（<0.75 提示速度能力落后于跑量）"],
                "impact": "medium",
            })

    # ---- 9. 必答的诚实项
    gaps.extend(_baseline_gaps(inventory, settings))

    # ---- 10. 数据来源质量：大量"手工/文件导入"的摘要记录会稀释可信度
    csv_like = [r for r in recent if not r.get("has_gps")]
    if recent and len(csv_like) / len(recent) > 0.5:
        gaps.append({
            "key": "summaries_only",
            "title": "超过一半的记录没有逐点数据",
            "detail": f"{len(csv_like)}/{len(recent)} 次活动来自汇总记录（CSV 或手工录入），"
                      "没有轨迹和逐秒心率。负荷与趋势仍可用，但强度分布、配速漂移、"
                      "有氧效率这些专项判断只能粗略估计。",
            "impact": "medium",
        })

    # ---- 汇总评分
    score = _score(ctl, mono, ratio, tsb, findings, risks, gaps, inventory)
    grade = _grade(score)
    verdict = _verdict(score, grade, inventory, findings, risks, ctl, tsb, mono)

    return {
        "generated_at": now,
        "period_days": days,
        "grade": grade,
        "score": round(score, 0),
        "verdict": verdict,
        "confidence": _confidence(inventory, len(recent)),
        "inventory": inventory,
        "fitness": fitness,
        "load": {
            "acute_7d": ratio["acute_7d"],
            "chronic_weekly": ratio["chronic_weekly"],
            "acwr": ratio["ratio"],
            "monotony": mono["monotony"],
            "strain": mono["strain"],
            "daily_loads": [round(x, 1) for x in daily],
        },
        "volume": {
            "current_seconds": round(cur_sec),
            "previous_seconds": round(prev_sec),
            "current_km": round(cur_km, 1),
            "previous_km": round(prev_km, 1),
            "change_pct": change,
            "sessions": len(recent),
            "previous_sessions": len(previous),
        },
        "sport_mix": mix,
        "zone_share": _zone_share(rows, since, now, hr_max, hr_rest) if inventory["has_hr"] else None,
        "findings": findings,
        "strengths": strengths,
        "risks": risks,
        "gaps": gaps,
        "next_steps": _next_steps(findings, risks, gaps, inventory, ratio, ctl),
    }


# ---------------------------------------------------------------- 辅助判断

def _family_cn(fam: str) -> str:
    return {"run": "跑步", "ride": "骑行", "swim": "游泳", "walk": "徒步",
            "other": "其他"}.get(fam, fam)


def _zone_share(rows: list[dict], since: float, now: float,
                hr_max: float, hr_rest: float) -> dict | None:
    """用活动的平均心率近似区间分布（没有逐点流时的保守估计）。"""
    buckets = {"z1": 0.0, "z2": 0.0, "z3": 0.0, "z4": 0.0, "z5": 0.0}
    total = 0.0
    reserve = max(1.0, hr_max - hr_rest)
    for r in rows:
        if not (since <= r["start_time"] <= now):
            continue
        hr = r.get("avg_hr")
        sec = float(r["moving_seconds"] or 0)
        if not hr or sec <= 0:
            continue
        pct = (float(hr) - hr_rest) / reserve
        if pct < 0.60:
            buckets["z1"] += sec
        elif pct < 0.70:
            buckets["z2"] += sec
        elif pct < 0.80:
            buckets["z3"] += sec
        elif pct < 0.90:
            buckets["z4"] += sec
        else:
            buckets["z5"] += sec
        total += sec
    if total <= 0:
        return None
    return {k: round(v / total * 100, 1) for k, v in buckets.items()}


def _running_findings(run_rows: list[dict], now: float, tz, hr_max: float,
                      hr_rest: float) -> list[dict]:
    """跑步专项：长距离占比、配速漂移、步频、配速一致性。"""
    out: list[dict] = []
    total_km = sum(r["distance_m"] or 0 for r in run_rows) / 1000
    longest = max(run_rows, key=lambda r: r["distance_m"] or 0)
    longest_km = (longest["distance_m"] or 0) / 1000
    if total_km > 0 and longest_km / total_km < THRESHOLDS["long_run_ratio"]:
        out.append({
            "key": "no_long_run",
            "title": "缺少长距离课",
            "detail": f"最长一次 {longest_km:.1f} km，只占同期跑量的 "
                      f"{longest_km / total_km * 100:.0f}%。半马以上目标通常需要"
                      "每周一次占总跑量 25–30% 的长距离。",
            "evidence": [f"{_fmt_date(longest['start_time'])} 最长 {longest_km:.1f} km"],
            "impact": "medium",
        })

    # 配速一致性：用同距离段活动的变异系数
    paces = [r["moving_seconds"] / (r["distance_m"] / 1000)
             for r in run_rows if r["distance_m"] > 1000 and r["moving_seconds"] > 0]
    if len(paces) >= 3:
        mean = sum(paces) / len(paces)
        sd = (sum((p - mean) ** 2 for p in paces) / len(paces)) ** 0.5
        cv = sd / mean if mean else 0
        if cv < 0.04 and mean > 1:
            out.append({
                "key": "pace_repeatable",
                "title": "配速重复性好",
                "detail": f"{len(paces)} 次跑步的配速变异系数仅 {cv * 100:.1f}%，"
                          "说明你对强度的控制比较稳，比赛日更容易执行计划。",
                "impact": "low",
            })
        elif cv > 0.12:
            out.append({
                "key": "pace_inconsistent",
                "title": "配速波动很大",
                "detail": f"配速变异系数 {cv * 100:.0f}%。如果没有刻意做变速课，"
                          "这通常说明起跑偏快、后程掉速，是耐力分配问题。",
                "impact": "low",
            })

    # 心率漂移（有前后半程数据时才有意义，这里用同配速下平均心率变化近似）
    hr_rows = [r for r in run_rows if r.get("avg_hr")]
    if len(hr_rows) >= 4:
        first = hr_rows[: len(hr_rows) // 2]
        second = hr_rows[len(hr_rows) // 2:]
        a = sum(r["avg_hr"] for r in first) / len(first)
        b = sum(r["avg_hr"] for r in second) / len(second)
        drift = (b - a) / max(1.0, a) * 100
        if drift > 3:
            out.append({
                "key": "hr_drift",
                "title": "同期平均心率在往上走",
                "detail": f"后半段的平均心率比前半段高 {drift:.1f}%。"
                          "在跑量增加期这通常是恢复不足的信号，而不是「进步」。",
                "impact": "medium",
            })
    return out


def _quality_vs_volume(prs: list[dict], rows: list[dict], now: float, tz) -> float | None:
    """跑量 vs 5km 最好成绩的粗略相对水平。

    用 age-graded 思路太复杂，这里做一个可解释的比值：
    近 8 周周均跑量(km) / 5km 最好成绩(秒) * 常数。仅用于提示，不作绝对评价。
    """
    since = tz.start_of_day(now) - 56 * 86400
    km = sum(r["distance_m"] or 0 for r in rows
             if r["start_time"] >= since and sport_family(r["sport"]) == "run") / 1000
    if km <= 0:
        return None
    weekly = km / 8.0
    five_k = next((p for p in prs if p["family"] == "run" and p["distance_m"] == 5000), None)
    if not five_k:
        return None
    # 参考锚点：周跑量 30km 对应 5km 25:00 为 1.0
    return round((weekly / 30.0) / ((five_k["seconds"] / 1500.0) ** 1.1), 2)


def _baseline_gaps(inventory: dict, settings: dict) -> list[dict]:
    gaps: list[dict] = []
    if not inventory.get("has_power"):
        gaps.append({
            "key": "no_power",
            "title": "没有功率数据",
            "detail": "骑行/跑步的功率区间分析、NP、FTP 对比都做不了。"
                      "有功率计的话接上，分析质量会明显提升。",
            "impact": "medium",
        })
    if not settings.get("weight_kg"):
        gaps.append({
            "key": "no_weight",
            "title": "没填体重",
            "detail": "没有体重就无法计算 W/kg、跑力值与热量消耗精度。",
            "impact": "low",
        })
    gaps.append({
        "key": "no_recovery",
        "title": "没有睡眠 / HRV / 主观疲劳",
        "detail": "睡眠、HRV、RPE 这三项能显著提高「该不该练」的判断准确率。"
                  "目前只能从负荷和心率反推，无法区分「练得累」和「睡得差」。",
        "impact": "high",
    })
    return gaps


def _score(ctl: float, mono: dict, ratio: dict, tsb: float,
           findings, risks, gaps, inventory) -> float:
    """分数只反映"训练结构是否健康"，不反映天赋或绝对水平。"""
    score = 55.0
    score += min(20.0, ctl / 2.0)                       # 有氧基础
    if mono.get("monotony") is not None:
        score -= max(0.0, (mono["monotony"] - 1.2) * 8)  # 单调性惩罚
    if ratio.get("ratio") is not None:
        dev = abs(ratio["ratio"] - 1.05)
        score -= min(18.0, dev * 22)                     # 偏离最佳区间
    if tsb < -25:
        score -= 12
    if tsb > 20:
        score += 3
    score -= 4 * sum(1 for r in risks if r.get("impact") == "high")
    score -= 2 * sum(1 for r in risks if r.get("impact") == "medium")
    if not inventory.get("sufficient_for_load"):
        score = min(score, 40)
    return max(5.0, min(95.0, score))


def _verdict(score: float, grade: str, inventory: dict, findings, risks,
             ctl: float, tsb: float, mono: dict) -> dict:
    """给出直白的总评。宁可说"不够"，也不虚假表扬。"""
    if not inventory.get("sufficient_for_load"):
        return {
            "tone": "insufficient",
            "headline": "数据太少，先别急着要结论",
            "body": f"最近 8 周只有 {inventory['activities_56d']} 条记录。"
                    "任何关于「进步/退步」的判断都会是猜的。先把设备连上、连续记录两周，"
                    "再来看这份报告。",
        }
    high_risks = [r for r in risks if r.get("impact") == "high"]
    if high_risks:
        return {
            "tone": "warning",
            "headline": "有明确的风险项，先处理它",
            "body": f"最需要改的是「{high_risks[0]['title']}」。"
                    f"当前 CTL {ctl:.0f}、TSB {tsb:.0f}，"
                    "在这条趋势上继续加量，收益会低于伤病风险。",
        }
    if score >= 72:
        return {
            "tone": "good",
            "headline": "训练结构健康，可以开始追求质量",
            "body": f"负荷在合理区间，波动也正常（CTL {ctl:.0f}、TSB {tsb:.0f}）。"
                    "接下来重点不是再加量，而是把质量课的执行标准提上去。",
        }
    if score >= 55:
        return {
            "tone": "fair",
            "headline": "能练，但结构有明显短板",
            "body": f"基础还行（CTL {ctl:.0f}），但有几个结构性问题在拖后腿。"
                    "按下面的建议改 2–3 周，再复评一次比继续堆量更划算。",
        }
    return {
        "tone": "poor",
        "headline": "现在这套训练方式性价比不高",
        "body": f"CTL {ctl:.0f}、TSB {tsb:.0f}"
                + (f"、单调性 {mono['monotony']:.1f}" if mono.get("monotony") else "")
                + "。不是练得不够多，而是负荷节奏和强度分配需要重排。"
                  "先别加量，按下面的顺序改。",
    }


def _confidence(inventory: dict, recent_count: int) -> dict:
    score = 0
    if inventory.get("has_hr"):
        score += 35
    if inventory.get("has_gps"):
        score += 25
    if inventory.get("has_power"):
        score += 15
    score += min(25, recent_count * 2)
    score = min(95, score)
    level = "high" if score >= 70 else "medium" if score >= 45 else "low"
    return {
        "score": score,
        "level": level,
        "label": {"high": "较可信", "medium": "中等", "low": "偏低"}[level],
        "reason": f"心率{'有' if inventory.get('has_hr') else '缺'} · "
                  f"GPS{'有' if inventory.get('has_gps') else '缺'} · "
                  f"功率{'有' if inventory.get('has_power') else '缺'} · "
                  f"8 周内 {recent_count} 次训练",
    }


def _next_steps(findings, risks, gaps, inventory, ratio, ctl) -> list[dict]:
    """把结论变成可执行的下一步，按优先级排序。"""
    steps: list[dict] = []
    keys = {r["key"] for r in risks} | {f["key"] for f in findings}

    priority = [
        ("overreached", "本周减量 30%，至少安排 2 个完全休息日",
         "TSB 已经很低，继续练只会加重疲劳。减量不等于退步。"),
        ("acwr_very_high", "未来 7 天不加量，把强度压到 Z2",
         "急性负荷是慢性负荷的 1.6 倍以上，先让身体追上训练。"),
        ("volume_spike", "把本周总量砍回上周的 1.1 倍以内",
         "周增幅超过 20% 是受伤概率上升最快的区间。"),
        ("too_hard", "把 80% 的训练压到心率 Z2 以下",
         "低强度不足是最常见的「练得勤但不进步」原因。"),
        ("monotony", "安排 1–2 个完全休息日，别每天同强度",
         "负荷没有起伏时，身体无法完成超量恢复。"),
        ("no_quality", "每周加 1 次阈值跑（20–30 分钟接近阈值配速）",
         "全慢跑只能维持，不能提高速度能力。"),
        ("quantity_over_quality", "把每周 1 次慢跑换成节奏跑，跑量不增",
         "现在的问题是量够了、刺激不够。"),
        ("no_long_run", "每周固定一次长距离，占总跑量 25–30%",
         "长距离是耐力项目的核心课，缺它后程一定掉。"),
        ("no_hr", "训练时佩戴心率设备，至少覆盖 80% 的课程",
         "没有心率就无法判断强度，后面所有建议都会打折。"),
        ("no_recovery", "每天记一句睡眠时长和主观疲劳（1–10）",
         "这两项能解释一半的「状态不好」，比多看一个图表有用。"),
    ]
    for key, action, why in priority:
        if key in keys:
            steps.append({"action": action, "why": why, "priority": len(steps) + 1})
        if len(steps) >= 5:
            break

    if not steps:
        steps.append({
            "action": "维持当前节奏，每周复评一次",
            "why": "没有明显问题，这时候最该做的是别乱改计划。",
            "priority": 1,
        })
    return steps
