"""打一个可以直接拖到 Cloudflare Pages 的 zip（根目录就是 index.html）。

用法: python tools/build_pages_zip.py
产物: dist/PACE-手机版-pages-<sha>.zip
"""
from __future__ import annotations

import os
import re
import subprocess
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
DIST = os.path.join(ROOT, "dist")


def git_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                      cwd=ROOT, text=True, timeout=10)
        return out.strip()
    except Exception:  # noqa: BLE001
        import time
        return time.strftime("%Y%m%d%H%M")


def main() -> int:
    os.makedirs(DIST, exist_ok=True)
    sha = git_sha()
    out = os.path.join(DIST, f"PACE-手机版-pages-{sha}.zip")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, dirnames, filenames in os.walk(WEB):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git")]
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, WEB).replace(os.sep, "/")
                data = open(full, "rb").read()
                if rel == "sw.js":
                    text = data.decode("utf-8")
                    text = re.sub(r"pace-shell-[0-9a-zA-Z]+", f"pace-shell-{sha}", text, count=1)
                    data = text.encode("utf-8")
                z.writestr(rel, data)
                count += 1
    print(f"完成: {out}")
    print(f"文件数 {count}，大小 {os.path.getsize(out) / 1048576:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
