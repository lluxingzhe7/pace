"""多格式活动文件解析：GPX / TCX / FIT / CSV / Strava JSON 片段。

统一输出结构::

    {
      "sport": "running",
      "sub_sport": None,
      "title": None | str,
      "start_time": float,          # Unix 秒
      "points": [ {t, lat, lon, alt, hr, cad, power, speed}, ... ],
      "summary": {...},             # 文件自带的汇总，可能为空
      "source_format": "gpx" | "tcx" | "fit" | "csv" | "json",
    }

设计原则：解析器只负责"把文件读成点序列"，所有指标重算交给 analysis.py，
这样不同来源的数据口径一致。
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import math
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from . import fitparse


class ParseError(Exception):
    pass


# ---------------------------------------------------------------- 工具

def _local(tag: str) -> str:
    """去掉 XML 命名空间，只保留本地名。

    ``{http://www.topografix.com/GPX/1/1}trkpt`` -> ``trkpt``
    ``{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}hr`` -> ``hr``
    """
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    if ":" in tag:
        return tag.rsplit(":", 1)[1].lower()
    return tag.lower()


def _find_in(elem, name: str):
    """在整棵子树里按本地名找第一个元素（GPX 的 hr/cad/power 嵌在 extensions 里）。"""
    for child in elem.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text(elem, *paths: str) -> str | None:
    """按本地标签名查找子元素文本。

    支持 "a/b" 形式的嵌套路径；单段名找不到时会回退到整棵子树搜索，
    因为 GPX 的扩展字段常被包在 <extensions><gpxtpx:TrackPointExtension> 里。
    """
    for path in paths:
        parts = path.split("/")
        node = elem
        for part in parts:
            if node is None:
                break
            node = next((c for c in list(node) if _local(c.tag) == part), None)
        if node is None and len(parts) == 1:
            node = _find_in(elem, parts[0])
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return None


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_time(value: str | None) -> float | None:
    """解析 ISO 8601 / 常见日期时间字符串为 Unix 秒（UTC）。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    candidates = [
        text,
        text.replace(" ", "T", 1),
    ]
    for cand in candidates:
        try:
            dt = datetime.fromisoformat(cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(text[:19] if len(text) >= 19 else text, fmt)
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- GPX

def parse_gpx(text: str) -> dict:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ParseError(f"GPX 不是合法 XML: {exc}") from exc

    points: list[dict] = []
    for elem in root.iter():
        if _local(elem.tag) != "trkpt":
            continue
        lat = _float(elem.get("lat"))
        lon = _float(elem.get("lon"))
        if lat is None or lon is None:
            continue
        pt = {
            "t": parse_time(_text(elem, "time")),
            "lat": lat, "lon": lon,
            "alt": _float(_text(elem, "ele")),
            "hr": _float(_text(elem, "hr", "heartrate")),
            "cad": _float(_text(elem, "cad", "cadence")),
            "power": _float(_text(elem, "power", "watts")),
            "speed": _float(_text(elem, "speed")),
            "temp": _float(_text(elem, "atemp", "temp")),
        }
        if pt["speed"] is None and pt["t"] is None:
            pass
        points.append(pt)

    if not points:
        # 可能是 <rtept> 路线
        for elem in root.iter():
            if _local(elem.tag) != "rtept":
                continue
            lat, lon = _float(elem.get("lat")), _float(elem.get("lon"))
            if lat is None or lon is None:
                continue
            points.append({"t": parse_time(_text(elem, "time")), "lat": lat, "lon": lon,
                           "alt": _float(_text(elem, "ele"))})
    if not points:
        raise ParseError("GPX 中没有找到轨迹点（trkpt）")

    times = [p["t"] for p in points if p["t"] is not None]
    if not times:
        raise ParseError("GPX 轨迹点缺少时间戳，无法计算速度与配速")

    trk_name = _text(root, "name") or None
    sport = "generic"
    for elem in root.iter():
        if _local(elem.tag) == "type" and elem.text:
            sport = normalize_sport(elem.text)
            break
    if sport == "generic":
        # iGPSPORT 导出的 GPX 没有 <type>，运动类型只写在轨迹名或 metadata 文本里
        # （例：<trk><name>户外骑行</name>、<metadata><link><text>户外骑行</text>）。
        # 不补这一步，真实骑行会被归成 generic，教练的同族统计就看不到它。
        for elem in root.iter():
            if _local(elem.tag) not in ("name", "text", "desc"):
                continue
            if not elem.text or not elem.text.strip():
                continue
            guess = normalize_sport(elem.text.strip())
            if guess != "generic":
                sport = guess
                break

    return {
        "sport": sport,
        "sub_sport": None,
        "title": trk_name,
        "start_time": min(times),
        "points": points,
        "summary": {},
        "source_format": "gpx",
    }


# ---------------------------------------------------------------- TCX

def parse_tcx(text: str) -> dict:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ParseError(f"TCX 不是合法 XML: {exc}") from exc

    points: list[dict] = []
    for elem in root.iter():
        if _local(elem.tag) != "trackpoint":
            continue
        pt = {
            "t": parse_time(_text(elem, "time")),
            "lat": _float(_text(elem, "position/latitudedegrees")),
            "lon": _float(_text(elem, "position/longitudedegrees")),
            "alt": _float(_text(elem, "altitudemeters")),
            "hr": _float(_text(elem, "heartratebpm/value")),
            "cad": _float(_text(elem, "cadence")),
            "power": _float(_text(elem, "extensions/tpx/watts", "watts")),
            "speed": _float(_text(elem, "extensions/tpx/speed", "speed")),
        }
        if pt["t"] is not None:
            points.append(pt)

    if not points:
        raise ParseError("TCX 中没有找到 trackpoint")

    sport_raw = _text(root, "sport") or "generic"
    # TCX 的运动类型在 <Activity Sport="Biking"> 属性里，而不是 <Sport> 元素
    if sport_raw == "generic":
        for elem in root.iter():
            attr = elem.get("Sport") or elem.get("SportType")
            if attr:
                sport_raw = attr
                break
    sport = normalize_sport(sport_raw)
    # TCX 的 Sport 只区分 Running/Biking/Other，用 Activity 的 Notes 等补充不了，
    # 再尝试从活动名判断
    notes = _text(root, "notes")
    if sport == "generic" and notes:
        sport = normalize_sport(notes)

    times = [p["t"] for p in points if p["t"] is not None]
    return {
        "sport": sport,
        "sub_sport": None,
        "title": None,
        "start_time": min(times),
        "points": points,
        "summary": {},
        "source_format": "tcx",
    }


# ---------------------------------------------------------------- 运动类型归一

SPORT_ALIASES = {
    "run": "running", "running": "running", "trailrun": "trail_running",
    "trail_running": "trail_running", "trail running": "trail_running",
    "treadmill": "treadmill_running", "treadmill_running": "treadmill_running",
    "ride": "cycling", "cycling": "cycling", "bike": "cycling", "biking": "cycling",
    "road_cycling": "road_cycling", "virtualride": "indoor_cycling",
    "virtual_ride": "indoor_cycling", "indoor_cycling": "indoor_cycling",
    "mountain_biking": "mountain_biking", "mtb": "mountain_biking", "ebike": "e_bike_ride",
    "ebike": "e_bike_ride", "ebikeride": "e_bike_ride", "e_bike_ride": "e_bike_ride",
    "gravel_cycling": "gravel_cycling", "gravelride": "gravel_cycling",
    "swim": "swimming", "swimming": "swimming", "pool_swimming": "pool_swimming",
    "open_water_swimming": "open_water_swimming",
    "walk": "walking", "walking": "walking", "hike": "hiking", "hiking": "hiking",
    "mountainbikeride": "mountain_biking", "gravelride": "gravel_cycling",
    "virtualrun": "indoor_running", "virtual_run": "indoor_running",
    "openwaterswim": "open_water_swimming", "open_water_swim": "open_water_swimming",
    "rowing": "rowing", "skiing": "skiing", "nordicski": "skiing",
    "workout": "workout", "weighttraining": "workout", "yoga": "workout",
    "other": "generic", "generic": "generic",
}

# 中文运动名 -> 内部类型（CSV / 手工表格常见）
SPORT_ALIASES_ZH = {
    "跑步": "running", "慢跑": "running", "跑": "running", "路跑": "running",
    "越野跑": "trail_running", "跑步机": "treadmill_running", "场地跑": "track_running",
    "骑行": "cycling", "单车": "cycling", "自行车": "cycling", "公路骑行": "road_cycling",
    "山地骑行": "mountain_biking", "室内骑行": "indoor_cycling", "骑行台": "indoor_cycling",
    "游泳": "swimming", "泳池": "pool_swimming", "泳池游泳": "pool_swimming",
    "公开水域": "open_water_swimming", "徒步": "hiking", "登山": "hiking",
    "步行": "walking", "散步": "walking", "划船": "rowing", "滑雪": "skiing",
    "力量": "workout", "力量训练": "workout", "健身": "workout", "瑜伽": "workout",
}

FAMILIES = {
    "running": "run", "trail_running": "run", "treadmill_running": "run",
    "track_running": "run", "virtual_run": "run", "indoor_running": "run",
    "cycling": "ride", "road_cycling": "ride", "indoor_cycling": "ride",
    "mountain_biking": "ride", "gravel_cycling": "ride", "cyclocross": "ride",
    "e_bike_ride": "ride",
    "swimming": "swim", "pool_swimming": "swim", "open_water_swimming": "swim",
    "walking": "walk", "hiking": "walk",
    "generic": "other", "workout": "other", "rowing": "other", "skiing": "other",
}

SPORT_LABELS = {
    "running": "跑步", "trail_running": "越野跑", "treadmill_running": "跑步机",
    "track_running": "场地跑",
    "cycling": "骑行", "road_cycling": "公路骑行", "indoor_cycling": "室内骑行",
    "mountain_biking": "山地骑行", "gravel_cycling": "砾石骑行", "cyclocross": "公路越野",
    "e_bike_ride": "电助力骑行",
    "swimming": "游泳", "pool_swimming": "泳池游泳", "open_water_swimming": "公开水域",
    "walking": "步行", "hiking": "徒步",
    "rowing": "划船", "skiing": "滑雪", "workout": "力量/其他", "generic": "其他",
}


def normalize_sport(raw: str | None) -> str:
    """把各种写法归一为内部运动类型。

    匹配顺序很重要：先精确匹配，再按别名词长度降序做子串匹配，
    避免 "TrailRun" 命中 "ride"、"SwimRun" 命中 "run" 这类误判。
    """
    if not raw:
        return "generic"
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "")
    compact = re.sub(r"[^\w]", "", key)

    # 中文名优先（避免中文被当成 generic）
    for zh, canonical in sorted(SPORT_ALIASES_ZH.items(), key=lambda kv: -len(kv[0])):
        if zh in key:
            return canonical

    if compact in SPORT_ALIASES:
        return SPORT_ALIASES[compact]
    if key in SPORT_ALIASES:
        return SPORT_ALIASES[key]

    # 子串匹配：长别名优先（trailrun 比 run 更具体）
    normalized_aliases = {}
    for alias, canonical in SPORT_ALIASES.items():
        bare = alias.replace("_", "").replace(" ", "").replace("-", "")
        normalized_aliases[bare] = canonical
    for alias in sorted(normalized_aliases, key=len, reverse=True):
        if len(alias) < 4:
            continue
        if alias in compact:
            return normalized_aliases[alias]
    return "generic"


def sport_family(sport: str) -> str:
    return FAMILIES.get(sport, "other")


def sport_label(sport: str) -> str:
    return SPORT_LABELS.get(sport, sport)


# ---------------------------------------------------------------- CSV（Strava 导出 / 自定义）

_CSV_COLUMNS = {
    "start_time": ["activity date", "date", "start_time", "start time", "日期", "开始时间", "时间"],
    "title": ["activity name", "name", "title", "标题", "名称", "活动名称"],
    "sport": ["activity type", "type", "sport", "运动类型", "类型", "活动类型"],
    "distance": ["distance", "distance_km", "distance (km)", "距离", "距离(km)", "距离（km）"],
    "elapsed": ["elapsed time", "elapsed_time", "elapsed time (s)", "耗时", "总时长"],
    "moving": ["moving time", "moving_time", "moving time (s)", "移动时间", "移动时长"],
    "elevation": ["elevation gain", "elevation_gain", "elevation gain (m)", "爬升", "累计爬升"],
    "avg_hr": ["average heart rate", "avg_hr", "average heart rate (bpm)", "平均心率"],
    "max_hr": ["max heart rate", "max_hr", "maximum heart rate", "最大心率"],
    "calories": ["calories", "卡路里", "消耗热量"],
    "notes": ["notes", "description", "备注", "描述"],
}


def _find_columns(header: list[str]) -> dict[str, int]:
    """把规范的键映射到 CSV 列索引。"""
    lowered = [h.strip().lower() for h in header]
    mapping: dict[str, int] = {}
    for key, aliases in _CSV_COLUMNS.items():
        for alias in aliases:
            if alias in lowered:
                mapping[key] = lowered.index(alias)
                break
    return mapping


_OUTPUT_COLUMNS = {
    "start_time", "title", "sport", "distance", "elapsed", "moving",
    "elevation", "avg_hr", "max_hr", "calories", "notes", "description",
    "filename", "activity_id", "id", "max_speed", "average_speed", "relative_effort",
    "commute", "gear", "elevation_loss", "prefered_per_unit",
}


def _looks_like_export_header(header: list[str]) -> bool:
    lowered = {h.strip().lower() for h in header}
    tracked = [h for h in lowered if h in _OUTPUT_COLUMNS or h in
               {"activity date", "activity type", "distance", "moving time", "elapsed time"}]
    return len(tracked) >= 4


def parse_csv(text: str) -> list[dict]:
    """解析 CSV。返回"摘要型活动"列表，每条可能没有轨迹点。

    兼容两类文件：
    * Strava 批量导出 activities.csv（含 activity_id / filename 关联 GPX/FIT）
    * 自定义手工记录表（日期/类型/距离/时长/心率）
    """
    sample = text[:4096]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        pass

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ParseError("CSV 是空的")
    header = rows[0]
    mapping = _find_columns(header)
    if not mapping or "start_time" not in mapping:
        raise ParseError(
            "CSV 缺少可识别的表头。至少需要日期列（例如 activity date / 日期）。"
        )

    out = []
    for row in rows[1:]:
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))

        def cell(key: str) -> str:
            idx = mapping.get(key)
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        start = parse_time(cell("start_time"))
        if start is None:
            continue
        distance = _num(cell("distance"))
        # 距离单位判断：Strava 导出是米，自定义表常用公里
        lowered_header = [h.strip().lower() for h in header]
        unit_km = any("(km)" in h or "（km）" in h or h in ("distance_km",) for h in lowered_header)
        if distance is not None:
            distance = distance * 1000.0 if unit_km else distance
            if distance > 3_000_000:
                distance = distance / 1000.0

        minutes = _duration_minutes(cell("moving")) or _duration_minutes(cell("elapsed"))
        avg_speed = _num(cell("avg_speed"))
        max_speed = _num(cell("max_speed"))

        out.append({
            "sport": normalize_sport(cell("sport") or "generic"),
            "title": cell("title") or None,
            "start_time": start,
            "distance_m": distance,
            "moving_seconds": minutes,
            "elevation_m": _num(cell("elevation")),
            "avg_hr": _num(cell("avg_hr")),
            "max_hr": _num(cell("max_hr")),
            "calories": _num(cell("calories")),
            "avg_speed_kmh": avg_speed,
            "max_speed_kmh": max_speed,
            "notes": cell("notes") or cell("description") or None,
            "source_format": "csv",
        })
    if not out:
        raise ParseError("CSV 中没有解析出有效行")
    return out


