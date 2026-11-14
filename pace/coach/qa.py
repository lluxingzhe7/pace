"""教练问答（规则引擎，不接大模型）。

设计取舍
--------
* 用户没告诉目标之前，**不主动给课程**：只回答"我需要你先知道什么"。
* 每个回答都先引用现有数据的真实数字，再给可执行动作，最后说清边界。
* 没有可用数据时，明确说"这项判断缺什么"，不编。
"""
from __future__ import annotations

from .. import analytics
from ..parsers import sport_family

TOPICS = {
    "pace": ["配速", "快", "成绩", "pb", "pr", "5公里", "5km", "10公里", "10km",
             "半马", "全马", "马拉松", "提速", "sub"],
    "fat_loss": ["减脂", "减肥", "瘦", "体重", "掉秤"],
    "recovery": ["恢复", "累", "疲劳", "睡眠", "休息", "腿沉", "没力", "状态"],
    "injury": ["膝", "疼", "痛", "伤", "拉伤", "胫骨", "跟腱", "足底"],
    "schedule": ["几次", "频率", "时间", "忙", "几天", "安排", "每周"],
    "nutrition": ["吃", "营养", "补给", "碳水", "蛋白", "喝", "能量胶"],
    "strength": ["力量", "深蹲", "核心", "健身房", "器械"],
    "taper": ["比赛", "赛前", "减量", "taper", "备赛"],
    "zones": ["心率", "区间", "zone", "z2", "阈值", "最大心率"],
    "plan": ["计划", "周期", "多久", "几个月", "安排训练"],
}


def classify(text: str) -> str:
    q = (text or "").lower()
    best, best_hits = "general", 0
    for topic, words in TOPICS.items():
        hits = sum(1 for w in words if w in q)
        if hits > best_hits:
            best, best_hits = topic, hits
    return best


def answer(store, report: dict, question: str, intake: dict | None = None) -> dict:
    intake = intake or {}
    topic = classify(question)
    ctx = _context(store, report, intake)
    handler = _HANDLERS.get(topic, _general)
    result = handler(ctx, question)
    result["topic"] = topic
    result.setdefault("caveat", "以上是规则建议，不替代医生或线下教练。")
    return result


# ---------------------------------------------------------------- 上下文

def _context(store, report: dict, intake: dict) -> dict:
    fit = report.get("fitness", {}) or {}
    load = report.get("load", {}) or {}
    vol = report.get("volume", {}) or {}
    inv = report.get("inventory", {}) or {}
    prs = analytics.personal_records(store, limit=0)
    run = [p for p in prs if p["family"] == "run"]
    five = next((p for p in run if p["distance_m"] == 5000), None)
    ten = next((p for p in run if p["distance_m"] == 10000), None)
    half = next((p for p in run if p["distance_m"] == 21097.5), None)

    settings = store.get_settings()
    hr_max = float(settings.get("hr_max") or 190)
    hr_rest = float(settings.get("hr_rest") or 56)
    reserve = max(1.0, hr_max - hr_rest)

    def zone(lo, hi):
        return f"{round(hr_rest + reserve * lo)}–{round(hr_rest + reserve * hi)} bpm"

    threshold = None
    if five:
        threshold = five["seconds"] / 5.0 * 1.03
    elif ten:
        threshold = ten["seconds"] / 10.0 * 0.98

    return {
        "report": report, "intake": intake,
        "ctl": float(fit.get("ctl") or 0), "atl": float(fit.get("atl") or 0),
        "tsb": float(fit.get("tsb") or 0),
        "acwr": load.get("acwr"), "monotony": load.get("monotony"),
        "weekly_km": float(vol.get("current_km") or 0),
        "weekly_hours": float(vol.get("current_seconds") or 0) / 3600,
        "sessions": int(vol.get("sessions") or 0),
        "pr_5k": five, "pr_10k": ten, "pr_half": half,
        "threshold": threshold,
        "zone": zone,
        "has_hr": bool(inv.get("has_hr")),
        "has_power": bool(inv.get("has_power")),
        "risks": {r["key"] for r in report.get("risks", [])},
        "gaps": {g["key"] for g in report.get("gaps", [])},
        "goal": intake.get("goal"),
        "horizon": intake.get("horizon_weeks"),
        "days": intake.get("days_per_week"),
        "constraints": set(intake.get("constraints") or []),
    }


