"""可选的 Strava 网页数据源（逆向得到的内部接口）。

⚠️ 重要说明
-----------
Strava 的服务条款（Terms of Service）禁止未经许可的自动化抓取。
使用本模块可能导致你的账号被限速、要求验证，甚至被封禁。
**默认不启用，也不会自动运行**。如果你要用，请自行承担风险；
更稳妥、完全合规的做法是使用 Strava 官方的 "导出我的数据" 功能，
把导出的 GPX/FIT/CSV 拖进 PACE 导入（功能完全一样，且不会封号）。

本模块提供的价值
----------------
1. **离线解析**：把你在浏览器里正常浏览时保存下来的 HTML/JSON 喂给它，
   不产生任何网络请求，也不会违反条款。这是默认、推荐的使用方式。
2. **可选直连**：显式传入会话 Cookie 后，按较低的固定频率请求，
   并遵守 Retry-After。必须由你主动启用。

逆向结论（2026 年观察，随时可能失效）
------------------------------------
* 活动列表：服务端渲染页面里嵌了 ``window.__NEXT_DATA__`` /
  ``<script type="application/json">`` 形态的 JSON，含活动摘要
* 活动详情：同样在 HTML 内嵌 JSON，含 ``activity`` 字段
* 逐点流数据：``/activities/{id}/streams`` 返回 ``{type: {data: [...]}}``
  或顶层数组，包含 time / latlng / heartrate / altitude / velocity_smooth
* 页面同时接受 ``/api/v3/activities/{id}/streams`` 这类内部 v3 路径

因为结构会变，所有解析都必须"容错 + 可降级"：找不到就返回部分数据，
绝不因为一个字段缺失就整体失败。
"""
from __future__ import annotations

import gzip
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterator

from .parsers import ParseError, normalize_sport, parse_time

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 请求间隔下限（秒）。即使调用方要求更快，也会被抬到这个值。
MIN_INTERVAL = 6.0
# 连续失败多少次后熔断
MAX_CONSECUTIVE_FAILURES = 3


class ScrapeDisabled(RuntimeError):
    """没有显式启用直连抓取时抛出。"""


class RateLimitExceeded(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str
    status: int
    body: str
    fetched_at: float


# ---------------------------------------------------------------- 内嵌 JSON 提取

_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]+type="application/(?:ld\+)?json"[^>]*>(.*?)</script>',
    re.S | re.I,
)
_NEXT_DATA_RE = re.compile(r'window\.__NEXT_DATA__\s*=\s*(\{.*?\})\s*;?\s*</script>', re.S)
_ASSIGN_RE = re.compile(r'window\.([A-Za-z_$][\w$]*)\s*=\s*(\{.*?\})\s*;?\s*</script>', re.S)


def extract_json_blobs(page: str) -> list[dict]:
    """从 HTML 里挖出所有可能的 JSON 对象（不执行脚本）。"""
    blobs: list[dict] = []

    def try_add(text: str) -> None:
        text = text.strip()
        if not text or text[0] not in "{[":
            return
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(data, dict):
            blobs.append(data)
        elif isinstance(data, list):
            blobs.extend(x for x in data if isinstance(x, dict))

    for match in _NEXT_DATA_RE.finditer(page):
        try_add(match.group(1))
    for match in _ASSIGN_RE.finditer(page):
        try_add(match.group(2))
    for match in _JSON_SCRIPT_RE.finditer(page):
        try_add(html.unescape(match.group(1)))
    return blobs


