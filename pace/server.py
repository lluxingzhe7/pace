"""零依赖 HTTP 服务：JSON API + 静态前端。

启动: python -m pace.server --port 8765
或:   python run.py
"""
from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import shutil
import socket
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import analysis, analytics, geo, parsers, routebook
from . import mail as mailer
from .auth import AuthError, AuthStore
from .coach import evaluate as coach_evaluate
from .coach import plan as coach_plan
from .devices import SyncEngine
from .importer import Importer, default_title as _default_title
from .parsers import ParseError, sport_family, sport_label
from .store import Store, open_store
from .summary_build import build_from_summary

APP_VERSION = "1.1.0"
# 这些类型才值得压缩（图片/字体本身已压缩）
COMPRESSIBLE = {"text/html", "text/css", "text/plain", "application/javascript",
                "application/json", "image/svg+xml"}
SESSION_COOKIE = "pace_session"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MB


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


class UserContext:
    """一个用户的运行时对象：独立数据库 + 导入器 + 同步引擎。"""

    def __init__(self, app: "App", user_id: int, db_path: str):
        self.user_id = user_id
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.store = open_store(db_path)

        def _options() -> dict:
            settings = self.store.get_settings()
            return {"max": settings.get("hr_max", 190), "rest": settings.get("hr_rest", 55),
                    "threshold": settings.get("hr_threshold", 168)}

        self.importer = Importer(self.store, _options)
        state_path = os.path.join(os.path.dirname(os.path.abspath(db_path)), "sync-state.json")
        self.sync = SyncEngine(self.store, state_path, self.importer.import_file)

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass


class App:
    """应用上下文：账号库 + 每个用户一套运行时。

    `store` / `importer` / `sync` / `settings()` 都跟着**当前请求的用户**走：
    handler 在处理请求前调用 `set_user()`，用户 id 存在线程本地，
    这样底下几百处 `app.store.xxx` 一行都不用改，也不会串号。
    """

    def __init__(self, db_path: str, web_dir: str, *, auth_required: bool = True):
        self.db_path = os.path.abspath(db_path)
        self.web_dir = web_dir
        self.auth_required = auth_required
        self.data_dir = os.path.dirname(self.db_path)
        self.users_dir = os.path.join(self.data_dir, "users")
        os.makedirs(self.data_dir, exist_ok=True)
        self.lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}
        self._contexts: dict[int, UserContext] = {}
        self._ctx_lock = threading.RLock()
        self._local = threading.local()
        self.auth = AuthStore(os.path.join(self.data_dir, "auth.db")) if auth_required else None
        if not auth_required:
            self._local.uid = 0
        self._auto_stop = threading.Event()
        self._auto_thread: threading.Thread | None = None

    # --------------------------------------------------- 用户上下文

    def set_user(self, user_id: int) -> None:
        self._local.uid = int(user_id)

    @property
    def user_id(self) -> int:
        return int(getattr(self._local, "uid", 0))

    def user_db_path(self, user_id: int) -> str:
        if not self.auth_required or user_id == 0:
            return self.db_path
        return os.path.join(self.users_dir, str(user_id), "pace.db")

    def context(self, user_id: int | None = None) -> UserContext:
        uid = self.user_id if user_id is None else int(user_id)
        if self.auth_required and uid <= 0:
            raise RuntimeError("未登录：没有可用的用户上下文")
        with self._ctx_lock:
            ctx = self._contexts.get(uid)
            if ctx is None:
                ctx = UserContext(self, uid, self.user_db_path(uid))
                self._contexts[uid] = ctx
            return ctx

    def close_user(self, user_id: int) -> None:
        with self._ctx_lock:
            ctx = self._contexts.pop(int(user_id), None)
        if ctx:
            ctx.close()

    @property
    def store(self) -> Store:
        return self.context().store

    @property
    def importer(self) -> Importer:
        return self.context().importer

    @property
    def sync(self) -> SyncEngine:
        return self.context().sync

    def migrate_legacy(self, user_id: int) -> bool:
        """把单用户时期的 data/pace.db 搬进第一个注册的账号。"""
        if not self.auth_required:
            return False
        target = self.user_db_path(user_id)
        if os.path.exists(target) or not os.path.exists(self.db_path):
            return False
        os.makedirs(os.path.dirname(target), exist_ok=True)
        for suffix in ("-wal", "-shm"):
            sidecar = self.db_path + suffix
            if os.path.exists(sidecar):
                try:
                    os.replace(sidecar, target + suffix)
                except OSError:
                    os.remove(sidecar)
        os.replace(self.db_path, target)
        return True

    # ------------------------------------------------------------ 自动同步

    def start_auto_sync(self) -> None:
        if self._auto_thread and self._auto_thread.is_alive():
            return
        self._auto_thread = threading.Thread(target=self._auto_loop, daemon=True,
                                             name="pace-auto-sync")
        self._auto_thread.start()

    def _auto_loop(self) -> None:
        while not self._auto_stop.wait(20):
            try:
                with self._ctx_lock:
                    uids = list(self._contexts.keys())
                for uid in uids:
                    self.set_user(uid)
                    interval = int(self.settings().get("auto_sync_seconds") or 0)
                    if interval <= 0:
                        continue
                    sync = self.sync
                    last = sync.state.data.get("last_sync") or 0
                    if time.time() - last < interval:
                        continue
                    with self.lock:
                        result = sync.sync(limit=20)
                        if result["imported"]:
                            self.invalidate()
                            print(f"[自动同步] 用户 {uid} 导入 {result['imported']} 条活动")
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def stop_auto_sync(self) -> None:
        self._auto_stop.set()

    def settings(self) -> dict:
        return self.store.get_settings()

    def analysis_options(self) -> dict:
        s = self.settings()
        return {"max": s.get("hr_max", 190), "rest": s.get("hr_rest", 55),
                "threshold": s.get("hr_threshold", 168)}

    def cached(self, key: str, ttl: float, builder):
        now = time.time()
        cache_key = f"{self.user_id}:{key}"
        hit = self._cache.get(cache_key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = builder()
        self._cache[cache_key] = (now, value)
        return value

    def invalidate(self) -> None:
        prefix = f"{self.user_id}:"
        for key in [k for k in self._cache if k.startswith(prefix)]:
            self._cache.pop(key, None)


def json_default(obj):
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    raise TypeError(f"无法序列化 {type(obj)}")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        # 浏览器/截图客户端常直接断开，别让 WinError 10054 冒到线程外
        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                self.close_connection = True

        server_version = f"PACE/{APP_VERSION}"
        protocol_version = "HTTP/1.1"

        # -------------------------------------------------- 基础工具

        def log_message(self, fmt, *args):
            if os.environ.get("PACE_QUIET") != "1":
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, status: int, body: bytes, content_type: str,
                  extra_headers: dict | None = None):
            headers = dict(extra_headers or {})
            ctype = content_type.split(";")[0].strip().lower()
            # 3M 带宽的机器上，gzip 能把前端从 450 KB 压到 ~120 KB
            if (len(body) >= 1024 and ctype in COMPRESSIBLE
                    and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
                    and "Content-Encoding" not in headers):
                body = gzip.compress(body, 6)
                headers["Content-Encoding"] = "gzip"
                headers["Vary"] = "Accept-Encoding"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/")
                             else "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_json(self, payload, status: int = 200, extra_headers: dict | None = None):
            body = json.dumps(payload, ensure_ascii=False, default=json_default).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8", extra_headers)

        def send_error_json(self, message: str, status: int = 400):
            self.send_json({"error": message, "status": status}, status=status)

        def read_body(self, limit: int = MAX_UPLOAD_BYTES) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return b""
            if length > limit:
                raise ApiError("上传内容过大", 413)
            return self.rfile.read(length)

        def read_json(self) -> dict:
            raw = self.read_body(limit=8 * 1024 * 1024)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ApiError(f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ApiError("请求体必须是 JSON 对象")
            return data

        # -------------------------------------------------- 路由

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _dispatch(self, method: str):
            try:
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                if path.startswith("/api/"):
                    with app.lock:
                        self._handle_api(method, path, query)
                else:
                    self._serve_static(path)
            except AuthError as exc:
                self.send_error_json(exc.message, exc.status)
            except ApiError as exc:
                self.send_error_json(exc.message, exc.status)
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001 - 顶层兜底
                traceback.print_exc()
                self.send_error_json(f"服务器内部错误: {exc}", 500)

        # -------------------------------------------------- 账号

        def _auth_token(self) -> str:
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name == SESSION_COOKIE:
                    return value
            return ""

        def _session_cookie(self, token: str, expires: float) -> str:
            ttl = max(0, int(expires - time.time()))
            secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
            return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                    f"Max-Age={ttl}{secure}")

        def _client_ip(self) -> str:
            """反代后面取真实客户端 IP（只信任我们自己配的反代）。"""
            if os.environ.get("PACE_TRUST_PROXY") == "1":
                forwarded = self.headers.get("X-Forwarded-For") or ""
                if forwarded:
                    return forwarded.split(",")[0].strip() or "127.0.0.1"
                real = self.headers.get("X-Real-IP")
                if real:
                    return real.strip()
            return self.client_address[0]

        def _clear_cookie(self) -> str:
            return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

        def _handle_auth(self, method: str, path: str, q: dict):
            auth = app.auth
            if auth is None:
                raise ApiError("这个实例没有开启账号系统", 404)
            ip = self._client_ip()

            if path == "/api/auth/status":
                user = auth.session_user(self._auth_token())
                if user:
                    app.set_user(user["id"])
                count = auth.count_users()
                return self.send_json({
                    "needs_setup": count == 0,
                    "registration_open": auth.registration_open(),
                    "users_count": count,
                    "user": user,
                    "legacy_pending": count == 0 and os.path.exists(app.db_path),
                })

            if path == "/api/auth/register":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                body = self.read_json()
                user = auth.register(body.get("username") or "", body.get("password") or "",
                                     body.get("email") or "")
                migrated = app.migrate_legacy(user["id"]) if user["role"] == "owner" else False
                token, expires = auth.create_session(user["id"], remember=True,
                                                     ua=self.headers.get("User-Agent", ""), ip=ip)
                app.set_user(user["id"])
                return self.send_json({"user": user, "expires_at": expires, "migrated": migrated},
                                      extra_headers={"Set-Cookie": self._session_cookie(token, expires)})

            if path == "/api/auth/login":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                body = self.read_json()
                user = auth.authenticate(body.get("username") or "", body.get("password") or "", ip=ip)
                token, expires = auth.create_session(user["id"], remember=bool(body.get("remember")),
                                                     ua=self.headers.get("User-Agent", ""), ip=ip)
                app.set_user(user["id"])
                return self.send_json({"user": user, "expires_at": expires},
                                      extra_headers={"Set-Cookie": self._session_cookie(token, expires)})

            if path == "/api/auth/logout":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                auth.revoke(self._auth_token())
                return self.send_json({"ok": True},
                                      extra_headers={"Set-Cookie": self._clear_cookie()})

            if path == "/api/auth/me":
                user = auth.session_user(self._auth_token())
                if not user:
                    raise ApiError("未登录", 401)
                app.set_user(user["id"])
                return self.send_json({"user": user, "registration_open": auth.registration_open()})

            if path == "/api/auth/password":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                user = auth.session_user(self._auth_token())
                if not user:
                    raise ApiError("未登录", 401)
                body = self.read_json()
                auth.change_password(user["id"], body.get("old_password") or "",
                                     body.get("new_password") or "")
                token, expires = auth.create_session(user["id"], remember=True,
                                                     ua=self.headers.get("User-Agent", ""), ip=ip)
                return self.send_json({"ok": True},
                                      extra_headers={"Set-Cookie": self._session_cookie(token, expires)})

            if path == "/api/auth/delete":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                user = auth.session_user(self._auth_token())
                if not user:
                    raise ApiError("未登录", 401)
                body = self.read_json()
                if not body.get("password"):
                    raise ApiError("请输入密码确认删除", 400)
                auth.authenticate(user["username"], body.get("password"), ip=ip)
                auth.delete_user(user["id"])
                app.close_user(user["id"])
                shutil.rmtree(os.path.join(app.users_dir, str(user["id"])), ignore_errors=True)
                return self.send_json({"ok": True},
                                      extra_headers={"Set-Cookie": self._clear_cookie()})

            if path == "/api/auth/users":
                user = auth.session_user(self._auth_token())
                if not user:
                    raise ApiError("未登录", 401)
                if user["role"] != "owner":
                    raise ApiError("只有站长能看成员列表", 403)
                return self.send_json({"users": auth.list_users(),
                                       "registration_open": auth.registration_open()})

            if path == "/api/auth/registration":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                user = auth.session_user(self._auth_token())
                if not user or user["role"] != "owner":
                    raise ApiError("只有站长能改注册开关", 403)
                body = self.read_json()
                auth.set_registration_open(bool(body.get("open")))
                return self.send_json({"registration_open": auth.registration_open()})

            if path == "/api/auth/forgot":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                if auth.reset_fail_count(ip) >= 5:
                    raise ApiError("请求太频繁，请 15 分钟后再试", 429)
                auth.record_reset_request(ip)
                body = self.read_json()
                email = (body.get("email") or "").strip()
                user = auth.user_by_email(email) if email else None
                mail_sent = False
                dev_code = None
                if user:
                    code = auth.create_reset(user["id"], email)
                    try:
                        mailer.send_reset_code(auth, email, code)
                        mail_sent = True
                    except mailer.MailError as exc:
                        print(f"[邮件] 发送失败 {email}: {exc}")
                        print(f"[邮件] {email} 的重置码：{code}（10 分钟内有效）")
                    if os.environ.get("PACE_DEV_CODE") == "1":
                        dev_code = code
                # 邮箱存不存在都返回同样形状，避免被拿来枚举账号
                return self.send_json({"ok": True, "mail_sent": mail_sent, "dev_code": dev_code})

            if path == "/api/auth/reset":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                body = self.read_json()
                user = auth.consume_reset(body.get("email") or "", body.get("code") or "",
                                          body.get("new_password") or "")
                return self.send_json({"ok": True, "user": user})

            if path == "/api/auth/smtp":
                user = auth.session_user(self._auth_token())
                if not user or user["role"] != "owner":
                    raise ApiError("只有站长能改发信配置", 403)
                if method == "GET":
                    return self.send_json(mailer.public_config(auth))
                body = self.read_json()
                mailer.save_config(auth, body)
                if body.get("test_to"):
                    mailer.send_test(auth, str(body["test_to"]))
                return self.send_json(mailer.public_config(auth))

            raise ApiError("未知接口 " + path, 404)

        # -------------------------------------------------- API

        def _handle_api(self, method: str, path: str, q: dict):
            if path == "/api/health":
                # 启动探针要能匿名访问（run.py 靠它等服务器起来）；
                # 活动数只在已登录（或关掉账号系统）时返回，避免匿名泄露数据量
                payload = {"ok": True, "version": APP_VERSION, "time": time.time()}
                if app.auth_required:
                    user = app.auth.session_user(self._auth_token())
                    if user:
                        app.set_user(user["id"])
                        payload["activities"] = app.store.count()
                else:
                    payload["activities"] = app.store.count()
                return self.send_json(payload)

            if path.startswith("/api/auth/"):
                return self._handle_auth(method, path, q)

            if app.auth_required:
                user = app.auth.session_user(self._auth_token())
                if not user:
                    raise ApiError("未登录", 401)
                app.set_user(user["id"])

            store = app.store

            if path == "/api/meta":
                return self.send_json({
                    "version": APP_VERSION,
                    "settings": app.settings(),
                    "sports": [{"key": k, "label": v, "family": sport_family(k)}
                               for k, v in sorted(parsers.SPORT_LABELS.items())],
                    "families": [
                        {"key": "run", "label": "跑步"},
                        {"key": "ride", "label": "骑行"},
                        {"key": "swim", "label": "游泳"},
                        {"key": "walk", "label": "徒步/步行"},
                        {"key": "other", "label": "其他"},
                    ],
                    "server_time": time.time(),
                })

            if path == "/api/dashboard":
                days = _int(q.get("days"), 28)
                tz_offset = _float(q.get("tz"), 8.0)
                sport = q.get("sport")
                key = f"dash:{days}:{tz_offset}:{sport}"
                data = app.cached(key, 20, lambda: analytics.dashboard(
                    store, days=max(7, min(365, days)), tz_offset=tz_offset, sport=sport))
                return self.send_json(data)

            if path == "/api/activities":
                return self.send_json(store.list_activities(
                    limit=max(1, min(500, _int(q.get("limit"), 50))),
                    offset=max(0, _int(q.get("offset"), 0)),
                    sport=q.get("sport"),
                    date_from=_float(q.get("from")),
                    date_to=_float(q.get("to")),
                    q=q.get("q"),
                    sort=q.get("sort") or "date",
                ))

            if path == "/api/activities/compare":
                ids = [int(x) for x in (q.get("ids") or "").split(",") if x.strip().isdigit()]
                if len(ids) < 1:
                    raise ApiError("请提供至少一个活动 ID", 400)
                return self.send_json(analytics.compare_activities(
                    store, ids, tz_offset=_float(q.get("tz"), 8.0)))

            if path == "/api/activities/recompute":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                count = _recompute_all(app)
                app.invalidate()
                return self.send_json({"ok": True, "recomputed": count})

            if path == "/api/import":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                return self._handle_import()

            if path.startswith("/api/activities/"):
                rest = path[len("/api/activities/"):].strip("/")
                if rest.endswith("/segments/candidates"):
                    activity_id = _int(rest.split("/")[0], 0)
                    act = store.get_activity(activity_id, full=True)
                    if not act:
                        raise ApiError("活动不存在", 404)
                    return self.send_json({"candidates": _segment_candidates(act)})
                activity_id = _int(rest, 0)
                if activity_id <= 0:
                    raise ApiError("非法的活动 ID", 400)
                if method == "GET":
                    act = store.get_activity(activity_id, full=True)
                    if not act:
                        raise ApiError("活动不存在", 404)
                    act["segments"] = _activity_segments(store, activity_id)
                    from .coach import activity as activity_report
                    act["analysis"] = activity_report.build(act, app.settings())
                    return self.send_json(act)
                if method == "PATCH":
                    payload = self.read_json()
                    if not store.update_activity(activity_id, payload):
                        raise ApiError("没有可更新的字段", 400)
                    app.invalidate()
                    return self.send_json({"ok": True})
                if method == "DELETE":
                    doomed = store.get_activity(activity_id, full=False)
                    if not doomed or not store.delete_activity(activity_id):
                        raise ApiError("活动不存在", 404)
                    # 同步状态里也要忘掉它，否则同一个文件删了再也传不回来
                    try:
                        app.sync.state.forget(filename=doomed.get("filename"),
                                              external_id=doomed.get("external_id"))
                    except Exception:
                        pass
                    app.invalidate()
                    return self.send_json({"ok": True})
                raise ApiError("不支持的请求方法", 405)

            if path == "/api/prs":
                family = q.get("family")
                records = analytics.personal_records(store, limit=0)
                if family and family != "all":
                    records = [r for r in records if r["family"] == family]
                return self.send_json({"records": records})

            if path == "/api/segments":
                if method == "GET":
                    return self.send_json({"segments": store.list_segments()})
                if method == "POST":
                    payload = self.read_json()
                    try:
                        seg = self._create_segment(payload)
                    except ValueError as exc:
                        raise ApiError(str(exc)) from exc
                    app.invalidate()
                    return self.send_json(seg, status=201)
                raise ApiError("不支持的请求方法", 405)

            if path == "/api/segments/suggest":
                return self.send_json({"suggestions": analytics.auto_suggest_segments(store)})

            if path == "/api/segments/rebuild":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                matched = analytics.rebuild_all_efforts(store)
                app.invalidate()
                return self.send_json({"ok": True, "matched": matched})

            if path.startswith("/api/segments/"):
                rest = path[len("/api/segments/"):].strip("/")
                parts = rest.split("/")
                segment_id = _int(parts[0], 0) if parts and parts[0] else 0
                if segment_id <= 0:
                    raise ApiError("非法的分段 ID", 400)
                if len(parts) > 1 and parts[1] == "rebuild":
                    seg = store.get_segment(segment_id)
                    if not seg:
                        raise ApiError("分段不存在", 404)
                    store.conn.execute("DELETE FROM segment_efforts WHERE segment_id=?", (segment_id,))
                    store.conn.commit()
                    matched = _rebuild_segment(app, seg)
                    store.refresh_effort_bests(segment_id)
                    app.invalidate()
                    return self.send_json({"ok": True, "matched": matched})
                if method == "GET":
                    seg = store.get_segment(segment_id)
                    if not seg:
                        raise ApiError("分段不存在", 404)
                    seg["polyline_points"] = geo.decode_polyline(seg["polyline"]) if seg.get("polyline") else []
                    return self.send_json(seg)
                if method == "DELETE":
                    if not store.delete_segment(segment_id):
                        raise ApiError("分段不存在", 404)
                    app.invalidate()
                    return self.send_json({"ok": True})
                raise ApiError("不支持的请求方法", 405)

            # -------------------------------------------------- 路书

            if path == "/api/routebooks":
                if method == "GET":
                    return self.send_json({"routebooks": store.list_routebooks()})
                if method == "POST":
                    payload = self.read_json()
                    points = _clean_points(payload.get("points"))
                    if len(points) < 2:
                        raise ApiError("至少需要两个坐标点", 400)
                    name = str(payload.get("name") or "").strip() or "未命名路书"
                    book_id = store.create_routebook({
                        "name": name[:120],
                        "note": str(payload.get("note") or ""),
                        "sport": payload.get("sport") or "cycling",
                        "points": points,
                        "waypoints": _clean_waypoints(payload.get("waypoints")),
                        "distance_m": _route_distance(points),
                        "climb_m": _float(payload.get("climb_m"), 0.0) or 0.0,
                        "source": payload.get("source") or "manual",
                        "source_activity_id": payload.get("source_activity_id"),
                    })
                    app.invalidate()
                    book = store.get_routebook(book_id) or {}
                    book["waypoints"] = _waypoint_distances(
                        book.get("waypoints") or [], book["points"])
                    book["routebook"] = routebook.build(points, {},
                                                        total_m=book.get("distance_m"))
                    return self.send_json(book, status=201)
                raise ApiError("不支持的请求方法", 405)

            if path == "/api/routebooks/from-activity":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                payload = self.read_json()
                activity_id = _int(payload.get("activity_id"), 0)
                act = store.get_activity(activity_id, full=True)
                if not act:
                    raise ApiError("活动不存在", 404)
                points = _clean_points(act.get("route"))
                if len(points) < 2:
                    raise ApiError("这条活动没有轨迹，做不了路书", 400)
                name = str(payload.get("name") or act.get("title") or "活动路书").strip()
                book_id = store.create_routebook({
                    "name": name[:120],
                    "note": f"来自活动 #{activity_id}",
                    "sport": "cycling",
                    "points": points,
                    "distance_m": float(act.get("distance_m") or _route_distance(points)),
                    "climb_m": float(act.get("elevation_gain_m") or 0),
                    "source": "activity",
                    "source_activity_id": activity_id,
                })
                app.invalidate()
                book = store.get_routebook(book_id) or {}
                book["routebook"] = routebook.build(
                    points, act.get("streams") or {},
                    total_m=book.get("distance_m"))
                return self.send_json(book, status=201)

            if path.startswith("/api/routebooks/"):
                rest = path[len("/api/routebooks/"):].strip("/")
                parts = [x for x in rest.split("/") if x]
                book_id = _int(parts[0], 0) if parts else 0
                if book_id <= 0:
                    raise ApiError("非法的路书 ID", 400)
                book = store.get_routebook(book_id)
                if not book:
                    raise ApiError("路书不存在", 404)
                if len(parts) > 1 and parts[1] == "gpx":
                    xml = routebook.to_gpx(book["name"], book["points"],
                                           sport=book.get("sport") or "cycling",
                                           waypoints=book.get("waypoints") or [])
                    # HTTP 头只能是 latin-1，中文名走 RFC 5987 的 filename*
                    from urllib.parse import quote as _quote
                    safe = "".join(c for c in book["name"]
                                   if c.isascii() and c not in '\\/:*?"<>|').strip()[:60]
                    disp = (f"attachment; filename=\"{safe or 'routebook'}.gpx\"; "
                            f"filename*=UTF-8''{_quote(book['name'][:60] + '.gpx')}")
                    return self._send(
                        200, xml.encode("utf-8"), "application/gpx+xml; charset=utf-8",
                        {"Content-Disposition": disp})
                if method == "GET":
                    # 来自活动的路书带上原活动的海拔/速度流，才有爬坡和公里节点；
                    # 点被手动改过就对不上了，这时只用形状算转向。
                    streams = _routebook_streams(store, book)
                    book["waypoints"] = _waypoint_distances(
                        book.get("waypoints") or [], book["points"])
                    book["routebook"] = routebook.build(
                        book["points"], streams, total_m=book.get("distance_m"))
                    return self.send_json(book)
                if method in ("PUT", "PATCH"):
                    payload = self.read_json()
                    data: dict = {}
                    if "name" in payload:
                        data["name"] = str(payload.get("name") or "").strip()[:120]
                    if "note" in payload:
                        data["note"] = str(payload.get("note") or "")
                    if "points" in payload:
                        points = _clean_points(payload.get("points"))
                        if len(points) < 2:
                            raise ApiError("至少需要两个坐标点", 400)
                        data["points"] = points
                        data["distance_m"] = _route_distance(points)
                    if "waypoints" in payload:
                        data["waypoints"] = _clean_waypoints(payload.get("waypoints"))
                    if not store.update_routebook(book_id, data):
                        raise ApiError("没有可更新的字段", 400)
                    app.invalidate()
                    book = store.get_routebook(book_id) or {}
                    book["waypoints"] = _waypoint_distances(
                        book.get("waypoints") or [], book["points"])
                    book["routebook"] = routebook.build(
                        book["points"], _routebook_streams(store, book),
                        total_m=book.get("distance_m"))
                    return self.send_json(book)
                if method == "DELETE":
                    if not store.delete_routebook(book_id):
                        raise ApiError("路书不存在", 404)
                    app.invalidate()
                    return self.send_json({"ok": True})
                raise ApiError("不支持的请求方法", 405)

            if path == "/api/settings":
                if method == "GET":
                    settings = dict(app.settings())
                    key = str(settings.get("llm_api_key") or "")
                    # Key 只回掩码，浏览器不需要拿到明文
                    settings["llm_api_key_set"] = bool(key)
                    settings["llm_api_key_mask"] = (f"{key[:4]}…{key[-4:]}"
                                                    if len(key) > 8 else ("已设置" if key else ""))
                    settings["llm_api_key"] = ""
                    return self.send_json({"settings": settings})
                if method in ("POST", "PATCH"):
                    payload = self.read_json()
                    current = app.store.get_settings()
                    updates = {k: v for k, v in payload.items() if k in current}
                    if not updates:
                        raise ApiError("没有可更新的设置项", 400)
                    app.invalidate()
                    saved = dict(app.store.set_settings(updates))
                    key = str(saved.get("llm_api_key") or "")
                    saved["llm_api_key_set"] = bool(key)
                    saved["llm_api_key_mask"] = (f"{key[:4]}…{key[-4:]}"
                                                 if len(key) > 8 else ("已设置" if key else ""))
                    saved["llm_api_key"] = ""
                    return self.send_json({"settings": saved})
                raise ApiError("不支持的请求方法", 405)

            # ------------------------------------------------ 数据源 / 设备同步

            if path == "/api/sources":
                if method == "GET":
                    return self.send_json(app.sync.status())
                if method in ("POST", "PUT"):
                    payload = self.read_json()
                    configs = payload.get("sources")
                    if not isinstance(configs, list):
                        raise ApiError("需要 sources 数组", 400)
                    saved = app.sync.set_configs(configs)
                    return self.send_json({"sources": saved, "status": app.sync.status()})
                raise ApiError("不支持的请求方法", 405)

            if path == "/api/sources/test":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                payload = self.read_json()
                source_id = str(payload.get("id") or "")
                if not source_id:
                    raise ApiError("缺少数据源 id", 400)
                return self.send_json(app.sync.test_source(source_id))

            if path == "/api/sources/sync":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                payload = self.read_json()
                ids = payload.get("sources") or None
                if ids is not None and not isinstance(ids, list):
                    raise ApiError("sources 必须是数组", 400)
                result = app.sync.sync(
                    source_ids=ids,
                    limit=int(payload.get("limit") or 40),
                    dry_run=bool(payload.get("dry_run")),
                )
                if result["imported"]:
                    app.invalidate()
                return self.send_json(result)

            # ------------------------------------------------ 教练

            if path == "/api/coach/intake":
                from .coach import program as coach_program
                saved = app.settings().get("coach_intake") or {}
                if method == "GET":
                    report = app.cached(f"coach:{_float(q.get('tz'), 8.0)}:56", 30,
                                        lambda: coach_evaluate.evaluate(
                                            store, tz_offset=_float(q.get("tz"), 8.0), days=56))
                    return self.send_json({
                        "intake": saved or coach_program.suggest_intake(report),
                        "saved": bool(saved),
                        "suggested": coach_program.suggest_intake(report),
                        "goals": [{"key": k, **v} for k, v in coach_program.GOALS.items()],
                        "levels": [{"key": k, "label": v}
                                   for k, v in coach_program.LEVEL_LABELS.items()],
                        "constraints": [{"key": k, "label": v}
                                        for k, v in coach_program.CONSTRAINTS.items()],
                    })
                if method == "POST":
                    payload = self.read_json()
                    app.store.set_settings({"coach_intake": payload})
                    app.invalidate()
                    return self.send_json({"ok": True, "intake": payload})
                raise ApiError("不支持的请求方法", 405)

            if path == "/api/coach/program":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                from .coach import program as coach_program
                tz_offset = _float(q.get("tz"), 8.0)
                payload = self.read_json()
                intake = payload.get("intake") or app.settings().get("coach_intake") or {}
                if not intake.get("goal"):
                    raise ApiError("先选择目标再生成计划", 400)
                report = app.cached(f"coach:{tz_offset}:56", 30,
                                    lambda: coach_evaluate.evaluate(
                                        store, tz_offset=tz_offset, days=56))
                program = coach_program.build_program(
                    store, report, intake, tz_offset=tz_offset)
                app.store.set_settings({"coach_intake": program["intake"]})
                app.invalidate()
                return self.send_json({"program": program, "grade": report["grade"]})

            if path == "/api/coach/ask":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                from .coach import llm as coach_llm
                tz_offset = _float(q.get("tz"), 8.0)
                payload = self.read_json()
                question = str(payload.get("question") or "").strip()
                if not question:
                    raise ApiError("问题不能为空", 400)
                report = app.cached(f"coach:{tz_offset}:56", 30,
                                    lambda: coach_evaluate.evaluate(
                                        store, tz_offset=tz_offset, days=56))
                intake = payload.get("intake") or app.settings().get("coach_intake") or {}
                program = payload.get("program")
                result = coach_llm.ask(store, report, question, intake, program,
                                       tz_offset=tz_offset)
                return self.send_json(result)

            if path == "/api/coach/llm/test":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                from .coach import llm as coach_llm
                payload = self.read_json()
                current = app.settings()
                probe = {
                    "llm_api_key": payload.get("llm_api_key") or current.get("llm_api_key"),
                    "llm_base_url": payload.get("llm_base_url") or current.get("llm_base_url"),
                    "llm_model": payload.get("llm_model") or current.get("llm_model"),
                    "llm_enabled": True,
                }
                return self.send_json(coach_llm.test_connection(probe))

            if path == "/api/coach/llm":
                if method != "GET":
                    raise ApiError("需要 GET", 405)
                from .coach import llm as coach_llm
                cfg = coach_llm.provider(app.settings())
                return self.send_json({
                    "enabled": cfg["enabled"],
                    "model": cfg["model"],
                    "base_url": cfg["base_url"],
                    "key_hint": coach_llm.mask_key(cfg["api_key"]),
                })

            if path == "/api/coach":
                tz_offset = _float(q.get("tz"), 8.0)
                days = _int(q.get("days"), 56)
                report = app.cached(f"coach:{tz_offset}:{days}", 30,
                                    lambda: coach_evaluate.evaluate(
                                        store, tz_offset=tz_offset, days=days))
                return self.send_json(report)

            if path == "/api/coach/plan":
                tz_offset = _float(q.get("tz"), 8.0)
                settings = app.settings()
                goal = q.get("goal") or settings.get("coach_goal") or "health"
                days = _int(q.get("days"), settings.get("coach_days_per_week") or 4)
                target = q.get("target") or ""
                target_ts = parsers.parse_time(target) if target else None
                report = app.cached(f"coach:{tz_offset}:56", 30,
                                    lambda: coach_evaluate.evaluate(
                                        store, tz_offset=tz_offset, days=56))
                plan = coach_plan.build_plan(
                    store, report, tz_offset=tz_offset, goal=goal,
                    days_per_week=days, target_date=target_ts)
                return self.send_json({"plan": plan, "report_grade": report["grade"]})

            # ------------------------------------------------ BLE

            if path == "/api/ble/protocols":
                from . import ble
                from .devices.ble_registry import DeviceRegistry
                data_dir = os.path.dirname(os.path.abspath(app.db_path))
                return self.send_json({
                    "web_bluetooth": True,
                    "note": "浏览器只能主动连接设备，不能模拟设备。厂商私有协议需先抓包。",
                    "standard_services": ble.STANDARD_SERVICES,
                    "vendor_hints": ble.VENDOR_HINTS,
                    "captures": ble.CaptureStore(os.path.join(data_dir, "ble-captures.json")).summary(),
                    "devices": DeviceRegistry(os.path.join(data_dir, "ble-devices.json")).all(),
                })

            if path == "/api/ble/devices":
                from .devices.ble_registry import DeviceRegistry
                data_dir = os.path.dirname(os.path.abspath(app.db_path))
                registry = DeviceRegistry(os.path.join(data_dir, "ble-devices.json"))
                if method == "GET":
                    return self.send_json({"devices": registry.all()})
                if method == "POST":
                    payload = self.read_json()
                    result = registry.save_observed(
                        str(payload.get("name") or "unknown"),
                        payload.get("services") or [],
                        payload.get("note"),
                    )
                    return self.send_json(result, status=201)
                raise ApiError("不支持的请求方法", 405)

            if path == "/api/ble/lookup":
                from .devices.ble_registry import DeviceRegistry
                data_dir = os.path.dirname(os.path.abspath(app.db_path))
                registry = DeviceRegistry(os.path.join(data_dir, "ble-devices.json"))
                return self.send_json({
                    "device": registry.lookup(q.get("name")),
                })

            if path == "/api/ble/decode":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                from . import ble
                payload = self.read_json()
                char = str(payload.get("uuid") or "")
                hexdata = str(payload.get("hex") or "")
                try:
                    raw = bytes.fromhex(hexdata.replace(" ", ""))
                except ValueError as exc:
                    raise ApiError(f"hex 格式错误: {exc}") from exc
                decoded = ble.decode_notification(char, raw)
                return self.send_json({"uuid": ble.normalize_uuid(char),
                                       "hex": raw.hex(),
                                       "decoded": decoded,
                                       "known": decoded is not None})

            if path == "/api/ble/captures":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                from . import ble
                payload = self.read_json()
                data_path = os.path.join(os.path.dirname(os.path.abspath(app.db_path)),
                                         "ble-captures.json")
                try:
                    result = ble.CaptureStore(data_path).save(payload)
                except ValueError as exc:
                    raise ApiError(str(exc)) from exc
                return self.send_json(result, status=201)

            # ------------------------------------------------ Strava OAuth

            if path == "/api/strava/status":
                cfg = _strava_config(app)
                src = app.sync.source_by_id("strava")
                status = src.status() if src else {}
                return self.send_json({
                    "configured": bool(cfg.get("client_id") and cfg.get("client_secret")),
                    "connected": bool((cfg.get("tokens") or {}).get("access_token")),
                    "athlete": (cfg.get("tokens") or {}).get("athlete_name"),
                    "days": cfg.get("days") or 30,
                    "client_id": cfg.get("client_id") or "",
                    "has_secret": bool(cfg.get("client_secret")),
                    "hint": status.get("hint"),
                })

            if path == "/api/strava/config":
                if method != "POST":
                    raise ApiError("需要 POST", 405)
                payload = self.read_json()
                cfg = _strava_config(app)
                if "client_id" in payload:
                    cfg["client_id"] = str(payload.get("client_id") or "").strip()
                if "client_secret" in payload:
                    secret = str(payload.get("client_secret") or "").strip()
                    if secret:
                        cfg["client_secret"] = secret
                if "days" in payload:
                    try:
                        cfg["days"] = max(1, min(365, int(payload["days"])))
                    except (TypeError, ValueError):
                        pass
                if payload.get("disconnect"):
                    cfg["tokens"] = {}
                _save_strava_config(app, cfg)
                return self.send_json({"ok": True, "config": {
                    "client_id": cfg.get("client_id"), "days": cfg.get("days"),
                    "has_secret": bool(cfg.get("client_secret")),
                    "connected": bool((cfg.get("tokens") or {}).get("access_token")),
                }})

            if path == "/api/strava/auth":
                cfg = _strava_config(app)
                if not (cfg.get("client_id") and cfg.get("client_secret")):
                    raise ApiError("请先填写 Client ID 与 Client Secret", 400)
                from . import strava_api
                redirect = _strava_redirect(self)
                url = strava_api.auth_url(cfg["client_id"], redirect)
                # 浏览器直接打开授权页
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if path == "/api/strava/callback":
                from . import strava_api
                err = q.get("error")
                if err:
                    return self._strava_page(
                        f"授权被拒绝：{U_escape(err)}", ok=False)
                code = q.get("code")
                if not code:
                    return self._strava_page("回调缺少 code 参数", ok=False)
                cfg = _strava_config(app)
                try:
                    tokens = strava_api.exchange_code(
                        cfg.get("client_id") or "",
                        cfg.get("client_secret") or "",
                        code,
                    )
                except Exception as exc:  # noqa: BLE001
                    return self._strava_page(f"换取令牌失败：{U_escape(str(exc))}", ok=False)
                cfg["tokens"] = tokens
                _save_strava_config(app, cfg)
                name = tokens.get("athlete_name") or "你的 Strava 账号"
                return self._strava_page(f"连接成功：{U_escape(name)}。可以回到 PACE 了。", ok=True)

            raise ApiError(f"未知接口 {path}", 404)

        def _strava_page(self, message: str, ok: bool = True):
            color = "#34d399" if ok else "#f87171"
            body = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Strava 授权</title><style>