def _num(value: str) -> float | None:
    if not value:
        return None
    cleaned = value.replace(",", "").replace(" ", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(match.group(0)) if match else None


def _duration_minutes(value: str) -> float | None:
    """解析时长，统一返回**秒**。

    支持 "1:02:03"、"45:00"、"3600" 三种写法。纯数字的判定：
    >= 300 视为秒（Strava 导出与手工表普遍用秒），否则视为分钟。
    """
    if not value:
        return None
    text = value.strip()
    if ":" in text:
        parts = text.split(":")
        try:
            nums = [int(float(p)) for p in parts]
        except ValueError:
            return None
        seconds = 0
        for n in nums:
            seconds = seconds * 60 + n
        return float(seconds)
    raw = _num(text)
    if raw is None:
        return None
    return float(raw) if raw >= 300 else raw * 60.0


# ---------------------------------------------------------------- Strava JSON 片段

def parse_strava_json(text: str) -> dict | list[dict]:
    """解析 Strava JSON（活动 + streams）。

    实现只有一份，放在 strava_web 里；这里做懒加载转发，
    避免两处逻辑各自演化（曾经因此导致室内活动导不进来）。
    """
    from .strava_web import parse_strava_json as _impl
    return _impl(text)


# ---------------------------------------------------------------- 入口

def detect_and_parse(filename: str, data: bytes) -> dict | list[dict]:
    """按扩展名与魔数分派解析。返回单条活动 dict 或摘要活动 list。

    支持 gzip 压缩的容器（Strava 批量导出里常见 `xxx.fit.gz`）。
    """
    name = (filename or "").lower()

    # gzip 容器：解压后按原名继续识别
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError as exc:
            raise ParseError(f"gzip 解压失败: {exc}") from exc
        if name.endswith(".gz"):
            name = name[:-3]

    head = data[:64]

    if name.endswith(".fit") or head[8:12] == b".FIT":
        return fitparse.parse_fit(data, filename)

    text = _decode_text(data)

    if head.lstrip().startswith(b"{") or name.endswith(".json"):
        return parse_strava_json(text)

    if name.endswith(".gpx") or "<gpx" in text[:2000].lower():
        return parse_gpx(text)

    if name.endswith(".tcx") or "<trainingcenterdatabase" in text[:2000].lower():
        return parse_tcx(text)

    if name.endswith(".csv") or name.endswith(".txt") or "," in text[:500]:
        return parse_csv(text)

    raise ParseError(f"无法识别文件格式: {filename}")


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")