def find_activity_payload(blobs: list[dict]) -> dict | None:
    """在 JSON 树里广度优先找长得像活动的对象。"""
    queue = list(blobs)
    seen = 0
    while queue and seen < 5000:
        node = queue.pop(0)
        seen += 1
        if isinstance(node, dict):
            keys = set(node.keys())
            score = 0
            if "start_date" in keys or "start_date_local" in keys:
                score += 2
            if "distance" in keys or "moving_time" in keys:
                score += 2
            if "type" in keys or "sport_type" in keys:
                score += 1
            if "id" in keys:
                score += 1
            if score >= 5:
                return node
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            queue.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def find_activity_list(blobs: list[dict]) -> list[dict]:
    """找活动摘要数组（至少 2 个元素且每个都像活动）。"""
    best: list[dict] = []
    queue = list(blobs)
    seen = 0
    while queue and seen < 5000:
        node = queue.pop(0)
        seen += 1
        if isinstance(node, list):
            candidates = [x for x in node if isinstance(x, dict) and
                          ("start_date" in x or "start_date_local" in x) and
                          ("type" in x or "sport_type" in x)]
            if len(candidates) >= 2 and len(candidates) > len(best):
                best = candidates
            queue.extend(x for x in node if isinstance(x, (dict, list)))
        elif isinstance(node, dict):
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return best


