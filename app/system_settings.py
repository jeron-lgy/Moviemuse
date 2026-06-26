"""系统集成设置存储。"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
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
    "wechat_work": {
        "name": "企业微信",
        "config": {
            "corp_id": "",
            "corp_secret": "",
            "agent_id": "",
            "proxy": "",
            "touser": "@all",
            "default_image_url": "",
            "cover_enabled": False,
            "token": "",
            "aes_key": "",
            "callback_path": "/api/v1/message",
        },
    },
}


def password_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


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
        "api_key": "",
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
    "network": {
        "proxy_enabled": False,
        "http_proxy": "",
        "https_proxy": "",
        "no_proxy": "localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12",
        "apply_to_javdb": True,
        "flaresolverr_url": "",
    },
    "notifications": {
        "channels": [],
        "events": {
            "av_subscribed": True,
            "mteam_found": True,
            "torrent_sent": True,
            "jellyfin_in_library": True,
            "task_failed": True,
            "scan_completed": False,
            "subtitle_completed": False,
            "subtitle_failed": True,
            "automation_actress_poll": True,
            "automation_av_download": True,
            "automation_wash_download": True,
        },
        "templates": {},
    },
    "auth": {
        "username": "admin",
        "password_hash": "",
    },
    "demo": {
        "enabled": False,
        "cover_url": "",
        "hide_system_settings": True,
    },
    "duplicate_scan": {
        "selected_scan_dirs": [],
    },
}


class SystemSettingsService:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.settings_file = data_dir / "system_settings.json"
        self.initial_password_file = data_dir / "initial_admin_password.txt"
        self._lock = threading.RLock()
        self._generated_initial_password: str | None = None
        self._initialized_auth_password = False
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self._normalize_network()
        self._normalize_notifications()
        self._normalize_auth()
        self._normalize_demo()
        self._normalize_duplicate_scan()
        if self._initialized_auth_password:
            self._save()
        if self._generated_initial_password:
            self._write_initial_password_hint(self.data["auth"]["username"], self._generated_initial_password)

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
            payload = json.loads(json.dumps(self.data))
            auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
            payload["auth"] = {
                "username": str(auth.get("username") or "admin"),
                "password_configured": bool(auth.get("password_hash")),
            }
            return payload

    def auth(self) -> dict[str, str]:
        with self._lock:
            auth = self.data.setdefault("auth", {})
            username = str(auth.get("username") or "admin").strip() or "admin"
            hashed = str(auth.get("password_hash") or "")
            if not hashed:
                self._normalize_auth()
                hashed = str(auth.get("password_hash") or "")
            return {"username": username, "password_hash": hashed}

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for section in ("mteam", "qbittorrent", "jellyfin", "network", "notifications", "demo", "duplicate_scan"):
                value = payload.get(section)
                if isinstance(value, dict):
                    self.data.setdefault(section, {})
                    for key, item in value.items():
                        if key in DEFAULT_SETTINGS[section]:
                            self.data[section][key] = item
            auth = payload.get("auth")
            if isinstance(auth, dict):
                self.data.setdefault("auth", {})
                username = str(auth.get("username") or "").strip()
                password = str(auth.get("password") or "")
                if username:
                    self.data["auth"]["username"] = username
                if password:
                    self.data["auth"]["password_hash"] = password_hash(password)
                    self._remove_initial_password_hint()
            self.data["mteam"]["enabled"] = bool(self.data.get("mteam", {}).get("enabled"))
            self._normalize_network()
            self._normalize_notifications()
            self._normalize_auth()
            self._normalize_demo()
            self._normalize_duplicate_scan()
            self._save()
            return self.get()

    def duplicate_scan(self) -> dict[str, Any]:
        with self._lock:
            self._normalize_duplicate_scan()
            return json.loads(json.dumps(self.data.get("duplicate_scan", {})))

    def update_duplicate_scan(self, selected_scan_dirs: list[str]) -> dict[str, Any]:
        with self._lock:
            self.data["duplicate_scan"] = {"selected_scan_dirs": selected_scan_dirs}
            self._normalize_duplicate_scan()
            self._save()
            return self.duplicate_scan()

    def _normalize_network(self) -> None:
        network = self.data.setdefault("network", {})
        defaults = DEFAULT_SETTINGS["network"]
        for key, default in defaults.items():
            network.setdefault(key, default)
        network["proxy_enabled"] = bool(network.get("proxy_enabled"))
        network["apply_to_javdb"] = bool(network.get("apply_to_javdb", True))
        for key in ("http_proxy", "https_proxy", "no_proxy", "flaresolverr_url"):
            network[key] = str(network.get(key) or "").strip()

    def _normalize_notifications(self) -> None:
        notifications = self.data.setdefault("notifications", {})
        notifications["channels"] = normalize_notification_channels(notifications.get("channels"))
        events = notifications.setdefault("events", {})
        for key, default in DEFAULT_SETTINGS["notifications"]["events"].items():
            events[key] = bool(events.get(key, default))
        templates = notifications.setdefault("templates", {})
        if not isinstance(templates, dict):
            notifications["templates"] = {}

    def _normalize_auth(self) -> None:
        auth = self.data.setdefault("auth", {})
        username = str(auth.get("username") or "admin").strip() or "admin"
        hashed = str(auth.get("password_hash") or "").strip()
        legacy_password = str(auth.get("password") or "")
        initial_password = str(os.getenv("MOVIEMUSE_ADMIN_PASSWORD") or "").strip()
        if not hashed and not legacy_password and not initial_password:
            initial_password = secrets.token_urlsafe(12)
            self._generated_initial_password = initial_password
        if not hashed:
            self._initialized_auth_password = True
        auth["username"] = username
        auth["password_hash"] = hashed or password_hash(legacy_password or initial_password)
        auth.pop("password", None)

    def _write_initial_password_hint(self, username: str, password: str) -> None:
        message = (
            "MovieMuse initial admin account\n"
            f"Username: {username}\n"
            f"Password: {password}\n"
            "Please change this password in System -> User Settings after first login.\n"
        )
        try:
            self.initial_password_file.write_text(message, encoding="utf-8")
        except Exception:
            pass
        print(
            f"[MovieMuse] initial admin password generated: username={username} password={password}",
            flush=True,
        )

    def _remove_initial_password_hint(self) -> None:
        try:
            self.initial_password_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _normalize_demo(self) -> None:
        demo = self.data.setdefault("demo", {})
        defaults = DEFAULT_SETTINGS["demo"]
        for key, default in defaults.items():
            demo.setdefault(key, default)
        demo["enabled"] = bool(demo.get("enabled", False))
        demo["hide_system_settings"] = bool(demo.get("hide_system_settings", True))
        demo["cover_url"] = str(demo.get("cover_url") or "").strip()

    def _normalize_duplicate_scan(self) -> None:
        duplicate_scan = self.data.setdefault("duplicate_scan", {})
        defaults = DEFAULT_SETTINGS["duplicate_scan"]
        for key, default in defaults.items():
            duplicate_scan.setdefault(key, json.loads(json.dumps(default)))
        raw_dirs = duplicate_scan.get("selected_scan_dirs")
        if not isinstance(raw_dirs, list):
            raw_dirs = []
        selected_dirs: list[str] = []
        seen: set[str] = set()
        for item in raw_dirs:
            value = str(item or "").strip()
            if not value or value in seen:
                continue
            selected_dirs.append(value)
            seen.add(value)
        duplicate_scan["selected_scan_dirs"] = selected_dirs

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
    for channel_type in ("serverchan", "gotify", "wechat_work"):
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
    if channel_type == "wechat_work":
        merged_config["cover_enabled"] = bool(merged_config.get("cover_enabled", False))
        for key in ("corp_id", "corp_secret", "agent_id", "proxy", "touser", "default_image_url", "token", "aes_key", "callback_path"):
            merged_config[key] = str(merged_config.get(key) or "").strip()
        if not merged_config["touser"]:
            merged_config["touser"] = "@all"
        if not merged_config["callback_path"]:
            merged_config["callback_path"] = "/api/v1/message"
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
