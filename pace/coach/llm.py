"""AI 教练：调用 OpenAI 兼容的对话接口（DeepSeek 等），规则引擎兜底。

设计取舍
--------
* **没有 Key 时功能不退化**：一律走 `pace/coach/qa.py` 的规则引擎，
  页面上只是少一个「AI」标记，不会报错、不会卡住。
* 发给模型的是**压缩后的结构化事实**（负荷、近期活动、本周课程、风险），
  不是整个数据库；模型只做解读和表达，不负责算数字。
* 超时、限流、网络错误、返回格式不对——全部静默回退到规则引擎，
  并在响应里带上 `source` / `error`，前端据此提示，不假装是 AI 说的。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from .. import analytics
from . import qa as coach_qa

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """你是 PACE 的骑行/跑步/游泳教练。你的用户是业余爱好者，自己用功率计、心率带和码表。

回答要求：
1. 先给结论，再给依据，最后给这周能执行的动作。控制在 200 字以内，除非用户明确要详细。
2. 只用给定的数据说话。数据里没有的（比如没有心率、没有功率），直接说「这项缺数据」，不许编数字。
3. 涉及训练强度时用区间和主观感受（能说话/说不出话）表述，不要堆术语。
4. 不做医疗诊断；疼痛、伤病、心悸一律建议就医或线下评估，不替代医生。
5. 用户问「怎么练」时，优先对齐他当前计划的周课表和负荷，而不是另起一套。
6. 中文回答，语气像一个靠谱的教练：直接、具体、不灌鸡汤。"""


# ---------------------------------------------------------------- 配置

def provider(settings: dict | None) -> dict:
    s = settings or {}
    key = str(s.get("llm_api_key") or "").strip()
    return {
        "enabled": bool(key and s.get("llm_enabled", True)),
        "api_key": key,
        "base_url": str(s.get("llm_base_url") or DEFAULT_BASE_URL).strip().rstrip("/"),
        "model": str(s.get("llm_model") or DEFAULT_MODEL).strip(),
        "timeout": float(s.get("llm_timeout_s") or 45),
    }


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 8:
        return "已设置" if key else ""
    return f"{key[:4]}…{key[-4:]}"


# ---------------------------------------------------------------- 上下文

def _fmt_dur(seconds) -> str:
    s = int(seconds or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}"


def _fmt_date(ts: float, tz) -> str:
    return tz.local(ts).strftime("%m-%d")


def build_context(store, report: dict, intake: dict | None = None,
                  program: dict | None = None, *, tz_offset: float = 8.0,
                  days: int = 56) -> str:
    """把训练现状压成一段给模型看的文本，控制在 ~1200 token 内。"""
    tz = analytics.TZ(tz_offset)
    settings = store.get_settings()
    intake = intake or {}
    fit = report.get("fitness", {}) or {}
    load = report.get("load", {}) or {}
    vol = report.get("volume", {}) or {}
    inv = report.get("inventory", {}) or {}
    mix = report.get("sport_mix", {}) or {}

    lines = [f"今天是 {tz.local(tz.now()).strftime('%Y-%m-%d')}。"]
    goal = intake.get("goal")
    if goal:
        lines.append(f"目标：{goal}（周期 {intake.get('horizon_weeks')} 周，"
                     f"每周 {intake.get('days_per_week')} 次，单次 {intake.get('session_minutes')} 分钟，"
                     f"水平 {intake.get('level')}）。")
    cons = intake.get("constraints") or []
    if cons:
        lines.append("限制：" + "、".join(str(c) for c in cons))
    if intake.get("note"):
        lines.append(f"用户补充：{intake['note']}")

    lines.append(
        f"负荷：CTL {fit.get('ctl', 0):.0f}，ATL {fit.get('atl', 0):.0f}，"
        f"TSB {fit.get('tsb', 0):.0f}；近 28 天单调性 {load.get('monotonic', 0):.1f}；"
        f"急性/慢性负荷比 {load.get('ratio', 0):.2f}。"
    )
    lines.append(
        f"近 {days} 天：{vol.get('sessions', 0)} 次，{vol.get('distance_km', 0):.0f} km，"
        f"{vol.get('hours', 0):.1f} 小时；运动构成 "
        + "、".join(f"{k} {v:.0f}%" for k, v in mix.items() if v)
    )

    hr_max = float(settings.get("hr_max") or 190)
    ftp = float(settings.get("ftp") or 200)
    lines.append(f"参数：最大心率 {hr_max:.0f} bpm，FTP {ftp:.0f} W；"
                 f"心率数据 {inv.get('with_hr', 0)} 条，功率数据 {inv.get('with_power', 0)} 条。")

    rows = store.all_activity_summaries()
    rows.sort(key=lambda r: r["start_time"], reverse=True)
    recent = rows[:8]
    if recent:
        lines.append("最近的活动：")
        for r in recent:
            bits = [f"{_fmt_date(r['start_time'], tz)} {r.get('sport') or '?'}",
                    f"{float(r.get('distance_m') or 0) / 1000:.1f} km",
                    _fmt_dur(r.get("moving_seconds"))]
            if r.get("avg_hr"):
                bits.append(f"均心率 {float(r['avg_hr']):.0f}")
            if r.get("avg_power"):
                bits.append(f"均功率 {float(r['avg_power']):.0f} W")
            bits.append(f"爬升 {float(r.get('elevation_m') or 0):.0f} m")
            lines.append("  - " + "，".join(bits))

    if program and program.get("weeks"):
        week = program["weeks"][0]
        lines.append(f"当前计划：{program.get('goal', {}).get('label', '')}，"
                     f"第 1 周（{week.get('phase')}），目标 "
                     f"{week.get('target_km') or (str(week.get('target_m')) + ' 米')}"
                     f"/{int(week.get('target_seconds', 0) // 60)} 分钟。课表：")
        for d in week.get("days", []):
            if d.get("kind") == "rest":
                continue
            extra = (" " + str(d["distance_m"]) + " 米" if d.get("distance_m")
                     else (" " + str(d["distance_km"]) + " km" if d.get("distance_km") else ""))
            lines.append(f"  - {d.get('day')} {d.get('title')} {d.get('duration_min') or 0} 分钟{extra}")

    risks = report.get("risks") or []
    if risks:
        lines.append("风险提示：" + "；".join(str(r.get("title") or r.get("key")) for r in risks[:4]))

    findings = (report.get("findings") or [])[:4]
    if findings:
        lines.append("系统诊断：" + "；".join(
            f"{f.get('title')}（{f.get('detail')}）" for f in findings))
    return "\n".join(lines)


# ---------------------------------------------------------------- 调用

class _HttpStatus(Exception):
    """非 2xx 响应（和 urllib.error.HTTPError 一样带 code，方便复用 _friendly）。"""

    def __init__(self, code: int, text: str):
        super().__init__(f"HTTP {code}: {text[:200]}")
        self.code = code
        self.body = text


def _in_browser() -> bool:
    return "pyodide" in sys.modules


def _post_raw(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, str]:
    """POST 并返回 (status, text)。浏览器里用同步 XHR（Pyodide 没有 socket）。"""
    if _in_browser():
        from js import XMLHttpRequest  # type: ignore[import-not-found]
        xhr = XMLHttpRequest.new()
        # 同步模式：Pyodide 跑在主线程，异步 fetch 没法在同步函数里等
        xhr.open("POST", url, False)
        # 同步 XHR 不允许设置 timeout（浏览器会直接抛错）；超时交给浏览器默认行为
        try:
            xhr.timeout = int(timeout * 1000)
        except Exception:  # noqa: BLE001
            pass
        for key, value in headers.items():
            xhr.setRequestHeader(key, value)
        xhr.send(body)
        return int(xhr.status), str(xhr.responseText or "")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def _post_chat(cfg: dict, messages: list[dict], *, timeout: float | None = None) -> dict:
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 800,
        "stream": False,
    }).encode("utf-8")
    status, text = _post_raw(
        cfg["base_url"] + "/chat/completions",
        body,
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        timeout or cfg["timeout"],
    )
    if not (200 <= status < 300):
        raise _HttpStatus(status, text)
    return json.loads(text)


def _extract(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("模型没有返回内容")
    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        raise ValueError("模型返回空回复")
    return text


def ask(store, report: dict, question: str, intake: dict | None = None,
        program: dict | None = None, *, tz_offset: float = 8.0) -> dict:
    """先试大模型；任何失败都回退规则引擎，并在结果里说明原因。"""
    settings = store.get_settings()
    cfg = provider(settings)
    fallback = coach_qa.answer(store, report, question, intake)
    if not cfg["enabled"]:
        fallback["source"] = "rules"
        fallback["llm"] = {"enabled": False, "reason": "未配置 API Key"}
        return fallback

    started = time.time()
    try:
        context = build_context(store, report, intake, program, tz_offset=tz_offset)
        payload = _post_chat(cfg, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"【训练数据】\n{context}\n\n【问题】\n{question}"},
        ])
        text = _extract(payload)
        usage = payload.get("usage") or {}
        return {
            "answer": text,
            "source": "llm",
            "topic": coach_qa.classify(question),
            "model": cfg["model"],
            "elapsed_ms": int((time.time() - started) * 1000),
            "usage": {
                "prompt": int(usage.get("prompt_tokens") or 0),
                "completion": int(usage.get("completion_tokens") or 0),
                "total": int(usage.get("total_tokens")
                             or (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)),
            },
            "llm": {"enabled": True, "model": cfg["model"]},
            "caveat": "AI 生成内容，数字以本页统计为准；不替代医生或线下教练。",
        }
    except Exception as exc:  # noqa: BLE001 - 任何失败都回退
        fallback["source"] = "rules"
        fallback["llm"] = {"enabled": True, "failed": True,
                           "model": cfg["model"], "error": _friendly(exc)}
        return fallback


def _friendly(exc: Exception) -> str:
    if isinstance(exc, _HttpStatus):
        return _friendly_http_code(exc.code)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "API Key 无效（401）"
        if exc.code == 402:
            return "账户余额不足（402）"
        if exc.code == 429:
            return "请求太频繁（429）"
        return f"接口返回 {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "连不上接口，检查网络或 base_url"
    if isinstance(exc, TimeoutError):
        return "请求超时"
    return str(exc)[:120]


def _friendly_http_code(code: int) -> str:
    if code == 401:
        return "API Key 无效（401）"
    if code == 402:
        return "账户余额不足（402）"
    if code == 429:
        return "请求太频繁（429）"
    if code in (403, 404):
        return f"接口拒绝或路径不对（{code}）"
    return f"接口返回 {code}"


def test_connection(settings: dict) -> dict:
    """设置页的「测试连接」。"""
    cfg = provider(settings)
    if not cfg["enabled"]:
        return {"ok": False, "error": "还没填 API Key"}
    started = time.time()
    try:
        payload = _post_chat(cfg, [
            {"role": "system", "content": "只回复四个字：连接正常"},
            {"role": "user", "content": "ping"},
        ], timeout=min(cfg["timeout"], 20))
        text = _extract(payload)
        return {"ok": True, "model": cfg["model"], "reply": text[:40],
                "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": _friendly(exc), "model": cfg["model"]}