def parse_streams_payload(raw: str | bytes) -> dict:
    """把 streams 接口返回体转成统一 points（或仅 streams 字典）。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(f"streams 不是合法 JSON: {exc}") from exc

    streams: dict = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("type"):
                streams[item["type"]] = item.get("data") or []
    elif isinstance(data, dict):
        if "streams" in data and isinstance(data["streams"], dict):
            data = data["streams"]
        for key, value in data.items():
            if isinstance(value, dict):
                streams[key] = value.get("data") or []
            elif isinstance(value, list):
                streams[key] = value
    return streams


# ---------------------------------------------------------------- 抓取器

class StravaPublicReader:
    """只读、低频、可熔断的抓取器。

    构造时必须显式传入 ``allow_network=True``，否则任何网络方法都会抛错。
    """

    def __init__(self, *, allow_network: bool = False, cookie: str | None = None,
                 user_agent: str | None = None, min_interval: float = MIN_INTERVAL,
                 max_requests: int = 60):
        self.allow_network = allow_network
        self.cookie = cookie or os.environ.get("STRAVA_COOKIE") or ""
        self.user_agent = user_agent or os.environ.get("PACE_UA") or DEFAULT_USER_AGENT
        self.min_interval = max(MIN_INTERVAL, float(min_interval))
        self.max_requests = max(1, int(max_requests))
        self._last_request = 0.0
        self._count = 0
        self._failures = 0

    # -------------------------------------------------- 网络

    def _guard(self) -> None:
        if not self.allow_network:
            raise ScrapeDisabled(
                "网络抓取未启用。请改用离线解析（save_page + parse_*），"
                "或显式传入 allow_network=True 并自行承担账号风险。"
            )
        if not self.cookie:
            raise ScrapeDisabled("缺少会话 Cookie。设置环境变量 STRAVA_COOKIE 后再试。")
        if self._failures >= MAX_CONSECUTIVE_FAILURES:
            raise RateLimitExceeded("连续失败次数过多，已熔断。请稍后再试。")
        if self._count >= self.max_requests:
            raise RateLimitExceeded(f"已达单次运行请求上限（{self.max_requests}）。")

    def fetch(self, url: str, *, accept: str = "text/html") -> FetchResult:
        self._guard()
        wait = self.min_interval - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)

        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cookie": self.cookie,
            "Referer": "https://www.strava.com/dashboard",
        }
        request = urllib.request.Request(url, headers=headers)
        self._last_request = time.time()
        self._count += 1
        try:
            with urllib.request.urlopen(request, timeout=25) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                self._failures = 0
                return FetchResult(url, resp.status, raw.decode("utf-8", "replace"), time.time())
        except urllib.error.HTTPError as exc:
            self._failures += 1
            if exc.code == 429:
                retry = exc.headers.get("Retry-After")
                raise RateLimitExceeded(
                    f"被限速（429）。Retry-After={retry or '未提供'}。请立即停止。"
                ) from exc
            raise
        except urllib.error.URLError:
            self._failures += 1
            raise

    # -------------------------------------------------- 高层接口

    def fetch_activity_page(self, activity_id: int) -> FetchResult:
        return self.fetch(f"https://www.strava.com/activities/{int(activity_id)}")

    def fetch_streams(self, activity_id: int) -> dict:
        """优先内部 v3 接口，失败再退回页面内嵌 JSON。"""
        urls = [
            f"https://www.strava.com/api/v3/activities/{int(activity_id)}/streams"
            f"?keys=time,latlng,altitude,heartrate,cadence,watts,velocity_smooth&key_by_type=true",
        ]
        last_error: Exception | None = None
        for url in urls:
            try:
                result = self.fetch(url, accept="application/json")
            except RateLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            return parse_streams_payload(result.body)
        if last_error:
            raise last_error
        return {}

    def fetch_feed(self, page: int = 1) -> list[dict]:
        """抓取仪表盘或运动员页的活动摘要列表。"""
        urls = [
            "https://www.strava.com/dashboard/following",
            "https://www.strava.com/athlete/training",
            "https://www.strava.com/athlete/activities",
        ]
        for url in urls:
            try:
                result = self.fetch(url)
            except RateLimitExceeded:
                raise
            except Exception:
                continue
            blobs = extract_json_blobs(result.body)
            acts = find_activity_list(blobs)
            if acts:
                return acts
        return []


# ---------------------------------------------------------------- 离线解析

def _looks_like_streams(node: dict) -> bool:
    """是否是 streams 容器：有 latlng，或有 time + 任一数值流（室内活动无坐标）。"""
    if not isinstance(node, dict):
        return False
    if "latlng" in node:
        return True
    return "time" in node and any(
        k in node for k in ("distance", "heartrate", "watts", "altitude", "velocity_smooth")
    )


def find_streams_node(blobs: list[dict]) -> dict | None:
    """在 JSON 树里找 streams 字典。"""
    queue = list(blobs)
    seen = 0
    while queue and seen < 5000:
        node = queue.pop(0)
        seen += 1
        if isinstance(node, dict):
            if _looks_like_streams(node):
                return node
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            queue.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def _normalize_streams(node: dict) -> dict:
    """把 {type: {data: [...]}} 与 {type: [...]} 两种形态统一。"""
    out: dict = {}
    for key, value in node.items():
        if isinstance(value, dict) and isinstance(value.get("data"), list):
            out[key] = value["data"]
        elif isinstance(value, list):
            out[key] = value
    return out


def parse_saved_activity_page(page: str) -> dict:
    """解析保存下来的活动详情 HTML。

    返回结构尽量接近 parsers 的统一输出；找不到轨迹点时也能给出摘要。
    """
    blobs = extract_json_blobs(page)
    activity = find_activity_payload(blobs)
    if activity is None:
        raise ParseError("页面里没有找到活动 JSON。可能未登录、页面结构已变，或保存的是空壳。")

    start = parse_time(activity.get("start_date") or activity.get("start_date_local"))
    if start is None:
        raise ParseError("活动 JSON 缺少 start_date")

    points: list[dict] = []
    # 页面里偶尔直接带 streams（可能嵌在任何层级）
    node = find_streams_node(blobs)
    if node is not None:
        try:
            points = _points_from_streams(_normalize_streams(node), start)
        except ParseError:
            points = []

    return {
        "sport": normalize_sport(activity.get("sport_type") or activity.get("type")),
        "title": activity.get("name"),
        "notes": activity.get("description"),
        "start_time": start,
        "points": points,
        "summary": {
            "total_distance": activity.get("distance"),
            "total_elapsed": activity.get("elapsed_time"),
            "total_timer": activity.get("moving_time"),
            "avg_hr": activity.get("average_heartrate"),
            "max_hr": activity.get("max_heartrate"),
            "ascent": activity.get("total_elevation_gain"),
            "avg_power": activity.get("average_watts"),
        },
        "external_id": f"strava:{activity.get('id')}" if activity.get("id") else None,
        "source_format": "strava-html",
        "raw_meta": {
            "strava_id": activity.get("id"),
            "source": "saved page",
        },
    }


def parse_saved_activity_list(page: str) -> list[dict]:
    """从保存的列表页 HTML 里解析活动摘要。"""
    blobs = extract_json_blobs(page)
    acts = find_activity_list(blobs)
    out = []
    for item in acts:
        start = parse_time(item.get("start_date") or item.get("start_date_local"))
        if start is None:
            continue
        out.append({
            "sport": normalize_sport(item.get("sport_type") or item.get("type")),
            "title": item.get("name"),
            "start_time": start,
            "distance_m": item.get("distance"),
            "moving_seconds": item.get("moving_time"),
            "elevation_m": item.get("total_elevation_gain"),
            "avg_hr": item.get("average_heartrate"),
            "max_hr": item.get("max_heartrate"),
            "external_id": f"strava:{item.get('id')}",
            "source_format": "strava-html",
        })
    if not out:
        raise ParseError("页面里没有找到活动列表 JSON")
    return out


def parse_saved_streams(raw: str | bytes, start_time: float | None = None,
                        sport: str = "generic") -> dict:
    """把保存的 streams JSON 转成统一活动 dict。

    Strava 的 streams 不含起始时间，必须由调用方提供 ``start_time``。
    """
    streams = parse_streams_payload(raw)
    if not streams:
        raise ParseError("streams 里没有数据")
    if start_time is None:
        raise ParseError(
            "streams 不含起始时间。请用 --start 指定该活动的开始时间（ISO 8601）。"
        )
    points = _points_from_streams(streams, float(start_time))
    return {
        "sport": normalize_sport(sport),
        "title": None,
        "start_time": float(start_time),
        "points": points,
        "summary": {},
        "source_format": "strava-streams",
    }


def _points_from_streams(streams: dict, start: float) -> list[dict]:
    times = streams.get("time") or []
    latlng = streams.get("latlng") or []
    if not times:
        raise ParseError("streams 缺少 time")
    hr = streams.get("heartrate") or []
    cad = streams.get("cadence") or []
    power = streams.get("watts") or []
    alt = streams.get("altitude") or []
    vel = streams.get("velocity_smooth") or []
    dist = streams.get("distance") or []

    points = []
    for i, t in enumerate(times):
        ll = latlng[i] if i < len(latlng) else None
        has_ll = isinstance(ll, (list, tuple)) and len(ll) >= 2
        # 室内活动（骑行台/跑步机）没有 latlng，但有 distance 流；
        # 丢掉它们会让用户以为数据没导进来，所以这里允许无坐标点。
        if not has_ll and not dist:
            continue
        points.append({
            "t": start + float(t),
            "lat": ll[0] if has_ll else None,
            "lon": ll[1] if has_ll else None,
            "alt": alt[i] if i < len(alt) else None,
            "hr": hr[i] if i < len(hr) else None,
            "cad": cad[i] if i < len(cad) else None,
            "power": power[i] if i < len(power) else None,
            "speed": vel[i] if i < len(vel) else None,
            "dist": dist[i] if i < len(dist) else None,
        })
    if not points:
        raise ParseError("streams 里没有有效数据点")
    return points


# ---------------------------------------------------------------- 导出 / 命令行

def to_gpx(activity: dict) -> str:
    """把解析结果导出为 GPX，之后可以走正常导入流程（不需要网络）。"""
    from datetime import datetime, timezone
    start = float(activity["start_time"])
    title = html.escape(activity.get("title") or "Strava activity")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="PACE" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        f"  <metadata><name>{title}</name></metadata>",
        f"  <trk><name>{title}</name><type>{activity.get('sport', 'generic')}</type><trkseg>",
    ]
    for point in activity.get("points") or []:
        if point.get("lat") is None:
            continue
        stamp = datetime.fromtimestamp(point["t"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts = [f'    <trkpt lat="{point["lat"]:.7f}" lon="{point["lon"]:.7f}">',
                 f"<time>{stamp}</time>"]
        if point.get("alt") is not None:
            parts.append(f'<ele>{point["alt"]:.2f}</ele>')
        ext = []
        if point.get("hr") is not None:
            ext.append(f'<gpxtpx:hr>{int(point["hr"])}</gpxtpx:hr>')
        if point.get("cad") is not None:
            ext.append(f'<gpxtpx:cad>{int(point["cad"])}</gpxtpx:cad>')
        if ext:
            parts.append("<extensions><gpxtpx:TrackPointExtension>"
                         + "".join(ext) + "</gpxtpx:TrackPointExtension></extensions>")
        parts.append("</trkpt>")
        lines.append("".join(parts))
    lines += ["</trkseg></trk>", "</gpx>"]
    return "\n".join(lines)


def parse_strava_json(raw: str | bytes, start_time: float | None = None,
                      sport: str = "generic") -> dict | list[dict]:
    """解析保存下来的 Strava JSON。

    支持三种形态：
    1. 完整活动对象（含 start_date）+ 任意层级的 streams -> 完整活动
    2. 纯 streams（无时间）-> 需要调用方给 start_time
    3. 活动摘要数组 -> 摘要列表
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(f"不是合法 JSON: {exc}") from exc

    blobs = [data] if isinstance(data, dict) else [x for x in data if isinstance(x, dict)] \
        if isinstance(data, list) else []
    if not blobs:
        raise ParseError("JSON 结构无法识别")

    activity = find_activity_payload(blobs)
    if activity is not None:
        node = find_streams_node(blobs)
        start = parse_time(activity.get("start_date") or activity.get("start_date_local"))
        if start is None and start_time is not None:
            start = float(start_time)
        points = []
        if node is not None and start is not None:
            try:
                points = _points_from_streams(_normalize_streams(node), start)
            except ParseError:
                points = []
        if start is None:
            raise ParseError("活动对象缺少 start_date，且未提供 --start")
        return {
            "sport": normalize_sport(activity.get("sport_type") or activity.get("type") or sport),
            "title": activity.get("name"),
            "notes": activity.get("description"),
            "start_time": start,
            "points": points,
            "summary": {
                "total_distance": activity.get("distance"),
                "total_elapsed": activity.get("elapsed_time"),
                "total_timer": activity.get("moving_time"),
                "avg_hr": activity.get("average_heartrate"),
                "max_hr": activity.get("max_heartrate"),
                "ascent": activity.get("total_elevation_gain"),
            },
            "external_id": f"strava:{activity.get('id')}" if activity.get("id") else None,
            "source_format": "strava-json",
        }

    # 纯 streams
    if any(k in data for k in ("latlng", "time")) and isinstance(data, dict):
        return parse_saved_streams(raw, start_time=start_time, sport=sport)

    # 摘要数组
    rows = find_activity_list(blobs)
    if rows:
        out = []
        for item in rows:
            start = parse_time(item.get("start_date") or item.get("start_date_local"))
            if start is None:
                continue
            out.append({
                "sport": normalize_sport(item.get("sport_type") or item.get("type")),
                "title": item.get("name"),
                "start_time": start,
                "distance_m": item.get("distance"),
                "moving_seconds": item.get("moving_time"),
                "elevation_m": item.get("total_elevation_gain"),
                "avg_hr": item.get("average_heartrate"),
                "max_hr": item.get("max_heartrate"),
                "external_id": f"strava:{item.get('id')}",
                "source_format": "strava-json",
            })
        if out:
            return out

    raise ParseError("JSON 里没有识别出活动或轨迹数据")


