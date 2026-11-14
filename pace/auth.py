"""账号、口令与会话。

设计要点（全部使用标准库，不引入第三方依赖）：

- 口令用 `scrypt`（N=2^14, r=8, p=1）加盐哈希，校验用 `hmac.compare_digest` 常数时间比较；
- 会话是**服务端** session：浏览器只拿到随机 token，库里存的是它的 sha256，
  就算库文件被拷走也不能直接拿来登录；
- 登录失败按「用户名 + IP」两个维度计数，15 分钟内 5 次就暂时锁住；
- 所有写操作走同一把可重入锁，配合 SQLite WAL，够几十人的并发量。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32

SESSION_TTL = 14 * 86400        # 默认 14 天
REMEMBER_TTL = 60 * 86400       # 勾「记住我」60 天
MAX_FAILS = 5                   # 单个 IP 15 分钟内的失败上限
MAX_USER_FAILS = 20             # 单个用户名 15 分钟内的失败上限（防分布式撞库）
FAIL_WINDOW = 15 * 60

# 2–24 个字符：中英文、数字、下划线、连字符
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{2,24}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email         TEXT NOT NULL DEFAULT '',
    password      TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    REAL NOT NULL,
    last_login_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    ua          TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS login_fails (
    scope     TEXT NOT NULL,
    happened  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fails_scope ON login_fails(scope, happened DESC);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_RESET = """
CREATE TABLE IF NOT EXISTS password_resets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    email       TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    salt        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    tries       INTEGER NOT NULL DEFAULT 0,
    used_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_reset_email ON password_resets(email, created_at DESC);
