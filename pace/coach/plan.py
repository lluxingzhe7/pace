"""训练计划生成。

原则
----
* 计划是"约束下的建议"，不是命令：每天给出可替换选项，并解释为什么。
* 目标决定结构：健康 → 频率优先；完赛 → 长距离优先；提成绩 → 质量课优先。
* 周总量基于过去 4 周实际均值，增幅硬上限 10%（新手 8%）。
* 有风险项时先修结构（减量/加休息），不叠加新强度。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import analytics
from ..parsers import sport_family

GOALS = {
    "health": {"label": "健康与体能", "focus": "规律、低强度、可持续"},
    "finish": {"label": "完成一场比赛", "focus": "长距离耐力 + 比赛配速"},
    "improve": {"label": "提高成绩", "focus": "质量课 + 阈值能力"},
    "lose_weight": {"label": "减脂", "focus": "总消耗 + 力量训练"},
}

DAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _tz(tz_offset: float) -> timezone:
    return timezone(timedelta(hours=tz_offset))


def _threshold_pace(store) -> dict:
    """用最近 8 周 5km/10km 最好成绩反推阈值配速（秒/公里）。"""
    prs = analytics.personal_records(store, limit=0)
    run_prs = [p for p in prs if p["family"] == "run"]
    five = next((p for p in run_prs if p["distance_m"] == 5000), None)
    ten = next((p for p in run_prs if p["distance_m"] == 10000), None)
    base = None
    if five:
        # 5km 比赛配速约等于阈值配速，稍慢 3%
        base = five["seconds"] / 5.0 * 1.03
    elif ten:
        base = ten["seconds"] / 10.0 * 0.98
    elif run_prs:
        p = run_prs[0]
        base = p["seconds"] / (p["distance_m"] / 1000) * 1.10
    if not base:
        return {}
    return {
        "threshold": round(base),
        "easy": round(base * 1.28),
        "marathon": round(base * 1.12),
        "interval": round(base * 0.94),
    }


def build_plan(store, report: dict, *, tz_offset: float = 8.0,
               goal: str = "health", days_per_week: int = 4,
               target_date: float | None = None, now: float | None = None) -> dict:
    """基于评估报告生成一周计划 + 4 周阶段建议。"""
    tz = analytics.TZ(tz_offset)
    now = now or tz.now()
    goal = goal if goal in GOALS else "health"
    days_per_week = max(2, min(6, int(days_per_week or 4)))

    rows = store.all_activity_summaries()
    since4w = tz.start_of_day(now) - 27 * 86400
    recent4w = [r for r in rows if r["start_time"] >= since4w]
    weekly_seconds = sum(r["moving_seconds"] or 0 for r in recent4w) / 4.0
    run_rows = [r for r in recent4w if sport_family(r["sport"]) == "run"]
    run_weekly_km = sum(r["distance_m"] or 0 for r in run_rows) / 4.0 / 1000
    has_run = bool(run_rows)
    primary = "run" if run_rows else (
        "ride" if any(sport_family(r["sport"]) == "ride" for r in recent4w) else "run")

    thr = _threshold_pace(store)
    settings = store.get_settings()
    hr_max = float(settings.get("hr_max") or 190)
    hr_rest = float(settings.get("hr_rest") or 56)

    def z_band(lo: float, hi: float) -> str:
        a = round(hr_rest + (hr_max - hr_rest) * lo)
        b = round(hr_rest + (hr_max - hr_rest) * hi)
        return f"{a}–{b} bpm"

    risks = {r["key"] for r in report.get("risks", [])}
    findings = {f["key"] for f in report.get("findings", [])}
    ctl = float(report.get("fitness", {}).get("ctl") or 0)
    tsb = float(report.get("fitness", {}).get("tsb") or 0)

    # ---- 周量目标：实际均值 × 增幅；有风险时先压住
    growth = 0.06
    if ctl < 15:
        growth = 0.08          # 基础薄，允许稍快建立习惯
    if "overreached" in risks or "acwr_very_high" in risks:
        growth = -0.30
    elif "volume_spike" in risks:
        growth = -0.15
    elif "too_hard" in risks:
        growth = 0.02

    target_seconds = max(1200.0, weekly_seconds * (1 + growth)) if weekly_seconds > 0 else 7200.0
    target_km = run_weekly_km * (1 + growth) if run_weekly_km > 0 else target_seconds / 3600 * 9.0

    # ---- 课程配比（基础模板，再按目标补充）
    base = ["easy", "quality", "easy", "long"]
    extras: list[str] = []
    if goal == "improve" or "no_quality" in findings or "quantity_over_quality" in risks:
        extras = ["easy", "quality"]
    elif goal == "finish":
        extras = ["easy", "easy"]
    elif goal == "lose_weight":
        extras = ["strength", "easy"]
    else:
        extras = ["easy", "strength"]
    chosen = (base + extras)[:days_per_week]
    if days_per_week >= 3 and "long" not in chosen:
        chosen[-1] = "long"
    # 一周最多一次质量课（除非明确要提成绩且天数 >= 5）
    if chosen.count("quality") > 1 and not (goal == "improve" and days_per_week >= 5):
        first_q = chosen.index("quality")
        chosen = [k if (i == first_q or k != "quality") else "easy" for i, k in enumerate(chosen)]

    # 分配到具体星期：长距离放周末，高质量课与长距离至少隔一天
    weekday_plan = {
        2: [1, 5],
        3: [1, 3, 5],
        4: [1, 2, 4, 6],
        5: [1, 2, 3, 5, 6],
        6: [1, 2, 3, 4, 5, 6],
    }.get(days_per_week, [1, 3, 5])
    # 把 long 放到靠后的位置，quality 放中间，easy 填前面
    order_hint = {"easy": 0, "strength": 1, "quality": 2, "long": 3}
    ordered = sorted(chosen, key=lambda k: order_hint.get(k, 9))
    if "long" in ordered:
        ordered.remove("long")
        ordered.append("long")

    sessions = []
    for idx, weekday in enumerate(weekday_plan[:len(ordered)]):
        kind = ordered[idx]
        sessions.append(_session(kind, weekday, target_seconds, target_km, thr,
                                 z_band, primary))

    # 休息日补齐
    rest_days = [d for d in range(7) if d not in weekday_plan[:len(ordered)]]
    for d in rest_days:
        sessions.append({
            "weekday": d,
            "day": DAY_NAMES[d],
            "kind": "rest",
            "title": "休息",
            "duration_min": 0,
            "distance_km": None,
            "target": "完全休息或 20 分钟拉伸",
            "why": "恢复日不是浪费；负荷只有配上恢复才会变成能力。",
        })
    sessions.sort(key=lambda s: s["weekday"])

    # ---- 4 周阶段
    phase = _phase(report, now, tz, target_date)

    return {
        "generated_at": now,
        "goal": goal,
        "goal_label": GOALS[goal]["label"],
        "goal_focus": GOALS[goal]["focus"],
        "days_per_week": days_per_week,
        "primary_sport": primary,
        "weekly_target": {
            "seconds": round(target_seconds),
            "distance_km": round(target_km, 1) if has_run else None,
            "growth_pct": round(growth * 100),
            "basis": f"最近 4 周周均 {sum(r['moving_seconds'] or 0 for r in recent4w) / 4 / 60:.0f} 分钟"
                     + (f" / {run_weekly_km:.1f} km" if has_run else ""),
        },
        "threshold": thr,
        "sessions": sessions,
        "phase": phase,
        "guardrails": _guardrails(report),
    }


def _session(kind: str, weekday: int, target_seconds: float, target_km: float,
             thr: dict, z_band, primary: str) -> dict:
    day = DAY_NAMES[weekday]
    if kind == "easy":
        minutes = max(25, round(target_seconds * 0.22 / 60))
        return {
            "weekday": weekday, "day": day, "kind": "easy", "title": "有氧慢跑（Z2）",
            "duration_min": minutes,
            "distance_km": round(minutes / 60 * 9.0, 1) if primary == "run" else None,
            "target": f"心率 {z_band(0.60, 0.72)}，能说完整句子的强度",
            "why": "低强度撑起总负荷的大部分，是耐力提升的地基；它几乎不增加恢复负担。",
            "pace_hint": f"约 {_pace_text(thr.get('easy'))}" if thr.get("easy") else None,
        }
    if kind == "quality":
        minutes = max(30, round(target_seconds * 0.20 / 60))
        return {
            "weekday": weekday, "day": day, "kind": "quality", "title": "阈值课",
            "duration_min": minutes,
            "distance_km": round(minutes / 60 * 10.5, 1) if primary == "run" else None,
            "target": f"热身 12 分钟，主课 20–25 分钟心率 {z_band(0.82, 0.89)}，放松 10 分钟",
            "why": "阈值能力直接决定你能「稳多久」，是提升比赛成绩性价比最高的一课。",
            "pace_hint": f"主课约 {_pace_text(thr.get('threshold'))}" if thr.get("threshold") else None,
        }
    if kind == "long":
        ratio = 0.30 if target_km >= 30 else 0.25
        minutes = max(45, round(target_seconds * ratio / 60))
        return {
            "weekday": weekday, "day": day, "kind": "long", "title": "长距离",
            "duration_min": minutes,
            "distance_km": round(max(6.0, target_km * ratio), 1) if primary == "run" else None,
            "target": f"全程心率 {z_band(0.58, 0.72)}，配速比日常慢 15–30 秒",
            "why": "长距离训练脂肪利用、肌腱韧性与后程抗掉速能力，别的课替代不了。",
            "pace_hint": f"约 {_pace_text(thr.get('easy'))} 或更慢" if thr.get("easy") else None,
        }
    if kind == "strength":
        return {
            "weekday": weekday, "day": day, "kind": "strength", "title": "力量训练",
            "duration_min": 35,
            "distance_km": None,
            "target": "深蹲/硬拉/弓步/提踵 各 3 组，核心 2 个动作",
            "why": "力量是抗伤和跑步经济性的底层能力，而且不会明显挤占有氧恢复。",
            "pace_hint": None,
        }
    return {}


def _pace_text(seconds: float | None) -> str | None:
    if not seconds:
        return None
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}/km"


def _phase(report: dict, now: float, tz, target_date: float | None) -> dict:
    """给出 4 周节奏：build / deload / peak / taper。"""
    ctl = float(report.get("fitness", {}).get("ctl") or 0)
    risks = {r["key"] for r in report.get("risks", [])}
    weeks_to_target = None
    if target_date:
        weeks_to_target = max(0, int((target_date - now) / (7 * 86400)))

    if weeks_to_target is not None and weeks_to_target <= 2:
        name, detail = "赛前减量", "总量砍到 60–70%，保留短促的配速刺激，最后 3 天彻底放松。"
    elif "overreached" in risks or "acwr_very_high" in risks:
        name, detail = "恢复周", "本周不追求量，目标是把 TSB 拉回 −10 以上。"
    elif ctl < 15:
        name, detail = "基础期", "连续 6–8 周稳定训练，先建立习惯和最小有氧基础，不安排高强度。"
    elif ctl < 35:
        name, detail = "建设期", "以有氧为主、每周一次质量课；总量增幅控制在 10% 以内。"
    else:
        name, detail = "强化期", "质量课占 20–25%，注意每 3–4 周安排一个减量周。"

    if name == "恢复周":
        weeks = [
            {"week": 1, "focus": "主动恢复", "note": "总量 −30%，全部 Z1–Z2，睡眠优先"},
            {"week": 2, "focus": "重建节奏", "note": "回到正常量的 80%，保留一次质量课"},
            {"week": 3, "focus": "恢复建设", "note": "总量回到 100%，观察静息心率是否回落"},
            {"week": 4, "focus": "复评", "note": "重看一次评估，TSB 回到 −10 以上再谈加量"},
        ]
    else:
        weeks = [
            {"week": 1, "focus": "执行与适应", "note": "先把频率跑通，不追求配速"},
            {"week": 2, "focus": "小幅加量", "note": f"总量 +{8 if ctl < 20 else 5}%，保持一次质量课"},
            {"week": 3, "focus": "刺激提升", "note": "质量课延长 5 分钟，其余不变"},
            {"week": 4, "focus": "减量恢复", "note": "总量 −30%，让身体吸收前三周"},
        ]

    return {
        "name": name,
        "detail": detail,
        "weeks_to_target": weeks_to_target,
        "weeks": weeks,
    }


def _guardrails(report: dict) -> list[str]:
    out = ["周总量增幅不超过 10%；连续两周超标就先减一周。"]
    risks = {r["key"] for r in report.get("risks", [])}
    if "too_hard" in risks:
        out.append("每周至少 4 次训练的心率压在 Z2 以下。")
    if "monotony" in risks:
        out.append("每周至少 1 个完全休息日，不要用「慢跑」代替休息。")
    if "overreached" in risks:
        out.append("TSB 回到 −10 以上之前，不加任何高强度课。")
    if "no_long_run" in {f["key"] for f in report.get("findings", [])}:
        out.append("长距离固定在周末，距离不低于周跑量的 25%。")
    out.append("出现持续疼痛（不是酸痛）立刻停练并就医，这一条不能被任何计划覆盖。")
    out.append("本计划由规则生成，不替代医生或线下教练的诊断。")
    return out