def _fmt(seconds):
    if not seconds:
        return "—"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _pace(seconds):
    return f"{_fmt(seconds)}/km" if seconds else "—"


def _baseline(ctx) -> list[dict]:
    out = [{"label": "CTL（体能）", "value": f"{ctx['ctl']:.0f}"},
           {"label": "TSB（状态）", "value": f"{ctx['tsb']:+.0f}"},
           {"label": "近 8 周周均", "value": f"{ctx['weekly_km']:.1f} km / {ctx['weekly_hours']:.1f} h"}]
    if ctx["pr_5k"]:
        out.append({"label": "5km 最好", "value": _fmt(ctx["pr_5k"]["seconds"])})
    return out


def _no_goal(ctx) -> dict:
    return {
        "title": "先把目标告诉我，我再排课",
        "sections": [
            {"h": "现在的情况", "p":
                f"你的数据我已经读了：CTL {ctx['ctl']:.0f}、TSB {ctx['tsb']:+.0f}、"
                f"近 8 周周均 {ctx['weekly_km']:.1f} 公里。但**目标不同，排法完全相反**——"
                "备赛要加长距离，减脂要控总量配力量，恢复期则要先减量。"},
            {"h": "我需要你回答三件事", "p":
                "① 你在准备什么（5km / 10km / 半马 / 全马 / 骑行长距离 / 减脂 / 只是健康）\n"
                "② 周期多长（1 周 / 1 个月 / 1 季度 / 半年）\n"
                "③ 每周能练几天、每次大概多久"},
            {"h": "为什么不能先给课表", "p":
                "同一个 CTL 的人，目标 5km 和目标是全马，周跑量能差 3 倍。"
                "先出课表等于让你按别人的目标练。"},
        ],
        "actions": ["在「目标与条件」里选好，再点生成计划"],
        "data": _baseline(ctx),
    }


def _general(ctx, q) -> dict:
    if not ctx["goal"]:
        return _no_goal(ctx)
    return {
        "title": "这个问题我需要更具体一点",
        "sections": [
            {"h": "你的现状", "p":
                f"CTL {ctx['ctl']:.0f}、TSB {ctx['tsb']:+.0f}，周均 {ctx['weekly_km']:.1f} 公里。"},
            {"h": "可以这样问", "p":
                "「5 公里怎么跑到 25 分」「每周只有 3 天怎么安排」「膝盖不舒服还能练吗」"
                "「比赛前两周怎么减量」「减脂要跑多少」——这类具体问题我能结合数据回答。"},
        ],
        "actions": ["换个更具体的问法，或直接生成计划"],
        "data": _baseline(ctx),
    }


