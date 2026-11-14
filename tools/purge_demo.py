"""清理演示数据：只保留真实导入的活动。

判定规则：文件名来自 sample-data 的样例（run_*.gpx / ride_*.fit / trainer_*.tcx /
road_ride.fit / activities.csv 等）视为演示行，删除；其余保留。

用法：
    python tools/purge_demo.py --dry-run   # 只看会删什么
    python tools/purge_demo.py             # 真正删除
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SAMPLE_FILES = {
    "run_trail.gpx", "run_park.gpx", "run_long.gpx", "ride_loop.gpx",
    "walk_evening.gpx", "trainer_ride.tcx", "road_ride.fit", "activities.csv",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(ROOT, "data", "pace.db"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from pace.store import open_store
    store = open_store(args.db)

    rows = store.conn.execute(
        "SELECT id, filename, sport, distance_m, start_time, external_id FROM activities"
    ).fetchall()
    demo, keep = [], []
    for r in rows:
        name = (r["filename"] or "").split("/")[-1].split("\\")[-1]
        if name in SAMPLE_FILES:
            demo.append(r)
        else:
            keep.append(r)

    print(f"总计 {len(rows)} 条：演示 {len(demo)} 条，保留 {len(keep)} 条")
    for r in demo:
        print(f"  删除  #{r['id']:>3}  {r['filename']}  {r['sport']}")
    for r in keep:
        print(f"  保留  #{r['id']:>3}  {r['filename']}  {r['sport']}")

    if args.dry_run:
        print("\n（dry-run，未改动任何数据）")
        store.close()
        return 0

    ids = [r["id"] for r in demo]
    if ids:
        marks = ",".join("?" for _ in ids)
        store.conn.execute(f"DELETE FROM activities WHERE id IN ({marks})", ids)
        # 分段成绩随活动级联删除
        store.conn.execute(
            f"DELETE FROM segment_efforts WHERE activity_id IN ({marks})", ids)
        store.conn.commit()

    print(f"\n已删除 {len(ids)} 条演示活动，剩余 {store.count()} 条")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
