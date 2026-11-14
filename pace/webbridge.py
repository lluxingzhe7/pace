"""在浏览器里跑 PACE：不监听端口，直接把 HTTP 请求喂给现有的 Handler。

Pyodide 里没有 socket，但 server.py 的每个接口都是 Handler 上的方法；
这里造一个「假连接」，把 _dispatch 的输出（状态码/响应头/响应体）截下来，
前端再用一个 fetch 假实现转成标准 Response —— 这样 40 多个接口一行都不用重写。
"""
from __future__ import annotations

import base64
import io
import json
import os
from email.message import Message

from . import server

_APP = None
_HANDLER_CLS = None
DB_PATH = "/data/pace.db"


class _CaptureWFile:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data) -> int:
        self.buf += data
        return len(data)

    def flush(self) -> None:  # noqa: D401
        return None


def init(db_path: str = DB_PATH, *, data_dir: str | None = None) -> dict:
    """初始化（或复用）单用户 App；db_path 指向浏览器内存文件系统里的库。"""
    global _APP, _HANDLER_CLS, DB_PATH
    if _APP is not None:
        return {"ok": True, "db": DB_PATH, "cached": True}
    DB_PATH = db_path
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    _APP = server.App(db_path, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web"), auth_required=False)
    _HANDLER_CLS = server.make_handler(_APP)
    return {"ok": True, "db": db_path, "cached": False}


def _handler():
    if _HANDLER_CLS is None:
        raise RuntimeError("webbridge.init() 还没调用")
    h = object.__new__(_HANDLER_CLS)
    h.wfile = _CaptureWFile()
    h.rfile = io.BytesIO(b"")
    h.client_address = ("127.0.0.1", 0)
    h.server = None
    h.request_version = "HTTP/1.1"
    h.protocol_version = "HTTP/1.1"
    h.close_connection = True
    h.requestline = ""
    h.headers = Message()
    h._status = 200
    h._headers = []

    def send_response(code, message=None):
        h._status = int(code)

    def send_header(key, value):
        h._headers.append((str(key), str(value)))

    def end_headers():
        return None

    def log_message(fmt, *args):
        return None

    h.send_response = send_response
    h.send_header = send_header
    h.end_headers = end_headers
    h.log_message = log_message
    return h


def handle(method: str, path: str, body_b64: str = "", headers: dict | None = None) -> dict:
    """执行一次「请求」，返回 {status, headers, body_b64}。"""
    h = _handler()
    h.path = path
    h.command = method.upper()
    for key, value in (headers or {}).items():
        h.headers[str(key)] = str(value)
    raw = base64.b64decode(body_b64) if body_b64 else b""
    h.headers["Content-Length"] = str(len(raw))
    h.rfile = io.BytesIO(raw)
    try:
        h._dispatch(h.command)
    except Exception as exc:  # noqa: BLE001 - 兜底，别让浏览器端直接白屏
        h._status = 500
        h.wfile.buf = bytearray(json.dumps({"error": f"内部错误: {exc}"},
                                           ensure_ascii=False).encode("utf-8"))
        h._headers = [("Content-Type", "application/json; charset=utf-8")]
    out = {
        "status": h._status,
        "headers": h._headers,
        "body_b64": base64.b64encode(bytes(h.wfile.buf)).decode("ascii"),
    }
    if h.command != "GET":
        out["db_b64"] = dump_db()
    return out


def dump_db() -> str:
    """把库（含 WAL）合并后读成 bytes，交给浏览器存 IndexedDB。"""
    if _APP is None:
        return ""
    con = _APP.store.conn
    try:
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001
        pass
    with open(DB_PATH, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def api_call(name: str, payload_json: str = "{}", method: str = "POST") -> str:
    """给 JS 的便捷入口：直接调用某个 /api 路径。"""
    payload = payload_json or "{}"
    body_b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii") if payload.strip() else ""
    result = handle(method, "/api/" + name.lstrip("/"), body_b64,
                    {"Content-Type": "application/json", "Accept": "application/json"})
    result["body"] = base64.b64decode(result.pop("body_b64")).decode("utf-8", "replace")
    return json.dumps(result, ensure_ascii=False)


def close() -> None:
    global _APP, _HANDLER_CLS
    if _APP is not None:
        try:
            _APP.store.close()
        except Exception:  # noqa: BLE001
            pass
    _APP = None
    _HANDLER_CLS = None
