"""发信：找回密码验证码 / 测试信。

只用标准库 smtplib，不引第三方。配置存在账号库的 meta 表里（键名 smtp），
由站长在「设置 → 邮件」里填写：
    {"host": "smtp.qq.com", "port": 465, "user": "x@qq.com",
     "password": "授权码", "from": "x@qq.com", "secure": "ssl"}
"""
from __future__ import annotations

import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

FIELDS = ("host", "port", "user", "password", "from", "secure")


class MailError(Exception):
    """发信失败（配置缺失 / 认证失败 / 网络不通）。"""


def load_config(auth) -> dict:
    raw = auth._meta_get("smtp", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_config(auth, data: dict) -> dict:
    clean = {}
    for key in FIELDS:
        value = data.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, ""):
            clean[key] = value
    old = load_config(auth)
    if not clean.get("password") and old.get("password"):
        clean["password"] = old["password"]
    if clean.get("port"):
        try:
            clean["port"] = int(clean["port"])
        except (TypeError, ValueError) as exc:
            raise MailError("端口必须是数字") from exc
    auth._meta_set("smtp", json.dumps(clean, ensure_ascii=False))
    auth.db.commit()
    return clean


def public_config(auth) -> dict:
    cfg = load_config(auth)
    return {
        "configured": bool(cfg.get("host") and cfg.get("from")),
        "host": cfg.get("host") or "",
        "port": cfg.get("port") or "",
        "user": cfg.get("user") or "",
        "from": cfg.get("from") or "",
        "secure": cfg.get("secure") or "",
    }


def is_configured(auth) -> bool:
    cfg = load_config(auth)
    return bool(cfg.get("host") and cfg.get("from"))


def send(auth, to: str, subject: str, body: str) -> None:
    cfg = load_config(auth)
    if not is_configured(auth):
        raise MailError("还没配置发信邮箱")
    host = str(cfg["host"])
    secure = str(cfg.get("secure") or "").lower()
    port = int(cfg.get("port") or (465 if secure == "ssl" else 587))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("PACE", str(cfg["from"])))
    msg["To"] = to
    msg.set_content(body)

    context = ssl.create_default_context()
    try:
        if port == 465 or secure == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
                if cfg.get("user"):
                    server.login(str(cfg["user"]), str(cfg.get("password") or ""))
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                if cfg.get("user"):
                    server.login(str(cfg["user"]), str(cfg.get("password") or ""))
                server.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(f"发信失败：{exc}") from exc


def send_reset_code(auth, to: str, code: str) -> None:
    body = (
        "你在 PACE 上申请重置密码。\n\n"
        f"验证码：{code}\n"
        "10 分钟内有效，用过一次就作废。\n\n"
        "如果不是你本人操作，忽略这封邮件即可，密码不会变。\n"
    )
    send(auth, to, "PACE 密码重置验证码", body)


def send_test(auth, to: str) -> None:
    send(auth, to, "PACE 测试邮件", "能收到这封说明发信配置是正确的。\n")
