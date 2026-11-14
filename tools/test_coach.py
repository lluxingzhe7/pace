"""教练引擎测试：用受控数据集验证每条判断都按预期触发。

重点验证两件事：
1. 阈值判断真的会触发（不是永远输出"不错"）
2. 数据不足时报告会明确说"不够"，而不是硬编结论
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import analytics
from pace.coach import evaluate, plan
from pace.store import open_store
from pace.summary_build import build_from_summary

PASSED = 0
FAILED: list[str] = []
NOW = 1_794_000_000.0  # 固定时间，避免"今天"漂移导致测试不稳
TZ = analytics.TZ(8)


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def make_store(path, activities):
    """activities: [(days_ago, sport, moving_sec, distance_m, avg_hr, load)]"""
    store = open_store(path)
    for i, (days, sport, sec, dist, hr, load) in enumerate(activities):
        start = NOW - days * 86400
        built = build_from_summary({
            "sport": sport, "start_time": start, "distance_m": dist,
            "moving_seconds": sec, "elevation_m": dist * 0.01,
            "avg_hr": hr, "max_hr": (hr + 20) if hr else None,
        })
        summary = built["summary"]
        summary["training_load"] = load
        store.upsert_activity({
            "source": "test", "external_id": f"t{i}", "filename": "t.csv",
            "sport": sport, "title": f"{sport}-{i}",
            "summary": summary, "streams": {}, "route": [],
        })
    return store


def scenario(name):
    print(f"\n[{name}]")


def test_insufficient():
    scenario("数据不足")
    store = make_store(os.path.join(ROOT, ".coach-test", "a.db"), [
        (3, "running", 1800, 5000, 150, 60),
        (10, "running", 1800, 5000, 152, 60),
    ])
    r = evaluate.evaluate(store, tz_offset=8, now=NOW)
    check("总评提示数据不足", r["verdict"]["tone"] == "insufficient", r["verdict"]["tone"])
    check("置信度偏低", r["confidence"]["level"] == "low", r["confidence"]["level"])
    check("列出低数据量缺口", any(g["key"] == "low_data" for g in r["gaps"]))
    check("分数被打压", r["score"] <= 40, str(r["score"]))
    store.close()


def test_healthy():
    scenario("结构健康的跑者")
    acts = []
    for w in range(8):
        base = w * 7
        acts += [
            (base + 0, "running", 3000, 8000, 145, 70),   # easy
            (base + 2, "running", 2400, 6000, 168, 95),   # quality
            (base + 4, "running", 3600, 12000, 142, 85),  # long
            (base + 6, "running", 2700, 7000, 146, 68),   # easy
        ]
    store = make_store(os.path.join(ROOT, ".coach-test", "b.db"), acts)
    r = evaluate.evaluate(store, tz_offset=8, now=NOW)
    check("有足够数据", r["inventory"]["sufficient_for_load"])
    check("等级不是 E", r["grade"] != "E", r["grade"])
    check("无高危风险", not [x for x in r["risks"] if x["impact"] == "high"],
          str([x["key"] for x in r["risks"]]))
    check("给出下一步", len(r["next_steps"]) >= 1)
    store.close()


def test_overreached():
    scenario("疲劳堆积")
    acts = []
    # 前 6 周稳定，最近 10 天猛加量
    for w in range(6):
        base = 40 + w * 7
        acts += [(base + 0, "running", 2400, 6000, 145, 60),
                 (base + 3, "running", 2400, 6000, 146, 60)]
    for d in range(1, 11):
        acts.append((d, "running", 5400, 15000, 165, 160))
    store = make_store(os.path.join(ROOT, ".coach-test", "c.db"), acts)
    r = evaluate.evaluate(store, tz_offset=8, now=NOW)
    keys = {x["key"] for x in r["risks"]} | {x["key"] for x in r["findings"]}
    check("识别负荷激增", "volume_spike" in keys or "acwr_very_high" in keys, str(keys))
    check("ACWR 偏高", (r["load"]["acwr"] or 0) > 1.35, str(r["load"]["acwr"]))
    check("总评是警告", r["verdict"]["tone"] in ("warning", "poor"), r["verdict"]["tone"])
    if r["next_steps"]:
        check("第一条建议是减量", "减量" in r["next_steps"][0]["action"]
              or "不加量" in r["next_steps"][0]["action"], r["next_steps"][0]["action"])
    store.close()


def test_too_hard():
    scenario("强度过高")
    acts = []
    for w in range(8):
        base = w * 7
        acts += [(base + 0, "running", 2400, 6500, 172, 110),
                 (base + 2, "running", 2400, 6500, 175, 115),
                 (base + 4, "running", 2400, 6500, 170, 105)]
    store = make_store(os.path.join(ROOT, ".coach-test", "d.db"), acts)
    r = evaluate.evaluate(store, tz_offset=8, now=NOW)
    keys = {x["key"] for x in r["risks"]} | {x["key"] for x in r["findings"]}
    check("识别低强度不足", "too_hard" in keys, str(keys))
    check("区间分布有 Z4/Z5", (r["zone_share"] or {}).get("z4", 0) > 0, str(r["zone_share"]))
    store.close()


def test_monotony():
    scenario("每天都练、强度雷同")
    acts = []
    for d in range(1, 29):
        acts.append((d, "running", 2700, 7000, 158, 80))
    store = make_store(os.path.join(ROOT, ".coach-test", "e.db"), acts)
    r = evaluate.evaluate(store, tz_offset=8, now=NOW)
    keys = {x["key"] for x in r["risks"]} | {x["key"] for x in r["findings"]}
    check("识别单调性偏高", "monotony" in keys, str(keys))
    check("单调性数值合理", (r["load"]["monotony"] or 0) >= 1.5, str(r["load"]["monotony"]))
    store.close()


def test_no_long_run():
    scenario("缺少长距离")
    acts = []
    for w in range(8):
        base = w * 7
        acts += [(base + 0, "running", 1800, 5000, 148, 55),
                 (base + 2, "running", 1800, 5000, 150, 55),
                 (base + 4, "running", 1800, 5000, 149, 55),
                 (base + 6, "running", 1800, 5000, 147, 55)]
    store = make_store(os.path.join(ROOT, ".coach-test", "f.db"), acts)
    r = evaluate.evaluate(store, tz_offset=8, now=NOW)
    keys = {x["key"] for x in r["findings"]}
    check("识别缺少长距离", "no_long_run" in keys, str(keys))
    store.close()


def test_plan():
    scenario("计划生成")
    acts = []
    for w in range(6):
        base = w * 7
        acts += [(base + 0, "running", 2400, 6000, 145, 60),
                 (base + 3, "running", 2700, 7000, 147, 65),
                 (base + 5, "running", 3600, 11000, 143, 80)]
    store = make_store(os.path.join(ROOT, ".coach-test", "g.db"), acts)
    r = evaluate.evaluate(store, tz_offset=8, now=NOW)

    for goal, days in (("health", 3), ("finish", 4), ("improve", 5)):
        p = plan.build_plan(store, r, goal=goal, days_per_week=days, now=NOW)
        kinds = [s["kind"] for s in p["sessions"]]
        check(f"{goal}: 7 天齐全", len(p["sessions"]) == 7, str(len(p["sessions"])))
        check(f"{goal}: 训练日数正确", sum(1 for k in kinds if k != "rest") == days, str(kinds))
        check(f"{goal}: 最多一次阈值课", kinds.count("quality") <= 1, str(kinds))
        check(f"{goal}: 长距离在周末", all(
            s["weekday"] >= 5 for s in p["sessions"] if s["kind"] == "long"), str(kinds))
        check(f"{goal}: 有护栏", len(p["guardrails"]) >= 3)
        check(f"{goal}: 总量增幅 <= 10%", p["weekly_target"]["growth_pct"] <= 10,
              str(p["weekly_target"]["growth_pct"]))

    # 疲劳状态下计划应当减量
    heavy = make_store(os.path.join(ROOT, ".coach-test", "h.db"),
                       [(d, "running", 5400, 15000, 168, 170) for d in range(1, 12)])
    rh = evaluate.evaluate(heavy, tz_offset=8, now=NOW)
    ph = plan.build_plan(heavy, rh, goal="improve", days_per_week=4, now=NOW)
    check("过劳时计划减量", ph["weekly_target"]["growth_pct"] < 0,
          str(ph["weekly_target"]["growth_pct"]))
    check("过劳时阶段为恢复周", ph["phase"]["name"] in ("恢复周", "赛前减量"), ph["phase"]["name"])
    heavy.close()
    store.close()


def test_honesty():
    scenario("诚实性检查")
    store = make_store(os.path.join(ROOT, ".coach-test", "i.db"), [
        (d, "running", 1800, 5000, None, 40) for d in range(1, 29)
    ])
    r = evaluate.evaluate(store, tz_offset=8, now=NOW)
    check("无心率时明确说缺", any(g["key"] == "no_hr" for g in r["gaps"]))
    check("无心率时不给区间分布", r["zone_share"] is None)
    check("永远列出恢复数据缺口", any(g["key"] == "no_recovery" for g in r["gaps"]))
    check("有护栏声明不替代医生",
          any("医生" in g or "就医" in g for g in plan.build_plan(store, r, now=NOW)["guardrails"]))
    texts = [x.get("title", "") + x.get("detail", "") for x in r["findings"] + r["risks"]]
    check("结论里没有空洞夸奖", not any("非常棒" in t or "太厉害" in t or "完美" in t for t in texts))
    store.close()


def test_power_inventory():
    """功率维度统计：汇总行里必须带 avg_power，否则有功率也会被报成「功率缺」。"""
    scenario("功率维度统计")
    store = open_store(os.path.join(ROOT, ".coach-test", "p.db"))
    for i, days in enumerate((2, 6, 11, 17)):
        built = build_from_summary({
            "sport": "cycling", "start_time": NOW - days * 86400,
            "distance_m": 20000, "moving_seconds": 2400, "elevation_m": 120,
            "avg_hr": None, "max_hr": None,
        })
        summary = built["summary"]
        summary["avg_power"] = 150.0
        summary["training_load"] = 70
        store.upsert_activity({
            "source": "test", "external_id": f"p{i}", "filename": "p.csv",
            "sport": "cycling", "title": f"ride-{i}",
            "summary": summary, "streams": {}, "route": [],
        })
    r = evaluate.evaluate(store, tz_offset=8, now=NOW)
    check("有功率的活动被计入", r["inventory"]["with_power"] == 4, str(r["inventory"]["with_power"]))
    check("功率充足时不再报缺功率", r["inventory"]["has_power"], str(r["inventory"]))
    check("置信度理由里功率为「有」", "功率有" in r["confidence"]["reason"], r["confidence"]["reason"])
    store.close()


def main() -> int:
    print("=" * 58)
    print("教练引擎测试")
    print("=" * 58)
    shutil.rmtree(os.path.join(ROOT, ".coach-test"), ignore_errors=True)
    os.makedirs(os.path.join(ROOT, ".coach-test"), exist_ok=True)

    test_insufficient()
    test_healthy()
    test_overreached()
    test_too_hard()
    test_monotony()
    test_no_long_run()
    test_plan()
    test_honesty()
    test_power_inventory()

    shutil.rmtree(os.path.join(ROOT, ".coach-test"), ignore_errors=True)
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