body{{background:#0b0d10;color:#eef1f5;font-family:system-ui,sans-serif;
display:grid;place-items:center;height:100vh;margin:0}}
.card{{background:#12151a;border:1px solid #262c35;border-radius:14px;padding:28px 32px;max-width:460px}}
h1{{font-size:17px;margin:0 0 10px;color:{color}}}
p{{color:#a8b1bd;font-size:13px;line-height:1.7;margin:0 0 16px}}
a{{display:inline-block;background:#fc4c02;color:#fff;text-decoration:none;
padding:9px 16px;border-radius:9px;font-size:13px;font-weight:600}}
</style></head><body><div class="card">
<h1>{"成功" if ok else "失败"}</h1><p>{message}</p>
<a href="/#/devices">返回 PACE 数据源页</a></div></body></html>"""
            self._send(200 if ok else 400, body.encode("utf-8"),
                       "text/html; charset=utf-8")

        # -------------------------------------------------- 导入

        def _handle_import(self):
            raw = self.read_body()
            if not raw:
                raise ApiError("没有收到文件内容")
            content_type = (self.headers.get("Content-Type") or "").lower()

            # 单文件：直接把请求体当文件
            filename = unquote(self.headers.get("X-Filename") or "upload")
            payloads: list[tuple[str, bytes]] = []

            if "multipart/form-data" in content_type:
                payloads = _parse_multipart(raw, content_type)
            else:
                payloads = [(filename, raw)]

            if not payloads:
                raise ApiError("没有解析到文件")

            results = []
            for name, data in payloads:
                results.append(self._import_one(name, data))
            app.invalidate()
            return self.send_json({"ok": True, "results": results}, status=201)

        def _import_one(self, filename: str, data: bytes) -> dict:
            return app.importer.import_file(filename, data)

        def _create_segment(self, payload: dict) -> dict:
            activity_id = _int(payload.get("activity_id"), 0)
            if activity_id <= 0:
                raise ValueError("缺少 activity_id")
            activity = app.store.get_activity(activity_id, full=True)
            if not activity:
                raise ValueError("活动不存在")

            start_index = payload.get("start_index")
            end_index = payload.get("end_index")
            if start_index is None or end_index is None:
                # 允许用比例指定
                frac_s = float(payload.get("start_frac", 0.0))
                frac_e = float(payload.get("end_frac", 1.0))
                route = activity.get("route") or []
                if not route:
                    raise ValueError("该活动没有 GPS 轨迹")
                start_index = int(len(route) * frac_s)
                end_index = int(len(route) * frac_e)

            data = analytics.build_segment_from_activity(
                activity, int(start_index), int(end_index), payload.get("name"))
            segment_id = app.store.create_segment(data)
            matched = _rebuild_segment(app, app.store.get_segment(segment_id))
            app.store.refresh_effort_bests(segment_id)
            return {"id": segment_id, "name": data["name"],
                    "distance_m": data["distance_m"], "matched": matched}

        # -------------------------------------------------- 静态文件

        def _serve_static(self, path: str):
            if path in ("", "/"):
                path = "/index.html"
            # 目录穿越防护
            rel = path.lstrip("/")
            target = os.path.normpath(os.path.join(app.web_dir, rel))
            if not target.startswith(os.path.normpath(app.web_dir)):
                raise ApiError("非法路径", 403)
            if not os.path.isfile(target):
                raise ApiError("页面不存在", 404)
            ext = os.path.splitext(target)[1].lower()
            mime = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".webmanifest": "application/manifest+json",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".ico": "image/x-icon",
                ".woff2": "font/woff2",
                ".map": "application/json",
            }.get(ext) or mimetypes.guess_type(target)[0] or "application/octet-stream"
            info = os.stat(target)
            etag = '"%x-%x"' % (int(info.st_mtime), info.st_size)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            with open(target, "rb") as fh:
                body = fh.read()
            self._send(200, body, mime, extra_headers={"ETag": etag})

    return Handler


def _strava_config(app) -> dict:
    for cfg in app.sync.configs():
        if cfg.get("id") == "strava":
            return dict(cfg.get("config") or {})
    return {"client_id": "", "client_secret": "", "tokens": {}, "days": 30}


def _save_strava_config(app, config: dict) -> None:
    configs = app.sync.configs()
    for cfg in configs:
        if cfg.get("id") == "strava":
            cfg["config"] = config
            cfg["enabled"] = True
            break
    app.sync.set_configs(configs)


def _strava_redirect(handler) -> str:
    """Stava 要求 redirect_uri 的域名与应用的 Authorization Callback Domain 一致。

    应用里通常填 `localhost`，所以这里把 127.0.0.1 归一成 localhost，端口保持。
    """
    host = handler.headers.get("Host") or "localhost:8765"
    if host.startswith("127.0.0.1"):
        host = "localhost" + host[len("127.0.0.1"):]
    return f"http://{host}/api/strava/callback"


def U_escape(text: str) -> str:
    import html as _html
    return _html.escape(str(text), quote=True)


def _int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_multipart(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    """极简 multipart/form-data 解析，只提取文件部分。"""
    marker = "boundary="
    idx = content_type.find(marker)
    if idx < 0:
        raise ApiError("multipart 请求缺少 boundary")
    boundary = content_type[idx + len(marker):].strip().strip('"')
    sep = ("--" + boundary).encode()
    out = []
    for part in body.split(sep):
        if not part or part in (b"--\r\n", b"--"):
            continue
        part = part.lstrip(b"\r\n")
        head_end = part.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        head = part[:head_end].decode("utf-8", "replace")
        content = part[head_end + 4:]
        # 去掉结尾的 \r\n（分隔符前的换行）
        if content.endswith(b"\r\n"):
            content = content[:-2]
        elif content.endswith(b"\n"):
            content = content[:-1]
        name = "upload"
        for line in head.split("\r\n"):
            if line.lower().startswith("content-disposition"):
                for piece in line.split(";"):
                    piece = piece.strip()
                    if piece.lower().startswith("filename="):
                        name = piece[len("filename="):].strip('"') or "upload"
        if content:
            out.append((name, content))
    return out



def _clean_points(raw) -> list:
    """路书坐标清洗：只留 [lat, lon, ele?]，去掉越界与连续重复点。

    海拔是可选的第三位（GPX 的 <ele>）：有就留着画剖面、算爬坡，没有也不影响。
    """
    out: list = []
    for item in (raw or []):
        ele = None
        try:
            if isinstance(item, dict):
                lat, lon = float(item.get("lat")), float(item.get("lon"))
                ele = item.get("ele", item.get("alt"))
            else:
                lat, lon = float(item[0]), float(item[1])
                if len(item) > 2:
                    ele = item[2]
        except (TypeError, ValueError, IndexError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        if out and abs(out[-1][0] - lat) < 1e-7 and abs(out[-1][1] - lon) < 1e-7:
            continue
        point = [round(lat, 6), round(lon, 6)]
        if ele is not None:
            try:
                ele_f = float(ele)
            except (TypeError, ValueError):
                ele_f = None
            if ele_f is not None and -1000.0 <= ele_f <= 9000.0:
                point.append(round(ele_f, 1))
        out.append(point)
    return out


def _route_distance(points: list) -> float:
    if len(points) < 2:
        return 0.0
    from . import geo
    return float(geo.cumulative_distance([(p[0], p[1]) for p in points])[-1])


def _routebook_streams(store: Store, book: dict) -> dict:
    """来自活动的路书带上原活动的海拔/速度流，才有爬坡和公里节点。

    点数对不上（被手动改过）就不用——时间轴和坐标已经不一一对应了。
    改名之类的「不碰点」的更新也要走这里，否则会把爬坡和公里节点一起弄丢。
    """
    src_id = book.get("source_activity_id")
    if not src_id or book.get("source") != "activity":
        return {}
    src = store.get_activity(int(src_id), full=True)
    if not src:
        return {}
    if len(src.get("route") or []) != int(book.get("point_count") or 0):
        return {}
    return src.get("streams") or {}


def _clean_waypoints(raw) -> list:
    """途经点清洗：坐标、名称、标签、描述、是否显示。

    行者那套「途经点」是路书的主角，结构固定，前端直接拿渲染。
    """
    out: list = []
    for item in (raw or [])[:300]:
        if not isinstance(item, dict):
            continue
        try:
            lat, lon = float(item.get("lat")), float(item.get("lon"))
        except (TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        out.append({
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "name": str(item.get("name") or "")[:60],
            "tag": str(item.get("tag") or "")[:24],
            "note": str(item.get("note") or "")[:300],
            "show": bool(item.get("show", True)),
        })
    return out


def _waypoint_distances(waypoints: list, points: list) -> list:
    """每个途经点在轨迹上的里程（取最近轨迹点），详情页排序和显示用。"""
    if not waypoints:
        return []
    from . import geo
    if len(points) < 2:
        return [dict(w, d=0.0) for w in waypoints]
    cum = geo.cumulative_distance([(p[0], p[1]) for p in points])
    out = []
    for w in waypoints:
        best, best_i = None, 0
        for i, p in enumerate(points):
            dd = (p[0] - w["lat"]) ** 2 + (p[1] - w["lon"]) ** 2
            if best is None or dd < best:
                best, best_i = dd, i
        out.append(dict(w, d=round(cum[best_i], 0)))
    return out


def _activity_segments(store: Store, activity_id: int) -> list[dict]:
    rows = store.conn.execute(
        "SELECT e.*, s.name, s.distance_m AS seg_distance_m, s.avg_grade_pct "
        "FROM segment_efforts e JOIN segments s ON s.id=e.segment_id "
        "WHERE e.activity_id=? ORDER BY e.elapsed_s", (activity_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _segment_candidates(activity: dict) -> list[dict]:
    """给前端提供"一键创建分段"的候选窗口（1km / 5km / 半马等）。"""
    route = activity.get("route") or []
    streams = activity.get("streams") or {}
    d_series = streams.get("d") or []
    if not route or not d_series:
        return []
    total = activity.get("distance_m") or 0
    family = sport_family(activity["sport"])
    targets = analysis.BEST_EFFORT_DISTANCES.get(family, [])
    out = []
    for dist, label in targets:
        if dist > total * 0.95:
            continue
        # 找最接近该距离的起点（默认从 0 开始）
        out.append({
            "label": label,
            "distance_m": dist,
            "start_frac": 0.0,
            "end_frac": min(1.0, dist / total) if total else 1.0,
        })
    return out


# ---------------------------------------------------------------- 重建

def _rebuild_segment(app: App, segment: dict) -> int:
    store = app.store
    rows = store.conn.execute(
        "SELECT id, sport FROM activities WHERE has_gps=1"
    ).fetchall()
    matched = 0
    for row in rows:
        if sport_family(row["sport"]) != segment.get("sport_family"):
            continue
        activity = store.get_activity(row["id"], full=True)
        if not activity:
            continue
        effort = analytics.match_segment(segment, activity)
        if effort:
            store.add_effort(segment["id"], activity["id"], effort)
            matched += 1
    return matched


def _recompute_all(app: App) -> int:
    """用当前心率设置重算所有活动（例如改了最大心率）。"""
    store = app.store
    ids = [r["id"] for r in store.conn.execute("SELECT id FROM activities").fetchall()]
    count = 0
    for aid in ids:
        act = store.get_activity(aid, full=True)
        if not act:
            continue
        streams = act.get("streams") or {}
        if not streams.get("t"):
            continue
        # 用 1Hz 流反向重建点序列
        points = []
        for i, t in enumerate(streams["t"]):
            points.append({
                "t": act["start_time"] + t,
                "lat": None, "lon": None,
                "alt": _at(streams.get("alt"), i),
                "hr": _at(streams.get("hr"), i),
                "cad": _at(streams.get("cad"), i),
                "power": _at(streams.get("pwr"), i),
                "speed": (_at(streams.get("speed"), i) or 0) / 3.6,
                "dist": _at(streams.get("d"), i),
            })
        try:
            built = analysis.analyze(points, act["sport"], app.analysis_options())
        except ValueError:
            continue
        summary = built["summary"]
        summary["distance_m"] = act["distance_m"] or summary["distance_m"]
        summary["start_time"] = act["start_time"]
        summary["streams"] = built["streams"]
        store.update_metrics(aid, summary)
        count += 1
    return count


def _at(series, index):
    if not series:
        return None
    return series[index] if index < len(series) else None


# ---------------------------------------------------------------- 启动

def find_free_port(preferred: int, host: str = "127.0.0.1") -> int:
    for port in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


class QuietServer(ThreadingHTTPServer):
    """客户端断连不当作服务端错误：只记日志，不打印堆栈。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def build_server(db_path: str, web_dir: str, port: int = 0, *,
                 auth_required: bool = True, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, App]:
    app = App(db_path, web_dir, auth_required=auth_required)
    handler = make_handler(app)
    httpd = QuietServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd, app


def main(argv: list[str] | None = None) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="PACE - 本地运动数据分析")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=os.path.join(root, "data", "pace.db"))
    parser.add_argument("--web", default=os.path.join(root, "web"))
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--no-auth", action="store_true",
                        help="关闭账号系统（只给自己本机用，等同于旧版行为）")
    parser.add_argument("--host", default=os.environ.get("PACE_HOST", "127.0.0.1"),
                        help="监听地址；容器/公网部署用 0.0.0.0")
    args = parser.parse_args(argv)

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    port = find_free_port(args.port, args.host)
    httpd, app = build_server(args.db, args.web, port, auth_required=not args.no_auth,
                              host=args.host)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown}:{httpd.server_address[1]}/"
    print(f"PACE 已启动: {url}")
    if app.auth_required:
        print(f"账号库: {os.path.join(app.data_dir, 'auth.db')}")
        ready = app.auth.count_users()
        if not ready:
            print("还没有账号：首次打开页面先创建站长账号（现有数据会迁入这个账号）")
        else:
            print(f"成员: {ready} 人")
    else:
        print(f"数据库: {args.db}")
        print(f"已有活动: {app.store.count()} 条")
    if args.open:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.shutdown()
        with app._ctx_lock:
            contexts = list(app._contexts.values())
        for ctx in contexts:
            ctx.close()
        if app.auth is not None:
            app.auth.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
