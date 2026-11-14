"""SQLite 存储层。

设计取舍
--------
* 单文件数据库 ``data/pace.db``，零配置、可备份、可拷走
* 大数据（1Hz 流、轨迹）用 zlib 压缩后存 BLOB，避免 JSON 膨胀
* 所有查询走参数化 SQL，杜绝注入
* 派生指标在写入时算好并落库，列表页不必重算
"""
from __future__ import annotations

import base64
import json
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 1


def _channel_score(streams: dict | None) -> int:
    """数据通道完整度：心率/踏频/功率各算 1 分。合并与去重时用它决定保留哪份数据。"""
    streams = streams or {}
    score = 0
    for key in ("hr", "cad", "pwr"):
        series = streams.get(key) or []
        if any(v is not None for v in series):
            score += 1
    return score


def _downsample_route(route: list, limit: int = 44) -> list[list[float]]:
    """把路线等距抽稀到 limit 个点（保留 4 位小数，约 11 m 精度），用于列表缩略图。"""
    pts = [p for p in route if isinstance(p, (list, tuple)) and len(p) >= 2]
    if len(pts) <= limit:
        return [[round(float(p[0]), 4), round(float(p[1]), 4)] for p in pts]
    step = (len(pts) - 1) / (limit - 1)
    out = []
    for i in range(limit):
        p = pts[min(len(pts) - 1, int(round(i * step)))]
        out.append([round(float(p[0]), 4), round(float(p[1]), 4)])
    return out


def _family_sports(name: str) -> list[str]:
    """把 run/ride/swim 这类分组名展开成库里的具体运动类型；不是分组名就返回空。"""
    fam = str(name or "").strip().lower()
    if fam not in ("run", "ride", "swim", "walk", "other"):
        return []
    from .parsers import FAMILIES
    return [s for s, f in FAMILIES.items() if f == fam]

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL DEFAULT 'import',
    external_id      TEXT,
    file_hash        TEXT,
    filename         TEXT,
    sport            TEXT    NOT NULL DEFAULT 'generic',
    title            TEXT,
    notes            TEXT,
    start_time       REAL    NOT NULL,
    end_time         REAL,
    distance_m       REAL    NOT NULL DEFAULT 0,
    source_distance_m REAL,
    moving_seconds   REAL    NOT NULL DEFAULT 0,
    elapsed_seconds  REAL    NOT NULL DEFAULT 0,
    elevation_gain_m REAL    NOT NULL DEFAULT 0,
    elevation_loss_m REAL    NOT NULL DEFAULT 0,
    avg_speed_kmh    REAL    NOT NULL DEFAULT 0,
    max_speed_kmh    REAL    NOT NULL DEFAULT 0,
    pace_s_per_km    REAL,
    avg_hr           REAL,
    max_hr           REAL,
    avg_power        REAL,
    np_power         REAL,
    calories         REAL,
    training_load    REAL    NOT NULL DEFAULT 0,
    load_method      TEXT,
    has_gps          INTEGER NOT NULL DEFAULT 0,
    point_count      INTEGER NOT NULL DEFAULT 0,
    center_lat       REAL,
    center_lon       REAL,
    bbox             TEXT,
    best_efforts     TEXT,
    climbs           TEXT,
    hr_zones         TEXT,
    streams          BLOB,
    route            BLOB,
    raw_meta         TEXT,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_act_start  ON activities(start_time DESC);
