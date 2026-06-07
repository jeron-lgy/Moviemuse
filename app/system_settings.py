"""系统集成设置存储。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


NOTIFICATION_CHANNEL_DEFAULTS: dict[str, dict[str, Any]] = {
    "serverchan": {
        "name": "Server 酱",
        "config": {"send_key": ""},
    },
    "gotify": {
        "name": "Gotify",
        "config": {"url": "", "token": "", "priority": 5},
    },
}


DEFAULT_SETTINGS: dict[str, Any] = {
    "mteam": {
        "site_url": "",
        "mode": "rss",
        "rss_url": "",
        "api_url": "",
        "api_key": "",
        "api_method": "POST",
        "search_mode": "adult",
        "enabled": False,
    },
    "qbittorrent": {
        "url": "",
        "username": "",
        "password": "",
        "save_path": "",
        "category": "",
        "tags": "",
    },
    "jellyfin": {
        "url": "",
        "api_key": "",
        "username": "",
        "library_id": "",
        "library_name": "",
        "dedupe_enabled": True,
    },
    "notifications": {
        "channels": [],
        "events": {
            "actress_new_av": True,
            "av_subscribed": True,
            "mteam_found": True,
            "torrent_sent": True,
            "jellyfin_in_library": True,
            "task_failed": True,
            "scan_completed": False,
            "subtitle_completed": False,
            "subtitle_failed": True,
        },
        "templates": {},
    },
}


class SystemSettingsService:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.settings_file = data_dir / "system_settings.json"
        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self._normalize_notifications()

    def _load(self) -> dict[str, Any]:
        data = json.loads(json.dumps(DEFAULT_SETTINGS))
        if self.settings_file.exists():
            try:
                loaded = json.loads(self.settings_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    merge_dict(data, loaded)
            except Exception:
                pass
        return data

    def _save(self) -> None:
        self.settings_file.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.data))

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for section in ("mteam", "qbittorrent", "jellyfin", "notifications"):
                value = payload.get(section)
                if isinstance(value, dict):
                    self.data.setdefault(section, {})
                    for key, item in value.items():
                        if key in DEFAULT_SETTINGS[section]:
                            self.data[section][key] = item
            self.data["mteam"]["enabled"] = bool(self.data.get("mteam", {}).get("enabled"))
            self._normalize_notifications()
            self._save()
            return self.get()

    def _normalize_notifications(self) -> None:
        notifications = self.data.setdefault("notifications", {})
        notifications["channels"] = normalize_notification_channels(notifications.get("channels"))
        events = notifications.setdefault("events", {})
        for key, default in DEFAULT_SETTINGS["notifications"]["events"].items():
            events[key] = bool(events.get(key, default))
        templates = notifications.setdefault("templates", {})
        if not isinstance(templates, dict):
            notifications["templates"] = {}


def merge_dict(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_dict(target[key], value)
        else:
            target[key] = value


def merge_defaults(target: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if key not in target:
            target[key] = value
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_defaults(target[key], value)


def normalize_notification_channels(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = legacy_notification_channels(raw)
    if not isinstance(raw, list):
        raw = []
    channels: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        channel = normalize_notification_channel(item, index)
        if not channel:
            continue
        base_id = channel["id"]
        suffix = 2
        while channel["id"] in used_ids:
            channel["id"] = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(channel["id"])
        channels.append(channel)
    return channels


def legacy_notification_channels(raw: dict[str, Any]) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    for channel_type in ("serverchan", "gotify"):
        config = raw.get(channel_type)
        if not isinstance(config, dict):
            continue
        if not config.get("enabled") and not any(value for key, value in config.items() if key != "enabled"):
            continue
        channels.append(
            {
                "id": channel_type,
                "type": channel_type,
                "name": NOTIFICATION_CHANNEL_DEFAULTS[channel_type]["name"],
                "enabled": bool(config.get("enabled")),
                "config": {key: value for key, value in config.items() if key != "enabled"},
            }
        )
    return channels


def normalize_notification_channel(item: dict[str, Any], index: int) -> dict[str, Any] | None:
    channel_type = str(item.get("type") or "").strip().lower()
    if channel_type not in NOTIFICATION_CHANNEL_DEFAULTS:
        return None
    defaults = NOTIFICATION_CHANNEL_DEFAULTS[channel_type]
    config = item.get("config") if isinstance(item.get("config"), dict) else {}
    if not config:
        config = {key: value for key, value in item.items() if key not in {"id", "type", "name", "enabled"}}
    merged_config = json.loads(json.dumps(defaults["config"]))
    for key, value in config.items():
        if key in merged_config:
            merged_config[key] = value
    if channel_type == "gotify":
        try:
            merged_config["priority"] = max(0, min(10, int(merged_config.get("priority") or 5)))
        except (TypeError, ValueError):
            merged_config["priority"] = 5
    raw_id = str(item.get("id") or "").strip()
    channel_id = slug_id(raw_id) if raw_id else f"{channel_type}-{index + 1}"
    name = str(item.get("name") or "").strip() or defaults["name"]
    return {
        "id": channel_id,
        "type": channel_type,
        "name": name,
        "enabled": bool(item.get("enabled")),
        "config": merged_config,
    }


def slug_id(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or "channel"
