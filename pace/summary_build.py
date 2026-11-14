"""把"只有汇总数字、没有轨迹"的记录（CSV 导入）构建成活动。

Strava 的 activities.csv、手工 Excel 导出都属于这一类：只有距离/时长/心率，
没有 GPS 点。这里生成与 analysis.analyze() 同构的 summary，但没有 streams/route。
"""
from __future__ import annotations

from .analysis import training_load, _pace_from
from .parsers import sport_family


def build_from_summary(record: dict, options: dict | None = None) -> dict:
    sport = record.get("sport") or "generic"
    family = sport_family(sport)
    opts = {"rest": 55.0, "max": 190.0}
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})

    start = float(record["start_time"])
    distance = float(record.get("distance_m") or 0.0)
    moving = float(record.get("moving_seconds") or 0.0)
    elapsed = float(record.get("elapsed_seconds") or moving or 0.0)
    elevation = float(record.get("elevation_m") or 0.0)
    avg_hr = record.get("avg_hr")
    max_hr = record.get("max_hr")

    avg_speed = (distance / 1000.0) / (moving / 3600.0) if moving > 0 else 0.0
    pace = _pace_from(distance, moving, family)
    load, method = training_load(
        moving, avg_hr=avg_hr, sport_family=family, distance_m=distance,
        elevation_gain=elevation, hr_max=float(opts["max"]), hr_rest=float(opts["rest"]),
    )

    summary = {
        "start_time": start,
        "distance_m": round(distance, 1),
        "elapsed_seconds": round(elapsed, 1),
        "moving_seconds": round(moving, 1),
        "stopped_seconds": round(max(0.0, elapsed - moving), 1),
        "avg_speed_kmh": round(avg_speed, 2),
        "max_speed_kmh": round(float(record.get("max_speed_kmh") or 0.0), 2),
        "pace_seconds_per_km": round(pace, 1) if pace else None,
        "elevation_gain_m": round(elevation, 1),
        "elevation_loss_m": round(float(record.get("elevation_loss_m") or 0.0), 1),
        "elevation_min_m": None,
        "elevation_max_m": None,
        "avg_hr": round(float(avg_hr), 1) if avg_hr else None,
        "max_hr": round(float(max_hr), 1) if max_hr else None,
        "avg_power": None,
        "np_power": None,
        "max_power": None,
        "hr_zones": {},
        "training_load": round(load, 1),
        "load_method": method,
        "has_gps": False,
        "point_count": 0,
        "best_efforts": [],
        "climbs": [],
        "center": None,
        "bbox": None,
    }
    return {"summary": summary, "streams": {}, "route": []}