CREATE INDEX IF NOT EXISTS idx_act_sport  ON activities(sport, start_time DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_act_hash ON activities(file_hash)
    WHERE file_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_act_ext ON activities(external_id)
    WHERE external_id IS NOT NULL;

-- 同一个活动的所有来源文件指纹（GPX / FIT / TCX）。
-- 一个活动只占 activities 一行，但可能吃过多个文件；这些指纹都指向同一行，
-- 这样用户再传另一份格式时会被认成「同一活动」，而不是新建一条重复记录。
CREATE TABLE IF NOT EXISTS activity_files (
    file_hash   TEXT PRIMARY KEY,
    activity_id INTEGER NOT NULL,
    filename    TEXT,
    source      TEXT,
    added_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_act_files_activity ON activity_files(activity_id);

CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    sport_family  TEXT NOT NULL DEFAULT 'run',
    distance_m    REAL NOT NULL DEFAULT 0,
    elevation_gain_m REAL NOT NULL DEFAULT 0,
    avg_grade_pct REAL,
    start_lat     REAL, start_lon REAL,
    end_lat       REAL, end_lon REAL,
    polyline      TEXT,
    source_activity_id INTEGER,
    effort_count  INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS routebooks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    note          TEXT,
    sport         TEXT NOT NULL DEFAULT 'cycling',
    points        TEXT NOT NULL,
    waypoints     TEXT NOT NULL DEFAULT '[]',
    point_count   INTEGER NOT NULL DEFAULT 0,
    distance_m    REAL NOT NULL DEFAULT 0,
    climb_m       REAL NOT NULL DEFAULT 0,
    source        TEXT,
    source_activity_id INTEGER,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS segment_efforts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id    INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    activity_id   INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    start_index   INTEGER NOT NULL DEFAULT 0,
    end_index     INTEGER NOT NULL DEFAULT 0,
    start_offset_s REAL NOT NULL DEFAULT 0,
    elapsed_s     REAL NOT NULL,
    avg_hr        REAL,
    is_best       INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    UNIQUE(segment_id, activity_id)
);

CREATE INDEX IF NOT EXISTS idx_eff_seg ON segment_efforts(segment_id, elapsed_s);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {
    "hr_max": 190,
    "hr_rest": 55,
    "hr_threshold": 168,
    "weight_kg": 68,
    "units": "metric",
    "tile_url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    "theme": "navy",
    # 教练偏好
    "coach_goal": "health",
    "coach_days_per_week": 4,
    "coach_target_date": "",
    "coach_intake": {},
    # 训练阈值（专项指标用；FTP 是功率区间与 IF/TSS 的基准）
    "ftp": 200,
    "threshold_pace_s": 300,
    "swim_pace_s": 150,
    # 地图底图：amap（高德，国内快）/ osm / offline（离线网格）
    "map_provider": "amap",
    # 高德 Web 服务 Key（路书搜索/自动命名用；空则退回免 Key 的 Photon）
    "amap_key": "",
    # AI 教练（OpenAI 兼容接口；没填 Key 就退回本地规则引擎）
    "llm_enabled": True,
    "llm_api_key": "",
    "llm_base_url": "https://api.deepseek.com/v1",
    "llm_model": "deepseek-chat",
    "llm_timeout_s": 45,
    # 自动同步（秒；0 表示关闭）
    "auto_sync_seconds": 0,
}


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self.conn.commit()
        self._ensure_settings()

    def _ensure_columns(self) -> None:
        """老库补列：路书 2.0 的途经点、正文等后加的字段。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(routebooks)")}
        if "waypoints" not in cols:
            self.conn.execute(
                "ALTER TABLE routebooks ADD COLUMN waypoints TEXT NOT NULL DEFAULT '[]'"
            )

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ 设置

    def _ensure_settings(self) -> None:
        for key, value in DEFAULT_SETTINGS.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO app_state(key, value) VALUES(?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        self.conn.execute("INSERT OR IGNORE INTO app_state(key,value) VALUES('schema_version', ?)",
                          (json.dumps(SCHEMA_VERSION),))
        # 主题改名迁移：老的 dark 等价于新的 navy
        self.conn.execute(
            "UPDATE app_state SET value=? WHERE key='theme' AND value=?",
            (json.dumps("navy"), json.dumps("dark")),
        )
        self.conn.commit()

    def get_settings(self) -> dict:
        rows = self.conn.execute("SELECT key, value FROM app_state").fetchall()
        out = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                out[row["key"]] = row["value"]
        return out

    def set_settings(self, updates: dict) -> dict:
        for key, value in updates.items():
            if key == "schema_version":
                continue
            self.conn.execute(
                "INSERT INTO app_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        self.conn.commit()
        return self.get_settings()

    # ------------------------------------------------------------ 写入

    def upsert_activity(self, rec: dict) -> tuple[int, bool]:
        """按 file_hash / 外部 ID 去重写入。返回 (id, 是否新建)。"""
        now = datetime.now(timezone.utc).timestamp()
        existing = None
        if rec.get("file_hash"):
            existing = self.conn.execute(
                "SELECT id FROM activities WHERE file_hash=?", (rec["file_hash"],)
            ).fetchone()
            if existing is None:
                # 这个文件之前作为「另一个格式」合进过某条活动
                aliased = self.conn.execute(
                    "SELECT activity_id FROM activity_files WHERE file_hash=?",
                    (rec["file_hash"],),
                ).fetchone()
                if aliased:
                    alive = self.conn.execute(
                        "SELECT 1 FROM activities WHERE id=?", (aliased["activity_id"],)
                    ).fetchone()
                    if alive:
                        existing = {"id": int(aliased["activity_id"])}
                    else:
                        # 活动已被删掉（可能绕过接口直接删的），别让悬空指纹挡住重新导入
                        self.conn.execute("DELETE FROM activity_files WHERE file_hash=?",
                                          (rec["file_hash"],))
        if existing is None and rec.get("external_id"):
            existing = self.conn.execute(
                "SELECT id FROM activities WHERE external_id=?", (rec["external_id"],)
            ).fetchone()

        payload = self._pack(rec)
        if existing:
            self._remember_file(rec.get("file_hash"), int(existing["id"]),
                                rec.get("filename"), rec.get("source"))
            # 库里那份可能是合并过 FIT 的高配数据：别让重新上传的 GPX 把心率/功率冲掉
            stored = self.conn.execute(
                "SELECT streams, has_gps FROM activities WHERE id=?", (existing["id"],)
            ).fetchone()
            stored_streams = self._unblob(stored["streams"], {}) if stored else {}
            incoming_has_gps = bool((rec.get("summary") or {}).get("has_gps"))
            # 已经带轨迹的记录，不能被一份没有坐标的文件覆盖（地图会消失）
            if stored and stored["has_gps"] and not incoming_has_gps:
                self.conn.commit()
                return int(existing["id"]), False
            if _channel_score(stored_streams) > _channel_score(rec.get("streams")):
                self.conn.commit()
                return int(existing["id"]), False
            sets = ", ".join(f"{k}=?" for k in payload)
            self.conn.execute(
                f"UPDATE activities SET {sets}, updated_at=? WHERE id=?",
                list(payload.values()) + [now, existing["id"]],
            )
            self.conn.commit()
            return int(existing["id"]), False

        payload["created_at"] = now
        payload["updated_at"] = now
        cols = ", ".join(payload)
        marks = ", ".join("?" for _ in payload)
        cur = self.conn.execute(
            f"INSERT INTO activities ({cols}) VALUES ({marks})", list(payload.values())
        )
        activity_id = int(cur.lastrowid)
        self._remember_file(rec.get("file_hash"), activity_id,
                            rec.get("filename"), rec.get("source"))
        self.conn.commit()
        return activity_id, True

    def _remember_file(self, file_hash: str | None, activity_id: int,
                       filename: str | None = None, source: str | None = None) -> None:
        """记住「这个文件属于哪条活动」，供跨格式去重用。"""
        if not file_hash:
            return
        self.conn.execute(
            "INSERT INTO activity_files(file_hash, activity_id, filename, source, added_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(file_hash) DO UPDATE SET activity_id=excluded.activity_id",
            (file_hash, activity_id, filename, source,
             datetime.now(timezone.utc).timestamp()),
        )

    def find_similar_activity(self, *, start_time: float, distance_m: float, sport: str,
                              tolerance_s: float = 300.0,
                              tolerance_ratio: float = 0.03,
                              exclude: set[int] | None = None) -> dict | None:
        """找「同一次运动」的已有记录。

        同一趟骑行导出的 GPX 与 FIT：起始时间差几秒到两分钟（GPS 定位前后不同），
        距离差通常在 1% 以内。命中就合并，而不是产生两条。
        """
        from .parsers import sport_family
        fam = sport_family(sport)
        rows = self.conn.execute(
            "SELECT id, sport, start_time, distance_m, moving_seconds FROM activities "
            "WHERE start_time BETWEEN ? AND ?",
            (start_time - tolerance_s, start_time + tolerance_s),
        ).fetchall()
        best = None
        for r in rows:
            if exclude and int(r["id"]) in exclude:
                continue
            other_fam = sport_family(r["sport"])
            # 运动类型未知（generic）时也允许匹配：GPX 常常不带类型，FIT/TCX 带
            if fam != other_fam and fam != "other" and other_fam != "other":
                continue
            old_d = float(r["distance_m"] or 0)
            new_d = float(distance_m or 0)
            near = abs(float(r["start_time"]) - start_time)
            if old_d < 100 or new_d < 100:
                same = near <= 60
            else:
                same = abs(old_d - new_d) / max(old_d, new_d) <= tolerance_ratio
            if same and (best is None or near < abs(best["start_time"] - start_time)):
                best = r
        return dict(best) if best else None

    def merge_activity(self, activity_id: int, rec: dict, *, replace: bool = True) -> int:
        """把新文件并入已有活动。

        replace=True 用新文件的数据覆盖（通常是 FIT 覆盖 GPX，补齐心率/功率/踏频）；
        replace=False 只登记文件指纹，保留原有更完整的数据。
        标题与备注永远保留用户改过的版本。
        """
        row = self.conn.execute(
            "SELECT id, title, notes, file_hash, filename, source FROM activities WHERE id=?",
            (activity_id,),
        ).fetchone()
        if row is None:
            return self.upsert_activity(rec)[0]
        now = datetime.now(timezone.utc).timestamp()
        if row["file_hash"]:
            self._remember_file(row["file_hash"], activity_id, row["filename"], row["source"])
        if replace:
            payload = self._pack(rec)
            payload["title"] = row["title"] or payload.get("title")
            payload["notes"] = row["notes"] or payload.get("notes")
            sets = ", ".join(f"{k}=?" for k in payload)
            self.conn.execute(
                f"UPDATE activities SET {sets}, updated_at=? WHERE id=?",
                list(payload.values()) + [now, activity_id],
            )
        self._remember_file(rec.get("file_hash"), activity_id,
                            rec.get("filename"), rec.get("source"))
        self.conn.commit()
        return activity_id

    def _pack(self, rec: dict) -> dict:
        summary = rec.get("summary") or {}

        def num(key, default=0.0):
            value = summary.get(key)
            return float(value) if value is not None else default

        def opt(key):
            value = summary.get(key)
            return float(value) if value is not None else None

        center = summary.get("center") or [None, None]
        bbox = summary.get("bbox")
        streams = rec.get("streams") or {}
        route = rec.get("route") or []

        return {
            "source": rec.get("source", "import"),
            "external_id": rec.get("external_id"),
            "file_hash": rec.get("file_hash"),
            "filename": rec.get("filename"),
            "sport": rec.get("sport") or "generic",
            "title": rec.get("title"),
            "notes": rec.get("notes"),
            "start_time": float(summary.get("start_time") or rec.get("start_time") or 0),
            "end_time": float(summary["start_time"]) + num("elapsed_seconds")
            if summary.get("start_time") else None,
            "distance_m": num("distance_m"),
            "source_distance_m": rec.get("source_distance_m"),
            "moving_seconds": num("moving_seconds"),
            "elapsed_seconds": num("elapsed_seconds"),
            "elevation_gain_m": num("elevation_gain_m"),
            "elevation_loss_m": num("elevation_loss_m"),
            "avg_speed_kmh": num("avg_speed_kmh"),
            "max_speed_kmh": num("max_speed_kmh"),
            "pace_s_per_km": opt("pace_seconds_per_km"),
            "avg_hr": opt("avg_hr"),
            "max_hr": opt("max_hr"),
            "avg_power": opt("avg_power"),
            "np_power": opt("np_power"),
            "calories": opt("calories"),
            "training_load": num("training_load"),
            "load_method": summary.get("load_method"),
            "has_gps": 1 if summary.get("has_gps") else 0,
            "point_count": int(summary.get("point_count") or 0),
            "center_lat": center[0] if center else None,
            "center_lon": center[1] if center else None,
            "bbox": json.dumps(bbox) if bbox else None,
            "best_efforts": json.dumps(summary.get("best_efforts") or [], ensure_ascii=False),
            "climbs": json.dumps(summary.get("climbs") or [], ensure_ascii=False),
            "hr_zones": json.dumps(summary.get("hr_zones") or {}, ensure_ascii=False),
            "streams": self._blob(streams) if streams else None,
            "route": self._blob(route) if route else None,
            "raw_meta": json.dumps(rec.get("raw_meta") or {}, ensure_ascii=False, default=str),
        }

    @staticmethod
    def _blob(obj) -> bytes:
        return zlib.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 6)

    @staticmethod
    def _unblob(blob, default):
        if not blob:
            return default
        try:
            return json.loads(zlib.decompress(blob).decode("utf-8"))
        except (zlib.error, json.JSONDecodeError):
            return default

    # ------------------------------------------------------------ 读取

    def list_activities(self, *, limit: int = 50, offset: int = 0, sport: str | None = None,
                        sports: list[str] | None = None, date_from: float | None = None,
                        date_to: float | None = None, q: str | None = None,
                        sort: str = "date") -> dict:
        where, params = self._filters(sport, sports, date_from, date_to, q)
        order = {
            "date": "start_time DESC",
            "date_asc": "start_time ASC",
            "distance": "distance_m DESC",
            "duration": "moving_seconds DESC",
            "elevation": "elevation_gain_m DESC",
            "load": "training_load DESC",
        }.get(sort, "start_time DESC")

        total = self.conn.execute(f"SELECT COUNT(*) c FROM activities {where}", params).fetchone()["c"]
        agg = self.conn.execute(
            f"SELECT COALESCE(SUM(distance_m),0) d, COALESCE(SUM(moving_seconds),0) t, "
            f"COALESCE(SUM(elevation_gain_m),0) e, COALESCE(SUM(training_load),0) l "
            f"FROM activities {where}", params
        ).fetchone()
        rows = self.conn.execute(
            f"SELECT * FROM activities {where} ORDER BY {order} LIMIT ? OFFSET ?",
            list(params) + [limit, offset],
        ).fetchall()
        from .parsers import sport_family
        families = {"run": 0, "ride": 0, "swim": 0, "walk": 0, "other": 0}
        for r in self.conn.execute("SELECT sport, COUNT(*) c FROM activities GROUP BY sport"):
            families[sport_family(r["sport"])] = families.get(sport_family(r["sport"]), 0) + r["c"]
        return {"total": total, "limit": limit, "offset": offset,
                "stats": {"distance_m": agg["d"], "moving_seconds": agg["t"],
                          "elevation_gain_m": agg["e"], "training_load": agg["l"]},
                "families": families,
                "items": [self._row_to_activity(r, full=False) for r in rows]}

    def _filters(self, sport, sports, date_from, date_to, q):
        clauses = []
        params: list = []
        if sport and sport != "all":
            # 前端顶部按「跑步 / 骑行 / 游泳」筛，传的是 family（run/ride/swim），
            # 库里存的是具体运动类型（running/cycling…），必须先展开成同族列表。
            members = _family_sports(sport)
            if members:
                clauses.append(f"sport IN ({','.join('?' for _ in members)})")
                params.extend(members)
            else:
                clauses.append("sport = ?")
                params.append(sport)
        if sports:
            clauses.append(f"sport IN ({','.join('?' for _ in sports)})")
            params.extend(sports)
        if date_from is not None:
            clauses.append("start_time >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("start_time < ?")
            params.append(date_to)
        if q:
            clauses.append("(title LIKE ? OR notes LIKE ? OR sport LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def get_activity(self, activity_id: int, full: bool = True) -> dict | None:
        row = self.conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_activity(row, full=full)

    def _row_to_activity(self, row: sqlite3.Row, full: bool = False) -> dict:
        item = {
            "id": row["id"],
            "source": row["source"],
            "external_id": row["external_id"],
            "file_hash": row["file_hash"],
            "sport": row["sport"],
            "title": row["title"],
            "notes": row["notes"],
            "filename": row["filename"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "distance_m": row["distance_m"],
            "source_distance_m": row["source_distance_m"],
            "moving_seconds": row["moving_seconds"],
            "elapsed_seconds": row["elapsed_seconds"],
            "elevation_gain_m": row["elevation_gain_m"],
            "elevation_loss_m": row["elevation_loss_m"],
            "avg_speed_kmh": row["avg_speed_kmh"],
            "max_speed_kmh": row["max_speed_kmh"],
            "pace_s_per_km": row["pace_s_per_km"],
            "avg_hr": row["avg_hr"],
            "max_hr": row["max_hr"],
            "avg_power": row["avg_power"],
            "np_power": row["np_power"],
            "calories": row["calories"],
            "training_load": row["training_load"],
            "load_method": row["load_method"],
            "has_gps": bool(row["has_gps"]),
            "point_count": row["point_count"],
            "center": [row["center_lat"], row["center_lon"]] if row["center_lat"] else None,
            "bbox": json.loads(row["bbox"]) if row["bbox"] else None,
            "best_efforts": json.loads(row["best_efforts"] or "[]"),
            "climbs": json.loads(row["climbs"] or "[]"),
            "hr_zones": json.loads(row["hr_zones"] or "{}"),
        }
        if full:
            item["streams"] = self._unblob(row["streams"], {})
            item["route"] = self._unblob(row["route"], [])
            item["raw_meta"] = json.loads(row["raw_meta"] or "{}")
        elif row["route"]:
            # 列表卡片要画缩略轨迹：给一份抽稀到 44 个点的路线，别把整条路线塞进列表
            route = self._unblob(row["route"], [])
            if route:
                item["route_preview"] = _downsample_route(route, 44)
        return item

    def update_metrics(self, activity_id: int, summary: dict) -> None:
        """只更新派生指标与流数据，不动原始元信息（用于改心率设置后重算）。"""
        now = datetime.now(timezone.utc).timestamp()
        fields = {
            "distance_m": float(summary.get("distance_m") or 0),
            "moving_seconds": float(summary.get("moving_seconds") or 0),
            "elapsed_seconds": float(summary.get("elapsed_seconds") or 0),
            "elevation_gain_m": float(summary.get("elevation_gain_m") or 0),
            "elevation_loss_m": float(summary.get("elevation_loss_m") or 0),
            "avg_speed_kmh": float(summary.get("avg_speed_kmh") or 0),
            "max_speed_kmh": float(summary.get("max_speed_kmh") or 0),
            "pace_s_per_km": summary.get("pace_seconds_per_km"),
            "avg_hr": summary.get("avg_hr"),
            "max_hr": summary.get("max_hr"),
            "training_load": float(summary.get("training_load") or 0),
            "load_method": summary.get("load_method"),
            "hr_zones": json.dumps(summary.get("hr_zones") or {}, ensure_ascii=False),
            "best_efforts": json.dumps(summary.get("best_efforts") or [], ensure_ascii=False),
            "climbs": json.dumps(summary.get("climbs") or [], ensure_ascii=False),
            "point_count": int(summary.get("point_count") or 0),
        }
        if summary.get("streams"):
            fields["streams"] = self._blob(summary["streams"])
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE activities SET {sets}, updated_at=? WHERE id=?",
            list(fields.values()) + [now, activity_id],
        )
        self.conn.commit()

    def update_sport_if_generic(self, activity_id: int, sport: str) -> None:
        """合并时如果旧记录是「其他」，用新文件更具体的类型补上。"""
        if not sport or sport == "generic":
            return
        row = self.conn.execute("SELECT sport FROM activities WHERE id=?", (activity_id,)).fetchone()
        if row and (row["sport"] in (None, "generic", "workout")):
            self.conn.execute("UPDATE activities SET sport=? WHERE id=?", (sport, activity_id))
            self.conn.commit()

    def update_streams(self, activity_id: int, streams: dict, updates: dict) -> None:
        """只更新流数据与传感器相关字段（合并另一份文件的数据时用）。"""
        allowed = {"avg_hr", "max_hr", "avg_power", "np_power",
                   "training_load", "load_method", "hr_zones", "point_count"}
        payload: dict = {}
        for key, value in (updates or {}).items():
            if key not in allowed:
                continue
            payload[key] = (json.dumps(value, ensure_ascii=False)
                            if key == "hr_zones" else value)
        if streams:
            payload["streams"] = self._blob(streams)
        if not payload:
            return
        payload["updated_at"] = datetime.now(timezone.utc).timestamp()
        sets = ", ".join(f"{k}=?" for k in payload)
        self.conn.execute(f"UPDATE activities SET {sets} WHERE id=?",
                          list(payload.values()) + [activity_id])
        self.conn.commit()

    def delete_activity(self, activity_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM activities WHERE id=?", (activity_id,))
        self.conn.execute("DELETE FROM activity_files WHERE activity_id=?", (activity_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def update_activity(self, activity_id: int, fields: dict) -> bool:
        allowed = {"title", "notes", "sport", "start_time"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        now = datetime.now(timezone.utc).timestamp()
        sets = ", ".join(f"{k}=?" for k in updates)
        self.conn.execute(
            f"UPDATE activities SET {sets}, updated_at=? WHERE id=?",
            list(updates.values()) + [now, activity_id],
        )
        self.conn.commit()
        return True

    def all_activity_summaries(self, date_from: float | None = None,
                               date_to: float | None = None) -> list[dict]:
        where, params = self._filters(None, None, date_from, date_to, None)
        rows = self.conn.execute(
            f"SELECT id, sport, start_time, distance_m, moving_seconds, elapsed_seconds, "
            f"elevation_gain_m, training_load, avg_hr, avg_power, avg_speed_kmh, has_gps "
            f"FROM activities {where} ORDER BY start_time ASC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) c FROM activities").fetchone()["c"])

    # ------------------------------------------------------------ 分段

    def create_segment(self, data: dict) -> int:
        now = datetime.now(timezone.utc).timestamp()
        cur = self.conn.execute(
            "INSERT INTO segments(name, sport_family, distance_m, elevation_gain_m, avg_grade_pct,"
            " start_lat, start_lon, end_lat, end_lon, polyline, source_activity_id, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data["name"], data.get("sport_family", "run"), data.get("distance_m", 0),
             data.get("elevation_gain_m", 0), data.get("avg_grade_pct"),
             data.get("start_lat"), data.get("start_lon"), data.get("end_lat"), data.get("end_lon"),
             data.get("polyline"), data.get("source_activity_id"), now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_segments(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM segment_efforts e WHERE e.segment_id=s.id) n "
            "FROM segments s ORDER BY s.created_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["effort_count"] = item.pop("n", 0)
            best = self.conn.execute(
                "SELECT elapsed_s, activity_id FROM segment_efforts WHERE segment_id=? "
                "ORDER BY elapsed_s ASC LIMIT 1", (r["id"],)
            ).fetchone()
            item["best_effort"] = dict(best) if best else None
            out.append(item)
        return out

    def get_segment(self, segment_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        efforts = self.conn.execute(
            "SELECT e.*, a.title, a.start_time, a.sport, a.distance_m, a.avg_hr AS act_avg_hr "
            "FROM segment_efforts e JOIN activities a ON a.id=e.activity_id "
            "WHERE e.segment_id=? ORDER BY e.elapsed_s ASC", (segment_id,)
        ).fetchall()
        item["efforts"] = [dict(e) for e in efforts]
        item["best"] = item["efforts"][0] if item["efforts"] else None
        return item

    def delete_segment(self, segment_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM segments WHERE id=?", (segment_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ 路书

    def create_routebook(self, data: dict) -> int:
        now = datetime.now(timezone.utc).timestamp()
        points = data.get("points") or []
        waypoints = data.get("waypoints") or []
        cur = self.conn.execute(
            "INSERT INTO routebooks(name, note, sport, points, waypoints, point_count,"
            " distance_m, climb_m, source, source_activity_id, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (data["name"], data.get("note") or "", data.get("sport") or "cycling",
             json.dumps(points, ensure_ascii=False),
             json.dumps(waypoints, ensure_ascii=False), len(points),
             float(data.get("distance_m") or 0), float(data.get("climb_m") or 0),
             data.get("source") or "manual", data.get("source_activity_id"), now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_routebook(self, book_id: int, data: dict) -> bool:
        row = self.conn.execute("SELECT * FROM routebooks WHERE id=?", (book_id,)).fetchone()
        if row is None:
            return False
        fields, values = [], []
        if "name" in data and data["name"]:
            fields.append("name=?"); values.append(str(data["name"])[:120])
        if "note" in data:
            fields.append("note=?"); values.append(str(data.get("note") or ""))
        if "points" in data and data["points"] is not None:
            pts = data["points"]
            fields += ["points=?", "point_count=?"]
            values += [json.dumps(pts, ensure_ascii=False), len(pts)]
        if "waypoints" in data and data["waypoints"] is not None:
            fields.append("waypoints=?")
            values.append(json.dumps(data["waypoints"], ensure_ascii=False))
        if "distance_m" in data and data["distance_m"] is not None:
            fields.append("distance_m=?"); values.append(float(data["distance_m"]))
        if "climb_m" in data and data["climb_m"] is not None:
            fields.append("climb_m=?"); values.append(float(data["climb_m"]))
        if not fields:
            return False
        fields.append("updated_at=?"); values.append(datetime.now(timezone.utc).timestamp())
        values.append(book_id)
        self.conn.execute(f"UPDATE routebooks SET {', '.join(fields)} WHERE id=?", values)
        self.conn.commit()
        return True

    def list_routebooks(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, name, note, sport, points, waypoints, point_count, distance_m, climb_m,"
            " source, source_activity_id, created_at, updated_at FROM routebooks"
            " ORDER BY updated_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                pts = json.loads(item.pop("points") or "[]")
            except Exception:
                pts = []
            try:
                item["waypoint_count"] = len(json.loads(item.pop("waypoints") or "[]"))
            except Exception:
                item["waypoint_count"] = 0
            item["start"] = pts[0] if pts else None
            item["end"] = pts[-1] if pts else None
            # 列表卡片要画缩略图：抽稀到 ≤60 点，别把整条轨迹塞给浏览器
            step = max(1, len(pts) // 60)
            item["preview"] = pts[::step][:60]
            out.append(item)
        return out

    def get_routebook(self, book_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM routebooks WHERE id=?", (book_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["points"] = json.loads(item.get("points") or "[]")
        except Exception:
            item["points"] = []
        try:
            item["waypoints"] = json.loads(item.get("waypoints") or "[]")
        except Exception:
            item["waypoints"] = []
        return item

    def delete_routebook(self, book_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM routebooks WHERE id=?", (book_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def add_effort(self, segment_id: int, activity_id: int, effort: dict) -> None:
        now = datetime.now(timezone.utc).timestamp()
        self.conn.execute(
            "INSERT INTO segment_efforts(segment_id, activity_id, start_index, end_index,"
            " start_offset_s, elapsed_s, avg_hr, created_at) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(segment_id, activity_id) DO UPDATE SET"
            " start_index=excluded.start_index, end_index=excluded.end_index,"
            " start_offset_s=excluded.start_offset_s, elapsed_s=excluded.elapsed_s,"
            " avg_hr=excluded.avg_hr",
            (segment_id, activity_id, effort.get("start_index", 0), effort.get("end_index", 0),
             effort.get("start_offset_s", 0), effort["elapsed_s"], effort.get("avg_hr"), now),
        )
        self.conn.commit()

    def refresh_effort_bests(self, segment_id: int | None = None) -> None:
        ids = [segment_id] if segment_id else [
            r["id"] for r in self.conn.execute("SELECT id FROM segments").fetchall()
        ]
        for sid in ids:
            self.conn.execute("UPDATE segment_efforts SET is_best=0 WHERE segment_id=?", (sid,))
            best = self.conn.execute(
                "SELECT id FROM segment_efforts WHERE segment_id=? ORDER BY elapsed_s ASC LIMIT 1",
                (sid,),
            ).fetchone()
            if best:
                self.conn.execute("UPDATE segment_efforts SET is_best=1 WHERE id=?", (best["id"],))
            self.conn.execute(
                "UPDATE segments SET effort_count=(SELECT COUNT(*) FROM segment_efforts WHERE segment_id=?),"
                " updated_at=? WHERE id=?",
                (sid, datetime.now(timezone.utc).timestamp(), sid),
            )
        self.conn.commit()

    def activities_with_streams(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, sport, start_time, streams FROM activities "
            "WHERE streams IS NOT NULL ORDER BY start_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{
            "id": r["id"], "sport": r["sport"], "start_time": r["start_time"],
            "streams": self._unblob(r["streams"], {}),
        } for r in rows]


def open_store(path: str) -> Store:
    return Store(path)


def day_bounds(days: int, now: float | None = None, tz_offset_hours: float = 8.0) -> tuple[float, float]:
    """以本地时区对齐的 [今天-days, 明天) 边界。"""
    now = now or datetime.now(timezone.utc).timestamp()
    tz = timezone(timedelta(hours=tz_offset_hours))
    local = datetime.fromtimestamp(now, tz)
    start_local = (local - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.timestamp(), end_local.timestamp()
