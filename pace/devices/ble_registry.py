"""BLE 设备档案：记录实测过的设备，避免重复踩坑。

这里的目标不是"支持更多设备"，而是把"哪条路走得通"变成可查询的事实。
每条记录都来自真实连接日志，不写猜测。
"""
from __future__ import annotations

import json
import os
import time

# 已实测的设备档案
KNOWN_DEVICES = {
    "BSC200S": {
        "name": "iGPSPORT BSC200S",
        "vendor": "iGPSPORT",
        "kind": "bike_computer",
        "observed": {
            "date": "2026-11",
            "services": [
                {"uuid": "00001800-0000-1000-8000-00805f9b34fb", "chars": ["2A00", "2A01", "2AA6", "2AC9"]},
                {"uuid": "00001801-0000-1000-8000-00805f9b34fb", "chars": ["2A05"]},
            ],
            "data_services_found": [],
        },
        "verdict": "unsupported",
        "reason": "只暴露 GAP/GATT 基础服务，没有心率(180D)/踏频(1816)/功率(1818)/FTMS(1826)。"
                  "2AA6 与 2AC9 说明它只提供非连接型广播与地址隐私，"
                  "运动数据不通过标准 BLE 服务外发。",
        "working_paths": ["intervals", "usb_fit"],
        "notes": [
            "手机 App 占用连接时，电脑端只能看到基础服务骨架。",
            "2A05（Service Changed）订阅失败是正常现象，与数据无关。",
        ],
    },
}


class DeviceRegistry:
    """读写 data/ble-devices.json，把用户实测结果累积下来。"""

    def __init__(self, path: str):
        self.path = path
        self.data = dict(KNOWN_DEVICES)
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                extra = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(extra, dict):
            for name, entry in extra.items():
                if isinstance(entry, dict):
                    self.data.setdefault(name, entry)

    def save_observed(self, device_name: str, services: list, note: str | None = None) -> dict:
        """记录一次实测的服务列表，自动给出结论。"""
        name = (device_name or "unknown").strip()
        shorts = []
        for svc in services or []:
            uuid = str(svc.get("uuid") or "")
            chars = [str(c.get("uuid") or "")[4:8].upper() for c in svc.get("chars") or []]
            shorts.append({"uuid": uuid, "chars": [c for c in chars if c]})
        data_services = [s for s in shorts
                         if s["uuid"][4:8].lower() in ("180d", "1816", "1818", "1826")]
        verdict = "supported" if data_services else "unsupported"
        entry = {
            "name": name,
            "kind": "unknown",
            "observed": {"date": time.strftime("%Y-%m"), "services": shorts},
            "verdict": verdict,
            "reason": ("发现标准运动数据服务，可用浏览器直读。"
                       if data_services else
                       "只暴露 GAP/GATT 基础服务，没有标准运动数据服务。"),
            "note": note,
        }
        # 已知设备保留更完整的档案，只补 note
        if name in self.data:
            self.data[name].setdefault("user_notes", [])
            if note:
                self.data[name]["user_notes"].append(note)
        else:
            self.data[name] = entry
        self._persist()
        return {"name": name, "verdict": verdict, "entry": self.data[name]}

    def _persist(self) -> None:
        extra = {k: v for k, v in self.data.items() if k not in KNOWN_DEVICES}
        if not extra:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(extra, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def lookup(self, device_name: str | None) -> dict | None:
        if not device_name:
            return None
        key = device_name.strip().upper()
        for name, entry in self.data.items():
            if name.upper() in key or key in name.upper():
                return entry
        return None

    def all(self) -> dict:
        return self.data
