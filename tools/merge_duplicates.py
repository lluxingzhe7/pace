"""把「同一次运动的多份文件」合并成一条。

同一趟骑行可能同时有 GPX（只有轨迹）和 FIT（含心率/功率）两份文件，
以前会变成两条记录。这个工具扫描库里的近似重复项，保留数据最全的那条，
把其余文件的指纹挂到它名下后删除多余记录。

用法:
    python tools/merge_duplicates.py --dry-run
    python tools/merge_duplicates.py
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace.store import _channel_score, open_store

DB = os.path.join(ROOT, "data", "pace.db")


def _richness(act: dict) -> tuple:
    """排序用：通道越全越好，其次采样点越多越好。"""
    return (_channel_score(act.get("streams")), act.get("point_count") or 0)


def find_groups(store) -> list[list[dict]]:
    rows = store.conn.execute(
        "SELECT id, sport, start_time, distance_m FROM activities ORDER BY start_time"
    ).fetchall()
    used: set[int] = set()
    groups: list[list[dict]] = []
    for row in rows:
        if row["id"] in used:
            continue
        group = [store.get_activity(row["id"], full=True)]
        used.add(row["id"])
        for other in rows:
            if other["id"] in used:
                continue
            same = store.find_similar_activity(
                start_time=other["start_time"],
                distance_m=other["distance_m"],
                sport=other["sport"],
                exclude={other["id"]},
            )
            if same and same["id"] in {g["id"] for g in group}:
                group.append(store.get_activity(other["id"], full=True))
                used.add(other["id"])
        if len(group) > 1:
            groups.append(group)
    return groups


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    store = open_store(args.db)
    try:
        groups = find_groups(store)
        if not groups:
            print("没有发现重复记录")
            return 0
        for group in groups:
            group.sort(key=_richness, reverse=True)
            keep = group[0]
            print(f"\n保留 #{keep['id']} {keep.get('filename')} "
                  f"({keep['distance_m']/1000:.2f} km, 心率={'有' if keep.get('avg_hr') else '无'})")
            for extra in group[1:]:
                print(f"  合并 #{extra['id']} {extra.get('filename')} "
                      f"({extra['distance_m']/1000:.2f} km, 心率={'有' if extra.get('avg_hr') else '无'})")
                if args.dry_run:
                    continue
                keep_id = int(keep["id"])
                # 保留数据不动，只把多余文件的指纹挂过来，再删掉那条重复记录
                store.merge_activity(keep_id, {
                    "file_hash": extra.get("file_hash"),
                    "filename": extra.get("filename"),
                    "source": extra.get("source"),
                    "summary": {}, "streams": {}, "route": [],
                }, replace=False)
                store.delete_activity(int(extra["id"]))
        if args.dry_run:
            print("\n（dry-run，未改动数据库）")
        else:
            print(f"\n完成，剩余 {store.count()} 条活动")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