def _guess_start_from_name(name: str) -> float | None:
    """从形如 ``2026-11-08T06-30-00_streams.json`` 的文件名猜起始时间。"""
    match = re.search(r"(\d{4}-\d{2}-\d{2})[T_ ](\d{2})[-:]?(\d{2})", name)
    if not match:
        return None
    try:
        from datetime import datetime
        dt = datetime.strptime(f"{match.group(1)} {match.group(2)}:{match.group(3)}",
                               "%Y-%m-%d %H:%M")
        return dt.timestamp()
    except ValueError:
        return None


def collect_offline(input_dir: str) -> Iterator[tuple[str, dict | list]]:
    """扫描一个目录里保存下来的 HTML / JSON，逐个解析。"""
    for name in sorted(os.listdir(input_dir)):
        path = os.path.join(input_dir, name)
        if not os.path.isfile(path):
            continue
        lower = name.lower()
        try:
            if lower.endswith((".html", ".htm")):
                text = open(path, encoding="utf-8", errors="replace").read()
                try:
                    yield name, parse_saved_activity_page(text)
                except ParseError:
                    yield name, parse_saved_activity_list(text)
            elif lower.endswith(".json"):
                raw = open(path, "rb").read()
                yield name, parse_saved_streams(raw, start_time=_guess_start_from_name(name))
        except ParseError as exc:
            yield name, {"error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Strava 网页数据（离线解析优先；直连需显式授权且自担风险）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_offline = sub.add_parser("offline", help="解析本地保存的 HTML/JSON，导出一个 GPX")
    p_offline.add_argument("input", help="单个文件或目录")
    p_offline.add_argument("--out", default="strava-export", help="输出目录")
    p_offline.add_argument("--start", default=None,
                           help="streams JSON 缺时间时使用，ISO 8601，如 2026-11-08T06:30:00")
    p_offline.add_argument("--sport", default="running", help="streams JSON 的运动类型")

    p_live = sub.add_parser("live", help="⚠️ 直连抓取（需 STRAVA_COOKIE，可能违反条款）")
    p_live.add_argument("activity_id", type=int)
    p_live.add_argument("--out", default="strava-export")
    p_live.add_argument("--i-understand-the-risk", action="store_true",
                        help="必须显式确认，否则拒绝运行")

    args = parser.parse_args(argv)

    if args.cmd == "offline":
        os.makedirs(args.out, exist_ok=True)
        target = args.input
        items: Iterator[tuple[str, dict | list]]
        if os.path.isdir(target):
            items = collect_offline(target)
        else:
            text = open(target, encoding="utf-8", errors="replace").read()
            if target.lower().endswith(".json"):
                items = [(os.path.basename(target),
                          parse_saved_streams(text, start_time=parse_time(args.start),
                                              sport=args.sport))]
            else:
                items = [(os.path.basename(target), parse_saved_activity_page(text))]
        count = 0
        for name, data in items:
            if isinstance(data, dict) and "error" in data:
                print(f"  跳过 {name}: {data['error']}")
                continue
            records = data if isinstance(data, list) else [data]
            for rec in records:
                if not rec.get("points"):
                    print(f"  跳过 {name}: 没有轨迹点")
                    continue
                stem = os.path.splitext(name)[0]
                out_path = os.path.join(args.out, f"{stem}.gpx")
                with open(out_path, "w", encoding="utf-8") as fh:
                    fh.write(to_gpx(rec))
                count += 1
                print(f"  已导出 {out_path}")
        print(f"完成，共 {count} 个 GPX。把它们导入 PACE 即可。")
        return 0

    if args.cmd == "live":
        if not args.i_understand_the_risk:
            print("拒绝执行：直连抓取可能违反 Strava 服务条款并导致账号受限。\n"
                  "如果你已明确知悉风险，请加上 --i-understand-the-risk。\n"
                  "更推荐的做法：在浏览器里正常打开活动页并另存为 HTML，"
                  "然后运行 `python -m pace.strava_web offline <文件>`。")
            return 2
        reader = StravaPublicReader(allow_network=True)
        page = reader.fetch_activity_page(args.activity_id)
        rec = parse_saved_activity_page(page.body)
        streams = reader.fetch_streams(args.activity_id)
        if streams:
            start = rec["start_time"]
            try:
                rec["points"] = _points_from_streams(streams, start)
            except ParseError:
                pass
        os.makedirs(args.out, exist_ok=True)
        out_path = os.path.join(args.out, f"strava-{args.activity_id}.gpx")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(to_gpx(rec))
        print(f"已导出 {out_path}（{len(rec.get('points') or [])} 个点）")
        print("请立即停止，不要连续请求。")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
