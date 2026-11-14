"""以脱离父进程的方式启动 PACE 服务（后台 job 会被会话回收，这个不会）。

用法：
    python tools/serve.py            # 启动
    python tools/serve.py --stop     # 停止
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(ROOT, "data", "server.pid")
LOG_FILE = os.path.join(ROOT, "data", "server.log")
PORT = 8765


def healthy(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health",
                                    timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def read_pid() -> int | None:
    try:
        return int(open(PID_FILE, encoding="utf-8").read().strip())
    except Exception:
        return None


def stop() -> int:
    pid = read_pid()
    if not pid:
        print("没有记录 PID")
        return 0
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, text=True)
        print(f"已停止 PID {pid}")
    except Exception as exc:  # noqa: BLE001
        print(f"停止失败：{exc}")
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return 0


def start() -> int:
    if healthy():
        print(f"服务已在运行：http://127.0.0.1:{PORT}/")
        return 0
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    py = sys.executable
    creation = getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with open(LOG_FILE, "ab") as log:
        proc = subprocess.Popen(
            [py, os.path.join(ROOT, "run.py"), "--port", str(PORT), "--no-open"],
            cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            creationflags=creation, close_fds=True,
        )
    open(PID_FILE, "w", encoding="utf-8").write(str(proc.pid))
    print(f"已启动 PID {proc.pid}，日志 {LOG_FILE}")
    for _ in range(40):
        if healthy(1.0):
            print(f"就绪：http://127.0.0.1:{PORT}/")
            return 0
        time.sleep(0.25)
    print("启动超时，检查日志")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    return stop() if args.stop else start()


if __name__ == "__main__":
    raise SystemExit(main())
