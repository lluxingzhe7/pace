"""BLE 协议知识库与抓包存档。

浏览器侧用的是 Web Bluetooth（`navigator.bluetooth`），它只能"主动连设备"，
不能"模拟一个设备"——没有外设模式的 API。这一点无法绕过。

能标准化解析的部分（公开的蓝牙 SIG 服务）：
* 0x180D 心率服务 HRS          -> 心率
* 0x1816 骑行速度与踏频 CSC     -> 速度 / 踏频
* 0x1818 骑行功率 CPS           -> 功率 / 踏频
* 0x1826 健身设备 FTMS          -> 速度 / 踏频 / 功率 / 心率（室内台）

厂商私有服务（iGPSPORT / 华为）没有公开 UUID 与帧格式，
必须先用真实的设备抓一次包，反推出「服务 UUID + 特征值 + 帧结构」，
再补进下面的 PROTOCOLS 表。在此之前，任何"直接连上就能读数据"的说法都是假的。
"""
from __future__ import annotations

import json
import os
import time

# ---------------------------------------------------------------- 标准服务

STANDARD_SERVICES = [
    {
        "uuid": "0000180d-0000-1000-8000-00805f9b34fb",
        "short": "180D",
        "name": "心率服务 HRS",
        "characteristics": [
            {"uuid": "00002a37-0000-1000-8000-00805f9b34fb", "name": "Heart Rate Measurement"},
        ],
    },
    {
        "uuid": "00001816-0000-1000-8000-00805f9b34fb",
        "short": "1816",
        "name": "骑行速度与踏频 CSC",
        "characteristics": [
            {"uuid": "00002a5b-0000-1000-8000-00805f9b34fb", "name": "CSC Measurement"},
        ],
    },
    {
        "uuid": "00001818-0000-1000-8000-00805f9b34fb",
        "short": "1818",
        "name": "骑行功率 CPS",
        "characteristics": [
            {"uuid": "00002a63-0000-1000-8000-00805f9b34fb", "name": "Cycling Power Measurement"},
        ],
    },
    {
        "uuid": "00001826-0000-1000-8000-00805f9b34fb",
        "short": "1826",
        "name": "健身设备 FTMS",
        "characteristics": [
            {"uuid": "00002ad2-0000-1000-8000-00805f9b34fb", "name": "Indoor Bike Data"},
            {"uuid": "00002aca-0000-1000-8000-00805f9b34fb", "name": "Cross Trainer Data"},
        ],
    },
]

# 已知厂商服务的特征值线索（通过社区抓包得到的部分，可能随固件变化）
VENDOR_HINTS = [
    {
        "name": "iGPSPORT（未验证）",
        "note": "社区没有公开的 GATT 规范。需要你自己抓一次：连上设备 → 打开通知 → "
                "骑行/记录 → 导出抓包，把服务 UUID 与帧格式补进来。",
        "services": [],
        "status": "unknown",
    },
    {
        "name": "华为手表（未验证）",
        "note": "华为穿戴走 HarmonyOS 分布式协议，且数据只回传运动健康。"
                "浏览器层面没有可用的公开服务，实际可行路径仍是「运动健康导出」。",
        "services": [],
        "status": "unknown",
    },
]

# ---------------------------------------------------------------- 解析器


def decode_heart_rate(data: bytes) -> dict | None:
    """0x2A37：flags 第 0 位决定心率是 uint8 还是 uint16。"""
    if len(data) < 2:
        return None
    flags = data[0]
    if flags & 0x01:
        if len(data) < 3:
            return None
        hr = int.from_bytes(data[1:3], "little")
        return {"heart_rate": hr}
    return {"heart_rate": data[1]}


def decode_csc(data: bytes) -> dict | None:
    """0x2A5B：flags 决定是否含轮速与曲柄数据。"""
    if len(data) < 1:
        return None
    flags = data[0]
    idx = 1
    out: dict = {}
    if flags & 0x01 and len(data) >= idx + 6:     # 轮速
        revs = int.from_bytes(data[idx:idx + 4], "little")
        evt = int.from_bytes(data[idx + 4:idx + 6], "little") / 1024.0
        out.update({"wheel_revs": revs, "wheel_evt_s": round(evt, 3)})
        idx += 6
    if flags & 0x02 and len(data) >= idx + 4:     # 曲柄
        revs = int.from_bytes(data[idx:idx + 2], "little")
        evt = int.from_bytes(data[idx + 2:idx + 4], "little") / 1024.0
        out.update({"crank_revs": revs, "crank_evt_s": round(evt, 3)})
    return out or None


