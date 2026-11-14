"""在线备份：用 SQLite 的 VACUUM INTO 给每个库做一致性快照。

用法（容器内）: python tools/backup_db.py --data /data --out /data/backups
宿主机 cron 示例:
    0 4 * * * cd /opt/pace && docker compose exec -T pace python tools/backup_db.py >> data/backups/cron.log 2>&1
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import time


def databases(data_dir: str) -> list[str]:
    found = []
    found += sorted(glob.glob(os.path.join(data_dir, "*.db")))
    found += sorted(glob.glob(os.path.join(data_dir, "users", "*", "*.db")))
    return found


def label(path: str, data_dir: str) -> str:
    rel = os.path.relpath(path, data_dir).replace(os.sep, "-")
    return rel[:-3] if rel.endswith(".db") else rel


def main() -> int:
    parser = argparse.ArgumentParser(description="PACE 数据库在线备份")
    parser.add_argument("--data", default=os.environ.get("PACE_DATA", "/data"))
    parser.add_argument("--out", default="")
    parser.add_argument("--keep-days", type=int, default=14)
    args = parser.parse_args()
    out_dir = args.out or os.path.join(args.data, "backups")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    count = 0
    for path in databases(args.data):
        if not os.path.isfile(path):
            continue
        dst = os.path.join(out_dir, f"{label(path, args.data)}-{stamp}.db")
        try:
            con = sqlite3.connect(path, timeout=20)
            con.execute("VACUUM INTO ?", (dst,))
            con.close()
            count += 1
            print(f"  backed up {path} -> {dst}")
        except sqlite3.Error as exc:
            print(f"  FAILED {path}: {exc}")
    # 清理过期备份
    cutoff = time.time() - args.keep_days * 86400
    for old in glob.glob(os.path.join(out_dir, "*.db")):
        try:
            if os.path.getmtime(old) < cutoff:
                os.remove(old)
        except OSError:
            pass
    print(f"done: {count} databases -> {out_dir}")
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
