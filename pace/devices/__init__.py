"""同步编排：扫描各数据源 → 调用导入函数 → 记录状态。

刻意与 HTTP 层解耦：导入函数作为参数注入，命令行和服务器共用同一套逻辑，
测试时可以直接传一个 fake importer。
"""
from __future__ import annotations

import os
import time

from . import sources as S


class SyncEngine:
    def __init__(self, store, state_path: str, importer):
        """
        store      : pace.store.Store
        state_path : data/sync-state.json
        importer   : callable(filename, bytes) -> dict（与 server._import_one 同构）
        """
        self.store = store
        self.state = S.SyncState(state_path)
        self.importer = importer

    # ------------------------------------------------------------ 配置

    def configs(self) -> list[dict]:
        raw = self.store.get_settings().get("sources")
        if not isinstance(raw, list) or not raw:
            raw = [dict(x) for x in S.DEFAULT_SOURCES]
            self.set_configs(raw)
        # 补齐新增的默认源，并给老配置补上 visible 字段
        known = {c.get("id") for c in raw}
        changed = False
        for default in S.DEFAULT_SOURCES:
            if default["id"] not in known:
                raw.append(dict(default))
                changed = True
        for cfg in raw:
            if "visible" not in cfg:
                cfg["visible"] = cfg.get("id") not in S.HIDDEN_BY_DEFAULT
                changed = True
        if changed:
            self.set_configs(raw)
        return raw

    def set_configs(self, configs: list[dict]) -> list[dict]:
        clean = []
        for cfg in configs:
            if not isinstance(cfg, dict):
                continue
            kind = cfg.get("type")
            if kind not in S.SOURCE_TYPES:
                continue
            clean.append({
                "id": str(cfg.get("id") or f"{kind}-{len(clean)}"),
                "type": kind,
                "enabled": bool(cfg.get("enabled", True)),
                "visible": bool(cfg.get("visible", True)),
                "label": str(cfg.get("label") or S.SOURCE_TYPES[kind].label),
                "config": cfg.get("config") or {},
            })
        self.store.set_settings({"sources": clean})
        return clean

    def sources(self) -> list[S.BaseSource]:
        enabled = [c for c in self.configs() if c.get("enabled")]
        return S.build_sources(enabled, self.state, persist=self.save_source_config)

    def save_source_config(self, source_id: str, config: dict) -> None:
        """OAuth token 刷新后写回，避免每次同步都重新授权。"""
        configs = self.configs()
        for cfg in configs:
            if cfg.get("id") == source_id:
                cfg["config"] = config
                break
        self.set_configs(configs)

    def source_by_id(self, source_id: str):
        for cfg in self.configs():
            if cfg["id"] == source_id:
                cls = S.SOURCE_TYPES[cfg["type"]]
                if cls is S.StravaSource:
                    return cls(cfg.get("config") or {}, self.state,
                               persist=lambda conf: self.save_source_config(source_id, conf))
                return cls(cfg.get("config") or {}, self.state)
        return None

    # ------------------------------------------------------------ 状态

    def status(self) -> dict:
        out = []
        for cfg in self.configs():
            cls = S.SOURCE_TYPES[cfg["type"]]
            src = cls(cfg.get("config") or {}, self.state)
            try:
                info = src.status()
            except Exception as exc:  # noqa: BLE001
                info = {"available": False, "hint": f"状态检查失败：{exc}"}
            info.update({"id": cfg["id"], "type": cfg["type"],
                         "label": cfg.get("label") or cls.label,
                         "enabled": bool(cfg.get("enabled")),
                         "visible": bool(cfg.get("visible", True)),
                         "config": cfg.get("config") or {}})
            out.append(info)
        return {
            "sources": out,
            "last_sync": self.state.data.get("last_sync"),
            "tracked_files": len(self.state.data.get("files") or {}),
            "intervals_tracked": len(self.state.data.get("intervals") or {}),
        }

    # ------------------------------------------------------------ 执行

    def sync(self, *, source_ids: list[str] | None = None, limit: int = 40,
             dry_run: bool = False) -> dict:
        started = time.time()
        results = []
        total_imported = 0
        total_merged = 0
        total_errors = 0

        active = self.sources()
        active_ids = {self._source_id(s) for s in active}

        # 指名要同步、却没有启用（或不存在）的数据源必须显式报出来，
        # 否则前端只会看到"没有发现活动"，完全无法定位。
        if source_ids:
            for sid in source_ids:
                if sid in active_ids:
                    continue
                cfg = next((c for c in self.configs() if c["id"] == sid), None)
                if cfg is None:
                    results.append({"source": sid, "scanned": 0, "imported": 0,
                                    "errors": 0, "detail": "数据源不存在"})
                else:
                    results.append({
                        "source": sid, "scanned": 0, "imported": 0, "errors": 0,
                        "detail": "该数据源未启用：勾选「启用」后再试",
                        "enabled": False,
                    })

        for src in active:
            sid = self._source_id(src)
            if source_ids and sid not in source_ids:
                continue
            try:
                items = src.scan(limit=limit)
            except Exception as exc:  # noqa: BLE001
                results.append({"source": sid, "scanned": 0, "imported": 0,
                                "errors": 1, "detail": f"扫描失败：{exc}"})
                total_errors += 1
                continue
            if not items:
                detail = "没有新文件"
                extra = getattr(src, "last_errors", None)
                if extra:
                    detail = f"{len(extra)} 条活动取不到原始文件"
                results.append({"source": sid, "scanned": 0, "imported": 0, "errors": 0,
                                "detail": detail, "skipped_activities": extra or []})
                continue

            imported, merged, duplicates, failed = 0, 0, 0, 0
            for item in items:
                try:
                    outcome = self.importer(item.name, item.data)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    results.append({"source": sid, "file": item.name,
                                    "status": "error", "reason": str(exc)})
                    continue
                fresh = (outcome or {}).get("imported") or []
                created = [x for x in fresh if not x.get("merged")]
                alikes = [x for x in fresh if x.get("merged")]
                dups = (outcome or {}).get("duplicates") or []
                errors = (outcome or {}).get("errors") or []
                imported += len(created)
                merged += len(alikes)
                duplicates += len(dups)
                failed += len(errors) + (1 if outcome.get("status") == "error" else 0)
                # 只有"导入成功"或"确认为重复"才标记已处理；
                # 解析失败必须留待下次重试，否则一次 bug 就永久丢数据
                if not dry_run and (created or alikes or dups):
                    src.mark_done(item)
                results.append({
                    "source": sid, "file": item.name,
                    "status": "ok" if (created or alikes or dups) else "error",
                    "imported": [{"id": x.get("id"), "title": x.get("title")} for x in created],
                    "merged": len(alikes),
                    "duplicates": len(dups),
                    "reason": (outcome.get("reason") or (errors[0] if errors else None)),
                })
            if not dry_run:
                self.state.touch()
                self.state.save()
            total_imported += imported
            total_merged += merged
            total_errors += failed
            results.append({"source": sid, "scanned": len(items), "imported": imported,
                            "merged": merged,
                            "duplicates": duplicates, "errors": failed,
                            "detail": f"扫描 {len(items)} 个，导入 {imported} 条"
                                      + (f"、合并 {merged} 条" if merged else ""),
                            "skipped_activities": getattr(src, "last_errors", None) or []})

        return {
            "started_at": started,
            "elapsed_ms": round((time.time() - started) * 1000),
            "dry_run": dry_run,
            "imported": total_imported,
            "merged": total_merged,
            "errors": total_errors,
            "active_sources": len(active),
            "results": results,
        }

    def _source_id(self, src) -> str:
        """根据 config 反查 source id（同一类型可能配置多个）。"""
        for cfg in self.configs():
            if not cfg.get("enabled"):
                continue
            if cfg["type"] != src.kind:
                continue
            if cfg["type"] == "folder" and (cfg.get("config") or {}).get("path") == src.path:
                return cfg["id"]
            if cfg["type"] != "folder":
                return cfg["id"]
        return src.kind

    def test_source(self, source_id: str) -> dict:
        src = self.source_by_id(source_id)
        if src is None:
            return {"ok": False, "error": "数据源不存在"}
        if hasattr(src, "test"):
            return src.test()
        status = src.status()
        return {"ok": bool(status.get("available")),
                "detail": status.get("hint") or "",
                "status": status}
