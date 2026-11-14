"""Strava API 客户端（OAuth + 活动 + 逐点流）。

合规说明
--------
这里只服务**单个用户看他自己的数据**：应用在本机跑，不上传、不分发、不对外展示。
Strava 的限制是"不得把数据转授第三方"，个人本地分析不属于转授。

流程
----
1. 用户在 strava.com/settings/api 建一个应用，拿到 Client ID / Secret，
   回调域名填 `localhost`
2. PACE 打开授权页 → Strava 回调 `/api/strava/callback?code=...`
3. 用 code 换 access_token + refresh_token，存进本机数据库
4. 之后用 Bearer token 拉活动列表与 streams，过期自动刷新

不依赖第三方库，纯 urllib。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://www.strava.com/oauth/token"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
API_BASE = "https://www.strava.com/api/v3"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

STREAM_KEYS = "time,distance,latlng,altitude,heartrate,cadence,watts,velocity_smooth"
SCOPES = "read,activity:read_all"


class StravaError(RuntimeError):
    pass


def auth_url(client_id: str, redirect_uri: str, state: str = "pace") -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _post_form(url: str, payload: dict, timeout: int = 25) -> dict:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    """用授权码换 token。返回 {access_token, refresh_token, expires_at, athlete}。"""
    raw = _post_form(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    })
    return _normalize_token(raw)


def refresh_token(client_id: str, client_secret: str, refresh: str) -> dict:
    raw = _post_form(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    })
    return _normalize_token(raw)


def _normalize_token(raw: dict) -> dict:
    if not isinstance(raw, dict) or not raw.get("access_token"):
        raise StravaError(f"授权失败：{raw}")
    athlete = raw.get("athlete") or {}
    return {
        "access_token": raw["access_token"],
        "refresh_token": raw.get("refresh_token") or "",
        "expires_at": int(raw.get("expires_at") or (time.time() + 3600)),
        "athlete_id": athlete.get("id"),
        "athlete_name": " ".join(
            x for x in [athlete.get("firstname"), athlete.get("lastname")] if x
        ) or None,
    }


def api_get(access_token: str, path: str, params: dict | None = None,
            timeout: int = 25, base: str | None = None):
    # 注意：base 不能写成默认参数 = API_BASE，那样会在定义时绑定，测试无法替换
    base = base or API_BASE
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200].decode("utf-8", "replace")
        if exc.code == 401:
            raise StravaError(
                "Strava 授权失效。若你刚取消订阅，应用可能已被停用——"
                "到 strava.com/settings/api 看应用状态，或改用「数据导出」"
            ) from exc
        if exc.code == 403:
            raise StravaError(
                "Strava 拒绝访问（403）。常见原因：订阅到期导致应用停用、"
                "权限范围不足（需要 activity:read_all），或当日请求超限"
            ) from exc
        if exc.code == 429:
            raise StravaError("Strava 限流（429），请 15 分钟后再试") from exc
        raise StravaError(f"Strava 返回 {exc.code}：{body}") from exc


def list_activities(access_token: str, *, after: float, before: float,
                    per_page: int = 50, base: str | None = None) -> list[dict]:
    out: list[dict] = []
    for page in range(1, 6):
        rows = api_get(access_token, "/athlete/activities", {
            "after": int(after), "before": int(before),
            "page": page, "per_page": per_page,
        }, base=base)
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if len(rows) < per_page:
            break
    return out


def fetch_streams(access_token: str, activity_id: int, base: str | None = None) -> dict:
    """返回 {type: [data...]}；Strava 的 key_by_type=true 会返回 {type:{data:[...]}}。"""
    raw = api_get(access_token, f"/activities/{int(activity_id)}/streams",
                  {"keys": STREAM_KEYS, "key_by_type": "true"}, base=base)
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(value.get("data"), list):
            out[key] = value["data"]
        elif isinstance(value, list):
            out[key] = value
    return out


def build_payload(activity: dict, streams: dict) -> dict:
    """合成与 `parsers.parse_strava_json` 兼容的结构。

    有 latlng 时走 GPS 路径；室内活动没有 latlng 也能导入（靠 distance 流）。
    """
    return {
        "activity": {
            "id": activity.get("id"),
            "name": activity.get("name"),
            "sport_type": activity.get("sport_type") or activity.get("type"),
            "type": activity.get("type"),
            "start_date": activity.get("start_date"),
            "distance": activity.get("distance"),
            "moving_time": activity.get("moving_time"),
            "elapsed_time": activity.get("elapsed_time"),
            "total_elevation_gain": activity.get("total_elevation_gain"),
            "average_heartrate": activity.get("average_heartrate"),
            "max_heartrate": activity.get("max_heartrate"),
            "average_watts": activity.get("average_watts"),
            "description": activity.get("description"),
            "device_name": activity.get("device_name"),
        },
        "streams": {k: {"data": v} for k, v in (streams or {}).items() if v},
    }