def _pace(ctx, q) -> dict:
    if not ctx["pr_5k"] and not ctx["pr_10k"]:
        return {
            "title": "还没有可用的成绩基准",
            "sections": [{"h": "缺什么", "p":
                "要给你配速目标，至少需要一次全力跑的 5km 或 10km 成绩。"
                "现在库里没有这样的记录，硬给配速只会是编的。"}],
            "actions": ["先做一次 5km 计时跑（热身 15 分钟，全力跑完，放松 10 分钟）"],
            "data": _baseline(ctx),
        }
    thr = ctx["threshold"]
    easy = thr * 1.28
    interval = thr * 0.94
    marathon = thr * 1.12
    target = None
    if ctx["pr_5k"]:
        target = ctx["pr_5k"]["seconds"] * 0.97
    sections = [
        {"h": "你的基准", "p":
            f"5km 最好 {_fmt(ctx['pr_5k']['seconds']) if ctx['pr_5k'] else '—'}，"
            f"据此推算阈值配速约 {_pace(thr)}。"},
        {"h": "四档训练配速（Daniels 体系简化）", "p":
            f"轻松跑 {_pace(easy)} ｜ 马拉松配速 {_pace(marathon)} ｜ "
            f"阈值 {_pace(thr)} ｜ 间歇 {_pace(interval)}"},
        {"h": "怎么练", "p":
            "每周 1 次阈值（20–30 分钟连续或 3×8 分钟）+ 1 次长距离 + 其余全部轻松跑。"
            "80% 的里程要在轻松区，否则速度课恢复不过来。"},
        {"h": "别做什么", "p":
            "不要每次都跑到「有点喘但能撑」——那是最容易卡平台的强度。"
            "要么更慢，要么更快，中间地带留给比赛。"},
    ]
    actions = [f"本周先按 {_pace(easy)} 跑 2 次，确认能边跑边说话",
               f"周末做 1 次阈值：热身 12 分钟 + 20 分钟 {_pace(thr)} + 放松 10 分钟"]
    if target:
        actions.append(f"目标：5km 跑到 {_fmt(target)}（比现在快约 3%）")
    if "no_quality" in {f["key"] for f in ctx["report"].get("findings", [])}:
        actions.insert(0, "你目前几乎没有质量课，先补上每周 1 次阈值再谈提速")
    return {"title": "配速与提速：按阈值推四档", "sections": sections,
            "actions": actions, "data": _baseline(ctx)}


def _fat_loss(ctx, q) -> dict:
    return {
        "title": "减脂：靠总量和力量，不靠饿",
        "sections": [
            {"h": "你现在的量", "p":
                f"周均 {ctx['weekly_km']:.1f} 公里 / {ctx['weekly_hours']:.1f} 小时。"
                + ("量已经不小，再往上加容易伤。" if ctx["weekly_km"] > 35 else
                   "量偏小，先加到每周 3–4 次有氧。")},
            {"h": "有效做法", "p":
                "① 有氧保持 Z2（" + ctx["zone"](0.60, 0.72) + "），时间比强度重要；"
                "② 每周 2 次力量，保住肌肉；③ 每天蛋白质 1.6 g/kg 体重；"
                "④ 热量缺口控制在 300–500 kcal，别超过。"},
            {"h": "常见误区", "p":
                "空腹长跑、只做高强度、每天称体重——这三样最容易掉肌肉和反弹。"},
        ],
        "actions": ["每周固定 2 次 35 分钟力量（深蹲/硬拉/弓步/核心）",
                    "有氧按时间而不是距离：先稳定每周 150–200 分钟",
                    "每周同一时间称一次体重，看 4 周趋势"],
        "data": _baseline(ctx),
    }


def _recovery(ctx, q) -> dict:
    over = ctx["tsb"] < -20 or "overreached" in ctx["risks"]
    return {
        "title": "恢复：先看 TSB，再谈吃什么补剂",
        "sections": [
            {"h": "你的状态", "p":
                f"TSB {ctx['tsb']:+.0f}（体能 {ctx['ctl']:.0f}、疲劳 {ctx['atl']:.0f}）。"
                + ("已经偏低，属于该减量的区间。" if over else
                   "在正常范围，不需要额外休息日。")},
            {"h": "优先级", "p":
                "睡眠 > 吃够 > 主动恢复 > 补剂。每天 7.5–9 小时睡眠对恢复的影响，"
                "比任何泡沫轴和补剂都大。"},
            {"h": "判断标准", "p":
                "静息心率连续 3 天比平时高 5 bpm 以上、或同样配速心率高 8 bpm，"
                "就是身体在说「今天别练强度」。"},
        ],
        "actions": (["这周总量砍 30%，全部 Z1–Z2", "至少 2 个完全休息日"]
                    if over else ["保持节奏，睡眠固定在同一个时间", "每周留 1 天完全休息"]),
        "data": _baseline(ctx),
    }


