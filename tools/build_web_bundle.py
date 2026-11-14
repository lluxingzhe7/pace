"""把 Python 包打成 web/py/pace.zip，供纯前端模式下的 Pyodide 加载。

改了 pace/ 里任何 .py，都要跑一次（Pages 工作流里会自动跑）。
"""
from __future__ import annotations

import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "web", "py", "pace.zip")


def main() -> int:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "pace")):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                z.write(full, os.path.relpath(full, ROOT).replace(os.sep, "/"))
                count += 1
    print(f"built {OUT}: {count} files, {os.path.getsize(OUT)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