"""

RESET_TTL = 10 * 60
RESET_MAX_TRIES = 5


class AuthError(Exception):
    """带 HTTP 状态码的业务错误，服务层直接转成 JSON。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DK_LEN)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, salt.hex(), digest.hex())


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_password_strength(password: str) -> None:
    if not isinstance(password, str) or len(password) < 8:
        raise AuthError("密码至少 8 位", 400)
    if len(password) > 128:
        raise AuthError("密码太长（最多 128 位）", 400)
    if password.strip() != password:
        raise AuthError("密码首尾不能有空格", 400)
    patterns = (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    classes = sum(1 for p in patterns if re.search(p, password))
    if classes < 2:
        raise AuthError("密码要同时包含字母和数字（或符号）", 400)


def _public_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "status": row["status"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


class AuthStore:
    """账号/会话库，和运动数据分开放（`data/auth.db`）。"""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.executescript(SCHEMA_RESET)
        self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _meta_get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _meta_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def count_users(self) -> int:
        with self.lock:
            return int(self.db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def registration_open(self) -> bool:
        with self.lock:
            return self._meta_get("registration_open", "1") == "1"

    def set_registration_open(self, open_: bool) -> None:
        with self.lock:
            self._meta_set("registration_open", "1" if open_ else "0")
            self.db.commit()

    def get_user(self, user_id: int):
        with self.lock:
            row = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return _public_user(row) if row else None

    def user_by_name(self, username: str):
        with self.lock:
            row = self.db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                                  ((username or "").strip(),)).fetchone()
            return _public_user(row) if row else None

    def list_users(self) -> list[dict]:
        with self.lock:
            return [_public_user(r) for r in
                    self.db.execute("SELECT * FROM users ORDER BY id").fetchall()]

    def register(self, username: str, password: str, email: str = "") -> dict:
        username = (username or "").strip()
        email = (email or "").strip()
        if not USERNAME_RE.match(username):
            raise AuthError("用户名 2–24 个字符，只能用中英文、数字、下划线、连字符", 400)
        if email and not EMAIL_RE.match(email):
            raise AuthError("邮箱格式不对", 400)
        check_password_strength(password)
        with self.lock:
            first = self.count_users() == 0
            if not first and not self.registration_open():
                raise AuthError("站点已关闭注册", 403)
            if self.db.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE",
                               (username,)).fetchone():
                raise AuthError("用户名已被占用", 409)
            now = time.time()
            cur = self.db.execute(
                "INSERT INTO users(username, email, password, role, status, created_at) "
                "VALUES(?,?,?,?, 'active', ?)",
                (username, email, _hash_password(password), "owner" if first else "user", now))
            self.db.commit()
            return _public_user(self.db.execute("SELECT * FROM users WHERE id=?",
                                                (cur.lastrowid,)).fetchone())

    def _fail_scope(self, username: str, ip: str) -> list[str]:
        return ["user:" + (username or "").strip().lower(), "ip:" + (ip or "-")]

    def _count_scope(self, scope: str, since: float) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM login_fails WHERE scope=? AND happened>=?",
            (scope, since)).fetchone()
        return int(row["n"])

    def fail_count(self, username: str, ip: str) -> int:
        """返回「该 IP」和「该用户名」里更大的那个失败次数。"""
        since = time.time() - FAIL_WINDOW
        with self.lock:
            return max(self._count_scope("ip:" + (ip or "-"), since),
                       self._count_scope("user:" + (username or "").strip().lower(), since))

    def locked(self, username: str, ip: str) -> bool:
        since = time.time() - FAIL_WINDOW
        with self.lock:
            ip_fails = self._count_scope("ip:" + (ip or "-"), since)
            user_fails = self._count_scope("user:" + (username or "").strip().lower(), since)
        return ip_fails >= MAX_FAILS or user_fails >= MAX_USER_FAILS

    def _record_fail(self, username: str, ip: str) -> None:
        now = time.time()
        with self.lock:
            for scope in self._fail_scope(username, ip):
                self.db.execute("INSERT INTO login_fails(scope, happened) VALUES(?,?)", (scope, now))
            self.db.execute("DELETE FROM login_fails WHERE happened < ?", (now - FAIL_WINDOW * 4,))
            self.db.commit()

    def _clear_fails(self, username: str, ip: str) -> None:
        with self.lock:
            for scope in self._fail_scope(username, ip):
                self.db.execute("DELETE FROM login_fails WHERE scope=?", (scope,))
            self.db.commit()

    def authenticate(self, username: str, password: str, ip: str = "") -> dict:
        username = (username or "").strip()
        if not username or not password:
            raise AuthError("请输入用户名和密码", 400)
        if self.locked(username, ip):
            raise AuthError("失败次数过多，请 %d 分钟后再试" % (FAIL_WINDOW // 60), 429)
        with self.lock:
            row = self.db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                                  (username,)).fetchone()
            if not row and "@" in username:
                row = self.db.execute("SELECT * FROM users WHERE lower(email)=?",
                                      (username.lower(),)).fetchone()
        if not row or not _verify_password(password, row["password"]):
            self._record_fail(username, ip)
            raise AuthError("用户名或密码不对", 401)
        if row["status"] != "active":
            raise AuthError("账号已被停用", 403)
        self._clear_fails(username, ip)
        with self.lock:
            self.db.execute("UPDATE users SET last_login_at=? WHERE id=?", (time.time(), row["id"]))
            self.db.commit()
        return _public_user(row)

    def create_session(self, user_id: int, *, remember: bool = False,
                       ua: str = "", ip: str = "") -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + (REMEMBER_TTL if remember else SESSION_TTL)
        with self.lock:
            self.db.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen, ua, ip) "
                "VALUES(?,?,?,?,?,?,?)",
                (_token_hash(token), user_id, now, expires, now, (ua or "")[:300], (ip or "")[:64]))
            self.db.commit()
        return token, expires

    def session_user(self, token: str):
        if not token:
            return None
        now = time.time()
        with self.lock:
            row = self.db.execute(
                "SELECT s.token_hash AS th, s.expires_at AS exp, s.last_seen AS seen, u.* "
                "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
                (_token_hash(token),)).fetchone()
            if not row:
                return None
            if row["exp"] < now or row["status"] != "active":
                self.db.execute("DELETE FROM sessions WHERE token_hash=?", (row["th"],))
                self.db.commit()
                return None
            if now - row["seen"] > 300:
                self.db.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?",
                                (now, row["th"]))
                self.db.commit()
            return _public_user(row)

    def revoke(self, token: str) -> None:
        if not token:
            return
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
            self.db.commit()

    def revoke_user_sessions(self, user_id: int) -> int:
        with self.lock:
            cur = self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.db.commit()
            return int(cur.rowcount or 0)

    def purge_expired(self) -> int:
        with self.lock:
            cur = self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            self.db.commit()
            return int(cur.rowcount or 0)

    def change_password(self, user_id: int, old: str, new: str) -> None:
        with self.lock:
            row = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise AuthError("账号不存在", 404)
            if not _verify_password(old or "", row["password"]):
                raise AuthError("原密码不对", 403)
            check_password_strength(new)
            self.db.execute("UPDATE users SET password=? WHERE id=?",
                            (_hash_password(new), user_id))
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.db.commit()

    # ---------------------------------------------------------------- 找回密码

    def user_by_email(self, email: str):
        email = (email or "").strip().lower()
        if not email:
            return None
        with self.lock:
            row = self.db.execute("SELECT * FROM users WHERE lower(email)=? LIMIT 1",
                                  (email,)).fetchone()
            return _public_user(row) if row else None

    def create_reset(self, user_id: int, email: str) -> str:
        """生成 6 位重置码（只存加盐哈希），明文只交给发信方。"""
        code = "%06d" % secrets.randbelow(1000000)
        salt = secrets.token_hex(8)
        now = time.time()
        with self.lock:
            self.db.execute(
                "UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL",
                (now, user_id))
            self.db.execute(
                "INSERT INTO password_resets(user_id, email, code_hash, salt, created_at, expires_at) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, (email or "").strip().lower(),
                 hashlib.sha256((salt + code).encode()).hexdigest(), salt, now, now + RESET_TTL))
            self.db.commit()
        return code

    def reset_fail_count(self, ip: str) -> int:
        since = time.time() - FAIL_WINDOW
        with self.lock:
            return self._count_scope("forgot:" + (ip or "-"), since)

    def record_reset_request(self, ip: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO login_fails(scope, happened) VALUES(?,?)",
                            ("forgot:" + (ip or "-"), time.time()))
            self.db.commit()

    def consume_reset(self, email: str, code: str, new_password: str) -> dict:
        """校验验证码并改密码；返回被重置的用户。"""
        email = (email or "").strip().lower()
        code = (code or "").strip()
        if not email or not code:
            raise AuthError("请填邮箱和验证码", 400)
        check_password_strength(new_password)
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM password_resets WHERE email=? AND used_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1", (email,)).fetchone()
            if not row:
                raise AuthError("验证码不对或已过期，请重新获取", 400)
            if row["expires_at"] < time.time():
                raise AuthError("验证码已过期，请重新获取", 400)
            if row["tries"] >= RESET_MAX_TRIES:
                raise AuthError("尝试次数过多，请重新获取验证码", 429)
            ok = hmac.compare_digest(
                row["code_hash"], hashlib.sha256((row["salt"] + code).encode()).hexdigest())
            if not ok:
                self.db.execute("UPDATE password_resets SET tries=tries+1 WHERE id=?", (row["id"],))
                self.db.commit()
                raise AuthError("验证码不对或已过期，请重新获取", 400)
            user_row = self.db.execute("SELECT * FROM users WHERE id=?",
                                       (row["user_id"],)).fetchone()
            if not user_row:
                raise AuthError("账号已不存在", 404)
            self.db.execute("UPDATE users SET password=? WHERE id=?",
                            (_hash_password(new_password), row["user_id"]))
            self.db.execute("UPDATE password_resets SET used_at=? WHERE id=?",
                            (time.time(), row["id"]))
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
            self.db.commit()
            return _public_user(user_row)

    def purge_expired_resets(self) -> int:
        with self.lock:
            cur = self.db.execute("DELETE FROM password_resets WHERE expires_at < ?",
                                  (time.time() - 86400,))
            self.db.commit()
            return int(cur.rowcount or 0)

    def delete_user(self, user_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                return None
            if row["role"] == "owner" and self.count_users() > 1:
                raise AuthError("站长的账号不能直接删，请先转让或删除其他成员", 403)
            user = _public_user(row)
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
            self.db.execute("DELETE FROM password_resets WHERE user_id=?", (user_id,))
            self.db.commit()
            return user
