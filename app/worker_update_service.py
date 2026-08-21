from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx


DEFAULT_WORKER_UPDATE_URL = "https://raw.githubusercontent.com/jeron-lgy/Moviemuse/main/deploy/windows-backend/worker-release.json"
UPDATE_CHECK_TTL_SECONDS = 6 * 60 * 60
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.]([A-Za-z0-9.-]+))?$")


def version_key(value: str) -> tuple[int, int, int, int, str] | None:
    match = VERSION_PATTERN.match(str(value or "").strip())
    if not match:
        return None
    prerelease = str(match.group(4) or "")
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        0 if prerelease else 1,
        prerelease,
    )


def newer_version(latest: str, current: str) -> bool:
    latest_key = version_key(latest)
    current_key = version_key(current)
    return bool(latest_key and current_key and latest_key > current_key)


class WorkerSoftwareUpdateService:
    def __init__(self, current_version: str, cache_path: Path) -> None:
        self.current_version = str(current_version or "dev")
        self.cache_path = cache_path
        self._lock = threading.RLock()
        self._cache = self._load_cache()

    def _load_cache(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def _persist_locked(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(f"{self.cache_path.suffix}.tmp")
        temporary.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.cache_path)

    def status(self) -> dict[str, Any]:
        source_url = os.getenv("MOVIEMUSE_WORKER_UPDATE_URL", "").strip() or DEFAULT_WORKER_UPDATE_URL
        with self._lock:
            if self._cache.get("source_url") == source_url and self._cache.get("current_version") == self.current_version:
                return dict(self._cache)
        return {
            "current_version": self.current_version,
            "latest_version": "",
            "update_available": False,
            "version_status": "not_checked",
            "release_url": "",
            "published_at": "",
            "checked_at": 0,
            "source_url": source_url,
            "error": "",
        }

    @staticmethod
    def _release_payload(payload: Any) -> dict[str, str]:
        candidates: list[dict[str, Any]] = []
        if isinstance(payload, list):
            candidates = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            candidates = [payload]
        versioned: list[tuple[tuple[int, int, int, int, str], dict[str, Any], str]] = []
        for item in candidates:
            version = str(item.get("version") or item.get("latest_version") or item.get("tag_name") or item.get("name") or "").strip()
            parsed = version_key(version)
            if parsed:
                versioned.append((parsed, item, version))
        if not versioned:
            raise ValueError("更新源没有提供有效的软件版本")
        stable = [value for value in versioned if value[0][3] == 1]
        _key, item, version = max(stable or versioned, key=lambda value: value[0])
        release_url = str(item.get("release_url") or item.get("html_url") or item.get("download_url") or "")
        return {
            "version": version if version.startswith("v") else f"v{version}",
            "release_url": release_url,
            "published_at": str(item.get("published_at") or item.get("created_at") or ""),
        }

    def check(self, *, force: bool = False) -> dict[str, Any]:
        now = time.time()
        current = self.status()
        if (
            not force
            and float(current.get("checked_at") or 0) > now - UPDATE_CHECK_TTL_SECONDS
            and current.get("version_status") != "not_checked"
        ):
            return current
        source_url = str(current["source_url"])
        token = os.getenv("MOVIEMUSE_WORKER_UPDATE_TOKEN", "").strip() or os.getenv("GITHUB_TOKEN", "").strip()
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "MovieMuse-Worker/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(follow_redirects=True, timeout=10, headers=headers) as client:
                response = client.get(source_url)
                response.raise_for_status()
                release = self._release_payload(response.json())
            latest = release["version"]
            if not release["release_url"] and source_url == DEFAULT_WORKER_UPDATE_URL:
                release["release_url"] = "https://github.com/jeron-lgy/Moviemuse/releases"
            if version_key(self.current_version) is None:
                status = "development"
                available = False
            else:
                available = newer_version(latest, self.current_version)
                status = "update_available" if available else "up_to_date"
            result = {
                "current_version": self.current_version,
                "latest_version": latest,
                "update_available": available,
                "version_status": status,
                "release_url": release["release_url"],
                "published_at": release["published_at"],
                "checked_at": now,
                "source_url": source_url,
                "error": "",
            }
        except Exception as exc:
            result = {
                **current,
                "checked_at": now,
                "version_status": "check_failed",
                "update_available": False,
                "error": str(exc),
            }
        with self._lock:
            self._cache = result
            self._persist_locked()
        return dict(result)