def decode_cps(data: bytes) -> dict | None:
    """0x2A63：flags(2B) + 瞬时功率(2B)。"""
    if len(data) < 4:
        return None
    power = int.from_bytes(data[2:4], "little")
    return {"power_w": power}


def decode_ftms_indoor_bike(data: bytes) -> dict | None:
    """0x2AD2：flags(2B)，其后按位顺序排列字段。"""
    if len(data) < 2:
        return None
    flags = int.from_bytes(data[0:2], "little")
    idx = 2
    out: dict = {}
    if flags & 0x0001 and len(data) >= idx + 1:
        out["speed_kmh"] = round(int.from_bytes(data[idx:idx + 2], "little") / 100, 2)
        idx += 2
    if flags & 0x0002 and len(data) >= idx + 2:
        out["avg_speed_kmh"] = round(int.from_bytes(data[idx:idx + 2], "little") / 100, 2)
        idx += 2
    if flags & 0x0004 and len(data) >= idx + 2:
        out["cadence_rpm"] = int.from_bytes(data[idx:idx + 2], "little") / 2
        idx += 2
    if flags & 0x0008 and len(data) >= idx + 2:
        out["avg_cadence_rpm"] = int.from_bytes(data[idx:idx + 2], "little") / 2
        idx += 2
    if flags & 0x0010 and len(data) >= idx + 3:
        out["distance_m"] = int.from_bytes(data[idx:idx + 3], "little")
        idx += 3
    if flags & 0x0040 and len(data) >= idx + 2:
        out["power_w"] = int.from_bytes(data[idx:idx + 2], "little", signed=True)
        idx += 2
    if flags & 0x0080 and len(data) >= idx + 2:
        out["avg_power_w"] = int.from_bytes(data[idx:idx + 2], "little", signed=True)
        idx += 2
    if flags & 0x0200 and len(data) >= idx + 2:
        out["heart_rate"] = int.from_bytes(data[idx:idx + 2], "little")
    return out or None


DECODERS = {
    "2a37": decode_heart_rate,
    "2a5b": decode_csc,
    "2a63": decode_cps,
    "2ad2": decode_ftms_indoor_bike,
}


def decode_notification(char_uuid: str, data: bytes) -> dict | None:
    """按特征值短 UUID 分派解析；未知特征值返回 None。"""
    short = normalize_uuid(char_uuid)[4:8]
    decoder = DECODERS.get(short)
    if not decoder:
        return None
    try:
        return decoder(data)
    except (IndexError, ValueError):
        return None


def normalize_uuid(uuid: str) -> str:
    u = (uuid or "").lower().replace("0x", "")
    if len(u) == 4:
        return f"0000{u}-0000-1000-8000-00805f9b34fb"
    return u


# ---------------------------------------------------------------- 抓包存档

MAX_CAPTURE_EVENTS = 4000


class CaptureStore:
    """保存用户从浏览器导出的 BLE 抓包，供后续补协议解析。

    只存 UUID 与十六进制帧，不存任何账号信息。
    """

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def save(self, capture: dict) -> dict:
        events = capture.get("events") or []
        if not isinstance(events, list):
            raise ValueError("events 必须是数组")
        entry = {
            "saved_at": time.time(),
            "device_name": str(capture.get("device_name") or "unknown")[:80],
            "device_id": str(capture.get("device_id") or "")[:80],
            "services": capture.get("services") or [],
            "event_count": min(len(events), MAX_CAPTURE_EVENTS),
            "events": events[:MAX_CAPTURE_EVENTS],
        }
        data = []
        if os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                data = []
        data.append(entry)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data[-20:], fh, ensure_ascii=False)
        os.replace(tmp, self.path)
        return {"ok": True, "event_count": entry["event_count"],
                "saved_total": len(data)}

    def summary(self) -> dict:
        if not os.path.isfile(self.path):
            return {"captures": 0, "devices": []}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {"captures": 0, "devices": []}
        devices = sorted({c.get("device_name") or "unknown" for c in data})
        return {"captures": len(data), "devices": devices,
                "last": data[-1].get("saved_at") if data else None}