def _injury(ctx, q) -> dict:
    return {
        "title": "疼痛先停，别用训练量赌",
        "sections": [
            {"h": "先分清", "p":
                "肌肉酸痛（延迟 24–48 小时、对称、按压缓解）可以继续低强度；"
                "关节/骨头/肌腱的**点状痛**、跑步中加重、第二天更痛——这是停练信号。"},
            {"h": "现在该做", "p":
                "停止跑坡和间歇，改骑车/游泳维持有氧；冰敷 15 分钟 × 3 次/天；"
                "48 小时无好转就去骨科或运动医学科。"},
            {"h": "长期", "p":
                "跑步相关膝痛多数和臀中肌/股四头肌力量、以及周量增幅过快有关。"
                "把周增幅压到 10% 以内，加 2 次力量，比换鞋更有用。"},
        ],
        "actions": ["暂停速度课与下坡跑", "每天 10 分钟臀桥、侧向走、提踵",
                    "疼痛持续超过 2 周，先做影像检查再恢复训练"],
        "caveat": "我不是医生，疼痛请以临床诊断为准。",
        "data": _baseline(ctx),
    }


def _schedule(ctx, q) -> dict:
    days = ctx["days"] or 4
    plan = {2: "1 次质量课 + 1 次长距离", 3: "1 次质量 + 1 次长距离 + 1 次轻松",
            4: "1 次质量 + 1 次长距离 + 2 次轻松",
            5: "2 次质量 + 1 次长距离 + 2 次轻松", 6: "2 次质量 + 1 次长距离 + 3 次轻松"}.get(days, "1 次质量 + 1 次长距离 + 2 次轻松")
    return {
        "title": f"每周 {days} 天怎么排",
        "sections": [
            {"h": "推荐结构", "p": plan + "。质量课和长距离之间至少隔 1 天。"},
            {"h": "时间紧怎么办", "p":
                "质量课优先（20 分钟阈值也有效），轻松跑可以拆成 2×15 分钟。"
                "别把两次轻松跑合并成一次很长的——恢复负担不一样。"},
            {"h": "你现在的量", "p":
                f"周均 {ctx['weekly_km']:.1f} 公里 / {ctx['sessions']} 次，"
                "按上面的结构直接套用即可。"},
        ],
        "actions": ["把质量课固定在体力最好的那天（通常是周三或周四）",
                    "长距离放周末，前一天不要做力量"],
        "data": _baseline(ctx),
    }


def _nutrition(ctx, q) -> dict:
    return {
        "title": "吃：日常够用就行，比赛才需要精细",
        "sections": [
            {"h": "日常", "p":
                "碳水按训练量走：轻松日 3–4 g/kg，质量日/长距离日 5–6 g/kg；"
                "蛋白质 1.6 g/kg 固定；脂肪补足其余热量。"},
            {"h": "训练前后", "p":
                "课前 1–2 小时少量碳水（香蕉/面包）；课后 1 小时内补 20–30 g 蛋白 + 碳水。"},
            {"h": "长距离/比赛", "p":
                "超过 75 分钟开始补碳水，每小时 30–60 g；比赛前 3 天把碳水提到 7–8 g/kg。"},
        ],
        "actions": ["长距离课按比赛补给演练：每 30–40 分钟一支胶 + 少量水",
                    "比赛当天不要尝试新食物"],
        "data": _baseline(ctx),
    }


def _strength(ctx, q) -> dict:
    return {
        "title": "力量：两周就能感觉到差别",
        "sections": [
            {"h": "为什么", "p":
                "力量训练提升跑步经济性（同样配速更省力）、降低受伤率，"
                "而且不会像多跑一次那样增加关节负担。"},
            {"h": "最小方案（每次 30 分钟，每周 2 次）", "p":
                "深蹲 3×8、罗马尼亚硬拉 3×8、弓步 3×10、提踵 3×15、平板支撑 3×45 秒。"},
            {"h": "注意", "p":
                "放在轻松日或质量课之后，别放在长距离前一天；先练动作质量再加重量。"},
        ],
        "actions": ["本周安排 2 次，间隔至少 2 天", "重量以「最后 2 次有点吃力但动作不变形」为准"],
        "data": _baseline(ctx),
    }


