"""Post-processing task and managed media version storage."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DEFAULT_POSTPROCESS_SETTINGS: dict[str, Any] = {
    "auto_transcode_enabled": False,
    "auto_subtitle_enabled": False,
    "worker_auto_run": False,
    "external_qb_adopt_enabled": False,
    "external_qb_trash_source_enabled": False,
    "download_dir": str(Path(os.getenv("POSTPROCESS_DOWNLOAD_DIR", "/media/study3"))),
    "output_dir": str(Path(os.getenv("POSTPROCESS_OUTPUT_DIR", "/media/压制"))),
    "target_codec": "av1",
    "target_encoder": "av1_nvenc",
    "crf": 36,
    "preset": "p1",
    "preset_flag": "-preset",
    "ffmpeg_mode": "standard",
    "ffmpeg_standard_enabled": True,
    "ffmpeg_custom_enabled": False,
    "ffmpeg_custom_template": "",
    "custom_encoding_presets": [],
    "allowed_categories": ["study3"],
    "required_tags": ["moviemuse", "auto-postprocess", "jav"],
    "max_concurrency": 1,
}


TERMINAL_TASK_STATUSES = {"completed", "failed", "ignored", "expired", "conflict"}


POSTPROCESS_TABLE_COLUMNS: dict[str, dict[str, str]] = {
    "media_versions": {
        "av_id": "TEXT NOT NULL DEFAULT ''",
        "path": "TEXT NOT NULL DEFAULT ''",
        "source_type": "TEXT NOT NULL DEFAULT ''",
        "codec": "TEXT NOT NULL DEFAULT ''",
        "has_chinese_subtitle": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'ready'",
        "generated_by": "TEXT NOT NULL DEFAULT 'moviemuse'",
        "file_size": "INTEGER NOT NULL DEFAULT 0",
        "file_hash": "TEXT NOT NULL DEFAULT ''",
        "mtime": "REAL NOT NULL DEFAULT 0",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "activated_at": "REAL NOT NULL DEFAULT 0",
        "trashed_at": "REAL NOT NULL DEFAULT 0",
    },
    "postprocess_tasks": {
        "av_id": "TEXT NOT NULL DEFAULT ''",
        "task_type": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'created'",
        "source_version_id": "TEXT NOT NULL DEFAULT ''",
        "supersede_version_id": "TEXT NOT NULL DEFAULT ''",
        "supersede_path": "TEXT NOT NULL DEFAULT ''",
        "torrent_hash": "TEXT NOT NULL DEFAULT ''",
        "input_path": "TEXT NOT NULL DEFAULT ''",
        "output_path": "TEXT NOT NULL DEFAULT ''",
        "target_codec": "TEXT NOT NULL DEFAULT ''",
        "needs_subtitle": "INTEGER NOT NULL DEFAULT 0",
        "error_code": "TEXT NOT NULL DEFAULT ''",
        "error_message": "TEXT NOT NULL DEFAULT ''",
        "data_json": "TEXT NOT NULL DEFAULT '{}'",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
        "finished_at": "REAL NOT NULL DEFAULT 0",
    },
    "qb_torrents": {
        "task_id": "TEXT NOT NULL DEFAULT ''",
        "av_id": "TEXT NOT NULL DEFAULT ''",
        "category": "TEXT NOT NULL DEFAULT ''",
        "tags": "TEXT NOT NULL DEFAULT ''",
        "save_path": "TEXT NOT NULL DEFAULT ''",
        "content_path": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT ''",
        "progress": "REAL NOT NULL DEFAULT 0",
        "size": "INTEGER NOT NULL DEFAULT 0",
        "state": "TEXT NOT NULL DEFAULT ''",
        "data_json": "TEXT NOT NULL DEFAULT '{}'",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "completed_at": "REAL NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
    "task_events": {
        "task_id": "TEXT NOT NULL DEFAULT ''",
        "level": "TEXT NOT NULL DEFAULT 'info'",
        "stage": "TEXT NOT NULL DEFAULT ''",
        "message": "TEXT NOT NULL DEFAULT ''",
        "data_json": "TEXT NOT NULL DEFAULT '{}'",
        "created_at": "REAL NOT NULL DEFAULT 0",
    },
}


class PostprocessService:
    def __init__(self, data_dir: Path):
        self.db_file = data_dir / "subscriptions.sqlite3"
        self.log_file = data_dir / "system_logs.jsonl"
        self._log_lock = threading.RLock()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS media_versions (
                    id TEXT PRIMARY KEY,
                    av_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    codec TEXT NOT NULL DEFAULT '',
                    has_chinese_subtitle INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ready',
                    generated_by TEXT NOT NULL DEFAULT 'moviemuse',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    file_hash TEXT NOT NULL DEFAULT '',
                    mtime REAL NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    activated_at REAL NOT NULL DEFAULT 0,
                    trashed_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS postprocess_tasks (
                    id TEXT PRIMARY KEY,
                    av_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_version_id TEXT NOT NULL DEFAULT '',
                    supersede_version_id TEXT NOT NULL DEFAULT '',
                    supersede_path TEXT NOT NULL DEFAULT '',
                    torrent_hash TEXT NOT NULL DEFAULT '',
                    input_path TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    target_codec TEXT NOT NULL DEFAULT '',
                    needs_subtitle INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS qb_torrents (
                    torrent_hash TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    av_id TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    save_path TEXT NOT NULL DEFAULT '',
                    content_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0,
                    size INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    completed_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS postprocess_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._migrate_schema(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_versions_av_status ON media_versions(av_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_postprocess_tasks_status ON postprocess_tasks(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_postprocess_tasks_av ON postprocess_tasks(av_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_postprocess_tasks_hash ON postprocess_tasks(torrent_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qb_torrents_task ON qb_torrents(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qb_torrents_av ON qb_torrents(av_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at)")

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        for table, columns in POSTPROCESS_TABLE_COLUMNS.items():
            existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def get_settings(self) -> dict[str, Any]:
        settings = json.loads(json.dumps(DEFAULT_POSTPROCESS_SETTINGS, ensure_ascii=False))
        with self._connect() as conn:
            for row in conn.execute("SELECT key, value FROM postprocess_settings"):
                try:
                    settings[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError:
                    settings[row["key"]] = row["value"]
        return normalize_postprocess_settings(settings)

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_settings()
        for key in DEFAULT_POSTPROCESS_SETTINGS:
            if key in payload:
                current[key] = payload[key]
        current = normalize_postprocess_settings(current)
        now = time.time()
        with self._connect() as conn:
            for key, value in current.items():
                conn.execute(
                    "INSERT OR REPLACE INTO postprocess_settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
        return current

    def active_version(self, av_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM media_versions WHERE av_id = ? AND status = 'active' ORDER BY activated_at DESC LIMIT 1",
                (av_id,),
            ).fetchone()
        return row_to_version(row) if row else None

    def list_versions(self, av_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if av_id:
            where = "WHERE av_id = ?"
            params.append(av_id)
        params.append(max(1, min(500, int(limit or 100))))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM media_versions {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [row_to_version(row) for row in rows]

    def create_task(
        self,
        *,
        av_id: str,
        task_type: str,
        status: str = "created",
        source_version_id: str = "",
        supersede_version_id: str = "",
        supersede_path: str = "",
        target_codec: str = "",
        needs_subtitle: bool = False,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        task_id = uuid.uuid4().hex
        payload = {
            "id": task_id,
            "av_id": av_id,
            "task_type": task_type,
            "status": status,
            "source_version_id": source_version_id,
            "supersede_version_id": supersede_version_id,
            "supersede_path": supersede_path,
            "torrent_hash": "",
            "input_path": "",
            "output_path": "",
            "target_codec": target_codec,
            "needs_subtitle": bool(needs_subtitle),
            "error_code": "",
            "error_message": "",
            "data": data or {},
            "created_at": now,
            "updated_at": now,
            "finished_at": 0.0,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO postprocess_tasks (
                    id, av_id, task_type, status, source_version_id, supersede_version_id, supersede_path,
                    torrent_hash, input_path, output_path, target_codec, needs_subtitle, error_code,
                    error_message, data_json, created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"], payload["av_id"], payload["task_type"], payload["status"],
                    payload["source_version_id"], payload["supersede_version_id"], payload["supersede_path"],
                    payload["torrent_hash"], payload["input_path"], payload["output_path"], payload["target_codec"],
                    1 if payload["needs_subtitle"] else 0, payload["error_code"], payload["error_message"],
                    json.dumps(payload["data"], ensure_ascii=False), payload["created_at"], payload["updated_at"],
                    payload["finished_at"],
                ),
            )
        self.add_event(task_id, "info", "task_created", "后处理任务已创建", {"av_id": av_id, "task_type": task_type})
        return self.get_task(task_id) or payload

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM postprocess_tasks WHERE id = ?", (task_id,)).fetchone()
        return row_to_task(row) if row else None

    def list_tasks(self, *, limit: int = 100, statuses: list[str] | None = None, order: str = "desc") -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where = f"WHERE status IN ({placeholders})"
            params.extend(statuses)
        direction = "ASC" if str(order or "").lower() == "asc" else "DESC"
        params.append(max(1, min(500, int(limit or 100))))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM postprocess_tasks {where} ORDER BY created_at {direction} LIMIT ?",
                params,
            ).fetchall()
        return [row_to_task(row) for row in rows]

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status", "source_version_id", "supersede_version_id", "supersede_path", "torrent_hash",
            "input_path", "output_path", "target_codec", "needs_subtitle", "error_code", "error_message",
            "data",
        }
        updates: dict[str, Any] = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_task(task_id)
        current = self.get_task(task_id)
        if not current:
            return None
        now = time.time()
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            column = "data_json" if key == "data" else key
            assignments.append(f"{column} = ?")
            if key == "data":
                merged = dict(current.get("data") or {})
                if isinstance(value, dict):
                    merged.update(value)
                params.append(json.dumps(merged, ensure_ascii=False))
            elif key == "needs_subtitle":
                params.append(1 if value else 0)
            else:
                params.append(value)
        assignments.append("updated_at = ?")
        params.append(now)
        if updates.get("status") in TERMINAL_TASK_STATUSES:
            assignments.append("finished_at = ?")
            params.append(now)
        elif "status" in updates:
            assignments.append("finished_at = ?")
            params.append(0.0)
        params.append(task_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE postprocess_tasks SET {', '.join(assignments)} WHERE id = ?", params)
        return self.get_task(task_id)

    def claim_task_status(self, task_id: str, expected_statuses: list[str] | set[str] | tuple[str, ...], next_status: str) -> dict[str, Any] | None:
        statuses = [str(item) for item in expected_statuses if str(item or "").strip()]
        if not statuses:
            return None
        placeholders = ",".join("?" for _ in statuses)
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE postprocess_tasks
                SET status = ?, error_code = '', error_message = '', updated_at = ?, finished_at = 0
                WHERE id = ? AND status IN ({placeholders})
                """,
                [next_status, now, task_id, *statuses],
            )
            if cursor.rowcount != 1:
                return None
        return self.get_task(task_id)

    def delete_task(self, task_id: str) -> dict[str, Any] | None:
        current = self.get_task(task_id)
        if not current:
            return None
        with self._connect() as conn:
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM qb_torrents WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM postprocess_tasks WHERE id = ?", (task_id,))
        return current

    def bind_qb_torrent(
        self,
        *,
        task_id: str,
        av_id: str,
        torrent_hash: str,
        category: str,
        tags: str,
        save_path: str,
        status: str = "torrent_pushed",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not torrent_hash:
            return None
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO qb_torrents (
                    torrent_hash, task_id, av_id, category, tags, save_path, content_path,
                    status, progress, size, state, data_json, created_at, completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT content_path FROM qb_torrents WHERE torrent_hash = ?), ''),
                    ?, COALESCE((SELECT progress FROM qb_torrents WHERE torrent_hash = ?), 0),
                    COALESCE((SELECT size FROM qb_torrents WHERE torrent_hash = ?), 0),
                    COALESCE((SELECT state FROM qb_torrents WHERE torrent_hash = ?), ''),
                    ?, COALESCE((SELECT created_at FROM qb_torrents WHERE torrent_hash = ?), ?),
                    COALESCE((SELECT completed_at FROM qb_torrents WHERE torrent_hash = ?), 0), ?)
                """,
                (
                    torrent_hash, task_id, av_id, category, tags, save_path, torrent_hash,
                    status, torrent_hash, torrent_hash, torrent_hash, json.dumps(data or {}, ensure_ascii=False),
                    torrent_hash, now, torrent_hash, now,
                ),
            )
        self.update_task(task_id, torrent_hash=torrent_hash, status=status)
        self.add_event(task_id, "info", "qb_torrent_bound", "qB 种子已绑定到系统任务", {
            "torrent_hash": torrent_hash,
            "category": category,
            "tags": tags,
            "save_path": save_path,
        })
        return self.get_qb_torrent(torrent_hash)

    def get_qb_torrent(self, torrent_hash: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM qb_torrents WHERE torrent_hash = ?", (torrent_hash,)).fetchone()
        return row_to_qb_torrent(row) if row else None

    def list_qb_torrents(self, *, statuses: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where = f"WHERE status IN ({placeholders})"
            params.extend(statuses)
        params.append(max(1, min(1000, int(limit or 200))))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM qb_torrents {where} ORDER BY created_at ASC LIMIT ?",
                params,
            ).fetchall()
        return [row_to_qb_torrent(row) for row in rows]

    def update_qb_torrent(self, torrent_hash: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"category", "tags", "save_path", "content_path", "status", "progress", "size", "state", "completed_at", "data"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_qb_torrent(torrent_hash)
        current = self.get_qb_torrent(torrent_hash)
        if not current:
            return None
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            column = "data_json" if key == "data" else key
            assignments.append(f"{column} = ?")
            if key == "data":
                merged = dict(current.get("data") or {})
                if isinstance(value, dict):
                    merged.update(value)
                params.append(json.dumps(merged, ensure_ascii=False))
            else:
                params.append(value)
        assignments.append("updated_at = ?")
        params.append(time.time())
        params.append(torrent_hash)
        with self._connect() as conn:
            conn.execute(f"UPDATE qb_torrents SET {', '.join(assignments)} WHERE torrent_hash = ?", params)
        return self.get_qb_torrent(torrent_hash)

    def add_version(
        self,
        *,
        av_id: str,
        path: str,
        source_type: str,
        codec: str = "",
        has_chinese_subtitle: bool = False,
        status: str = "ready",
        generated_by: str = "moviemuse",
        file_size: int = 0,
        file_hash: str = "",
        mtime: float = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        version_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO media_versions (
                    id, av_id, path, source_type, codec, has_chinese_subtitle, status, generated_by,
                    file_size, file_hash, mtime, metadata_json, created_at, activated_at, trashed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id, av_id, path, source_type, codec, 1 if has_chinese_subtitle else 0, status,
                    generated_by, int(file_size or 0), file_hash, float(mtime or 0),
                    json.dumps(metadata or {}, ensure_ascii=False), now, now if status == "active" else 0, 0,
                ),
            )
        return self.get_version(version_id) or {"id": version_id, "av_id": av_id, "path": path, "status": status}

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM media_versions WHERE id = ?", (version_id,)).fetchone()
        return row_to_version(row) if row else None

    def update_version(self, version_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "path", "source_type", "codec", "has_chinese_subtitle", "status", "generated_by",
            "file_size", "file_hash", "mtime", "metadata", "activated_at", "trashed_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_version(version_id)
        current = self.get_version(version_id)
        if not current:
            return None
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            column = "metadata_json" if key == "metadata" else key
            assignments.append(f"{column} = ?")
            if key == "metadata":
                merged = dict(current.get("metadata") or {})
                if isinstance(value, dict):
                    merged.update(value)
                params.append(json.dumps(merged, ensure_ascii=False))
            elif key == "has_chinese_subtitle":
                params.append(1 if value else 0)
            else:
                params.append(value)
        params.append(version_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE media_versions SET {', '.join(assignments)} WHERE id = ?", params)
        return self.get_version(version_id)

    def activate_version(
        self,
        version_id: str,
        *,
        supersede_version_id: str = "",
        trashed_path: str = "",
        trash_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            new_row = conn.execute("SELECT * FROM media_versions WHERE id = ?", (version_id,)).fetchone()
            if not new_row:
                return {"status": "missing", "message": "新版本不存在"}
            av_id = str(new_row["av_id"] or "")
            active_row = conn.execute(
                "SELECT * FROM media_versions WHERE av_id = ? AND status = 'active' ORDER BY activated_at DESC LIMIT 1",
                (av_id,),
            ).fetchone()
            if supersede_version_id:
                if not active_row:
                    return {"status": "conflict", "message": "创建洗版时绑定了旧版本，但当前没有 active version"}
                if str(active_row["id"]) != supersede_version_id:
                    return {
                        "status": "conflict",
                        "message": "active version 已变化，拒绝替换旧版本",
                        "current_active_version_id": str(active_row["id"]),
                        "expected_active_version_id": supersede_version_id,
                    }
            elif active_row and str(active_row["id"]) != version_id:
                return {
                    "status": "conflict",
                    "message": "当前已存在 active version，未绑定 supersede_version_id，拒绝覆盖",
                    "current_active_version_id": str(active_row["id"]),
                    "new_version_id": version_id,
                }
            metadata = row_json(new_row, "metadata_json")
            metadata["activated_by"] = "postprocess"
            if trash_result:
                metadata["trash_result"] = trash_result
            conn.execute(
                "UPDATE media_versions SET status = 'superseded' WHERE av_id = ? AND status = 'active'",
                (av_id,),
            )
            conn.execute(
                "UPDATE media_versions SET status = 'active', activated_at = ?, metadata_json = ? WHERE id = ?",
                (now, json.dumps(metadata, ensure_ascii=False), version_id),
            )
            if supersede_version_id and trashed_path:
                old_metadata = row_json(active_row, "metadata_json") if active_row else {}
                old_metadata["trashed_path"] = trashed_path
                if trash_result:
                    old_metadata["trash_result"] = trash_result
                conn.execute(
                    "UPDATE media_versions SET status = 'trashed', trashed_at = ?, metadata_json = ? WHERE id = ?",
                    (now, json.dumps(old_metadata, ensure_ascii=False), supersede_version_id),
                )
        return {"status": "activated", "version": self.get_version(version_id), "active_version_id": version_id}

    def add_event(self, task_id: str, level: str, stage: str, message: str, data: dict[str, Any] | None = None) -> None:
        now = time.time()
        payload = data or {}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, level, stage, message, data_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, level, stage, message, json.dumps(payload, ensure_ascii=False), now),
            )
        self._mirror_event_to_system_log(task_id, level, stage, message, payload, now)

    def _mirror_event_to_system_log(
        self,
        task_id: str,
        level: str,
        stage: str,
        message: str,
        data: dict[str, Any],
        ts: float,
    ) -> None:
        entry_data = {"task_id": task_id, "stage": stage, **(data or {})}
        entry = {
            "ts": ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "level": level,
            "source": "postprocess",
            "message": message,
            "data": entry_data,
        }
        try:
            with self._log_lock:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def list_events(self, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
                (task_id, max(1, min(1000, int(limit or 200)))),
            ).fetchall()
        return [row_to_event(row) for row in rows]


def normalize_postprocess_settings(raw: dict[str, Any]) -> dict[str, Any]:
    result = dict(DEFAULT_POSTPROCESS_SETTINGS)
    result.update(raw if isinstance(raw, dict) else {})
    result["auto_transcode_enabled"] = bool(result.get("auto_transcode_enabled"))
    result["auto_subtitle_enabled"] = bool(result.get("auto_subtitle_enabled"))
    result["worker_auto_run"] = bool(result.get("worker_auto_run"))
    result["external_qb_adopt_enabled"] = bool(result.get("external_qb_adopt_enabled"))
    result["external_qb_trash_source_enabled"] = bool(result.get("external_qb_trash_source_enabled"))
    result["ffmpeg_mode"] = str(result.get("ffmpeg_mode") or "").strip().lower()
    if result["ffmpeg_mode"] not in {"standard", "custom"}:
        result["ffmpeg_mode"] = "custom" if bool(result.get("ffmpeg_custom_enabled")) else "standard"
    result["ffmpeg_standard_enabled"] = result["ffmpeg_mode"] == "standard"
    result["ffmpeg_custom_enabled"] = result["ffmpeg_mode"] == "custom"
    result["target_codec"] = str(result.get("target_codec") or "av1").lower()
    if result["target_codec"] not in {"h265", "av1"}:
        result["target_codec"] = "av1"
    for key in ("download_dir", "output_dir", "target_encoder", "preset", "preset_flag", "ffmpeg_custom_template"):
        result[key] = str(result.get(key) or DEFAULT_POSTPROCESS_SETTINGS[key]).strip()
    result["download_dir"] = normalize_container_media_path(result["download_dir"], DEFAULT_POSTPROCESS_SETTINGS["download_dir"])
    result["output_dir"] = normalize_container_media_path(result["output_dir"], DEFAULT_POSTPROCESS_SETTINGS["output_dir"])
    if result["preset_flag"] not in {"-preset", "-cpu-used"}:
        result["preset_flag"] = "-preset"
    for key, fallback, low, high in (("crf", 36, 12, 51), ("max_concurrency", 1, 1, 8)):
        try:
            number = int(result.get(key) or fallback)
        except (TypeError, ValueError):
            number = fallback
        result[key] = max(low, min(high, number))
    result["allowed_categories"] = normalize_string_list(result.get("allowed_categories"))
    result["required_tags"] = normalize_string_list(result.get("required_tags"))
    result["custom_encoding_presets"] = normalize_encoding_presets(result.get("custom_encoding_presets"))
    return result


def normalize_container_media_path(value: str, fallback: str) -> str:
    text = str(value or fallback or "").strip().replace("\\", "/")
    if not text:
        return str(fallback)
    if text.startswith("//") or re.match(r"^[A-Za-z]:/", text):
        return text
    if text.startswith("/"):
        return text.rstrip("/") or "/"
    return f"/media/{text.strip('/')}"


def normalize_encoding_presets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    presets: list[dict[str, Any]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        encoder = str(item.get("encoder") or "").strip()
        if not name or not encoder:
            continue
        codec = str(item.get("codec") or "av1").strip().lower()
        if codec not in {"av1", "h265"}:
            codec = "av1"
        preset_flag = str(item.get("preset_flag") or "-preset").strip()
        if preset_flag not in {"-preset", "-cpu-used"}:
            preset_flag = "-preset"
        try:
            quality = max(12, min(51, int(item.get("quality") or item.get("crf") or 36)))
        except (TypeError, ValueError):
            quality = 36
        presets.append(
            {
                "name": name,
                "codec": codec,
                "encoder": encoder,
                "preset": str(item.get("preset") or "p1").strip(),
                "preset_flag": preset_flag,
                "quality": quality,
            }
        )
    return presets


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = value.replace("\n", ",").split(",")
    elif isinstance(value, list):
        parts = value
    else:
        parts = []
    result: list[str] = []
    seen: set[str] = set()
    for item in parts:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def row_json(row: sqlite3.Row, key: str) -> dict[str, Any]:
    try:
        value = json.loads(row[key] or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "av_id": row["av_id"],
        "task_type": row["task_type"],
        "status": row["status"],
        "source_version_id": row["source_version_id"],
        "supersede_version_id": row["supersede_version_id"],
        "supersede_path": row["supersede_path"],
        "torrent_hash": row["torrent_hash"],
        "input_path": row["input_path"],
        "output_path": row["output_path"],
        "target_codec": row["target_codec"],
        "needs_subtitle": bool(row["needs_subtitle"]),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "data": row_json(row, "data_json"),
        "created_at": float(row["created_at"] or 0),
        "updated_at": float(row["updated_at"] or 0),
        "finished_at": float(row["finished_at"] or 0),
    }


def row_to_qb_torrent(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "torrent_hash": row["torrent_hash"],
        "task_id": row["task_id"],
        "av_id": row["av_id"],
        "category": row["category"],
        "tags": row["tags"],
        "save_path": row["save_path"],
        "content_path": row["content_path"],
        "status": row["status"],
        "progress": float(row["progress"] or 0),
        "size": int(row["size"] or 0),
        "state": row["state"],
        "data": row_json(row, "data_json"),
        "created_at": float(row["created_at"] or 0),
        "completed_at": float(row["completed_at"] or 0),
        "updated_at": float(row["updated_at"] or 0),
    }


def row_to_version(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "av_id": row["av_id"],
        "path": row["path"],
        "source_type": row["source_type"],
        "codec": row["codec"],
        "has_chinese_subtitle": bool(row["has_chinese_subtitle"]),
        "status": row["status"],
        "generated_by": row["generated_by"],
        "file_size": int(row["file_size"] or 0),
        "file_hash": row["file_hash"],
        "mtime": float(row["mtime"] or 0),
        "metadata": row_json(row, "metadata_json"),
        "created_at": float(row["created_at"] or 0),
        "activated_at": float(row["activated_at"] or 0),
        "trashed_at": float(row["trashed_at"] or 0),
    }


def row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "level": row["level"],
        "stage": row["stage"],
        "message": row["message"],
        "data": row_json(row, "data_json"),
        "created_at": float(row["created_at"] or 0),
    }
