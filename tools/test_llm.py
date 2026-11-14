"""AI 教练（大模型接入）测试：不联网，只验证降级与上下文。

重点不是「模型答得好不好」，而是：
1. 没 Key 时必须无痛回退到规则引擎，接口不报错；
2. Key 无效/网络不通时同样回退，并给出可读原因；
3. 发给模型的上下文必须是压缩过的、且包含关键事实（否则模型会瞎编）。
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import analytics
from pace.coach import evaluate, llm, program, qa
from pace.store import open_store
from pace.summary_build import build_from_summary

PASSED = 0
FAILED: list[str] = []
NOW = 1_794_000_000.0
TMP = os.path.join(ROOT, ".llm-test")


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f" FAIL {name} {detail}")


def make_store(name="llm.db"):
    path = os.path.join(TMP, name)
    store = open_store(path)
    for w in range(6):
        base = NOW - (6 - w) * 7 * 86400
        for offset, sec, km, hr, pwr in ((0, 2400, 7.0, 145, 180), (2, 5400, 40.0, 138, 165)):
            sport = "running" if km < 10 else "cycling"
            built = build_from_summary({
                "sport": sport, "start_time": base + offset * 86400,
                "distance_m": km * 1000, "moving_seconds": sec,
                "elevation_m": km * 6, "avg_hr": hr, "max_hr": hr + 20,
                "avg_power": pwr if sport == "cycling" else None,
            })
            store.upsert_activity({
                "source": "test", "external_id": f"{name}-{w}-{offset}",
                "filename": "t.gpx", "sport": sport, "title": sport,
                "summary": built["summary"], "streams": {}, "route": [],
            })
    return store


def test_provider():
    print("\n[1] 配置解析与掩码")
    off = llm.provider({})
    check("空设置默认关闭", off["enabled"] is False, str(off["enabled"]))
    check("默认地址是 DeepSeek", off["base_url"] == "https://api.deepseek.com/v1", off["base_url"])
    check("默认模型", off["model"] == "deepseek-chat", off["model"])

    on = llm.provider({"llm_api_key": "sk-1234567890abcdef", "llm_model": "deepseek-v4.1-flash",
                       "llm_base_url": "https://example.com/v1/", "llm_timeout_s": 20})
    check("有 Key 即启用", on["enabled"] is True)
    check("自定义模型名原样保留", on["model"] == "deepseek-v4.1-flash", on["model"])
    check("base_url 去掉尾部斜杠", on["base_url"] == "https://example.com/v1", on["base_url"])
    check("超时按设置", on["timeout"] == 20.0, str(on["timeout"]))

    check("长 Key 只露头尾", llm.mask_key("sk-1234567890abcdef") == "sk-1…cdef",
          llm.mask_key("sk-1234567890abcdef"))
    check("短 Key 不外露", llm.mask_key("sk-123") == "已设置", llm.mask_key("sk-123"))
    check("空 Key 返回空串", llm.mask_key("") == "")


def test_context(store, report):
    print("\n[2] 发给模型的上下文")
    text = llm.build_context(store, report,
                             {"goal": "swim_1000", "horizon_weeks": 13,
                              "days_per_week": 4, "session_minutes": 60,
                              "level": "intermediate", "note": "膝盖有点不舒服"},
                             tz_offset=8, days=56)
    check("包含日期", "今天是" in text)
    check("包含目标", "swim_1000" in text, text[:60])
    check("包含用户补充", "膝盖" in text)
    check("包含负荷数字", "CTL" in text and "ATL" in text)
    check("包含运动构成", "运动构成" in text)
    check("包含最近活动", "最近的活动" in text)
    check("活动带距离与时长", "km" in text and "爬升" in text)
    check("包含参数", "FTP" in text and "最大心率" in text)
    check("有长度上限（不是整库倾倒）", len(text) < 4000, str(len(text)))

    prog = program.build_program(store, report, {
        "goal": "swim_1000", "horizon_weeks": 4, "days_per_week": 4,
        "session_minutes": 60, "level": "intermediate"})
    text2 = llm.build_context(store, report, {"goal": "swim_1000"}, prog, days=56)
    check("带计划时会写课表", "第 1 周" in text2, text2[-200:])
    check("游泳计划用米", " 米" in text2, text2[-200:])


def test_fallback(store, report):
    print("\n[3] 没有 Key：无痛回退")
    store.set_settings({"llm_api_key": ""})
    res = llm.ask(store, report, "这周该怎么练？", {}, None)
    rules = qa.answer(store, report, "这周该怎么练？", {})
    check("回退到规则引擎", res.get("source") == "rules", str(res.get("source")))
    check("仍然有答案", bool(res.get("answer") or res.get("title")))
    check("标明未启用", res.get("llm", {}).get("enabled") is False, str(res.get("llm")))
    check("规则引擎结果不受影响", res.get("title") == rules.get("title"),
          f'{res.get("title")} vs {rules.get("title")}')
    check("带 topic", bool(res.get("topic")), str(res.get("topic")))

    print("\n[4] Key 无效 / 网络不通：仍然回退")
    store.set_settings({"llm_api_key": "sk-test-invalid", "llm_base_url": "http://127.0.0.1:9/v1",
                        "llm_timeout_s": 3})
    res2 = llm.ask(store, report, "配速怎么提？", {}, None)
    check("回退到规则引擎", res2.get("source") == "rules", str(res2.get("source")))
    check("失败信息可读", bool(res2.get("llm", {}).get("error")), str(res2.get("llm")))
    check("失败不抛异常", isinstance(res2, dict))

    probe = llm.test_connection({"llm_api_key": "", "llm_enabled": True})
    check("无 Key 时测试连接给提示", probe["ok"] is False and "Key" in probe["error"], str(probe))
    probe2 = llm.test_connection({"llm_api_key": "sk-x", "llm_base_url": "http://127.0.0.1:9/v1",
                                  "llm_timeout_s": 3})
    check("连不上时测试连接不抛异常", probe2["ok"] is False and bool(probe2.get("error")),
          str(probe2))


def test_prompt():
    print("\n[5] 系统提示词的边界")
    p = llm.SYSTEM_PROMPT
    check("要求先结论", "结论" in p)
    check("禁止编数字", "不许编" in p or "缺数据" in p, p[:80])
    check("不替代医生", "医生" in p)
    check("中文回答", "中文" in p)


def main() -> int:
    print("=" * 58)
    print("AI 教练接入测试（不联网）")
    print("=" * 58)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    store = make_store()
    report = evaluate.evaluate(store, tz_offset=8, now=NOW)
    test_provider()
    test_context(store, report)
    test_fallback(store, report)
    test_prompt()
    store.close()
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
