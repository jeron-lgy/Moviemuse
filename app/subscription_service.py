"""订阅服务 - 纯 javdb 数据源"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus
from urllib.parse import urlparse

DEFAULT_ACTRESS_CRON = "0 21 * * *"
DEFAULT_AV_CRON = "0 22 * * *"
DEFAULT_WASH_CRON = "0 22 * * *"
DEFAULT_POSTPROCESS_CRON = "*/5 * * * *"
DEFAULT_MAKER_CRON = "0 */6 * * *"
DEFAULT_RANKING_CRON = "30 4 */2 * *"
DEFAULT_ASSET_CRON = "15 3 * * *"
DEFAULT_MAX_COACTORS = 2
DEFAULT_WASH_EXPIRE_DAYS = 90
DEFAULT_WASH_SETTINGS = {
    "enabled": True,
    "expire_days": DEFAULT_WASH_EXPIRE_DAYS,
    "auto_cancel_expired": True,
    "check_chinese": True,
    "check_4k": True,
    "prefer_chinese": True,
    "prefer_4k": True,
    "min_seeders": 1,
    "max_size_gb": 80,
}
DEFAULT_PINNED_MAKERS = [
    {"name": "S1 NO.1 STYLE", "url": "https://javdb.com/makers/7R?f=download", "preferred_listing_source": "javlibrary"},
    {"name": "PRESTIGE", "url": "https://javdb.com/makers/6M?f=download", "preferred_listing_source": "javlibrary"},
    {"name": "IDEA POCKET", "url": "https://javdb.com/makers/ZXX?f=download", "preferred_listing_source": "javlibrary"},
    {"name": "Madonna", "url": "https://javdb.com/makers/zKW?f=download", "preferred_listing_source": "javlibrary"},
    {"name": "SOD Create", "url": "https://javdb.com/makers/q6?f=download", "preferred_listing_source": "javlibrary"},
]
DEFAULT_PINNED_MAKER_URLS = {item["name"].lower(): item["url"] for item in DEFAULT_PINNED_MAKERS}
DEFAULT_PINNED_MAKER_SOURCES = {item["name"].lower(): item.get("preferred_listing_source", "auto") for item in DEFAULT_PINNED_MAKERS}


class SubscriptionService:
    """订阅管理 - 数据存储"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.sub_file = data_dir / "subscriptions.json"
        self.db_file = data_dir / "subscriptions.sqlite3"
        self._lock = threading.RLock()
        self._ensure_dir()
        self._init_db()
        self.data = self._load()

    def _ensure_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

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
                CREATE TABLE IF NOT EXISTS subscription_av (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscription_actress (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscription_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscription_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscription_metadata_cache (
                    namespace TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (namespace, cache_key)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subscription_metadata_cache_expires
                ON subscription_metadata_cache (expires_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscription_asset_cache (
                    asset_key TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    immutable INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subscription_asset_cache_entity
                ON subscription_asset_cache (entity_id, kind)
                """
            )

    def _load(self) -> dict[str, Any]:
        data: dict[str, Any] = {"av": {}, "actress": {}, "settings": {}}
        loaded_from_sqlite = False
        try:
            with self._connect() as conn:
                for row in conn.execute("SELECT id, data FROM subscription_av"):
                    item = json.loads(row["data"])
                    if isinstance(item, dict):
                        data["av"][row["id"]] = item
                for row in conn.execute("SELECT id, data FROM subscription_actress"):
                    item = json.loads(row["data"])
                    if isinstance(item, dict):
                        data["actress"][row["id"]] = item
                for row in conn.execute("SELECT key, value FROM subscription_settings"):
                    data["settings"][row["key"]] = json.loads(row["value"])
                loaded_from_sqlite = bool(data["av"] or data["actress"] or data["settings"])
        except Exception:
            loaded_from_sqlite = False

        if not loaded_from_sqlite and self.sub_file.exists():
            try:
                loaded = json.loads(self.sub_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                pass
        data.setdefault("av", {})
        data.setdefault("actress", {})
        data.setdefault("settings", {})
        data["settings"].setdefault("actress_cron", DEFAULT_ACTRESS_CRON)
        data["settings"].setdefault("av_cron", DEFAULT_AV_CRON)
        data["settings"].setdefault("wash_cron", DEFAULT_WASH_CRON)
        data["settings"].setdefault("postprocess_cron", DEFAULT_POSTPROCESS_CRON)
        data["settings"].setdefault("maker_cron", DEFAULT_MAKER_CRON)
        data["settings"].setdefault("ranking_cron", DEFAULT_RANKING_CRON)
        data["settings"].setdefault("asset_cron", DEFAULT_ASSET_CRON)
        data["settings"].setdefault("asset_cache_max_mb", 2048)
        data["settings"].setdefault("max_coactors", DEFAULT_MAX_COACTORS)
        data["settings"].setdefault("javdb_source_enabled", False)
        data["settings"].setdefault("poll_enabled", True)
        data["settings"].setdefault("last_poll_at", 0)
        data["settings"].setdefault("last_poll_minute", "")
        data["settings"].setdefault("last_av_poll_at", 0)
        data["settings"].setdefault("last_av_poll_minute", "")
        data["settings"].setdefault("last_wash_poll_at", 0)
        data["settings"].setdefault("last_wash_poll_minute", "")
        data["settings"].setdefault("last_postprocess_poll_at", 0)
        data["settings"].setdefault("last_postprocess_poll_minute", "")
        data["settings"].setdefault("last_maker_poll_at", 0)
        data["settings"].setdefault("last_maker_poll_minute", "")
        data["settings"].setdefault("last_ranking_poll_at", 0)
        data["settings"].setdefault("last_ranking_poll_minute", "")
        data["settings"].setdefault("last_asset_poll_at", 0)
        data["settings"].setdefault("last_asset_poll_minute", "")
        data["settings"].setdefault("last_task_results", {})
        data["settings"]["wash"] = normalize_wash_settings(data["settings"].get("wash", {}))
        data["settings"]["pinned_makers"] = normalize_pinned_makers(data["settings"].get("pinned_makers"))
        for item in data.get("av", {}).values():
            if not isinstance(item, dict):
                continue
            item["filters"] = normalize_filters(item.get("filters", {}))
            item.setdefault("subscription_mode", "strict")
            item["wash"] = normalize_wash_request(item.get("wash", {}))
            if item.get("status", "pending") == "pending" and item.get("download_status") in {"ok", "exists", "sent"}:
                item["status"] = "done"
        for item in data.get("actress", {}).values():
            if not isinstance(item, dict):
                continue
            item.setdefault("include_vr", False)
        if not loaded_from_sqlite:
            self.data = data
            self._save()
        return data

    def _save(self) -> None:
        with self._lock:
            now = time.time()
            with self._connect() as conn:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM subscription_av")
                conn.execute("DELETE FROM subscription_actress")
                conn.execute("DELETE FROM subscription_settings")
                for av_id, item in self.data.get("av", {}).items():
                    conn.execute(
                        "INSERT OR REPLACE INTO subscription_av (id, data, updated_at) VALUES (?, ?, ?)",
                        (str(av_id), json.dumps(item, ensure_ascii=False), now),
                    )
                for actress_id, item in self.data.get("actress", {}).items():
                    conn.execute(
                        "INSERT OR REPLACE INTO subscription_actress (id, data, updated_at) VALUES (?, ?, ?)",
                        (str(actress_id), json.dumps(item, ensure_ascii=False), now),
                    )
                for key, value in self.data.get("settings", {}).items():
                    conn.execute(
                        "INSERT OR REPLACE INTO subscription_settings (key, value, updated_at) VALUES (?, ?, ?)",
                        (str(key), json.dumps(value, ensure_ascii=False), now),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO subscription_meta (key, value, updated_at) VALUES (?, ?, ?)",
                    ("storage_version", json.dumps(1), now),
                )
                conn.commit()

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.data.get("settings", {}))

    def get_metadata_cache(self, namespace: str, cache_key: str, *, allow_stale: bool = False) -> Any | None:
        ns = str(namespace or "").strip()
        key = str(cache_key or "").strip()
        if not ns or not key:
            return None
        now = time.time()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT data, expires_at FROM subscription_metadata_cache WHERE namespace = ? AND cache_key = ?",
                    (ns, key),
                ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        try:
            expires_at = float(row["expires_at"] or 0)
            if expires_at <= now and not allow_stale:
                return None
            return json.loads(row["data"])
        except Exception:
            return None

    def set_metadata_cache(self, namespace: str, cache_key: str, value: Any, ttl_seconds: int) -> None:
        ns = str(namespace or "").strip()
        key = str(cache_key or "").strip()
        if not ns or not key or value in (None, "", [], {}):
            return
        now = time.time()
        ttl = max(60, int(ttl_seconds or 60))
        try:
            payload = json.dumps(value, ensure_ascii=False)
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO subscription_metadata_cache
                    (namespace, cache_key, data, updated_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ns, key, payload, now, now + ttl),
                )
        except Exception:
            return

    def delete_metadata_cache(self, namespace: str, cache_key: str) -> bool:
        ns = str(namespace or "").strip()
        key = str(cache_key or "").strip()
        if not ns or not key:
            return False
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM subscription_metadata_cache WHERE namespace = ? AND cache_key = ?",
                    (ns, key),
                )
                return bool(cur.rowcount)
        except Exception:
            return False

    def delete_expired_metadata_cache(self) -> int:
        now = time.time()
        try:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM subscription_metadata_cache WHERE expires_at <= ?", (now,))
                return int(cur.rowcount or 0)
        except Exception:
            return 0

    def metadata_cache_stats(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                total = conn.execute("SELECT COUNT(*) AS count FROM subscription_metadata_cache").fetchone()["count"]
                expired = conn.execute(
                    "SELECT COUNT(*) AS count FROM subscription_metadata_cache WHERE expires_at <= ?",
                    (time.time(),),
                ).fetchone()["count"]
                rows = conn.execute(
                    "SELECT namespace, COUNT(*) AS count FROM subscription_metadata_cache GROUP BY namespace ORDER BY namespace"
                ).fetchall()
            return {
                "total": int(total or 0),
                "expired": int(expired or 0),
                "namespaces": {str(row["namespace"]): int(row["count"] or 0) for row in rows},
            }
        except Exception:
            return {"total": 0, "expired": 0, "namespaces": {}}

    @staticmethod
    def asset_cache_key(entity_id: str, kind: str) -> str:
        return f"{str(kind or '').strip().lower()}:{str(entity_id or '').strip().upper()}"

    def get_asset_cache(self, entity_id: str, kind: str = "cover") -> dict[str, Any] | None:
        asset_key = self.asset_cache_key(entity_id, kind)
        if not asset_key or asset_key == ":":
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT asset_key, entity_id, kind, source_url, local_path, media_type,
                           sha256, bytes, immutable, updated_at
                    FROM subscription_asset_cache
                    WHERE asset_key = ?
                    """,
                    (asset_key,),
                ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        return {
            "asset_key": str(row["asset_key"]),
            "entity_id": str(row["entity_id"]),
            "kind": str(row["kind"]),
            "source_url": str(row["source_url"]),
            "local_path": str(row["local_path"]),
            "media_type": str(row["media_type"]),
            "sha256": str(row["sha256"]),
            "bytes": int(row["bytes"] or 0),
            "immutable": bool(row["immutable"]),
            "updated_at": float(row["updated_at"] or 0),
        }

    def set_asset_cache(
        self,
        entity_id: str,
        kind: str,
        source_url: str,
        local_path: str,
        media_type: str,
        sha256: str,
        bytes_count: int,
        *,
        immutable: bool = False,
    ) -> dict[str, Any]:
        entity = str(entity_id or "").strip().upper()
        safe_kind = str(kind or "cover").strip().lower()
        asset_key = self.asset_cache_key(entity, safe_kind)
        if not entity or not safe_kind or not local_path:
            return {}
        now = time.time()
        payload = {
            "asset_key": asset_key,
            "entity_id": entity,
            "kind": safe_kind,
            "source_url": str(source_url or "").strip(),
            "local_path": str(local_path or "").strip(),
            "media_type": str(media_type or "image/jpeg").strip(),
            "sha256": str(sha256 or "").strip(),
            "bytes": max(0, int(bytes_count or 0)),
            "immutable": bool(immutable),
            "updated_at": now,
        }
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO subscription_asset_cache
                    (asset_key, entity_id, kind, source_url, local_path, media_type,
                     sha256, bytes, immutable, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["asset_key"],
                        payload["entity_id"],
                        payload["kind"],
                        payload["source_url"],
                        payload["local_path"],
                        payload["media_type"],
                        payload["sha256"],
                        payload["bytes"],
                        1 if payload["immutable"] else 0,
                        payload["updated_at"],
                    ),
                )
        except Exception:
            return {}
        return payload

    def set_asset_immutable(self, entity_id: str, kind: str = "cover", immutable: bool = True) -> bool:
        asset_key = self.asset_cache_key(entity_id, kind)
        if not asset_key or asset_key == ":":
            return False
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE subscription_asset_cache SET immutable = ?, updated_at = ? WHERE asset_key = ?",
                    (1 if immutable else 0, time.time(), asset_key),
                )
                return bool(cur.rowcount)
        except Exception:
            return False

    def list_metadata_cache(self, namespace: str, limit: int = 10000) -> list[dict[str, Any]]:
        ns = str(namespace or "").strip()
        if not ns:
            return []
        safe_limit = max(1, min(100000, int(limit or 10000)))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT namespace, cache_key, data, updated_at, expires_at
                    FROM subscription_metadata_cache
                    WHERE namespace = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (ns, safe_limit),
                ).fetchall()
        except Exception:
            return []
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except Exception:
                continue
            result.append({
                "namespace": str(row["namespace"]),
                "cache_key": str(row["cache_key"]),
                "data": data,
                "updated_at": float(row["updated_at"] or 0),
                "expires_at": float(row["expires_at"] or 0),
            })
        return result

    def cleanup_asset_cache(self, max_bytes: int | None = None) -> dict[str, Any]:
        deleted = 0
        deleted_bytes = 0
        removed_missing = 0
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT asset_key, local_path, bytes, immutable, updated_at
                    FROM subscription_asset_cache
                    ORDER BY immutable ASC, updated_at ASC
                    """
                ).fetchall()
                total_bytes = int(conn.execute("SELECT COALESCE(SUM(bytes), 0) AS bytes FROM subscription_asset_cache").fetchone()["bytes"] or 0)
                for row in rows:
                    path = (self.data_dir / str(row["local_path"] or "")).resolve()
                    if not str(row["local_path"] or "") or not path.exists() or not path.is_file():
                        conn.execute("DELETE FROM subscription_asset_cache WHERE asset_key = ?", (row["asset_key"],))
                        removed_missing += 1
                if max_bytes is not None:
                    target = max(0, int(max_bytes or 0))
                    for row in rows:
                        if total_bytes <= target:
                            break
                        if int(row["immutable"] or 0):
                            continue
                        asset_key = str(row["asset_key"] or "")
                        path = (self.data_dir / str(row["local_path"] or "")).resolve()
                        size = int(row["bytes"] or 0)
                        try:
                            if path.exists() and path.is_file():
                                path.unlink()
                        except Exception:
                            pass
                        conn.execute("DELETE FROM subscription_asset_cache WHERE asset_key = ?", (asset_key,))
                        deleted += 1
                        deleted_bytes += size
                        total_bytes -= size
            return {"deleted": deleted, "deleted_bytes": deleted_bytes, "removed_missing": removed_missing}
        except Exception:
            return {"deleted": deleted, "deleted_bytes": deleted_bytes, "removed_missing": removed_missing}

    def asset_cache_stats(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                total = conn.execute("SELECT COUNT(*) AS count FROM subscription_asset_cache").fetchone()["count"]
                bytes_total = conn.execute("SELECT COALESCE(SUM(bytes), 0) AS bytes FROM subscription_asset_cache").fetchone()["bytes"]
                rows = conn.execute(
                    "SELECT kind, COUNT(*) AS count, COALESCE(SUM(bytes), 0) AS bytes FROM subscription_asset_cache GROUP BY kind ORDER BY kind"
                ).fetchall()
            return {
                "total": int(total or 0),
                "bytes": int(bytes_total or 0),
                "kinds": {
                    str(row["kind"]): {"count": int(row["count"] or 0), "bytes": int(row["bytes"] or 0)}
                    for row in rows
                },
            }
        except Exception:
            return {"total": 0, "bytes": 0, "kinds": {}}

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            settings = self.data.setdefault("settings", {})
            if "poll_enabled" in payload:
                settings["poll_enabled"] = bool(payload.get("poll_enabled"))
            if "actress_cron" in payload:
                settings["actress_cron"] = normalize_cron(payload.get("actress_cron"), DEFAULT_ACTRESS_CRON)
            if "av_cron" in payload:
                settings["av_cron"] = normalize_cron(payload.get("av_cron"), DEFAULT_AV_CRON)
            if "wash_cron" in payload:
                settings["wash_cron"] = normalize_cron(payload.get("wash_cron"), DEFAULT_WASH_CRON)
            if "postprocess_cron" in payload:
                settings["postprocess_cron"] = normalize_cron(payload.get("postprocess_cron"), DEFAULT_POSTPROCESS_CRON)
            if "maker_cron" in payload:
                settings["maker_cron"] = normalize_cron(payload.get("maker_cron"), DEFAULT_MAKER_CRON)
            if "ranking_cron" in payload:
                settings["ranking_cron"] = normalize_cron(payload.get("ranking_cron"), DEFAULT_RANKING_CRON)
            if "asset_cron" in payload:
                settings["asset_cron"] = normalize_cron(payload.get("asset_cron"), DEFAULT_ASSET_CRON)
            if "asset_cache_max_mb" in payload:
                try:
                    settings["asset_cache_max_mb"] = max(0, int(float(payload.get("asset_cache_max_mb") or 0)))
                except (TypeError, ValueError):
                    settings["asset_cache_max_mb"] = 2048
            if "pinned_makers" in payload:
                settings["pinned_makers"] = normalize_pinned_makers(payload.get("pinned_makers"))
            if "max_coactors" in payload:
                try:
                    count = int(payload.get("max_coactors") or DEFAULT_MAX_COACTORS)
                except (TypeError, ValueError):
                    count = DEFAULT_MAX_COACTORS
                settings["max_coactors"] = max(1, min(DEFAULT_MAX_COACTORS, count))
            if "javdb_source_enabled" in payload:
                settings["javdb_source_enabled"] = bool(payload.get("javdb_source_enabled"))
            if "wash" in payload:
                settings["wash"] = normalize_wash_settings(payload.get("wash"))
            self._save()
            return dict(settings)

    # ========== 番号订阅 ==========

    def subscribe_av(self, av: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            av_id = av.get("id", "")
            if not av_id:
                return {}
            existing = self.data["av"].get(av_id, {})
            self.data["av"][av_id] = {
                "id": av_id,
                "title": av.get("title", existing.get("title", "")),
                "cover": av.get("cover", av.get("cover_url", existing.get("cover", ""))),
                "date": av.get("date", av.get("release_date", existing.get("date", ""))),
                "actresses": av.get("actresses", av.get("actress", existing.get("actresses", []))),
                "url": av.get("url", existing.get("url", "")),
                "source": av.get("source", existing.get("source", "")),
                "source_chain": av.get("source_chain", existing.get("source_chain", [])),
                "source_scope": av.get("source_scope", existing.get("source_scope", "")),
                "match_reason": av.get("match_reason", existing.get("match_reason", "")),
                "confidence": av.get("confidence", existing.get("confidence", "")),
                "maker": av.get("maker", existing.get("maker", "")),
                "label": av.get("label", existing.get("label", "")),
                "status": av.get("status", existing.get("status", "pending")),
                "library_status": av.get("library_status", existing.get("library_status", "")),
                "jellyfin_item_id": av.get("jellyfin_item_id", existing.get("jellyfin_item_id", "")),
                "jellyfin_item_name": av.get("jellyfin_item_name", existing.get("jellyfin_item_name", "")),
                "jellyfin_path": av.get("jellyfin_path", existing.get("jellyfin_path", "")),
                "download_status": av.get("download_status", existing.get("download_status", "")),
                "download_message": av.get("download_message", existing.get("download_message", "")),
                "mteam_torrent_id": av.get("mteam_torrent_id", existing.get("mteam_torrent_id", "")),
                "mteam_torrent_title": av.get("mteam_torrent_title", existing.get("mteam_torrent_title", "")),
                "filters": normalize_filters(av.get("filters", existing.get("filters", {}))),
                "subscription_mode": str(av.get("subscription_mode", existing.get("subscription_mode", "strict")) or "strict"),
                "wash": normalize_wash_request(av.get("wash", existing.get("wash", {}))),
                "detail": av.get("detail", existing.get("detail", {})),
                "downloaded_at": av.get("downloaded_at", existing.get("downloaded_at", 0)),
                "subscribed_at": existing.get("subscribed_at", time.time()),
                "auto_subscribed": bool(av.get("auto_subscribed", existing.get("auto_subscribed", False))),
                "source_actress_id": av.get("source_actress_id", existing.get("source_actress_id", "")),
                "source_actress_name": av.get("source_actress_name", existing.get("source_actress_name", "")),
            }
            self._save()
            return dict(self.data["av"][av_id])

    def update_av_wash(self, av_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            item = self.data["av"].get(av_id)
            if not item:
                return None
            current = normalize_wash_request(item.get("wash", {}))
            mode = str(payload.get("mode") or current.get("mode") or "").strip().lower()
            status = str(payload.get("status") or current.get("status") or "requested").strip().lower()
            if mode not in {"chinese", "4k"}:
                return None
            if status not in {"requested", "downloading", "completed", "expired", "cancelled", "error"}:
                status = "requested"
            now = time.time()
            requested_at = payload.get("requested_at")
            if requested_at is None:
                requested_at = now if mode != current.get("mode") else (current.get("requested_at") or now)
            merged = {
                **current,
                "mode": mode,
                "status": status,
                "requested_at": float(requested_at or now),
                "updated_at": now,
                "completed_at": now if status == "completed" else current.get("completed_at", 0),
            }
            for key in (
                "download_status",
                "download_message",
                "mteam_torrent_id",
                "mteam_torrent_title",
                "qb_hash",
                "old_path",
                "new_path",
                "new_jellyfin_item_id",
                "new_jellyfin_item_name",
                "trash_path",
                "replace_status",
                "replace_message",
                "last_checked_at",
                "task_id",
            ):
                if key in payload:
                    merged[key] = payload[key]
            item["wash"] = normalize_wash_request(merged)
            self._save()
            return dict(item)

    def expire_wash_requests(self) -> int:
        with self._lock:
            wash_settings = normalize_wash_settings(self.data.get("settings", {}).get("wash", {}))
            if not wash_settings.get("auto_cancel_expired", True):
                return 0
            expire_days = int(wash_settings.get("expire_days") or DEFAULT_WASH_EXPIRE_DAYS)
            cutoff = time.time() - max(1, expire_days) * 86400
            changed = 0
            for item in self.data.get("av", {}).values():
                if not isinstance(item, dict):
                    continue
                wash = normalize_wash_request(item.get("wash", {}))
                if wash.get("status") in {"requested", "downloading", "error"} and float(wash.get("requested_at") or 0) < cutoff:
                    wash["status"] = "expired"
                    wash["updated_at"] = time.time()
                    item["wash"] = wash
                    changed += 1
            if changed:
                self._save()
            return changed

    def unsubscribe_av(self, av_id: str) -> bool:
        with self._lock:
            if av_id in self.data["av"]:
                del self.data["av"][av_id]
                self._save()
                return True
            return False

    def update_av_status(self, av_id: str, status: str) -> bool:
        with self._lock:
            if av_id in self.data["av"]:
                self.data["av"][av_id]["status"] = status
                self._save()
                return True
            return False

    def update_av_download(self, av_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            item = self.data["av"].get(av_id)
            if not item:
                return None
            for key in (
                "status",
                "library_status",
                "jellyfin_item_id",
                "jellyfin_item_name",
                "jellyfin_path",
                "download_status",
                "download_message",
                "mteam_torrent_id",
                "mteam_torrent_title",
                "qb_hash",
                "downloaded_at",
                "detail",
            ):
                if key in payload:
                    item[key] = payload[key]
            self._save()
            return dict(item)

    def get_subscribed_av(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                self.data["av"].values(),
                key=lambda item: float(item.get("subscribed_at") or 0),
                reverse=True,
            )

    def is_av_subscribed(self, av_id: str) -> bool:
        with self._lock:
            return av_id in self.data["av"]

    # ========== 女优订阅 ==========

    def subscribe_actress(self, actress: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            aid = str(actress.get("id") or "").strip()
            if not aid:
                return {}
            duplicate_id = self._find_actress_duplicate_id(actress, aid)
            if duplicate_id and duplicate_id != aid:
                merged = dict(self.data["actress"].get(duplicate_id, {}))
                merged.update({key: value for key, value in actress.items() if value not in ("", None)})
                merged["id"] = aid
                self.data["actress"].pop(duplicate_id, None)
                actress = merged
            existing = self.data["actress"].get(aid, {})
            since_date = normalize_date(actress.get("since_date") or existing.get("since_date") or str(date.today()))
            self.data["actress"][aid] = {
                "id": aid,
                "name": actress.get("name", existing.get("name", "")),
                "cover": actress.get("cover", actress.get("cover_url", existing.get("cover", ""))),
                "latest_cover": actress.get("latest_cover", existing.get("latest_cover", "")),
                "latest_av_id": actress.get("latest_av_id", existing.get("latest_av_id", "")),
                "latest_title": actress.get("latest_title", existing.get("latest_title", "")),
                "latest_date": actress.get("latest_date", existing.get("latest_date", "")),
                "source": actress.get("source", existing.get("source", "")),
                "source_chain": actress.get("source_chain", existing.get("source_chain", [])),
                "match_reason": actress.get("match_reason", existing.get("match_reason", "")),
                "confidence": actress.get("confidence", existing.get("confidence", "")),
                "javdb_id": actress.get("javdb_id", existing.get("javdb_id", "")),
                "dmm_name": actress.get("dmm_name", existing.get("dmm_name", "")),
                "dmm_url": actress.get("dmm_url", existing.get("dmm_url", "")),
                "javlibrary_star_id": actress.get("javlibrary_star_id", existing.get("javlibrary_star_id", "")),
                "since_date": since_date,
                "poll_enabled": bool(actress.get("poll_enabled", existing.get("poll_enabled", True))),
                "include_vr": bool(actress.get("include_vr", existing.get("include_vr", False))),
                "last_polled_at": existing.get("last_polled_at", 0),
                "last_new_count": existing.get("last_new_count", 0),
                "subscribed_at": existing.get("subscribed_at", time.time()),
            }
            self._save()
            return dict(self.data["actress"][aid])

    def _find_actress_duplicate_id(self, actress: dict[str, Any], target_id: str) -> str:
        target_keys = actress_identity_keys({**actress, "id": target_id})
        if not target_keys:
            return ""
        for current_id, item in self.data.get("actress", {}).items():
            if current_id == target_id:
                continue
            if target_keys & actress_identity_keys(item):
                return str(current_id)
        return ""

    def update_actress_subscription(self, actress_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            item = self.data["actress"].get(actress_id)
            if not item:
                return None
            for key in ("name", "cover", "latest_cover", "latest_av_id", "latest_title", "latest_date", "source", "source_chain", "match_reason", "confidence", "javdb_id", "dmm_name", "dmm_url", "javlibrary_star_id"):
                if key not in payload:
                    continue
                value = payload.get(key)
                if value or key == "cover":
                    item[key] = value
            if "since_date" in payload:
                item["since_date"] = normalize_date(payload.get("since_date") or item.get("since_date"))
            if "poll_enabled" in payload:
                item["poll_enabled"] = bool(payload.get("poll_enabled"))
            if "include_vr" in payload:
                item["include_vr"] = bool(payload.get("include_vr"))
            self._save()
            return dict(item)

    def unsubscribe_actress(self, actress_id: str) -> bool:
        with self._lock:
            if actress_id in self.data["actress"]:
                del self.data["actress"][actress_id]
                self._save()
                return True
            return False

    def get_subscribed_actresses(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                self.data["actress"].values(),
                key=lambda item: float(item.get("subscribed_at") or 0),
                reverse=True,
            )

    def is_actress_subscribed(self, actress_id: str) -> bool:
        with self._lock:
            return actress_id in self.data["actress"]

    def mark_actress_polled(self, actress_id: str, new_count: int) -> None:
        with self._lock:
            item = self.data["actress"].get(actress_id)
            if not item:
                return
            item["last_polled_at"] = time.time()
            item["last_new_count"] = new_count
            self._save()

    def mark_global_poll(self, minute_key: str | None = None) -> None:
        with self._lock:
            settings = self.data.setdefault("settings", {})
            settings["last_poll_at"] = time.time()
            if minute_key:
                settings["last_poll_minute"] = minute_key
            self._save()

    def mark_task_poll(self, task_id: str, minute_key: str | None = None, result: dict[str, Any] | None = None, status: str = "ok") -> None:
        keys = {
            "actress_poll": ("last_poll_at", "last_poll_minute"),
            "av_download": ("last_av_poll_at", "last_av_poll_minute"),
            "wash_download": ("last_wash_poll_at", "last_wash_poll_minute"),
            "postprocess_qb": ("last_postprocess_poll_at", "last_postprocess_poll_minute"),
            "maker_refresh": ("last_maker_poll_at", "last_maker_poll_minute"),
            "ranking_refresh": ("last_ranking_poll_at", "last_ranking_poll_minute"),
            "asset_maintenance": ("last_asset_poll_at", "last_asset_poll_minute"),
        }
        if task_id not in keys:
            return
        with self._lock:
            settings = self.data.setdefault("settings", {})
            at_key, minute_key_name = keys[task_id]
            settings[at_key] = time.time()
            if minute_key:
                settings[minute_key_name] = minute_key
            if result is not None:
                results = settings.setdefault("last_task_results", {})
                if not isinstance(results, dict):
                    results = {}
                    settings["last_task_results"] = results
                results[task_id] = {
                    "task_id": task_id,
                    "ran_at": settings[at_key],
                    "status": status if status in {"ok", "failed"} else "ok",
                    "result": result,
                }
            self._save()


def normalize_date(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return date.today().isoformat()


def date_is_after(value: str | None, boundary: str | None) -> bool:
    try:
        item_date = date.fromisoformat((value or "")[:10])
        boundary_date = date.fromisoformat((boundary or "")[:10])
    except ValueError:
        return False
    return item_date > boundary_date


def actress_identity_keys(item: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("id", "name"):
        value = str(item.get(field) or "").strip().lower()
        if value:
            keys.add(value)
    cover = str(item.get("cover") or item.get("cover_url") or "").strip()
    if cover:
        stem = Path(urlparse(cover).path).stem.strip().lower()
        if stem:
            keys.add(stem)
    return keys


def normalize_cron(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    parts = raw.split()
    if len(parts) != 5:
        return fallback
    return raw


def normalize_pinned_makers(value: Any) -> list[dict[str, str]]:
    rows = value if isinstance(value, list) else DEFAULT_PINNED_MAKERS
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            name = str(row.get("name") or "").strip()
            url = str(row.get("url") or "").strip()
            preferred_listing_source = str(row.get("preferred_listing_source") or row.get("preferred_source") or "").strip().lower()
        else:
            name = str(row or "").strip()
            url = ""
            preferred_listing_source = ""
        if not name or name.lower() in seen:
            continue
        default_url = DEFAULT_PINNED_MAKER_URLS.get(name.lower(), "")
        default_source = DEFAULT_PINNED_MAKER_SOURCES.get(name.lower(), "auto")
        legacy_search_url = f"https://javdb.com/search?q={quote_plus(name)}&f=all"
        if not url or url == legacy_search_url:
            url = default_url or legacy_search_url
        if preferred_listing_source not in {"auto", "javlibrary", "dmm", "javdb"}:
            preferred_listing_source = default_source
        seen.add(name.lower())
        result.append({
            "name": name,
            "url": url,
            "preferred_listing_source": preferred_listing_source,
        })
    if result:
        return result[:20]
    return normalize_pinned_makers(DEFAULT_PINNED_MAKERS)


def normalize_filters(value: Any) -> dict[str, Any]:
    filters = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in ("only_chinese", "only_uncensored", "exclude_uncensored", "only_free", "only_uhd", "exclude_uhd"):
        result[key] = bool(filters.get(key))
    for key in ("min_size_mb", "max_size_mb"):
        raw = filters.get(key)
        if raw in (None, ""):
            result[key] = ""
            continue
        try:
            number = int(float(raw))
        except (TypeError, ValueError):
            number = 0
        result[key] = max(0, number)
    return result


def normalize_wash_settings(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_WASH_SETTINGS)
    result.update({key: raw.get(key, result[key]) for key in result})
    for key in ("enabled", "auto_cancel_expired", "check_chinese", "check_4k", "prefer_chinese", "prefer_4k"):
        result[key] = bool(result.get(key))
    try:
        result["expire_days"] = int(result.get("expire_days") or DEFAULT_WASH_EXPIRE_DAYS)
    except (TypeError, ValueError):
        result["expire_days"] = DEFAULT_WASH_EXPIRE_DAYS
    result["expire_days"] = max(7, min(365, result["expire_days"]))
    try:
        result["min_seeders"] = int(result.get("min_seeders") or 1)
    except (TypeError, ValueError):
        result["min_seeders"] = 1
    result["min_seeders"] = max(0, min(999, result["min_seeders"]))
    try:
        result["max_size_gb"] = int(result.get("max_size_gb") or 80)
    except (TypeError, ValueError):
        result["max_size_gb"] = 80
    result["max_size_gb"] = max(1, min(500, result["max_size_gb"]))
    return result


def normalize_wash_request(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    mode = str(raw.get("mode") or "").strip().lower()
    status = str(raw.get("status") or "").strip().lower()
    if mode not in {"chinese", "4k"}:
        mode = ""
    if status not in {"requested", "downloading", "completed", "expired", "cancelled", "error"}:
        status = "requested" if mode else ""
    result: dict[str, Any] = {
        "mode": mode,
        "status": status,
        "requested_at": float(raw.get("requested_at") or 0),
        "updated_at": float(raw.get("updated_at") or 0),
        "completed_at": float(raw.get("completed_at") or 0),
        "last_checked_at": float(raw.get("last_checked_at") or 0),
        "download_status": str(raw.get("download_status") or ""),
        "download_message": str(raw.get("download_message") or ""),
        "task_id": str(raw.get("task_id") or ""),
        "mteam_torrent_id": str(raw.get("mteam_torrent_id") or ""),
        "mteam_torrent_title": str(raw.get("mteam_torrent_title") or ""),
        "qb_hash": str(raw.get("qb_hash") or ""),
        "old_path": str(raw.get("old_path") or ""),
        "new_path": str(raw.get("new_path") or ""),
        "new_jellyfin_item_id": str(raw.get("new_jellyfin_item_id") or ""),
        "new_jellyfin_item_name": str(raw.get("new_jellyfin_item_name") or ""),
        "trash_path": str(raw.get("trash_path") or ""),
        "replace_status": str(raw.get("replace_status") or ""),
        "replace_message": str(raw.get("replace_message") or ""),
    }
    if not mode:
        result["status"] = ""
    return result
