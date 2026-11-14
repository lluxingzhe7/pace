"""设备/数据源接入层。

为什么不是蓝牙
--------------
iGPSPORT 码表与华为手表都使用厂商私有的 BLE 协议，且只在「设备 ↔ 厂商 App」
之间配对；浏览器没有权限、也没有这些 GATT 服务的定义。所以直连不可行。

可行且稳的三条路径
------------------
1. **文件夹同步**：华为运动健康导出包、iGPSPORT App 下载的 FIT、电脑拷出来的
   FIT 目录，都只是文件。监视目录 → 有新文件就导入。
2. **USB 大容量存储**：iGPSPORT 多数型号插上电脑会挂成 U 盘，活动在固定子目录。
3. **Intervals.icu 拉取**：iGPSPORT Ride App 能直连 Intervals.icu，
   用个人 API Key 就能把原始 FIT 拉回本地（官方支持的 API）。

每个 source 都实现同一个接口：
    status()   -> 连接/可用性状态
    scan()     -> 发现的新文件 [(path_or_id, name, bytes, meta)]
    mark_done(keys) -> 记录已处理，避免重复导入
状态持久化在 data/sync-state.json，内容只有路径与哈希，不含活动数据。
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import string
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .. import strava_api

SUPPORTED_EXT = {".fit", ".gpx", ".tcx", ".csv", ".json"}


def is_supported(name: str) -> bool:
    """是否是可导入的活动文件；兼容 Strava 导出里的 `xxx.fit.gz`。"""
    ext = os.path.splitext(name)[1].lower()
    if ext == ".gz":
        ext = os.path.splitext(os.path.splitext(name)[0])[1].lower()
    return ext in SUPPORTED_EXT
DEVICE_HINTS = {
    "HUAWEI": "华为运动健康导出",
    "iGPSPORT": "iGPSPORT 码表",
    "GARMIN": "Garmin",
    "COROS": "COROS",
    "WAHOO": "Wahoo",
}


# ---------------------------------------------------------------- 状态文件

class SyncState:
    """记录已经导入过的文件/活动，避免重复。"""

    def __init__(self, path: str):
        self.path = path
        self.data: dict = {"files": {}, "intervals": {}, "last_sync": None}
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def is_done(self, key: str, fingerprint: str) -> bool:
        entry = self.data["files"].get(key)
        return bool(entry and entry.get("fingerprint") == fingerprint)

    def mark(self, key: str, fingerprint: str, meta: dict | None = None) -> None:
        self.data["files"][key] = {
            "fingerprint": fingerprint,
            "at": time.time(),
            "meta": meta or {},
        }

    def intervals_done(self, activity_id: str) -> bool:
        return activity_id in self.data["intervals"]

    def intervals_mark(self, activity_id: str, meta: dict | None = None) -> None:
        self.data["intervals"][activity_id] = {"at": time.time(), "meta": meta or {}}

    def touch(self) -> None:
        self.data["last_sync"] = time.time()

    def forget(self, *, filename: str | None = None, external_id: str | None = None) -> int:
        """删掉活动后，把它在同步状态里的记录一并清掉。

        不清的话，同一个文件再也进不来：扫描时会被当成「已处理」跳过，
        用户看到的就是「删了再传却说已存在」。
        """
        removed = 0
        if filename:
            base = os.path.basename(filename)
            for key in list(self.data["files"].keys()):
                entry = self.data["files"][key]
                meta = entry.get("meta") or {}
                names = {os.path.basename(str(meta.get("source_path") or "")),
                         os.path.basename(str(meta.get("name") or "")),
                         os.path.basename(key)}
                if base in names:
                    del self.data["files"][key]
                    removed += 1
        if external_id and external_id in self.data["intervals"]:
            del self.data["intervals"][external_id]
            removed += 1
        if removed:
            self.save()
        return removed


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


# ---------------------------------------------------------------- 结果

@dataclass
class Discovered:
    key: str                 # 稳定标识（路径或外部 ID）
    name: str                # 展示名 / 文件名
    data: bytes
    meta: dict = field(default_factory=dict)


class BaseSource:
    kind = "base"
    label = "数据源"

    def __init__(self, config: dict, state: SyncState):
        self.config = config or {}
        self.state = state

    def status(self) -> dict:
        raise NotImplementedError

    def scan(self, limit: int = 40) -> list[Discovered]:
        return []

    def mark_done(self, item: Discovered) -> None:
        self.state.mark(item.key, item.meta.get("fingerprint") or fingerprint(item.data),
                        item.meta)


# ---------------------------------------------------------------- 文件夹

class FolderSource(BaseSource):
    """监视一个目录（含子目录），导入其中的活动文件。"""

    kind = "folder"
    label = "文件夹同步"

    def __init__(self, config, state):
        super().__init__(config, state)
        self.path = os.path.expanduser(self.config.get("path") or "")
        self.device_hint = self.config.get("device") or ""

    def status(self) -> dict:
        exists = bool(self.path) and os.path.isdir(self.path)
        count = 0
        if exists:
            for _, _, files in os.walk(self.path):
                count += sum(1 for f in files if is_supported(f))
        done = len(self.state.data["files"])
        if not self.path:
            hint = "还没设置目录。填路径后点保存。"
        elif not exists:
            hint = "目录不存在，请检查路径"
        else:
            hint = f"发现 {count} 个活动文件"
        if self.device_hint and exists:
            hint = f"{self.device_hint} · {hint}"
        return {
            "kind": self.kind,
            "label": self.label,
            "configured": bool(self.path),
            "available": exists,
            "path": self.path,
            "files_pending": count,
            "imported_total": done,
            "hint": hint,
        }

    def scan(self, limit: int = 40) -> list[Discovered]:
        if not self.path or not os.path.isdir(self.path):
            return []
        out: list[Discovered] = []
        for root, dirs, files in os.walk(self.path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in sorted(files):
                if not is_supported(name):
                    continue
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                    if stat.st_size == 0 or stat.st_size > 80 * 1024 * 1024:
                        continue
                    key = os.path.abspath(path)
                    fp = f"{stat.st_size}:{int(stat.st_mtime)}"
                    if self.state.is_done(key, fp):
                        continue
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                out.append(Discovered(
                    key=key, name=name, data=data,
                    meta={"fingerprint": fp, "source_path": path,
                          "device": self.device_hint},
                ))
                if len(out) >= limit:
                    return out
        return out


# ---------------------------------------------------------------- USB 盘

class UsbSource(BaseSource):
    """自动探测挂载的码表/手表（以 U 盘形式出现）。

    Windows 盘符 + 常见活动子目录；只读，不改动设备上的任何文件。
    """

    kind = "usb"
    label = "USB 设备"

    CANDIDATE_DIRS = [
        "Activities", "Activity", "ACTIVITY", "activities", "FIT", "fit",
        "Garmin/Activities", "Garmin/Activity", "iGPSPORT", "IGPSPORT",
        "Sports", "Sport", "Workout", "Workouts", "History",
        os.path.join("Internal Storage", "Activities"),
    ]

    def _drives(self) -> list[str]:
        if os.name == "nt":
            return [f"{letter}:\\" for letter in string.ascii_uppercase
                    if os.path.exists(f"{letter}:\\")]
        roots = ["/media", "/Volumes", "/run/media", "/mnt"]
        out = []
        for root in roots:
            if os.path.isdir(root):
                for name in os.listdir(root):
                    path = os.path.join(root, name)
                    if os.path.isdir(path):
                        out.append(path)
                        # /run/media/<user>/<disk>
                        for sub in os.listdir(path):
                            subpath = os.path.join(path, sub)
                            if os.path.isdir(subpath):
                                out.append(subpath)
        return out

    def _looks_like_device(self, path: str) -> bool:
        """判断一个挂载点是否像运动设备：看目录名或根目录下的标志性文件/文件夹。"""
        name = os.path.basename(os.path.normpath(path)).upper()
        for hint in DEVICE_HINTS:
            if hint.upper() in name:
                return True
        if name in {"GARMIN", "IGPSPORT", "ACTIVITIES", "FIT"}:
            return True
        STRONG = {"ACTIVITIES", "ACTIVITY", "GARMIN", "IGPSPORT", "COROS", "WAHOO",
                  "FIT", "SPORTS", "SPORT"}
        try:
            entries = os.listdir(path)
        except OSError:
            return False
        return any(entry.upper() in STRONG for entry in entries)

    def detect(self) -> list[dict]:
        found = []
        for drive in self._drives():
            try:
                if not self._looks_like_device(drive):
                    continue
                label = ""
                for name in os.listdir(drive):
                    for hint, human in DEVICE_HINTS.items():
                        if hint.upper() in name.upper():
                            label = human
                            break
                activity_dirs = []
                for candidate in self.CANDIDATE_DIRS:
                    full = os.path.join(drive, candidate)
                    if os.path.isdir(full):
                        activity_dirs.append(full)
                if not activity_dirs:
                    activity_dirs = [drive]
                found.append({"path": drive, "label": label or "外接设备",
                              "dirs": activity_dirs})
            except OSError:
                continue
        return found

    def status(self) -> dict:
        devices = self.detect()
        return {
            "kind": self.kind,
            "label": self.label,
            "configured": True,
            "available": bool(devices),
            "devices": devices,
            "hint": (f"发现 {len(devices)} 个设备" if devices
                     else "没检测到设备。用数据线连接码表，选择「文件传输/USB 存储」模式"),
        }

    def _active_folders(self) -> list[str]:
        configured = self.config.get("path")
        if configured and os.path.isdir(configured):
            return [configured]
        dirs = []
        for dev in self.detect():
            dirs.extend(dev["dirs"])
        return dirs

    def scan(self, limit: int = 40) -> list[Discovered]:
        folders = self._active_folders()
        out: list[Discovered] = []
        for folder in folders:
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in sorted(files):
                    if os.path.splitext(name)[1].lower() not in SUPPORTED_EXT:
                        continue
                    path = os.path.join(root, name)
                    try:
                        stat = os.stat(path)
                        key = os.path.abspath(path)
                        fp = f"{stat.st_size}:{int(stat.st_mtime)}"
                        if self.state.is_done(key, fp):
                            continue
                        with open(path, "rb") as fh:
                            data = fh.read()
                    except OSError:
                        continue
                    out.append(Discovered(key=key, name=name, data=data,
                                          meta={"fingerprint": fp, "source_path": path,
                                                "device": "usb"}))
                    if len(out) >= limit:
                        return out
        return out


# ---------------------------------------------------------------- Intervals.icu

class IntervalsSource(BaseSource):
    """从 Intervals.icu 拉取原始活动文件。

    认证：个人 API Key（settings 页底部），Basic Auth 用户名固定为 API_KEY。
    athlete_id 填 0 表示"API Key 所属运动员"，通常不需要改。

    注意：Intervals.icu 在 Cloudflare 后面，缺 User-Agent 会被 1010 拦掉，
    所以这里必须带浏览器 UA。
    """

    kind = "intervals"
    label = "Intervals.icu"
    API = "https://intervals.icu/api/v1"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

    def __init__(self, config, state):
        super().__init__(config, state)
        self.athlete = (self.config.get("athlete_id") or "0").strip() or "0"
        self.api_key = (self.config.get("api_key") or "").strip()
        self.days = int(self.config.get("days") or 30)
        self.include_types = self.config.get("types") or []

    def _auth(self) -> str:
        token = base64.b64encode(f"API_KEY:{self.api_key}".encode()).decode()
        return f"Basic {token}"

    def _get(self, path: str, accept: str = "application/json", timeout: int = 25):
        if not self.api_key:
            raise RuntimeError("缺少 api_key")
        req = urllib.request.Request(
            f"{self.API}{path}",
            headers={
                "Authorization": self._auth(),
                "Accept": accept,
                "User-Agent": self.UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def status(self) -> dict:
        configured = bool(self.api_key)
        return {
            "kind": self.kind,
            "label": self.label,
            "configured": configured,
            "available": configured,
            "athlete_id": self.athlete,
            "days": self.days,
            "imported_total": len(self.state.data["intervals"]),
            "hint": ("已配置 API Key，可测试连接" if configured
                     else "在 intervals.icu/settings 页面底部复制 API Key（Athlete ID 填 0 即可）"),
        }

    def test(self) -> dict:
        """用最近 7 天做一次轻量请求，确认 Key 可用。"""
        from datetime import datetime, timedelta, timezone
        newest = datetime.now(timezone.utc).date()
        oldest = newest - timedelta(days=7)
        try:
            raw = self._get(
                f"/athlete/{self.athlete}/activities?oldest={oldest}&newest={newest}"
            )
            rows = json.loads(raw)
            return {"ok": True, "activities": len(rows) if isinstance(rows, list) else 0,
                    "athlete_id": self.athlete}
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                msg = ("API Key 无效或已过期。请到 intervals.icu/settings 底部重新复制"
                       "（只复制 Key 本身，不要带 'API_KEY:' 前缀）")
            elif exc.code == 422:
                msg = "请求参数被拒绝，可能是 Key 格式不对"
            else:
                msg = f"HTTP {exc.code}"
            return {"ok": False, "error": msg}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def scan(self, limit: int = 20) -> list[Discovered]:
        self.last_errors = []
        if not self.api_key:
            return []
        from datetime import datetime, timedelta, timezone
        newest = datetime.now(timezone.utc).date()
        oldest = newest - timedelta(days=self.days)
        raw = self._get(
            f"/athlete/{self.athlete}/activities?oldest={oldest}&newest={newest}"
        )
        rows = json.loads(raw)
        if not isinstance(rows, list):
            return []
        out: list[Discovered] = []
        for item in rows:
            aid = str(item.get("id") or "")
            if not aid or self.state.intervals_done(aid):
                continue
            act_type = str(item.get("type") or "")
            if self.include_types and act_type not in self.include_types:
                continue
            try:
                blob = self._get(f"/activity/{aid}/file", accept="*/*")
                if blob[:2] == b"\x1f\x8b":
                    blob = gzip.decompress(blob)
            except urllib.error.HTTPError as exc:
                # Strava 来源的活动常见 403/404：不让单条失败中断整批，但要报出来
                self.last_errors.append({
                    "activity_id": aid,
                    "name": item.get("name") or "",
                    "sport": act_type,
                    "status": exc.code,
                    "reason": ("Strava 来源的活动不允许第三方下载原始文件"
                               if exc.code in (401, 403)
                               else f"HTTP {exc.code}"),
                })
                continue
            except Exception as exc:  # noqa: BLE001
                self.last_errors.append({"activity_id": aid,
                                         "name": item.get("name") or "",
                                         "sport": act_type,
                                         "reason": str(exc)})
                continue
            ext = "." + (item.get("file_type") or "fit").lower()
            name = f"{str(item.get('start_date_local') or '')[:10]}-{aid}{ext}"
            out.append(Discovered(
                key=f"intervals:{aid}", name=name, data=blob,
                meta={"intervals_id": aid, "sport": act_type,
                      "start": item.get("start_date_local")},
            ))
            if len(out) >= limit:
                break
        return out

    def mark_done(self, item: Discovered) -> None:
        self.state.intervals_mark(item.meta.get("intervals_id", item.key),
                                  {"name": item.name})


# ---------------------------------------------------------------- Strava

class StravaSource(BaseSource):
    """直接走 Strava 官方 API（OAuth），拿你**自己**的逐点数据。

    合规：应用在本机运行、只分析本人活动、不上传不分发。
    Strava 禁止的是把数据转授第三方，个人本地分析不属于此列。

    配置（存在本机 data/pace.db 的 sources 里）：
        client_id / client_secret / tokens{access_token, refresh_token, expires_at}
        days：回溯天数
    首次连接需要用户在 strava.com/settings/api 建一个应用，回调域名填 localhost。
    """

    kind = "strava"
    label = "Strava"

    def __init__(self, config, state, persist=None):
        super().__init__(config, state)
        self.client_id = str(self.config.get("client_id") or "").strip()
        self.client_secret = str(self.config.get("client_secret") or "").strip()
        self.tokens = dict(self.config.get("tokens") or {})
        self.days = int(self.config.get("days") or 30)
        self._persist = persist
        self.last_errors: list[dict] = []

    # -------------------------------------------------- 状态

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def connected(self) -> bool:
        return bool(self.tokens.get("access_token"))

    def status(self) -> dict:
        hint = "在 strava.com/settings/api 创建应用，回调域名填 localhost"
        if self.configured() and not self.connected():
            hint = "已填应用凭据，点「连接 Strava」完成授权"
        elif self.connected():
            hint = f"已连接：{self.tokens.get('athlete_name') or 'Strava 账号'} · 回溯 {self.days} 天"
        return {
            "kind": self.kind,
            "label": self.label,
            "configured": self.configured(),
            "available": self.connected(),
            "days": self.days,
            "athlete": self.tokens.get("athlete_name"),
            "imported_total": len(self.state.data["files"]),
            "hint": hint,
        }

    def test(self) -> dict:
        try:
            token = self.access_token()
            rows = strava_api.api_get(token, "/athlete")
            return {"ok": True, "athlete": " ".join(
                x for x in [rows.get("firstname"), rows.get("lastname")] if x) or None}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # -------------------------------------------------- token

    def access_token(self) -> str:
        if not self.connected():
            raise RuntimeError("还没有授权 Strava，请先点「连接 Strava」")
        expires = int(self.tokens.get("expires_at") or 0)
        if expires > time.time() + 120:
            return self.tokens["access_token"]
        self._refresh()
        return self.tokens["access_token"]

    def _refresh(self) -> None:
        if not (self.client_id and self.client_secret and self.tokens.get("refresh_token")):
            raise RuntimeError("刷新令牌失败：缺少 refresh_token")
        fresh = strava_api.refresh_token(self.client_id, self.client_secret,
                                         self.tokens["refresh_token"])
        self.tokens.update(fresh)
        self.config["tokens"] = self.tokens
        if self._persist:
            self._persist(self.config)

    # -------------------------------------------------- 扫描

    def scan(self, limit: int = 20) -> list[Discovered]:
        self.last_errors = []
        if not self.connected():
            return []
        token = self.access_token()
        newest = time.time()
        oldest = newest - self.days * 86400
        try:
            activities = strava_api.list_activities(token, after=oldest, before=newest,
                                                    per_page=50)
        except Exception as exc:  # noqa: BLE001
            self.last_errors.append({"activity_id": "-", "reason": str(exc)})
            return []

        out: list[Discovered] = []
        for act in activities:
            aid = str(act.get("id") or "")
            if not aid:
                continue
            key = f"strava:{aid}"
            if self.state.is_done(key, str(act.get("start_date") or "")):
                continue
            try:
                streams = strava_api.fetch_streams(token, int(aid))
            except Exception as exc:  # noqa: BLE001
                self.last_errors.append({
                    "activity_id": aid,
                    "name": act.get("name") or "",
                    "sport": act.get("sport_type") or act.get("type") or "",
                    "reason": f"取不到逐点数据：{exc}",
                })
                continue
            payload = strava_api.build_payload(act, streams)
            out.append(Discovered(
                key=key,
                name=f"strava-{aid}.json",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                meta={"fingerprint": str(act.get("start_date") or ""),
                      "strava_id": aid, "device": "strava"},
            ))
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------- 注册表

SOURCE_TYPES = {
    "folder": FolderSource,
    "usb": UsbSource,
    "intervals": IntervalsSource,
    "strava": StravaSource,
}

# 当前只保留已实测可用的两条通道；其余默认隐藏（代码保留，随时可恢复）
HIDDEN_BY_DEFAULT = {"huawei_export", "intervals", "strava", "outbase_export", "phone_export"}

DEFAULT_SOURCES = [
    {
        "id": "igpsport_usb",
        "type": "usb",
        "enabled": True,
        "visible": True,
        "label": "iGPSPORT 码表（数据线）",
        "config": {"path": ""},
    },
    {
        "id": "igpsport_folder",
        "type": "folder",
        "enabled": True,
        "visible": True,
        "label": "iGPSPORT 数据文件",
        "config": {"path": "", "device": "iGPSPORT"},
    },
    {
        "id": "phone_export",
        "type": "folder",
        "enabled": False,
        "visible": False,
        "label": "手机导出中转文件夹",
        "config": {"path": "", "device": ""},
    },
    {
        "id": "huawei_export",
        "type": "folder",
        "enabled": False,
        "visible": False,
        "label": "华为运动健康导出",
        "config": {"path": "", "device": "HUAWEI"},
    },
    {
        "id": "outbase_export",
        "type": "folder",
        "enabled": False,
        "visible": False,
        "label": "Outbase / 其他 App 导出",
        "config": {"path": "", "device": "Outbase"},
    },
    {
        "id": "intervals",
        "type": "intervals",
        "enabled": False,
        "visible": False,
        "label": "Intervals.icu",
        "config": {"athlete_id": "", "api_key": "", "days": 30},
    },
    {
        "id": "strava",
        "type": "strava",
        "enabled": False,
        "visible": False,
        "label": "Strava（官方 API）",
        "config": {"client_id": "", "client_secret": "", "tokens": {}, "days": 30},
    },
]


def build_sources(configs: list[dict], state: SyncState, persist=None) -> list[BaseSource]:
    """persist(source_id, config) 用于把刷新后的 token 写回设置。"""
    out = []
    for cfg in configs:
        cls = SOURCE_TYPES.get(cfg.get("type"))
        if not cls:
            continue
        if cls is StravaSource:
            sid = cfg.get("id")
            cb = (lambda conf, _sid=sid: persist(_sid, conf)) if persist else None
            out.append(StravaSource(cfg.get("config") or {}, state, persist=cb))
        else:
            out.append(cls(cfg.get("config") or {}, state))
    return out
