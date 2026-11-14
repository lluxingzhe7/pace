"""账号体系测试：口令、会话、限流、注册开关、HTTP 鉴权与多用户隔离。

用法: python tools/test_auth.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pace import auth
from pace.server import build_server
from pace.store import open_store
from pace.summary_build import build_from_summary

PASSED = 0
FAILED: list[str] = []
TMP = os.path.join(ROOT, ".auth-test")
NOW = 1_794_000_000.0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def fresh_dir(name: str) -> str:
    path = os.path.join(TMP, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- 口令

def test_password():
    print("\n[1] 口令哈希与强度")
    stored = auth._hash_password("abc12345")
    check("哈希格式是 scrypt$", stored.startswith("scrypt$"), stored[:12])
    check("不是明文", "abc12345" not in stored)
    check("正确口令校验通过", auth._verify_password("abc12345", stored))
    check("错误口令校验失败", not auth._verify_password("abc12346", stored))
    check("空哈希不崩", not auth._verify_password("x", ""))
    check("两次加盐不同", auth._hash_password("abc12345") != auth._hash_password("abc12345"))
    for bad, why in (("short1", "太短"), ("12345678", "全数字"), ("abcdefgh", "全字母")):
        try:
            auth.check_password_strength(bad)
            check(f"拒绝弱密码（{why}）", False, bad)
        except auth.AuthError:
            check(f"拒绝弱密码（{why}）", True)
    try:
        auth.check_password_strength("abc12345")
        check("接受合格密码", True)
    except auth.AuthError as exc:
        check("接受合格密码", False, exc.message)


def test_register_rules():
    print("\n[2] 注册规则")
    d = fresh_dir("rules")
    store = auth.AuthStore(os.path.join(d, "auth.db"))
    first = store.register("laoban", "abc12345")
    check("第一个账号是站长", first["role"] == "owner", first["role"])
    second = store.register("pengyou", "abc12345")
    check("后续账号是普通用户", second["role"] == "user", second["role"])
    try:
        store.register("pengyou", "abc12345")
        check("用户名重复被拒", False)
    except auth.AuthError as exc:
        check("用户名重复被拒", exc.status == 409, str(exc.status))
    try:
        store.register("a", "abc12345")
        check("用户名太短被拒", False)
    except auth.AuthError:
        check("用户名太短被拒", True)
    try:
        store.register("xiao ming", "abc12345")
        check("用户名带空格被拒", False)
    except auth.AuthError:
        check("用户名带空格被拒", True)
    check("大小写同名算重复", store.user_by_name("LAOBAN") is not None)
    store.set_registration_open(False)
    try:
        store.register("disan", "abc12345")
        check("关闭注册后无法注册", False)
    except auth.AuthError as exc:
        check("关闭注册后无法注册", exc.status == 403, str(exc.status))
    store.close()


def test_sessions():
    print("\n[3] 会话")
    d = fresh_dir("sessions")
    path = os.path.join(d, "auth.db")
    store = auth.AuthStore(path)
    user = store.register("sess", "abc12345")
    token, expires = store.create_session(user["id"])
    check("会话有效期是未来", expires > time.time())
    check("token 能换回用户", (store.session_user(token) or {}).get("id") == user["id"])
    raw = open(path, "rb").read()
    check("库里不存明文 token", token.encode() not in raw)
    store.revoke(token)
    check("撤销后失效", store.session_user(token) is None)

    token2, _ = store.create_session(user["id"])
    store.db.execute("UPDATE sessions SET expires_at=? WHERE user_id=?", (time.time() - 10, user["id"]))
    store.db.commit()
    check("过期会话失效", store.session_user(token2) is None)

    store.change_password(user["id"], "abc12345", "xyz98765")
    check("改密后旧会话全部失效", store.session_user(token2) is None)
    try:
        store.authenticate("sess", "abc12345")
        check("改密后旧密码失效", False)
    except auth.AuthError:
        check("改密后旧密码失效", True)
    check("新密码可登录", store.authenticate("sess", "xyz98765")["id"] == user["id"])
    store.close()


def test_rate_limit():
    print("\n[4] 登录限流")
    d = fresh_dir("limit")
    store = auth.AuthStore(os.path.join(d, "auth.db"))
    user = store.register("lock", "abc12345")
    statuses = []
    for _ in range(5):
        try:
            store.authenticate("lock", "wrong1234", ip="1.1.1.1")
        except auth.AuthError as exc:
            statuses.append(exc.status)
    check("前 5 次报密码错误", statuses and all(s == 401 for s in statuses), str(statuses))
    try:
        store.authenticate("lock", "abc12345", ip="1.1.1.1")
        check("锁定期内正确密码也拒绝", False)
    except auth.AuthError as exc:
        check("锁定期内正确密码也拒绝", exc.status == 429, str(exc.status))
    check("换个 IP 能正常登录", store.authenticate("lock", "abc12345", ip="2.2.2.2")["id"] == user["id"])
    # 同一个用户名被分布式撞库时，20 次后全局锁定
    for _ in range(20):
        try:
            store.authenticate("lock", "wrong1234", ip=f"9.9.9.{_ % 200}")
        except auth.AuthError:
            pass
    try:
        store.authenticate("lock", "abc12345", ip="3.3.3.3")
        check("用户名被撞 20 次后全局锁定", False)
    except auth.AuthError as exc:
        check("用户名被撞 20 次后全局锁定", exc.status == 429, str(exc.status))
    store.close()


# ---------------------------------------------------------------- HTTP

class Client:
    def __init__(self, base):
        self.base = base
        self.cookie = ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, payload=None, use_cookie=True):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if use_cookie and self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                self._grab_cookie(resp)
                body = resp.read()
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return exc.code, json.loads(body) if body else {}
            except json.JSONDecodeError:
                return exc.code, {}

    def _grab_cookie(self, resp):
        for header in resp.headers.get_all("Set-Cookie") or []:
            if header.startswith("pace_session="):
                self.cookie = header.split(";", 1)[0]

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, payload=None, **kw):
        return self.request("POST", path, payload if payload is not None else {}, **kw)


def seed_legacy(db_path: str, count: int) -> None:
    store = open_store(db_path)
    for i in range(count):
        built = build_from_summary({
            "sport": "running", "start_time": NOW - i * 86400,
            "distance_m": 5000 + i * 500, "moving_seconds": 1800,
            "elevation_m": 40, "avg_hr": 150, "max_hr": 170,
        })
        store.upsert_activity({
            "source": "test", "external_id": f"legacy-{i}", "filename": "t.csv",
            "sport": "running", "title": f"legacy-{i}",
            "summary": built["summary"], "streams": {}, "route": [],
        })
    store.close()


def test_http():
    print("\n[5] HTTP 鉴权与多用户隔离")
    d = fresh_dir("http")
    db = os.path.join(d, "pace.db")
    seed_legacy(db, 2)
    httpd, app = build_server(db, os.path.join(ROOT, "web"), 0, auth_required=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        anon = Client(base)
        status, body = anon.get("/api/activities")
        check("匿名访问接口被拒", status == 401, str(status))
        status, health = anon.get("/api/health")
        check("健康检查可匿名", status == 200 and health.get("ok"), json.dumps(health)[:80])
        check("匿名健康检查不带活动数", "activities" not in health, json.dumps(health)[:80])

        status, st = anon.get("/api/auth/status")
        check("首次启动提示建站", st.get("needs_setup") is True, json.dumps(st)[:80])
        check("提示有旧数据待迁移", st.get("legacy_pending") is True, json.dumps(st)[:80])

        owner = Client(base)
        status, body = owner.post("/api/auth/register",
                                  {"username": "laoban", "password": "abc12345", "email": "a@b.com"})
        check("注册站长成功", status == 200 and body.get("user", {}).get("role") == "owner", str(status))
        check("注册时把旧数据迁进来", body.get("migrated") is True, str(body.get("migrated")))
        check("下发了会话 cookie", owner.cookie.startswith("pace_session="), owner.cookie[:20])

        status, acts = owner.get("/api/activities?limit=5")
        check("站长看到迁移来的 2 条活动", status == 200 and acts.get("total") == 2, str(acts.get("total")))
        first_id = acts["items"][0]["id"]

        friend = Client(base)
        status, body = friend.post("/api/auth/register", {"username": "pengyou", "password": "abc12345"})
        check("第二个人注册成功", status == 200, str(status))
        check("第二个人没有迁移数据", body.get("migrated") is False, str(body.get("migrated")))
        status, acts2 = friend.get("/api/activities?limit=5")
        check("朋友看不到站长的活动", acts2.get("total") == 0, str(acts2.get("total")))
        status, _ = friend.get(f"/api/activities/{first_id}")
        check("朋友直接访问站长活动 404", status == 404, str(status))
        status, books = friend.get("/api/routebooks")
        check("朋友的路书是空的", books.get("routebooks") == [], str(books)[:60])

        status, _ = owner.post("/api/auth/logout")
        check("退出登录", status == 200, str(status))
        status, _ = owner.get("/api/auth/me")
        check("退出后 me 401", status == 401, str(status))
        status, _ = owner.post("/api/auth/login", {"username": "laoban", "password": "wrong1234"})
        check("错误密码 401", status == 401, str(status))
        status, body = owner.post("/api/auth/login", {"username": "laoban", "password": "abc12345"})
        check("正确密码登录", status == 200 and body["user"]["username"] == "laoban", str(status))

        status, body = owner.get("/api/auth/users")
        check("站长能看成员列表", status == 200 and len(body.get("users", [])) == 2, str(status))
        status, _ = friend.get("/api/auth/users")
        check("普通用户看不了成员列表", status == 403, str(status))

        status, body = owner.post("/api/auth/delete", {"password": "abc12345"})
        check("站长不能被直接删", status == 403, str(status))

        status, _ = friend.get("/api/auth/delete")
        check("删除账号要 POST", status in (405, 404), str(status))
        status, body = friend.post("/api/auth/delete", {"password": "abc12345"})
        check("普通用户可以删除自己", status == 200, str(status))
        check("删号后数据目录清掉", not os.path.exists(os.path.join(d, "users", "2")), "")
    finally:
        httpd.shutdown()
        with app._ctx_lock:
            for ctx in list(app._contexts.values()):
                ctx.close()
        app.auth.close()


def main() -> int:
    print("=" * 58)
    print("账号体系测试")
    print("=" * 58)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    test_password()
    test_register_rules()
    test_sessions()
    test_rate_limit()
    test_http()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 58)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
