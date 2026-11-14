"""多周期训练计划（1 周 / 1 月 / 1 季度 / 半年）。

方法论参考（公开、可解释，不抄具体课程表）：
* **周期化**：基础期 → 建设期 → 高峰/专项期 → 减量期，每 4 周一个减量周
* **极化训练**：低强度占 75–85%，高强度 10–20%（Seiler 的 80/20）
* **阈值优先**：业余选手提成绩的最高性价比区间在 LT2 附近（挪威双阈值法的简化）
* **比赛专项**：赛前 2–3 周加入目标配速段，最后 1–2 周减量 40–60%
* **力量**：每周 2 次基础力量，抗伤 + 提升经济性
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import analytics
from ..parsers import sport_family

DAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

GOALS = {
    "health": {"label": "健康与体能", "family": "run", "focus": "规律、低强度、可持续"},
    "finish_5k": {"label": "跑完 5 公里", "family": "run", "distance": 5000,
                  "focus": "从走跑结合到连续跑完"},
    "finish_10k": {"label": "跑完 10 公里", "family": "run", "distance": 10000,
                   "focus": "有氧底子 + 配速控制"},
    "half_marathon": {"label": "半程马拉松", "family": "run", "distance": 21097,
                      "focus": "长距离耐力 + 比赛配速"},
    "marathon": {"label": "全程马拉松", "family": "run", "distance": 42195,
                 "focus": "周跑量 + 长距离 + 补给演练"},
    "improve_pace": {"label": "提高配速", "family": "run", "focus": "阈值 + 速度刺激"},
    "lose_weight": {"label": "减脂", "family": "run", "focus": "总消耗 + 力量 + 饮食"},
    "sportive": {"label": "骑行长距离", "family": "ride", "focus": "耐力 + 爬坡能力"},
    "ride_base": {"label": "骑行打底（有氧）", "family": "ride", "focus": "低强度有氧，先把频率稳住"},
    "ride_ftp": {"label": "提高 FTP（阈值）", "family": "ride", "focus": "甜区与阈值间歇，把持续功率练上去"},
    "ride_climb": {"label": "爬坡专项", "family": "ride", "focus": "长坡重复 + 爬升量，功率体重比"},
    "ride_century": {"label": "百公里骑行", "family": "ride", "distance": 100000,
                     "focus": "后程耐力 + 补给与配速"},
    "swim_start": {"label": "游泳入门（学会连续游）", "family": "swim",
                   "focus": "换气、漂浮、连续游 200 米"},
    "swim_1000": {"label": "游完 1000 米", "family": "swim", "distance": 1000,
                  "focus": "技术 + 有氧连续游"},
    "swim_1500": {"label": "1500 米计时", "family": "swim", "distance": 1500,
                  "focus": "配速分配与转身效率"},
    "swim_openwater": {"label": "公开水域 / 铁三游泳", "family": "swim", "distance": 1500,
                       "focus": "抬头看方向、跟游、耐寒与出发"},
    "recover": {"label": "恢复与调整", "family": "run", "focus": "降负荷、修复"},
}

# 各运动族的「一小时大概多少公里」，用于没有历史数据时估量
FAMILY_KM_PER_HOUR = {"run": 9.0, "ride": 26.0, "swim": 2.0}
FAMILY_LABEL = {"run": "跑步", "ride": "骑行", "swim": "游泳", "walk": "徒步", "other": "运动"}

LEVELS = ["beginner", "intermediate", "advanced"]
LEVEL_LABELS = {"beginner": "新手（<1 年）", "intermediate": "进阶（1–3 年）",
                "advanced": "老练（3 年以上）"}

CONSTRAINTS = {
    "no_gym": "没有健身房/器械",
    "night_only": "只能晚上练",
    "knee": "膝盖或关节不适",
    "time_frag": "时间碎片化",
    "no_power": "没有功率计",
}


def suggest_intake(report: dict) -> dict:
    """根据现有数据给出建议值，用户只需微调。"""
    ctl = float(report.get("fitness", {}).get("ctl") or 0)
    mix = report.get("sport_mix") or {}
    dominant = max(mix, key=mix.get) if mix else "run"
    share = float(mix.get(dominant) or 0)
    if ctl < 15:
        level = "beginner"
        horizon = 13
    elif ctl < 35:
        level = "intermediate"
        horizon = 13
    else:
        level = "advanced"
        horizon = 26

    # 目标跟着实际练的项目走，否则推荐一个计划却全是别的运动
    if dominant == "swim" and share >= 25:
        goal = "swim_start" if level == "beginner" else "swim_1000"
    elif dominant == "ride" and share >= 25:
        goal = "ride_base" if level == "beginner" else "sportive"
    elif ctl < 15:
        goal = "health"
    elif ctl < 35:
        goal = "improve_pace"
    else:
        goal = "half_marathon"

    note = ("CTL 低于 15，先从建立习惯开始" if ctl < 15 else
            "有基础，可以直接进入阈值建设" if ctl < 35 else
            "负荷基础较好，可以安排长距离专项")
    if dominant in ("swim", "ride") and share >= 25:
        note += f"；近期以{FAMILY_LABEL[dominant]}为主（{share:.0f}%），目标按这个项目给"
    return {
        "goal": goal,
        "horizon_weeks": horizon,
        "days_per_week": 4 if ctl >= 15 else 3,
        "session_minutes": 60,
        "level": level,
        "constraints": [],
        "target_date": "",
        "note": note,
    }


def build_program(store, report: dict, intake: dict, *, tz_offset: float = 8.0,
                  now: float | None = None) -> dict:
    tz = analytics.TZ(tz_offset)
    now = now or tz.now()
    intake = _normalize_intake(intake)

    goal = GOALS[intake["goal"]]
    family = goal["family"]
    weeks = int(intake["horizon_weeks"])
    days_per_week = int(intake["days_per_week"])
    session_min = int(intake["session_minutes"])
    constraints = set(intake.get("constraints") or [])

    rows = store.all_activity_summaries()
    since4w = tz.start_of_day(now) - 27 * 86400
    recent = [r for r in rows if r["start_time"] >= since4w]

    # 只统计与目标同族的活动：目标跑步却拿骑行量排课，会排出荒唐的周量
    family_recent = [r for r in recent if sport_family(r["sport"]) == family]
    has_family_data = len(family_recent) >= 2
    if has_family_data:
        base_seconds = sum(r["moving_seconds"] or 0 for r in family_recent) / 4.0
        run_km = sum(r["distance_m"] or 0 for r in family_recent) / 4.0 / 1000
    else:
        # 没有同族记录：按可练时间给保守起步量，而不是 0
        base_seconds = session_min * 60 * days_per_week * 0.6
        run_km = base_seconds / 3600 * FAMILY_KM_PER_HOUR.get(family, 9.0)

    settings = store.get_settings()
    hr_max = float(settings.get("hr_max") or 190)
    hr_rest = float(settings.get("hr_rest") or 56)
    thr = _threshold_paces(store)
    thr["swim"] = float(settings.get("swim_pace_s") or 150)
    ctl = float(report.get("fitness", {}).get("ctl") or 0)
    tsb = float(report.get("fitness", {}).get("tsb") or 0)
    risks = {r["key"] for r in report.get("risks", [])}

    # 首周目标：不高于近 4 周均值的 110%；过劳则直接减量
    growth = 0.10 if intake["level"] != "beginner" else 0.06
    if "overreached" in risks or "acwr_very_high" in risks:
        growth = -0.30
    elif "volume_spike" in risks:
        growth = -0.15
    floor_seconds = session_min * 60 * days_per_week * 0.5
    base_target = max(floor_seconds, base_seconds * (1 + growth)) if base_seconds > 0 \
        else floor_seconds

    phases = _phase_table(weeks, ctl, risks, intake)
    goal_m = int(goal.get("distance") or 0) if family == "swim" else 0
    week_rows = []
    for idx in range(1, weeks + 1):
        phase = next(p for p in phases if idx in p["weeks"])
        mult = _week_multiplier(idx, phase, weeks)
        target_seconds = base_target * mult
        target_km = (run_km * mult if run_km > 0
                     else target_seconds / 3600 * FAMILY_KM_PER_HOUR.get(family, 9.0))
        days = _week_days(family, phase["name"], target_seconds, target_km, thr,
                          hr_max, hr_rest, days_per_week, session_min,
                          constraints, idx, weeks, intake, goal_m=goal_m, phase_mult=mult)
        week_rows.append({
            "index": idx,
            "phase": phase["name"],
            "phase_key": phase["key"],
            "focus": phase["focus"],
            "volume_mult": round(mult, 2),
            "target_seconds": round(target_seconds),
            "target_km": round(target_km, 1) if family in ("run", "ride") else None,
            "target_m": (sum(int(d.get("distance_m") or 0) for d in days)
                         if family == "swim" else None),
            "days": days,
            "key": [d["title"] for d in days if d["kind"] in ("quality", "long", "race")],
        })

    return {
        "generated_at": now,
        "intake": intake,
        "goal": {"key": intake["goal"], **goal},
        "level": intake["level"],
        "level_label": LEVEL_LABELS[intake["level"]],
        "horizon_weeks": weeks,
        "phases": [{"name": p["name"], "weeks": p["weeks"], "focus": p["focus"]}
                   for p in phases],
        "weeks": week_rows,
        "start_date": tz.start_of_week(now),
        "threshold": thr,
        "current": {
            "ctl": round(ctl, 1), "tsb": round(tsb, 1),
            "weekly_seconds": round(base_seconds),
            "weekly_km": round(run_km, 1),
        },
        "guardrails": _guardrails(report, intake, constraints),
        "milestones": _milestones(week_rows, goal, intake),
        "warnings": _warnings(has_family_data, family, intake, recent),
        "note": "计划由规则引擎生成，参考周期化与极化训练原则；不是医学建议。",
    }


def _warnings(has_family_data: bool, family: str, intake: dict,
              recent: list[dict]) -> list[str]:
    out = []
    label = FAMILY_LABEL.get(family, "跑步")
    if not has_family_data:
        extra = ("游泳的量按米算，先把「每周 3 次、每次 600–1000 米」跑顺，"
                 "再谈配速。" if family == "swim" else
                 "前两周以建立频率为主，别急着追配速。")
        out.append(f"最近 4 周没有{label}记录，计划按「可练时间」保守起量，{extra}")
    if intake.get("level") == "beginner" and intake.get("horizon_weeks", 0) <= 4:
        out.append("新手 + 4 周以内：这个周期只能建立习惯，不要指望成绩变化。")
    if not recent:
        out.append("库里几乎没有近期活动，负荷曲线与状态判断还不可用。")
    return out


# ---------------------------------------------------------------- 内部

def _normalize_intake(raw: dict) -> dict:
    goal = raw.get("goal") if raw.get("goal") in GOALS else "health"
    try:
        weeks = int(raw.get("horizon_weeks") or 13)
    except (TypeError, ValueError):
        weeks = 13
    weeks = weeks if weeks in (1, 4, 13, 26) else 13
    try:
        days = int(raw.get("days_per_week") or 4)
    except (TypeError, ValueError):
        days = 4
    days = max(2, min(6, days))
    try:
        minutes = int(raw.get("session_minutes") or 60)
    except (TypeError, ValueError):
        minutes = 60
    minutes = minutes if minutes in (30, 45, 60, 90) else 60
    level = raw.get("level") if raw.get("level") in LEVELS else "intermediate"
    cons = [c for c in (raw.get("constraints") or []) if c in CONSTRAINTS]
    return {
        "goal": goal, "horizon_weeks": weeks, "days_per_week": days,
        "session_minutes": minutes, "level": level, "constraints": cons,
        "target_date": str(raw.get("target_date") or ""),
        "note": str(raw.get("note") or "")[:200],
        "question": str(raw.get("question") or "")[:500],
    }


def _phase_table(weeks: int, ctl: float, risks: set, intake: dict) -> list[dict]:
    recovery_first = "overreached" in risks or "acwr_very_high" in risks
    if weeks == 1:
        return [{"key": "week", "name": "执行周" if not recovery_first else "恢复周",
                 "weeks": [1], "focus": "把频率跑通，不追求配速" if not recovery_first
                 else "降负荷，优先睡眠与恢复"}]
    if weeks == 4:
        if recovery_first:
            return [{"key": "recover", "name": "恢复周", "weeks": [1, 2],
                     "focus": "总量 −30%，全部低强度"},
                    {"key": "build", "name": "建设期", "weeks": [3],
                     "focus": "回到正常量，保留一次质量课"},
                    {"key": "deload", "name": "减量周", "weeks": [4],
                     "focus": "吸收前几周，准备下一周期"}]
        return [{"key": "base", "name": "基础期", "weeks": [1], "focus": "建立有氧底子"},
                {"key": "build", "name": "建设期", "weeks": [2], "focus": "加一次质量课"},
                {"key": "peak", "name": "强化期", "weeks": [3], "focus": "质量课延长"},
                {"key": "deload", "name": "减量周", "weeks": [4], "focus": "总量 −30%"}]
    if weeks == 13:
        if recovery_first:
            return [{"key": "recover", "name": "恢复期", "weeks": list(range(1, 4)),
                     "focus": "先把 TSB 拉回 −10 以上"},
                    {"key": "base", "name": "基础期", "weeks": list(range(4, 8)),
                     "focus": "重建有氧与频率"},
                    {"key": "build", "name": "建设期", "weeks": list(range(8, 12)),
                     "focus": "阈值与长距离"},
                    {"key": "deload", "name": "减量周", "weeks": [12, 13],
                     "focus": "吸收训练"}]
        return [{"key": "base", "name": "基础期", "weeks": list(range(1, 5)),
                 "focus": "有氧打底，每周 1 次质量课"},
                {"key": "build", "name": "建设期", "weeks": list(range(5, 10)),
                 "focus": "阈值课加量，长距离延长"},
                {"key": "peak", "name": "强化期", "weeks": list(range(10, 12)),
                 "focus": "比赛配速与专项耐力"},
                {"key": "taper", "name": "减量期", "weeks": [12, 13],
                 "focus": "总量 −40%，保持刺激"}]
    # 26 周
    if recovery_first:
        return [{"key": "recover", "name": "恢复期", "weeks": list(range(1, 4)),
                 "focus": "修复与重建"},
                {"key": "base", "name": "基础期", "weeks": list(range(4, 12)),
                 "focus": "有氧与力量打底"},
                {"key": "build", "name": "建设期", "weeks": list(range(12, 21)),
                 "focus": "阈值 + 长距离"},
                {"key": "peak", "name": "强化期", "weeks": list(range(21, 25)),
                 "focus": "专项强度"},
                {"key": "taper", "name": "减量期", "weeks": [25, 26],
                 "focus": "减量 50%"}]
    return [{"key": "base", "name": "基础期", "weeks": list(range(1, 9)),
             "focus": "有氧底子 + 力量，每周 1 次质量课"},
            {"key": "build", "name": "建设期", "weeks": list(range(9, 19)),
             "focus": "阈值课与长距离同步推进"},
            {"key": "peak", "name": "强化期", "weeks": list(range(19, 25)),
             "focus": "比赛配速与专项耐力"},
            {"key": "taper", "name": "减量期", "weeks": [25, 26],
             "focus": "总量 −50%，保留短促刺激"}]


def _week_multiplier(index: int, phase: dict, weeks: int) -> float:
    key = phase["key"]
    if key == "recover":
        return 0.7
    if key == "taper":
        # 越接近比赛越少
        remain = weeks - index
        return 0.6 if remain <= 1 else 0.7
    if key == "deload":
        return 0.7
    # 每 4 周安排一个减量（若该周不在 taper/recover 内）
    if index % 4 == 0 and key not in ("taper", "recover"):
        return 0.75
    if key == "base":
        return 0.9 + min(0.15, (index - phase["weeks"][0]) * 0.03)
    if key == "build":
        return 1.0 + min(0.15, (index - phase["weeks"][0]) * 0.02)
    if key == "peak":
        return 1.1
    return 1.0


def _week_days(family, phase_name, target_seconds, target_km, thr, hr_max, hr_rest,
               days_per_week, session_min, constraints, week_index, weeks, intake,
               goal_m=0, phase_mult=1.0):
    def zone(lo, hi):
        a = round(hr_rest + (hr_max - hr_rest) * lo)
        b = round(hr_rest + (hr_max - hr_rest) * hi)
        return f"{a}–{b} bpm"

    weekday_plan = {2: [1, 5], 3: [1, 3, 5], 4: [1, 2, 4, 6],
                    5: [1, 2, 3, 5, 6], 6: [1, 2, 3, 4, 5, 6]}.get(days_per_week, [1, 3, 5])

    is_ride = family == "ride"
    is_swim = family == "swim"
    taper = "减量" in phase_name or "恢复" in phase_name
    quality_count = 2 if (intake["level"] != "beginner" and not taper and days_per_week >= 5) else 1
    include_strength = "no_gym" not in constraints and not is_ride
    if is_swim:
        # 游泳 1 小时也就 2 公里左右，阈值不能沿用跑步的 8 km
        include_long = target_km >= 0.7
    else:
        include_long = target_km >= 8 if not is_ride else True

    kinds: list[str] = []
    if include_long:
        kinds.append("long")
    kinds.extend(["quality"] * quality_count)
    if include_strength:
        kinds.append("strength")
    while len(kinds) < days_per_week:
        kinds.append("easy")
    kinds = kinds[:days_per_week]
    order = {"easy": 0, "strength": 1, "quality": 2, "long": 3}
    kinds.sort(key=lambda k: order.get(k, 9))
    if "long" in kinds:
        kinds.remove("long")
        kinds.append("long")

    days = []
    for i, wd in enumerate(weekday_plan[:len(kinds)]):
        kind = kinds[i]
        days.append(_day(kind, wd, target_seconds, target_km, thr, zone, is_ride,
                         session_min, constraints, week_index, weeks, is_swim=is_swim,
                         goal_m=goal_m, goal_key=intake.get("goal", ""),
                         phase_mult=phase_mult))
    used = set(weekday_plan[:len(kinds)])
    for wd in range(7):
        if wd in used:
            continue
        days.append({"weekday": wd, "day": DAY_NAMES[wd], "kind": "rest",
                     "title": "休息", "duration_min": 0, "distance_km": None,
                     "target": "完全休息或 20 分钟拉伸",
                     "why": "恢复日不是浪费：负荷只有配上恢复才会变成能力。"})
    days.sort(key=lambda d: d["weekday"])
    return days


def _day(kind, wd, target_seconds, target_km, thr, zone, is_ride, session_min,
         constraints, week_index, weeks, is_swim=False, goal_m=0, goal_key="",
         phase_mult=1.0):
    day = DAY_NAMES[wd]
    cap = session_min

    if is_swim:
        return _swim_day(kind, wd, day, cap, target_seconds, target_km, thr,
                         goal_m=goal_m, week_index=week_index, weeks=weeks)

    if kind == "easy":
        minutes = max(20, min(cap, round(target_seconds * 0.22 / 60)))
        return {"weekday": wd, "day": day, "kind": "easy",
                "title": "有氧慢跑（Z2）" if not is_ride else "有氧骑（Z2）",
                "duration_min": minutes,
                "distance_km": round(minutes / 60 * (9.0 if not is_ride else 26.0), 1),
                "target": f"心率 {zone(0.60, 0.72)}，能说完整句子的强度",
                "why": "低强度撑起总负荷的大部分，几乎不增加恢复负担。",
                "pace_hint": None if is_ride else (f"约 {_pace(thr.get('easy'))}" if thr.get("easy") else None)}

    if kind == "quality":
        minutes = max(25, min(cap, round(target_seconds * 0.20 / 60)))
        if is_ride:
            r_title, r_target, r_why = _ride_quality(goal_key, zone)
            return {"weekday": wd, "day": day, "kind": "quality", "title": r_title,
                    "duration_min": minutes,
                    "distance_km": round(minutes / 60 * 30.0, 1),
                    "target": r_target,
                    "why": r_why,
                    "pace_hint": None}
        return {"weekday": wd, "day": day, "kind": "quality", "title": "阈值课",
                "duration_min": minutes,
                "distance_km": round(minutes / 60 * 10.5, 1),
                "target": f"热身 12 分钟，主课 20–25 分钟 Z4（{zone(0.82, 0.89)}），放松 10 分钟",
                "why": "阈值能力决定你能「稳多久」，是提成绩性价比最高的一课。",
                "pace_hint": f"主课约 {_pace(thr.get('threshold'))}" if thr.get("threshold") else None}

    if kind == "long":
        if is_ride and goal_key == "ride_century":
            span = max(1, weeks - 1)
            p = min(1.0, max(0.0, (week_index - 1) / span))
            km = round(min(100.0, (45.0 + 55.0 * p) * phase_mult), 1)
            return {"weekday": wd, "day": day, "kind": "long", "title": "百公里长距离",
                    "duration_min": max(60, round(km / 26.0 * 60)),
                    "distance_km": km,
                    "target": "平路为主，全程 Z2，每 30–40 分钟补水/进食，后 20 km 再提速",
                    "why": "百公里拼的是后半程还能吃、还能踩，长度必须真坐满。",
                    "pace_hint": None}
        if is_ride and goal_key == "ride_climb":
            km = round(max(20.0, min(90.0, target_km * 0.40)), 1)
            return {"weekday": wd, "day": day, "kind": "long", "title": "爬升长距离",
                    "duration_min": max(50, round(km / 22.0 * 60)),
                    "distance_km": km,
                    "target": "挑 1–2 个长坡的路书，上坡 Z3、下坡 Z1，累计爬升逐周加 100–200 m",
                    "why": "爬坡专项的耐力要靠在坡上的稳定输出，不是平路刷里程。",
                    "pace_hint": None}
        ratio = 0.30 if target_km >= 30 else 0.25
        minutes = max(40, min(cap * 1.6, round(target_seconds * ratio / 60)))
        km = round(max(6.0, target_km * ratio), 1)
        return {"weekday": wd, "day": day, "kind": "long",
                "title": "长距离" if not is_ride else "长距离骑行",
                "duration_min": minutes,
                "distance_km": km,
                "target": f"全程心率 {zone(0.58, 0.72)}，配速比日常慢 15–30 秒",
                "why": "长距离训练脂肪利用、肌腱韧性与后程抗掉速，别的课替代不了。",
                "pace_hint": f"约 {_pace(thr.get('easy'))} 或更慢" if thr.get("easy") else None}

    if kind == "strength":
        return {"weekday": wd, "day": day, "kind": "strength", "title": "力量训练",
                "duration_min": 35, "distance_km": None,
                "target": "深蹲 / 硬拉 / 弓步 / 提踵 各 3 组，核心 2 个动作",
                "why": "力量是抗伤和跑步经济性的底层能力，不会明显挤占有氧恢复。",
                "pace_hint": None}
    return {}


def _ride_quality(goal_key, zone):
    """骑行质量课按目标分化：阈值 / 甜区 / 爬坡 / 节奏 / 踏频。

    这些课本来就该不一样：爬坡要短坡重复，百公里要稳态节奏，
    打底阶段先练踩踏，只有「长距离」目标才用经典阈值间歇。
    """
    if goal_key == "ride_climb":
        return ("爬坡重复",
                f"热身 15 分钟，主课 4×6 分钟 6–8% 坡（{zone(0.82, 0.90)}），坡底轻松骑下当恢复",
                "爬坡比的是功率体重比，短坡重复比重齿硬顶更安全有效。")
    if goal_key == "ride_ftp":
        return ("甜区 2×20",
                f"热身 10 分钟，2×20 分钟 Z3–Z4（{zone(0.76, 0.88)}），组间 5 分钟轻松骑",
                "甜区把「能扛多久」往上抬，比反复冲阈值更可持续。")
    if goal_key == "ride_century":
        return ("节奏骑（配速演练）",
                f"热身 10 分钟，主课 40 分钟稳定 Z3（{zone(0.70, 0.80)}），中途练补给",
                "百公里的关键在后 30 km 还能维持节奏，先把稳态练顺。")
    if goal_key == "ride_base":
        return ("踏频与技术",
                f"热身 10 分钟，3×8 分钟 90–100 rpm（{zone(0.65, 0.78)}），组间 3 分钟",
                "先把踩踏效率和有氧底子打稳，强度不着急。")
    return ("阈值间歇",
            f"热身 10 分钟，主课 3×8 分钟 Z4（{zone(0.82, 0.89)}），组间 4 分钟",
            "阈值功率/心率是长距离骑行最直接的能力指标。")


def _pace100(seconds: float | None) -> str | None:
    if not seconds:
        return None
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}/100m"


def _swim_day(kind, wd, day, cap, target_seconds, target_km, thr,
              *, goal_m=0, week_index=1, weeks=4):
    """游泳课：按 100 米配速与 25 米池折算距离。

    游泳的量和跑步不是一回事：1 小时大约 2 公里，所以距离用「米」表示，
    课程也以「组数 × 距离」写，而不是跑量。目标里有比赛距离时，
    长距离课从目标距离的 55% 起，逐步加到 110%，而不是凭训练时间瞎涨。
    """
    pace = float(thr.get("swim") or 150)  # 秒/100m

    def meters(minutes, pct=1.0):
        m = minutes * 60 / pace * 100 * pct
        return int(max(200, round(m / 25) * 25))  # 25 米池，取整到一趟

    # 没有比赛距离（如「学会连续游」）时，别按训练时长把量推到 4 km/周
    NO_GOAL_CAP = {"easy": 600, "quality": 1000, "long": 800}

    def ramp(base_m, low, high):
        """把目标距离摊到整个周期：低点是首周比例，高点是末周比例。"""
        span = max(1, weeks - 1)
        p = min(1.0, max(0.0, (week_index - 1) / span))
        return int(max(400, round(base_m * (low + (high - low) * p) / 25) * 25))

    if kind == "long":
        if goal_m:
            m = ramp(goal_m, 0.55, 1.10)
            minutes = max(20, min(int(round(cap * 1.6)), int(round(m / 100 * pace / 60)) + 10))
        else:
            m = meters(max(35, min(round(cap * 1.5), round(target_seconds * 0.30 / 60))))
            minutes = max(35, min(round(cap * 1.5), round(target_seconds * 0.30 / 60)))
        if not goal_m:
            m = min(m, NO_GOAL_CAP["long"])
            minutes = max(20, min(minutes, int(round(m / 100 * pace / 60)) + 10))
        stop = 400 if m >= 800 else 200
        return {"weekday": wd, "day": day, "kind": "long",
                "title": "长距离连续游",
                "duration_min": minutes,
                "distance_m": m,
                "target": (f"连续游 {m} 米，每 {stop} 米停 20 秒；前 1/3 放松、"
                           "中段稳住、最后 200 米提速；用双侧换气"),
                "why": "连续游练的是有氧底子与动作稳定性，间歇课替代不了。",
                "pace_hint": _pace100(pace + 10)}

    if kind == "quality":
        minutes = max(30, min(cap, round(target_seconds * 0.22 / 60)))
        reps = 8 if minutes <= 45 else 10
        qm = min(meters(minutes), NO_GOAL_CAP["quality"] if not goal_m else 10**9)
        return {"weekday": wd, "day": day, "kind": "quality",
                "title": "间歇组（配速刺激）",
                "duration_min": minutes,
                "distance_m": qm,
                "target": (f"热身 300 米；主课 {reps}×50 米冲刺（休息 20 秒）"
                           "或 6×100 米（休息 30 秒），配速贴近目标；放松 200 米"),
                "why": "短间歇把划水力量和心肺顶起来，是提配速最直接的一课。",
                "pace_hint": _pace100(pace - 5)}

    if kind == "strength":
        return {"weekday": wd, "day": day, "kind": "strength",
                "title": "陆上力量 + 肩部灵活性",
                "duration_min": 30, "distance_m": None,
                "target": ("弹力带划船 / 俯身飞鸟 / 高位下拉 各 3 组 12 次；"
                           "核心 2 个动作；肩袖拉伸与胸椎旋转 5 分钟"),
                "why": "游泳的推进力来自背与核心，肩袖力量还能防「游泳肩」。",
                "pace_hint": None}

    if goal_m:
        m = ramp(goal_m, 0.28, 0.50)
        minutes = max(20, min(cap, int(round(m / 100 * pace / 60)) + 12))
    else:
        m = meters(max(25, min(cap, round(target_seconds * 0.26 / 60))))
        minutes = max(25, min(cap, round(target_seconds * 0.26 / 60)))
    if not goal_m:
        m = min(m, NO_GOAL_CAP["easy"])
        minutes = max(20, min(minutes, int(round(m / 100 * pace / 60)) + 12))
    return {"weekday": wd, "day": day, "kind": "easy",
            "title": "技术 + 有氧游",
            "duration_min": minutes,
            "distance_m": m,
            "target": ("热身 200 米；技术分解 4×50 米（浮板打腿 / 单臂划手 / 侧身换气）；"
                       f"轻松游完剩下的距离（总计约 {m} 米），每 50 米休息 15 秒"),
            "why": "技术不解决，游得越久错得越熟；低强度也几乎不占恢复。",
            "pace_hint": _pace100(pace + 15)}


def _pace(seconds: float | None) -> str | None:
    if not seconds:
        return None
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}/km"


def _threshold_paces(store) -> dict:
    prs = analytics.personal_records(store, limit=0)
    run = [p for p in prs if p["family"] == "run"]
    five = next((p for p in run if p["distance_m"] == 5000), None)
    ten = next((p for p in run if p["distance_m"] == 10000), None)
    base = None
    if five:
        base = five["seconds"] / 5.0 * 1.03
    elif ten:
        base = ten["seconds"] / 10.0 * 0.98
    elif run:
        p = run[0]
        base = p["seconds"] / (p["distance_m"] / 1000) * 1.10
    if not base:
        return {}
    return {"threshold": round(base), "easy": round(base * 1.28),
            "marathon": round(base * 1.12), "interval": round(base * 0.94)}


def _guardrails(report, intake, constraints) -> list[str]:
    out = ["周总量增幅不超过 10%；连续两周超标就先减一周。",
           "每 4 周安排一个减量周，别一路加到底。"]
    if "knee" in constraints:
        out.append("膝盖不适期间禁止跑坡与间歇；疼痛持续 48 小时以上先停练就医。")
    if "night_only" in constraints:
        out.append("夜跑降到 Z2，配反光装备；睡前 2 小时结束高强度。")
    if "time_frag" in constraints:
        out.append("碎片时间优先做 20–30 分钟 Z2，质量课留到周末整块时间。")
    risks = {r["key"] for r in report.get("risks", [])}
    if "overreached" in risks:
        out.append("TSB 回到 −10 以上之前，不加任何高强度课。")
    out.append("本计划是规则生成，不替代医生或线下教练；出现胸痛、眩晕立即停止。")
    return out


def _milestones(week_rows: list[dict], goal: dict, intake: dict) -> list[dict]:
    out = []
    total = len(week_rows)
    distance = goal.get("distance")
    if distance and total:
        target_km = distance / 1000
        for w in week_rows:
            longest = max((d.get("distance_km") or 0 for d in w["days"]), default=0)
            if longest >= target_km * 0.8:
                out.append({"week": w["index"],
                            "text": f"长距离达到 {longest} km，接近目标距离的 80%"})
                break
    if total:
        out.append({"week": max(1, total - 1), "text": "进入减量期，开始复盘补给与装备"})
    return out