def _taper(ctx, q) -> dict:
    return {
        "title": "赛前减量：砍量不砍强度",
        "sections": [
            {"h": "时间线", "p":
                "半马：赛前 2 周开始减量，总量 −40%；全马：赛前 3 周 −20%、2 周 −40%、"
                "最后一周 −60%。最后 3 天只做 20–30 分钟轻松跑 + 几次短加速。"},
            {"h": "为什么要保留强度", "p":
                "只减量不减强度，能保住神经肌肉的「锐度」；全砍掉会让比赛日发木。"},
            {"h": "你现在的状态", "p":
                f"TSB {ctx['tsb']:+.0f}。理想是比赛日 TSB 在 +5 到 +15 之间，"
                + ("现在偏低，减量要更早开始。" if ctx["tsb"] < -10 else
                   "现在合适，按标准减量即可。")},
        ],
        "actions": ["赛前 10 天做最后一次长距离（不超过目标距离的 70%）",
                    "赛前 3 天碳水提到 7–8 g/kg，睡够"],
        "data": _baseline(ctx),
    }


def _zones(ctx, q) -> dict:
    if not ctx["has_hr"]:
        return {
            "title": "没有心率数据，区间只能推算",
            "sections": [{"h": "缺什么", "p":
                "区间要基于你的最大心率/静息心率。现在库里没有足够心率记录，"
                "下面是用设置里的默认值推算的，只能当参考。"}],
            "actions": ["跑步时戴心率带或光电心率，至少记录 8 周"],
            "data": _baseline(ctx),
        }
    return {
        "title": "五个区间怎么用",
        "sections": [
            {"h": "区间定义（心率储备法）", "p":
                f"Z1 <{ctx['zone'](0, 0.60)}｜Z2 {ctx['zone'](0.60, 0.70)}｜"
                f"Z3 {ctx['zone'](0.70, 0.80)}｜Z4 {ctx['zone'](0.80, 0.90)}｜"
                f"Z5 >{ctx['zone'](0.90, 1.0)}"},
            {"h": "剂量", "p":
                "Z1–Z2 占 75–85%（轻松跑、长距离）；Z4 每周 20–40 分钟（阈值课）；"
                "Z5 每周不超过 10 分钟（间歇）。Z3 尽量少待。"},
            {"h": "常见错误", "p":
                "把「有点累」当有氧——那通常是 Z3，练多了既不够慢也不够快。"},
        ],
        "actions": ["轻松跑用「能说完整句子」作标准，比看表更准",
                    "质量课按时间不按距离，避免为了凑公里数降强度"],
        "data": _baseline(ctx),
    }


def _plan_topic(ctx, q) -> dict:
    if not ctx["goal"]:
        return _no_goal(ctx)
    return {
        "title": "周期怎么定",
        "sections": [
            {"h": "按目标定周期", "p":
                "5km/10km：8–12 周；半马：12–16 周；全马：16–20 周；"
                "纯健康：以 4 周为一个循环，长期滚动。"},
            {"h": "结构", "p":
                "基础期（有氧+力量）→ 建设期（阈值+长距离）→ 强化期（比赛配速）→ "
                "减量期（赛前 2–3 周）。每 4 周插一个减量周。"},
        ],
        "actions": ["在「目标与条件」里选周期，我按这个结构排到每一周"],
        "data": _baseline(ctx),
    }


_HANDLERS = {
    "pace": _pace, "fat_loss": _fat_loss, "recovery": _recovery,
    "injury": _injury, "schedule": _schedule, "nutrition": _nutrition,
    "strength": _strength, "taper": _taper, "zones": _zones,
    "plan": _plan_topic, "general": lambda ctx, q: (
        _no_goal(ctx) if not ctx["goal"] else _general(ctx, q)),
}
