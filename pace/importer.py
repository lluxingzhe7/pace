"""导入管线（与 HTTP 层解耦）。

把"解析 → 分析 → 入库 → 匹配分段"抽出来，让 HTTP 上传、设备同步、命令行
共用同一份逻辑。返回结构与前端期望的导入结果一致。
"""
from __future__ import annotations

import hashlib
import json
import traceback

from . import analysis, analytics, parsers
from .parsers import ParseError, sport_family
from .summary_build import build_from_summary


def strava_id_from_json(data: bytes) -> str | None:
    try:
        payload = json.loads(data.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    node = payload.get("activity") if isinstance(payload, dict) else None
    if isinstance(node, dict) and node.get("id"):
        return f"strava:{node['id']}"
    if isinstance(payload, dict) and payload.get("id") and payload.get("start_date"):
        return f"strava:{payload['id']}"
    return None


def default_title(sport: str, start_ts: float) -> str:
    from datetime import datetime, timedelta, timezone
    local = datetime.fromtimestamp(start_ts, timezone(timedelta(hours=8)))
    hour = local.hour
    slot = ("早晨" if 5 <= hour < 11 else "上午" if 11 <= hour < 13
            else "下午" if 13 <= hour < 18 else "傍晚" if 18 <= hour < 21 else "夜间")
    return f"{slot}{parsers.sport_label(sport)}"


class Importer:
    """把文件变成库里的活动。线程安全由调用方（API 的全局锁）保证。"""

    def __init__(self, store, analysis_options, *, on_created=None):
        self.store = store
        self.analysis_options = analysis_options   # callable -> dict
        self.on_created = on_created                # callable(activity_id) 可选

    # ---------------------------------------------------------- 入口

    def import_file(self, filename: str, data: bytes) -> dict:
        if not data:
            return {"file": filename, "status": "skipped", "reason": "空文件"}
        try:
            parsed = self.parse_any(filename, data)
        except (ParseError, ValueError) as exc:
            return {"file": filename, "status": "error", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"file": filename, "status": "error", "reason": f"解析失败: {exc}"}

        records = parsed if isinstance(parsed, list) else [parsed]
        imported, skipped, errors = [], [], []
        for rec in records:
            try:
                result = self.persist(filename, data, rec)
                # 合并也算「有新东西进来」，不能归到 duplicates 里，否则界面会说「已存在，已跳过」
                if result["created"] or result.get("merged"):
                    imported.append(result)
                else:
                    skipped.append(result)
                if result["created"] and self.on_created:
                    try:
                        self.on_created(result["id"])
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                errors.append(str(exc))
        return {
            "file": filename,
            "status": "ok" if (imported or skipped) else "error",
            "imported": imported,
            "duplicates": skipped,
            "errors": errors,
        }

    # ---------------------------------------------------------- 解析

    def parse_any(self, filename: str, data: bytes):
        """先走标准格式；失败后尝试 Strava 网页存档（HTML / 内嵌 JSON）。"""
        try:
            return parsers.detect_and_parse(filename, data)
        except ParseError as first_error:
            lower = (filename or "").lower()
            text = parsers._decode_text(data)
            looks_html = "<html" in text[:4000].lower() or lower.endswith((".html", ".htm"))
            looks_strava_json = "__NEXT_DATA__" in text[:200000] or "latlng" in text[:200000]
            if not (looks_html or looks_strava_json):
                # 华为健康导出的 JSON 也走这条兜底
                if lower.endswith(".json"):
                    return self.parse_huawei_json(text)
                raise first_error
            from . import strava_web
            try:
                if looks_html:
                    try:
                        return strava_web.parse_saved_activity_page(text)
                    except ParseError:
                        return strava_web.parse_saved_activity_list(text)
                return strava_web.parse_strava_json(text)
            except ParseError:
                raise first_error

    def parse_huawei_json(self, text: str):
        """华为运动健康导出包的 JSON 结构（字段名因版本而异，做宽松匹配）。"""
        payload = json.loads(text)
        rows = payload if isinstance(payload, list) else [payload]
        out = []
        for node in rows:
            if not isinstance(node, dict):
                continue
            # 常见字段：sportType / startTime / distance / duration / averageHeartRate
            sport_raw = (node.get("sportType") or node.get("sport_type")
                         or node.get("activityType") or node.get("type"))
            start = node.get("startTime") or node.get("start_time") or node.get("beginTime")
            if not start or not sport_raw:
                continue
            ts = parsers.parse_time(str(start))
            if ts is None:
                continue
            out.append({
                "sport": parsers.normalize_sport(str(sport_raw)),
                "title": node.get("name") or "华为运动健康记录",
                "start_time": ts,
                "distance_m": _num(node.get("distance") or node.get("totalDistance")),
                "moving_seconds": _num(node.get("duration") or node.get("totalTime")),
                "elevation_m": _num(node.get("totalElevation") or node.get("elevationGain")),
                "avg_hr": _num(node.get("averageHeartRate") or node.get("avgHeartRate")),
                "max_hr": _num(node.get("maxHeartRate")),
                "external_id": f"huawei:{node.get('id') or int(ts)}",
                "source_format": "huawei-json",
            })
        if not out:
            raise ParseError("不是可识别的 JSON。支持 GPX/TCX/FIT/CSV，或厂商导出包里的标准文件。")
        return out

    # ---------------------------------------------------------- 入库

    def persist(self, filename: str, data: bytes, rec: dict) -> dict:
        options = self.analysis_options()
        source_format = rec.get("source_format") or "import"
        sport = rec.get("sport") or "generic"
        title = rec.get("title")
        file_hash = None
        external = rec.get("external_id")
        if not external and source_format == "json":
            external = strava_id_from_json(data)

        if rec.get("points"):
            built = analysis.analyze(rec["points"], sport, options)
            summary = built["summary"]
            file_hash = hashlib.sha256(data).hexdigest()
            source_dist = (rec.get("summary") or {}).get("total_distance")
            if source_dist:
                src = float(source_dist)
                if abs(summary["distance_m"] - src) > max(30.0, src * 0.02):
                    summary["source_distance_m"] = src
        else:
            normalized = dict(rec)
            normalized.setdefault("distance_m",
                                  (rec.get("summary") or {}).get("total_distance"))
            normalized.setdefault("moving_seconds",
                                  (rec.get("summary") or {}).get("total_timer"))
            normalized.setdefault("elevation_m",
                                  (rec.get("summary") or {}).get("ascent"))
            normalized.setdefault("avg_hr", (rec.get("summary") or {}).get("avg_hr"))
            built = build_from_summary(normalized, options)
            summary = built["summary"]
            if not external:
                external = (f"{source_format}:{rec['start_time']}:{sport}:"
                            f"{round(float(normalized.get('distance_m') or 0), 1)}")

        activity = {
            "source": source_format,
            "external_id": external,
            "file_hash": file_hash,
            "filename": filename,
            "sport": sport,
            "title": title or default_title(sport, summary["start_time"]),
            "notes": rec.get("notes"),
            "summary": summary,
            "streams": built.get("streams") or {},
            "route": built.get("route") or [],
            "source_distance_m": summary.get("source_distance_m"),
            "raw_meta": {"source_format": source_format,
                         "file_summary": rec.get("summary") or {}},
        }

        # 同一趟运动可能有 GPX 和 FIT 两份：FIT 有心率/功率，GPX 只有轨迹。
        # 完全相同的文件由 upsert 去重；不同格式的同一次运动在这里合并成一条。
        similar = None
        if file_hash:
            known = self.store.conn.execute(
                "SELECT activity_id FROM activity_files WHERE file_hash=?", (file_hash,)
            ).fetchone()
            same_row = self.store.conn.execute(
                "SELECT id FROM activities WHERE file_hash=?", (file_hash,)
            ).fetchone()
            if not known and not same_row:
                similar = self.store.find_similar_activity(
                    start_time=summary["start_time"],
                    distance_m=summary["distance_m"], sport=sport)
        if similar:
            return self._merge_into(similar, activity, summary, built, sport)

        activity_id, created = self.store.upsert_activity(activity)

        matched = 0
        if created and summary.get("has_gps"):
            matched = match_segments(self.store, activity_id)
        return {"id": activity_id, "created": created, "title": activity["title"],
                "sport": sport, "distance_m": summary["distance_m"],
                "moving_seconds": summary["moving_seconds"],
                "has_hr": bool(summary.get("avg_hr")),
                "has_power": bool(summary.get("avg_power")),
                "segments_matched": matched}


    def _merge_into(self, similar: dict, activity: dict, summary: dict,
                    built: dict, sport: str) -> dict:
        """把新文件并进同一次运动的已有记录。

        关键点：以「带轨迹」的那份为底（通常是 GPX），把另一份的心率/踏频/功率
        按绝对时间叠上去。否则用不带 GPS 的 FIT 覆盖，地图就没了。
        """
        opts = self.analysis_options()
        existing = self.store.get_activity(similar["id"], full=True) or {}
        ex_streams = existing.get("streams") or {}
        new_streams = built.get("streams") or {}
        ex_gps = bool(existing.get("has_gps"))
        new_gps = bool(summary.get("has_gps"))
        ex_start = float(existing.get("start_time") or summary["start_time"])

        base_is_new = new_gps and not ex_gps
        if base_is_new:
            streams = _overlay_streams(new_streams, ex_streams, summary["start_time"], ex_start)
            rec = dict(activity)
            rec["streams"] = streams
            rec["summary"] = _with_sensors(
                summary, streams, sport,
                moving=float(summary.get("moving_seconds") or 0),
                distance=float(summary.get("distance_m") or 0),
                gain=float(summary.get("elevation_gain_m") or 0), opts=opts)
            self.store.merge_activity(existing["id"], rec, replace=True)
            final = rec["summary"]
            base = "new"
        else:
            streams = _overlay_streams(ex_streams, new_streams, ex_start, summary["start_time"])
            patch = _sensor_metrics(
                streams, sport,
                moving=float(existing.get("moving_seconds") or 0),
                distance=float(existing.get("distance_m") or 0),
                gain=float(existing.get("elevation_gain_m") or 0), opts=opts)
            self.store.update_streams(existing["id"], streams, patch)
            final = dict(existing)
            final.update(patch)
            # 记住新文件的指纹，但它不覆盖轨迹类数据
            self.store._remember_file(activity.get("file_hash"), existing["id"],
                                      activity.get("filename"), activity.get("source"))
            base = "existing"

        self.store.update_sport_if_generic(existing["id"], sport)
        return {"id": existing["id"], "created": False, "merged": True,
                "replaced": base == "new",
                "title": existing.get("title") or activity["title"],
                "sport": sport,
                "distance_m": final.get("distance_m"),
                "moving_seconds": final.get("moving_seconds"),
                "has_hr": bool(final.get("avg_hr")),
                "has_power": bool(final.get("avg_power")),
                "segments_matched": 0}


def _overlay_streams(base: dict, other: dict, base_start: float, other_start: float) -> dict:
    """把 other 的心率/踏频/功率按绝对时间叠加到 base 的采样网格上。

    base 的空通道才填，已经有的不覆盖（避免反复导入时来回横跳）。
    两份流的起点可能差几分钟，靠绝对时间对齐。
    """
    t_base = base.get("t") or []
    t_other = other.get("t") or []
    if not t_base or not t_other:
        return dict(base)
    base_abs = [base_start + float(x) for x in t_base]
    other_abs = [other_start + float(x) for x in t_other]
    gaps = sorted(b - a for a, b in zip(other_abs, other_abs[1:]) if b > a)
    median_gap = gaps[len(gaps) // 2] if gaps else 1.0
    tolerance = max(6.0, median_gap * 1.5)
    out = {k: list(v) if isinstance(v, list) else v for k, v in base.items()}
    out.setdefault("t", list(t_base))
    for key in ("hr", "cad", "pwr"):
        other_vals = other.get(key) or []
        if not other_vals or all(v is None for v in other_vals):
            continue
        base_vals = list(out.get(key) or [])
        if len(base_vals) < len(t_base):
            base_vals += [None] * (len(t_base) - len(base_vals))
        filled = sum(1 for v in base_vals if v is not None)
        if filled > len(base_vals) * 0.5:
            continue
        j = 0  # 每个通道各自从头对齐
        for i, at in enumerate(base_abs):
            while j + 1 < len(other_abs) and abs(other_abs[j + 1] - at) <= abs(other_abs[j] - at):
                j += 1
            if j < len(other_vals) and abs(other_abs[j] - at) <= tolerance:
                base_vals[i] = other_vals[j]
        out[key] = base_vals
    return out


def _sensor_metrics(streams: dict, sport: str, *, moving: float, distance: float,
                    gain: float, opts: dict) -> dict:
    """根据合并后的流，重算心率/功率相关的汇总字段。"""
    from .coach.activity import _norm_power, zone_times

    hr_max = float(opts.get("max") or 190)
    hr_rest = float(opts.get("rest") or 55)
    fam = sport_family(sport)
    out: dict = {}
    hr = [float(v) for v in (streams.get("hr") or []) if v]
    if hr:
        avg_hr = sum(hr) / len(hr)
        out["avg_hr"] = round(avg_hr, 1)
        out["max_hr"] = round(max(hr), 1)
        out["hr_zones"] = {k: round(v, 1) for k, v in
                           zone_times(streams, hr_max, hr_rest).items()}
        load, method = analysis.training_load(
            moving, avg_hr=avg_hr, sport_family=fam, distance_m=distance,
            elevation_gain=gain, hr_max=hr_max, hr_rest=hr_rest)
        out["training_load"] = load
        out["load_method"] = method
    pwr = [float(v) for v in (streams.get("pwr") or []) if v is not None]
    if pwr:
        out["avg_power"] = round(sum(pwr) / len(pwr), 1)
        t = streams.get("t") or []
        dt = (t[1] - t[0]) if len(t) > 1 else 1.0
        npv = _norm_power(pwr, dt)
        if npv:
            out["np_power"] = round(npv, 1)
    return out


def _with_sensors(summary: dict, streams: dict, sport: str, *, moving: float,
                  distance: float, gain: float, opts: dict) -> dict:
    out = dict(summary)
    out.update(_sensor_metrics(streams, sport, moving=moving, distance=distance,
                               gain=gain, opts=opts))
    return out


def match_segments(store, activity_id: int) -> int:
    """新活动导入后，尝试匹配所有已有分段。"""
    segs = store.list_segments()
    if not segs:
        return 0
    full = store.get_activity(activity_id, full=True)
    if not full:
        return 0
    matched = 0
    for seg in segs:
        seg_full = store.get_segment(seg["id"])
        if not seg_full:
            continue
        effort = analytics.match_segment(seg_full, full)
        if effort:
            store.add_effort(seg["id"], activity_id, effort)
            matched += 1
    store.refresh_effort_bests()
    return matched


def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
