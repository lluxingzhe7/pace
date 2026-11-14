#!/usr/bin/env python3
"""PACE 启动器。

用法:
    python run.py                 # 启动并打开浏览器
    python run.py --port 9000     # 指定端口
    python run.py --no-open       # 不自动打开浏览器
    python run.py --seed-demo     # 首次启动时导入示例数据

只依赖 Python 3.11+ 标准库，不需要 pip install。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(ROOT, "data", "pace.db")
SAMPLE_DIR = os.path.join(ROOT, "sample-data")


def wait_for_server(url: str, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "api/health", timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def import_samples(port: int) -> None:
    """把 sample-data 里的文件通过 HTTP 接口导入，复用服务端去重逻辑。"""
    files = []
    for name in sorted(os.listdir(SAMPLE_DIR)):
        path = os.path.join(SAMPLE_DIR, name)
        if os.path.isfile(path) and name.lower().endswith((".gpx", ".tcx", ".fit", ".csv")):
            files.append(path)
    if not files:
        print("没有找到示例数据，先运行 python tools/make_sample_data.py")
        return
    for path in files:
        with open(path, "rb") as fh:
            payload = fh.read()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/import",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": os.path.basename(path),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                print(f"  导入 {os.path.basename(path)} -> {resp.status}")
        except Exception as exc:
            print(f"  导入失败 {os.path.basename(path)}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="PACE 本地运动数据分析")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=DATABASE)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--seed-demo", action="store_true", help="导入 sample-data 示例数据")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)

    sys.path.insert(0, ROOT)
    from pace import server

    port = server.find_free_port(args.port)
    httpd, app = server.build_server(args.db, os.path.join(ROOT, "web"), port)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 52)
    print("  PACE · 运动数据分析")
    print("=" * 52)
    print(f"  地址   {url}")
    print(f"  数据库 {args.db}")
    print(f"  活动数 {app.store.count()}")
    print("  按 Ctrl+C 停止")
    print("=" * 52)

    import threading
    stop = threading.Event()

    def serve_loop():
        """serve_forever 意外退出时自动重启，别让一个客户端断连拖垮整个服务。"""
        while not stop.is_set():
            try:
                httpd.serve_forever(poll_interval=0.4)
                return
            except Exception as exc:  # noqa: BLE001
                if stop.is_set():
                    return
                print(f"  服务线程异常，2 秒后自动恢复：{exc}")
                time.sleep(2)

    thread = threading.Thread(target=serve_loop, daemon=True)
    thread.start()
    app.start_auto_sync()

    if args.seed_demo:
        if wait_for_server(url):
            print("正在导入示例数据…")
            import_samples(port)
            print(f"完成，现在有 {app.store.count()} 条活动")
        else:
            print("服务未就绪，跳过示例导入")

    if not args.no_open:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        while thread.is_alive():
            thread.join(timeout=0.6)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        stop.set()
        app.stop_auto_sync()
        httpd.shutdown()
        app.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
