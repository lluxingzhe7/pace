"""多周期计划与教练问答测试。"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import analytics
from pace.coach import evaluate, program, qa
from pace.store import open_store
from pace.summary_build import build_from_summary

PASSED = 0
FAILED: list[str] = []
NOW = 1_794_000_000.0
TMP = os.path.join(ROOT, ".program-test")


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def make_store(name: str, run_weeks: int = 8) -> "object":
    path = os.path.join(TMP, name)
    store = open_store(path)
    for w in range(run_weeks):
        base = NOW - (run_weeks - w) * 7 * 86400
        for offset, sec, km, hr in ((0, 2400, 7.0, 145), (2, 2700, 8.0, 160),
                                    (4, 4200, 14.0, 143)):
            built = build_from_summary({
                "sport": "running", "start_time": base + offset * 86400,
                "distance_m": km * 1000, "moving_seconds": sec,
                "elevation_m": km * 8, "avg_hr": hr, "max_hr": hr + 18,
            })
            store.upsert_activity({
                "source": "test", "external_id": f"{name}-{w}-{offset}",
                "filename": "t.csv", "sport": "running", "title": "run",
                "summary": built["summary"], "streams": {}, "route": [],
            })
    return store


def test_horizons():
    print("\n[1] 四种周期的结构")
    store = make_store("a.db")
    report = evaluate.evaluate(store, tz_offset=8, now=NOW)
    for weeks, expect_phases in ((1, 1), (4, 4), (13, 4), (26, 4)):
        p = program.build_program(store, report, {
            "goal": "half_marathon", "horizon_weeks": weeks,
            "days_per_week": 4, "session_minutes": 60, "level": "intermediate",
        }, now=NOW)
        check(f"{weeks} 周：周数正确", len(p["weeks"]) == weeks, str(len(p["weeks"])))
        covered = sorted(w for ph in p["phases"] for w in ph["weeks"])
        check(f"{weeks} 周：阶段覆盖完整", covered == list(range(1, weeks + 1)),
              f"{covered[:3]}...{covered[-2:]}")
        check(f"{weeks} 周：阶段数", len(p["phases"]) == expect_phases, str(len(p["phases"])))
        check(f"{weeks} 周：每天 7 条", all(len(w["days"]) == 7 for w in p["weeks"]))
    store.close()


def test_volume_curve():
    print("\n[2] 负荷曲线")
    store = make_store("b.db")
    report = evaluate.evaluate(store, tz_offset=8, now=NOW)
    p = program.build_program(store, report, {
        "goal": "marathon", "horizon_weeks": 26, "days_per_week": 5,
        "session_minutes": 60, "level": "intermediate",
    }, now=NOW)
    vols = [w["target_seconds"] for w in p["weeks"]]
    check("总量有起伏", max(vols) > min(vols) * 1.15, f"{min(vols)}–{max(vols)}")
    taper = [w for w in p["weeks"] if w["phase_key"] == "taper"]
    check("减量期低于峰值", taper and max(w["target_seconds"] for w in taper) < max(vols),
          str([w["target_seconds"] for w in taper]))
    check("至少有一个长距离", any("long" in [d["kind"] for d in w["days"]] for w in p["weeks"]))
    check("每 4 周有减量", any(w["volume_mult"] <= 0.8 for w in p["weeks"][3::4]),
          str([round(w["volume_mult"], 2) for w in p["weeks"][3::4]]))
    store.close()


def test_family_mismatch():
    print("\n[3] 运动族不匹配")
    store = make_store("c.db", run_weeks=1)   # 只有 1 周跑步
    report = evaluate.evaluate(store, tz_offset=8, now=NOW)
    p = program.build_program(store, report, {
        "goal": "sportive", "horizon_weeks": 4,
        "days_per_week": 4, "session_minutes": 60, "level": "beginner",
    }, now=NOW)
    check("目标骑行但只有跑步数据时给出提示",
          any("骑行" in w for w in p["warnings"]), str(p["warnings"]))
    check("仍能生成计划", len(p["weeks"]) == 4)
    store.close()


def test_days_and_constraints():
    print("\n[4] 天数与限制条件")
    store = make_store("d.db")
    report = evaluate.evaluate(store, tz_offset=8, now=NOW)
    for days in (2, 3, 4, 5, 6):
        p = program.build_program(store, report, {
            "goal": "improve_pace", "horizon_weeks": 4, "days_per_week": days,
            "session_minutes": 45, "level": "intermediate",
        }, now=NOW)
        w = p["weeks"][0]
        training = [d for d in w["days"] if d["kind"] != "rest"]
        check(f"每周 {days} 天被遵守", len(training) == days, str(len(training)))

    p2 = program.build_program(store, report, {
        "goal": "improve_pace", "horizon_weeks": 4, "days_per_week": 4,
        "session_minutes": 60, "level": "intermediate",
        "constraints": ["no_gym", "knee"],
    }, now=NOW)
    kinds = [d["kind"] for w in p2["weeks"] for d in w["days"]]
    check("无器械时不出力量课", "strength" not in kinds, str(set(kinds)))
    check("膝盖问题写进护栏", any("膝" in g for g in p2["guardrails"]),
          str(p2["guardrails"]))
    store.close()


def test_qa():
    print("\n[5] 教练问答")
    store = make_store("e.db")
    report = evaluate.evaluate(store, tz_offset=8, now=NOW)

    a = qa.answer(store, report, "我膝盖有点疼还能练吗", {"goal": "improve_pace"})
    check("识别伤病话题", a["topic"] == "injury", a["topic"])
    check("伤病回答含停练建议", any("停" in x for x in a["actions"]), str(a["actions"]))
    check("伤病有医疗免责", "医生" in a.get("caveat", ""), a.get("caveat", ""))

    a2 = qa.answer(store, report, "每周只有三天怎么安排", {"goal": "improve_pace", "days_per_week": 3})
    check("识别频率话题", a2["topic"] == "schedule", a2["topic"])
    check("频率回答提到 3 天", "3" in a2["title"], a2["title"])

    a3 = qa.answer(store, report, "减脂要跑多少", {"goal": "lose_weight"})
    check("识别减脂话题", a3["topic"] == "fat_loss", a3["topic"])

    a4 = qa.answer(store, report, "随便聊聊", {})
    check("没目标时先要目标", a4["topic"] == "general" and "目标" in a4["title"], a4["title"])

    a5 = qa.answer(store, report, "5公里怎么跑进25分", {"goal": "improve_pace"})
    check("提速回答带数据", bool(a5.get("data")), str(len(a5.get("data") or [])))
    # 没有 5km/10km 成绩时应诚实说"缺基准"，而不是编配速
    has_baseline = len(a5["sections"]) >= 2
    honest_missing = "基准" in a5["title"] or "缺" in a5["title"]
    check("提速回答有内容且不编造", has_baseline or honest_missing, a5["title"])
    store.close()


def test_intake_normalize():
    print("\n[6] 输入容错")
    raw = {"goal": "not-a-goal", "horizon_weeks": 999, "days_per_week": 99,
           "session_minutes": 7, "level": "god", "constraints": ["nope", "no_gym"]}
    n = program._normalize_intake(raw)
    check("非法目标回退", n["goal"] == "health", n["goal"])
    check("非法周期回退", n["horizon_weeks"] == 13, str(n["horizon_weeks"]))
    check("天数夹紧到 6", n["days_per_week"] == 6, str(n["days_per_week"]))
    check("时长回退 60", n["session_minutes"] == 60, str(n["session_minutes"]))
    check("非法限制被过滤", n["constraints"] == ["no_gym"], str(n["constraints"]))


def test_swim_program():
    print("\n[7] 游泳计划")
    store = make_store("swim.db")
    report = evaluate.evaluate(store, tz_offset=8, now=NOW)

    keys = {k: v for k, v in program.GOALS.items() if v.get("family") == "swim"}
    check("有 4 个游泳目标", len(keys) == 4, str(sorted(keys)))
    check("游泳目标带距离", keys["swim_1000"]["distance"] == 1000,
          str(keys["swim_1000"].get("distance")))

    p = program.build_program(store, report, {
        "goal": "swim_1000", "horizon_weeks": 13, "days_per_week": 4,
        "session_minutes": 60, "level": "intermediate",
    }, now=NOW)
    check("计划族是游泳", p["goal"]["family"] == "swim", p["goal"]["family"])

    week1 = p["weeks"][0]
    swim_days = [d for d in week1["days"] if d["kind"] != "rest"]
    check("游泳课都有距离（米）或陆上力量",
          all(d.get("distance_m") or d["kind"] == "strength" for d in swim_days),
          str([(d["kind"], d.get("distance_m")) for d in swim_days]))
    check("距离取整到 25 米池",
          all(d["distance_m"] % 25 == 0 for d in swim_days if d.get("distance_m")),
          str([d.get("distance_m") for d in swim_days]))
    check("周目标用 target_m 而不是 target_km",
          week1["target_m"] and week1["target_km"] is None,
          f'{week1["target_m"]} / {week1["target_km"]}')
    check("周目标 = 各课距离之和",
          week1["target_m"] == sum(int(d.get("distance_m") or 0) for d in week1["days"]),
          str(week1["target_m"]))
    check("配速提示按 100 米给",
          all(("/100m" in (d.get("pace_hint") or "/100m")) for d in swim_days),
          str([d.get("pace_hint") for d in swim_days]))

    longs = [max((d.get("distance_m") or 0) for d in w["days"] if d["kind"] == "long")
             for w in p["weeks"]]
    check("长距离课随周期增长", longs[-1] > longs[0], str(longs[:3] + longs[-2:]))
    check("长距离课不超过目标 115%", max(longs) <= 1000 * 1.15 + 25, str(max(longs)))
    check("起点低于目标距离", longs[0] < 1000, str(longs[0]))

    # 「学会连续游」没有比赛距离，量必须保守
    beginner = program.build_program(store, report, {
        "goal": "swim_start", "horizon_weeks": 4, "days_per_week": 4,
        "session_minutes": 60, "level": "beginner",
    }, now=NOW)
    wk = beginner["weeks"][0]
    check("入门周量 ≤ 3000 米", (wk["target_m"] or 0) <= 3000, str(wk["target_m"]))
    check("入门长距离课 ≤ 1000 米",
          max((d.get("distance_m") or 0) for d in wk["days"]) <= 1000,
          str([d.get("distance_m") for d in wk["days"]]))

    opt = program._normalize_intake({"goal": "swim_1500"})
    check("游泳目标能通过归一化", opt["goal"] == "swim_1500", opt["goal"])

    # 建议目标要跟着实际练的项目走
    swim_report = dict(report)
    swim_report["sport_mix"] = {"swim": 80.0, "run": 20.0}
    sug = program.suggest_intake(swim_report)
    check("游泳为主时建议游泳目标", program.GOALS[sug["goal"]]["family"] == "swim", sug["goal"])
    store.close()



def test_ride_program():
    print("\n[8] 骑行目标与课程分化")
    store = make_store("ride.db")
    report = evaluate.evaluate(store, tz_offset=8, now=NOW)

    ride = {k: v for k, v in program.GOALS.items() if v.get("family") == "ride"}
    check("骑行目标至少 5 个", len(ride) >= 5, str(sorted(ride)))
    check("百公里目标带距离", ride["ride_century"].get("distance") == 100000,
          str(ride["ride_century"].get("distance")))

    def plan_for(goal):
        return program.build_program(store, report, {
            "goal": goal, "horizon_weeks": 13, "days_per_week": 4,
            "session_minutes": 90, "level": "intermediate",
        }, now=NOW)

    titles = {}
    for goal in ("sportive", "ride_base", "ride_ftp", "ride_climb", "ride_century"):
        p = plan_for(goal)
        check(f"{goal}: 计划族是骑行", p["goal"]["family"] == "ride", p["goal"]["family"])
        w = p["weeks"][5]
        q = [d for d in w["days"] if d["kind"] == "quality"]
        l = [d for d in w["days"] if d["kind"] == "long"]
        check(f"{goal}: 质量课与长距离课都在", bool(q) and bool(l),
              str([d["kind"] for d in w["days"]]))
        check(f"{goal}: 长距离课 >= 20 km", bool(l) and (l[0].get("distance_km") or 0) >= 20,
              str([(d["kind"], d.get("distance_km")) for d in w["days"]]))
        titles[goal] = q[0]["title"] if q else ""

    check("FTP 目标排甜区课", "甜区" in titles["ride_ftp"], titles["ride_ftp"])
    check("爬坡目标排爬坡课", "爬坡" in titles["ride_climb"], titles["ride_climb"])
    check("打底目标排踏频课", "踏频" in titles["ride_base"], titles["ride_base"])
    check("百公里目标排节奏课", "节奏" in titles["ride_century"], titles["ride_century"])
    check("五个骑行目标的质量课各不相同", len(set(titles.values())) == 5, str(titles))

    century = plan_for("ride_century")
    longs = [max((d.get("distance_km") or 0) for d in w["days"] if d["kind"] == "long")
             for w in century["weeks"]]
    check("百公里长距离随周期增长", longs[-1] > longs[0], str(longs[:3] + longs[-2:]))
    check("百公里长距离封顶 100 km", max(longs) <= 100.0, str(max(longs)))
    check("减量周长距离短于峰值周", longs[-1] < max(longs), str(longs))

    beginner = program.build_program(store, report, {
        "goal": "ride_base", "horizon_weeks": 4, "days_per_week": 3,
        "session_minutes": 60, "level": "beginner",
    }, now=NOW)
    check("骑行课表不排力量课",
          all(d["kind"] != "strength" for d in beginner["weeks"][0]["days"]), "strength")
    store.close()


def main() -> int:
    print("=" * 58)
    print("多周期计划与问答测试")
    print("=" * 58)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    test_horizons()
    test_volume_curve()
    test_family_mismatch()
    test_days_and_constraints()
    test_qa()
    test_intake_normalize()
    test_swim_program()
    test_ride_program()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 58)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
