"""订阅服务 - 纯 javdb 数据源"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

DEFAULT_ACTRESS_CRON = "0 21 * * *"
DEFAULT_AV_CRON = "0 22 * * *"
DEFAULT_MAKER_CRON = "0 */6 * * *"
DEFAULT_MAX_COACTORS = 2
DEFAULT_PINNED_MAKERS = [
    {"name": "S1 NO.1 STYLE", "url": "https://javdb.com/makers/7R?f=download"},
    {"name": "PRESTIGE", "url": "https://javdb.com/makers/6M?f=download"},
    {"name": "IDEA POCKET", "url": "https://javdb.com/makers/ZXX?f=download"},
    {"name": "Madonna", "url": "https://javdb.com/makers/zKW?f=download"},
    {"name": "SOD Create", "url": "https://javdb.com/makers/q6?f=download"},
]
DEFAULT_PINNED_MAKER_URLS = {item["name"].lower(): item["url"] for item in DEFAULT_PINNED_MAKERS}


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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

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
        data["settings"].setdefault("maker_cron", DEFAULT_MAKER_CRON)
        data["settings"].setdefault("max_coactors", DEFAULT_MAX_COACTORS)
        data["settings"].setdefault("poll_enabled", True)
        data["settings"].setdefault("last_poll_at", 0)
        data["settings"].setdefault("last_poll_minute", "")
        data["settings"].setdefault("last_av_poll_at", 0)
        data["settings"].setdefault("last_av_poll_minute", "")
        data["settings"].setdefault("last_maker_poll_at", 0)
        data["settings"].setdefault("last_maker_poll_minute", "")
        data["settings"]["pinned_makers"] = normalize_pinned_makers(data["settings"].get("pinned_makers"))
        for item in data.get("av", {}).values():
            if not isinstance(item, dict):
                continue
            item["filters"] = normalize_filters(item.get("filters", {}))
            item.setdefault("subscription_mode", "strict")
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

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            settings = self.data.setdefault("settings", {})
            if "poll_enabled" in payload:
                settings["poll_enabled"] = bool(payload.get("poll_enabled"))
            if "actress_cron" in payload:
                settings["actress_cron"] = normalize_cron(payload.get("actress_cron"), DEFAULT_ACTRESS_CRON)
            if "av_cron" in payload:
                settings["av_cron"] = normalize_cron(payload.get("av_cron"), DEFAULT_AV_CRON)
            if "maker_cron" in payload:
                settings["maker_cron"] = normalize_cron(payload.get("maker_cron"), DEFAULT_MAKER_CRON)
            if "pinned_makers" in payload:
                settings["pinned_makers"] = normalize_pinned_makers(payload.get("pinned_makers"))
            if "max_coactors" in payload:
                try:
                    count = int(payload.get("max_coactors") or DEFAULT_MAX_COACTORS)
                except (TypeError, ValueError):
                    count = DEFAULT_MAX_COACTORS
                settings["max_coactors"] = max(1, min(12, count))
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
                "status": av.get("status", existing.get("status", "pending")),
                "library_status": av.get("library_status", existing.get("library_status", "")),
                "jellyfin_item_id": av.get("jellyfin_item_id", existing.get("jellyfin_item_id", "")),
                "jellyfin_item_name": av.get("jellyfin_item_name", existing.get("jellyfin_item_name", "")),
                "download_status": av.get("download_status", existing.get("download_status", "")),
                "download_message": av.get("download_message", existing.get("download_message", "")),
                "mteam_torrent_id": av.get("mteam_torrent_id", existing.get("mteam_torrent_id", "")),
                "mteam_torrent_title": av.get("mteam_torrent_title", existing.get("mteam_torrent_title", "")),
                "filters": normalize_filters(av.get("filters", existing.get("filters", {}))),
                "subscription_mode": str(av.get("subscription_mode", existing.get("subscription_mode", "strict")) or "strict"),
                "detail": av.get("detail", existing.get("detail", {})),
                "downloaded_at": av.get("downloaded_at", existing.get("downloaded_at", 0)),
                "subscribed_at": existing.get("subscribed_at", time.time()),
                "auto_subscribed": bool(av.get("auto_subscribed", existing.get("auto_subscribed", False))),
                "source_actress_id": av.get("source_actress_id", existing.get("source_actress_id", "")),
                "source_actress_name": av.get("source_actress_name", existing.get("source_actress_name", "")),
            }
            self._save()
            return dict(self.data["av"][av_id])

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
                "download_status",
                "download_message",
                "mteam_torrent_id",
                "mteam_torrent_title",
                "downloaded_at",
                "detail",
            ):
                if key in payload:
                    item[key] = payload[key]
            self._save()
            return dict(item)

    def get_subscribed_av(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.data["av"].values())

    def is_av_subscribed(self, av_id: str) -> bool:
        with self._lock:
            return av_id in self.data["av"]

    # ========== 女优订阅 ==========

    def subscribe_actress(self, actress: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            aid = actress.get("id", "")
            if not aid:
                return {}
            existing = self.data["actress"].get(aid, {})
            since_date = normalize_date(actress.get("since_date") or existing.get("since_date") or str(date.today()))
            self.data["actress"][aid] = {
                "id": aid,
                "name": actress.get("name", existing.get("name", "")),
                "cover": actress.get("cover", actress.get("cover_url", existing.get("cover", ""))),
                "since_date": since_date,
                "poll_enabled": bool(actress.get("poll_enabled", existing.get("poll_enabled", True))),
                "include_vr": bool(actress.get("include_vr", existing.get("include_vr", False))),
                "last_polled_at": existing.get("last_polled_at", 0),
                "last_new_count": existing.get("last_new_count", 0),
                "subscribed_at": existing.get("subscribed_at", time.time()),
            }
            self._save()
            return dict(self.data["actress"][aid])

    def update_actress_subscription(self, actress_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            item = self.data["actress"].get(actress_id)
            if not item:
                return None
            for key in ("name", "cover"):
                value = payload.get(key)
                if value:
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
            return list(self.data["actress"].values())

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

    def mark_task_poll(self, task_id: str, minute_key: str | None = None) -> None:
        keys = {
            "actress_poll": ("last_poll_at", "last_poll_minute"),
            "av_download": ("last_av_poll_at", "last_av_poll_minute"),
            "maker_refresh": ("last_maker_poll_at", "last_maker_poll_minute"),
        }
        if task_id not in keys:
            return
        with self._lock:
            settings = self.data.setdefault("settings", {})
            at_key, minute_key_name = keys[task_id]
            settings[at_key] = time.time()
            if minute_key:
                settings[minute_key_name] = minute_key
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
        else:
            name = str(row or "").strip()
            url = ""
        if not name or name.lower() in seen:
            continue
        default_url = DEFAULT_PINNED_MAKER_URLS.get(name.lower(), "")
        legacy_search_url = f"https://javdb.com/search?q={quote_plus(name)}&f=all"
        if not url or url == legacy_search_url:
            url = default_url or legacy_search_url
        seen.add(name.lower())
        result.append({
            "name": name,
            "url": url,
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
