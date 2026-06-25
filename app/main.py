from __future__ import annotations

import os
import platform
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import json
import secrets
import uuid
import hashlib
import base64
import codecs
import struct
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .log_service import AppLogService
from .scanner import ScanResult, detect_catalog_number, normalize_catalog_digits, scan_libraries
from .scan_state import scan_cache
from .storage import MoveRequest, MoveResult, Storage
from .mteam_service import download_mteam_torrent, search_mteam
from .postprocess_service import PostprocessService
from .subscription_service import SubscriptionService, date_is_after
from .system_settings import SystemSettingsService
from .dmm_service import dmm
from .javdb_service import is_access_ban_error, javdb
from .javlibrary_service import BASE_URL as JAVLIBRARY_BASE_URL, javlibrary
from .subtitle_service import (
    SubtitleJob,
    SubtitleSegment,
    SubtitleService,
    load_compute_config,
    load_subtitle_settings,
    read_srt,
    save_compute_config,
    translation_source_text,
)


BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = BASE_DIR.parent / "frontend" / "dist"
SUBTITLE_FILE_KINDS = {"original_srt", "translated_srt", "bilingual_srt", "original_vtt", "translated_vtt"}
SUBTITLE_REMOTE_JOBS_CACHE_FILE = "subtitle_remote_jobs_cache.json"


def split_dirs(value: str) -> list[Path]:
    return [Path(item.strip()) for item in value.split(";") if item.strip()]


def settings() -> tuple[list[Path], Path, Path]:
    default_media = "sample-media" if os.name == "nt" else "/media"
    default_trash = "trash" if os.name == "nt" else "/trash"
    default_data = "data" if os.name == "nt" else "/data"
    media_dirs = split_dirs(os.getenv("MEDIA_DIRS", default_media))
    trash_dir = Path(os.getenv("TRASH_DIR", default_trash))
    data_dir = Path(os.getenv("APP_DATA_DIR", default_data))
    return media_dirs, trash_dir, data_dir


LOCAL_TIMEZONE = os.getenv("MOVIEMUSE_TIMEZONE", "Asia/Shanghai")


def local_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(LOCAL_TIMEZONE))
    except Exception:
        return datetime.now()


def local_time_text(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return local_now().strftime(fmt)


def selectable_scan_dirs(media_dirs: list[Path], excluded_dirs: list[Path] | None = None) -> list[Path]:
    choices: list[Path] = []
    excluded_roots = resolved_roots(excluded_dirs or [])
    for media_dir in media_dirs:
        if not media_dir.exists() or not media_dir.is_dir():
            continue
        for child in sorted(media_dir.iterdir(), key=lambda item: item.name.lower()):
            try:
                child_path = child.resolve()
            except OSError:
                child_path = child.absolute()
            if child.is_dir() and not is_under_any(child_path, excluded_roots):
                choices.append(child)
    return choices


def selected_scan_dirs(
    media_dirs: list[Path],
    raw_dirs: list[str],
    excluded_dirs: list[Path] | None = None,
) -> list[Path]:
    if not raw_dirs:
        return []
    roots = [media_dir.resolve() for media_dir in media_dirs if media_dir.exists()]
    excluded_roots = resolved_roots(excluded_dirs or [])
    selected: list[Path] = []
    for raw_dir in raw_dirs:
        candidate = Path(raw_dir).resolve()
        if any(is_relative_to(candidate, root) for root in roots) and not is_under_any(candidate, excluded_roots):
            selected.append(candidate)
    return selected


def scan_dir_identity(path: Path) -> str:
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path.absolute()).casefold()


def saved_duplicate_scan_dirs(media_dirs: list[Path], trash_dir: Path, choices: list[Path]) -> list[Path]:
    settings_data = get_system_settings_service().duplicate_scan()
    raw_dirs = settings_data.get("selected_scan_dirs")
    if not isinstance(raw_dirs, list):
        raw_dirs = []
    choice_by_key = {scan_dir_identity(path): path for path in choices}
    selected: list[Path] = []
    seen: set[str] = set()
    for path in selected_scan_dirs(media_dirs, [str(item) for item in raw_dirs], [trash_dir]):
        key = scan_dir_identity(path)
        if key in choice_by_key and key not in seen:
            selected.append(choice_by_key[key])
            seen.add(key)
    return selected


def save_duplicate_scan_dirs(media_dirs: list[Path], trash_dir: Path, raw_dirs: list[str], allow_empty: bool = False) -> list[Path]:
    choices = selectable_scan_dirs(media_dirs, [trash_dir])
    choice_by_key = {scan_dir_identity(path): path for path in choices}
    selected: list[Path] = []
    seen: set[str] = set()
    for path in selected_scan_dirs(media_dirs, raw_dirs, [trash_dir]):
        key = scan_dir_identity(path)
        if key in choice_by_key and key not in seen:
            selected.append(choice_by_key[key])
            seen.add(key)
    if not selected and not allow_empty:
        raise HTTPException(status_code=400, detail="请至少选择一个媒体子目录")
    get_system_settings_service().update_duplicate_scan([str(path) for path in selected])
    return selected


def resolved_roots(paths: list[Path]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for path in paths:
        try:
            roots.append(path.resolve())
        except OSError:
            roots.append(path.absolute())
    return tuple(roots)


def is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(is_relative_to(path, root) for root in roots)


def backend_url() -> str:
    _, _, data_dir = settings()
    configured = str(load_compute_config(data_dir).get("subtitle_backend_url", "")).strip().rstrip("/")
    if configured:
        return configured
    return os.getenv("SUBTITLE_BACKEND_URL", "").strip().rstrip("/")


def compute_node_only() -> bool:
    return os.getenv("COMPUTE_NODE_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def subtitle_public_url() -> str:
    configured = os.getenv("SUBTITLE_BACKEND_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    if backend_url():
        return backend_url()
    return os.getenv("SUBTITLE_LOCAL_PUBLIC_URL", "http://127.0.0.1:18181").strip().rstrip("/")


def invalid_public_url(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return True
    if any(token in normalized for token in ("unraid-ip", "windows-ip", "your-unraid-ip", "your-windows-ip")):
        return True
    host = urlparse(normalized).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def console_public_url() -> str:
    _, _, data_dir = settings()
    configured = str(load_compute_config(data_dir).get("console_public_url", "")).strip().rstrip("/")
    if configured:
        return configured
    for name in ("CONSOLE_PUBLIC_URL", "MOVIEMUSE_PUBLIC_URL", "SUBTITLE_CONSOLE_PUBLIC_URL"):
        configured = os.getenv(name, "").strip().rstrip("/")
        if configured:
            return configured
    return os.getenv("SUBTITLE_LOCAL_PUBLIC_URL", "http://127.0.0.1:18180").strip().rstrip("/")


def require_remote_callback_url(task_id: str) -> str:
    public_url = console_public_url()
    if invalid_public_url(public_url):
        raise RuntimeError(
            "远程算力端回调地址未正确配置。请在算力端设置里填写 Unraid 回调地址，"
            "例如 http://unraid-host.local:18188，不能使用 UNRAID-IP、localhost 或 127.0.0.1。"
        )
    return f"{public_url}/api/postprocess/tasks/{task_id}/worker-done"


def backend_headers() -> dict[str, str]:
    _, _, data_dir = settings()
    token = str(load_compute_config(data_dir).get("subtitle_backend_token", "")).strip()
    if not token:
        token = os.getenv("SUBTITLE_BACKEND_TOKEN", "").strip()
    return {"X-API-Key": token} if token else {}


def remote_http_client(timeout: float | None = 30) -> httpx.Client:
    return httpx.Client(timeout=timeout, trust_env=False)


def frontend_api_token() -> str:
    return os.getenv("SUBTITLE_API_TOKEN", "").strip()


POSTPROCESS_TERMINAL_STATUSES = {"completed", "failed", "ignored", "expired", "conflict"}


def postprocess_task_is_terminal(task: dict[str, Any] | None) -> bool:
    return str((task or {}).get("status") or "") in POSTPROCESS_TERMINAL_STATUSES


def postprocess_callback_headers(job: dict[str, Any] | None) -> dict[str, str]:
    token = str((job or {}).get("callback_token") or "").strip()
    return {"X-API-Key": token} if token else {}


def parse_proxy_path_map() -> list[tuple[str, str]]:
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    raw_parts = [
        str(config.get("subtitle_path_map", "") or ""),
        os.getenv("SUBTITLE_PROXY_PATH_MAP", ""),
    ]
    raw = "\n".join(part.strip() for part in raw_parts if part.strip())
    pairs: list[tuple[str, str]] = []
    for item in raw.replace("\n", ";").split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            source, target = item.split("=", 1)
        else:
            source, target = "/media", item
        source = source.strip().replace("\\", "/").rstrip("/")
        target = target.strip().rstrip("\\/")
        if source and not source.startswith("/"):
            source = f"/{source}"
        if source and target:
            pairs.append((source, target))
    return pairs


def rewrite_proxy_path(value: str | None) -> str | None:
    if not value:
        return value
    normalized = value.replace("\\", "/")
    for source, target in parse_proxy_path_map():
        if normalized == source or normalized.startswith(source + "/"):
            suffix = normalized[len(source) :].lstrip("/")
            if "\\" in target:
                windows_suffix = suffix.replace("/", "\\")
                return f"{target}\\{windows_suffix}" if suffix else target
            return f"{target}/{suffix}" if suffix else target
    return value


def rewrite_backend_path_to_console(value: str | None) -> str | None:
    if not value:
        return value
    normalized = value.replace("\\", "/")
    for source, target in parse_proxy_path_map():
        source_base = source.rstrip("\\/")
        clean_source = source.replace("\\", "/").rstrip("/")
        clean_target = target.replace("\\", "/").rstrip("/")
        if not clean_target:
            continue
        if normalized == clean_target or normalized.startswith(clean_target + "/"):
            suffix = normalized[len(clean_target) :].lstrip("/")
            if "\\" in source:
                windows_suffix = suffix.replace("/", "\\")
                return f"{source_base}\\{windows_suffix}" if suffix else source_base
            return f"{clean_source}/{suffix}" if suffix else clean_source
    return value


def rewrite_subtitle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    rewritten = dict(payload)
    rewritten["video_path"] = rewrite_proxy_path(rewritten.get("video_path"))
    rewritten["output_dir"] = rewrite_proxy_path(rewritten.get("output_dir"))
    return rewritten


def remote_settings() -> dict[str, Any]:
    return {
        "whisper_model": os.getenv("WHISPER_MODEL", "large-v3"),
        "whisper_model_dir": "",
        "whisper_device": "cuda",
        "whisper_compute_type": "float16",
        "subtitle_max_workers": 1,
        "translation_max_workers": 1,
        "subtitle_output_dir": os.getenv("SUBTITLE_OUTPUT_DIR", ""),
        "subtitle_path_map": "",
        "default_translate_backend": "google",
        "google_translate_url": "https://translate.google.com/translate_a/single",
        "deepl_api_url": "https://api-free.deepl.com/v2/translate",
        "deepl_api_key": "",
        "openai_base_url": "",
        "openai_api_key": "",
        "openai_model": "gpt-4.1-mini",
        "openai_batch_size": 12,
        "openai_max_concurrency": 2,
        "openai_translation_style": "adult_natural",
        "openai_style_intensity": "medium",
        "openai_context_lines": 2,
        "openai_glossary": "",
        "ollama_url": "",
        "ollama_model": "qwen2.5:7b",
        "subtitle_api_token": "",
        "default_model": os.getenv("WHISPER_MODEL", "large-v3"),
        "device": "remote",
        "compute_type": "Windows 5090 后端",
        "default_output_dir": os.getenv("SUBTITLE_OUTPUT_DIR", "") or "后端决定",
        "path_map": parse_proxy_path_map() or [("Unraid 容器", backend_url())],
        "api_token": frontend_api_token(),
    }


def translation_backend_options(settings_obj: Any | None = None) -> list[dict[str, object]]:
    openai_base = getattr(settings_obj, "openai_base_url", "") if settings_obj else os.getenv("TRANSLATE_OPENAI_BASE_URL", "")
    openai_key = getattr(settings_obj, "openai_api_key", "") if settings_obj else os.getenv("TRANSLATE_OPENAI_API_KEY", "")
    deepl_key = getattr(settings_obj, "deepl_api_key", "") if settings_obj else os.getenv("DEEPL_API_KEY", "")
    ollama_url = getattr(settings_obj, "ollama_url", "") if settings_obj else os.getenv("OLLAMA_URL", "")
    return [
        {"id": "google", "name": "Google 免费翻译", "available": True, "note": "默认优先，无需 API Key，使用 translate.googleapis.com"},
        {"id": "deepl", "name": "DeepL API", "available": bool(deepl_key), "note": "填写 DeepL API Key 后可用，默认使用 api-free.deepl.com"},
        {"id": "deepseek", "name": "DeepSeek API", "available": bool(openai_base and openai_key), "note": "填写 Base URL、API Key 和模型名"},
        {"id": "ollama", "name": "本地 Ollama API", "available": bool(ollama_url), "note": "OLLAMA_URL / OLLAMA_TRANSLATE_MODEL"},
    ]


SECRET_PLACEHOLDER = "********"
SECRET_SETTING_KEYS = {
    "api_token",
    "deepl_api_key",
    "openai_api_key",
    "subtitle_api_token",
    "subtitle_backend_token",
}


def is_secret_placeholder(value: Any) -> bool:
    return str(value or "").strip() == SECRET_PLACEHOLDER


def redact_secret_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    redacted = dict(payload or {})
    for key in SECRET_SETTING_KEYS:
        if key in redacted and str(redacted.get(key) or ""):
            redacted[key] = SECRET_PLACEHOLDER
    return redacted


def redact_secret_response(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted = {key: redact_secret_response(value) for key, value in payload.items()}
        return redact_secret_settings(redacted)
    if isinstance(payload, list):
        return [redact_secret_response(item) for item in payload]
    return payload


def restore_secret_placeholders(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    restored = dict(payload or {})
    for key in SECRET_SETTING_KEYS:
        if key in restored and is_secret_placeholder(restored.get(key)):
            existing_value = existing.get(key)
            if existing_value not in (None, ""):
                restored[key] = existing_value
            else:
                restored.pop(key, None)
    return restored


def whisper_model_options() -> list[dict[str, str]]:
    return [
        {
            "id": "large-v3",
            "name": "large-v3",
            "note": "推荐 5090 使用，精度最高，适合电影字幕。",
            "url": "https://huggingface.co/Systran/faster-whisper-large-v3",
        },
        {
            "id": "large-v3-turbo",
            "name": "large-v3-turbo",
            "note": "速度更快，适合批量补字幕时优先尝试。",
            "url": "https://huggingface.co/Systran/faster-whisper-large-v3-turbo",
        },
        {
            "id": "medium",
            "name": "medium",
            "note": "占用更低，适合临时降负载或 CPU 回退。",
            "url": "https://huggingface.co/Systran/faster-whisper-medium",
        },
    ]


def local_model_dirs(model_dir: Path | None) -> list[dict[str, object]]:
    if not model_dir or not model_dir.exists():
        return []
    entries: list[dict[str, object]] = []
    for child in sorted(model_dir.iterdir(), key=lambda item: item.name.lower()):
        if child.is_dir():
            size = sum(file.stat().st_size for file in child.rglob("*") if file.is_file())
            entries.append({"name": child.name, "path": str(child), "size": size})
    return entries


def bytes_label(value: int) -> str:
    size = float(value)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def raise_remote_error(exc: httpx.HTTPStatusError) -> None:
    try:
        detail = exc.response.json().get("detail", exc.response.text)
    except Exception:
        detail = exc.response.text
    raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc


def remote_get(path: str) -> dict[str, Any]:
    try:
        with remote_http_client(timeout=30) as client:
            response = client.get(f"{backend_url()}{path}", headers=backend_headers())
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise_remote_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接字幕后端: {exc}") from exc


def remote_get_with_timeout(path: str, timeout: float = 3.0) -> dict[str, Any]:
    try:
        with remote_http_client(timeout=timeout) as client:
            response = client.get(f"{backend_url()}{path}", headers=backend_headers())
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise_remote_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接字幕后端: {exc}") from exc


def remote_get_safe(path: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return remote_get(path), None
    except HTTPException as exc:
        return None, str(exc.detail)


def remote_post_json(path: str, payload: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    try:
        with remote_http_client(timeout=timeout) as client:
            response = client.post(f"{backend_url()}{path}", headers=backend_headers(), json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise_remote_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接字幕后端: {exc}") from exc


def remote_delete(path: str, timeout: float = 30) -> dict[str, Any]:
    try:
        with remote_http_client(timeout=timeout) as client:
            response = client.delete(f"{backend_url()}{path}", headers=backend_headers())
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise_remote_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接字幕后端: {exc}") from exc


def compute_settings_payload(settings_obj: Any, config: dict[str, Any] | None = None) -> dict[str, object]:
    config = config or {}
    def value(key: str, attr: str, default: object) -> object:
        if key in config:
            return config[key]
        return getattr(settings_obj, attr, default)

    return {
        "whisper_model": value("whisper_model", "default_model", "large-v3"),
        "whisper_model_dir": str(value("whisper_model_dir", "model_dir", "") or ""),
        "whisper_device": value("whisper_device", "device", "cuda"),
        "whisper_compute_type": value("whisper_compute_type", "compute_type", "float16"),
        "subtitle_max_workers": value("subtitle_max_workers", "max_workers", 1),
        "translation_max_workers": value("translation_max_workers", "translation_max_workers", 1),
        "subtitle_output_dir": str(value("subtitle_output_dir", "default_output_dir", "") or ""),
        "subtitle_path_map": config.get("subtitle_path_map", ""),
        "console_public_url": str(config.get("console_public_url", "") or console_public_url()),
        "default_translate_backend": value("default_translate_backend", "default_translate_backend", "google"),
        "google_translate_url": value("google_translate_url", "google_translate_url", "https://translate.google.com/translate_a/single"),
        "deepl_api_url": value("deepl_api_url", "deepl_api_url", "https://api-free.deepl.com/v2/translate"),
        "deepl_api_key": value("deepl_api_key", "deepl_api_key", ""),
        "openai_base_url": value("openai_base_url", "openai_base_url", ""),
        "openai_api_key": value("openai_api_key", "openai_api_key", ""),
        "openai_model": value("openai_model", "openai_model", "gpt-4.1-mini"),
        "openai_batch_size": value("openai_batch_size", "openai_batch_size", 12),
        "openai_max_concurrency": value("openai_max_concurrency", "openai_max_concurrency", 2),
        "openai_translation_style": value("openai_translation_style", "openai_translation_style", "adult_natural"),
        "openai_style_intensity": value("openai_style_intensity", "openai_style_intensity", "medium"),
        "openai_context_lines": value("openai_context_lines", "openai_context_lines", 2),
        "openai_glossary": value("openai_glossary", "openai_glossary", ""),
        "ollama_url": value("ollama_url", "ollama_url", ""),
        "ollama_model": value("ollama_model", "ollama_model", "qwen2.5:7b"),
        "subtitle_api_token": value("subtitle_api_token", "api_token", ""),
    }


SAVED_COMPUTE_SETTING_KEYS = {
    "whisper_model",
    "whisper_model_dir",
    "whisper_device",
    "whisper_compute_type",
    "subtitle_max_workers",
    "translation_max_workers",
    "subtitle_output_dir",
    "subtitle_path_map",
    "console_public_url",
    "default_translate_backend",
    "google_translate_url",
    "deepl_api_url",
    "deepl_api_key",
    "openai_base_url",
    "openai_api_key",
    "openai_model",
    "openai_batch_size",
    "openai_max_concurrency",
    "openai_translation_style",
    "openai_style_intensity",
    "openai_context_lines",
    "openai_glossary",
    "ollama_url",
    "ollama_model",
    "subtitle_api_token",
}


REMOTE_COMPUTE_SETTING_KEYS = {
    "whisper_model",
    "whisper_device",
    "whisper_compute_type",
    "subtitle_max_workers",
    "translation_max_workers",
    "default_translate_backend",
    "google_translate_url",
    "deepl_api_url",
    "deepl_api_key",
    "openai_base_url",
    "openai_api_key",
    "openai_model",
    "openai_batch_size",
    "openai_max_concurrency",
    "openai_translation_style",
    "openai_style_intensity",
    "openai_context_lines",
    "openai_glossary",
    "ollama_url",
    "ollama_model",
}


def remote_compute_settings_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in REMOTE_COMPUTE_SETTING_KEYS if key in payload}


def overlay_saved_console_settings(visible_settings: Any, console_config: dict[str, Any] | None = None) -> dict[str, object]:
    """Keep freshly saved console settings from being hidden by stale worker defaults."""
    base = dict(visible_settings) if isinstance(visible_settings, dict) else {}
    _, _, data_dir = settings()
    config = console_config if isinstance(console_config, dict) else load_compute_config(data_dir)
    console_settings = compute_settings_payload(load_subtitle_settings(data_dir), config)
    for key in SAVED_COMPUTE_SETTING_KEYS:
        if key in config and key in console_settings:
            base[key] = console_settings[key]
    return base or console_settings


def console_settings_payload() -> dict[str, object]:
    """Return saved console settings without requiring a live worker."""
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    saved_settings = load_subtitle_settings(data_dir)
    return redact_secret_settings({
        **compute_settings_payload(saved_settings, config),
        "default_model": saved_settings.default_model,
        "model_dir": str(saved_settings.model_dir) if saved_settings.model_dir else "",
        "device": saved_settings.device,
        "compute_type": saved_settings.compute_type,
        "max_workers": saved_settings.max_workers,
        "default_output_dir": str(saved_settings.default_output_dir) if saved_settings.default_output_dir else "",
        "translation_backends": translation_backend_options(saved_settings),
        "local_models": [],
    })


def save_local_compute_settings(payload: dict[str, Any]) -> dict[str, object]:
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    payload = restore_secret_placeholders(payload, config)
    config.update(payload)
    save_compute_config(data_dir, config)
    restarted = reset_subtitle_service_if_idle()
    if not restarted:
        print("[MovieMuse] settings saved; restart required after active jobs finish", flush=True)
    else:
        print("[MovieMuse] settings saved and reloaded", flush=True)
    return {"status": "ok", "restart_required": not restarted}


def save_console_compute_config(payload: dict[str, Any]) -> dict[str, Any]:
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    payload = restore_secret_placeholders(payload, config)
    config.update(payload)
    save_compute_config(data_dir, config)
    return config


app = FastAPI(title="媒体工具箱")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST), name="frontend-assets")
move_jobs: dict[str, dict[str, Any]] = {}
move_jobs_lock = threading.Lock()
console_sessions: dict[str, dict[str, Any]] = {}
console_sessions_lock = threading.RLock()
CONSOLE_SESSION_COOKIE = "moviemuse_session"
CONSOLE_SESSION_TTL = 7 * 24 * 60 * 60
CONSOLE_AUTH_OPEN_PREFIXES = (
    "/api/auth",
    "/api/subtitle/jobs",
    "/api/subtitle/upload",
    "/api/subtitle/node",
    "/api/subtitle/translate",
    "/api/transcode",
    "/api/compute",
    "/api/integrations/jellyfin",
    "/static",
    "/assets",
    "/docs",
    "/openapi.json",
)
CONSOLE_AUTH_OPEN_EXACT = {"/api/v1/message"}


def console_auth_required_path(path: str) -> bool:
    if path in CONSOLE_AUTH_OPEN_EXACT:
        return False
    if any(path.startswith(prefix) for prefix in CONSOLE_AUTH_OPEN_PREFIXES):
        return False
    if path.startswith("/api/postprocess/tasks/") and path.endswith("/worker-done"):
        return False
    return path.startswith("/api/")


@app.middleware("http")
async def console_auth_middleware(request: Request, call_next: Callable[[Request], Any]) -> Response:
    if console_auth_required_path(request.url.path) and not current_console_user(request):
        return Response('{"detail":"请先登录"}', status_code=401, media_type="application/json")
    return await call_next(request)


def frontend_index_response() -> FileResponse | None:
    frontend_index = FRONTEND_DIST / "index.html"
    if frontend_index.exists():
        return FileResponse(
            frontend_index,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return None


def frontend_app_response() -> FileResponse:
    frontend_index = frontend_index_response()
    if frontend_index:
        return frontend_index
    raise HTTPException(status_code=404, detail="MovieMuse frontend is not built. Run the frontend build first.")


transcode_jobs: dict[str, dict[str, Any]] = {}
transcode_jobs_lock = threading.Lock()


def move_result_payload(result: MoveResult) -> dict[str, object]:
    return {
        "source": str(result.source),
        "target": str(result.target),
        "status": result.status,
        "reason": result.reason,
        "mode": result.mode,
    }


def move_job_snapshot(job_id: str) -> dict[str, Any]:
    with move_jobs_lock:
        job = move_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="移动任务不存在")
        reconcile_move_job_locked(job)
        return json.loads(json.dumps(job, ensure_ascii=False))


def reconcile_move_job_locked(job: dict[str, Any]) -> None:
    if job.get("status") != "running" or job.get("processed"):
        return
    paths = [Path(path) for path in job.get("paths", []) if path]
    if not paths:
        return
    updated_at = float(job.get("updated_at") or job.get("created_at") or 0)
    if time.time() - updated_at < 3:
        return
    if all(not path.exists() for path in paths):
        total = int(job.get("total") or len(paths))
        job["processed"] = total
        job["moved"] = total
        job["skipped"] = 0
        job["failed"] = 0
        job["status"] = "completed"
        job["message"] = "文件已移动，进度已校准"
        job["current_path"] = ""
        job["updated_at"] = time.time()
        job["finished_at"] = time.time()


def create_move_job(paths: list[str]) -> str:
    unique_paths = list(dict.fromkeys(path for path in paths if path))
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "total": len(unique_paths),
        "processed": 0,
        "moved": 0,
        "skipped": 0,
        "failed": 0,
        "paths": unique_paths,
        "current_path": "",
        "message": "等待开始移动",
        "items": [],
        "created_at": time.time(),
        "updated_at": time.time(),
        "finished_at": None,
    }
    with move_jobs_lock:
        move_jobs[job_id] = job
    thread = threading.Thread(target=run_move_job, args=(job_id, unique_paths), daemon=True)
    thread.start()
    return job_id


def run_move_job(job_id: str, paths: list[str]) -> None:
    with move_jobs_lock:
        move_jobs[job_id]["status"] = "running"
        move_jobs[job_id]["updated_at"] = time.time()
        move_jobs[job_id]["message"] = "正在移动文件"
    try:
        media_dirs, trash_dir, data_dir = settings()
        store = Storage(data_dir, trash_dir, media_dirs)

        def on_progress(index: int, total: int, result: MoveResult) -> None:
            payload = move_result_payload(result)
            with move_jobs_lock:
                job = move_jobs[job_id]
                job["processed"] = index
                job["total"] = total
                job["current_path"] = str(result.source)
                job["message"] = result.reason
                job["updated_at"] = time.time()
                job["items"].append(payload)
                if result.status == "moved":
                    job["moved"] += 1
                elif result.status == "skipped":
                    job["skipped"] += 1
                elif result.status == "failed":
                    job["failed"] += 1

        store.move_to_trash([MoveRequest(source=Path(path)) for path in paths], on_progress=on_progress)
        with move_jobs_lock:
            job = move_jobs[job_id]
            job["status"] = "completed" if not job["failed"] else "failed"
            job["message"] = "移动完成" if not job["failed"] else "部分文件移动失败"
            job["updated_at"] = time.time()
            job["finished_at"] = time.time()
    except Exception as exc:
        with move_jobs_lock:
            job = move_jobs[job_id]
            job["status"] = "failed"
            job["message"] = str(exc)
            job["failed"] = job.get("failed", 0) or len(paths)
            job["updated_at"] = time.time()
            job["finished_at"] = time.time()
subtitle_service: SubtitleService | None = None


class SubtitleJobCreate(BaseModel):
    video_path: str = Field(..., description="本机路径、UNC 路径，或通过后端 SUBTITLE_PATH_MAP 映射的 Unraid 路径")
    output_dir: str | None = Field(default=None, description="字幕输出目录；为空则写到视频同目录")
    source_language: str | None = Field(default=None, description="原语言，例如 ja/en/zh；为空自动识别")
    target_language: str = Field(default="zh", description="目标语言，例如 zh/en")
    model: str | None = Field(default=None, description="Whisper 模型，例如 large-v3、medium")
    translate: bool = True
    translate_backend: str = "google"


class JellyfinIntegrationRequest(BaseModel):
    item_id: str = Field(default="", description="Jellyfin 媒体 item id")
    media_source_id: str = Field(default="", description="Jellyfin MediaSource id；为空使用第一个带路径的 source")
    title: str = Field(default="", description="当前页面标题，作为任务名回退")
    path: str = Field(default="", description="可选直接传入的媒体路径；正常由后端向 Jellyfin 查询")
    target_codec: str | None = Field(default=None, description="转码目标编码；为空使用后处理设置")


def get_subtitle_service() -> SubtitleService:
    global subtitle_service
    if subtitle_service is None:
        _, _, data_dir = settings()
        subtitle_service = SubtitleService(load_subtitle_settings(data_dir))
    return subtitle_service


def reset_subtitle_service_if_idle() -> bool:
    global subtitle_service
    if subtitle_service is None:
        return True
    active = [
        job
        for job in subtitle_service.list_jobs()
        if job.status in {"queued", "running"}
    ]
    if active:
        return False
    subtitle_service = None
    return True


def require_subtitle_token(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    expected = frontend_api_token()
    if not expected:
        return
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if x_api_key != expected and bearer != expected:
        raise HTTPException(status_code=401, detail="字幕 API token 不正确")


def job_payload(job: SubtitleJob | dict[str, Any]) -> dict[str, object]:
    if isinstance(job, dict):
        return job
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "video_path": job.video_path,
        "output_dir": job.output_dir,
        "source_language": job.source_language,
        "target_language": job.target_language,
        "detected_language": job.detected_language,
        "model": job.model,
        "translate": job.translate,
        "translate_backend": job.translate_backend,
        "duration": job.duration,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "original_srt": job.original_srt,
        "translated_srt": job.translated_srt,
        "bilingual_srt": job.bilingual_srt,
        "original_vtt": job.original_vtt,
        "translated_vtt": job.translated_vtt,
        "error": job.error,
    }


def subtitle_jobs_cache_path(data_dir: Path) -> Path:
    return data_dir / SUBTITLE_REMOTE_JOBS_CACHE_FILE


def normalize_subtitle_job_items(jobs: Any) -> list[dict[str, Any]]:
    if not isinstance(jobs, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in jobs:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or "").strip()
        if not job_id:
            continue
        normalized.append(dict(item))
    return normalized


def save_subtitle_jobs_cache(jobs: Any) -> dict[str, Any]:
    _, _, data_dir = settings()
    normalized = normalize_subtitle_job_items(jobs)
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "jobs": normalized,
        "total": len(normalized),
        "updated_at": time.time(),
    }
    target = subtitle_jobs_cache_path(data_dir)
    tmp = target.with_suffix(f"{target.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return payload


def load_subtitle_jobs_cache(limit: int | None = None) -> dict[str, Any]:
    _, _, data_dir = settings()
    target = subtitle_jobs_cache_path(data_dir)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        payload = {}
    jobs = normalize_subtitle_job_items(payload.get("jobs") if isinstance(payload, dict) else [])
    active = [job for job in jobs if job.get("status") in {"queued", "running", "translating"}]
    visible_jobs = jobs[:limit] if limit and limit > 0 else jobs
    return {
        "jobs": visible_jobs,
        "total": len(jobs),
        "active": len(active),
        "cached": True,
        "cache_updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
    }


def upsert_subtitle_job_cache(job: Any) -> None:
    if not isinstance(job, dict) or not str(job.get("id") or "").strip():
        return
    cached = load_subtitle_jobs_cache(limit=0)
    jobs = [dict(item) for item in cached.get("jobs", []) if isinstance(item, dict)]
    job_id = str(job.get("id"))
    updated = False
    for index, item in enumerate(jobs):
        if str(item.get("id") or "") == job_id:
            jobs[index] = dict(job)
            updated = True
            break
    if not updated:
        jobs.insert(0, dict(job))
    save_subtitle_jobs_cache(jobs)


def subtitle_job_from_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    nested = result.get("job")
    if isinstance(nested, dict):
        return nested
    if str(result.get("id") or "").strip():
        return result
    return None


def remove_subtitle_job_cache(job_id: str) -> None:
    cached = load_subtitle_jobs_cache(limit=0)
    jobs = [
        dict(item)
        for item in cached.get("jobs", [])
        if isinstance(item, dict) and str(item.get("id") or "") != str(job_id)
    ]
    save_subtitle_jobs_cache(jobs)


def subtitle_jobs_payload_from_remote_or_cache(limit: int = 0) -> dict[str, Any]:
    suffix = f"?limit={limit}" if limit else "?limit=0"
    try:
        payload = remote_get(f"/api/subtitle/jobs{suffix}")
        jobs = normalize_subtitle_job_items(payload.get("jobs") if isinstance(payload, dict) else [])
        save_subtitle_jobs_cache(jobs)
        active = [job for job in jobs if job.get("status") in {"queued", "running", "translating"}]
        return {
            **(payload if isinstance(payload, dict) else {}),
            "jobs": jobs[:limit] if limit and limit > 0 else jobs,
            "total": len(jobs),
            "active": len(active),
            "cached": False,
        }
    except HTTPException as exc:
        cached = load_subtitle_jobs_cache(limit or None)
        cached["backend_error"] = str(exc.detail)
        return cached


def scan_file_payload(file: Any) -> dict[str, object]:
    return {
        "path": str(file.path),
        "name": file.path.name,
        "title": file.title,
        "year": file.year,
        "size_bytes": file.size_bytes,
        "size_label": file.size_label,
        "resolution": file.resolution,
        "source_tag": file.source_tag,
        "uncensored": file.uncensored,
        "ignored": file.ignored,
        "subtitle_kind": file.subtitle_kind,
        "subtitle_label": file.subtitle_label,
        "srt_count": file.srt_count,
        "srt_label": file.srt_label,
        "subtitles": [str(sub.path) for sub in file.subtitles],
        "group_key": file.group_key,
        "group_source": file.group_source,
        "cover_path": str(file.cover_path) if file.cover_path else None,
    }


def scan_group_payload(group: Any) -> dict[str, object]:
    return {
        "key": group.key,
        "title": group.title,
        "year": group.year,
        "source": group.source,
        "cover_path": str(group.cover_path) if group.cover_path else None,
        "files": [scan_file_payload(file) for file in group.files],
    }


def submit_subtitle_job_for_path(path: str) -> dict[str, object]:
    defaults = current_subtitle_job_defaults()
    return submit_subtitle_job(
        video_path=path,
        output_dir=defaults["output_dir"],
        source_language=defaults["source_language"],
        target_language=defaults["target_language"],
        model=defaults["model"],
        translate=defaults["translate"],
        translate_backend=defaults["translate_backend"],
    )


def subtitle_job_payload_for_path(path: str) -> dict[str, object]:
    defaults = current_subtitle_job_defaults()
    return {
        "video_path": path,
        "output_dir": defaults["output_dir"],
        "source_language": defaults["source_language"],
        "target_language": defaults["target_language"],
        "model": defaults["model"],
        "translate": defaults["translate"],
        "translate_backend": defaults["translate_backend"],
    }


def current_subtitle_job_defaults() -> dict[str, Any]:
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    settings_payload: dict[str, Any] = config
    if backend_url():
        remote_status, _ = remote_get_safe("/api/subtitle/node/status")
        if remote_status and isinstance(remote_status.get("settings"), dict):
            settings_payload = {**remote_status["settings"], **config}
    elif subtitle_service is not None:
        settings_payload = compute_settings_payload(subtitle_service.settings, config)
    translate_backend = str(settings_payload.get("default_translate_backend") or "google")
    return {
        "output_dir": settings_payload.get("subtitle_output_dir") or None,
        "source_language": None,
        "target_language": "zh",
        "model": settings_payload.get("whisper_model") or settings_payload.get("default_model") or None,
        "translate": translate_backend != "none",
        "translate_backend": translate_backend,
    }


def submit_subtitle_job(
    video_path: str,
    output_dir: str | None = None,
    source_language: str | None = None,
    target_language: str = "zh",
    model: str | None = None,
    translate: bool = True,
    translate_backend: str = "google",
) -> dict[str, object]:
    payload = {
        "video_path": video_path,
        "output_dir": output_dir or None,
        "source_language": source_language or None,
        "target_language": target_language or "zh",
        "model": model or None,
        "translate": translate,
        "translate_backend": translate_backend or "google",
    }
    if backend_url():
        result = remote_post_json("/api/subtitle/jobs", rewrite_subtitle_payload(payload))
        upsert_subtitle_job_cache(subtitle_job_from_result(result))
        return result
    service = get_subtitle_service()
    job = service.create_job(**payload)
    return job_payload(job)


def submit_subtitle_jobs_bulk(payloads: list[dict[str, object]]) -> dict[str, object]:
    if not payloads:
        return {"status": "ok", "submitted": 0, "jobs": []}
    if backend_url():
        rewritten = [rewrite_subtitle_payload(dict(payload)) for payload in payloads]
        result = remote_post_json("/api/subtitle/jobs/bulk", {"jobs": rewritten}, timeout=120)
        if isinstance(result, dict):
            for item in result.get("jobs", []) or []:
                upsert_subtitle_job_cache(item)
        return result
    service = get_subtitle_service()
    jobs = service.create_jobs([dict(payload) for payload in payloads])
    return {"status": "ok", "submitted": len(jobs), "jobs": [job_payload(job) for job in jobs]}


def submit_subtitle_jobs_bulk_background(payloads: list[dict[str, object]]) -> None:
    try:
        result = submit_subtitle_jobs_bulk(payloads)
        print(
            f"[MovieMuse] bulk subtitle submission accepted submitted={result.get('submitted', 0)}",
            flush=True,
        )
    except Exception as exc:
        print(f"[MovieMuse] bulk subtitle submission failed: {exc}", flush=True)


def subtitle_batch_dir(data_dir: Path) -> Path:
    path = data_dir / "pending_subtitle_batches"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_pending_subtitle_batch(data_dir: Path, paths: list[str]) -> str:
    batch_id = uuid.uuid4().hex
    unique_paths = list(dict.fromkeys(path for path in paths if path))
    payload = {
        "id": batch_id,
        "created_at": time.time(),
        "paths": unique_paths,
    }
    (subtitle_batch_dir(data_dir) / f"{batch_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return batch_id


def load_pending_subtitle_batch(data_dir: Path, batch_id: str | None) -> dict[str, object] | None:
    if not batch_id:
        return None
    path = subtitle_batch_dir(data_dir) / f"{batch_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload.get("paths"), list):
        return None
    return payload


def memory_summary() -> dict[str, object]:
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            total = int(status.ullTotalPhys)
            available = int(status.ullAvailPhys)
            return {
                "total_bytes": total,
                "available_bytes": available,
                "used_percent": int(status.dwMemoryLoad),
                "label": f"{total / (1024 ** 3):.0f} GB",
            }
        except Exception:
            pass

    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        values: dict[str, int] = {}
        for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        used_percent = round((1 - available / total) * 100) if total else 0
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_percent": used_percent,
            "label": f"{total / (1024 ** 3):.0f} GB" if total else "未知",
        }

    return {"total_bytes": 0, "available_bytes": 0, "used_percent": 0, "label": "未知"}


def gpu_summary() -> list[dict[str, object]]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total,memory.used,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            check=True,
        )
    except Exception:
        return []

    gpus: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        total = int(float(parts[1])) if parts[1].replace(".", "", 1).isdigit() else 0
        used = int(float(parts[2])) if parts[2].replace(".", "", 1).isdigit() else 0
        gpus.append(
            {
                "name": parts[0],
                "memory_total_mb": total,
                "memory_used_mb": used,
                "driver": parts[3],
                "label": f"{parts[0]} · {total / 1024:.0f} GB",
            }
        )
    return gpus


def transcode_job_snapshot(job_id: str) -> dict[str, Any]:
    with transcode_jobs_lock:
        job = transcode_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="转码任务不存在")
        return json.loads(json.dumps(job, ensure_ascii=False))


def transcode_jobs_payload(limit: int | None = None) -> list[dict[str, Any]]:
    with transcode_jobs_lock:
        jobs = sorted(transcode_jobs.values(), key=lambda item: float(item.get("created_at") or 0), reverse=True)
        if limit:
            jobs = jobs[: max(1, min(500, int(limit)))]
        return json.loads(json.dumps(jobs, ensure_ascii=False))


def set_transcode_job(job_id: str, **fields: Any) -> dict[str, Any]:
    with transcode_jobs_lock:
        job = transcode_jobs.get(job_id)
        if not job:
            raise RuntimeError(f"转码任务不存在: {job_id}")
        job.update(fields)
        job["updated_at"] = time.time()
        return json.loads(json.dumps(job, ensure_ascii=False))


FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
FFMPEG_TIME_RE = re.compile(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
FFMPEG_FRAME_RE = re.compile(r"frame=\s*(\d+)")
FFMPEG_FPS_RE = re.compile(r"fps=\s*([0-9.]+)")
FFMPEG_SPEED_RE = re.compile(r"speed=\s*([0-9.]+x|N/A)")
FFMPEG_BITRATE_RE = re.compile(r"bitrate=\s*([^\s]+)")
FFMPEG_SIZE_RE = re.compile(r"size=\s*([^\s]+)")


def ffmpeg_timestamp_seconds(match: re.Match[str] | None) -> float:
    if not match:
        return 0.0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def clamp_progress(value: float) -> float:
    return max(0.0, min(1.0, value))


def format_duration_seconds(value: float) -> str:
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_ffmpeg_progress_line(line: str, duration: float) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    duration_match = FFMPEG_DURATION_RE.search(line)
    if duration_match:
        parsed_duration = ffmpeg_timestamp_seconds(duration_match)
        if parsed_duration > 0:
            fields["duration"] = parsed_duration
            duration = parsed_duration
    time_match = FFMPEG_TIME_RE.search(line)
    if time_match:
        processed = ffmpeg_timestamp_seconds(time_match)
        fields["processed_seconds"] = processed
        if duration > 0:
            fields["progress"] = clamp_progress(processed / duration)
            fields["progress_percent"] = round(fields["progress"] * 100, 1)
    patterns = {
        "frame": FFMPEG_FRAME_RE,
        "fps": FFMPEG_FPS_RE,
        "speed": FFMPEG_SPEED_RE,
        "bitrate": FFMPEG_BITRATE_RE,
        "size": FFMPEG_SIZE_RE,
    }
    for key, pattern in patterns.items():
        match = pattern.search(line)
        if match:
            value: Any = match.group(1)
            if key == "frame":
                value = int(value)
            elif key == "fps":
                value = float(value)
            fields[key] = value
    if fields:
        fields["last_progress_line"] = line
    return fields


def read_process_stream(stream: Any, chunks: "queue.Queue[str | None]") -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    stream_is_binary = False
    try:
        while True:
            chunk = stream.read(1)
            if not chunk:
                break
            if isinstance(chunk, bytes):
                stream_is_binary = True
                chunk = decoder.decode(chunk)
                if not chunk:
                    continue
            chunks.put(chunk)
        if stream_is_binary:
            tail = decoder.decode(b"", final=True)
            if tail:
                chunks.put(tail)
    finally:
        try:
            stream.close()
        except Exception:
            pass
        chunks.put(None)


def transcode_progress_message(job: dict[str, Any]) -> str:
    parts: list[str] = []
    processed = float(job.get("processed_seconds") or 0)
    duration = float(job.get("duration") or 0)
    if processed and duration:
        parts.append(f"{format_duration_seconds(processed)} / {format_duration_seconds(duration)}")
    if job.get("fps") not in (None, ""):
        parts.append(f"{job.get('fps')} fps")
    if job.get("speed"):
        parts.append(str(job.get("speed")))
    if job.get("frame"):
        parts.append(f"frame {job.get('frame')}")
    return " · ".join(parts) or str(job.get("last_progress_line") or "")


def append_tail(current: str, chunk: str, limit: int = 4000) -> str:
    return f"{current}{chunk}"[-limit:]


def normalize_target_codec(value: Any) -> str:
    codec = str(value or "av1").strip().lower()
    if codec in {"h265", "hevc", "x265"}:
        return "h265"
    return "av1"


def default_encoder_for_codec(target_codec: str) -> str:
    codec = normalize_target_codec(target_codec)
    if codec == "av1":
        return os.getenv("TRANSCODE_AV1_ENCODER", "av1_nvenc")
    return os.getenv("TRANSCODE_H265_ENCODER", "libx265")


def encoder_codec_family(encoder: str) -> str:
    value = str(encoder or "").strip().lower()
    if not value:
        return ""
    if "av1" in value or value in {"libsvtav1", "libaom-av1"}:
        return "av1"
    if "hevc" in value or "h265" in value or "x265" in value:
        return "h265"
    return ""


def encoder_matches_codec(target_codec: str, encoder: str) -> bool:
    family = encoder_codec_family(encoder)
    return not family or family == normalize_target_codec(target_codec)


def normalize_transcode_settings_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload or {})
    codec = normalize_target_codec(normalized.get("target_codec"))
    encoder = str(normalized.get("target_encoder") or "").strip()
    if not encoder or not encoder_matches_codec(codec, encoder):
        encoder = default_encoder_for_codec(codec)
    normalized["target_codec"] = codec
    normalized["target_encoder"] = encoder
    return normalized


def transcode_encoder(target_codec: str, target_encoder: str = "") -> str:
    custom = str(target_encoder or "").strip()
    if custom and encoder_matches_codec(target_codec, custom):
        return custom
    return default_encoder_for_codec(target_codec)


def transcode_quality_flag(encoder: str) -> str:
    if encoder in {"av1_qsv", "hevc_qsv"}:
        return "-global_quality"
    return "-cq" if encoder.endswith("_nvenc") or encoder in {"av1_nvenc", "hevc_nvenc"} else "-crf"


def build_ffmpeg_preview(settings_payload: dict[str, Any], input_path: str = "<输入文件>", output_path: str = "<输出文件>") -> str:
    settings_payload = normalize_transcode_settings_payload(settings_payload)
    target_codec = str(settings_payload.get("target_codec") or "av1")
    encoder = str(settings_payload.get("target_encoder") or transcode_encoder(target_codec))
    crf = str(settings_payload.get("crf") or 36)
    preset = str(settings_payload.get("preset") or "p1")
    preset_flag = str(settings_payload.get("preset_flag") or "-preset")
    quality_flag = transcode_quality_flag(encoder)
    return f'ffmpeg -hide_banner -nostdin -i "{input_path}" -c:v {encoder} {preset_flag} {preset} {quality_flag} {crf} -c:a copy "{output_path}" -y'


def transcode_ffmpeg_command(job: dict[str, Any]) -> list[str]:
    job = normalize_transcode_settings_payload(job)
    ffmpeg = os.getenv("FFMPEG_BIN", "ffmpeg")
    input_path = str(job.get("input_path") or "")
    output_path = str(job.get("output_path") or "")
    target_codec = str(job.get("target_codec") or "av1")
    encoder = transcode_encoder(target_codec, str(job.get("target_encoder") or ""))
    crf = str(job.get("crf") or 36)
    preset = str(job.get("preset") or "p1")
    preset_flag = str(job.get("preset_flag") or "-preset")
    quality_flag = transcode_quality_flag(encoder)
    custom_template = str(job.get("ffmpeg_custom_template") or "").strip()
    if bool(job.get("ffmpeg_custom_enabled")) and custom_template:
        try:
            rendered = custom_template.format(
                input=input_path,
                output=output_path,
                encoder=encoder,
                preset=preset,
                preset_flag=preset_flag,
                quality_flag=quality_flag,
                quality=crf,
                crf=crf,
            )
        except KeyError as exc:
            raise RuntimeError(f"自定义 FFmpeg 模板变量不存在: {exc}") from exc
        return shlex.split(rendered)
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-i",
        input_path,
        "-map",
        "0",
        "-c:v",
        encoder,
        preset_flag,
        preset,
        quality_flag,
        crf,
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        output_path,
        "-y",
    ]
    extra = os.getenv("TRANSCODE_FFMPEG_EXTRA", "").strip()
    if extra:
        command = command[:-1] + extra.split() + [output_path]
    return command


def create_transcode_job(payload: dict[str, Any], *, start: bool = True) -> dict[str, Any]:
    payload = normalize_transcode_settings_payload(payload)
    input_path = str(payload.get("input_path") or payload.get("video_path") or "").strip()
    output_path = str(payload.get("output_path") or "").strip()
    if not input_path:
        raise HTTPException(status_code=400, detail="缺少 input_path")
    if not output_path:
        raise HTTPException(status_code=400, detail="缺少 output_path")
    job_id = str(payload.get("job_id") or uuid.uuid4().hex)
    ffmpeg_mode = str(payload.get("ffmpeg_mode") or "").strip().lower()
    if ffmpeg_mode not in {"standard", "custom"}:
        ffmpeg_mode = "custom" if bool(payload.get("ffmpeg_custom_enabled")) else "standard"
    job = {
        "id": job_id,
        "task_id": str(payload.get("task_id") or ""),
        "av_id": str(payload.get("av_id") or ""),
        "status": "queued",
        "input_path": input_path,
        "output_path": output_path,
        "target_codec": str(payload.get("target_codec") or "av1"),
        "target_encoder": str(payload.get("target_encoder") or ""),
        "crf": int(payload.get("crf") or 36),
        "preset": str(payload.get("preset") or "p1"),
        "preset_flag": str(payload.get("preset_flag") or "-preset"),
        "ffmpeg_mode": ffmpeg_mode,
        "ffmpeg_standard_enabled": ffmpeg_mode == "standard",
        "ffmpeg_custom_enabled": ffmpeg_mode == "custom",
        "ffmpeg_custom_template": str(payload.get("ffmpeg_custom_template") or ""),
        "ffmpeg_standard_command": str(payload.get("ffmpeg_standard_command") or ""),
        "callback_url": str(payload.get("callback_url") or ""),
        "callback_token": str(payload.get("callback_token") or ""),
        "error": "",
        "command": [],
        "stderr_tail": "",
        "last_progress_line": "",
        "message": "等待启动",
        "progress": 0,
        "progress_percent": 0,
        "duration": 0,
        "processed_seconds": 0,
        "frame": 0,
        "fps": 0,
        "speed": "",
        "bitrate": "",
        "size": "",
        "returncode": None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "started_at": 0,
        "finished_at": 0,
    }
    with transcode_jobs_lock:
        transcode_jobs[job_id] = job
    if start:
        start_transcode_job(job_id)
    return transcode_job_snapshot(job_id)


def start_transcode_job(job_id: str) -> None:
    threading.Thread(target=run_transcode_job_background, args=(job_id,), daemon=True).start()


def run_transcode_job_background(job_id: str) -> None:
    callback_payload: dict[str, Any] = {}
    try:
        job = set_transcode_job(job_id, status="running", started_at=time.time(), message="正在准备 ffmpeg")
        output_path = Path(str(job.get("output_path") or ""))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration = probe_video_duration(str(job.get("input_path") or ""))
        command = transcode_ffmpeg_command(job)
        set_transcode_job(job_id, command=command, duration=duration, message="ffmpeg 已启动")
        timeout = int(os.getenv("TRANSCODE_TIMEOUT_SECONDS", "43200"))
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        chunks: queue.Queue[str | None] = queue.Queue()
        if process.stderr:
            threading.Thread(target=read_process_stream, args=(process.stderr, chunks), daemon=True).start()
        stderr_tail = ""
        line_buffer = ""
        deadline = time.time() + timeout if timeout > 0 else 0
        while True:
            if deadline and time.time() > deadline and process.poll() is None:
                process.kill()
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                chunk = chunks.get(timeout=0.5)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if chunk is None:
                if process.poll() is not None:
                    break
                continue
            stderr_tail = append_tail(stderr_tail, chunk)
            if chunk in {"\r", "\n"}:
                line = line_buffer.strip()
                line_buffer = ""
                if not line:
                    set_transcode_job(job_id, stderr_tail=stderr_tail)
                    continue
                progress_fields = parse_ffmpeg_progress_line(line, duration)
                if progress_fields.get("duration"):
                    duration = float(progress_fields["duration"])
                update_fields = {"stderr_tail": stderr_tail, "last_progress_line": line}
                update_fields.update(progress_fields)
                if progress_fields:
                    temp_job = {**job, **update_fields}
                    update_fields["message"] = transcode_progress_message(temp_job)
                set_transcode_job(job_id, **update_fields)
            else:
                line_buffer += chunk
        if line_buffer.strip():
            line = line_buffer.strip()
            progress_fields = parse_ffmpeg_progress_line(line, duration)
            update_fields = {"stderr_tail": stderr_tail, "last_progress_line": line}
            update_fields.update(progress_fields)
            if progress_fields:
                temp_job = {**job, **update_fields}
                update_fields["message"] = transcode_progress_message(temp_job)
            set_transcode_job(job_id, **update_fields)
        returncode = process.wait()
        if returncode != 0:
            message = f"ffmpeg 退出码 {returncode}"
            job = set_transcode_job(
                job_id,
                status="failed",
                error=message,
                stderr_tail=stderr_tail,
                message=message,
                returncode=returncode,
                finished_at=time.time(),
            )
            callback_payload = {"status": "failed", "job_id": job_id, "error": message, "stderr_tail": stderr_tail}
        else:
            job = set_transcode_job(
                job_id,
                status="worker_done",
                stderr_tail=stderr_tail,
                progress=1,
                progress_percent=100,
                message="转码完成",
                returncode=returncode,
                finished_at=time.time(),
            )
            callback_payload = {
                "status": "worker_done",
                "job_id": job_id,
                "output_path": job.get("output_path", ""),
                "input_path": job.get("input_path", ""),
                "console_output_path": job.get("console_output_path", ""),
                "console_input_path": job.get("console_input_path", ""),
                "target_codec": job.get("target_codec", ""),
                "progress": job.get("progress", 1),
                "progress_percent": job.get("progress_percent", 100),
            }
    except Exception as exc:
        try:
            job = set_transcode_job(job_id, status="failed", error=str(exc), message=str(exc), finished_at=time.time())
        except Exception:
            job = {"callback_url": ""}
        callback_payload = {"status": "failed", "job_id": job_id, "error": str(exc)}
    callback_url = str((job or {}).get("callback_url") or "")
    if callback_url:
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                client.post(callback_url, headers=postprocess_callback_headers(job), json=callback_payload)
        except Exception as exc:
            try:
                set_transcode_job(job_id, callback_error=str(exc))
            except Exception:
                pass
    else:
        task_id = str((job or {}).get("task_id") or "")
        if task_id:
            try:
                post = get_postprocess_service()
                current_task = post.get_task(task_id)
                if postprocess_task_is_terminal(current_task):
                    post.add_event(task_id, "info", "worker_callback_ignored", "本地转码完成但任务已终止，忽略回调", callback_payload)
                    return
                if callback_payload.get("status") == "worker_done":
                    validate_and_activate_postprocess_task(
                        task_id,
                        output_path=str(callback_payload.get("output_path") or ""),
                        worker_result=callback_payload,
                    )
                else:
                    post.update_task(
                        task_id,
                        status="failed",
                        error_code="worker_failed",
                        error_message=str(callback_payload.get("error") or "本地转码失败"),
                        data={"worker_done": callback_payload},
                    )
                    post.add_event(task_id, "error", "worker_done", "本地转码任务失败", callback_payload)
            except Exception as exc:
                try:
                    set_transcode_job(job_id, callback_error=str(exc))
                except Exception:
                    pass


def apply_path_pairs(value: str | None, pairs: list[tuple[str, str]]) -> str | None:
    if not value:
        return value
    normalized = value.replace("\\", "/")
    for source, target in pairs:
        clean_source = source.replace("\\", "/").rstrip("/")
        clean_target = target.rstrip("\\/").replace("\\", "/")
        if normalized == clean_source or normalized.startswith(clean_source + "/"):
            suffix = normalized[len(clean_source) :].lstrip("/")
            return f"{clean_target}/{suffix}" if suffix else clean_target
    return value


def remote_status_path_map(remote_status: dict[str, Any] | None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if not remote_status:
        return pairs
    for pair in remote_status.get("path_map", []) or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            pairs.append((str(pair[0]), str(pair[1])))
    return pairs


def backend_path_preview(sample_path: str | None = None, remote_status: dict[str, Any] | None = None) -> dict[str, object]:
    media_dirs, _, _ = settings()
    raw_path = sample_path or (str(media_dirs[0] / "电影/example.mkv") if media_dirs else "/media/电影/example.mkv")
    console_pairs = parse_proxy_path_map()
    console_output = apply_path_pairs(raw_path, console_pairs) or raw_path
    backend_pairs = remote_status_path_map(remote_status)
    backend_output = apply_path_pairs(console_output, backend_pairs) or console_output
    return {
        "input": raw_path,
        "console_output": console_output,
        "backend_output": backend_output,
        "console_pairs": console_pairs,
        "backend_pairs": backend_pairs,
    }


def local_node_status() -> dict[str, object]:
    service = get_subtitle_service()
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    compute_settings = compute_settings_payload(service.settings, config)
    jobs = [job_payload(job) for job in service.list_jobs()]
    active_jobs = [job for job in jobs if job.get("status") in {"queued", "running", "translating"}]
    all_transcode_items = transcode_jobs_payload()
    transcode_items = all_transcode_items[:10]
    active_transcode = [job for job in all_transcode_items if job.get("status") in {"queued", "running"}]
    return {
        "status": "ok",
        "online": True,
        "mode": "local",
        "settings": redact_secret_settings({
            **compute_settings,
            "default_model": service.settings.default_model,
            "model_dir": str(service.settings.model_dir) if service.settings.model_dir else "",
            "device": service.settings.device,
            "compute_type": service.settings.compute_type,
            "max_workers": service.settings.max_workers,
            "default_output_dir": str(service.settings.default_output_dir) if service.settings.default_output_dir else "",
            "translation_backends": translation_backend_options(service.settings),
            "local_models": local_model_dirs(service.settings.model_dir),
        }),
        "hardware": {
            "cpu": platform.processor() or platform.machine() or "未知 CPU",
            "cpu_count": os.cpu_count(),
            "memory": memory_summary(),
            "gpus": gpu_summary(),
            "platform": platform.platform(),
        },
        "jobs": {
            "total": len(jobs),
            "active": len(active_jobs),
            "items": jobs[:10],
        },
        "transcode_jobs": {
            "total": len(all_transcode_items),
            "active": len(active_transcode),
            "items": transcode_items,
        },
        "path_map": service.settings.path_map,
        "updated_at": time.time(),
    }


def offline_backend_status(error: str) -> dict[str, object]:
    cached_jobs = load_subtitle_jobs_cache(limit=10)
    return {
        "status": "offline",
        "online": False,
        "mode": "remote",
        "backend_url": backend_url(),
        "error": error,
        "settings": console_settings_payload(),
        "hardware": None,
        "jobs": {
            "total": cached_jobs.get("total", 0),
            "active": cached_jobs.get("active", 0),
            "items": cached_jobs.get("jobs", []),
            "cached": True,
            "cache_updated_at": cached_jobs.get("cache_updated_at"),
        },
        "path_map": [],
        "updated_at": time.time(),
    }


def subtitle_backend_status() -> dict[str, object]:
    if not backend_url():
        status = local_node_status()
        status["mode"] = "local"
        status["backend_url"] = ""
        return status
    try:
        status = remote_get_with_timeout("/api/subtitle/node/status", timeout=8.0)
        if isinstance(status.get("settings"), dict):
            status["settings"] = redact_secret_settings(status.get("settings"))
        status["online"] = True
        status["mode"] = "remote"
        status["backend_url"] = backend_url()
        return status
    except HTTPException as exc:
        return offline_backend_status(str(exc.detail))


def subtitle_console_payload() -> dict[str, object]:
    _, _, data_dir = settings()
    console_config = load_compute_config(data_dir)
    status = subtitle_backend_status()
    jobs: list[Any] = []
    backend_error = None
    if backend_url():
        payload = subtitle_jobs_payload_from_remote_or_cache(limit=0)
        jobs = list(payload.get("jobs", []))
        backend_error = str(payload.get("backend_error") or status.get("error") or "") or None
    else:
        jobs = [job_payload(job) for job in get_subtitle_service().list_jobs()]
    visible_settings = redact_secret_settings(overlay_saved_console_settings(status.get("settings"), console_config))
    return {
        "connection": {
            "subtitle_backend_url": backend_url() or str(console_config.get("subtitle_backend_url", "")),
            "subtitle_backend_token": SECRET_PLACEHOLDER if str(console_config.get("subtitle_backend_token", "")) else "",
        },
        "backend_status": status,
        "backend_error": backend_error,
        "jobs": jobs,
        "compute_settings": visible_settings,
        "path_preview": backend_path_preview(remote_status=status if status.get("online") else None),
        "translation_backends": (visible_settings or {}).get("translation_backends")
        or translation_backend_options(None),
        "model_options": whisper_model_options(),
    }


def dashboard_time(value: Any) -> str:
    try:
        ts = float(value or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return "暂无"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def dashboard_week_count(items: list[dict[str, Any]], key: str, start: float, end: float) -> int:
    total = 0
    for item in items:
        try:
            ts = float(item.get(key) or 0)
        except (TypeError, ValueError):
            ts = 0
        if start <= ts < end:
            total += 1
    return total


def dashboard_trend(current: int, previous: int) -> dict[str, str]:
    delta = current - previous
    if previous <= 0:
        if current > 0:
            return {"text": f"本周 +{current}", "tone": "up"}
        return {"text": "近 7 天持平", "tone": "flat"}
    percent = round(delta / previous * 100)
    if delta > 0:
        return {"text": f"较上周 +{percent}%", "tone": "up"}
    if delta < 0:
        return {"text": f"较上周 {percent}%", "tone": "down"}
    return {"text": "较上周持平", "tone": "flat"}


def task_label(task_id: str) -> str:
    labels = {
        "actress_poll": "女优订阅轮询",
        "av_download": "番号订阅下载",
        "wash_download": "洗版轮询",
        "postprocess_qb": "后处理下载检查",
        "maker_refresh": "厂牌发售更新",
        "ranking_refresh": "DMM/FANZA 榜单更新",
        "asset_maintenance": "资产缓存维护",
    }
    return labels.get(task_id, task_id or "自动任务")


def task_result_summary(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    if "checked" in result:
        parts.append(f"检查 {result.get('checked')} 个")
    if "added" in result:
        added = result.get("added")
        parts.append(f"新增 {len(added) if isinstance(added, list) else added} 个")
    if "skipped" in result:
        skipped = result.get("skipped")
        parts.append(f"跳过 {len(skipped) if isinstance(skipped, list) else skipped} 个")
    if "errors" in result:
        errors = result.get("errors")
        parts.append(f"错误 {len(errors) if isinstance(errors, list) else errors} 个")
    if "processed" in result:
        parts.append(f"处理 {result.get('processed')} 个")
    if "refreshed" in result:
        refreshed = result.get("refreshed")
        parts.append(f"刷新 {len(refreshed) if isinstance(refreshed, list) else refreshed} 个")
    return " / ".join(parts) or str(result)[:80]


def dashboard_payload() -> dict[str, Any]:
    media_dirs, trash_dir, data_dir = settings()
    scan_cache.configure(data_dir)
    snapshot = scan_cache.snapshot()
    result = snapshot.result or ScanResult(tuple(), 0, 0, tuple(media_dirs), tuple(), tuple())
    service = get_subscription_service()
    avs = service.get_subscribed_av()
    actresses = service.get_subscribed_actresses()
    settings_data = service.get_settings()
    system_data = get_system_settings_service().get()
    post = get_postprocess_service()
    post_settings = post.get_settings()
    post_tasks = post.list_tasks(limit=200)
    logs = get_app_log_service().recent(120)

    now = time.time()
    week = 7 * 24 * 60 * 60
    current_week = (now - week, now)
    previous_week = (now - week * 2, now - week)
    av_week = dashboard_week_count(avs, "subscribed_at", *current_week)
    av_prev = dashboard_week_count(avs, "subscribed_at", *previous_week)
    actress_week = dashboard_week_count(actresses, "subscribed_at", *current_week)
    actress_prev = dashboard_week_count(actresses, "subscribed_at", *previous_week)
    error_week = dashboard_week_count([item for item in logs if item.get("level") == "error"], "ts", *current_week)
    error_prev = dashboard_week_count([item for item in logs if item.get("level") == "error"], "ts", *previous_week)

    pending = sum(1 for item in avs if item.get("status", "pending") == "pending")
    done = sum(1 for item in avs if item.get("status") == "done")
    in_library = sum(1 for item in avs if item.get("status") == "in_library" or item.get("library_status") == "in_library")
    active_actresses = sum(1 for item in actresses if item.get("poll_enabled", True))
    duplicate_groups = len(result.groups)
    total_files = int(result.total_files or len(result.files))
    duplicate_files = int(result.duplicate_files or 0)
    duplicate_ratio = round(duplicate_files / total_files * 100) if total_files else 0
    scan_progress = round(snapshot.progress * 100)
    mteam_downloaded = sum(1 for item in avs if item.get("mteam_torrent_id") or item.get("download_status") in {"ok", "sent", "exists"})

    integrations = [
        {
            "name": "Jellyfin",
            "status": "已配置" if system_data.get("jellyfin", {}).get("url") else "未配置",
            "tone": "ok" if system_data.get("jellyfin", {}).get("url") else "muted",
        },
        {
            "name": "qBittorrent",
            "status": "已配置" if system_data.get("qbittorrent", {}).get("url") else "未配置",
            "tone": "ok" if system_data.get("qbittorrent", {}).get("url") else "muted",
        },
        {
            "name": "MTeam",
            "status": "已启用" if system_data.get("mteam", {}).get("enabled") else "未启用",
            "tone": "ok" if system_data.get("mteam", {}).get("enabled") else "muted",
        },
        {
            "name": "通知",
            "status": f"{len(system_data.get('notifications', {}).get('channels') or [])} 个通道",
            "tone": "ok" if system_data.get("notifications", {}).get("channels") else "muted",
        },
    ]

    queue_running_statuses = {
        "downloading",
        "sent_to_worker",
        "transcoding",
        "worker_done",
        "transcode_validating",
        "subtitle_processing",
        "subtitle_validating",
        "transcode_done",
    }
    queue_waiting_statuses = {"created", "torrent_pushed", "waiting_worker", "ready_to_run"}
    queue_failed_statuses = {"failed", "ignored", "conflict", "expired"}
    queue = {
        "running": sum(1 for task in post_tasks if str(task.get("status") or "") in queue_running_statuses),
        "waiting": sum(1 for task in post_tasks if str(task.get("status") or "") in queue_waiting_statuses),
        "completed": sum(1 for task in post_tasks if str(task.get("status") or "") == "completed"),
        "failed": sum(1 for task in post_tasks if str(task.get("status") or "") in queue_failed_statuses),
        "total": len(post_tasks),
    }

    recent_tasks: list[dict[str, Any]] = []
    for task in post_tasks[:10]:
        status = str(task.get("status") or "")
        stage = "生成字幕" if status in {"subtitle_processing", "subtitle_validating", "transcode_done"} else "转码" if status in {"sent_to_worker", "transcoding", "worker_done", "transcode_validating"} else "后处理"
        recent_tasks.append(
            {
                "type": stage,
                "title": str(task.get("av_id") or task.get("task_type") or "后处理任务"),
                "status": status or "created",
                "time": dashboard_time(task.get("updated_at") or task.get("created_at")),
                "ts": float(task.get("updated_at") or task.get("created_at") or 0),
                "note": str(task.get("error_message") or task.get("output_path") or task.get("input_path") or "")[:80],
            }
        )
    recent_avs = sorted(avs, key=lambda item: float(item.get("subscribed_at") or 0), reverse=True)[:8]
    for item in recent_avs:
        recent_tasks.append(
            {
                "type": "订阅",
                "title": str(item.get("id") or item.get("av_id") or "番号订阅"),
                "status": str(item.get("status") or "pending"),
                "time": dashboard_time(item.get("subscribed_at")),
                "ts": float(item.get("subscribed_at") or 0),
                "note": str(item.get("title") or "")[:80],
            }
        )
    last_task_results = settings_data.get("last_task_results") if isinstance(settings_data.get("last_task_results"), dict) else {}
    for item in last_task_results.values():
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        recent_tasks.append(
            {
                "type": "自动任务",
                "title": task_label(str(item.get("task_id") or "")),
                "status": str(item.get("status") or "ok"),
                "time": dashboard_time(item.get("ran_at")),
                "ts": float(item.get("ran_at") or 0),
                "note": task_result_summary(result),
            }
        )
    recent_tasks = sorted(recent_tasks, key=lambda item: float(item.get("ts") or 0), reverse=True)[:8]

    return {
        "cards": [
            {"label": "订阅番号", "value": len(avs), "note": f"{pending} 个订阅中 / {done} 个已完成", "trend": dashboard_trend(av_week, av_prev)},
            {"label": "订阅女优", "value": len(actresses), "note": f"{active_actresses} 个正在轮询", "trend": dashboard_trend(actress_week, actress_prev)},
            {"label": "媒体扫描", "value": total_files, "note": f"{duplicate_groups} 组重复 / {duplicate_ratio}% 重复文件", "trend": {"text": "缓存快照", "tone": "flat"}},
            {"label": "异常事件", "value": error_week, "note": f"最近日志 {len(logs)} 条", "trend": dashboard_trend(error_week, error_prev)},
        ],
        "subscription": {
            "pending": pending,
            "done": done,
            "in_library": in_library,
            "downloaded": mteam_downloaded,
            "total": max(len(avs), 1),
        },
        "scan": {
            "status": snapshot.status,
            "progress": scan_progress,
            "started_at": dashboard_time(snapshot.started_at),
            "finished_at": dashboard_time(snapshot.finished_at),
            "current_path": snapshot.current_path or "",
            "scanned_dirs": [str(path) for path in (snapshot.scanned_dirs or tuple(media_dirs))],
        },
        "tasks": [
            {"name": "女优订阅轮询", "cron": settings_data.get("actress_cron", ""), "last": dashboard_time(settings_data.get("last_poll_at"))},
            {"name": "番号下载检查", "cron": settings_data.get("av_cron", ""), "last": dashboard_time(settings_data.get("last_av_poll_at"))},
            {"name": "厂牌更新", "cron": settings_data.get("maker_cron", ""), "last": dashboard_time(settings_data.get("last_maker_poll_at"))},
            {"name": "榜单更新", "cron": settings_data.get("ranking_cron", ""), "last": dashboard_time(settings_data.get("last_ranking_poll_at"))},
        ],
        "queue": queue,
        "integrations": integrations,
        "automation": {
            "auto_transcode_enabled": bool(post_settings.get("auto_transcode_enabled")),
            "auto_subtitle_enabled": bool(post_settings.get("auto_subtitle_enabled")),
            "worker_auto_run": bool(post_settings.get("worker_auto_run")),
            "target_codec": post_settings.get("target_codec") or "av1",
            "crf": post_settings.get("crf") or 36,
            "preset": post_settings.get("preset") or "p1",
        },
        "recent_tasks": recent_tasks,
        "logs": logs[:6],
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running. Use the Unraid console to manage settings.", media_type="text/plain")
    return frontend_app_response()


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running. Use the Unraid console to manage settings.", media_type="text/plain")
    return frontend_app_response()


@app.get("/api/dashboard")
def api_dashboard() -> dict[str, Any]:
    return {"dashboard": dashboard_payload()}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, view: str = "duplicates", legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running. Use the Unraid console to manage settings.", media_type="text/plain")
    return frontend_app_response()


@app.get("/api/scan")
def api_scan() -> dict[str, object]:
    media_dirs, trash_dir, data_dir = settings()
    scan_cache.configure(data_dir)
    snapshot = scan_cache.snapshot()
    scan_alive = scan_cache.scan_alive()
    scan_stale = scan_cache.scan_stale()
    result = snapshot.result or ScanResult(tuple(), 0, 0, tuple(media_dirs), tuple(), tuple())
    duplicate_group_keys = {group.key for group in result.groups}
    scan_choices = selectable_scan_dirs(media_dirs, [trash_dir])
    saved_scan_dirs = saved_duplicate_scan_dirs(media_dirs, trash_dir, scan_choices)
    return {
        "status": snapshot.status,
        "mode": snapshot.mode,
        "started_at": snapshot.started_at,
        "finished_at": snapshot.finished_at,
        "error": snapshot.error,
        "progress": snapshot.progress,
        "processed_files": snapshot.processed_files,
        "scan_total_files": snapshot.total_files,
        "reused_files": snapshot.reused_files,
        "changed_files": snapshot.changed_files,
        "missing_files": snapshot.missing_files,
        "changed_paths": list(snapshot.changed_paths),
        "current_path": snapshot.current_path,
        "last_progress_at": snapshot.last_progress_at,
        "scan_alive": scan_alive,
        "scan_stale": scan_stale,
        "can_reset": snapshot.status == "running",
        "can_cancel": snapshot.status == "running",
        "active_scan_dirs": [str(path) for path in snapshot.scanned_dirs],
        "selectable_scan_dirs": [str(path) for path in scan_choices],
        "selected_scan_dirs": [str(path) for path in saved_scan_dirs],
        "total_files": result.total_files,
        "duplicate_groups": len(result.groups),
        "duplicate_files": result.duplicate_files,
        "scanned_dirs": [str(path) for path in result.scanned_dirs],
        "missing_dirs": [str(path) for path in result.missing_dirs],
        "files": [scan_file_payload(file) for file in result.files],
        "groups": [scan_group_payload(group) for group in result.groups],
        "single_files": [
            scan_file_payload(file)
            for file in result.files
            if file.group_key not in duplicate_group_keys
        ],
    }


@app.post("/api/scan/selection")
def api_save_scan_selection(paths: list[str] = Form(default=[])) -> dict[str, object]:
    media_dirs, trash_dir, _ = settings()
    selected = save_duplicate_scan_dirs(media_dirs, trash_dir, paths, allow_empty=True)
    return {
        "status": "saved",
        "selected_scan_dirs": [str(path) for path in selected],
    }


@app.post("/api/scan/run")
def api_scan_run(paths: list[str] = Form(default=[]), mode: str = Form(default="incremental")) -> dict[str, object]:
    media_dirs, trash_dir, data_dir = settings()
    scan_cache.configure(data_dir)
    scan_dirs = save_duplicate_scan_dirs(media_dirs, trash_dir, paths)
    scan_mode = "full" if mode == "full" else "incremental"
    started = scan_cache.start(scan_dirs, force=False, mode=scan_mode, excluded_dirs=[trash_dir], completion_callback=notify_scan_completed)
    if not started:
        raise HTTPException(status_code=409, detail="已有扫描任务在运行")
    return {"status": "running", "started": started, "mode": scan_mode, "scan_dirs": [str(path) for path in scan_dirs]}


@app.post("/api/scan/cancel")
def api_scan_cancel() -> dict[str, object]:
    _, _, data_dir = settings()
    scan_cache.configure(data_dir)
    cancelled = scan_cache.cancel_running()
    snapshot = scan_cache.snapshot()
    return {
        "status": snapshot.status,
        "cancelled": cancelled,
        "error": snapshot.error,
    }


@app.post("/api/scan/reset")
def api_scan_reset() -> dict[str, object]:
    _, _, data_dir = settings()
    scan_cache.configure(data_dir)
    reset = scan_cache.reset_running()
    snapshot = scan_cache.snapshot()
    return {
        "status": snapshot.status,
        "reset": reset,
        "error": snapshot.error,
    }


@app.post("/scan/run")
def scan_run(paths: list[str] = Form(default=[]), mode: str = Form(default="incremental")) -> RedirectResponse:
    media_dirs, trash_dir, data_dir = settings()
    scan_cache.configure(data_dir)
    scan_dirs = selected_scan_dirs(media_dirs, paths, [trash_dir])
    if scan_dirs:
        scan_cache.start(
            scan_dirs,
            force=False,
            mode="full" if mode == "full" else "incremental",
            excluded_dirs=[trash_dir],
            completion_callback=notify_scan_completed,
        )
    return RedirectResponse("/", status_code=303)


def notify_scan_completed(snapshot: Any) -> None:
    try:
        result = getattr(snapshot, "result", None)
        status = str(getattr(snapshot, "status", "") or "")
        if status == "completed" and result:
            send_notification_event("scan_completed", {
                "status": "completed",
                "title": "重复视频扫描完成",
                "detail": f"扫描 {result.total_files} 个文件，发现 {len(result.groups)} 组重复、{result.duplicate_files} 个重复文件",
                "total_files": result.total_files,
                "duplicate_groups": len(result.groups),
                "duplicate_files": result.duplicate_files,
            })
        elif status == "failed":
            send_notification_event("task_failed", {
                "status": "failed",
                "title": "重复视频扫描失败",
                "detail": str(getattr(snapshot, "error", "") or "扫描失败"),
            })
    except Exception as exc:
        app_log("error", "notification", "扫描完成通知失败", {"error": str(exc)})


@app.post("/scan/subtitles")
def create_subtitle_jobs_from_scan(paths: list[str] = Form(default=[])) -> RedirectResponse:
    if not paths:
        return RedirectResponse("/subtitles", status_code=303)
    unique_paths = list(dict.fromkeys(paths))
    payloads = [subtitle_job_payload_for_path(path) for path in unique_paths]
    if backend_url():
        threading.Thread(target=submit_subtitle_jobs_bulk_background, args=(payloads,), daemon=True).start()
        return RedirectResponse(f"/subtitles?submitted={len(payloads)}&failed=0", status_code=303)
    try:
        result = submit_subtitle_jobs_bulk(payloads)
        submitted = int(result.get("submitted") or 0)
        failed = max(0, len(payloads) - submitted)
    except Exception as exc:
        print(f"[MovieMuse] failed to submit subtitle jobs: {exc}", flush=True)
        submitted = 0
        failed = len(payloads)
    return RedirectResponse(f"/subtitles?submitted={submitted}&failed={failed}", status_code=303)


@app.get("/terminal", response_class=HTMLResponse)
def terminal_console(saved: str = "", restart: str = "") -> RedirectResponse:
    if compute_node_only():
        raise HTTPException(status_code=404, detail="Windows 算力端不提供 Web 控制台，请在 Unraid 字幕算力控制台管理设置。")
    return RedirectResponse("/subtitles", status_code=307)


@app.post("/terminal/settings")
def save_terminal_settings(
    whisper_model: str = Form(default="large-v3"),
    whisper_model_dir: str = Form(default=""),
    whisper_device: str = Form(default="cuda"),
    whisper_compute_type: str = Form(default="float16"),
    subtitle_max_workers: int = Form(default=1),
    translation_max_workers: int = Form(default=1),
    subtitle_output_dir: str = Form(default=""),
    subtitle_path_map: str = Form(default=""),
    default_translate_backend: str = Form(default="google"),
    google_translate_url: str = Form(default="https://translate.google.com/translate_a/single"),
    deepl_api_url: str = Form(default="https://api-free.deepl.com/v2/translate"),
    deepl_api_key: str = Form(default=""),
    openai_base_url: str = Form(default=""),
    openai_api_key: str = Form(default=""),
    openai_model: str = Form(default=""),
    openai_batch_size: int = Form(default=12),
    openai_max_concurrency: int = Form(default=2),
    openai_translation_style: str = Form(default="adult_natural"),
    openai_style_intensity: str = Form(default="medium"),
    openai_context_lines: int = Form(default=2),
    openai_glossary: str = Form(default=""),
    ollama_url: str = Form(default=""),
    ollama_model: str = Form(default=""),
    subtitle_api_token: str = Form(default=""),
) -> RedirectResponse:
    result = save_local_compute_settings(
        {
            "whisper_model": whisper_model,
            "whisper_model_dir": whisper_model_dir,
            "whisper_device": whisper_device,
            "whisper_compute_type": whisper_compute_type,
            "subtitle_max_workers": subtitle_max_workers,
            "translation_max_workers": translation_max_workers,
            "subtitle_output_dir": subtitle_output_dir,
            "subtitle_path_map": subtitle_path_map,
            "default_translate_backend": default_translate_backend,
            "google_translate_url": google_translate_url,
            "deepl_api_url": deepl_api_url,
            "deepl_api_key": deepl_api_key,
            "openai_base_url": openai_base_url,
            "openai_api_key": openai_api_key,
            "openai_model": openai_model,
            "openai_batch_size": openai_batch_size,
            "openai_max_concurrency": openai_max_concurrency,
            "openai_translation_style": openai_translation_style,
            "openai_style_intensity": openai_style_intensity,
            "openai_context_lines": openai_context_lines,
            "openai_glossary": openai_glossary,
            "ollama_url": ollama_url,
            "ollama_model": ollama_model,
            "subtitle_api_token": subtitle_api_token,
        }
    )
    restarted = not result.get("restart_required")
    suffix = "saved=1" if restarted else "saved=1&restart=1"
    return RedirectResponse(f"/subtitles?{suffix}", status_code=303)


@app.get("/subtitles", response_class=HTMLResponse)
def subtitles(
    request: Request,
    batch: str | None = None,
    submitted: int = 0,
    failed: int = 0,
) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running. Use the Unraid console to manage subtitle jobs.", media_type="text/plain")
    return frontend_app_response()


@app.get("/subtitles/compare", response_class=HTMLResponse)
def subtitle_compare_page() -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running. Use the Unraid console to compare subtitle translations.", media_type="text/plain")
    return frontend_app_response()


@app.get("/subtitles/assets/{asset_path:path}")
def subtitle_frontend_asset(asset_path: str) -> FileResponse:
    path = (FRONTEND_DIST / asset_path).resolve()
    try:
        path.relative_to(FRONTEND_DIST.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="资源不存在") from exc
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(path)


@app.post("/subtitles/backend/settings")
def save_subtitle_backend_settings(
    whisper_model: str = Form(default="large-v3"),
    whisper_model_dir: str = Form(default=""),
    whisper_device: str = Form(default="cuda"),
    whisper_compute_type: str = Form(default="float16"),
    subtitle_max_workers: int = Form(default=1),
    translation_max_workers: int = Form(default=1),
    subtitle_output_dir: str = Form(default=""),
    subtitle_path_map: str = Form(default=""),
    default_translate_backend: str = Form(default="google"),
    google_translate_url: str = Form(default="https://translate.google.com/translate_a/single"),
    deepl_api_url: str = Form(default="https://api-free.deepl.com/v2/translate"),
    deepl_api_key: str = Form(default=""),
    openai_base_url: str = Form(default=""),
    openai_api_key: str = Form(default=""),
    openai_model: str = Form(default=""),
    openai_batch_size: int = Form(default=12),
    openai_max_concurrency: int = Form(default=2),
    openai_translation_style: str = Form(default="adult_natural"),
    openai_style_intensity: str = Form(default="medium"),
    openai_context_lines: int = Form(default=2),
    openai_glossary: str = Form(default=""),
    ollama_url: str = Form(default=""),
    ollama_model: str = Form(default=""),
    subtitle_api_token: str = Form(default=""),
) -> RedirectResponse:
    payload = {
        "whisper_model": whisper_model,
        "whisper_model_dir": whisper_model_dir,
        "whisper_device": whisper_device,
        "whisper_compute_type": whisper_compute_type,
        "subtitle_max_workers": subtitle_max_workers,
        "translation_max_workers": translation_max_workers,
        "subtitle_output_dir": subtitle_output_dir,
        "subtitle_path_map": subtitle_path_map,
        "default_translate_backend": default_translate_backend,
        "google_translate_url": google_translate_url,
        "deepl_api_url": deepl_api_url,
        "deepl_api_key": deepl_api_key,
        "openai_base_url": openai_base_url,
        "openai_api_key": openai_api_key,
        "openai_model": openai_model,
        "openai_batch_size": openai_batch_size,
        "openai_max_concurrency": openai_max_concurrency,
        "openai_translation_style": openai_translation_style,
        "openai_style_intensity": openai_style_intensity,
        "openai_context_lines": openai_context_lines,
        "openai_glossary": openai_glossary,
        "ollama_url": ollama_url,
        "ollama_model": ollama_model,
        "subtitle_api_token": subtitle_api_token,
    }
    save_console_compute_config(payload)
    if backend_url():
        remote_post_json("/api/compute/settings", payload)
    else:
        save_local_compute_settings(payload)
    return RedirectResponse("/subtitles", status_code=303)


@app.post("/subtitles/connection")
def save_subtitle_connection(
    subtitle_backend_url: str = Form(default=""),
    subtitle_backend_token: str = Form(default=""),
) -> RedirectResponse:
    save_console_compute_config(
        {
            "subtitle_backend_url": subtitle_backend_url,
            "subtitle_backend_token": subtitle_backend_token,
        }
    )
    return RedirectResponse("/subtitles", status_code=303)


@app.get("/api/subtitle/console")
def api_subtitle_console() -> dict[str, object]:
    return subtitle_console_payload()


@app.post("/api/subtitle/connection")
async def api_save_subtitle_connection(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="连接配置格式不正确")
    _, _, data_dir = settings()
    current_config = load_compute_config(data_dir)
    token = str(payload.get("subtitle_backend_token", "")).strip()
    if is_secret_placeholder(token):
        token = str(current_config.get("subtitle_backend_token", ""))
    save_console_compute_config(
        {
            "subtitle_backend_url": str(payload.get("subtitle_backend_url", "")).strip(),
            "subtitle_backend_token": token,
        }
    )
    saved_token = str(load_compute_config(data_dir).get("subtitle_backend_token", ""))
    return {
        "status": "ok",
        "connection": {
            "subtitle_backend_url": backend_url(),
            "subtitle_backend_token": SECRET_PLACEHOLDER if saved_token else "",
        },
        "backend_status": subtitle_backend_status(),
    }


@app.post("/api/subtitle/settings")
async def api_save_subtitle_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="算力端设置格式不正确")
    payload = restore_secret_placeholders(payload, load_compute_config(settings()[2]))
    save_console_compute_config(payload)
    if backend_url():
        try:
            result = remote_post_json("/api/compute/settings", remote_compute_settings_payload(payload))
        except HTTPException as exc:
            return {
                "status": "saved",
                "synced": False,
                "warning": "\u8bbe\u7f6e\u5df2\u4fdd\u5b58\u5230\u63a7\u5236\u53f0\uff0c\u4f46\u7b97\u529b\u7aef\u6682\u65f6\u65e0\u6cd5\u8fde\u63a5\uff0c\u5f85\u5728\u7ebf\u540e\u518d\u6b21\u4fdd\u5b58\u5373\u53ef\u540c\u6b65\u3002" + str(exc.detail),
                "settings": console_settings_payload(),
                "backend_status": offline_backend_status(str(exc.detail)),
            }
        return {
            "status": "ok",
            "synced": True,
            "remote": redact_secret_response(result),
            "settings": redact_secret_settings(overlay_saved_console_settings(result.get("settings"), load_compute_config(settings()[2]))),
            "backend_status": subtitle_backend_status(),
        }
    result = save_local_compute_settings(payload)
    service = get_subtitle_service()
    _, _, data_dir = settings()
    return {
        **result,
        "settings": redact_secret_settings(compute_settings_payload(service.settings, load_compute_config(data_dir))),
        "backend_status": subtitle_backend_status(),
    }


@app.post("/api/subtitle/translate/test", dependencies=[Depends(require_subtitle_token)])
async def api_test_translate_backend(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="翻译测试请求格式不正确")
    if backend_url():
        payload = dict(payload)
        if isinstance(payload.get("settings"), dict):
            payload["settings"] = restore_secret_placeholders(payload["settings"], load_compute_config(settings()[2]))
        return remote_post_json("/api/subtitle/translate/test", payload, timeout=180)

    backend = str(payload.get("backend") or payload.get("translate_backend") or "google").strip().lower()
    if backend not in {"google", "deepl", "deepseek", "openai", "ollama"}:
        raise HTTPException(status_code=400, detail=f"不支持的翻译后端: {backend}")
    text = str(payload.get("text") or "クッションがいっぱいある、かわいい")
    source_language = str(payload.get("source_language") or "ja")
    target_language = str(payload.get("target_language") or "zh")
    settings_override = payload.get("settings")
    if settings_override is not None and not isinstance(settings_override, dict):
        raise HTTPException(status_code=400, detail="翻译设置格式不正确")
    if isinstance(settings_override, dict):
        settings_override = restore_secret_placeholders(settings_override, load_compute_config(settings()[2]))
    try:
        result = get_subtitle_service().test_translation_backend(
            backend=backend,
            text=text,
            source_language=source_language,
            target_language=target_language,
            settings_override=settings_override or {},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", **result}


def media_srt_file(raw_path: str) -> Path:
    media_dirs, trash_dir, _ = settings()
    try:
        candidate = Path(raw_path).resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法读取字幕路径: {exc}") from exc
    if candidate.suffix.lower() != ".srt":
        raise HTTPException(status_code=400, detail="仅支持选择 .srt 字幕文件")
    roots = resolved_roots([path for path in media_dirs if path.exists()])
    excluded = resolved_roots([trash_dir])
    if not roots or not any(is_relative_to(candidate, root) for root in roots):
        raise HTTPException(status_code=400, detail="字幕文件必须位于已挂载媒体目录中")
    if is_under_any(candidate, excluded):
        raise HTTPException(status_code=400, detail="不读取回收站内的字幕文件")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="字幕文件不存在")
    return candidate


@app.post("/api/subtitle/compare/sample")
async def api_load_compare_sample(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="样本请求格式不正确")
    path = media_srt_file(str(payload.get("path") or "").strip())
    try:
        start = max(0, int(payload.get("start") or 0))
        requested_count = int(payload.get("count") or 20)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="字幕截取范围不正确") from exc
    count = 40 if requested_count == 40 else 20
    text_mode = "full" if str(payload.get("text_mode") or "").strip().lower() == "full" else "auto"
    source_language = str(payload.get("source_language") or "ja")
    target_language = str(payload.get("target_language") or "zh")
    segments = read_srt(path)
    if not segments:
        raise HTTPException(status_code=400, detail="字幕文件没有可读取的字幕段")
    selected = segments[start : start + count]
    if not selected:
        raise HTTPException(status_code=400, detail="起始序号超出字幕范围")
    prepared: list[dict[str, object]] = []
    extracted_count = 0
    for index, segment in enumerate(selected):
        text, extracted = (
            translation_source_text(segment.text, source_language, target_language)
            if text_mode == "auto"
            else (segment.text, False)
        )
        extracted_count += int(extracted)
        prepared.append(
            {
                "index": start + index + 1,
                "start": segment.start,
                "end": segment.end,
                "text": text,
                "display_text": segment.text,
                "source_extracted": extracted,
            }
        )
    return {
        "status": "ok",
        "path": str(path),
        "total": len(segments),
        "start": start,
        "count": len(selected),
        "text_mode": text_mode,
        "extracted_count": extracted_count,
        "segments": prepared,
    }


@app.post("/api/subtitle/translate/compare", dependencies=[Depends(require_subtitle_token)])
async def api_compare_deepseek_translation(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="翻译对比请求格式不正确")
    if backend_url():
        return remote_post_json("/api/subtitle/translate/compare", payload, timeout=300)

    raw_segments = payload.get("segments")
    raw_variants = payload.get("variants")
    if not isinstance(raw_segments, list) or not raw_segments or len(raw_segments) > 40:
        raise HTTPException(status_code=400, detail="请选择 1 至 40 段字幕进行对比")
    if not isinstance(raw_variants, list) or not 1 <= len(raw_variants) <= 2:
        raise HTTPException(status_code=400, detail="每次最多比较两个翻译方案")
    segments: list[SubtitleSegment] = []
    for item in raw_segments:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            raise HTTPException(status_code=400, detail="字幕样本格式不正确")
        segments.append(
            SubtitleSegment(
                start=float(item.get("start") or 0),
                end=float(item.get("end") or 0),
                text=str(item["text"]),
            )
        )
    source_language = str(payload.get("source_language") or "ja")
    target_language = str(payload.get("target_language") or "zh")
    results: list[dict[str, object]] = []
    for index, variant in enumerate(raw_variants):
        if not isinstance(variant, dict):
            raise HTTPException(status_code=400, detail="翻译方案格式不正确")
        settings_override = variant.get("settings")
        if settings_override is not None and not isinstance(settings_override, dict):
            raise HTTPException(status_code=400, detail="翻译方案设置格式不正确")
        started = time.perf_counter()
        try:
            translated = get_subtitle_service().translate_sample(
                segments=segments,
                backend="deepseek",
                source_language=source_language,
                target_language=target_language,
                settings_override=settings_override or {},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"方案 {variant.get('label') or index + 1} 翻译失败: {exc}") from exc
        results.append(
            {
                "id": str(variant.get("id") or index),
                "label": str(variant.get("label") or f"方案 {index + 1}"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "translations": [segment.translated_text for segment in translated],
            }
        )
    return {"status": "ok", "variants": results}


@app.post("/api/subtitle/backend/test")
def test_subtitle_backend(
    subtitle_backend_url: str = Form(default=""),
    subtitle_backend_token: str = Form(default=""),
) -> dict[str, object]:
    target = subtitle_backend_url.strip().rstrip("/")
    if not target:
        raise HTTPException(status_code=400, detail="请先填写 Windows 算力端地址")
    headers = {"X-API-Key": subtitle_backend_token.strip()} if subtitle_backend_token.strip() else {}
    try:
        with remote_http_client(timeout=8.0) as client:
            response = client.get(f"{target}/api/subtitle/node/status", headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=f"算力端返回错误: {exc.response.text}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接算力端: {exc}") from exc
    return {
        "status": "ok",
        "online": True,
        "backend_url": target,
        "hardware": payload.get("hardware"),
        "settings": payload.get("settings"),
        "jobs": payload.get("jobs"),
    }


@app.post("/subtitles/jobs")
def create_subtitle_job_from_form(
    video_path: str = Form(...),
    output_dir: str = Form(default=""),
    source_language: str = Form(default=""),
    target_language: str = Form(default="zh"),
    model: str = Form(default=""),
    translate: str | None = Form(default=None),
    translate_backend: str = Form(default="auto"),
) -> RedirectResponse:
    payload = {
        "video_path": video_path,
        "output_dir": output_dir or None,
        "source_language": source_language or None,
        "target_language": target_language or "zh",
        "model": model or None,
        "translate": translate == "on" and translate_backend != "none",
        "translate_backend": translate_backend or "google",
    }
    if backend_url():
        remote_post_json("/api/subtitle/jobs", rewrite_subtitle_payload(payload))
    else:
        service = get_subtitle_service()
        try:
            service.create_job(**payload)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/subtitles", status_code=303)


@app.post("/subtitles/batches/{batch_id}/submit")
def submit_subtitle_batch(
    batch_id: str,
    paths: list[str] = Form(default=[]),
    output_dir: str = Form(default=""),
    source_language: str = Form(default=""),
    target_language: str = Form(default="zh"),
    model: str = Form(default=""),
    translate: str | None = Form(default=None),
    translate_backend: str = Form(default="auto"),
) -> RedirectResponse:
    _, _, data_dir = settings()
    batch_paths = paths
    if not batch_paths:
        pending = load_pending_subtitle_batch(data_dir, batch_id)
        batch_paths = [str(path) for path in pending.get("paths", [])] if pending else []
    submitted = 0
    failures = 0
    for path in batch_paths:
        try:
            submit_subtitle_job(
                video_path=path,
                output_dir=output_dir or None,
                source_language=source_language or None,
                target_language=target_language or "zh",
                model=model or None,
                translate=translate == "on" and translate_backend != "none",
                translate_backend=translate_backend or "google",
            )
            submitted += 1
        except Exception:
            failures += 1
            continue
    batch_path = subtitle_batch_dir(data_dir) / f"{batch_id}.json"
    if submitted and batch_path.exists():
        batch_path.unlink()
    if submitted:
        return RedirectResponse(f"/subtitles?submitted={submitted}&failed={failures}", status_code=303)
    return RedirectResponse(f"/subtitles?batch={batch_id}&failed={failures}", status_code=303)


@app.get("/api/subtitle/batches/{batch_id}")
def api_get_subtitle_batch(batch_id: str) -> dict[str, object]:
    _, _, data_dir = settings()
    pending = load_pending_subtitle_batch(data_dir, batch_id)
    if not pending:
        raise HTTPException(status_code=404, detail="字幕批次不存在或已提交")
    return pending


@app.post("/api/subtitle/batches/{batch_id}/submit")
def api_submit_subtitle_batch(batch_id: str, payload: dict[str, Any]) -> dict[str, object]:
    _, _, data_dir = settings()
    pending = load_pending_subtitle_batch(data_dir, batch_id)
    if not pending:
        raise HTTPException(status_code=404, detail="字幕批次不存在或已提交")
    batch_paths = [str(path) for path in pending.get("paths", [])]
    submitted = 0
    failures: list[dict[str, str]] = []
    for path in batch_paths:
        try:
            submit_subtitle_job(
                video_path=path,
                output_dir=str(payload.get("output_dir") or "") or None,
                source_language=str(payload.get("source_language") or "") or None,
                target_language=str(payload.get("target_language") or "zh") or "zh",
                model=str(payload.get("model") or "") or None,
                translate=bool(payload.get("translate", True)) and str(payload.get("translate_backend") or "google") != "none",
                translate_backend=str(payload.get("translate_backend") or "google"),
            )
            submitted += 1
        except Exception as exc:
            failures.append({"path": path, "error": str(exc)})
    if submitted:
        batch_path = subtitle_batch_dir(data_dir) / f"{batch_id}.json"
        if batch_path.exists():
            batch_path.unlink()
    return {"status": "ok", "submitted": submitted, "failed": len(failures), "failures": failures}


@app.post("/preview")
def preview(paths: list[str] = Form(default=[])) -> dict[str, object]:
    media_dirs, trash_dir, data_dir = settings()
    store = Storage(data_dir, trash_dir, media_dirs)
    selected = store.preview(paths)
    return {
        "trash_dir": str(trash_dir),
        "selected": [
            {
                "source": str(item.source),
                "target": str(item.target),
                "status": item.status,
                "reason": item.reason,
                "mode": item.mode,
            }
            for item in selected
        ],
    }


@app.post("/move")
def move(paths: list[str] = Form(default=[])) -> RedirectResponse:
    job_id = create_move_job(paths)
    return RedirectResponse(f"/move/jobs/{job_id}", status_code=303)


@app.post("/move/jobs")
def start_move_job(paths: list[str] = Form(default=[])) -> RedirectResponse:
    job_id = create_move_job(paths)
    return RedirectResponse(f"/move/jobs/{job_id}", status_code=303)


@app.get("/move/jobs/{job_id}", response_class=HTMLResponse)
def move_job_page(job_id: str) -> RedirectResponse:
    move_job_snapshot(job_id)
    return RedirectResponse(f"/duplicates?move_job={job_id}", status_code=307)


@app.get("/api/move/jobs/{job_id}")
def api_move_job(job_id: str) -> dict[str, object]:
    return move_job_snapshot(job_id)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "subtitle_mode": "remote" if backend_url() else "local"}


@app.get("/api/subtitle/node/status", dependencies=[Depends(require_subtitle_token)])
def api_subtitle_node_status() -> dict[str, object]:
    return local_node_status()


@app.get("/api/subtitle/backend/status")
def api_subtitle_backend_status(sample_path: str | None = None) -> dict[str, object]:
    status = subtitle_backend_status()
    status["path_preview"] = backend_path_preview(
        sample_path=sample_path,
        remote_status=status if status.get("online") else None,
    )
    return status


@app.get("/api/compute/settings", dependencies=[Depends(require_subtitle_token)])
def api_get_compute_settings() -> dict[str, object]:
    service = get_subtitle_service()
    _, _, data_dir = settings()
    return {
        "status": "ok",
        "settings": redact_secret_settings(compute_settings_payload(service.settings, load_compute_config(data_dir))),
        "translation_backends": translation_backend_options(service.settings),
        "local_models": local_model_dirs(service.settings.model_dir),
    }


@app.post("/api/compute/settings", dependencies=[Depends(require_subtitle_token)])
async def api_save_compute_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="设置内容格式不正确")
    result = save_local_compute_settings(payload)
    service = get_subtitle_service()
    _, _, data_dir = settings()
    return {
        **result,
        "settings": redact_secret_settings(compute_settings_payload(service.settings, load_compute_config(data_dir))),
    }


@app.get("/api/transcode/jobs", dependencies=[Depends(require_subtitle_token)])
def api_list_transcode_jobs(limit: int = 100) -> dict[str, object]:
    jobs = transcode_jobs_payload(limit or None)
    active = [job for job in jobs if job.get("status") in {"queued", "running"}]
    return {"jobs": jobs, "total": len(transcode_jobs_payload()), "active": len(active)}


@app.post("/api/transcode/settings", dependencies=[Depends(require_subtitle_token)])
async def api_save_transcode_worker_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="转码算力端设置格式不正确")
    payload = normalize_transcode_settings_payload(payload)
    settings_payload = get_postprocess_service().update_settings(payload)
    return {
        "status": "ok",
        "settings": settings_payload,
        "ffmpeg_standard_command": build_ffmpeg_preview(settings_payload),
    }


@app.post("/api/transcode/jobs", dependencies=[Depends(require_subtitle_token)])
async def api_create_transcode_job(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="转码任务格式不正确")
    job = create_transcode_job(payload)
    return {"status": "queued", "job_id": job["id"], "job": job}


@app.get("/api/transcode/jobs/{job_id}", dependencies=[Depends(require_subtitle_token)])
def api_get_transcode_job(job_id: str) -> dict[str, object]:
    return {"job": transcode_job_snapshot(job_id)}


@app.get("/api/subtitle/jobs", dependencies=[Depends(require_subtitle_token)])
def api_list_subtitle_jobs(limit: int = 0) -> dict[str, object]:
    if backend_url():
        return subtitle_jobs_payload_from_remote_or_cache(limit)
    service = get_subtitle_service()
    jobs = [job_payload(job) for job in service.list_jobs(limit or None)]
    active = [job for job in jobs if job.get("status") in {"queued", "running", "translating"}]
    return {"jobs": jobs, "total": len(jobs), "active": len(active)}


@app.post("/api/subtitle/jobs", dependencies=[Depends(require_subtitle_token)])
def api_create_subtitle_job(payload: SubtitleJobCreate) -> dict[str, object]:
    if backend_url():
        result = remote_post_json("/api/subtitle/jobs", rewrite_subtitle_payload(payload.model_dump()))
        upsert_subtitle_job_cache(subtitle_job_from_result(result))
        return result
    service = get_subtitle_service()
    try:
        job = service.create_job(
            video_path=payload.video_path,
            output_dir=payload.output_dir,
            source_language=payload.source_language,
            target_language=payload.target_language,
            model=payload.model,
            translate=payload.translate,
            translate_backend=payload.translate_backend,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job_payload(job)


@app.post("/api/subtitle/jobs/bulk", dependencies=[Depends(require_subtitle_token)])
def api_create_subtitle_jobs_bulk(payload: dict[str, Any]) -> dict[str, object]:
    raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(raw_jobs, list):
        raise HTTPException(status_code=400, detail="jobs must be a list")
    if backend_url():
        rewritten = [rewrite_subtitle_payload(dict(item)) for item in raw_jobs if isinstance(item, dict)]
        result = remote_post_json("/api/subtitle/jobs/bulk", {"jobs": rewritten}, timeout=120)
        if isinstance(result, dict):
            for item in result.get("jobs", []) or []:
                upsert_subtitle_job_cache(item)
        return result
    service = get_subtitle_service()
    jobs = service.create_jobs([dict(item) for item in raw_jobs if isinstance(item, dict)])
    return {"status": "ok", "submitted": len(jobs), "jobs": [job_payload(job) for job in jobs]}


@app.post("/api/subtitle/jobs/retry-failed", dependencies=[Depends(require_subtitle_token)])
async def api_retry_failed_subtitle_jobs(request: Request) -> dict[str, object]:
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(payload, dict):
        payload = {}
    translate_backend = str(payload.get("translate_backend") or current_subtitle_job_defaults()["translate_backend"])
    retry_payload = {"translate_backend": translate_backend}
    if backend_url():
        result = remote_post_json("/api/subtitle/jobs/retry-failed", retry_payload)
        if isinstance(result, dict):
            for item in result.get("jobs", []) or []:
                upsert_subtitle_job_cache(item)
        return result
    jobs = get_subtitle_service().retry_failed_jobs(translate_backend=translate_backend)
    return {
        "status": "ok",
        "count": len(jobs),
        "jobs": [job_payload(job) for job in jobs],
    }


@app.post("/api/subtitle/jobs/{job_id}/retry", dependencies=[Depends(require_subtitle_token)])
async def api_retry_subtitle_job(job_id: str, request: Request) -> dict[str, object]:
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(payload, dict):
        payload = {}
    translate_backend = str(payload.get("translate_backend") or current_subtitle_job_defaults()["translate_backend"])
    retry_payload = {"translate_backend": translate_backend}
    if backend_url():
        result = remote_post_json(f"/api/subtitle/jobs/{job_id}/retry", retry_payload)
        upsert_subtitle_job_cache(subtitle_job_from_result(result))
        return result
    try:
        job = get_subtitle_service().retry_job(job_id, translate_backend=translate_backend)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "job": job_payload(job)}


@app.post("/api/subtitle/jobs/{job_id}/cancel", dependencies=[Depends(require_subtitle_token)])
def api_cancel_subtitle_job(job_id: str) -> dict[str, object]:
    if backend_url():
        result = remote_post_json(f"/api/subtitle/jobs/{job_id}/cancel", {})
        upsert_subtitle_job_cache(subtitle_job_from_result(result))
        return result
    try:
        job = get_subtitle_service().cancel_job(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "cancelled", "job": job_payload(job)}


@app.delete("/api/subtitle/jobs/{job_id}", dependencies=[Depends(require_subtitle_token)])
def api_delete_subtitle_job(job_id: str) -> dict[str, object]:
    if backend_url():
        result = remote_delete(f"/api/subtitle/jobs/{job_id}")
        remove_subtitle_job_cache(job_id)
        return result
    try:
        job = get_subtitle_service().delete_job(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "deleted": job_payload(job)}


@app.post("/api/subtitle/upload", dependencies=[Depends(require_subtitle_token)])
async def api_upload_subtitle_job(
    file: UploadFile = File(...),
    source_language: str | None = Form(default=None),
    target_language: str = Form(default="zh"),
    model: str | None = Form(default=None),
    translate: bool = Form(default=True),
) -> dict[str, object]:
    if backend_url():
        content = await file.read()
        files = {"file": (file.filename or "upload.mkv", content, file.content_type or "application/octet-stream")}
        data = {
            "source_language": source_language or "",
            "target_language": target_language,
            "model": model or "",
            "translate": str(translate).lower(),
        }
        try:
            with remote_http_client(timeout=None) as client:
                response = client.post(
                    f"{backend_url()}/api/subtitle/upload",
                    headers=backend_headers(),
                    files=files,
                    data=data,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise_remote_error(exc)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"无法连接字幕后端: {exc}") from exc

    service = get_subtitle_service()
    try:
        saved_path = service.save_upload(file.filename or "upload.mkv", await file.read())
        job = service.create_job(
            video_path=str(saved_path),
            source_language=source_language,
            target_language=target_language,
            model=model,
            translate=translate,
            translate_backend="google",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job_payload(job)


@app.get("/api/subtitle/jobs/{job_id}", dependencies=[Depends(require_subtitle_token)])
def api_get_subtitle_job(job_id: str) -> dict[str, object]:
    if backend_url():
        return remote_get(f"/api/subtitle/jobs/{job_id}")
    service = get_subtitle_service()
    job = service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job_payload(job)


@app.get("/api/subtitle/jobs/{job_id}/files/{kind}", dependencies=[Depends(require_subtitle_token)])
def api_download_subtitle_file(job_id: str, kind: str):
    if kind not in SUBTITLE_FILE_KINDS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    if backend_url():
        return proxy_subtitle_file(job_id, kind)
    service = get_subtitle_service()
    try:
        path = service.file_for(job_id, kind)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name)


@app.get("/subtitles/jobs/{job_id}/files/{kind}")
def download_subtitle_file(job_id: str, kind: str):
    if kind not in SUBTITLE_FILE_KINDS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    if backend_url():
        return proxy_subtitle_file(job_id, kind)
    service = get_subtitle_service()
    try:
        path = service.file_for(job_id, kind)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name)


def proxy_subtitle_file(job_id: str, kind: str) -> Response:
    try:
        with remote_http_client(timeout=120) as client:
            response = client.get(
                f"{backend_url()}/api/subtitle/jobs/{job_id}/files/{kind}",
                headers=backend_headers(),
            )
            response.raise_for_status()
            headers = {}
            content_disposition = response.headers.get("content-disposition")
            if content_disposition:
                headers["content-disposition"] = content_disposition
            return Response(
                content=response.content,
                media_type=response.headers.get("content-type", "application/octet-stream"),
                headers=headers,
            )
    except httpx.HTTPStatusError as exc:
        raise_remote_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接字幕后端: {exc}") from exc


@app.get("/cover")
def cover(path: str) -> FileResponse:
    media_dirs, _, _ = settings()
    image_path = Path(path).resolve()
    allowed = any(
        media_dir.exists() and is_relative_to(image_path, media_dir.resolve())
        for media_dir in media_dirs
    )
    if not allowed or not image_path.exists() or not image_path.is_file():
        raise HTTPException(status_code=404)
    if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(status_code=404)
    return FileResponse(image_path)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


# ============================================================
# 订阅功能 API
# ============================================================

subscription_service: SubscriptionService | None = None
postprocess_service: PostprocessService | None = None
system_settings_service: SystemSettingsService | None = None
app_log_service: AppLogService | None = None
subscription_poll_thread: threading.Thread | None = None
subscription_poll_stop = threading.Event()
notification_dedupe_lock = threading.RLock()
notification_dedupe: dict[str, float] = {}
asset_prewarm_lock = threading.RLock()
asset_prewarm_pending: set[str] = set()
wechat_token_lock = threading.RLock()
wechat_token_cache: dict[str, dict[str, Any]] = {}
wechat_session_lock = threading.RLock()
wechat_sessions: dict[str, dict[str, Any]] = {}
wechat_action_dedupe_lock = threading.RLock()
wechat_action_dedupe: dict[str, float] = {}
IMAGE_PROXY_HOSTS = ("javbus.com", "javdb.com", "jdbstatic.com", "dmm.co.jp", "libredmm.com", "javlibrary.com")
JAVDB_HOSTS = ("javdb.com",)
DMM_HOSTS = ("dmm.co.jp",)
JAVLIBRARY_HOSTS = ("javlibrary.com",)
DMM_MAKER_LIST_URLS = {
    "s1 no.1 style": [
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=3152/sort=date/",
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=3152/list_type=reserve/sort=date/",
    ],
    "prestige": [
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=40136/sort=date/",
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=40136/list_type=reserve/sort=date/",
    ],
    "idea pocket": [
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=1219/sort=date/",
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=1219/list_type=reserve/sort=date/",
    ],
    "madonna": [
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=2661/sort=date/",
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=2661/list_type=reserve/sort=date/",
    ],
    "sod create": [
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=45276/sort=date/",
        "https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=45276/list_type=reserve/sort=date/",
    ],
}
DMM_MAKER_ALIASES = {
    "s1 no.1 style": ("s1", "s1 no.1 style", "エスワン", "エスワン ナンバーワンスタイル"),
    "prestige": ("prestige", "プレステージ"),
    "idea pocket": ("idea pocket", "ideapocket", "アイデアポケット"),
    "madonna": ("madonna", "マドンナ"),
    "sod create": ("sod create", "sodクリエイト", "sodクリエイト"),
}
DMM_MAKER_SEARCH_TERMS = {
    "s1 no.1 style": ("S1 NO.1 STYLE",),
    "prestige": ("PRESTIGE",),
    "idea pocket": ("IDEA POCKET",),
    "madonna": ("Madonna",),
    "sod create": ("SODクリエイト", "SOD Create"),
}
DMM_MAKER_PRIMARY_LABELS = {
    "s1 no.1 style": ("S1 NO.1 STYLE",),
    "prestige": ("ABSOLUTELY FANTASIA",),
    "idea pocket": ("ティッシュ",),
    "madonna": ("Madonna",),
    "sod create": ("SODSTAR",),
}
JAVLIBRARY_FLARESOLVERR_URL = os.getenv("JAVLIBRARY_FLARESOLVERR_URL", "http://127.0.0.1:8281/v1")
JAVLIBRARY_ACTOR_IDS = {
    "涼森れむ": "aeqfy",
    "桜空もも": "aemco",
    "野々浦暖": "aesse",
}
JAVLIBRARY_MAKER_URLS = {
    "s1 no.1 style": [
        "https://www.javlibrary.com/cn/vl_label.php?l=bvla",
        "https://www.javlibrary.com/cn/vl_maker.php?m=arlq",
    ],
    "idea pocket": [
        "https://www.javlibrary.com/cn/vl_label.php?l=buwq",
        "https://www.javlibrary.com/cn/vl_maker.php?m=aq4q",
    ],
    "madonna": [
        "https://www.javlibrary.com/cn/vl_label.php?l=bvkq",
        "https://www.javlibrary.com/cn/vl_maker.php?m=aqsa",
    ],
    "prestige": [
        "https://www.javlibrary.com/cn/vl_label.php?l=aqmuc",
        "https://www.javlibrary.com/cn/vl_maker.php?m=aa",
    ],
    "sod create": [
        "https://www.javlibrary.com/cn/vl_label.php?l=defa",
        "https://www.javlibrary.com/cn/vl_maker.php?m=oq",
    ],
}
PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")
METADATA_SEARCH_TTL = int(os.getenv("SUBSCRIPTION_METADATA_SEARCH_TTL_SECONDS", "21600"))
METADATA_ACTRESS_AVS_TTL = int(os.getenv("SUBSCRIPTION_METADATA_ACTRESS_AVS_TTL_SECONDS", "21600"))
METADATA_JAVLIBRARY_LIST_TTL = int(os.getenv("SUBSCRIPTION_JAVLIBRARY_LIST_TTL_SECONDS", "43200"))
METADATA_JAVLIBRARY_MAP_TTL = int(os.getenv("SUBSCRIPTION_JAVLIBRARY_MAP_TTL_SECONDS", "2592000"))
METADATA_DETAIL_TTL = int(os.getenv("SUBSCRIPTION_METADATA_DETAIL_TTL_SECONDS", "2592000"))
METADATA_PROFILE_TTL = int(os.getenv("SUBSCRIPTION_METADATA_PROFILE_TTL_SECONDS", "604800"))
METADATA_LISTING_TTL = int(os.getenv("SUBSCRIPTION_METADATA_LISTING_TTL_SECONDS", "43200"))
METADATA_MAKER_LISTING_TTL = int(os.getenv("SUBSCRIPTION_MAKER_LISTING_TTL_SECONDS", "21600"))
METADATA_DMM_RANKING_TTL = int(os.getenv("SUBSCRIPTION_DMM_RANKING_TTL_SECONDS", "172800"))
MAKER_REFRESH_PREWARM_LIMIT = int(os.getenv("SUBSCRIPTION_MAKER_REFRESH_PREWARM_LIMIT", "60"))
MAKER_REFRESH_COVER_PREWARM_LIMIT = int(os.getenv("SUBSCRIPTION_MAKER_REFRESH_COVER_PREWARM_LIMIT", "28"))
ASSET_IMAGE_MAX_BYTES = int(os.getenv("SUBSCRIPTION_ASSET_IMAGE_MAX_BYTES", str(12 * 1024 * 1024)))
ASSET_CACHE_MAX_BYTES = int(os.getenv("SUBSCRIPTION_ASSET_CACHE_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
ASSET_MEDIA_MAX_BYTES = int(os.getenv("SUBSCRIPTION_ASSET_MEDIA_MAX_BYTES", str(300 * 1024 * 1024)))
GLOBAL_MAX_COACTORS = 2
ASSET_KIND_DIRS = {
    "cover": "covers",
    "screenshot": "screenshots",
    "actor": "actors",
    "trailer": "trailers",
    "image": "images",
}


def allowed_external_url(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def metadata_cache_key(*parts: object) -> str:
    return ":".join(str(part or "").strip().lower() for part in parts if str(part or "").strip())


def cache_get(namespace: str, key: str, *, allow_stale: bool = False) -> Any | None:
    return get_subscription_service().get_metadata_cache(namespace, key, allow_stale=allow_stale)


def cache_set(namespace: str, key: str, value: Any, ttl_seconds: int) -> None:
    get_subscription_service().set_metadata_cache(namespace, key, value, ttl_seconds)


def cached_metadata(namespace: str, key: str, ttl_seconds: int, fetch: Callable[[], Any]) -> Any:
    cached = cache_get(namespace, key)
    if cached is not None:
        app_log("info", "metadata-cache", "元数据缓存命中", {"stage": "metadata_cache_hit", "namespace": namespace, "key": key})
        return cached
    try:
        value = fetch()
    except Exception:
        stale = cache_get(namespace, key, allow_stale=True)
        if stale is not None:
            app_log("warning", "metadata-cache", "外站抓取失败，返回过期元数据缓存", {"stage": "metadata_cache_stale", "namespace": namespace, "key": key})
            return stale
        raise
    if value not in (None, "", [], {}):
        cache_set(namespace, key, value, ttl_seconds)
        app_log("info", "metadata-cache", "元数据缓存写入", {"stage": "metadata_cache_store", "namespace": namespace, "key": key, "ttl": ttl_seconds})
    return value


def network_proxy_settings() -> dict[str, Any]:
    settings_data = get_system_settings_service().get()
    network = settings_data.get("network")
    return network if isinstance(network, dict) else {}


def configured_proxy_url() -> str:
    network = network_proxy_settings()
    if network.get("proxy_enabled"):
        https_proxy = str(network.get("https_proxy") or "").strip()
        http_proxy = str(network.get("http_proxy") or "").strip()
        return https_proxy or http_proxy
    return str(
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or ""
    ).strip()


def configured_flaresolverr_url() -> str:
    network = network_proxy_settings()
    configured = str(network.get("flaresolverr_url") or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(os.getenv("JAVLIBRARY_FLARESOLVERR_URL") or JAVLIBRARY_FLARESOLVERR_URL).strip().rstrip("/")


def apply_system_proxy_settings() -> None:
    network = network_proxy_settings()
    if network.get("proxy_enabled"):
        http_proxy = str(network.get("http_proxy") or "").strip()
        https_proxy = str(network.get("https_proxy") or http_proxy).strip()
        no_proxy = str(network.get("no_proxy") or "").strip()
        if http_proxy:
            os.environ["HTTP_PROXY"] = http_proxy
            os.environ["http_proxy"] = http_proxy
        if https_proxy:
            os.environ["HTTPS_PROXY"] = https_proxy
            os.environ["https_proxy"] = https_proxy
        if no_proxy:
            os.environ["NO_PROXY"] = no_proxy
            os.environ["no_proxy"] = no_proxy
    javdb.set_proxy_provider(lambda: configured_proxy_url() if network_proxy_settings().get("apply_to_javdb", True) else "")
    dmm.set_proxy_provider(lambda: configured_proxy_url() if network_proxy_settings().get("apply_to_javdb", True) else "")
    javlibrary.set_service_url_provider(configured_flaresolverr_url)


def proxy_status_payload() -> dict[str, object]:
    network = network_proxy_settings()
    env = {key: os.getenv(key, "") for key in PROXY_ENV_KEYS}
    return {
        "settings": network,
        "env": env,
        "effective_proxy": configured_proxy_url(),
        "javdb_proxy_enabled": bool(network.get("apply_to_javdb", True)),
        "flaresolverr_url": configured_flaresolverr_url(),
        "javlibrary": javlibrary.stats(),
    }


def get_subscription_service() -> SubscriptionService:
    global subscription_service
    if subscription_service is None:
        _, _, data_dir = settings()
        subscription_service = SubscriptionService(data_dir)
    return subscription_service


def get_postprocess_service() -> PostprocessService:
    global postprocess_service
    if postprocess_service is None:
        _, _, data_dir = settings()
        postprocess_service = PostprocessService(data_dir)
    return postprocess_service


def get_system_settings_service() -> SystemSettingsService:
    global system_settings_service
    if system_settings_service is None:
        _, _, data_dir = settings()
        system_settings_service = SystemSettingsService(data_dir)
    return system_settings_service


def console_password_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def current_console_user(request: Request) -> str:
    token = str(request.cookies.get(CONSOLE_SESSION_COOKIE) or "")
    if not token:
        return ""
    now = time.time()
    with console_sessions_lock:
        session = console_sessions.get(token)
        if not session:
            return ""
        if now - float(session.get("created_at") or 0) > CONSOLE_SESSION_TTL:
            console_sessions.pop(token, None)
            return ""
        session["last_seen_at"] = now
        return str(session.get("username") or "")


def create_console_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with console_sessions_lock:
        console_sessions[token] = {"username": username, "created_at": time.time(), "last_seen_at": time.time()}
    return token


def expire_wash_requests_with_postprocess() -> int:
    service = get_subscription_service()
    changed = service.expire_wash_requests()
    post = get_postprocess_service()
    for item in service.get_subscribed_av():
        wash = item.get("wash") if isinstance(item.get("wash"), dict) else {}
        if wash.get("status") != "expired":
            continue
        task_id = str(wash.get("task_id") or "")
        if not task_id:
            continue
        task = post.get_task(task_id)
        if task and task.get("status") not in {"completed", "failed", "ignored", "expired", "conflict"}:
            post.update_task(task_id, status="expired", error_code="wash_expired", error_message="洗版任务超过设置期限，已自动取消")
            post.add_event(task_id, "info", "wash_expired", "洗版任务超过设置期限，已自动取消", {"av_id": item.get("id", "")})
    return changed


def get_app_log_service() -> AppLogService:
    global app_log_service
    if app_log_service is None:
        _, _, data_dir = settings()
        app_log_service = AppLogService(data_dir)
    return app_log_service


def app_log(level: str, source: str, message: str, data: dict[str, Any] | None = None) -> None:
    try:
        get_app_log_service().write(level, source, message, data)
    except Exception as exc:
        print(f"[LogService] write failed: {exc}", flush=True)
    try:
        notifier = globals().get("notify_from_app_log")
        if callable(notifier):
            notifier(level, source, message, data or {})
    except Exception as exc:
        print(f"[NotificationLogHook] failed: {exc}", flush=True)


def notify_from_app_log(level: str, source: str, message: str, data: dict[str, Any]) -> None:
    if source == "notification":
        return
    sender = globals().get("send_notification_event")
    if not callable(sender):
        return
    stage = str(data.get("stage") or "")
    av_id = str(data.get("av_id") or "")
    title = str(data.get("title") or data.get("mteam_torrent_title") or av_id or message)
    if stage == "mteam_search_empty":
        return
    if message in {"自动订阅新增番号", "女优一键订阅新增番号"} or stage == "actress_subscribe_latest_added":
        return
    if stage == "download_done":
        status = str(data.get("status") or "")
        event_key = "torrent_sent" if status in {"ok", "exists", "sent"} else "task_failed"
        sender(event_key, {
            "status": status or "done",
            "title": title,
            "detail": str(data.get("message") or message),
            "av_id": av_id,
            "torrent_id": data.get("torrent_id", ""),
            "torrent_title": data.get("torrent_title", ""),
            "site": data.get("site", ""),
            "size": data.get("size", ""),
            "seeders": data.get("seeders", ""),
            "downloader": data.get("downloader", ""),
            "cover": data.get("cover") or data.get("cover_url") or "",
        })
    elif stage in {"download_error", "download_qb_hash_conflict", "mteam_missing_id"}:
        sender("task_failed", {
            "status": str(data.get("status") or "failed"),
            "title": f"{message}：{av_id}" if av_id else message,
            "detail": str(data.get("error") or data.get("message") or message),
            "av_id": av_id,
            "torrent_id": data.get("torrent_id", ""),
        })
    elif stage == "jellyfin_refresh_done" and int(data.get("changed") or 0) > 0:
        sender("jellyfin_in_library", {
            "status": "in_library",
            "title": "Jellyfin 入库状态更新",
            "detail": f"{data.get('changed')} 个订阅已确认入库",
        })
    elif level == "error" and source in {"subscription", "download", "mteam", "qbittorrent", "task", "wash", "postprocess"}:
        sender("task_failed", {
            "status": "failed",
            "title": message,
            "detail": str(data.get("error") or data.get("message") or message),
            "av_id": av_id,
            "task_id": data.get("task_id", ""),
        })


javdb.set_logger(app_log)
dmm.set_logger(app_log)
javlibrary.set_logger(app_log)
apply_system_proxy_settings()


def is_vr_work(av: dict[str, Any]) -> bool:
    return "VR" in str(av.get("title") or "").upper()


def normalize_actor_items(items: object) -> list[dict[str, str]]:
    if not isinstance(items, list):
        items = [items] if items else []
    actors: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            actor_id = str(item.get("id") or item.get("code") or item.get("url") or "").strip()
            name = str(item.get("name") or item.get("value") or "").strip()
            url = str(item.get("url") or "").strip()
            source = str(item.get("source") or "").strip()
            dmm_name = str(item.get("dmm_name") or "").strip()
        else:
            actor_id = ""
            name = str(item or "").strip()
            url = ""
            source = ""
            dmm_name = ""
        actor_names = split_actor_names(name)
        is_split_group = len(actor_names) > 1
        for actor_name in actor_names:
            key = ((actor_id or url or actor_name) if not is_split_group else actor_name).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            actor = {"id": actor_id if not is_split_group else "", "name": actor_name}
            if url and not is_split_group:
                actor["url"] = url
            if source:
                actor["source"] = source
            if dmm_name:
                actor["dmm_name"] = actor_name if is_split_group else dmm_name
            actors.append(actor)
    return actors


def split_actor_names(value: object) -> list[str]:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"\s*[、,|]\s*|\s{2,}", text) if part.strip()]
    if len(parts) > 1:
        return parts
    spaced = [part.strip() for part in text.split(" ") if part.strip()]
    if 2 <= len(spaced) <= 80 and all(1 <= len(part) <= 32 for part in spaced):
        return spaced
    return [text]


def configured_max_coactors() -> int:
    try:
        value = int(get_subscription_service().get_settings().get("max_coactors") or GLOBAL_MAX_COACTORS)
    except (TypeError, ValueError):
        value = GLOBAL_MAX_COACTORS
    return max(1, min(GLOBAL_MAX_COACTORS, value))


def javdb_source_enabled() -> bool:
    env_value = os.getenv("SUBSCRIPTION_JAVDB_SOURCE_ENABLED")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(get_subscription_service().get_settings().get("javdb_source_enabled", False))


def resolve_av_actors_for_limit(av: dict[str, Any]) -> list[dict[str, str]]:
    detail = cached_av_detail(av)
    if isinstance(detail, dict):
        actors = normalize_actor_items(detail.get("actors") or detail.get("actresses") or detail.get("actress"))
        if actors:
            return actors
        nested = detail.get("detail") if isinstance(detail.get("detail"), dict) else {}
        actors = normalize_actor_items(nested.get("actors") or nested.get("actresses") or nested.get("actress"))
        if actors:
            return actors
    detail_actors = []
    if javdb_source_enabled() and av.get("url") and allowed_external_url(str(av.get("url") or ""), JAVDB_HOSTS):
        detail_actors = javdb.get_av_actresses(str(av.get("url") or ""), include_profiles=False)
    return normalize_actor_items(detail_actors) or normalize_actor_items(av.get("actresses") or av.get("actress"))


def av_date_key(item: dict[str, Any]) -> str:
    return str(item.get("date") or item.get("release_date") or "")[:10]


def canonical_av_id(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    compact = re.sub(r"[^A-Z0-9]", "", raw)
    compact = re.sub(r"(?:BOD|EC|R|V)$", "", compact)
    for prefix in ("FTKT", "ZTKT", "TKT", "TK"):
        if compact.startswith(prefix) and re.fullmatch(rf"{prefix}[A-Z]{{2,}}\d{{2,}}", compact):
            compact = compact[len(prefix) :]
            break
    match = re.fullmatch(r"([A-Z]{2,})(\d{1,})", compact)
    if not match:
        return raw
    return f"{match.group(1)}-{normalize_catalog_digits(match.group(2))}"


def av_id_parts(value: object) -> tuple[str, str]:
    match = re.fullmatch(r"([A-Z]{2,})-(\d{2,5})", canonical_av_id(value))
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def dmm_cid_candidates_from_item(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("url", "cover", "cover_url", "cover_proxy"):
        value = str(item.get(key) or "").strip()
        if value:
            values.append(value)
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    for key in ("url", "cover", "cover_url", "image"):
        value = str(detail.get(key) or "").strip()
        if value:
            values.append(value)
    candidates: list[str] = []
    for value in values:
        parsed = urlparse(value)
        text = f"{parsed.path}?{parsed.query}"
        for pattern in (
            r"(?:cid=|/cid=)([a-z0-9_]+)",
            r"/adult/([a-z0-9_]+)/",
            r"/([a-z0-9_]+?)(?:p[sl]|jp|pl|ps)?\.(?:jpg|webp|png)",
        ):
            for match in re.finditer(pattern, text, re.IGNORECASE):
                candidate = str(match.group(1) or "").strip()
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
    return candidates


def canonical_subscription_av_id(item: dict[str, Any]) -> str:
    av_id = canonical_av_id(item.get("id"))
    prefix, number = av_id_parts(av_id)
    source_chain = source_chain_for_item(item)
    has_dmm_source = "dmm" in source_chain or any("dmm.co.jp" in str(item.get(key) or "") for key in ("url", "cover", "cover_url"))
    if not has_dmm_source:
        return av_id
    for candidate in dmm_cid_candidates_from_item(item):
        normalized = dmm.normalize_av_id_from_cid(candidate)
        dmm_prefix, dmm_number = av_id_parts(normalized)
        if not dmm_prefix:
            continue
        if not av_id:
            return normalized
        if prefix == dmm_prefix and number.lstrip("0") == dmm_number.lstrip("0") and len(dmm_number) > len(number):
            return normalized
    return av_id


def normalized_source_name(value: object) -> str:
    source = str(value or "").strip().lower()
    aliases = {
        "fanza": "dmm",
        "dmm/fanza": "dmm",
        "javlibrary+flaresolverr": "javlibrary",
    }
    return aliases.get(source, source)


def source_chain_for_item(item: dict[str, Any]) -> list[str]:
    chain: list[str] = []
    raw_chain = item.get("source_chain")
    if isinstance(raw_chain, list):
        for value in raw_chain:
            source = normalized_source_name(value)
            if source and source not in chain:
                chain.append(source)
    source = normalized_source_name(item.get("source"))
    if source and source not in chain:
        chain.append(source)
    return chain


def append_source_chain(item: dict[str, Any], *sources: object) -> dict[str, Any]:
    result = dict(item)
    chain = source_chain_for_item(result)
    for value in sources:
        source = normalized_source_name(value)
        if source and source not in chain:
            chain.append(source)
    if chain:
        result["source_chain"] = chain
    return result


def explain_match(item: dict[str, Any], *, default_reason: str = "", default_confidence: str = "medium") -> dict[str, Any]:
    result = append_source_chain(item)
    reason = str(result.get("match_reason") or "").strip()
    confidence = str(result.get("confidence") or "").strip()
    source = normalized_source_name(result.get("source"))
    scope = str(result.get("source_scope") or "").strip().lower()
    if not reason:
        if result.get("_cache_hit"):
            reason = "sqlite_cache"
        elif scope == "label":
            reason = "primary_label"
        elif str(result.get("source_actor_match") or "").strip():
            reason = "actress_seed"
        elif default_reason:
            reason = default_reason
        elif source:
            reason = f"{source}_result"
    if not confidence:
        if reason in {"primary_label", "actress_seed", "exact_av_id", "sqlite_cache", "dmm_actress_search", "actress_dmm_match"}:
            confidence = "high"
        elif source in {"dmm", "javlibrary"}:
            confidence = "medium"
        else:
            confidence = default_confidence
    elif reason in {"primary_label", "actress_seed", "exact_av_id", "sqlite_cache", "dmm_actress_search", "actress_dmm_match"} and confidence in {"low", "medium"}:
        confidence = "high"
    if reason:
        result["match_reason"] = reason
    if confidence:
        result["confidence"] = confidence
    return result


def merge_av_sources(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source in sources:
        for raw in source:
            if not isinstance(raw, dict):
                continue
            original_id = str(raw.get("id") or "").strip().upper()
            av_id = canonical_av_id(original_id)
            if not av_id:
                continue
            item = dict(raw)
            item["id"] = av_id
            item = append_source_chain(item)
            if original_id and original_id != av_id:
                item.setdefault("source_id", original_id)
            existing = merged.get(av_id)
            if not existing:
                merged[av_id] = item
                continue
            combined = dict(existing)
            for key, value in item.items():
                if key == "source_chain":
                    continue
                if value not in ("", None, [], {}):
                    combined[key] = value
            for value in source_chain_for_item(existing) + source_chain_for_item(item):
                combined = append_source_chain(combined, value)
            if isinstance(existing.get("detail"), dict) and isinstance(item.get("detail"), dict):
                combined["detail"] = {**existing["detail"], **item["detail"]}
            elif isinstance(existing.get("detail"), dict) and not combined.get("detail"):
                combined["detail"] = existing["detail"]
            merged[av_id] = combined
    return sorted(merged.values(), key=lambda item: (av_date_key(item), str(item.get("id") or "")), reverse=True)


def metadata_item_released(item: dict[str, Any]) -> bool:
    value = av_date_key(item)
    if not value:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() <= date.today()
    except ValueError:
        return False


def normalize_cover_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host.endswith("pics.dmm.co.jp"):
        return re.sub(r"(?i)(p)[st](\.(?:jpg|jpeg|png|webp)(?:\?.*)?)$", r"\1l\2", value)
    return value


def normalize_image_fields(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    for key in ("cover", "cover_url", "latest_cover", "poster", "image", "photo", "avatar"):
        if payload.get(key):
            payload[key] = normalize_cover_url(str(payload.get(key) or "").strip())
    return payload


DMM_PLACEHOLDER_COVER_CACHE: dict[str, tuple[float, bool]] = {}
DMM_PLACEHOLDER_COVER_CACHE_TTL = int(os.getenv("DMM_PLACEHOLDER_COVER_CACHE_TTL_SECONDS", "86400"))


def dmm_placeholder_cover_cache_key(url: str) -> str:
    return metadata_cache_key("dmm_cover_placeholder", normalize_cover_url(str(url or "").strip()))


def remember_dmm_placeholder_cover(url: str, placeholder: bool = True) -> None:
    target = normalize_cover_url(str(url or "").strip())
    if not target or "pics.dmm.co.jp/mono/movie/adult/" not in target.lower():
        return
    now = time.time()
    value = bool(placeholder)
    DMM_PLACEHOLDER_COVER_CACHE[target] = (now, value)
    try:
        cache_set("dmm_cover_placeholder", dmm_placeholder_cover_cache_key(target), {"placeholder": value, "checked_at": now}, DMM_PLACEHOLDER_COVER_CACHE_TTL)
    except Exception:
        pass


def dmm_placeholder_cover_cached(url: str) -> bool:
    target = normalize_cover_url(str(url or "").strip())
    if not target or "pics.dmm.co.jp/mono/movie/adult/" not in target.lower():
        return False
    now = time.time()
    cached = DMM_PLACEHOLDER_COVER_CACHE.get(target)
    if cached and now - cached[0] < DMM_PLACEHOLDER_COVER_CACHE_TTL:
        return cached[1]
    payload = cache_get("dmm_cover_placeholder", dmm_placeholder_cover_cache_key(target))
    if isinstance(payload, dict):
        result = bool(payload.get("placeholder"))
        DMM_PLACEHOLDER_COVER_CACHE[target] = (now, result)
        return result
    return False


def annotate_unavailable_cover(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    cover = str(row.get("cover") or row.get("cover_url") or "").strip()
    if cover and dmm_placeholder_cover_cached(cover):
        row["cover_unavailable"] = True
        row["cover_unavailable_reason"] = "dmm_noimage"
        row["cover"] = ""
        row["cover_url"] = ""
    return row


def image_proxy_url(source_url: str, av_id: str = "", kind: str = "image", *, immutable: bool = False) -> str:
    url = normalize_cover_url(source_url) if kind == "cover" else str(source_url or "").strip()
    if not url:
        return ""
    if url.startswith(("/api/proxy/image", "data:")):
        return url
    params: dict[str, str] = {"url": url}
    safe_kind = str(kind or "image").strip().lower()
    if safe_kind:
        params["kind"] = safe_kind
    normalized_id = canonical_av_id(av_id)
    if normalized_id:
        params["av_id"] = normalized_id
    if immutable:
        params["immutable"] = "1"
    return f"/api/proxy/image?{urlencode(params)}"


def public_metadata_item(item: dict[str, Any]) -> dict[str, Any]:
    payload = normalize_image_fields({key: value for key, value in dict(item).items() if not str(key).startswith("_")})
    av_id = canonical_av_id(payload.get("id"))
    cover = normalize_cover_url(str(payload.get("cover") or "").strip())
    if av_id and cover:
        payload["id"] = av_id
        payload["cover"] = cover
        payload["cover_proxy"] = image_proxy_url(cover, av_id, "cover", immutable=metadata_item_released(payload))
    return payload


def remember_av_metadata(item: dict[str, Any]) -> dict[str, Any]:
    av_id = canonical_av_id(item.get("id"))
    if not av_id:
        return {}
    payload = public_metadata_item({**item, "id": av_id})
    existing = cached_av_summary(av_id)
    if existing:
        combined = dict(existing)
        for key, value in payload.items():
            if value not in ("", None, [], {}):
                combined[key] = value
        payload = combined
    cache_set("av_summary", av_id, payload, METADATA_DETAIL_TTL)
    return payload


def prewarm_cover_asset(item: dict[str, Any]) -> None:
    if not isinstance(item, dict):
        return
    av_id = canonical_av_id(item.get("id"))
    cover = normalize_cover_url(str(item.get("cover") or "").strip())
    if not av_id or not cover:
        return
    service = get_subscription_service()
    existing = service.get_asset_cache(av_id, "cover")
    if existing and image_source_rank(str(existing.get("source_url") or "")) >= image_source_rank(cover):
        return
    try:
        persist_image_asset(cover, av_id, "cover", metadata_item_released(item))
    except Exception as exc:
        app_log("warning", "asset-cache", "封面资产预热失败", {
            "stage": "asset_cache_prewarm_error",
            "av_id": av_id,
            "cover": cover,
            "error": str(exc),
        })


def schedule_cover_asset_prewarm(items: list[dict[str, Any]], *, limit: int = 14) -> None:
    if os.getenv("SUBSCRIPTION_ASSET_PREWARM", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    selected: list[dict[str, Any]] = []
    safe_limit = max(0, min(40, int(limit or 0)))
    if safe_limit <= 0:
        return
    with asset_prewarm_lock:
        for item in items:
            if not isinstance(item, dict):
                continue
            av_id = canonical_av_id(item.get("id"))
            cover = normalize_cover_url(str(item.get("cover") or "").strip())
            if not av_id or not cover:
                continue
            key = f"cover:{av_id}"
            existing = get_subscription_service().get_asset_cache(av_id, "cover")
            if existing and image_source_rank(str(existing.get("source_url") or "")) >= image_source_rank(cover):
                continue
            if key in asset_prewarm_pending:
                continue
            asset_prewarm_pending.add(key)
            selected.append(public_metadata_item(item))
            if len(selected) >= safe_limit:
                break
    if not selected:
        return

    def worker(batch: list[dict[str, Any]]) -> None:
        for entry in batch:
            key = f"cover:{canonical_av_id(entry.get('id'))}"
            try:
                prewarm_cover_asset(entry)
            finally:
                with asset_prewarm_lock:
                    asset_prewarm_pending.discard(key)

    threading.Thread(target=worker, args=(selected,), name="asset-cover-prewarm", daemon=True).start()


def remember_av_summaries(items: list[dict[str, Any]]) -> None:
    remembered: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            remembered.append(remember_av_metadata(item))
    schedule_cover_asset_prewarm(remembered)


def cached_av_summary(av_id: str) -> dict[str, Any]:
    normalized = canonical_av_id(av_id)
    cached = cache_get("av_summary", normalized)
    if isinstance(cached, dict) and (cached.get("date") or cached.get("release_date")):
        return cached
    detail = cache_get("av_detail", f"id:{normalized}")
    if isinstance(detail, dict):
        if isinstance(cached, dict):
            combined = dict(detail)
            combined.update({key: value for key, value in cached.items() if value not in ("", None, [], {})})
            return combined
        return detail
    return cached if isinstance(cached, dict) else {}


def hydrate_av_with_cached_summary(item: dict[str, Any]) -> dict[str, Any]:
    av_id = canonical_av_id(item.get("id"))
    if not av_id:
        return normalize_image_fields(item)
    cached = cached_av_summary(av_id)
    if not cached:
        return normalize_image_fields(item)
    result = dict(item)
    for field in ("date", "release_date", "duration", "rating", "maker", "label", "director", "source", "source_chain", "source_scope", "match_reason", "confidence"):
        if not result.get(field) and cached.get(field):
            result[field] = cached.get(field)
    if not result.get("title") or result.get("title") == "Product":
        result["title"] = cached.get("title") or result.get("title")
    if not result.get("cover") and cached.get("cover"):
        result["cover"] = cached.get("cover")
    if not result.get("actresses") and cached.get("actresses"):
        result["actresses"] = cached.get("actresses")
    if cached.get("cover_proxy"):
        result["cover_proxy"] = cached.get("cover_proxy")
    return normalize_image_fields(result)


def detail_cache_keys(payload: dict[str, Any] | str) -> list[str]:
    if isinstance(payload, dict):
        av_id = canonical_av_id(payload.get("id"))
        url = str(payload.get("url") or "").strip()
    else:
        av_id = ""
        url = str(payload or "").strip()
    keys: list[str] = []
    if av_id:
        keys.append(f"id:{av_id}")
    if url:
        keys.append(f"url:{hashlib.sha256(url.encode('utf-8')).hexdigest()}")
    return keys


def cached_av_detail(payload: dict[str, Any] | str) -> dict[str, Any]:
    for key in detail_cache_keys(payload):
        cached = cache_get("av_detail", key)
        if isinstance(cached, dict):
            return cached
    return {}


def detail_has_actors(detail: dict[str, Any]) -> bool:
    actors = detail.get("actors") or detail.get("actresses")
    if actors:
        return True
    nested = detail.get("detail") if isinstance(detail.get("detail"), dict) else {}
    return bool(nested.get("actors") or nested.get("actresses"))


def detail_needs_refresh(detail: dict[str, Any], target_url: str) -> bool:
    if not detail:
        return False
    if not allowed_external_url(target_url, DMM_HOSTS):
        return False
    actors = detail.get("actresses") or detail.get("actors") or []
    if not actors and isinstance(detail.get("detail"), dict):
        actors = detail["detail"].get("actresses") or detail["detail"].get("actors") or []
    has_dmm_actor_url = any(isinstance(actor, dict) and str(actor.get("url") or "").strip() for actor in (actors if isinstance(actors, list) else []))
    return not detail_has_actors(detail) or not has_dmm_actor_url


def store_av_detail(payload: dict[str, Any] | str, detail: dict[str, Any]) -> None:
    if not detail:
        return
    for key in detail_cache_keys(payload):
        cache_set("av_detail", key, detail, METADATA_DETAIL_TTL)
    av_id = canonical_av_id(detail.get("id") or (payload.get("id") if isinstance(payload, dict) else ""))
    if av_id:
        normalized_detail = {**detail, "id": av_id}
        cache_set("av_detail", f"id:{av_id}", normalized_detail, METADATA_DETAIL_TTL)
        remember_av_metadata(normalized_detail)


def actress_lookup_name(actress: dict[str, Any]) -> str:
    name = str(actress.get("name") or "").strip()
    if name and not bad_actress_name(name):
        return name
    dmm_name = str(actress.get("dmm_name") or "").strip()
    if dmm_name and not bad_actress_name(dmm_name):
        return dmm_name
    return str(actress.get("id") or "").strip()


def actress_dmm_lookup_url(actress: dict[str, Any]) -> str:
    return str(actress.get("dmm_url") or "").strip()


def actress_javdb_lookup_id(actress: dict[str, Any]) -> str:
    javdb_id = str(actress.get("javdb_id") or "").strip()
    if javdb_id:
        return javdb_id
    source = str(actress.get("source") or "").lower()
    raw_id = str(actress.get("id") or "").strip()
    if source == "dmm":
        return ""
    return raw_id


def actress_identity_key(item: dict[str, Any]) -> str:
    name = str(item.get("name") or item.get("dmm_name") or "").strip().lower()
    if name and not bad_actress_name(name):
        return name
    return str(item.get("id") or "").strip().lower()


def actor_identity_cache_key(value: object) -> str:
    normalized = normalized_person_name(value)
    return f"name:{normalized}" if normalized else ""


def actor_identity_payload(item: dict[str, Any], *, match_reason: str = "", confidence: str = "") -> dict[str, Any]:
    item = normalize_image_fields(item)
    name = str(item.get("name") or item.get("dmm_name") or item.get("id") or "").strip()
    if not name or bad_actress_name(name):
        return {}
    payload = {
        "id": str(item.get("id") or name).strip(),
        "display_name": name,
        "name": name,
        "aliases": sorted(person_name_aliases(name) | person_name_aliases(item.get("dmm_name"))),
        "source_chain": source_chain_for_item(item),
        "source": normalized_source_name(item.get("source")),
        "javdb_id": str(item.get("javdb_id") or (item.get("id") if normalized_source_name(item.get("source")) == "javdb" else "") or "").strip(),
        "dmm_name": str(item.get("dmm_name") or "").strip(),
        "dmm_url": str(item.get("dmm_url") or "").strip(),
        "javlibrary_star_id": str(item.get("javlibrary_star_id") or item.get("star_id") or "").strip(),
        "cover": str(item.get("cover") or "").strip(),
        "latest_cover": str(item.get("latest_cover") or "").strip(),
        "latest_av_id": str(item.get("latest_av_id") or "").strip(),
        "latest_title": str(item.get("latest_title") or "").strip(),
        "latest_date": str(item.get("latest_date") or "").strip(),
        "match_reason": match_reason or str(item.get("match_reason") or "").strip() or "identity_observed",
        "confidence": confidence or str(item.get("confidence") or "").strip() or "medium",
        "updated_at": time.time(),
    }
    return {key: value for key, value in payload.items() if value not in ("", [], {}, None)}


def remember_actor_identity(item: dict[str, Any], *, match_reason: str = "", confidence: str = "") -> dict[str, Any]:
    payload = actor_identity_payload(item, match_reason=match_reason, confidence=confidence)
    if not payload:
        return {}
    keys = set()
    for value in [payload.get("display_name"), payload.get("name"), payload.get("dmm_name"), payload.get("id")]:
        key = actor_identity_cache_key(value)
        if key:
            keys.add(key)
    if payload.get("javdb_id"):
        keys.add(f"javdb:{str(payload['javdb_id']).strip().lower()}")
    if payload.get("javlibrary_star_id"):
        keys.add(f"javlibrary:{str(payload['javlibrary_star_id']).strip().lower()}")
    if payload.get("dmm_url"):
        keys.add(f"dmm_url:{hashlib.sha256(str(payload['dmm_url']).encode('utf-8')).hexdigest()}")
    for key in keys:
        cache_set("actor_identity", key, payload, METADATA_DETAIL_TTL)
    return payload


def actor_identity_canonical_id(payload: dict[str, Any]) -> str:
    for key in ("javdb_id", "javlibrary_star_id", "dmm_url", "canonical_id", "display_name", "name", "dmm_name", "id"):
        value = str(payload.get(key) or "").strip()
        if not value:
            continue
        if key == "dmm_url":
            return f"dmm_url:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
        if key in {"javdb_id", "javlibrary_star_id"}:
            return f"{key}:{value.lower()}"
        normalized = normalized_person_name(value)
        if normalized:
            return f"actor:{normalized}"
    return ""


def normalize_alias_list(value: object) -> list[str]:
    raw_items = value if isinstance(value, list) else re.split(r"[,，、\n]+", str(value or ""))
    aliases: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        key = normalized_person_name(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        aliases.append(text)
    return aliases


def manual_actor_identity_payload(payload: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict(existing or {})
    data = {**base, **(payload or {})}
    display_name = str(data.get("display_name") or data.get("name") or data.get("dmm_name") or data.get("id") or "").strip()
    aliases = normalize_alias_list(data.get("aliases"))
    for value in (display_name, data.get("name"), data.get("dmm_name"), data.get("id")):
        text = str(value or "").strip()
        if text and normalized_person_name(text) not in {normalized_person_name(alias) for alias in aliases}:
            aliases.append(text)
    result = {
        "canonical_id": str(data.get("canonical_id") or "").strip(),
        "id": str(data.get("id") or data.get("javdb_id") or display_name).strip(),
        "display_name": display_name,
        "name": str(data.get("name") or display_name).strip(),
        "aliases": aliases,
        "preferred_source": str(data.get("preferred_source") or "").strip().lower(),
        "source": str(data.get("source") or "manual").strip() or "manual",
        "source_chain": source_chain_for_item(data) or ["manual"],
        "javdb_id": str(data.get("javdb_id") or "").strip(),
        "dmm_name": str(data.get("dmm_name") or "").strip(),
        "dmm_url": str(data.get("dmm_url") or "").strip(),
        "javlibrary_star_id": str(data.get("javlibrary_star_id") or data.get("star_id") or "").strip(),
        "cover": str(data.get("cover") or "").strip(),
        "latest_cover": str(data.get("latest_cover") or "").strip(),
        "latest_av_id": str(data.get("latest_av_id") or "").strip(),
        "latest_title": str(data.get("latest_title") or "").strip(),
        "latest_date": str(data.get("latest_date") or "").strip(),
        "match_reason": "manual_identity",
        "confidence": "manual",
        "locked": bool(data.get("locked", True)),
        "manual": True,
        "updated_at": time.time(),
    }
    if not result["canonical_id"]:
        result["canonical_id"] = actor_identity_canonical_id(result)
    return {key: value for key, value in result.items() if value not in ("", [], {}, None)}


def actor_identity_lookup_keys(payload: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for value in [payload.get("display_name"), payload.get("name"), payload.get("dmm_name"), payload.get("id"), *(payload.get("aliases") or [])]:
        key = actor_identity_cache_key(value)
        if key:
            keys.add(key)
    if payload.get("javdb_id"):
        keys.add(f"javdb:{str(payload['javdb_id']).strip().lower()}")
    if payload.get("javlibrary_star_id"):
        keys.add(f"javlibrary:{str(payload['javlibrary_star_id']).strip().lower()}")
    if payload.get("dmm_url"):
        keys.add(f"dmm_url:{hashlib.sha256(str(payload['dmm_url']).encode('utf-8')).hexdigest()}")
    if payload.get("canonical_id"):
        keys.add(f"canonical:{str(payload['canonical_id']).strip().lower()}")
    return keys


def cached_manual_actor_identity(query: object) -> dict[str, Any]:
    keys: list[str] = []
    key = actor_identity_cache_key(query)
    if key:
        keys.append(key)
    raw = str(query or "").strip()
    if raw:
        keys.extend([f"javdb:{raw.lower()}", f"javlibrary:{raw.lower()}", f"canonical:{raw.lower()}"])
    for cache_key in keys:
        cached = cache_get("actor_identity_manual", cache_key, allow_stale=True)
        if isinstance(cached, dict) and cached:
            return normalize_image_fields(cached)
    return {}


def save_manual_actor_identity(payload: dict[str, Any]) -> dict[str, Any]:
    service = get_subscription_service()
    normalized = manual_actor_identity_payload(payload)
    normalized["canonical_id"] = actor_identity_canonical_id({**normalized, "canonical_id": ""})
    canonical_id = str(normalized.get("canonical_id") or "").strip()
    if not canonical_id:
        raise ValueError("缺少可识别的演员名称或外部 ID")
    normalized_lookup_keys = actor_identity_lookup_keys(normalized)
    normalized_group_keys = actor_identity_group_keys(actor_identity_public_record(normalized, origin="manual"))
    for row in service.list_metadata_cache("actor_identity_manual", limit=50000):
        data = row.get("data") if isinstance(row, dict) else {}
        if not isinstance(data, dict):
            continue
        data_canonical = str(data.get("canonical_id") or "").strip()
        data_lookup_keys = actor_identity_lookup_keys(data)
        data_group_keys = actor_identity_group_keys(actor_identity_public_record(data, origin="manual", cache_key=str(row.get("cache_key") or "")))
        if data_canonical == canonical_id or normalized_lookup_keys & data_lookup_keys or normalized_group_keys & data_group_keys:
            service.delete_metadata_cache("actor_identity_manual", str(row.get("cache_key") or ""))
    for key in actor_identity_lookup_keys(normalized):
        cache_set("actor_identity_manual", key, normalized, METADATA_DETAIL_TTL * 12)
    return normalized


def cached_actor_identity(query: object) -> dict[str, Any]:
    manual = cached_manual_actor_identity(query)
    if manual:
        return manual
    keys: list[str] = []
    key = actor_identity_cache_key(query)
    if key:
        keys.append(key)
    raw = str(query or "").strip()
    if raw:
        keys.append(f"javdb:{raw.lower()}")
        keys.append(f"javlibrary:{raw.lower()}")
    for cache_key in keys:
        cached = cache_get("actor_identity", cache_key, allow_stale=True)
        if isinstance(cached, dict) and cached:
            return normalize_image_fields(cached)
    return {}


def maker_identity_key(value: object) -> str:
    key = str(value or "").strip().lower()
    return f"name:{key}" if key else ""


def remember_maker_identity(maker_name: str) -> dict[str, Any]:
    name = str(maker_name or "").strip()
    if not name:
        return {}
    key = str(name).strip().lower()
    payload = {
        "id": key,
        "display_name": name,
        "name": name,
        "dmm_listing_urls": dmm_listing_urls_for_maker(name),
        "dmm_primary_labels": list(dmm_primary_label_aliases(name)),
        "javlibrary_urls": javlibrary_urls_for_maker(name),
        "javdb_url": next(
            (str(item.get("url") or "") for item in get_subscription_service().get_settings().get("pinned_makers") or []
             if isinstance(item, dict) and str(item.get("name") or "").strip().lower() == key),
            "",
        ),
        "match_reason": "maker_config",
        "confidence": "high" if dmm_primary_label_aliases(name) or javlibrary_urls_for_maker(name) else "medium",
        "updated_at": time.time(),
    }
    payload = {k: v for k, v in payload.items() if v not in ("", [], {}, None)}
    cache_key = maker_identity_key(name)
    if cache_key:
        cache_set("maker_identity", cache_key, payload, METADATA_DETAIL_TTL)
    return payload


def merge_actress_sources(javdb_results: list[dict[str, Any]], dmm_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in javdb_results:
        if not isinstance(raw, dict):
            continue
        item = public_metadata_item(dict(raw))
        item["source"] = str(item.get("source") or "javdb")
        item = append_source_chain(item, item["source"])
        if item["source"] == "javdb":
            item["javdb_id"] = str(item.get("id") or "").strip()
        item = explain_match(item, default_reason="actress_search", default_confidence="medium")
        key = actress_identity_key(item)
        if key:
            merged[key] = item
            remember_actor_identity(item, match_reason=str(item.get("match_reason") or ""), confidence=str(item.get("confidence") or ""))
    for raw in dmm_results:
        if not isinstance(raw, dict):
            continue
        item = public_metadata_item(dict(raw))
        item["source"] = "dmm"
        item = append_source_chain(item, "dmm")
        item["dmm_name"] = str(item.get("dmm_name") or item.get("name") or item.get("id") or "").strip()
        if item.get("url") and allowed_external_url(str(item.get("url") or ""), DMM_HOSTS):
            item["dmm_url"] = item.get("url")
        item = explain_match(item, default_reason="dmm_actress_search", default_confidence="high")
        key = actress_identity_key(item)
        if not key:
            continue
        existing = merged.get(key)
        if existing:
            combined = dict(existing)
            existing_source = str(existing.get("source") or "").strip()
            combined["source"] = existing_source if "dmm" in existing_source else f"{existing_source}+dmm".strip("+")
            combined = append_source_chain(combined, *source_chain_for_item(existing), *source_chain_for_item(item))
            combined["dmm_name"] = item.get("dmm_name") or item.get("name") or combined.get("dmm_name", "")
            combined["dmm_url"] = item.get("dmm_url") or combined.get("dmm_url", "")
            combined["latest"] = item.get("latest") or combined.get("latest")
            combined["latest_cover"] = item.get("latest_cover") or combined.get("latest_cover", "")
            combined["latest_av_id"] = item.get("latest_av_id") or combined.get("latest_av_id", "")
            combined["latest_title"] = item.get("latest_title") or combined.get("latest_title", "")
            combined["latest_date"] = item.get("latest_date") or combined.get("latest_date", "")
            if not combined.get("cover"):
                combined["cover"] = item.get("cover", "")
            combined = explain_match(combined, default_reason="merged_actress_identity", default_confidence="high")
            merged[key] = combined
        else:
            merged[key] = item
        remember_actor_identity(merged[key], match_reason=str(merged[key].get("match_reason") or ""), confidence=str(merged[key].get("confidence") or ""))
    return list(merged.values())


def normalized_person_name(value: object) -> str:
    return re.sub(r"[\s・･._\\-]+", "", str(value or "").strip().lower())


def person_name_aliases(value: object) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    aliases = {text}
    for match in re.finditer(r"[（(]([^（）()]+)[）)]", text):
        aliases.add(match.group(1).strip())
    aliases.add(re.sub(r"[（(].*?[）)]", "", text).strip())
    parts = re.split(r"\s*[、,/|]\s*", text)
    aliases.update(part.strip() for part in parts if part.strip())
    return {normalized for alias in aliases if (normalized := normalized_person_name(alias))}


def actress_aliases(actress: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for key in ("name", "dmm_name", "source_actress_name", "id"):
        value = str(actress.get(key) or "").strip()
        if value and not bad_actress_name(value):
            aliases.update(person_name_aliases(value))
    return aliases


def actor_matches_actress(actor: dict[str, Any], targets: set[str]) -> bool:
    if not targets:
        return False
    aliases: set[str] = set()
    for key in ("name", "dmm_name", "value"):
        aliases.update(person_name_aliases(actor.get(key) if isinstance(actor, dict) else actor))
    return bool(aliases & targets)


def av_matches_actress(av: dict[str, Any], actress: dict[str, Any]) -> bool:
    targets = actress_aliases(actress)
    if not targets:
        return True
    actors = normalize_actor_items(av.get("actors") or av.get("actresses") or av.get("actress"))
    if actors:
        return any(actor_matches_actress(actor, targets) for actor in actors)
    title_key = normalized_person_name(av.get("title"))
    return any(target and target in title_key for target in targets)


def dmm_actress_queries(actress: dict[str, Any]) -> list[str]:
    values = [
        actress.get("dmm_name"),
        actress.get("name"),
        actress_lookup_name(actress),
        actress.get("id"),
    ]
    queries: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or bad_actress_name(text):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append(text)
    return queries


def javlibrary_video_actresses(av_id: str) -> list[dict[str, Any]]:
    normalized_id = canonical_av_id(av_id)
    if not normalized_id:
        return []
    key = metadata_cache_key("video", normalized_id)
    cached = cache_get("javlibrary_video_actors", key)
    if isinstance(cached, list):
        return [item for item in cached if isinstance(item, dict)]
    try:
        actors = javlibrary.get_video_actresses(normalized_id)
    except Exception as exc:
        stale = cache_get("javlibrary_video_actors", key, allow_stale=True)
        if isinstance(stale, list):
            app_log("warning", "javlibrary", "JavLibrary 番号演员反查失败，返回过期缓存", {
                "stage": "javlibrary_video_actors_stale",
                "av_id": normalized_id,
                "error": str(exc),
            })
            return [item for item in stale if isinstance(item, dict)]
        app_log("warning", "javlibrary", "JavLibrary 番号演员反查失败", {
            "stage": "javlibrary_video_actors_error",
            "av_id": normalized_id,
            "error": str(exc),
        })
        return []
    if actors:
        cache_set("javlibrary_video_actors", key, actors, METADATA_DETAIL_TTL)
    return actors


def cache_javlibrary_actor_map(name: str, star_id: str) -> None:
    target_id = str(star_id or "").strip()
    if not target_id:
        return
    for alias in person_name_aliases(name):
        cache_set("javlibrary_actor_map", alias, target_id, METADATA_JAVLIBRARY_MAP_TTL)


def javlibrary_seed_disallowed(seed: dict[str, Any]) -> bool:
    if actor_count_from_summary(seed) > GLOBAL_MAX_COACTORS:
        return True
    text = f"{seed.get('id') or ''} {seed.get('title') or ''}".lower()
    return any(token in text for token in (
        "best",
        "総集",
        "総集編",
        "合集",
        "精選",
        "精选",
        "オールスター",
        "all star",
        "コラボ",
        "collab",
        "×",
    ))


def javlibrary_get_listing_avs(url: str, limit: int, *, retries: int = 3, timeout_ms: int = 120000) -> list[dict[str, Any]]:
    try:
        return javlibrary.get_listing_avs(url, limit=limit, retries=retries, timeout_ms=timeout_ms)
    except TypeError:
        return javlibrary.get_listing_avs(url, limit=limit)


def javlibrary_get_actor_avs(star_id: str, limit: int, *, retries: int = 3, timeout_ms: int = 120000) -> list[dict[str, Any]]:
    try:
        return javlibrary.get_actor_avs(star_id, limit=limit, retries=retries, timeout_ms=timeout_ms)
    except TypeError:
        return javlibrary.get_actor_avs(star_id, limit=limit)


def javlibrary_actor_star_id(actress: dict[str, Any], seed_avs: list[dict[str, Any]] | None = None) -> str:
    anchored_star_id = str(actress.get("javlibrary_star_id") or "").strip()
    if anchored_star_id:
        for value in dmm_actress_queries(actress):
            cache_javlibrary_actor_map(value, anchored_star_id)
        return anchored_star_id
    for value in dmm_actress_queries(actress):
        normalized = normalized_person_name(value)
        if not normalized:
            continue
        cached = cache_get("javlibrary_actor_map", normalized)
        if isinstance(cached, str) and cached.strip():
            return cached.strip()
        for name, star_id in JAVLIBRARY_ACTOR_IDS.items():
            if normalized_person_name(name) == normalized:
                cache_javlibrary_actor_map(name, star_id)
                return star_id
    targets = actress_aliases(actress)
    if not targets:
        return ""
    for seed in seed_avs or []:
        if not isinstance(seed, dict):
            continue
        if javlibrary_seed_disallowed(seed):
            continue
        av_id = canonical_av_id(seed.get("id") or seed.get("code"))
        if not av_id:
            continue
        actors = javlibrary_video_actresses(av_id)
        if len(actors) > GLOBAL_MAX_COACTORS:
            app_log("info", "javlibrary", "跳过超过共演人数限制的 JavLibrary 反查锚点", {
                "stage": "javlibrary_actor_map_seed_skip",
                "av_id": av_id,
                "actor_count": len(actors),
                "max_coactors": GLOBAL_MAX_COACTORS,
            })
            continue
        for actor in actors:
            star_id = str(actor.get("star_id") or actor.get("id") or "").strip()
            actor_name = str(actor.get("name") or "").strip()
            if not star_id or not actor_name:
                continue
            if actor_matches_actress(actor, targets):
                cache_javlibrary_actor_map(actor_name, star_id)
                for value in dmm_actress_queries(actress):
                    cache_javlibrary_actor_map(value, star_id)
                app_log("info", "javlibrary", "已通过番号详情反查 JavLibrary 女优 ID", {
                    "stage": "javlibrary_actor_map_from_video",
                    "actress": actress_lookup_name(actress),
                    "av_id": av_id,
                    "star_id": star_id,
                })
                return star_id
    return ""


def javlibrary_actor_avs(actress: dict[str, Any], limit: int, seed_avs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    star_id = javlibrary_actor_star_id(actress, seed_avs=seed_avs)
    if not star_id:
        return []
    key = metadata_cache_key("star", star_id)
    cached = cache_get("javlibrary_actor_avs", key)
    if isinstance(cached, list):
        return [
            item for item in cached
            if isinstance(item, dict) and not javlibrary_seed_disallowed({"id": item.get("id", ""), "title": item.get("title", "")})
        ][:limit]
    try:
        items = javlibrary_get_actor_avs(star_id, limit=max(limit, 20), retries=1, timeout_ms=60000)
    except Exception as exc:
        stale = cache_get("javlibrary_actor_avs", key, allow_stale=True)
        if isinstance(stale, list):
            app_log("warning", "javlibrary", "JavLibrary 女优作品抓取失败，返回过期缓存", {
                "stage": "javlibrary_actor_avs_stale",
                "star_id": star_id,
                "error": str(exc),
            })
            return [
                item for item in stale
                if isinstance(item, dict) and not javlibrary_seed_disallowed({"id": item.get("id", ""), "title": item.get("title", "")})
            ][:limit]
        app_log("warning", "javlibrary", "JavLibrary 女优作品抓取失败", {
            "stage": "javlibrary_actor_avs_error",
            "star_id": star_id,
            "error": str(exc),
        })
        return []
    actor_name = actress_lookup_name(actress)
    normalized: list[dict[str, Any]] = []
    for item in items:
        av_id = canonical_av_id(item.get("id") or item.get("code"))
        if not av_id:
            continue
        if javlibrary_seed_disallowed({"id": av_id, "title": item.get("title", "")}):
            continue
        payload = {
            **item,
            "id": av_id,
            "source": "javlibrary",
            "actresses": [{"name": actor_name}] if actor_name else [],
        }
        normalized.append(payload)
    if normalized:
        cache_set("javlibrary_actor_avs", key, [public_metadata_item(item) for item in normalized], METADATA_JAVLIBRARY_LIST_TTL)
    return normalized[:limit]


def javlibrary_urls_for_maker(maker_name: str) -> list[str]:
    key = str(maker_name or "").strip().lower()
    return list(JAVLIBRARY_MAKER_URLS.get(key, []))


def javlibrary_maker_scope(url: str) -> str:
    target = str(url or "").lower()
    if "vl_label.php" in target:
        return "label"
    if "vl_maker.php" in target:
        return "maker"
    return ""


def sort_javlibrary_maker_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[str, int, str]:
        scope = str(item.get("source_scope") or "").lower()
        scope_rank = 1 if scope == "label" else 0
        return (av_date_key(item), scope_rank, str(item.get("id") or ""))

    return sorted(items, key=key, reverse=True)


def javlibrary_maker_avs(maker_name: str, limit: int) -> list[dict[str, Any]]:
    urls = javlibrary_urls_for_maker(maker_name)
    if not urls:
        return []
    merged: list[dict[str, Any]] = []
    for url in urls:
        scope = javlibrary_maker_scope(url)
        key = metadata_cache_key("url", url)
        cached = cache_get("javlibrary_maker_avs", key)
        if isinstance(cached, list):
            merged.extend(
                {**item, "source_scope": item.get("source_scope") or scope} for item in cached
                if isinstance(item, dict) and not javlibrary_seed_disallowed({"id": item.get("id", ""), "title": item.get("title", "")})
            )
            continue
        try:
            items = javlibrary_get_listing_avs(url, limit=max(limit, 20), retries=0, timeout_ms=45000)
        except Exception as exc:
            stale = cache_get("javlibrary_maker_avs", key, allow_stale=True)
            if isinstance(stale, list):
                app_log("warning", "javlibrary", "JavLibrary 厂牌抓取失败，返回过期缓存", {
                    "stage": "javlibrary_maker_avs_stale",
                    "maker": maker_name,
                    "url": url,
                    "error": str(exc),
                })
                merged.extend(
                    {**item, "source_scope": item.get("source_scope") or scope} for item in stale
                    if isinstance(item, dict) and not javlibrary_seed_disallowed({"id": item.get("id", ""), "title": item.get("title", "")})
                )
                continue
            app_log("warning", "javlibrary", "JavLibrary 厂牌抓取失败", {
                "stage": "javlibrary_maker_avs_error",
                "maker": maker_name,
                "url": url,
                "error": str(exc),
            })
            continue
        normalized: list[dict[str, Any]] = []
        for item in items:
            av_id = canonical_av_id(item.get("id") or item.get("code"))
            if not av_id:
                continue
            if javlibrary_seed_disallowed({"id": av_id, "title": item.get("title", "")}):
                continue
            normalized.append({**item, "id": av_id, "source": "javlibrary", "maker": maker_name, "source_scope": scope})
        if normalized:
            cache_set("javlibrary_maker_avs", key, [public_metadata_item(item) for item in normalized], METADATA_JAVLIBRARY_LIST_TTL)
            merged.extend(normalized)
    dedupe_order = sorted(merged, key=lambda item: 1 if str(item.get("source_scope") or "").lower() == "label" else 0)
    return sort_javlibrary_maker_items(merge_av_sources(dedupe_order))[:limit]


def enrich_dmm_actress_items(items: list[dict[str, Any]], actress: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    if not items:
        return []
    detail_limit = max(4, min(len(items), 8))
    verified: list[dict[str, Any]] = []
    for item in items[:detail_limit]:
        if not isinstance(item, dict):
            continue
        merged = dict(item)
        if actor_count_from_summary(merged) > GLOBAL_MAX_COACTORS:
            continue
        if av_matches_actress(merged, actress):
            verified.append(merged)
            continue
        title = str(item.get("title") or "").strip()
        if title and title.lower() != "product":
            continue
        url = str(item.get("url") or "")
        if url and allowed_external_url(url, DMM_HOSTS):
            try:
                detail = cached_detail_for_url(url) or dmm.get_av_detail(item)
                if detail:
                    store_av_detail(item, detail)
                    merged = {**item, **detail}
            except Exception as exc:
                app_log("warning", "subscription", "DMM/FANZA 女优作品详情校验失败", {
                    "stage": "actress_dmm_detail_verify_error",
                    "actress": actress_lookup_name(actress),
                    "av_id": item.get("id", ""),
                    "url": url,
                    "error": str(exc),
                })
        if av_matches_actress(merged, actress):
            verified.append(merged)
    for item in items[detail_limit:]:
        if not isinstance(item, dict):
            continue
        if actor_count_from_summary(item) > GLOBAL_MAX_COACTORS:
            continue
        if av_matches_actress(item, actress):
            verified.append(item)
    return verified


def subscription_avs_for_actress(actress: dict[str, Any], limit: int = 100, *, include_dmm_detail: bool = False) -> list[dict[str, Any]]:
    actress = dict(actress or {})
    for identity_query in (actress.get("javdb_id"), actress.get("id"), actress.get("name"), actress.get("dmm_name")):
        identity = cached_actor_identity(identity_query)
        if not identity:
            continue
        for field in ("javdb_id", "dmm_name", "dmm_url", "javlibrary_star_id", "preferred_source", "cover", "latest_cover", "latest_av_id", "latest_title", "latest_date"):
            if identity.get(field) and not actress.get(field):
                actress[field] = identity[field]
        if identity.get("name") and bad_actress_name(actress.get("name")):
            actress["name"] = identity["name"]
        break
    actress_id = str(actress.get("id") or "").strip()
    javdb_id = actress_javdb_lookup_id(actress)
    lookup_name = actress_lookup_name(actress)
    dmm_url = actress_dmm_lookup_url(actress)
    source_strategy = normalize_source_preference(actress.get("preferred_source"), "auto")
    javlibrary_star_id = str(actress.get("javlibrary_star_id") or "").strip()
    cache_key = metadata_cache_key("v9", "actress_avs", actress_id, javdb_id, lookup_name, dmm_url, javlibrary_star_id, source_strategy)
    cached = cache_get("actress_avs", cache_key) if cache_key else None
    if isinstance(cached, list):
        cached = [
            public_metadata_item(explain_match({**item, "_cache_hit": True}, default_reason="sqlite_cache", default_confidence="high"))
            for item in cached
            if isinstance(item, dict)
        ]
        cached = filter_avs_by_actor_limit(cached, context="actress_avs_cache")
        remember_av_summaries(cached)
        app_log("info", "metadata-cache", "女优作品列表命中 SQLite 缓存", {
            "stage": "actress_avs_sqlite_cache_hit",
            "actress_id": actress_id,
            "actress": lookup_name,
            "count": len(cached),
        })
        return cached[:limit]

    javlibrary_results: list[dict[str, Any]] = []
    dmm_results: list[dict[str, Any]] = []
    javdb_results: list[dict[str, Any]] = []

    def fetch_dmm_actress() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for query in dmm_actress_queries(actress):
            try:
                rows = dmm.get_actress_avs(query, limit=min(max(limit, 40), 100), include_detail=False)
                results.extend(enrich_dmm_actress_items(rows, {**actress, "dmm_name": query}, limit))
            except Exception as exc:
                app_log("warning", "subscription", "DMM/FANZA 女优名字搜索失败", {
                    "stage": "actress_avs_dmm_search_error",
                    "actress_id": actress_id,
                    "actress": lookup_name,
                    "query": query,
                    "error": str(exc),
                })
        if dmm_url and len(merge_av_sources(results)) < min(limit, 20):
            try:
                page_results = dmm.get_listing_avs(dmm_url, limit=min(max(limit, 40), 100), include_detail=False)
                results.extend(enrich_dmm_actress_items(page_results, actress, limit))
            except Exception as exc:
                app_log("warning", "subscription", "DMM 女优专页作品读取失败，尝试按名字搜索", {
                    "stage": "actress_avs_dmm_url_error",
                    "actress_id": actress_id,
                    "actress": lookup_name,
                    "dmm_url": dmm_url,
                    "error": str(exc),
                })
        return results

    def fetch_javdb_actress() -> list[dict[str, Any]]:
        if not javdb_source_enabled():
            app_log("info", "subscription", "JavDB 数据源已关闭，跳过女优作品读取", {
                "stage": "javdb_source_disabled",
                "context": "actress_avs",
                "actress_id": actress_id,
                "javdb_id": javdb_id,
            })
            return []
        if not javdb_id:
            return []
        try:
            return javdb.get_actress_avs(javdb_id, limit=limit)
        except Exception as exc:
            app_log("warning", "subscription", "JavDB 女优作品读取失败", {
                "stage": "actress_avs_javdb_error",
                "actress_id": actress_id,
                "javdb_id": javdb_id,
                "error": str(exc),
            })
            return []

    javlibrary_first = source_strategy == "javlibrary" or (source_strategy == "auto" and bool(javlibrary_star_id))
    if javlibrary_first and javlibrary_star_id:
        javlibrary_results = javlibrary_actor_avs(actress, limit, seed_avs=[])
        if len(merge_av_sources(javlibrary_results)) < min(limit, 20):
            dmm_results = fetch_dmm_actress()
        if len(merge_av_sources(javlibrary_results, dmm_results)) < min(limit, 20):
            javdb_results = fetch_javdb_actress()
    elif source_strategy == "javdb":
        javdb_results = fetch_javdb_actress()
        if len(merge_av_sources(javdb_results)) < min(limit, 20):
            dmm_results = fetch_dmm_actress()
        if len(merge_av_sources(javdb_results, dmm_results)) < min(limit, 20):
            javlibrary_results = javlibrary_actor_avs(actress, limit, seed_avs=merge_av_sources(dmm_results)[:6])
    else:
        dmm_results = fetch_dmm_actress()
        if len(merge_av_sources(dmm_results)) < min(limit, 20):
            javlibrary_results = javlibrary_actor_avs(actress, limit, seed_avs=merge_av_sources(dmm_results)[:6])
        if len(merge_av_sources(javdb_results, javlibrary_results, dmm_results)) < min(limit, 20):
            javdb_results = fetch_javdb_actress()
    dmm_results = [
        explain_match(append_source_chain(item, "dmm"), default_reason="actress_dmm_match", default_confidence="high")
        for item in dmm_results
        if isinstance(item, dict)
    ]
    javlibrary_results = [
        explain_match(append_source_chain(item, "javlibrary"), default_reason="actress_seed", default_confidence="high")
        for item in javlibrary_results
        if isinstance(item, dict)
    ]
    javdb_results = [
        explain_match(append_source_chain(item, "javdb"), default_reason="actress_javdb_fallback", default_confidence="medium")
        for item in javdb_results
        if isinstance(item, dict)
    ]
    merged = filter_avs_by_actor_limit(
        [public_metadata_item(explain_match(item, default_reason="actress_merged", default_confidence="medium")) for item in merge_av_sources(javdb_results, javlibrary_results, dmm_results)],
        context="actress_avs",
    )
    remember_av_summaries(merged)
    if cache_key and merged:
        cache_set("actress_avs", cache_key, [public_metadata_item(item) for item in merged], METADATA_ACTRESS_AVS_TTL)
    app_log("info", "subscription", "女优作品数据源合并完成", {
        "stage": "actress_avs_merged",
        "actress_id": actress_id,
        "actress": lookup_name,
        "source_strategy": source_strategy,
        "javdb_count": len(javdb_results),
        "javlibrary_count": len(javlibrary_results),
        "dmm_count": len(dmm_results),
        "merged_count": len(merged),
    })
    return merged[:limit]


def prepare_subscription_av_payload(av: dict[str, Any], *, allow_live_detail: bool = True) -> dict[str, Any]:
    payload = dict(av)
    cached_detail = cached_av_detail(payload)
    payload_url = str(payload.get("url") or "")
    if cached_detail and not detail_needs_refresh(cached_detail, payload_url):
        payload = {**payload, **cached_detail}
        payload["detail"] = cached_detail.get("detail") if isinstance(cached_detail.get("detail"), dict) else cached_detail
        return payload
    if not allow_live_detail:
        if cached_detail:
            payload = {**payload, **cached_detail}
            payload["detail"] = cached_detail.get("detail") if isinstance(cached_detail.get("detail"), dict) else cached_detail
        return payload
    if str(payload.get("source") or "").lower() == "dmm" and payload.get("url"):
        try:
            detail = cached_detail_for_url(str(payload.get("url") or "")) or dmm.get_av_detail(payload)
            if detail:
                store_av_detail(payload, detail)
                payload = {**payload, **detail}
                payload["detail"] = detail.get("detail") if isinstance(detail.get("detail"), dict) else detail
        except Exception as exc:
            app_log("warning", "subscription", "DMM 番号详情补全失败", {
                "stage": "dmm_detail_enrich_error",
                "av_id": payload.get("id", ""),
                "url": payload.get("url", ""),
                "error": str(exc),
            })
    return payload


def fetch_subscription_search(q: str, search_type: str) -> list[dict[str, Any]]:
    query = str(q or "").strip()
    if not query:
        return []
    if search_type == "actress":
        javdb_results: list[dict[str, Any]] = []
        dmm_results: list[dict[str, Any]] = []
        javlibrary_results: list[dict[str, Any]] = []
        dmm_works: list[dict[str, Any]] = []
        try:
            dmm_works = dmm.get_actress_avs(query, limit=12, include_detail=False)
            dmm_works = enrich_dmm_actress_items(dmm_works, {"id": query, "name": query, "dmm_name": query}, 12)
            dmm_works = filter_avs_by_actor_limit(dmm_works, context="actress_search_latest")
            remember_av_summaries(dmm_works)
            if dmm_works:
                dmm_results = [{
                    "id": query,
                    "name": query,
                    "dmm_name": query,
                    "source": "dmm",
                    "latest": dmm_works[0],
                    "latest_cover": dmm_works[0].get("cover", ""),
                    "latest_av_id": dmm_works[0].get("id", ""),
                    "latest_title": dmm_works[0].get("title", ""),
                    "latest_date": dmm_works[0].get("date") or dmm_works[0].get("release_date") or "",
                }]
        except Exception as exc:
            app_log("warning", "subscription", "DMM/FANZA 女优搜索失败", {
                "stage": "actress_search_dmm_error",
                "query": query,
                "error": str(exc),
            })
        if not dmm_results:
            jl_works = javlibrary_actor_avs({"id": query, "name": query}, 12, seed_avs=dmm_works[:3])
            if jl_works:
                remember_av_summaries(jl_works)
                javlibrary_results = [{
                    "id": query,
                    "name": query,
                    "source": "javlibrary",
                    "latest": jl_works[0],
                    "latest_cover": jl_works[0].get("cover", ""),
                    "latest_av_id": jl_works[0].get("id", ""),
                    "latest_title": jl_works[0].get("title", ""),
                    "latest_date": jl_works[0].get("date") or jl_works[0].get("release_date") or "",
                }]
        if javdb_source_enabled() and not dmm_results and not javlibrary_results:
            try:
                javdb_results = javdb.search_actress(query)
            except Exception as exc:
                app_log("warning", "subscription", "JavDB 女优搜索失败", {
                    "stage": "actress_search_javdb_error",
                    "query": query,
                    "error": str(exc),
                })
        results = merge_actress_sources(javdb_results + javlibrary_results, dmm_results)
        return [public_metadata_item(item) for item in results if isinstance(item, dict)]
    dmm_avs: list[dict[str, Any]] = []
    javdb_avs: list[dict[str, Any]] = []
    try:
        dmm_avs = dmm.search_av(query, limit=12, include_detail=True)
    except Exception as exc:
        app_log("warning", "subscription", "DMM/FANZA 番号搜索失败", {"stage": "av_search_dmm_error", "query": query, "error": str(exc)})
    if javdb_source_enabled() and not dmm_avs:
        try:
            javdb_avs = javdb.search_av(query)
        except Exception as exc:
            app_log("warning", "subscription", "JavDB 番号搜索失败", {"stage": "av_search_javdb_error", "query": query, "error": str(exc)})
    dmm_avs = [
        explain_match(append_source_chain(item, "dmm"), default_reason="exact_av_id", default_confidence="high")
        for item in dmm_avs
        if isinstance(item, dict)
    ]
    javdb_avs = [
        explain_match(append_source_chain(item, "javdb"), default_reason="javdb_av_fallback", default_confidence="medium")
        for item in javdb_avs
        if isinstance(item, dict)
    ]
    results = filter_avs_by_actor_limit(
        [explain_match(item, default_reason="av_search", default_confidence="medium") for item in merge_av_sources(dmm_avs, javdb_avs)],
        context="av_search",
    )
    remember_av_summaries(results)
    return [public_metadata_item(item) for item in results]


def cached_subscription_search(q: str, search_type: str) -> list[dict[str, Any]]:
    query = str(q or "").strip()
    safe_type = "actress" if search_type == "actress" else "av"
    key = metadata_cache_key("v8", safe_type, query)
    result = cached_metadata(
        "subscription_search",
        key,
        METADATA_SEARCH_TTL,
        lambda: fetch_subscription_search(query, safe_type),
    )
    if not isinstance(result, list):
        return []
    return [
        explain_match(normalize_image_fields(item), default_reason=f"{safe_type}_search", default_confidence="medium") if isinstance(item, dict) else item
        for item in result
    ]


def cached_actress_profile(actress_id: str) -> dict[str, Any]:
    if not javdb_source_enabled():
        stale = cache_get("actress_profile", metadata_cache_key("profile", actress_id), allow_stale=True)
        return stale if isinstance(stale, dict) else {}
    key = metadata_cache_key("profile", actress_id)
    result = cached_metadata(
        "actress_profile",
        key,
        METADATA_PROFILE_TTL,
        lambda: javdb.get_actress_profile(actress_id) or {},
    )
    return result if isinstance(result, dict) else {}


def cached_actress_identity(query: str) -> dict[str, Any]:
    value = str(query or "").strip()
    if not value:
        return {}
    local_identity = cached_actor_identity(value)
    if local_identity:
        return explain_match(local_identity, default_reason="actor_identity_cache", default_confidence="high")
    candidates = cached_subscription_search(value, "actress")
    if not candidates:
        return {}
    value_key = value.lower()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip().lower() == value_key or str(item.get("id") or "").strip().lower() == value_key:
            return item
    return next((item for item in candidates if isinstance(item, dict)), {})


def maker_name_for_listing_url(page_url: str, explicit_name: str = "") -> str:
    name = str(explicit_name or "").strip()
    if name:
        return name
    target_url = str(page_url or "").strip()
    if not target_url:
        return ""
    for maker in get_subscription_service().get_settings().get("pinned_makers") or []:
        if not isinstance(maker, dict):
            continue
        if str(maker.get("url") or "").strip() == target_url:
            return str(maker.get("name") or "").strip()
    return ""


def normalize_source_preference(value: object, fallback: str = "auto") -> str:
    source = str(value or "").strip().lower()
    return source if source in {"auto", "javlibrary", "dmm", "javdb"} else fallback


def maker_listing_source_strategy(maker_name: str, page_url: str = "") -> str:
    key = str(maker_name or "").strip().lower()
    target_url = str(page_url or "").strip()
    for maker in get_subscription_service().get_settings().get("pinned_makers") or []:
        if not isinstance(maker, dict):
            continue
        maker_key = str(maker.get("name") or "").strip().lower()
        maker_url = str(maker.get("url") or "").strip()
        if (key and maker_key == key) or (target_url and maker_url == target_url):
            return normalize_source_preference(maker.get("preferred_listing_source") or maker.get("preferred_source"), "auto")
    if key in JAVLIBRARY_MAKER_URLS:
        return "javlibrary"
    return "auto"


def dmm_listing_urls_for_maker(maker_name: str) -> list[str]:
    key = str(maker_name or "").strip().lower()
    return list(DMM_MAKER_LIST_URLS.get(key, []))


def dmm_search_terms_for_maker(maker_name: str) -> list[str]:
    key = str(maker_name or "").strip().lower()
    terms = list(DMM_MAKER_SEARCH_TERMS.get(key, ()))
    if maker_name and maker_name not in terms:
        terms.append(maker_name)
    return [term for term in terms if str(term or "").strip()]


def dmm_detail_matches_maker(item: dict[str, Any], maker_name: str) -> bool:
    maker = str(item.get("maker") or "").strip().lower()
    if not maker:
        nested = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        maker = str(nested.get("maker") or "").strip().lower()
    if not maker:
        return True
    key = str(maker_name or "").strip().lower()
    aliases = DMM_MAKER_ALIASES.get(key, (key,))
    return any(str(alias or "").strip().lower() and str(alias or "").strip().lower() in maker for alias in aliases)


def dmm_primary_label_aliases(maker_name: str) -> tuple[str, ...]:
    key = str(maker_name or "").strip().lower()
    return DMM_MAKER_PRIMARY_LABELS.get(key, ())


def dmm_item_label(item: dict[str, Any]) -> str:
    label = str(item.get("label") or "").strip()
    if label:
        return label
    nested = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    return str(nested.get("label") or "").strip()


def dmm_detail_matches_primary_label(item: dict[str, Any], maker_name: str) -> bool:
    label = dmm_item_label(item).lower()
    if not label:
        return False
    return any(str(alias or "").strip().lower() and str(alias or "").strip().lower() in label for alias in dmm_primary_label_aliases(maker_name))


def prioritize_dmm_maker_labels(items: list[dict[str, Any]], maker_name: str, limit: int) -> list[dict[str, Any]]:
    if not dmm_primary_label_aliases(maker_name):
        return items
    preferred: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in merge_av_sources(items):
        av_id = canonical_av_id(item.get("id"))
        if not av_id or av_id in seen:
            continue
        seen.add(av_id)
        if dmm_detail_matches_primary_label(item, maker_name):
            preferred.append({**item, "source_scope": "label"})
        else:
            fallback.append({**item, "source_scope": "maker"})
    return preferred or fallback


def sort_maker_listing_items(items: list[dict[str, Any]], maker_name: str) -> list[dict[str, Any]]:
    if not dmm_primary_label_aliases(maker_name):
        return sorted(items, key=lambda item: (av_date_key(item), str(item.get("id") or "")), reverse=True)

    def key(item: dict[str, Any]) -> tuple[int, str, str]:
        label_rank = 1 if str(item.get("source_scope") or "").lower() == "label" or dmm_detail_matches_primary_label(item, maker_name) else 0
        return (label_rank, av_date_key(item), str(item.get("id") or ""))

    return sorted(items, key=key, reverse=True)


def enrich_dmm_maker_items(items: list[dict[str, Any]], maker_name: str, limit: int) -> list[dict[str, Any]]:
    if not items:
        return []
    target_limit = max(1, min(limit, len(items)))
    enriched: list[dict[str, Any]] = []
    for item in items[:target_limit]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        detail = {}
        if url and allowed_external_url(url, DMM_HOSTS):
            try:
                detail = cached_detail_for_url(url) or dmm.get_av_detail(item)
                if detail:
                    store_av_detail(item, detail)
            except Exception as exc:
                app_log("warning", "maker", "DMM/FANZA 厂牌详情校验失败，保留列表摘要", {
                    "stage": "maker_dmm_detail_enrich_error",
                    "maker": maker_name,
                    "av_id": item.get("id", ""),
                    "url": url,
                    "error": str(exc),
                })
        merged = {**item, **detail} if detail else item
        if dmm_detail_matches_maker(merged, maker_name):
            enriched.append(merged)
    return prioritize_dmm_maker_labels(enriched, maker_name, limit)


def fetch_listing_sources(page_url: str, maker_name: str, safe_limit: int, *, force_refresh: bool) -> list[dict[str, Any]]:
    if maker_name:
        remember_maker_identity(maker_name)
    javdb_results: list[dict[str, Any]] = []
    dmm_results: list[dict[str, Any]] = []
    javlibrary_results: list[dict[str, Any]] = []
    dmm_limit = min(80, max(safe_limit * 3, safe_limit + 14))
    source_strategy = maker_listing_source_strategy(maker_name, page_url)

    def fetch_dmm() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if allowed_external_url(page_url, DMM_HOSTS):
            try:
                results.extend(dmm.get_listing_avs(page_url, limit=dmm_limit, force_refresh=force_refresh))
            except Exception as exc:
                app_log("warning", "maker", "DMM/FANZA 厂牌页面读取失败", {
                    "stage": "maker_dmm_page_listing_error",
                    "url": page_url,
                    "error": str(exc),
                })
        mapped_dmm_urls = dmm_listing_urls_for_maker(maker_name)
        for dmm_url in mapped_dmm_urls:
            try:
                results.extend(dmm.get_listing_avs(dmm_url, limit=dmm_limit, force_refresh=force_refresh))
            except Exception as exc:
                app_log("warning", "maker", "DMM/FANZA 厂牌映射读取失败", {
                    "stage": "maker_dmm_mapped_listing_error",
                    "maker": maker_name,
                    "url": dmm_url,
                    "error": str(exc),
                })
        if not mapped_dmm_urls or len(merge_av_sources(results)) < safe_limit:
            for term in dmm_search_terms_for_maker(maker_name):
                try:
                    results.extend(dmm.get_maker_avs(term, limit=dmm_limit, force_refresh=force_refresh))
                except Exception as exc:
                    app_log("warning", "maker", "DMM/FANZA 厂牌发售读取失败", {
                        "stage": "maker_dmm_listing_error",
                        "maker": maker_name,
                        "term": term,
                        "error": str(exc),
                    })
        if any("/mono/dvd/" in str(item.get("url") or "") for item in results):
            results = [item for item in results if "/mono/dvd/" in str(item.get("url") or "")]
        if maker_name:
            results = enrich_dmm_maker_items(results, maker_name, max(safe_limit * 2, safe_limit + 8))
        return results

    def fetch_javdb() -> list[dict[str, Any]]:
        if not javdb_source_enabled():
            app_log("info", "maker", "JavDB 数据源已关闭，跳过厂牌发售兜底", {
                "stage": "javdb_source_disabled",
                "context": "maker_listing",
                "url": page_url,
            })
            return []
        if not allowed_external_url(page_url, JAVDB_HOSTS):
            return []
        try:
            return javdb.get_listing(page_url, limit=safe_limit, force_refresh=force_refresh)
        except Exception as exc:
            app_log("warning", "maker", "JavDB 厂牌发售读取失败", {
                "stage": "maker_javdb_listing_error",
                "url": page_url,
                "error": str(exc),
            })
            return []

    if source_strategy == "javlibrary":
        javlibrary_results = javlibrary_maker_avs(maker_name, safe_limit)
        javlibrary_ids = {canonical_av_id(item.get("id")) for item in javlibrary_results if isinstance(item, dict)}
        if force_refresh or len(javlibrary_ids) < safe_limit:
            dmm_results = fetch_dmm()
            if len(javlibrary_ids) >= safe_limit:
                dmm_results = [item for item in dmm_results if canonical_av_id(item.get("id")) in javlibrary_ids]
            if len(merge_av_sources(javlibrary_results, dmm_results)) < safe_limit:
                javdb_results = fetch_javdb()
        else:
            app_log("info", "maker", "JavLibrary 厂牌列表已满足首屏，跳过同步 DMM 补全", {
                "stage": "maker_javlibrary_fast_path",
                "maker": maker_name,
                "count": len(javlibrary_ids),
                "limit": safe_limit,
            })
    elif source_strategy == "javdb":
        javdb_results = fetch_javdb()
        if len(merge_av_sources(javdb_results)) < safe_limit:
            javlibrary_results = javlibrary_maker_avs(maker_name, safe_limit)
        if len(merge_av_sources(javdb_results, javlibrary_results)) < safe_limit:
            dmm_results = fetch_dmm()
    else:
        dmm_results = fetch_dmm()
        if len(merge_av_sources(dmm_results)) < safe_limit or source_strategy == "auto":
            javlibrary_results = javlibrary_maker_avs(maker_name, safe_limit)
        if len(merge_av_sources(javdb_results, javlibrary_results, dmm_results)) < safe_limit:
            javdb_results = fetch_javdb()
    dmm_results = [
        explain_match(append_source_chain(item, "dmm"), default_reason="primary_label", default_confidence="high")
        for item in dmm_results
        if isinstance(item, dict)
    ]
    javlibrary_results = [
        explain_match(append_source_chain(item, "javlibrary"), default_reason="primary_label", default_confidence="high")
        for item in javlibrary_results
        if isinstance(item, dict)
    ]
    javdb_results = [
        explain_match(append_source_chain(item, "javdb"), default_reason="javdb_maker_fallback", default_confidence="low")
        for item in javdb_results
        if isinstance(item, dict)
    ]
    results = [
        public_metadata_item(explain_match(hydrate_av_with_cached_summary(item), default_reason="maker_listing", default_confidence="medium"))
        for item in merge_av_sources(javdb_results, javlibrary_results, dmm_results)
    ]
    results = filter_avs_by_actor_limit(results, context="maker_listing")
    results = sort_maker_listing_items(results, maker_name)
    app_log("info", "maker", "厂牌发售数据源合并完成", {
        "stage": "maker_listing_merged",
        "maker": maker_name,
        "source_strategy": source_strategy,
        "javdb_count": len(javdb_results),
        "javlibrary_count": len(javlibrary_results),
        "dmm_count": len(dmm_results),
        "merged_count": len(results),
    })
    return results


def cached_listing(page_url: str, limit: int = 60, *, force_refresh: bool = False, maker_name: str = "") -> list[dict[str, Any]]:
    url = str(page_url or "").strip()
    if not url:
        return []
    safe_limit = max(1, min(60, int(limit or 60)))
    resolved_maker_name = maker_name_for_listing_url(url, maker_name)
    source_strategy = maker_listing_source_strategy(resolved_maker_name, url)
    key = hashlib.sha256(f"v21|{url}|{resolved_maker_name}|{source_strategy}".encode("utf-8")).hexdigest()
    if force_refresh:
        results = fetch_listing_sources(url, resolved_maker_name, safe_limit, force_refresh=True)
        if results:
            cache_set("listing", key, [public_metadata_item(item) for item in results], METADATA_MAKER_LISTING_TTL)
            remember_av_summaries(results)
        return results[:safe_limit]
    cached = cache_get("listing", key)
    cached_count = len(cached) if isinstance(cached, list) else 0
    if isinstance(cached, list) and cached_count > 0:
        result = [
            public_metadata_item(explain_match({**item, "_cache_hit": True}, default_reason="sqlite_cache", default_confidence="high"))
            for item in cached
            if isinstance(item, dict)
        ]
        app_log("info", "metadata-cache", "厂牌发售命中 SQLite 缓存", {
            "stage": "listing_sqlite_cache_hit",
            "maker": resolved_maker_name,
            "count": len(cached),
            "limit": safe_limit,
            "partial": len(cached) < safe_limit,
        })
        return filter_avs_by_actor_limit(result, context="listing_cache")[:safe_limit]
    else:
        result = fetch_listing_sources(url, resolved_maker_name, safe_limit, force_refresh=False)
        if result:
            cache_set("listing", key, [public_metadata_item(item) for item in result], METADATA_MAKER_LISTING_TTL)
    items = filter_avs_by_actor_limit(result if isinstance(result, list) else [], context="listing_cache")
    remember_av_summaries([item for item in items if isinstance(item, dict)])
    return items[:safe_limit]


def normalize_dmm_ranking_kind(value: object) -> str:
    return "actress" if str(value or "").strip().lower() == "actress" else "movie"


def normalize_dmm_ranking_term(kind: str, value: object) -> str:
    if kind == "actress":
        return "monthly"
    term = str(value or "").strip().lower()
    aliases = {
        "day": "daily",
        "daily": "daily",
        "week": "week",
        "weekly": "week",
        "month": "monthly",
        "monthly": "monthly",
    }
    return aliases.get(term, "daily")


def public_dmm_ranking(payload: dict[str, Any], *, cached: bool = False) -> dict[str, Any]:
    kind = normalize_dmm_ranking_kind(payload.get("kind"))
    term = normalize_dmm_ranking_term(kind, payload.get("term"))
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    items: list[dict[str, Any]] = []
    if kind == "actress":
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            row = normalize_image_fields({key: value for key, value in item.items() if not str(key).startswith("_")})
            row["source"] = str(row.get("source") or "dmm")
            row["source_scope"] = str(row.get("source_scope") or "ranking")
            row = explain_match(row, default_reason="dmm_actress_ranking", default_confidence="medium")
            items.append(row)
    else:
        rows = [
            annotate_unavailable_cover(public_metadata_item(explain_match(append_source_chain(item, "dmm"), default_reason="dmm_ranking", default_confidence="medium")))
            for item in raw_items
            if isinstance(item, dict) and canonical_av_id(item.get("id")) and re.fullmatch(r"[A-Z]{2,10}-\d{2,5}", canonical_av_id(item.get("id")))
        ]
        items = filter_avs_by_actor_limit(rows, context="dmm_ranking")
    return {
        "kind": kind,
        "term": term,
        "source": "dmm",
        "cached": cached,
        "fetched_at": payload.get("fetched_at", 0),
        "items": items,
    }


def cached_dmm_ranking(kind: str = "movie", term: str = "daily", limit: int = 100, *, force_refresh: bool = False) -> dict[str, Any]:
    safe_kind = normalize_dmm_ranking_kind(kind)
    safe_term = normalize_dmm_ranking_term(safe_kind, term)
    safe_limit = max(1, min(100, int(limit or 100)))
    key = metadata_cache_key("v2", safe_kind, safe_term, safe_limit)
    if force_refresh:
        payload = dmm.get_ranking(safe_kind, safe_term, safe_limit, force_refresh=True)
        if payload.get("items"):
            cache_set("dmm_ranking", key, payload, METADATA_DMM_RANKING_TTL)
            if safe_kind == "movie":
                remember_av_summaries([item for item in payload.get("items", []) if isinstance(item, dict)])
        return public_dmm_ranking(payload, cached=False)
    cached = cache_get("dmm_ranking", key)
    if isinstance(cached, dict) and cached.get("items"):
        app_log("info", "metadata-cache", "DMM 榜单命中 SQLite 缓存", {
            "stage": "dmm_ranking_cache_hit",
            "kind": safe_kind,
            "term": safe_term,
            "limit": safe_limit,
        })
        return public_dmm_ranking(cached, cached=True)
    payload = cached_metadata(
        "dmm_ranking",
        key,
        METADATA_DMM_RANKING_TTL,
        lambda: dmm.get_ranking(safe_kind, safe_term, safe_limit, force_refresh=False),
    )
    if isinstance(payload, dict):
        if safe_kind == "movie":
            remember_av_summaries([item for item in payload.get("items", []) if isinstance(item, dict)])
        return public_dmm_ranking(payload, cached=False)
    return {"kind": safe_kind, "term": safe_term, "source": "dmm", "cached": False, "fetched_at": 0, "items": []}


def cached_detail_for_url(url: str) -> dict[str, Any]:
    target_url = str(url or "").strip()
    if not target_url:
        return {}
    cached = cached_av_detail(target_url)
    if cached and not detail_needs_refresh(cached, target_url):
        app_log("info", "metadata-cache", "番号详情命中 SQLite 缓存", {"stage": "av_detail_sqlite_cache_hit", "url": target_url})
        return cached
    if cached:
        app_log("info", "metadata-cache", "DMM 详情缓存缺少女优字段，刷新详情", {"stage": "dmm_detail_cache_refresh", "url": target_url})

    summary = {}
    for item in cache_get("subscription_search", metadata_cache_key("av", target_url), allow_stale=True) or []:
        if isinstance(item, dict) and str(item.get("url") or "").strip() == target_url:
            summary = item
            break

    def fetch() -> dict[str, Any]:
        if allowed_external_url(target_url, DMM_HOSTS):
            return dmm.get_av_detail(summary or target_url)
        if allowed_external_url(target_url, JAVDB_HOSTS) and not javdb_source_enabled():
            return {}
        return javdb.get_av_detail(target_url)

    detail = fetch()
    if isinstance(detail, dict) and detail:
        store_av_detail({"url": target_url, **summary}, detail)
        return detail
    stale = cache_get("av_detail", detail_cache_keys(target_url)[0], allow_stale=True) if detail_cache_keys(target_url) else None
    return stale if isinstance(stale, dict) else {}


def estimate_actor_count_from_title(title: object) -> int:
    text = str(title or "")
    counts: list[int] = []
    for match in re.finditer(r"(?<!\d)(\d{1,3})\s*(?:人|名)", text):
        try:
            counts.append(int(match.group(1)))
        except ValueError:
            continue
    multi_tokens = (
        "BEST",
        "ベスト",
        "総集編",
        "総集",
        "合集",
        "合辑",
        "オールスター",
        "ハーレム",
        "大乱交",
        "乱交",
        "複数話",
        "多人",
        "共演",
        "全員",
        "スペシャルコラボ",
        "コラボ",
        "集めました",
        "まとめ",
        "厳選",
        "COLLECTION",
        "コレクション",
        "タイトル",
        "連発",
    )
    if any(token.lower() in text.lower() for token in multi_tokens):
        counts.append(GLOBAL_MAX_COACTORS + 1)
    return max(counts) if counts else 0


def actor_count_for_limit(av: dict[str, Any], actors: list[dict[str, str]]) -> int:
    return max(len(actors), estimate_actor_count_from_title(av.get("title")))


def actor_count_from_summary(av: dict[str, Any]) -> int:
    actors = normalize_actor_items(av.get("actors") or av.get("actresses") or av.get("actress"))
    return actor_count_for_limit(av, actors)


def av_within_global_actor_limit(av: dict[str, Any], *, max_coactors: int | None = None) -> bool:
    safe_max = max(1, min(GLOBAL_MAX_COACTORS, int(max_coactors or configured_max_coactors())))
    return actor_count_from_summary(av) <= safe_max


def actor_limit_verification(av: dict[str, Any], *, max_coactors: int | None = None, context: str = "") -> dict[str, Any]:
    safe_max = max(1, min(GLOBAL_MAX_COACTORS, int(max_coactors or configured_max_coactors())))
    payload = prepare_subscription_av_payload(av)
    actors = resolve_av_actors_for_limit(payload)
    actor_count = actor_count_for_limit(payload, actors)
    if actors:
        payload["actresses"] = actors
    ok = actor_count <= safe_max
    result = {
        "ok": ok,
        "payload": payload,
        "actors": actors,
        "actor_count": actor_count,
        "max_coactors": safe_max,
        "reason": "" if ok else f"共演人数 {actor_count} 超过限制 {safe_max}",
    }
    if not ok:
        app_log("info", "subscription", "最终动作前跳过超过共演人数限制的番号", {
            "stage": "actor_limit_final_skip",
            "context": context,
            "av_id": payload.get("id", av.get("id", "")),
            "actor_count": actor_count,
            "max_coactors": safe_max,
            "title": payload.get("title", ""),
        })
    return result


def filter_avs_by_actor_limit(items: list[dict[str, Any]], *, context: str = "", max_coactors: int | None = None) -> list[dict[str, Any]]:
    safe_max = max(1, min(GLOBAL_MAX_COACTORS, int(max_coactors or configured_max_coactors())))
    result: list[dict[str, Any]] = []
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        actor_count = actor_count_from_summary(item)
        if actor_count > safe_max:
            skipped += 1
            continue
        result.append(item)
    if skipped:
        app_log("info", "subscription", "已按全局共演人数过滤番号", {
            "stage": "global_actor_limit_filter",
            "context": context,
            "max_coactors": safe_max,
            "kept": len(result),
            "skipped": skipped,
        })
    return result


def subscribed_avs_with_global_filter(context: str = "subscribed_av") -> list[dict[str, Any]]:
    items = filter_avs_by_actor_limit(get_subscription_service().get_subscribed_av(), context=context)
    return [
        public_metadata_item(explain_match(hydrate_av_with_cached_summary(item), default_reason="subscription_list", default_confidence="medium"))
        for item in items
        if isinstance(item, dict)
    ]


def dirty_subscription_candidates() -> list[dict[str, Any]]:
    dirty: list[dict[str, Any]] = []
    max_coactors = configured_max_coactors()
    for item in get_subscription_service().get_subscribed_av():
        if not isinstance(item, dict):
            continue
        actor_count = actor_count_from_summary(item)
        if actor_count > max_coactors:
            dirty.append({
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "date": item.get("date", ""),
                "actor_count": actor_count,
                "max_coactors": max_coactors,
                "reason": f"超过全局 {max_coactors} 人共演限制",
            })
    return dirty


def cleanup_dirty_subscriptions(*, dry_run: bool = True) -> dict[str, Any]:
    candidates = dirty_subscription_candidates()
    removed: list[dict[str, Any]] = []
    if not dry_run:
        service = get_subscription_service()
        for item in candidates:
            av_id = str(item.get("id") or "")
            if av_id and service.unsubscribe_av(av_id):
                removed.append(item)
        app_log("info", "subscription", "历史脏订阅清理完成", {
            "stage": "dirty_subscription_cleanup",
            "candidates": len(candidates),
            "removed": len(removed),
        })
    return {
        "dry_run": dry_run,
        "candidates": candidates,
        "removed": removed,
        "candidate_count": len(candidates),
        "removed_count": len(removed),
    }


def poll_subscriptions_once() -> dict[str, Any]:
    service = get_subscription_service()
    max_coactors = configured_max_coactors()
    actresses = [item for item in service.get_subscribed_actresses() if item.get("poll_enabled", True)]
    added: list[dict[str, Any]] = []
    errors: list[str] = []
    app_log("info", "subscription", "开始执行订阅轮询", {"actress_count": len(actresses), "max_coactors": max_coactors})
    for actress in actresses:
        actress_id = str(actress.get("id") or "")
        if not actress_id:
            continue
        new_count = 0
        try:
            avs = subscription_avs_for_actress(actress, limit=100)
            since_date = str(actress.get("since_date") or "")
            for av in avs:
                av_id = canonical_subscription_av_id(av)
                if not date_is_after(str(av.get("date") or ""), since_date):
                    continue
                if not av_id or service.is_av_subscribed(av_id):
                    continue
                av = {**av, "id": av_id}
                if not actress.get("include_vr", False) and is_vr_work(av):
                    app_log("info", "subscription", "跳过 VR 女优作品", {"av_id": av_id, "actress_id": actress_id})
                    continue
                actors = resolve_av_actors_for_limit(av)
                actor_count = actor_count_for_limit(av, actors)
                if actor_count > max_coactors:
                    app_log("info", "subscription", "跳过超过共演人数限制的番号", {"av_id": av_id, "actor_count": actor_count, "max_coactors": max_coactors})
                    continue
                payload = prepare_subscription_av_payload(av)
                verification = actor_limit_verification(payload, max_coactors=max_coactors, context="actress_poll")
                if not verification["ok"]:
                    app_log("info", "subscription", "详情补全后跳过超过共演人数限制的番号", {"av_id": av_id, "max_coactors": max_coactors, "actor_count": verification["actor_count"]})
                    continue
                payload = verification["payload"]
                actors = verification["actors"] or actors
                payload["auto_subscribed"] = True
                payload["source_actress_id"] = actress_id
                payload["source_actress_name"] = actress.get("name", "")
                payload["actresses"] = [actor.get("name", "") for actor in actors] or [actress.get("name", "")]
                apply_jellyfin_status(payload)
                saved = service.subscribe_av(payload)
                added.append(saved)
                app_log("info", "subscription", "自动订阅新增番号", {
                    "av_id": av_id,
                    "title": saved.get("title", ""),
                    "status": saved.get("status"),
                    "actress": actress.get("name", ""),
                    "cover": saved.get("cover") or saved.get("cover_url") or "",
                    "release_date": av.get("date", ""),
                })
                send_notification_event("av_subscribed", {
                    "status": saved.get("status") or "subscribed",
                    "title": str(saved.get("title") or av_id),
                    "detail": f"已订阅 {av_id}：{saved.get('title') or ''}".strip(),
                    "av_id": av_id,
                    "actress": actress.get("name", ""),
                    "release_date": av.get("date", ""),
                    "cover": saved.get("cover") or saved.get("cover_url") or "",
                })
                new_count += 1
            service.mark_actress_polled(actress_id, new_count)
        except Exception as exc:
            errors.append(f"{actress.get('name') or actress_id}: {exc}")
            app_log("error", "subscription", "女优轮询失败", {"actress_id": actress_id, "error": str(exc)})
            send_notification_event("task_failed", {
                "status": "failed",
                "title": "女优订阅轮询失败",
                "detail": f"{actress.get('name') or actress_id}: {exc}",
                "actress": actress.get("name", ""),
            })
    service.mark_global_poll()
    app_log("info", "subscription", "订阅轮询完成", {"checked": len(actresses), "added": len(added), "errors": len(errors)})
    return {"checked": len(actresses), "added": added, "errors": errors}


def subscribe_latest_for_actress(actress: dict[str, Any], *, future_only: bool = False, download: bool = True) -> dict[str, Any]:
    service = get_subscription_service()
    max_coactors = configured_max_coactors()
    actress_id = str(actress.get("id") or "")
    since_date = str(actress.get("since_date") or "")
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    if not actress_id:
        return {"added": [], "skipped": [], "errors": ["缺少女优 ID"]}
    app_log("info", "subscription", "开始一键订阅女优最新作品", {
        "stage": "actress_subscribe_latest_start",
        "actress_id": actress_id,
        "since_date": since_date,
        "future_only": future_only,
        "download": download,
        "include_vr": bool(actress.get("include_vr", False)),
    })
    try:
        avs = subscription_avs_for_actress(actress, limit=100, include_dmm_detail=True)
    except Exception as exc:
        app_log("error", "subscription", "读取女优作品失败", {"stage": "actress_subscribe_latest_error", "actress_id": actress_id, "error": str(exc)})
        return {"added": [], "skipped": [], "errors": [str(exc)]}
    app_log("info", "subscription", "女优作品读取完成", {
        "stage": "actress_subscribe_latest_avs_loaded",
        "actress_id": actress_id,
        "count": len(avs),
    })

    today = date.today().isoformat()
    for av in avs:
        av_id = canonical_subscription_av_id(av)
        release_date = str(av.get("date") or "")
        if not av_id:
            continue
        av = {**av, "id": av_id}
        if since_date and not date_is_after(release_date, since_date):
            skipped.append({"id": av_id, "reason": "早于限制日期"})
            continue
        if future_only and not date_is_after(release_date, today):
            skipped.append({"id": av_id, "reason": "不是未发售作品"})
            continue
        if not actress.get("include_vr", False) and is_vr_work(av):
            skipped.append({"id": av_id, "reason": "VR 作品未启用订阅"})
            app_log("info", "subscription", "跳过 VR 女优作品", {"stage": "actress_vr_skip", "actress_id": actress_id, "av_id": av_id})
            continue
        if service.is_av_subscribed(av_id):
            skipped.append({"id": av_id, "reason": "已订阅"})
            continue
        try:
            actors = resolve_av_actors_for_limit(av)
            actor_count = actor_count_for_limit(av, actors)
            if actor_count > max_coactors:
                skipped.append({"id": av_id, "reason": f"共演人数 {actor_count} 超过限制"})
                continue
            payload = prepare_subscription_av_payload(av)
            verification = actor_limit_verification(payload, max_coactors=max_coactors, context="actress_subscribe_latest")
            if not verification["ok"]:
                skipped.append({"id": av_id, "reason": verification["reason"] or f"详情补全后共演人数超过 {max_coactors}"})
                continue
            payload = verification["payload"]
            actors = verification["actors"] or actors
            payload["auto_subscribed"] = True
            payload["source_actress_id"] = actress_id
            payload["source_actress_name"] = actress.get("name", "")
            payload["actresses"] = actors or [{"id": actress_id, "name": actress.get("name", "")}]
            apply_jellyfin_status(payload)
            saved = service.subscribe_av(payload)
            added.append(saved)
            app_log("info", "subscription", "女优一键订阅新增番号", {
                "stage": "actress_subscribe_latest_added",
                "actress_id": actress_id,
                "actress": actress.get("name", ""),
                "av_id": av_id,
                "title": saved.get("title", ""),
                "cover": saved.get("cover") or saved.get("cover_url") or "",
                "release_date": release_date,
                "status": saved.get("status"),
            })
            send_notification_event("av_subscribed", {
                "status": saved.get("status") or "subscribed",
                "title": str(saved.get("title") or av_id),
                "detail": f"已订阅 {av_id}：{saved.get('title') or ''}".strip(),
                "av_id": av_id,
                "actress": actress.get("name", ""),
                "release_date": release_date,
                "cover": saved.get("cover") or saved.get("cover_url") or "",
            })
            if download and saved.get("status") != "in_library":
                download_av_from_mteam(saved)
        except Exception as exc:
            errors.append(f"{av_id}: {exc}")
            send_notification_event("task_failed", {
                "status": "failed",
                "title": "女优最新作品订阅失败",
                "detail": f"{av_id}: {exc}",
                "av_id": av_id,
                "actress": actress.get("name", ""),
            })
    service.mark_actress_polled(actress_id, len(added))
    app_log("info", "subscription", "一键订阅女优最新作品完成", {
        "stage": "actress_subscribe_latest_done",
        "actress_id": actress_id,
        "added": len(added),
        "skipped": len(skipped),
        "errors": len(errors),
    })
    return {"added": added, "skipped": skipped, "errors": errors}


def refresh_library_status_for_subscriptions(limit: int = 80) -> int:
    service = get_subscription_service()
    changed = 0
    items = [item for item in subscribed_avs_with_global_filter("library_refresh") if item.get("status") != "in_library"][:limit]
    for item in items:
        updated = refresh_subscription_library_status(item)
        if updated.get("status") == "in_library":
            changed += 1
    if changed:
        app_log("info", "jellyfin", "刷新订阅入库状态完成", {"stage": "jellyfin_refresh_done", "changed": changed})
    return changed


def download_pending_subscriptions() -> dict[str, Any]:
    service = get_subscription_service()
    items = [item for item in subscribed_avs_with_global_filter("bulk_download") if item.get("status", "pending") == "pending"]
    app_log("info", "download", "开始一键下载订阅中番号", {"stage": "bulk_download_start", "count": len(items)})
    results = [download_av_from_mteam(item) for item in items]
    sent = len([item for item in results if item.get("status") in {"ok", "exists", "sent"}])
    app_log("info", "download", "一键下载完成", {"stage": "bulk_download_done", "count": len(results), "sent": sent})
    return {"results": results, "checked": len(items), "sent": sent}


def download_pending_wash_subscriptions() -> dict[str, Any]:
    service = get_subscription_service()
    expired = expire_wash_requests_with_postprocess()
    items = [
        item for item in subscribed_avs_with_global_filter("wash_bulk_download")
        if isinstance(item.get("wash"), dict)
        and item.get("wash", {}).get("mode") in {"chinese", "4k"}
        and item.get("wash", {}).get("status") in {"requested", "error"}
    ]
    app_log("info", "wash", "开始洗版轮询", {
        "stage": "wash_bulk_start",
        "count": len(items),
        "expired": expired,
    })
    results = [download_wash_from_mteam(item, str(item.get("wash", {}).get("mode") or "")) for item in items]
    sent = len([item for item in results if item.get("status") in {"ok", "exists", "sent"}])
    not_found = len([item for item in results if item.get("status") == "not_found"])
    errors = len([item for item in results if item.get("status") == "error"])
    app_log("info", "wash", "洗版轮询完成", {
        "stage": "wash_bulk_done",
        "count": len(results),
        "sent": sent,
        "not_found": not_found,
        "errors": errors,
        "expired": expired,
    })
    return {
        "results": results,
        "checked": len(items),
        "sent": sent,
        "not_found": not_found,
        "errors": errors,
        "expired": expired,
    }


def run_subscription_background_job(name: str, worker: Callable[[], Any]) -> None:
    try:
        app_log("info", "subscription", "后台订阅任务开始", {"stage": "subscription_background_start", "job": name})
        worker()
        app_log("info", "subscription", "后台订阅任务完成", {"stage": "subscription_background_done", "job": name})
    except Exception as exc:
        app_log("error", "subscription", "后台订阅任务失败", {
            "stage": "subscription_background_error",
            "job": name,
            "error": str(exc),
        })


def background_download_subscription_av(av_id: str) -> None:
    service = get_subscription_service()
    av = next((item for item in service.get_subscribed_av() if item.get("id") == av_id), None)
    if not av:
        return
    run_subscription_background_job(f"download_av:{av_id}", lambda: download_av_from_mteam(av))


def cached_only_enriched_actress_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = normalize_image_fields(payload)
    if av_cover_used_as_actress_cover(result.get("cover")):
        result.setdefault("latest_cover", result.get("cover"))
        result["cover"] = ""
    actress_ref = str(result.get("id") or result.get("name") or "").strip()
    actress_name = str(result.get("name") or actress_ref).strip()
    identity = cached_actor_identity(actress_name or actress_ref)
    if identity:
        for field in ("javdb_id", "dmm_name", "dmm_url", "source", "source_chain", "match_reason", "confidence", "javlibrary_star_id"):
            if identity.get(field) and not result.get(field):
                result[field] = identity.get(field)
        if not result.get("cover") and identity.get("cover"):
            result["cover"] = normalize_cover_url(str(identity.get("cover") or ""))
        if not result.get("latest_cover") and identity.get("latest_cover"):
            result["latest_cover"] = normalize_cover_url(str(identity.get("latest_cover") or ""))
        for field in ("latest_av_id", "latest_title", "latest_date"):
            if identity.get(field) and not result.get(field):
                result[field] = identity.get(field)
        if bad_actress_name(result.get("name")) and identity.get("name"):
            result["name"] = identity.get("name")
    if not result.get("dmm_name") and not bad_actress_name(result.get("name")):
        result["dmm_name"] = result.get("name")
    return explain_match(normalize_image_fields(result), default_reason="actor_identity_cache", default_confidence="medium")


def hydrate_actress_subscriptions_cached(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    service = get_subscription_service()
    hydrated: list[dict[str, Any]] = []
    for item in items:
        current = normalize_image_fields(dict(item or {}))
        needs_name = bad_actress_name(current.get("name"))
        bad_cover = av_cover_used_as_actress_cover(current.get("cover"))
        needs_cover = not current.get("cover") or bad_cover
        if needs_name or needs_cover or not current.get("javlibrary_star_id"):
            cached = cached_only_enriched_actress_payload(current)
            patch: dict[str, Any] = {}
            for field in ("name", "cover", "latest_cover", "latest_av_id", "latest_title", "latest_date", "source", "source_chain", "match_reason", "confidence", "javdb_id", "dmm_name", "dmm_url", "javlibrary_star_id"):
                value = cached.get(field)
                if value and not current.get(field):
                    patch[field] = value
            if bad_cover:
                patch["cover"] = cached.get("cover") or ""
            if patch:
                updated = service.update_actress_subscription(str(current.get("id") or ""), patch)
                current = updated or {**current, **patch}
        hydrated.append(normalize_image_fields(current))
    return hydrated


def background_enrich_and_subscribe_latest_actress(actress_id: str, *, future_only: bool = True, download: bool = True) -> None:
    def worker() -> None:
        service = get_subscription_service()
        actress = next((item for item in service.get_subscribed_actresses() if item.get("id") == actress_id), None)
        if not actress:
            return
        enriched = enriched_actress_payload(dict(actress))
        if enriched:
            updated = service.subscribe_actress({**actress, **enriched})
            remember_actor_identity(updated, match_reason=str(updated.get("match_reason") or "subscribed_actor"), confidence=str(updated.get("confidence") or "medium"))
            subscribe_latest_for_actress(updated, future_only=future_only, download=download)
        else:
            subscribe_latest_for_actress(actress, future_only=future_only, download=download)

    run_subscription_background_job(f"actress_latest:{actress_id}", worker)


def refresh_pinned_makers() -> dict[str, Any]:
    makers = get_subscription_service().get_settings().get("pinned_makers") or []
    refreshed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    prewarm_limit = max(14, min(60, int(MAKER_REFRESH_PREWARM_LIMIT or 60)))
    cover_limit = max(0, min(prewarm_limit, int(MAKER_REFRESH_COVER_PREWARM_LIMIT or 0)))
    app_log("info", "maker", "开始刷新常驻厂牌", {
        "stage": "maker_refresh_start",
        "count": len(makers),
        "prewarm_limit": prewarm_limit,
        "cover_limit": cover_limit,
    })
    for maker in makers:
        name = str(maker.get("name") or "")
        url = str(maker.get("url") or "")
        try:
            started_at = time.time()
            results = cached_listing(url, limit=prewarm_limit, force_refresh=True, maker_name=name)
            cover_cached = 0
            cover_errors = 0
            for item in results[:cover_limit]:
                av_id = canonical_av_id(item.get("id")) if isinstance(item, dict) else ""
                if not av_id:
                    continue
                try:
                    before = get_subscription_service().get_asset_cache(av_id, "cover")
                    prewarm_cover_asset(item)
                    after = get_subscription_service().get_asset_cache(av_id, "cover")
                    if after:
                        cover_cached += 1
                    elif before:
                        cover_cached += 1
                except Exception as exc:
                    cover_errors += 1
                    app_log("warning", "maker", "厂牌封面预热失败", {
                        "stage": "maker_refresh_cover_error",
                        "name": name,
                        "av_id": av_id,
                        "error": str(exc),
                    })
            cache_probe = cached_listing(url, limit=min(prewarm_limit, 15), force_refresh=False, maker_name=name)
            elapsed = round(time.time() - started_at, 2)
            item_result = {
                "name": name,
                "url": url,
                "source_strategy": maker_listing_source_strategy(name, url),
                "requested": prewarm_limit,
                "cached_listing": len(results),
                "first_screen_cache_ok": len(cache_probe) >= min(14, prewarm_limit),
                "cover_checked": min(len(results), cover_limit),
                "cover_cached": cover_cached,
                "cover_errors": cover_errors,
                "elapsed": elapsed,
            }
            refreshed.append(item_result)
            app_log("info", "maker", "厂牌刷新完成", {"stage": "maker_refresh_item_done", **item_result})
        except Exception as exc:
            errors.append({"name": name, "error": str(exc)})
            app_log("error", "maker", "厂牌刷新失败", {"stage": "maker_refresh_item_error", "name": name, "error": str(exc)})
    result = {
        "refreshed": refreshed,
        "errors": errors,
        "maker_count": len(makers),
        "refreshed_count": len(refreshed),
        "error_count": len(errors),
        "prewarm_limit": prewarm_limit,
        "cover_limit": cover_limit,
        "metadata_cache": get_subscription_service().metadata_cache_stats(),
        "asset_cache": get_subscription_service().asset_cache_stats(),
    }
    app_log("info", "maker", "常驻厂牌刷新完成", {"stage": "maker_refresh_done", **result})
    return result


def freeze_released_asset_cache() -> dict[str, Any]:
    service = get_subscription_service()
    checked = 0
    frozen = 0
    for row in service.list_metadata_cache("av_summary", limit=50000):
        item = row.get("data") if isinstance(row.get("data"), dict) else {}
        if not item:
            continue
        av_id = canonical_av_id(item.get("id"))
        if not av_id or not metadata_item_released(item):
            continue
        checked += 1
        asset = service.get_asset_cache(av_id, "cover")
        if asset and not asset.get("immutable") and service.set_asset_immutable(av_id, "cover", True):
            frozen += 1
    app_log("info", "asset-cache", "已发售封面资产冻结完成", {
        "stage": "asset_freeze_released",
        "checked": checked,
        "frozen": frozen,
    })
    return {"checked": checked, "frozen": frozen}


def maintain_asset_cache(max_bytes: int | None = None) -> dict[str, Any]:
    service = get_subscription_service()
    if max_bytes is None:
        try:
            max_mb = int(float(service.get_settings().get("asset_cache_max_mb") or 2048))
            max_bytes = max(0, max_mb * 1024 * 1024)
        except (TypeError, ValueError):
            max_bytes = ASSET_CACHE_MAX_BYTES
    freeze_result = freeze_released_asset_cache()
    cleanup_result = service.cleanup_asset_cache(max_bytes)
    stats = service.asset_cache_stats()
    result = {
        "freeze": freeze_result,
        "cleanup": cleanup_result,
        "asset_cache": stats,
        "max_bytes": max_bytes,
    }
    app_log("info", "asset-cache", "资产缓存维护完成", {"stage": "asset_maintenance_done", **result})
    return result


def refresh_dmm_rankings() -> dict[str, Any]:
    targets = (
        ("movie", "week", "作品周榜"),
        ("movie", "monthly", "作品月榜"),
        ("actress", "monthly", "女优榜"),
    )
    refreshed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    app_log("info", "ranking", "开始刷新 DMM/FANZA 榜单", {
        "stage": "ranking_refresh_start",
        "count": len(targets),
        "limit": 100,
    })
    for kind, term, label in targets:
        started_at = time.time()
        try:
            ranking = cached_dmm_ranking(kind, term, 100, force_refresh=True)
            items = ranking.get("items") if isinstance(ranking.get("items"), list) else []
            missing_covers = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                cover_url = str(item.get("cover") or item.get("image") or item.get("latest_cover") or "").strip()
                if not cover_url or dmm_cover_url_is_placeholder(cover_url):
                    missing_covers += 1
            item_result = {
                "label": label,
                "kind": kind,
                "term": term,
                "count": len(items),
                "missing_covers": missing_covers,
                "elapsed": round(time.time() - started_at, 2),
            }
            refreshed.append(item_result)
            app_log("info", "ranking", "DMM/FANZA 榜单刷新完成", {"stage": "ranking_refresh_item_done", **item_result})
        except Exception as exc:
            errors.append({"label": label, "kind": kind, "term": term, "error": str(exc)})
            app_log("error", "ranking", "DMM/FANZA 榜单刷新失败", {
                "stage": "ranking_refresh_item_error",
                "label": label,
                "kind": kind,
                "term": term,
                "error": str(exc),
            })
    result = {
        "refreshed": refreshed,
        "errors": errors,
        "refreshed_count": len(refreshed),
        "error_count": len(errors),
        "metadata_cache": get_subscription_service().metadata_cache_stats(),
    }
    app_log("info", "ranking", "DMM/FANZA 榜单刷新结束", {"stage": "ranking_refresh_done", **result})
    return result


def subscription_tasks_payload() -> list[dict[str, Any]]:
    sub_settings = get_subscription_service().get_settings()
    last_results = sub_settings.get("last_task_results") if isinstance(sub_settings.get("last_task_results"), dict) else {}
    return [
        {
            "id": "actress_poll",
            "name": "女优订阅轮询",
            "cron": sub_settings.get("actress_cron") or "0 21 * * *",
            "last_run_at": sub_settings.get("last_poll_at") or 0,
            "last_result": last_results.get("actress_poll") or {},
            "description": "检查已订阅女优在限制日期之后的新番号。",
        },
        {
            "id": "av_download",
            "name": "番号订阅下载",
            "cron": sub_settings.get("av_cron") or "0 22 * * *",
            "last_run_at": sub_settings.get("last_av_poll_at") or 0,
            "last_result": last_results.get("av_download") or {},
            "description": "为订阅中的番号检查 Jellyfin、搜索 MTeam 并推送 qBittorrent。",
        },
        {
            "id": "wash_download",
            "name": "洗版轮询",
            "cron": sub_settings.get("wash_cron") or "0 22 * * *",
            "last_run_at": sub_settings.get("last_wash_poll_at") or 0,
            "last_result": last_results.get("wash_download") or {},
            "description": "为等待中的洗版番号搜索中文或 4K 资源并推送 qBittorrent。",
        },
        {
            "id": "postprocess_qb",
            "name": "后处理下载检查",
            "cron": sub_settings.get("postprocess_cron") or "*/5 * * * *",
            "last_run_at": sub_settings.get("last_postprocess_poll_at") or 0,
            "last_result": last_results.get("postprocess_qb") or {},
            "description": "轮询系统绑定的 qB 种子，确认下载完成并进入转码/字幕队列。",
        },
        {
            "id": "maker_refresh",
            "name": "厂牌发售更新",
            "cron": sub_settings.get("maker_cron") or "0 */6 * * *",
            "last_run_at": sub_settings.get("last_maker_poll_at") or 0,
            "last_result": last_results.get("maker_refresh") or {},
            "description": "刷新订阅设置中常驻厂牌的最近发售缓存。",
        },
        {
            "id": "ranking_refresh",
            "name": "DMM/FANZA 榜单更新",
            "cron": sub_settings.get("ranking_cron") or "30 4 */2 * *",
            "last_run_at": sub_settings.get("last_ranking_poll_at") or 0,
            "last_result": last_results.get("ranking_refresh") or {},
            "description": "每 2 天低频刷新作品周榜、作品月榜和女优榜，只缓存榜单信息和封面 URL。",
        },
        {
            "id": "asset_maintenance",
            "name": "资产缓存维护",
            "cron": sub_settings.get("asset_cron") or "15 3 * * *",
            "last_run_at": sub_settings.get("last_asset_poll_at") or 0,
            "last_result": last_results.get("asset_maintenance") or {},
            "description": "冻结已发售番号封面，清理缺失记录，并按容量回收非冻结资产。",
        },
    ]


AUTOMATION_NOTIFICATION_EVENTS: dict[str, str] = {
    "actress_poll": "automation_actress_poll",
    "av_download": "automation_av_download",
    "wash_download": "automation_wash_download",
}


def automation_result_status(result: dict[str, Any]) -> str:
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return "completed_with_errors"
    try:
        if int(errors or 0) > 0:
            return "completed_with_errors"
    except (TypeError, ValueError):
        pass
    return "completed"


def automation_task_notification_detail(task_id: str, result: dict[str, Any]) -> str:
    checked = int(result.get("checked") or 0)
    run_time = local_time_text("%H:%M:%S")
    if task_id == "actress_poll":
        added = len(result.get("added") or [])
        errors = result.get("errors") or []
        error_count = len(errors) if isinstance(errors, list) else int(errors or 0)
        return f"检查女优：{checked} 个\n新增番号：{added} 个\n错误：{error_count} 个\n执行时间：{run_time}"
    if task_id == "av_download":
        results = result.get("results") if isinstance(result.get("results"), list) else []
        not_found = sum(1 for item in results if isinstance(item, dict) and item.get("status") == "not_found")
        errors = sum(1 for item in results if isinstance(item, dict) and item.get("status") in {"error", "failed", "conflict"})
        return f"检查订阅番号：{checked} 个\n推送下载：{int(result.get('sent') or 0)} 个\n未找到资源：{not_found} 个\n错误：{errors} 个\n执行时间：{run_time}"
    if task_id == "wash_download":
        return (
            f"检查洗版番号：{checked} 个\n"
            f"推送下载：{int(result.get('sent') or 0)} 个\n"
            f"未匹配：{int(result.get('not_found') or 0)} 个\n"
            f"过期：{int(result.get('expired') or 0)} 个\n"
            f"错误：{int(result.get('errors') or 0)} 个\n"
            f"执行时间：{run_time}"
        )
    return f"任务已完成\n执行时间：{run_time}"


def notify_automation_task_completed(task_id: str, result: dict[str, Any]) -> None:
    event_key = AUTOMATION_NOTIFICATION_EVENTS.get(task_id)
    if not event_key:
        return
    label = task_label(task_id)
    send_notification_event(event_key, {
        "status": automation_result_status(result),
        "title": f"{label}完成",
        "detail": automation_task_notification_detail(task_id, result),
        "task": task_id,
        "task_name": label,
        "checked": result.get("checked", 0),
        "sent": result.get("sent", 0),
        "added": len(result.get("added") or []),
        "errors": len(result.get("errors") or []) if isinstance(result.get("errors"), list) else int(result.get("errors") or 0),
    })


def run_subscription_task(task_id: str, *, minute_key: str | None = None) -> dict[str, Any]:
    service = get_subscription_service()
    app_log("info", "task", "开始执行定时任务", {"stage": "task_start", "task_id": task_id})
    try:
        if task_id == "actress_poll":
            result = poll_subscriptions_once()
        elif task_id == "av_download":
            result = download_pending_subscriptions()
        elif task_id == "wash_download":
            result = download_pending_wash_subscriptions()
        elif task_id == "postprocess_qb":
            result = poll_postprocess_once()
        elif task_id == "maker_refresh":
            result = refresh_pinned_makers()
        elif task_id == "ranking_refresh":
            result = refresh_dmm_rankings()
        elif task_id == "asset_maintenance":
            result = maintain_asset_cache()
        else:
            raise HTTPException(status_code=404, detail="未知订阅任务")
    except Exception as exc:
        failure_result = {
            "status": "failed",
            "task_id": task_id,
            "error": str(exc),
            "failed_at": "task",
        }
        service.mark_task_poll(task_id, minute_key, failure_result, status="failed")
        send_notification_event("task_failed", {
            "status": "failed",
            "title": "定时任务执行失败",
            "detail": f"{task_id}: {exc}",
            "task_id": task_id,
        })
        raise
    service.mark_task_poll(task_id, minute_key, result)
    notify_automation_task_completed(task_id, result)
    app_log("info", "task", "定时任务执行完成", {"stage": "task_done", "task_id": task_id})
    return result


def subscription_poll_loop() -> None:
    while not subscription_poll_stop.is_set():
        try:
            service = get_subscription_service()
            sub_settings = service.get_settings()
            if sub_settings.get("poll_enabled", True):
                now = local_now()
                minute_key = now.strftime("%Y-%m-%d %H:%M")
                schedules = (
                    ("actress_poll", "actress_cron", "last_poll_minute"),
                    ("av_download", "av_cron", "last_av_poll_minute"),
                    ("wash_download", "wash_cron", "last_wash_poll_minute"),
                    ("postprocess_qb", "postprocess_cron", "last_postprocess_poll_minute"),
                    ("maker_refresh", "maker_cron", "last_maker_poll_minute"),
                    ("ranking_refresh", "ranking_cron", "last_ranking_poll_minute"),
                    ("asset_maintenance", "asset_cron", "last_asset_poll_minute"),
                )
                for task_id, cron_key, last_minute_key in schedules:
                    if sub_settings.get(last_minute_key) != minute_key and cron_matches(str(sub_settings.get(cron_key) or ""), now):
                        run_subscription_task(task_id, minute_key=minute_key)
        except Exception as exc:
            print(f"[SubscriptionPoll] error: {exc}", flush=True)
        subscription_poll_stop.wait(30)


def cron_matches(expression: str, moment: datetime) -> bool:
    parts = expression.split()
    if len(parts) != 5:
        return False
    minute, hour, day, month, weekday = parts
    return (
        cron_part_matches(minute, moment.minute)
        and cron_part_matches(hour, moment.hour)
        and cron_part_matches(day, moment.day)
        and cron_part_matches(month, moment.month)
        and cron_part_matches(weekday, moment.weekday())
    )


def cron_part_matches(part: str, value: int) -> bool:
    if part == "*":
        return True
    if "," in part:
        return any(cron_part_matches(item.strip(), value) for item in part.split(","))
    if part.startswith("*/"):
        try:
            step = int(part[2:])
            return step > 0 and value % step == 0
        except ValueError:
            return False
    try:
        return int(part) == value
    except ValueError:
        return False


def apply_jellyfin_status(av: dict[str, Any]) -> None:
    jellyfin = get_system_settings_service().get().get("jellyfin", {})
    if not jellyfin.get("dedupe_enabled", True):
        app_log("info", "jellyfin", "跳过 Jellyfin 查重：未启用", {"stage": "jellyfin_skip", "av_id": av.get("id", "")})
        return
    app_log("info", "jellyfin", "开始 Jellyfin 查重", {
        "stage": "jellyfin_start",
        "av_id": av.get("id", ""),
        "library": jellyfin.get("library_name") or jellyfin.get("library_id") or "全部媒体库",
    })
    match = find_jellyfin_match(str(av.get("id") or ""), str(av.get("title") or ""), jellyfin)
    if not match:
        app_log("info", "jellyfin", "Jellyfin 未入库", {"stage": "jellyfin_miss", "av_id": av.get("id", "")})
        return
    av["status"] = "in_library"
    av["library_status"] = "in_library"
    av["jellyfin_item_id"] = match.get("id", "")
    av["jellyfin_item_name"] = match.get("name", "")
    av["jellyfin_path"] = match.get("path", "")
    app_log("info", "jellyfin", "Jellyfin 查重命中，标记已入库", {"av_id": av.get("id", ""), "item": match.get("name", ""), "path": match.get("path", "")})


def refresh_subscription_library_status(av: dict[str, Any]) -> dict[str, Any]:
    probe = dict(av)
    apply_jellyfin_status(probe)
    if probe.get("status") == "in_library":
        saved = get_subscription_service().update_av_download(str(probe.get("id") or ""), {
            "status": "in_library",
            "library_status": "in_library",
            "jellyfin_item_id": probe.get("jellyfin_item_id", ""),
            "jellyfin_item_name": probe.get("jellyfin_item_name", ""),
            "jellyfin_path": probe.get("jellyfin_path", ""),
        })
        if str(av.get("status") or "") != "in_library":
            send_notification_event("jellyfin_in_library", {
                "status": "in_library",
                "title": str(probe.get("title") or probe.get("id") or ""),
                "detail": f"{probe.get('id', '')} 已在 Jellyfin 媒体库中",
                "av_id": probe.get("id", ""),
                "path": probe.get("jellyfin_path", ""),
                "file_name": notification_filename(probe.get("jellyfin_path"), str(probe.get("id") or "")),
                "save_path": notification_parent_path(probe.get("jellyfin_path"), ""),
                "cover": probe.get("cover") or probe.get("cover_url") or "",
            })
        return saved or probe
    return av


def wash_task_type(mode: str) -> str:
    return "wash_4k" if mode == "4k" else "wash_chinese"


def ensure_wash_postprocess_task(av: dict[str, Any], mode: str) -> dict[str, Any]:
    service = get_subscription_service()
    post = get_postprocess_service()
    av_id = str(av.get("id") or "")
    wash = av.get("wash") if isinstance(av.get("wash"), dict) else {}
    task_id = str(wash.get("task_id") or "")
    existing = post.get_task(task_id) if task_id else None
    if existing and existing.get("status") not in {"completed", "expired", "ignored"}:
        return existing
    active = post.active_version(av_id)
    task = post.create_task(
        av_id=av_id,
        task_type=wash_task_type(mode),
        status="created",
        supersede_version_id=str(active.get("id") or "") if active else "",
        supersede_path=str(active.get("path") or "") if active else "",
        target_codec=str(post.get_settings().get("target_codec") or "av1"),
        needs_subtitle=bool(post.get_settings().get("auto_subtitle_enabled")),
        data={"wash_mode": mode, "title": av.get("title", "")},
    )
    service.update_av_wash(av_id, {
        "mode": mode,
        "status": str(wash.get("status") or "requested"),
        "task_id": task["id"],
    })
    app_log("info", "postprocess", "洗版后处理任务已创建", {
        "stage": "postprocess_wash_task_created",
        "task_id": task["id"],
        "av_id": av_id,
        "mode": mode,
        "supersede_version_id": task.get("supersede_version_id", ""),
        "supersede_path": task.get("supersede_path", ""),
    })
    return task


@app.get("/duplicates", response_class=HTMLResponse)
def duplicates_page(legacy: int = 0) -> Response:
    return frontend_app_response()


@app.get("/scan-api", response_class=HTMLResponse)
def scan_api_page() -> Response:
    return frontend_app_response()


def create_subscription_postprocess_task(av: dict[str, Any]) -> dict[str, Any]:
    post = get_postprocess_service()
    task = post.create_task(
        av_id=str(av.get("id") or ""),
        task_type="subscription",
        status="mteam_searching",
        target_codec=str(post.get_settings().get("target_codec") or "av1"),
        needs_subtitle=bool(post.get_settings().get("auto_subtitle_enabled")),
        data={"title": av.get("title", "")},
    )
    app_log("info", "postprocess", "普通订阅后处理任务已创建", {
        "stage": "postprocess_subscription_task_created",
        "task_id": task["id"],
        "av_id": av.get("id", ""),
    })
    return task


def bind_qb_to_postprocess_task(task: dict[str, Any], qb_result: dict[str, Any], qb_config: dict[str, Any]) -> dict[str, Any]:
    torrent_hash = str(qb_result.get("hash") or "")
    if not torrent_hash or not task:
        return {"status": "skipped", "reason": "missing_hash_or_task"}
    post = get_postprocess_service()
    task_id = str(task.get("id") or "")
    existing_qb = post.get_qb_torrent(torrent_hash)
    if existing_qb and str(existing_qb.get("task_id") or "") != task_id:
        post.update_task(
            task_id,
            status="conflict",
            error_code="torrent_hash_conflict",
            error_message="qB torrent_hash 已绑定到其他后处理任务",
        )
        post.add_event(task_id, "error", "torrent_hash_conflict", "qB torrent_hash 已绑定到其他后处理任务，拒绝重绑", {
            "torrent_hash": torrent_hash,
            "existing_task_id": existing_qb.get("task_id", ""),
            "existing_av_id": existing_qb.get("av_id", ""),
            "current_task_id": task_id,
            "current_av_id": task.get("av_id", ""),
        })
        return {"status": "conflict", "reason": "torrent_hash_conflict", "existing": existing_qb}
    post.bind_qb_torrent(
        task_id=task_id,
        av_id=str(task.get("av_id") or ""),
        torrent_hash=torrent_hash,
        category=str(qb_config.get("category") or ""),
        tags=str(qb_config.get("tags") or ""),
        save_path=str(qb_config.get("save_path") or ""),
        status="torrent_pushed" if qb_result.get("status") in {"ok", "exists", "sent"} else "failed",
        data={
            "qb_message": qb_result.get("message", ""),
            "qb_status": qb_result.get("status", ""),
            "category_result": qb_result.get("category_result", {}),
            "label_result": qb_result.get("label_result", {}),
        },
    )
    post.add_event(str(task.get("id") or ""), "info", "qb_bound", "qB 种子已绑定到后处理任务", {
        "av_id": task.get("av_id", ""),
        "torrent_hash": torrent_hash,
        "qb_status": qb_result.get("status", ""),
        "qb_message": qb_result.get("message", ""),
        "category": qb_config.get("category", ""),
        "tags": qb_config.get("tags", ""),
        "save_path": qb_config.get("save_path", ""),
        "category_result": qb_result.get("category_result", {}),
        "label_result": qb_result.get("label_result", {}),
    })
    return {"status": "bound", "torrent_hash": torrent_hash}


def merge_qb_tags(config_tags: Any, required_tags: Any) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    raw_parts: list[Any] = []
    if isinstance(config_tags, list):
        raw_parts.extend(config_tags)
    else:
        raw_parts.extend(str(config_tags or "").replace("\n", ",").split(","))
    if isinstance(required_tags, list):
        raw_parts.extend(required_tags)
    else:
        raw_parts.extend(str(required_tags or "").replace("\n", ",").split(","))
    for item in raw_parts:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            merged.append(text)
    return ",".join(merged)


def postprocess_qb_config(qb_config: dict[str, Any]) -> dict[str, Any]:
    merged = dict(qb_config or {})
    post_settings = get_postprocess_service().get_settings()
    if not str(merged.get("save_path") or "").strip():
        merged["save_path"] = str(post_settings.get("download_dir") or "")
    if not str(merged.get("category") or "").strip():
        categories = post_settings.get("allowed_categories") or []
        if categories:
            merged["category"] = str(categories[0])
    merged["tags"] = merge_qb_tags(merged.get("tags"), post_settings.get("required_tags"))
    return merged


def download_av_from_mteam(av: dict[str, Any], *, save_to_subscription: bool = True) -> dict[str, Any]:
    av_id = str(av.get("id") or "").strip()
    result: dict[str, Any] = {"av_id": av_id, "status": "skipped", "message": ""}
    if not av_id:
        result["message"] = "缺少番号"
        return result
    if save_to_subscription:
        av = refresh_subscription_library_status(av)
    if str(av.get("status") or "") == "in_library":
        result.update({"status": "skipped", "message": "Jellyfin 已入库，跳过下载"})
        app_log("info", "download", "跳过下载：Jellyfin 已入库", {"stage": "download_skip_library", "av_id": av_id})
        return result
    verification = actor_limit_verification(av, context="mteam_download")
    if not verification["ok"]:
        message = str(verification.get("reason") or f"超过全局 {GLOBAL_MAX_COACTORS} 人共演限制，跳过下载")
        result.update({"status": "skipped", "message": message})
        if save_to_subscription:
            get_subscription_service().update_av_download(av_id, {"download_status": "skipped", "download_message": message})
        app_log("info", "download", "跳过下载：超过共演人数限制", {
            "stage": "download_skip_actor_limit",
            "av_id": av_id,
            "actor_count": verification.get("actor_count"),
            "max_coactors": verification.get("max_coactors"),
        })
        return result
    av = verification["payload"]

    settings_data = get_system_settings_service().get()
    service = get_subscription_service()
    post_task: dict[str, Any] | None = None
    app_log("info", "mteam", "开始搜索 MTeam 资源", {"stage": "mteam_search_start", "av_id": av_id})
    mteam_result = search_mteam(av_id, settings_data, limit=8)
    torrents_all = mteam_result.get("results") or []
    filters = av.get("filters") if isinstance(av.get("filters"), dict) else {}
    subscription_mode = str(av.get("subscription_mode") or "strict")
    torrents = filter_mteam_results(torrents_all, filters)
    if not torrents and torrents_all and subscription_mode == "predownload":
        app_log("info", "mteam", "MTeam 过滤无匹配，预下载模式尝试使用原始结果", {
            "stage": "mteam_filter_fallback",
            "av_id": av_id,
            "total": len(torrents_all),
        })
        torrents = torrents_all
    if not torrents:
        message = str(mteam_result.get("message") or "MTeam 没有匹配资源")
        if torrents_all:
            message = "MTeam 有资源，但不符合当前订阅过滤条件"
        app_log("error", "mteam", "MTeam 未找到资源", {"stage": "mteam_search_empty", "av_id": av_id, "message": message, "raw_count": len(torrents_all)})
        if save_to_subscription:
            service.update_av_download(av_id, {"download_status": "not_found", "download_message": message})
        return {"av_id": av_id, "status": "not_found", "message": message}

    torrent = choose_mteam_torrent(av_id, torrents)
    torrent_id = str(torrent.get("id") or "")
    torrent_title = str(torrent.get("title") or "")
    send_notification_event("mteam_found", {
        "status": "found",
        "title": str(av.get("title") or av_id),
        "detail": f"{av_id} 命中 MTeam：{torrent_title}",
        "av_id": av_id,
        "torrent_id": torrent_id,
        "torrent_title": torrent_title,
    })
    if save_to_subscription:
        post_task = create_subscription_postprocess_task(av)
        get_postprocess_service().update_task(
            post_task["id"],
            data={
                "mteam_keyword": av_id,
                "mteam_raw_count": len(torrents_all),
                "mteam_filtered_count": len(torrents),
                "selected_torrent_id": torrent_id,
                "selected_torrent_title": torrent_title,
            },
        )
        get_postprocess_service().add_event(post_task["id"], "info", "mteam_filter_done", "普通订阅 MTeam 搜索和过滤完成", {
            "av_id": av_id,
            "mteam_keyword": av_id,
            "mteam_raw_count": len(torrents_all),
            "mteam_filtered_count": len(torrents),
            "filters": filters,
            "selected_torrent_id": torrent_id,
            "selected_torrent_title": torrent_title,
            "candidates": summarize_mteam_candidates(torrents_all),
            "matched": summarize_mteam_candidates(torrents),
            "filter_audit": mteam_filter_audit(torrents_all, filters),
        })
    if not torrent_id:
        message = "MTeam 结果缺少种子 ID"
        app_log("error", "mteam", message, {"stage": "mteam_missing_id", "av_id": av_id, "title": torrent_title})
        if save_to_subscription:
            service.update_av_download(av_id, {"download_status": "error", "download_message": message})
        if post_task:
            get_postprocess_service().update_task(
                post_task["id"],
                status="failed",
                error_code="mteam_missing_id",
                error_message=message,
            )
        send_notification_event("task_failed", {
            "status": "failed",
            "title": f"MTeam 资源异常：{av_id}",
            "detail": message,
            "av_id": av_id,
            "torrent_title": torrent_title,
        })
        return {"av_id": av_id, "status": "error", "message": message}

    app_log("info", "mteam", "开始下载 MTeam 种子文件", {"stage": "mteam_torrent_download_start", "av_id": av_id, "torrent_id": torrent_id, "title": torrent_title})
    try:
        torrent_bytes, filename = download_mteam_torrent(torrent_id, settings_data)
        app_log("info", "qbittorrent", "开始推送种子到 qBittorrent", {"stage": "qb_add_start", "av_id": av_id, "torrent_id": torrent_id, "filename": filename})
        qb_config = postprocess_qb_config(settings_data.get("qbittorrent", {})) if post_task else settings_data.get("qbittorrent", {})
        qb_result = add_torrent_to_qbittorrent(torrent_bytes, filename, qb_config)
        bind_result = {"status": "skipped"}
        if post_task:
            bind_result = bind_qb_to_postprocess_task(post_task, qb_result, qb_config)
        qb_status = str(qb_result.get("status") or "ok")
        if bind_result.get("status") == "conflict":
            message = "qB torrent_hash 已绑定到其他后处理任务"
            if save_to_subscription:
                service.update_av_download(av_id, {
                    "download_status": "error",
                    "download_message": message,
                    "mteam_torrent_id": torrent_id,
                    "mteam_torrent_title": torrent_title,
                    "qb_hash": qb_result.get("hash", ""),
                    "downloaded_at": time.time(),
                })
            app_log("error", "download", "下载链路绑定 qB hash 冲突", {
                "stage": "download_qb_hash_conflict",
                "av_id": av_id,
                "torrent_id": torrent_id,
                "hash": qb_result.get("hash", ""),
            })
            send_notification_event("task_failed", {
                "status": "conflict",
                "title": f"下载链路冲突：{av_id}",
                "detail": message,
                "av_id": av_id,
                "torrent_id": torrent_id,
                "torrent_title": torrent_title,
            })
            return {"av_id": av_id, "status": "conflict", "message": message, "torrent": torrent}
        qb_accepted = qb_status in ("ok", "exists", "sent")
        payload = {
            "status": str(av.get("status") or "pending"),
            "download_status": "downloading" if qb_accepted else qb_status,
            "download_message": qb_result.get("message", "已发送到 qBittorrent"),
            "mteam_torrent_id": torrent_id,
            "mteam_torrent_title": torrent_title,
            "qb_hash": qb_result.get("hash", ""),
            "downloaded_at": time.time(),
        }
        if save_to_subscription:
            service.update_av_download(av_id, payload)
        app_log("info", "download", "下载链路完成", {
            "stage": "download_done",
            "av_id": av_id,
            "title": str(av.get("title") or av_id),
            "torrent_id": torrent_id,
            "torrent_title": torrent_title,
            "status": qb_status,
            "message": payload["download_message"],
            "site": "馒头",
            "size": format_wechat_mteam_size(torrent.get("size")),
            "seeders": torrent.get("seeders", ""),
            "downloader": "qbittorrent",
            "cover": av.get("cover") or av.get("cover_url") or "",
        })
        if qb_accepted:
            send_notification_event("torrent_sent", {
                "status": qb_status,
                "title": str(av.get("title") or av_id),
                "detail": f"{av_id} 已推送到 qBittorrent：{payload['download_message']}",
                "av_id": av_id,
                "torrent_id": torrent_id,
                "torrent_title": torrent_title,
                "site": "馒头",
                "size": format_wechat_mteam_size(torrent.get("size")),
                "seeders": torrent.get("seeders", ""),
                "downloader": "qbittorrent",
                "qb_hash": qb_result.get("hash", ""),
                "cover": av.get("cover") or av.get("cover_url") or "",
            })
        else:
            send_notification_event("task_failed", {
                "status": qb_status,
                "title": f"qBittorrent 推送失败：{av_id}",
                "detail": str(payload["download_message"]),
                "av_id": av_id,
                "torrent_id": torrent_id,
                "torrent_title": torrent_title,
            })
        return {"av_id": av_id, "status": qb_status, "message": payload["download_message"], "torrent": torrent}
    except Exception as exc:
        message = str(exc)
        if save_to_subscription:
            service.update_av_download(av_id, {"download_status": "error", "download_message": message, "mteam_torrent_id": torrent_id, "mteam_torrent_title": torrent_title})
        if post_task:
            get_postprocess_service().update_task(
                post_task["id"],
                status="failed",
                error_code="download_push_failed",
                error_message=message,
            )
        app_log("error", "download", "下载链路失败", {"stage": "download_error", "av_id": av_id, "torrent_id": torrent_id, "error": message})
        send_notification_event("task_failed", {
            "status": "failed",
            "title": f"下载链路失败：{av_id}",
            "detail": message,
            "av_id": av_id,
            "torrent_id": torrent_id,
            "torrent_title": torrent_title,
        })
        return {"av_id": av_id, "status": "error", "message": message, "torrent": torrent}


def download_wash_from_mteam(av: dict[str, Any], mode: str) -> dict[str, Any]:
    av_id = str(av.get("id") or "").strip()
    service = get_subscription_service()
    if not av_id:
        return {"av_id": av_id, "status": "error", "message": "缺少番号"}
    settings_data = get_system_settings_service().get()
    wash_settings = service.get_settings().get("wash", {})
    post_task = ensure_wash_postprocess_task(av, mode)
    get_postprocess_service().update_task(
        post_task["id"],
        status="mteam_searching",
        data={"mteam_keyword": av_id, "wash_mode": mode},
    )
    filters = wash_filters_for_mode(mode, wash_settings)
    app_log("info", "wash", "开始洗版搜索", {
        "stage": "wash_search_start",
        "av_id": av_id,
        "mode": mode,
        "filters": filters,
    })
    mteam_result = search_mteam(av_id, settings_data, limit=20)
    torrents_all = mteam_result.get("results") or []
    app_log("info", "wash", "MTeam 洗版搜索返回", {
        "stage": "wash_search_result",
        "av_id": av_id,
        "mode": mode,
        "raw_count": len(torrents_all),
        "candidates": summarize_mteam_candidates(torrents_all),
        "message": mteam_result.get("message", ""),
    })
    torrents = filter_mteam_results(torrents_all, filters)
    app_log("info", "wash", "洗版过滤完成", {
        "stage": "wash_filter_done",
        "av_id": av_id,
        "mode": mode,
        "matched_count": len(torrents),
        "matched": summarize_mteam_candidates(torrents),
        "filter_audit": mteam_filter_audit(torrents_all, filters),
    })
    get_postprocess_service().add_event(post_task["id"], "info", "wash_filter_done", "洗版 MTeam 搜索和过滤完成", {
        "av_id": av_id,
        "mode": mode,
        "mteam_keyword": av_id,
        "mteam_raw_count": len(torrents_all),
        "mteam_filtered_count": len(torrents),
        "filters": filters,
        "matched": summarize_mteam_candidates(torrents),
        "filter_audit": mteam_filter_audit(torrents_all, filters),
    })
    if not torrents:
        message = str(mteam_result.get("message") or "MTeam 没有匹配洗版资源")
        if torrents_all:
            message = "MTeam 有资源，但不符合洗版条件"
        service.update_av_wash(av_id, {
            "mode": mode,
            "status": "requested",
            "download_status": "waiting",
            "download_message": message,
            "last_checked_at": time.time(),
            "task_id": post_task["id"],
        })
        get_postprocess_service().update_task(
            post_task["id"],
            status="mteam_not_found",
            error_code="mteam_not_found",
            error_message=message,
            data={
                "mteam_raw_count": len(torrents_all),
                "mteam_filtered_count": 0,
                "filter_audit": mteam_filter_audit(torrents_all, filters),
            },
        )
        app_log("info", "wash", "洗版本轮未匹配，等待下次轮询", {
            "stage": "wash_wait_next_poll",
            "av_id": av_id,
            "mode": mode,
            "message": message,
            "raw_count": len(torrents_all),
        })
        return {"av_id": av_id, "status": "not_found", "message": message}

    torrent = choose_mteam_torrent(av_id, torrents)
    torrent_id = str(torrent.get("id") or "")
    torrent_title = str(torrent.get("title") or "")
    get_postprocess_service().update_task(
        post_task["id"],
        data={
            "mteam_raw_count": len(torrents_all),
            "mteam_filtered_count": len(torrents),
            "selected_torrent_id": torrent_id,
            "selected_torrent_title": torrent_title,
        },
    )
    if not torrent_id:
        message = "MTeam 结果缺少种子 ID"
        service.update_av_wash(av_id, {
            "mode": mode,
            "status": "error",
            "download_status": "error",
            "download_message": message,
            "mteam_torrent_title": torrent_title,
            "task_id": post_task["id"],
        })
        get_postprocess_service().update_task(
            post_task["id"],
            status="failed",
            error_code="mteam_missing_id",
            error_message=message,
        )
        app_log("error", "wash", message, {"stage": "wash_missing_id", "av_id": av_id, "mode": mode, "title": torrent_title})
        return {"av_id": av_id, "status": "error", "message": message, "torrent": torrent}

    app_log("info", "wash", "开始下载洗版种子", {
        "stage": "wash_torrent_download_start",
        "av_id": av_id,
        "mode": mode,
        "torrent_id": torrent_id,
        "title": torrent_title,
    })
    try:
        torrent_bytes, filename = download_mteam_torrent(torrent_id, settings_data)
        app_log("info", "wash", "开始推送洗版种子到 qBittorrent", {
            "stage": "wash_qb_add_start",
            "av_id": av_id,
            "mode": mode,
            "torrent_id": torrent_id,
            "filename": filename,
        })
        qb_config = postprocess_qb_config(settings_data.get("qbittorrent", {}))
        qb_result = add_torrent_to_qbittorrent(torrent_bytes, filename, qb_config)
        bind_result = {"status": "skipped"}
        if post_task:
            bind_result = bind_qb_to_postprocess_task(post_task, qb_result, qb_config)
        qb_status = str(qb_result.get("status") or "ok")
        if bind_result.get("status") == "conflict":
            message = "qB torrent_hash 已绑定到其他后处理任务"
            saved = service.update_av_wash(av_id, {
                "mode": mode,
                "status": "error",
                "download_status": "error",
                "download_message": message,
                "mteam_torrent_id": torrent_id,
                "mteam_torrent_title": torrent_title,
                "qb_hash": qb_result.get("hash", ""),
                "task_id": post_task["id"],
            })
            app_log("error", "wash", "洗版绑定 qB hash 冲突", {
                "stage": "wash_qb_hash_conflict",
                "av_id": av_id,
                "mode": mode,
                "torrent_id": torrent_id,
                "hash": qb_result.get("hash", ""),
            })
            return {"av_id": av_id, "status": "conflict", "message": message, "torrent": torrent, "subscription": saved}
        next_status = "downloading" if qb_status in {"ok", "exists", "sent"} else "error"
        saved = service.update_av_wash(av_id, {
            "mode": mode,
            "status": next_status,
            "download_status": qb_status,
            "download_message": qb_result.get("message", "已发送到 qBittorrent"),
            "mteam_torrent_id": torrent_id,
            "mteam_torrent_title": torrent_title,
            "qb_hash": qb_result.get("hash", ""),
            "task_id": post_task["id"],
        })
        app_log("info", "wash", "洗版种子推送完成", {
            "stage": "wash_qb_add_done",
            "av_id": av_id,
            "mode": mode,
            "torrent_id": torrent_id,
            "status": qb_status,
            "message": qb_result.get("message", ""),
            "hash": qb_result.get("hash", ""),
        })
        return {"av_id": av_id, "status": qb_status, "message": qb_result.get("message", ""), "torrent": torrent, "subscription": saved}
    except Exception as exc:
        message = str(exc)
        service.update_av_wash(av_id, {
            "mode": mode,
            "status": "error",
            "download_status": "error",
            "download_message": message,
            "mteam_torrent_id": torrent_id,
            "mteam_torrent_title": torrent_title,
            "task_id": post_task["id"],
        })
        get_postprocess_service().update_task(
            post_task["id"],
            status="failed",
            error_code="qb_push_failed",
            error_message=message,
        )
        app_log("error", "wash", "洗版下载链路失败", {
            "stage": "wash_download_error",
            "av_id": av_id,
            "mode": mode,
            "torrent_id": torrent_id,
            "error": message,
        })
        return {"av_id": av_id, "status": "error", "message": message, "torrent": torrent}


def complete_wash_if_jellyfin_ready(av: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ignored",
        "message": "旧 Jellyfin 库洗版替换流程已停用，洗版完成必须通过后处理版本链激活",
    }


def wash_filters_for_mode(mode: str, wash_settings: dict[str, Any]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if mode == "chinese":
        filters["only_chinese"] = True
    if mode == "4k":
        filters["only_uhd"] = True
    try:
        max_size_gb = int(wash_settings.get("max_size_gb") or 0)
    except (TypeError, ValueError):
        max_size_gb = 0
    if max_size_gb:
        filters["max_size_mb"] = max_size_gb * 1024
    return filters


def summarize_mteam_candidates(torrents: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for item in torrents[:limit]:
        summary.append({
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "labels": item.get("labels", []),
            "smallDescr": item.get("smallDescr", ""),
            "size": item.get("size", ""),
            "seeders": item.get("seeders", ""),
            "standard": item.get("standard", ""),
            "videoCodec": item.get("videoCodec", ""),
        })
    return summary


def complete_wash_replacement(av_id: str, mode: str, *, new_item: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "status": "ignored",
        "message": "旧 Jellyfin 库洗版替换流程已停用，洗版替换必须绑定 media_versions 的 active version",
    }


def choose_mteam_torrent(av_id: str, torrents: list[dict[str, Any]]) -> dict[str, Any]:
    av_lower = av_id.lower()
    for item in torrents:
        if av_lower in str(item.get("title") or "").lower():
            return item
    return torrents[0]


def filter_mteam_results(torrents: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    if not filters:
        return torrents
    return [item for item in torrents if not mteam_filter_reasons(item, filters)]


def mteam_item_matches_filters(item: dict[str, Any], filters: dict[str, Any]) -> bool:
    return not mteam_filter_reasons(item, filters)


def mteam_filter_reasons(item: dict[str, Any], filters: dict[str, Any]) -> list[dict[str, Any]]:
    if not filters:
        return []
    reasons: list[dict[str, Any]] = []
    haystack = mteam_item_text(item)
    if filters.get("only_chinese") and not contains_any(haystack, ("中字", "中文", "字幕", "chinese", "chs", "cht", "sub")):
        reasons.append({"code": "missing_chinese", "message": "未命中中字/中文/字幕关键词"})
    if filters.get("only_uncensored") and not contains_any(haystack, ("无码", "無碼", "uncensored")):
        reasons.append({"code": "missing_uncensored", "message": "未命中无码关键词"})
    if filters.get("exclude_uncensored") and contains_any(haystack, ("无码", "無碼", "uncensored")):
        reasons.append({"code": "excluded_uncensored", "message": "命中排除的无码关键词"})
    if filters.get("only_free") and not contains_any(haystack, ("免费", "免費", "free", "freeleech")):
        reasons.append({"code": "missing_free", "message": "未命中免费/FreeLeech 关键词"})
    if filters.get("only_uhd") and not contains_any(haystack, ("uhd", "4k", "2160", "2160p")):
        reasons.append({"code": "missing_uhd", "message": "未命中 UHD/4K/2160p 关键词"})
    if filters.get("exclude_uhd") and contains_any(haystack, ("uhd", "4k", "2160", "2160p")):
        reasons.append({"code": "excluded_uhd", "message": "命中排除的 UHD/4K/2160p 关键词"})
    size_mb = mteam_size_mb(item.get("size"))
    min_size = int(filters.get("min_size_mb") or 0)
    max_size = int(filters.get("max_size_mb") or 0)
    if min_size and (not size_mb or size_mb < min_size):
        reasons.append({"code": "size_too_small", "message": f"体积小于 {min_size} MB", "size_mb": size_mb, "min_size_mb": min_size})
    if max_size and size_mb and size_mb > max_size:
        reasons.append({"code": "size_too_large", "message": f"体积大于 {max_size} MB", "size_mb": size_mb, "max_size_mb": max_size})
    return reasons


def mteam_filter_audit(torrents: list[dict[str, Any]], filters: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    for item in torrents[:limit]:
        reasons = mteam_filter_reasons(item, filters)
        row = summarize_mteam_candidates([item], limit=1)[0]
        row["matched"] = not reasons
        row["reasons"] = reasons
        audit.append(row)
    return audit


def mteam_item_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("title", ""),
        item.get("smallDescr", ""),
        item.get("category", ""),
        item.get("discount", ""),
        item.get("standard", ""),
        item.get("medium", ""),
        item.get("videoCodec", ""),
        item.get("source", ""),
    ]
    labels = item.get("labels") or []
    if isinstance(labels, list):
        parts.extend(str(label) for label in labels)
    return " ".join(str(part) for part in parts if part).lower()


def contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.lower() in text for needle in needles)


def mteam_size_mb(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1024 / 1024 if number > 1024 * 1024 else number
    raw = str(value).strip().lower()
    try:
        number = float(raw)
        return number / 1024 / 1024 if number > 1024 * 1024 else number
    except ValueError:
        pass
    compact = raw.replace(" ", "")
    for unit, factor in (("tb", 1024 * 1024), ("gb", 1024), ("mb", 1), ("kb", 1 / 1024)):
        if compact.endswith(unit):
            try:
                return float(compact[:-len(unit)]) * factor
            except ValueError:
                return 0.0
    return 0.0


def add_torrent_to_qbittorrent(torrent_bytes: bytes, filename: str, config: dict[str, Any]) -> dict[str, object]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("未配置 qBittorrent Web UI 地址")
    info_hash = torrent_info_hash(torrent_bytes)
    data: dict[str, str] = {}
    if config.get("save_path"):
        data["savepath"] = str(config.get("save_path") or "")
    if config.get("category"):
        data["category"] = str(config.get("category") or "")
    if config.get("tags"):
        data["tags"] = str(config.get("tags") or "")

    def operation(client: httpx.Client, _auth_method: str) -> dict[str, object]:
        category_result = ensure_qb_category(client, base_url, config)
        if info_hash:
            existing = client.get(f"{base_url}/api/v2/torrents/info", params={"hashes": info_hash})
            existing.raise_for_status()
            items = existing.json()
            if items:
                name = items[0].get("name") or filename
                label_result = ensure_qb_torrent_labels(client, base_url, info_hash, config)
                return {
                    "status": "exists",
                    "message": f"qBittorrent 已存在: {name}",
                    "hash": info_hash,
                    "category_result": category_result,
                    "label_result": label_result,
                }
        resp = client.post(
            f"{base_url}/api/v2/torrents/add",
            data=data,
            files={"torrents": (filename or "mteam.torrent", torrent_bytes, "application/x-bittorrent")},
        )
        resp.raise_for_status()
        text = resp.text.strip()
        if text and text.lower() not in ("ok.", "ok"):
            if info_hash:
                existing = client.get(f"{base_url}/api/v2/torrents/info", params={"hashes": info_hash})
                existing.raise_for_status()
                items = existing.json()
                if items:
                    name = items[0].get("name") or filename
                    label_result = ensure_qb_torrent_labels(client, base_url, info_hash, config)
                    return {
                        "status": "exists",
                        "message": f"qBittorrent 已存在: {name}",
                        "hash": info_hash,
                        "category_result": category_result,
                        "label_result": label_result,
                    }
            raise RuntimeError(f"qBittorrent 添加失败: {text}")
        label_result = ensure_qb_torrent_labels(client, base_url, info_hash, config) if info_hash else {"status": "skipped"}
        return {"status": "ok", "message": "已发送到 qBittorrent", "hash": info_hash, "category_result": category_result, "label_result": label_result}

    return with_qbittorrent_client(base_url, config, operation, timeout=30)


def ensure_qb_category(client: httpx.Client, base_url: str, config: dict[str, Any]) -> dict[str, Any]:
    category = str(config.get("category") or "").strip()
    if not category:
        return {"status": "skipped", "reason": "no_category"}
    save_path = str(config.get("save_path") or "").strip()
    try:
        response = client.get(f"{base_url}/api/v2/torrents/categories")
        response.raise_for_status()
        categories = response.json()
        if isinstance(categories, dict) and category in categories:
            return {"status": "exists", "category": category}
    except Exception as exc:
        app_log("info", "qbittorrent", "读取 qBittorrent 分类列表失败，继续尝试创建分类", {
            "stage": "qb_category_list_failed",
            "category": category,
            "error": str(exc),
        })
    response = client.post(
        f"{base_url}/api/v2/torrents/createCategory",
        data={"category": category, "savePath": save_path},
    )
    if response.status_code in {400, 409}:
        text = response.text.strip()
        if "exist" in text.lower() or "already" in text.lower() or "exists" in text.lower():
            return {"status": "exists", "category": category, "message": text}
    response.raise_for_status()
    return {"status": "created", "category": category, "save_path": save_path}


def ensure_qb_torrent_labels(client: httpx.Client, base_url: str, torrent_hash: str, config: dict[str, Any]) -> dict[str, Any]:
    if not torrent_hash:
        return {"status": "skipped", "reason": "missing_hash"}
    result: dict[str, Any] = {"status": "ok"}
    category = str(config.get("category") or "").strip()
    tags = str(config.get("tags") or "").strip()
    if category:
        response = client.post(
            f"{base_url}/api/v2/torrents/setCategory",
            data={"hashes": torrent_hash, "category": category},
        )
        response.raise_for_status()
        result["category"] = category
    if tags:
        response = client.post(
            f"{base_url}/api/v2/torrents/addTags",
            data={"hashes": torrent_hash, "tags": tags},
        )
        response.raise_for_status()
        result["tags"] = tags
    if "category" not in result and "tags" not in result:
        result["status"] = "skipped"
        result["reason"] = "no_category_or_tags"
    return result


QB_DONE_STATES = {"completed", "uploading", "stalledUP", "pausedUP", "forcedUP", "queuedUP"}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts"}


def qb_tags_text(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(part).strip() for part in value if str(part).strip())
    return str(value or "").strip()


def qb_tag_set(value: Any) -> set[str]:
    return {item.strip().lower() for item in qb_tags_text(value).split(",") if item.strip()}


def qb_torrent_hash(item: dict[str, Any]) -> str:
    for key in ("hash", "infohash_v1", "infohash_v2"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def infer_external_qb_av_id(item: dict[str, Any], files: list[dict[str, Any]] | None = None) -> str:
    candidates = [
        item.get("name", ""),
        item.get("content_path", ""),
        item.get("save_path", ""),
    ]
    for file_item in files or []:
        candidates.append(file_item.get("name", ""))
    for candidate in candidates:
        detected = detect_catalog_number(str(candidate or ""))
        if detected:
            return canonical_av_id(detected)
    return ""


def external_qb_adoption_match(item: dict[str, Any], post_settings: dict[str, Any]) -> dict[str, Any]:
    category = str(item.get("category") or "").strip()
    tags = qb_tag_set(item.get("tags", ""))
    allowed_categories = {str(item).strip().lower() for item in post_settings.get("allowed_categories") or [] if str(item).strip()}
    required_tags = {str(item).strip().lower() for item in post_settings.get("required_tags") or [] if str(item).strip()}
    if not allowed_categories and not required_tags:
        return {"ok": False, "reason": "未配置 qB 接管分类或标签，拒绝扫描外部种子"}
    if allowed_categories and category.lower() not in allowed_categories:
        return {"ok": False, "reason": f"qB 分类 {category} 不在接管范围", "category": category}
    missing_tags = sorted(required_tags - tags)
    if missing_tags:
        return {"ok": False, "reason": f"qB 标签缺失: {', '.join(missing_tags)}", "tags": sorted(tags)}
    return {"ok": True, "category": category, "tags": sorted(tags)}


def adopt_external_qb_torrents(qb_config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(qb_config.get("url") or "").strip().rstrip("/")
    if not base_url:
        return {"status": "skipped", "reason": "未配置 qBittorrent Web UI 地址", "adopted": 0}
    post = get_postprocess_service()
    post_settings = post.get_settings()
    if not bool(post_settings.get("external_qb_adopt_enabled")):
        return {"status": "disabled", "adopted": 0}

    def operation(client: httpx.Client, _auth_method: str) -> dict[str, Any]:
        response = client.get(f"{base_url}/api/v2/torrents/info")
        response.raise_for_status()
        items = response.json()
        if not isinstance(items, list):
            items = []
        adopted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            torrent_hash = qb_torrent_hash(raw_item)
            name = str(raw_item.get("name") or "")
            if not torrent_hash:
                skipped.append({"name": name, "reason": "missing_hash"})
                continue
            if post.get_qb_torrent(torrent_hash):
                skipped.append({"torrent_hash": torrent_hash, "name": name, "reason": "already_bound"})
                continue
            match = external_qb_adoption_match(raw_item, post_settings)
            if not match.get("ok"):
                skipped.append({"torrent_hash": torrent_hash, "name": name, "reason": match.get("reason", "")})
                continue
            files: list[dict[str, Any]] = []
            av_id = infer_external_qb_av_id(raw_item)
            if not av_id:
                files = qb_torrent_files(client, base_url, torrent_hash)
                av_id = infer_external_qb_av_id(raw_item, files)
            if not av_id:
                skipped.append({"torrent_hash": torrent_hash, "name": name, "reason": "missing_av_id"})
                continue
            category = str(raw_item.get("category") or "")
            tags = qb_tags_text(raw_item.get("tags", ""))
            save_path = str(raw_item.get("save_path") or "")
            content_path = str(raw_item.get("content_path") or save_path)
            progress = float(raw_item.get("progress") or 0)
            state = str(raw_item.get("state") or "")
            size = int(raw_item.get("size") or raw_item.get("total_size") or 0)
            task = post.create_task(
                av_id=av_id,
                task_type="external_qb",
                status="torrent_pushed",
                target_codec=str(post_settings.get("target_codec") or "av1"),
                needs_subtitle=bool(post_settings.get("auto_subtitle_enabled")),
                data={
                    "source": "external_qb",
                    "qb_name": name,
                    "external_qb_hash": torrent_hash,
                    "source_cleanup": "keep",
                    "files_preview": files[:20],
                },
            )
            post.bind_qb_torrent(
                task_id=task["id"],
                av_id=av_id,
                torrent_hash=torrent_hash,
                category=category,
                tags=tags,
                save_path=save_path,
                status="torrent_pushed",
                data={
                    "source": "external_qb",
                    "qb_name": name,
                    "content_path": content_path,
                    "progress": progress,
                    "state": state,
                    "size": size,
                },
            )
            post.update_qb_torrent(
                torrent_hash,
                category=category,
                tags=tags,
                save_path=save_path,
                content_path=content_path,
                progress=progress,
                state=state,
                data={"source": "external_qb", "qb_name": name},
            )
            post.add_event(task["id"], "info", "external_qb_adopted", "外部 qB 种子已接管到后处理队列", {
                "torrent_hash": torrent_hash,
                "av_id": av_id,
                "category": category,
                "tags": tags,
                "save_path": save_path,
                "content_path": content_path,
                "source_cleanup": "keep",
            })
            adopted.append({"task_id": task["id"], "av_id": av_id, "torrent_hash": torrent_hash, "name": name})
        return {"status": "ok", "checked": len(items), "adopted": len(adopted), "items": adopted, "skipped": skipped[:20]}

    return with_qbittorrent_client(base_url, qb_config, operation, timeout=30)


def poll_qb_postprocess_once() -> dict[str, Any]:
    post = get_postprocess_service()
    settings_data = get_system_settings_service().get()
    qb_config = settings_data.get("qbittorrent", {})
    adoption: dict[str, Any] = {"status": "disabled", "adopted": 0}
    if bool(post.get_settings().get("external_qb_adopt_enabled")):
        try:
            adoption = adopt_external_qb_torrents(qb_config)
        except Exception as exc:
            adoption = {"status": "error", "message": str(exc), "adopted": 0}
            app_log("error", "qbittorrent", "外部 qB 种子接管扫描失败", {
                "stage": "external_qb_adopt_failed",
                "error": str(exc),
            })
    pending = post.list_qb_torrents(statuses=["torrent_pushed", "downloading"], limit=200)
    results: list[dict[str, Any]] = []
    for row in pending:
        task = post.get_task(str(row.get("task_id") or ""))
        if not task:
            continue
        try:
            result = refresh_qb_torrent_status(row, task, qb_config)
            results.append(result)
        except Exception as exc:
            post.update_task(task["id"], status="failed", error_code="qb_poll_failed", error_message=str(exc))
            post.add_event(task["id"], "error", "qb_poll_failed", "qB 下载状态轮询失败", {"error": str(exc)})
            results.append({"torrent_hash": row.get("torrent_hash", ""), "status": "error", "message": str(exc)})
    return {"checked": len(pending), "adoption": adoption, "results": results}


def refresh_qb_torrent_status(row: dict[str, Any], task: dict[str, Any], qb_config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(qb_config.get("url") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("未配置 qBittorrent Web UI 地址")
    torrent_hash = str(row.get("torrent_hash") or "")
    post = get_postprocess_service()

    def operation(client: httpx.Client, _auth_method: str) -> dict[str, Any]:
        resp = client.get(f"{base_url}/api/v2/torrents/info", params={"hashes": torrent_hash})
        resp.raise_for_status()
        items = resp.json()
        if not items:
            post.update_qb_torrent(torrent_hash, status="missing")
            post.update_task(task["id"], status="failed", error_code="qb_torrent_missing", error_message="qB 中找不到系统绑定的种子")
            return {"torrent_hash": torrent_hash, "status": "missing"}
        item = items[0]
        progress = float(item.get("progress") or 0)
        state = str(item.get("state") or "")
        content_path = str(item.get("content_path") or item.get("save_path") or "")
        category = str(item.get("category") or row.get("category") or "")
        tags_value = item.get("tags", row.get("tags", ""))
        tags = ",".join(str(part).strip() for part in tags_value if str(part).strip()) if isinstance(tags_value, list) else str(tags_value or "")
        save_path = str(item.get("save_path") or row.get("save_path") or "")
        size = int(item.get("size") or item.get("total_size") or 0)
        complete = progress >= 1.0 and state in QB_DONE_STATES and bool(content_path)
        previous_size = int(row.get("size") or 0)
        stable = complete and size > 0 and previous_size == size
        post.update_qb_torrent(
            torrent_hash,
            category=category,
            tags=tags,
            save_path=save_path,
            content_path=content_path,
            progress=progress,
            state=state,
            size=size,
            status="downloaded" if stable else "downloading",
            completed_at=time.time() if stable else 0,
            data={"qb_name": item.get("name", ""), "save_path": save_path},
        )
        if not stable:
            post.update_task(task["id"], status="downloading", data={"qb_progress": progress, "qb_state": state, "content_path": content_path})
            return {"torrent_hash": torrent_hash, "status": "downloading", "progress": progress, "state": state}

        files = qb_torrent_files(client, base_url, torrent_hash)
        picked = pick_main_video_file(files, str(task.get("av_id") or ""), content_path)
        if not picked:
            post.update_qb_torrent(torrent_hash, status="file_pick_failed", data={"files": files[:20]})
            post.update_task(task["id"], status="failed", error_code="file_pick_failed", error_message="下载完成但未能选择主视频文件")
            post.add_event(task["id"], "error", "file_pick_failed", "下载完成但未能选择主视频文件", {"files": files[:20]})
            return {"torrent_hash": torrent_hash, "status": "file_pick_failed"}
        file_ready = local_postprocess_file_ready(str(picked.get("path") or ""), int(picked.get("size") or 0))
        if not file_ready.get("ok"):
            post.update_qb_torrent(torrent_hash, status="downloading")
            post.update_task(task["id"], status="downloading", data={"picked_video": picked, "file_ready": file_ready})
            post.add_event(task["id"], "info", "download_file_waiting", "qB 已完成，但控制端下载文件尚未就绪", {
                "torrent_hash": torrent_hash,
                "picked_video": picked,
                "file_ready": file_ready,
            })
            return {"torrent_hash": torrent_hash, "status": "download_file_waiting", "file_ready": file_ready}
        effective_row = dict(row)
        effective_row.update({"category": category, "tags": tags, "save_path": save_path, "content_path": content_path})
        protection = qb_protection_check(effective_row, qb_config, content_path, str(picked.get("path") or ""))
        if not protection["ok"]:
            post.update_qb_torrent(torrent_hash, status="ignored", data={"protection": protection, "picked_video": picked})
            post.update_task(task["id"], status="ignored", error_code="protected_check_failed", error_message=protection["reason"], input_path=picked["path"])
            post.add_event(task["id"], "error", "protected_check_failed", "qB 保护规则未通过", protection)
            return {"torrent_hash": torrent_hash, "status": "ignored", "reason": protection["reason"]}
        post_settings = post.get_settings()
        needs_worker = bool(post_settings.get("auto_transcode_enabled") or post_settings.get("auto_subtitle_enabled"))
        next_status = "ready_to_run"
        if needs_worker:
            next_status = "waiting_worker" if worker_is_offline() else "ready_to_run"
        post.update_task(task["id"], status=next_status, input_path=picked["path"], data={"picked_video": picked, "file_ready": file_ready, "protection": protection})
        post.add_event(task["id"], "info", "downloaded", "qB 下载完成，主视频已通过保护检查", {
            "torrent_hash": torrent_hash,
            "input_path": picked["path"],
            "next_status": next_status,
            "needs_worker": needs_worker,
            "file_ready": file_ready,
        })
        return {"torrent_hash": torrent_hash, "status": next_status, "input_path": picked["path"]}

    return with_qbittorrent_client(base_url, qb_config, operation, timeout=30)


def qb_auth_methods(config: dict[str, Any]) -> list[str]:
    methods: list[str] = []
    if str(config.get("api_key") or "").strip():
        methods.append("api_key")
    if str(config.get("username") or "").strip() or str(config.get("password") or ""):
        methods.append("password")
    if not methods:
        methods.append("none")
    return methods


def apply_qbittorrent_auth(client: httpx.Client, base_url: str, config: dict[str, Any], method: str) -> None:
    if method == "api_key":
        api_key = str(config.get("api_key") or "").strip()
        if api_key:
            client.headers["Authorization"] = f"Bearer {api_key}"
        return
    if method == "password":
        username = str(config.get("username") or "")
        password = str(config.get("password") or "")
        login = client.post(f"{base_url}/api/v2/auth/login", data={"username": username, "password": password})
        login.raise_for_status()
        login_text = login.text.strip()
        if login_text and "Ok." not in login_text:
            if any(token in login_text.lower() for token in ("fail", "forbidden", "unauthorized", "denied")):
                raise RuntimeError(f"qBittorrent 账号密码登录失败: {login_text[:120]}")
            app_log("info", "qbittorrent", "qBittorrent 登录返回非标准文本，继续尝试请求", {
                "stage": "qb_login_nonstandard",
                "response": login_text[:120],
            })


def qb_auth_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {401, 403}
    text = str(exc).lower()
    return "forbidden" in text or "unauthorized" in text or "login failed" in text


def with_qbittorrent_client(
    base_url: str,
    config: dict[str, Any],
    operation: Callable[[httpx.Client, str], Any],
    *,
    timeout: float = 30,
) -> Any:
    methods = qb_auth_methods(config)
    last_exc: Exception | None = None
    for index, method in enumerate(methods):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                apply_qbittorrent_auth(client, base_url, config, method)
                return operation(client, method)
        except Exception as exc:
            last_exc = exc
            if index >= len(methods) - 1 or not qb_auth_error(exc):
                raise
            app_log("info", "qbittorrent", "qBittorrent 认证方式失败，切换下一种方式", {
                "stage": "qb_auth_fallback",
                "method": method,
                "next_method": methods[index + 1],
                "error": str(exc),
            })
    if last_exc:
        raise last_exc
    raise RuntimeError("qBittorrent 请求未执行")


def qb_torrent_files(client: httpx.Client, base_url: str, torrent_hash: str) -> list[dict[str, Any]]:
    try:
        resp = client.get(f"{base_url}/api/v2/torrents/files", params={"hash": torrent_hash})
        resp.raise_for_status()
        files = resp.json()
        return files if isinstance(files, list) else []
    except Exception:
        return []


def resolve_qb_file_path(content_path: str, file_name: str) -> str:
    name_path = Path(file_name)
    if name_path.is_absolute():
        return file_name.replace("\\", "/")
    content = Path(content_path)
    if content.suffix.lower() in VIDEO_EXTENSIONS:
        if not file_name or name_path.name.lower() == content.name.lower():
            return content_path.replace("\\", "/")
        return str(content.parent / file_name).replace("\\", "/")
    parts = name_path.parts
    if parts and parts[0].lower() == content.name.lower():
        return str(content.parent / file_name).replace("\\", "/")
    return (str(content / file_name) if content_path else file_name).replace("\\", "/")


def catalog_ids_match(expected: str, candidate: str) -> bool:
    expected_prefix, expected_number = av_id_parts(expected)
    candidate_prefix, candidate_number = av_id_parts(candidate)
    if not expected_prefix or not candidate_prefix:
        return False
    return (
        expected_prefix == candidate_prefix
        and (expected_number.lstrip("0") or "0") == (candidate_number.lstrip("0") or "0")
    )


def pick_main_video_file(files: list[dict[str, Any]], av_id: str, content_path: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    normalized_av_id = canonical_av_id(av_id)
    av_lower = normalized_av_id.lower()
    for item in files:
        name = str(item.get("name") or "")
        suffix = Path(name).suffix.lower()
        lower = name.lower()
        if suffix not in VIDEO_EXTENSIONS:
            continue
        if any(token in lower for token in ("sample", "trailer")):
            continue
        if normalized_av_id:
            detected = canonical_av_id(detect_catalog_number(name))
            if detected:
                if not catalog_ids_match(normalized_av_id, detected):
                    continue
            elif av_lower not in lower:
                continue
        size = int(item.get("size") or 0)
        full_path = resolve_qb_file_path(content_path, name)
        candidates.append({"path": full_path, "name": name, "size": size, "reason": "matched_av_largest_video"})
    if candidates:
        return sorted(candidates, key=lambda row: row["size"], reverse=True)[0]
    content = Path(content_path)
    detected_content = canonical_av_id(detect_catalog_number(content.name))
    if content.suffix.lower() in VIDEO_EXTENSIONS and (
        catalog_ids_match(normalized_av_id, detected_content) or (av_lower and av_lower in content.name.lower())
    ):
        return {"path": content_path, "name": content.name, "size": 0, "reason": "content_path_video"}
    return None


def local_postprocess_path_candidates(path: str) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    raw = str(path or "").strip()
    if not raw:
        return candidates
    rewritten = rewrite_proxy_path(raw)
    for label, value in (("mapped_path", rewritten), ("path", raw)):
        if not value:
            continue
        candidate = Path(str(value))
        if any(existing == candidate for _, existing in candidates):
            continue
        candidates.append((label, candidate))
    return candidates


def local_postprocess_file_ready(path: str, expected_size: int = 0) -> dict[str, Any]:
    candidates = local_postprocess_path_candidates(path)
    target = next((candidate for _, candidate in candidates if candidate.exists() and candidate.is_file()), None)
    if target is None:
        checked = [{label: str(candidate)} for label, candidate in candidates]
        return {
            "ok": False,
            "reason": "控制端无法读取下载文件，请检查 qB 下载目录和路径映射",
            "path": str(path or ""),
            "checked_paths": checked,
        }
    first = target.stat().st_size
    time.sleep(0.2)
    second = target.stat().st_size
    if first <= 0 or first != second:
        return {
            "ok": False,
            "reason": "下载文件本地大小未稳定",
            "path": str(target),
            "size_before": first,
            "size_after": second,
        }
    if expected_size > 0:
        tolerance = max(1024 * 1024, int(expected_size * 0.02))
        delta = abs(second - expected_size)
        if delta > tolerance:
            return {
                "ok": False,
                "reason": "下载文件本地大小与 qB 记录差异过大",
                "path": str(target),
                "file_size": second,
                "expected_size": expected_size,
                "size_delta": delta,
                "size_tolerance": tolerance,
            }
    return {"ok": True, "path": str(target), "file_size": second, "expected_size": expected_size}


def qb_protection_check(row: dict[str, Any], qb_config: dict[str, Any], content_path: str, selected_path: str = "") -> dict[str, Any]:
    post_settings = get_postprocess_service().get_settings()
    category = str(row.get("category") or "")
    tags = {item.strip().lower() for item in str(row.get("tags") or "").split(",") if item.strip()}
    allowed_categories = {item.lower() for item in post_settings.get("allowed_categories") or []}
    required_tags = {item.lower() for item in post_settings.get("required_tags") or []}
    download_dir = normalize_media_path(str(post_settings.get("download_dir") or ""))
    save_path = normalize_media_path(str(row.get("save_path") or qb_config.get("save_path") or ""))
    content_norm = normalize_media_path(content_path)
    selected_norm = normalize_media_path(selected_path)
    if allowed_categories and category.lower() not in allowed_categories:
        return {"ok": False, "reason": f"qB 分类 {category} 不在允许列表", "category": category}
    missing_tags = sorted(required_tags - tags)
    if missing_tags:
        return {"ok": False, "reason": f"qB 标签缺失: {', '.join(missing_tags)}", "tags": sorted(tags)}
    if download_dir and not normalized_media_path_is_under(content_norm, download_dir):
        return {"ok": False, "reason": f"下载内容路径不在 {download_dir}", "save_path": save_path, "content_path": content_norm}
    if download_dir and selected_norm and not normalized_media_path_is_under(selected_norm, download_dir):
        return {
            "ok": False,
            "reason": f"选中视频路径不在 {download_dir}",
            "save_path": save_path,
            "content_path": content_norm,
            "selected_path": selected_norm,
        }
    if download_dir and save_path and not normalized_media_path_is_under(save_path, download_dir):
        return {"ok": False, "reason": f"下载目录不在 {download_dir}", "save_path": save_path, "content_path": content_norm}
    return {"ok": True, "category": category, "tags": sorted(tags), "save_path": save_path, "content_path": content_norm, "selected_path": selected_norm}


def worker_is_offline() -> bool:
    try:
        status = subtitle_backend_status()
        return not bool(status.get("online") or status.get("status") == "ok")
    except Exception:
        return True


def build_postprocess_output_path(task: dict[str, Any], settings_payload: dict[str, Any]) -> str:
    input_path = Path(str(task.get("input_path") or ""))
    av_id = canonical_av_id(task.get("av_id")) or canonical_av_id(detect_catalog_number(input_path.stem)) or str(input_path.stem or "unknown").upper()
    filename = input_path.name if input_path.suffix.lower() in VIDEO_EXTENSIONS else f"{input_path.stem or av_id}.mkv"
    output_dir = Path(str(settings_payload.get("output_dir") or "/media/压制")) / av_id
    return str(output_dir / filename)


def build_postprocess_original_output_path(task: dict[str, Any], settings_payload: dict[str, Any], source_path: str) -> str:
    source = Path(str(source_path or task.get("input_path") or ""))
    av_id = canonical_av_id(task.get("av_id")) or canonical_av_id(detect_catalog_number(source.stem)) or str(source.stem or "unknown").upper()
    filename = source.name if source.suffix.lower() in VIDEO_EXTENSIONS else f"{source.stem or av_id}.mkv"
    output_dir = Path(str(settings_payload.get("output_dir") or "/media/压制")) / av_id
    return str(output_dir / filename)


def avoid_output_conflict(path: str, task_id: str) -> str:
    target = Path(path)
    if not output_path_conflicts(str(target), task_id):
        return str(target)
    suffix = "".join(target.suffixes) or target.suffix
    stem = target.name[: -len(suffix)] if suffix and target.name.endswith(suffix) else target.stem
    token = str(task_id or uuid.uuid4().hex)[:8]
    return str(target.with_name(f"{stem}.{token}{suffix}"))


def output_path_conflicts(path: str, task_id: str = "") -> bool:
    if not path:
        return False
    target_norm = normalize_media_path(path)
    if Path(path).exists():
        return True
    try:
        post = get_postprocess_service()
    except Exception:
        return False
    for task in post.list_tasks(limit=500):
        if task_id and str(task.get("id") or "") == task_id:
            continue
        existing = str(task.get("output_path") or "")
        if existing and normalize_media_path(existing) == target_norm:
            if str(task.get("status") or "") in {"failed", "ignored", "expired", "conflict"} and not Path(existing).exists():
                continue
            return True
    for version in post.list_versions(limit=500):
        existing = str(version.get("path") or "")
        if existing and normalize_media_path(existing) == target_norm:
            if str(version.get("status") or "") in {"failed", "trashed"} and not Path(existing).exists():
                continue
            return True
    return False


def ensure_managed_original_product(task: dict[str, Any], settings_payload: dict[str, Any], source_path: str) -> str:
    source = Path(source_path)
    output_root = Path(str(settings_payload.get("output_dir") or "/media/压制"))
    configured_output = str(task.get("output_path") or "").strip()
    if configured_output:
        target = Path(configured_output)
        try:
            if target.resolve() == source.resolve():
                target = Path(build_postprocess_original_output_path(task, settings_payload, str(source)))
        except OSError:
            target = Path(build_postprocess_original_output_path(task, settings_payload, str(source)))
    else:
        target = Path(build_postprocess_original_output_path(task, settings_payload, str(source)))
    if not path_under(target, output_root):
        target = Path(build_postprocess_original_output_path(task, settings_payload, str(source)))
    target = Path(avoid_output_conflict(str(target), str(task.get("id") or "")))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return str(target)


def ensure_managed_subtitle_product(subtitle_path: str, product_path: str) -> str:
    subtitle = Path(subtitle_path)
    product = Path(product_path)
    if not subtitle.exists() or not product.exists():
        return subtitle_path
    target = product.with_suffix(subtitle.suffix or ".srt")
    try:
        if subtitle.resolve() == target.resolve():
            return str(subtitle)
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(subtitle, target)
    return str(target)


POSTPROCESS_METADATA_IMAGE_SUFFIXES = ("fanart", "poster", "thumb")
POSTPROCESS_METADATA_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_FILE_GLOB_EXTENSIONS = ("*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv", "*.flv", "*.ts", "*.m2ts")


def nfo_actor_names(path: Path) -> list[str]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for actor in root.findall("actor"):
        name = str(actor.findtext("name") or "").strip()
        if not name:
            continue
        key = re.sub(r"\s+", "", name).lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def directory_catalog_ids(directory: Path, nfo_files: list[Path]) -> set[str]:
    ids: set[str] = set()
    detected_dir = canonical_av_id(detect_catalog_number(directory.name))
    if detected_dir:
        ids.add(detected_dir)
    for nfo in nfo_files:
        if nfo.name.lower() == "movie.nfo":
            continue
        detected = canonical_av_id(detect_catalog_number(nfo.stem))
        if detected:
            ids.add(detected)
    for pattern in VIDEO_FILE_GLOB_EXTENSIONS:
        for video in directory.glob(pattern):
            detected = canonical_av_id(detect_catalog_number(video.stem))
            if detected:
                ids.add(detected)
    return ids


def nfo_repair_actor_node(name: str) -> ET.Element:
    actor = ET.Element("actor")
    name_node = ET.SubElement(actor, "name")
    name_node.text = name
    type_node = ET.SubElement(actor, "type")
    type_node.text = "Actor"
    return actor


def insert_nfo_actor_nodes(path: Path, actors: list[str]) -> dict[str, Any]:
    original_mode = 0o644
    try:
        original_mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    tree = ET.parse(path)
    root = tree.getroot()
    existing_keys = {re.sub(r"\s+", "", name).lower() for name in nfo_actor_names(path)}
    inserted: list[str] = []
    insert_at = len(root)
    for index, child in enumerate(list(root)):
        if child.tag == "fileinfo":
            insert_at = index
            break
    for name in actors:
        key = re.sub(r"\s+", "", name).lower()
        if not key or key in existing_keys:
            continue
        root.insert(insert_at, nfo_repair_actor_node(name))
        insert_at += 1
        existing_keys.add(key)
        inserted.append(name)
    if not inserted:
        return {"status": "skipped", "reason": "actor_exists", "inserted": []}
    try:
        ET.indent(tree, space="  ")
    except Exception:
        pass
    backup = path.with_name(f"{path.name}.bak-{local_now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    temp_path = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        tree.write(temp_path, encoding="utf-8", xml_declaration=True)
        try:
            temp_path.chmod(original_mode)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return {"status": "updated", "backup": str(backup), "inserted": inserted}


def nfo_actor_repair_candidates(*, apply: bool = False) -> dict[str, Any]:
    post_settings = get_postprocess_service().get_settings()
    output_root = Path(str(post_settings.get("output_dir") or "/media/压制"))
    payload: dict[str, Any] = {
        "status": "ok",
        "root": str(output_root),
        "dry_run": not apply,
        "checked_dirs": 0,
        "repairable_dirs": 0,
        "target_files": 0,
        "repaired_files": 0,
        "items": [],
        "skipped": [],
    }
    if not output_root.exists() or not output_root.is_dir():
        payload["status"] = "skipped"
        payload["reason"] = "output_dir_missing"
        return payload

    directories = sorted({path.parent for path in output_root.rglob("*.nfo") if path.is_file()})
    for directory in directories:
        if directory == output_root:
            payload["skipped"].append({"directory": str(directory), "reason": "skip_output_root"})
            continue
        nfo_files = sorted(path for path in directory.glob("*.nfo") if path.is_file())
        if len(nfo_files) < 2:
            continue
        payload["checked_dirs"] += 1
        catalog_ids = directory_catalog_ids(directory, nfo_files)
        if len(catalog_ids) > 1:
            payload["skipped"].append({"directory": str(directory), "reason": "multiple_catalog_ids", "catalog_ids": sorted(catalog_ids)})
            continue
        file_rows: list[dict[str, Any]] = []
        actor_names: list[str] = []
        actor_keys: set[str] = set()
        for nfo in nfo_files:
            actors = nfo_actor_names(nfo)
            for name in actors:
                key = re.sub(r"\s+", "", name).lower()
                if key and key not in actor_keys:
                    actor_keys.add(key)
                    actor_names.append(name)
            file_rows.append({"path": str(nfo), "name": nfo.name, "actors": actors})
        targets = [row for row in file_rows if not row["actors"]]
        sources = [row for row in file_rows if row["actors"]]
        if not actor_names or not targets:
            continue

        item: dict[str, Any] = {
            "directory": str(directory),
            "catalog_id": sorted(catalog_ids)[0] if catalog_ids else "",
            "actors": actor_names,
            "source_files": [row["name"] for row in sources],
            "target_files": [row["name"] for row in targets],
            "results": [],
        }
        payload["repairable_dirs"] += 1
        payload["target_files"] += len(targets)
        if apply:
            for target in targets:
                path = Path(str(target["path"]))
                if not path_under(path, output_root):
                    result = {"file": target["name"], "status": "skipped", "reason": "outside_output_dir"}
                else:
                    try:
                        result = {"file": target["name"], **insert_nfo_actor_nodes(path, actor_names)}
                    except Exception as exc:
                        result = {"file": target["name"], "status": "failed", "reason": str(exc)}
                if result.get("status") == "updated":
                    payload["repaired_files"] += 1
                item["results"].append(result)
        payload["items"].append(item)
    return payload


def nfo_is_backup_or_auxiliary(path: Path, output_root: Path) -> bool:
    try:
        relative = path.relative_to(output_root)
    except ValueError:
        relative = path
    lower_parts = {part.lower() for part in relative.parts}
    if lower_parts & {"nfo_back", "backup", "backups"}:
        return True
    name_lower = path.name.lower()
    return ".bak-" in name_lower or name_lower.startswith(".")


def matching_video_for_nfo(nfo_path: Path) -> Path | None:
    if nfo_path.stem.lower() == "movie":
        return None
    for suffix in VIDEO_EXTENSIONS:
        candidate = nfo_path.with_suffix(suffix)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def jellyfin_people_names(item: dict[str, Any]) -> list[str]:
    people = item.get("People") if isinstance(item.get("People"), list) else []
    names: list[str] = []
    seen: set[str] = set()
    for person in people:
        if not isinstance(person, dict):
            continue
        person_type = str(person.get("Type") or person.get("Role") or "").strip().lower()
        if person_type and person_type != "actor":
            continue
        name = str(person.get("Name") or "").strip()
        key = re.sub(r"\s+", "", name).lower()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def find_jellyfin_item_for_video(av_id: str, video_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    if not base_url or not api_key or not av_id:
        return None
    user_id = get_jellyfin_user_id(config)
    endpoint = f"/Users/{user_id}/Items" if user_id else "/Items"
    library_id = str(config.get("library_id") or "").strip()
    expected_path = normalize_media_path(str(video_path))
    params = {
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Video,Episode",
        "SearchTerm": av_id,
        "Limit": "20",
        "Fields": "Path,People,ProviderIds,MediaSources",
    }
    if library_id:
        params["ParentId"] = library_id
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            response = client.get(f"{base_url}{endpoint}", headers=jellyfin_auth_headers(config), params=params)
            response.raise_for_status()
            items = response.json().get("Items", [])
    except (httpx.HTTPError, ValueError):
        return None
    fallback: dict[str, Any] | None = None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        path_value = str(item.get("Path") or "")
        haystack = f"{item.get('Name') or ''} {path_value}".lower()
        if av_id.lower() not in haystack:
            continue
        if not fallback:
            fallback = item
        if expected_path and normalize_media_path(path_value) == expected_path:
            return item
    return fallback


def jellyfin_item_with_people(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if "People" in item:
        return item
    item_id = str(item.get("Id") or "").strip()
    if not item_id:
        return item
    try:
        detail = fetch_jellyfin_item(item_id, config)
    except HTTPException:
        return item
    if not isinstance(detail, dict):
        return item
    merged = {**item, **detail}
    if "People" not in merged and "People" in detail:
        merged["People"] = detail["People"]
    return merged


def refresh_jellyfin_item_metadata(item_id: str, config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    clean_item_id = str(item_id or "").strip()
    if not base_url or not api_key:
        return {"status": "skipped", "reason": "jellyfin_not_configured"}
    if not clean_item_id:
        return {"status": "skipped", "reason": "missing_item_id"}
    params = {
        "Recursive": "false",
        "MetadataRefreshMode": "FullRefresh",
        "ImageRefreshMode": "Default",
        "ReplaceAllMetadata": "true",
        "ReplaceAllImages": "false",
    }
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            response = client.post(
                f"{base_url}/Items/{clean_item_id}/Refresh",
                headers=jellyfin_auth_headers(config),
                params=params,
            )
            response.raise_for_status()
        return {"status": "refreshed"}
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)}


def jellyfin_actor_refresh_candidates(*, apply: bool = False) -> dict[str, Any]:
    post_settings = get_postprocess_service().get_settings()
    output_root = Path(str(post_settings.get("output_dir") or "/media/压制"))
    config = jellyfin_config()
    payload: dict[str, Any] = {
        "status": "ok",
        "root": str(output_root),
        "dry_run": not apply,
        "checked_nfos": 0,
        "actor_nfos": 0,
        "matched_items": 0,
        "target_items": 0,
        "refreshed_items": 0,
        "items": [],
        "skipped": [],
    }
    if not output_root.exists() or not output_root.is_dir():
        payload["status"] = "skipped"
        payload["reason"] = "output_dir_missing"
        return payload
    if not str(config.get("url") or "").strip() or not str(config.get("api_key") or "").strip():
        payload["status"] = "skipped"
        payload["reason"] = "jellyfin_not_configured"
        return payload

    for nfo in sorted(output_root.rglob("*.nfo")):
        if not nfo.is_file() or nfo_is_backup_or_auxiliary(nfo, output_root):
            continue
        payload["checked_nfos"] += 1
        actors = nfo_actor_names(nfo)
        if not actors:
            continue
        payload["actor_nfos"] += 1
        video = matching_video_for_nfo(nfo)
        if not video:
            payload["skipped"].append({"nfo": str(nfo), "reason": "matching_video_missing"})
            continue
        av_id = canonical_av_id(detect_catalog_number(nfo.stem))
        if not av_id:
            payload["skipped"].append({"nfo": str(nfo), "reason": "catalog_id_missing"})
            continue
        item = find_jellyfin_item_for_video(av_id, video, config)
        if not item:
            payload["skipped"].append({"nfo": str(nfo), "video": str(video), "av_id": av_id, "reason": "jellyfin_item_missing"})
            continue
        payload["matched_items"] += 1
        item = jellyfin_item_with_people(item, config)
        item_people = jellyfin_people_names(item)
        if item_people:
            continue
        row: dict[str, Any] = {
            "av_id": av_id,
            "nfo": str(nfo),
            "video": str(video),
            "actors": actors,
            "item_id": str(item.get("Id") or ""),
            "item_name": str(item.get("Name") or ""),
            "item_path": str(item.get("Path") or ""),
            "results": [],
        }
        payload["target_items"] += 1
        if apply:
            result = refresh_jellyfin_item_metadata(row["item_id"], config)
            if result.get("status") == "refreshed":
                payload["refreshed_items"] += 1
            row["results"].append(result)
        payload["items"].append(row)
    return payload


def move_postprocess_metadata_sidecars(task: dict[str, Any], product_path: str) -> dict[str, Any]:
    source = Path(str(task.get("input_path") or ""))
    product = Path(product_path)
    payload: dict[str, Any] = {
        "source": str(source),
        "target_dir": str(product.parent),
        "moved": [],
        "skipped": [],
        "missing": [],
    }
    if not source.parent.exists() or not product.parent.exists():
        payload["status"] = "skipped"
        payload["reason"] = "source_or_target_dir_missing"
        return payload

    av_id = str(task.get("av_id") or source.stem or product.stem or "").upper()
    prefixes: list[str] = []
    for prefix in (source.stem, av_id):
        prefix = str(prefix or "").strip()
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)

    wanted: list[tuple[str, str, tuple[str, ...]]] = [("nfo", "", (".nfo",))]
    wanted.extend((kind, f"-{kind}", POSTPROCESS_METADATA_IMAGE_EXTENSIONS) for kind in POSTPROCESS_METADATA_IMAGE_SUFFIXES)

    for kind, name_suffix, extensions in wanted:
        found: Path | None = None
        for prefix in prefixes:
            for extension in extensions:
                candidate = source.parent / f"{prefix}{name_suffix}{extension}"
                if candidate.exists() and candidate.is_file():
                    found = candidate
                    break
            if found:
                break
        if not found:
            payload["missing"].append(kind)
            continue

        target = product.parent / found.name
        try:
            if found.resolve() == target.resolve():
                payload["skipped"].append({"kind": kind, "path": str(found), "reason": "already_in_place"})
                continue
        except OSError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            payload["skipped"].append({"kind": kind, "source": str(found), "target": str(target), "reason": "target_exists"})
            continue
        shutil.move(str(found), str(target))
        payload["moved"].append({"kind": kind, "source": str(found), "target": str(target)})

    payload["status"] = "ok" if payload["moved"] or payload["skipped"] else "missing"
    return payload


def record_postprocess_metadata_sidecars(task_id: str, task: dict[str, Any], product_path: str) -> dict[str, Any]:
    post = get_postprocess_service()
    try:
        payload = move_postprocess_metadata_sidecars(task, product_path)
        post.update_task(task_id, data={"metadata_sidecars": payload})
        post.add_event(
            task_id,
            "info",
            "metadata_sidecars_moved",
            "刮削元数据已移动到转码目录",
            payload,
        )
        return payload
    except Exception as exc:
        payload = {"status": "failed", "error": str(exc), "output_path": product_path}
        post.update_task(task_id, data={"metadata_sidecars": payload})
        post.add_event(
            task_id,
            "error",
            "metadata_sidecars_failed",
            "刮削元数据同步失败，视频成品继续进入后处理链路",
            payload,
        )
        return payload


def subtitle_job_from_worker_result(worker_result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(worker_result, dict):
        return {}
    job = worker_result.get("subtitle_job")
    return job if isinstance(job, dict) else {}


def subtitle_artifact_candidates(worker_result: dict[str, Any] | None) -> list[Path]:
    job = subtitle_job_from_worker_result(worker_result)
    paths: list[Path] = []
    for key in SUBTITLE_FILE_KINDS:
        value = str(job.get(key) or "").strip()
        if value:
            paths.append(Path(rewrite_backend_path_to_console(value) or value))
    return paths


def ensure_managed_vtt_product(worker_result: dict[str, Any] | None, product_path: str) -> str:
    job = subtitle_job_from_worker_result(worker_result)
    vtt_path = str(job.get("translated_vtt") or job.get("original_vtt") or "").strip()
    if not vtt_path:
        return ""
    return ensure_managed_subtitle_product(rewrite_backend_path_to_console(vtt_path) or vtt_path, product_path)


def cleanup_postprocess_subtitle_artifacts(
    product_path: str,
    worker_result: dict[str, Any] | None,
    keep_paths: list[str],
) -> dict[str, Any]:
    product = Path(product_path)
    try:
        product_dir = product.parent.resolve()
    except OSError:
        product_dir = product.parent
    keep: set[str] = set()
    for item in keep_paths:
        if not item:
            continue
        try:
            keep.add(str(Path(item).resolve()))
        except OSError:
            keep.add(str(Path(item)))
    candidates: list[Path] = []
    candidates.extend(subtitle_artifact_candidates(worker_result))
    candidates.extend(product.parent.glob(f"{product.stem}.zh.*"))
    candidates.extend(product.parent.glob(f"{product.stem}.bilingual.*"))
    unique_candidates = list(dict.fromkeys(candidates))
    cleanup_results: list[dict[str, object]] = []
    media_dirs, trash_dir, data_dir = settings()
    store = Storage(data_dir, trash_dir, media_dirs)
    for candidate in unique_candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if str(resolved) in keep:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            if resolved.parent.resolve() != product_dir:
                continue
        except OSError:
            continue
        result = store.move_to_trash([MoveRequest(source=resolved)])[0]
        cleanup_results.append(move_result_payload(result))
    return {"checked": len(unique_candidates), "moved": cleanup_results}


def dispatch_postprocess_task(task: dict[str, Any]) -> dict[str, Any]:
    post = get_postprocess_service()
    settings_payload = post.get_settings()
    task_id = str(task.get("id") or "")
    input_path = str(task.get("input_path") or "").strip()
    if not input_path:
        raise RuntimeError("任务缺少输入文件，不能派发到算力端")
    if bool(settings_payload.get("auto_transcode_enabled")):
        transcode_settings = normalize_transcode_settings_payload({
            **settings_payload,
            "target_codec": task.get("target_codec") or settings_payload.get("target_codec"),
        })
        output_path = str(task.get("output_path") or avoid_output_conflict(build_postprocess_output_path(task, settings_payload), task_id))
        remote_worker = bool(backend_url())
        callback_url = require_remote_callback_url(task_id) if remote_worker else ""
        payload = {
            "task_id": task_id,
            "av_id": task.get("av_id", ""),
            "input_path": rewrite_proxy_path(input_path) if remote_worker else input_path,
            "output_path": rewrite_proxy_path(output_path) if remote_worker else output_path,
            "console_input_path": input_path,
            "console_output_path": output_path,
            "target_codec": transcode_settings.get("target_codec"),
            "target_encoder": transcode_settings.get("target_encoder"),
            "crf": transcode_settings.get("crf"),
            "preset": transcode_settings.get("preset"),
            "preset_flag": transcode_settings.get("preset_flag"),
            "ffmpeg_mode": transcode_settings.get("ffmpeg_mode"),
            "ffmpeg_standard_enabled": transcode_settings.get("ffmpeg_standard_enabled"),
            "ffmpeg_custom_enabled": transcode_settings.get("ffmpeg_custom_enabled"),
            "ffmpeg_custom_template": transcode_settings.get("ffmpeg_custom_template"),
            "ffmpeg_standard_command": build_ffmpeg_preview(transcode_settings),
            "callback_url": callback_url,
            "callback_token": frontend_api_token() if remote_worker else "",
        }
        if remote_worker:
            result = remote_post_json("/api/transcode/jobs", payload, timeout=60)
        else:
            payload["job_id"] = uuid.uuid4().hex
            result = create_transcode_job(payload, start=False)
        worker_job_id = str(result.get("job_id") or result.get("id") or "")
        post.update_task(
            task_id,
            status="sent_to_worker",
            output_path=output_path,
            error_code="",
            error_message="",
            data={"worker_job_id": worker_job_id, "worker_payload": payload, "worker_result": result},
        )
        post.add_event(task_id, "info", "sent_to_worker", "转码任务已派发到算力端", {
            "worker_job_id": worker_job_id,
            "input_path": input_path,
            "output_path": output_path,
            "target_codec": transcode_settings.get("target_codec"),
            "target_encoder": transcode_settings.get("target_encoder"),
        })
        if not remote_worker:
            start_transcode_job(worker_job_id)
        return {"task_id": task_id, "status": "sent_to_worker", "worker_job_id": worker_job_id}

    if bool(settings_payload.get("auto_subtitle_enabled")) and bool(task.get("needs_subtitle")):
        output_path = str(task.get("output_path") or avoid_output_conflict(build_postprocess_original_output_path(task, settings_payload, input_path), task_id))
        result = submit_subtitle_job_for_path(input_path)
        subtitle_job_id = str(result.get("id") or result.get("job_id") or "")
        post.update_task(
            task_id,
            status="subtitle_processing",
            output_path=output_path,
            error_code="",
            error_message="",
            data={"subtitle_job_id": subtitle_job_id, "subtitle_result": result, "planned_output_path": output_path},
        )
        post.add_event(task_id, "info", "subtitle_processing", "字幕任务已派发到算力端", {
            "subtitle_job_id": subtitle_job_id,
            "input_path": input_path,
            "planned_output_path": output_path,
        })
        return {"task_id": task_id, "status": "subtitle_processing", "subtitle_job_id": subtitle_job_id}

    post.update_task(task_id, status="version_validating", error_code="", error_message="")
    post.add_event(task_id, "info", "version_validating", "自动转码和自动字幕均未开启，开始本地托管成品校验", {
        "input_path": input_path,
    })
    return validate_and_activate_postprocess_task(task_id, output_path=input_path)


def submit_postprocess_subtitle_task(task: dict[str, Any], video_path: str) -> dict[str, Any]:
    post = get_postprocess_service()
    task_id = str(task.get("id") or "")
    if not video_path:
        raise RuntimeError("缺少字幕处理视频路径")
    result = submit_subtitle_job_for_path(video_path)
    subtitle_job_id = str(result.get("id") or result.get("job_id") or "")
    post.update_task(
        task_id,
        status="subtitle_processing",
        output_path=video_path,
        error_code="",
        error_message="",
        data={"subtitle_job_id": subtitle_job_id, "subtitle_result": result},
    )
    post.add_event(task_id, "info", "subtitle_processing", "字幕任务已派发到算力端", {
        "subtitle_job_id": subtitle_job_id,
        "video_path": video_path,
    })
    return {"task_id": task_id, "status": "subtitle_processing", "subtitle_job_id": subtitle_job_id}


def subtitle_job_status(job_id: str) -> dict[str, Any] | None:
    if not job_id:
        return None
    if backend_url():
        payload = remote_get(f"/api/subtitle/jobs/{job_id}")
        if isinstance(payload.get("job"), dict):
            return payload["job"]
        return payload
    job = get_subtitle_service().get_job(job_id)
    return job_payload(job) if job else None


def pick_subtitle_output(job: dict[str, Any]) -> str:
    for key in ("translated_srt", "bilingual_srt", "original_srt"):
        value = str(job.get(key) or "")
        if value:
            return value
    return ""


def file_stat_payload(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"file_size": int(stat.st_size), "mtime": float(stat.st_mtime)}


def run_ffprobe(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("Unraid 控制端未找到 ffprobe，不能校验成品")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout or "{}")


def validate_video_output(path: str, *, target_codec: str = "", source_path: str = "") -> dict[str, Any]:
    output = Path(path)
    if not output.exists() or not output.is_file():
        return {"ok": False, "reason": "输出文件不存在", "path": str(output)}
    first = output.stat().st_size
    time.sleep(0.2)
    second = output.stat().st_size
    if first <= 0 or first != second:
        return {"ok": False, "reason": "输出文件大小不稳定或为空", "path": str(output), "size_before": first, "size_after": second}
    try:
        probe = run_ffprobe(output)
    except Exception as exc:
        return {"ok": False, "reason": f"ffprobe 校验失败: {exc}", "path": str(output)}
    video_streams = [item for item in probe.get("streams", []) if item.get("codec_type") == "video"]
    if not video_streams:
        return {"ok": False, "reason": "ffprobe 未找到视频流", "path": str(output), "probe": probe}
    codec_name = str(video_streams[0].get("codec_name") or "").lower()
    expected = {"h265": "hevc", "hevc": "hevc", "av1": "av1"}.get(str(target_codec or "").lower(), "")
    if expected and codec_name != expected:
        return {"ok": False, "reason": f"视频编码不匹配，期望 {expected}，实际 {codec_name}", "path": str(output), "codec_name": codec_name}
    duration = 0.0
    try:
        duration = float(probe.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return {"ok": False, "reason": "视频时长无效", "path": str(output), "codec_name": codec_name}
    width = int(video_streams[0].get("width") or 0)
    height = int(video_streams[0].get("height") or 0)
    if width < 64 or height < 64:
        return {
            "ok": False,
            "reason": "视频分辨率异常",
            "path": str(output),
            "codec_name": codec_name,
            "duration": duration,
            "width": width,
            "height": height,
        }
    payload = file_stat_payload(output)
    if int(payload.get("file_size") or 0) < 4096:
        return {
            "ok": False,
            "reason": "视频文件大小异常",
            "path": str(output),
            "codec_name": codec_name,
            "duration": duration,
            "file_size": payload.get("file_size"),
        }
    source_duration = probe_video_duration(source_path) if source_path else 0.0
    if source_duration > 0:
        tolerance = max(2.0, source_duration * 0.1)
        delta = abs(duration - source_duration)
        if delta > tolerance:
            return {
                "ok": False,
                "reason": "输出时长与源文件差异过大",
                "path": str(output),
                "codec_name": codec_name,
                "duration": duration,
                "source_duration": source_duration,
                "duration_delta": delta,
                "duration_tolerance": tolerance,
            }
    payload.update({
        "ok": True,
        "path": str(output),
        "codec_name": codec_name,
        "duration": duration,
        "source_duration": source_duration,
        "width": width,
        "height": height,
    })
    return payload


SUBTITLE_TIMESTAMP_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})(?P<f>[,.]\d{1,3})?"
)


def subtitle_timestamp_seconds(value: str) -> float:
    match = SUBTITLE_TIMESTAMP_RE.search(value or "")
    if not match:
        return 0.0
    fraction = str(match.group("f") or "").replace(",", ".")
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + (float(fraction) if fraction else 0.0)
    )


def subtitle_last_timestamp(raw: str) -> float:
    last = 0.0
    for match in SUBTITLE_TIMESTAMP_RE.finditer(raw or ""):
        last = max(last, subtitle_timestamp_seconds(match.group(0)))
    return last


def probe_video_duration(path: str) -> float:
    try:
        probe = run_ffprobe(Path(path))
        return float(probe.get("format", {}).get("duration") or 0)
    except Exception:
        return 0.0


def validate_subtitle_output(path: str, *, video_path: str = "") -> dict[str, Any]:
    subtitle = Path(path)
    if not subtitle.exists() or not subtitle.is_file():
        return {"ok": False, "reason": "字幕文件不存在", "path": str(subtitle)}
    raw = subtitle.read_text(encoding="utf-8", errors="strict")
    if not raw.strip():
        return {"ok": False, "reason": "字幕文件为空", "path": str(subtitle)}
    chinese_chars = sum(1 for char in raw if "\u4e00" <= char <= "\u9fff")
    visible_chars = sum(1 for char in raw if not char.isspace())
    ratio = chinese_chars / visible_chars if visible_chars else 0
    cues = raw.count("-->")
    if cues <= 0:
        return {"ok": False, "reason": "字幕时间轴无效", "path": str(subtitle), "cue_count": cues}
    if ratio < 0.03:
        return {"ok": False, "reason": "中文字幕占比过低", "path": str(subtitle), "chinese_ratio": ratio, "cue_count": cues}
    payload = file_stat_payload(subtitle)
    last_timestamp = subtitle_last_timestamp(raw)
    video_duration = probe_video_duration(video_path) if video_path else 0.0
    if video_duration > 0:
        min_coverage = max(1.0, video_duration * 0.5)
        if last_timestamp < min_coverage:
            return {
                "ok": False,
                "reason": "字幕覆盖时长过短",
                "path": str(subtitle),
                "chinese_ratio": ratio,
                "cue_count": cues,
                "last_timestamp": last_timestamp,
                "video_duration": video_duration,
                "min_coverage": min_coverage,
            }
    payload.update({
        "ok": True,
        "path": str(subtitle),
        "chinese_ratio": ratio,
        "cue_count": cues,
        "last_timestamp": last_timestamp,
        "video_duration": video_duration,
    })
    return payload


def path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def unraid_disk_roots_for_postprocess() -> list[Path]:
    root = Path(os.getenv("UNRAID_MOUNT_ROOT", "/unraid"))
    if os.name == "nt" or not root.exists():
        return []
    roots: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        name = child.name.lower()
        if name.startswith("disk") or name.startswith("cache"):
            roots.append(child)
    return sorted(roots, key=lambda item: item.name)


def postprocess_unraid_relative(root: Path, kind: str) -> Path:
    env_key = "POSTPROCESS_DOWNLOAD_UNRAID_RELATIVE" if kind == "download" else "POSTPROCESS_OUTPUT_UNRAID_RELATIVE"
    configured = str(os.getenv(env_key, "") or "").strip().strip("/\\")
    if configured:
        return Path(configured)
    root_text = str(root).replace("\\", "/").rstrip("/")
    media_dirs, _, _ = settings()
    for media_dir in media_dirs or [Path("/media")]:
        media_text = str(media_dir).replace("\\", "/").rstrip("/")
        if not media_text:
            continue
        if root_text == media_text:
            return Path(media_dir.name)
        if root_text.startswith(media_text + "/"):
            suffix = root_text[len(media_text) :].strip("/")
            return Path(media_dir.name, *suffix.split("/")) if suffix else Path(media_dir.name)
    if kind == "output" and root.name == "压制":
        return Path("media") / root.name
    return Path(root.name)


def try_unraid_postprocess_fast_move(
    *,
    source: Path,
    logical_target: Path,
    root: Path,
    relative: Path,
    kind: str,
    trash_relative: Path,
) -> dict[str, Any] | None:
    root_relative = postprocess_unraid_relative(root, kind)
    for disk_root in unraid_disk_roots_for_postprocess():
        physical_source = disk_root / root_relative / relative
        if not physical_source.exists():
            continue
        physical_target = disk_root / Path(os.getenv("UNRAID_TRASH_RELATIVE", "media/trash")) / trash_relative
        if physical_target.exists():
            raise RuntimeError(f"同盘快速回收目标已存在: {physical_target}")
        physical_target.parent.mkdir(parents=True, exist_ok=True)
        physical_source.rename(physical_target)
        return {
            "source": str(source),
            "target": str(logical_target),
            "status": "moved",
            "reason": "同盘快速移动",
            "mode": "fast",
            "physical_source": str(physical_source),
            "physical_target": str(physical_target),
        }
    return None


def validate_managed_version_trashable(version: dict[str, Any], settings_payload: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(version.get("path") or ""))
    output_root = Path(str(settings_payload.get("output_dir") or "/media/压制"))
    if str(version.get("generated_by") or "") != "moviemuse":
        raise RuntimeError("旧版本不是 MovieMuse 托管版本，拒绝移动")
    if not path_under(source, output_root):
        raise RuntimeError("旧版本路径不在托管输出目录，拒绝移动")
    if not source.exists() or not source.is_file():
        raise RuntimeError("旧版本文件不存在，拒绝移动")
    recorded_size = int(version.get("file_size") or 0)
    recorded_mtime = float(version.get("mtime") or 0)
    current = source.stat()
    if recorded_size and int(current.st_size) != recorded_size:
        raise RuntimeError("旧版本文件大小与版本记录不一致，拒绝移动")
    if recorded_mtime and abs(float(current.st_mtime) - recorded_mtime) > 2:
        raise RuntimeError("旧版本 mtime 与版本记录不一致，拒绝移动")
    relative = source.resolve().relative_to(output_root.resolve())
    return {
        "source": source,
        "output_root": output_root,
        "relative": relative,
        "file_size": int(current.st_size),
        "mtime": float(current.st_mtime),
    }


def move_managed_version_to_trash(version: dict[str, Any], settings_payload: dict[str, Any]) -> dict[str, Any]:
    validation = validate_managed_version_trashable(version, settings_payload)
    source = validation["source"]
    output_root = validation["output_root"]
    relative = validation["relative"]
    media_dirs, trash_dir, data_dir = settings()
    logical_target = trash_dir / "postprocess" / relative
    fast_result = try_unraid_postprocess_fast_move(
        source=source,
        logical_target=logical_target,
        root=output_root,
        relative=relative,
        kind="output",
        trash_relative=Path("postprocess") / relative,
    )
    if fast_result:
        return fast_result
    store = Storage(data_dir, trash_dir, media_dirs)
    result = store.move_to_trash([MoveRequest(source=source)])[0]
    if result.status == "moved":
        return move_result_payload(result)
    target = logical_target
    if target.exists():
        raise RuntimeError(f"托管版本回收目标已存在: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return {"source": str(source), "target": str(target), "status": "moved", "reason": "managed output fallback", "mode": "managed"}


def move_postprocess_source_to_trash(task: dict[str, Any], product_path: str, settings_payload: dict[str, Any]) -> dict[str, Any] | None:
    source_value = str(task.get("input_path") or "").strip()
    if not source_value:
        return None
    source = Path(source_value)
    product = Path(str(product_path or ""))
    if not source:
        return None
    try:
        if product and source.resolve() == product.resolve():
            return None
    except OSError:
        return None
    download_root = Path(str(settings_payload.get("download_dir") or "/media/study3"))
    if not path_under(source, download_root):
        raise RuntimeError("源文件不在后处理下载目录内，拒绝自动清理")
    if not source.exists() or not source.is_file():
        return {"source": str(source), "status": "skipped", "reason": "源文件不存在，可能已被清理"}

    media_dirs, trash_dir, data_dir = settings()
    relative = source.resolve().relative_to(download_root.resolve())
    logical_target = trash_dir / "postprocess" / "source" / relative
    fast_result = try_unraid_postprocess_fast_move(
        source=source,
        logical_target=logical_target,
        root=download_root,
        relative=relative,
        kind="download",
        trash_relative=Path("postprocess") / "source" / relative,
    )
    if fast_result:
        return fast_result
    store = Storage(data_dir, trash_dir, media_dirs)
    result = store.move_to_trash([MoveRequest(source=source)])[0]
    if result.status == "moved":
        return move_result_payload(result)

    target = logical_target
    if target.exists():
        raise RuntimeError(f"源文件回收目标已存在: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return {"source": str(source), "target": str(target), "status": "moved", "reason": "postprocess source fallback", "mode": "managed"}


def validate_and_activate_postprocess_task(
    task_id: str,
    *,
    output_path: str = "",
    subtitle_path: str = "",
    subtitle_error: dict[str, Any] | None = None,
    worker_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    post = get_postprocess_service()
    task = post.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="后处理任务不存在")
    settings_payload = post.get_settings()
    chosen_output = rewrite_backend_path_to_console(output_path or str(task.get("output_path") or "")) or ""
    subtitle_path = rewrite_backend_path_to_console(subtitle_path) or ""
    if worker_result:
        post.update_task(task_id, data={"worker_result": worker_result})
    subtitle_failure = dict(subtitle_error or {})
    if subtitle_failure:
        post.add_event(task_id, "error", "subtitle_failed", "字幕阶段失败，继续保留并激活视频成品", subtitle_failure)
    task_data = task.get("data") if isinstance(task.get("data"), dict) else {}
    metadata_sidecars = task_data.get("metadata_sidecars") if isinstance(task_data.get("metadata_sidecars"), dict) else {}
    if bool(settings_payload.get("auto_transcode_enabled")):
        post.update_task(task_id, status="transcode_validating", output_path=chosen_output)
        validation = validate_video_output(
            chosen_output,
            target_codec=str(task.get("target_codec") or settings_payload.get("target_codec") or ""),
            source_path=str(task.get("input_path") or ""),
        )
        post.add_event(task_id, "info" if validation.get("ok") else "error", "transcode_validating", "转码成品校验完成", validation)
        if not validation.get("ok"):
            post.update_task(task_id, status="failed", error_code="transcode_validation_failed", error_message=str(validation.get("reason") or "转码校验失败"))
            return {"status": "failed", "validation": validation}
        product_path = str(validation.get("path") or chosen_output)
        if not subtitle_path:
            metadata_sidecars = record_postprocess_metadata_sidecars(task_id, task, product_path)
        if bool(settings_payload.get("auto_subtitle_enabled")) and bool(task.get("needs_subtitle")) and not subtitle_path and not subtitle_failure:
            post.update_task(task_id, status="transcode_done", output_path=product_path, data={"transcode_validation": validation, "metadata_sidecars": metadata_sidecars})
            post.add_event(task_id, "info", "transcode_done", "转码校验通过，继续派发字幕任务", {"output_path": product_path})
            return submit_postprocess_subtitle_task({**task, "output_path": product_path}, product_path)
        has_chinese_subtitle = False
    else:
        input_source = str(task.get("input_path") or "")
        source_product = chosen_output or input_source
        if chosen_output and not Path(chosen_output).exists() and input_source and Path(input_source).exists():
            source_product = input_source
        output_root = Path(str(settings_payload.get("output_dir") or "/media/压制"))
        if path_under(Path(source_product), output_root):
            product_path = source_product
        else:
            try:
                product_path = ensure_managed_original_product(task, settings_payload, source_product)
                post.update_task(task_id, output_path=product_path, data={"managed_original_source": source_product})
                post.add_event(task_id, "info", "managed_original_created", "原始视频已复制到托管成品目录", {
                    "source_path": source_product,
                    "output_path": product_path,
                })
            except Exception as exc:
                post.update_task(task_id, status="failed", error_code="managed_original_failed", error_message=str(exc))
                post.add_event(task_id, "error", "managed_original_failed", "原始视频复制到托管成品目录失败", {
                    "source_path": source_product,
                    "error": str(exc),
                })
                return {"status": "failed", "message": str(exc)}
        validation = validate_video_output(product_path, target_codec="", source_path=str(task.get("input_path") or ""))
        if not validation.get("ok"):
            post.update_task(task_id, status="failed", error_code="video_validation_failed", error_message=str(validation.get("reason") or "视频校验失败"))
            post.add_event(task_id, "error", "video_validation_failed", "视频成品校验失败", validation)
            return {"status": "failed", "validation": validation}
        has_chinese_subtitle = False
    subtitle_validation: dict[str, Any] | None = None
    if subtitle_path:
        subtitle_validation = validate_subtitle_output(subtitle_path, video_path=product_path)
        post.add_event(task_id, "info" if subtitle_validation.get("ok") else "error", "subtitle_validating", "字幕成品校验完成", subtitle_validation)
        has_chinese_subtitle = bool(subtitle_validation.get("ok"))
        if not subtitle_validation.get("ok") and bool(task.get("needs_subtitle")):
            subtitle_failure = {
                "error_code": "subtitle_validation_failed",
                "message": str(subtitle_validation.get("reason") or "字幕校验失败"),
                "validation": subtitle_validation,
            }
            post.add_event(task_id, "error", "subtitle_validation_failed", "字幕校验失败，视频成品继续进入版本链", subtitle_failure)
        if subtitle_validation.get("ok"):
            managed_subtitle_path = ensure_managed_subtitle_product(subtitle_path, product_path)
            if managed_subtitle_path != subtitle_path:
                subtitle_path = managed_subtitle_path
                subtitle_validation = validate_subtitle_output(subtitle_path, video_path=product_path)
                post.add_event(task_id, "info", "subtitle_managed", "中文字幕已复制到最终视频目录", subtitle_validation)
            managed_vtt_path = ensure_managed_vtt_product(worker_result, product_path)
            if managed_vtt_path:
                post.add_event(task_id, "info", "subtitle_vtt_managed", "VTT 字幕已整理到最终视频目录", {"path": managed_vtt_path})
            cleanup_payload = cleanup_postprocess_subtitle_artifacts(
                product_path,
                worker_result,
                [subtitle_path, managed_vtt_path],
            )
            if cleanup_payload.get("moved"):
                post.add_event(task_id, "info", "subtitle_artifacts_cleaned", "字幕中间文件已移动到 trash", cleanup_payload)
    source_type = str(task.get("task_type") or "subscription")
    version_codec = (
        str(task.get("target_codec") or settings_payload.get("target_codec") or "")
        if bool(settings_payload.get("auto_transcode_enabled"))
        else str(validation.get("codec_name") or "")
    )
    version = post.add_version(
        av_id=str(task.get("av_id") or ""),
        path=product_path,
        source_type=source_type,
        codec=version_codec,
        has_chinese_subtitle=has_chinese_subtitle,
        status="ready",
        generated_by="moviemuse",
        file_size=int(validation.get("file_size") or 0),
        mtime=float(validation.get("mtime") or 0),
        metadata={
            "task_id": task_id,
            "validation": validation,
            "subtitle_validation": subtitle_validation or {},
            "subtitle_failure": subtitle_failure,
            "metadata_sidecars": metadata_sidecars,
        },
    )
    trash_payload: dict[str, Any] | None = None
    old_version_trash_failure: dict[str, Any] | None = None
    supersede_version_id = str(task.get("supersede_version_id") or "")
    old_version: dict[str, Any] | None = None
    if supersede_version_id:
        old_version = post.get_version(supersede_version_id)
        if not old_version:
            post.update_task(task_id, status="conflict", error_code="supersede_missing", error_message="洗版绑定的旧版本不存在")
            post.update_version(version["id"], status="failed", metadata={"activation_conflict": {"status": "conflict", "message": "洗版绑定的旧版本不存在"}})
            return {"status": "conflict", "message": "洗版绑定的旧版本不存在"}
        current_active = post.active_version(str(task.get("av_id") or ""))
        if not current_active or str(current_active.get("id") or "") != supersede_version_id:
            message = "active version 已变化，拒绝移动旧版本"
            post.update_task(task_id, status="conflict", error_code="active_version_changed", error_message=message)
            post.update_version(version["id"], status="failed", metadata={
                "activation_conflict": {
                    "status": "conflict",
                    "message": message,
                    "expected_active_version_id": supersede_version_id,
                    "current_active_version_id": (current_active or {}).get("id", ""),
                }
            })
            post.add_event(task_id, "error", "old_version_trashing", message, {
                "expected_active_version_id": supersede_version_id,
                "current_active_version_id": (current_active or {}).get("id", ""),
            })
            return {"status": "conflict", "message": message}
        try:
            validate_managed_version_trashable(old_version, settings_payload)
        except Exception as exc:
            post.update_task(task_id, status="failed", error_code="old_version_trash_failed", error_message=str(exc))
            post.update_version(version["id"], status="failed", metadata={
                "activation_conflict": {
                    "status": "failed",
                    "message": str(exc),
                    "error_code": "old_version_trash_failed",
                }
            })
            post.add_event(task_id, "error", "old_version_trashing", "旧 active version 回收预检失败", {"error": str(exc), "version": old_version})
            return {"status": "failed", "message": str(exc), "version": version}
    activation = post.activate_version(
        version["id"],
        supersede_version_id=supersede_version_id,
    )
    if activation.get("status") != "activated":
        post.update_version(version["id"], status="failed", metadata={"activation_conflict": activation})
        post.update_task(task_id, status="conflict", error_code="version_activate_conflict", error_message=str(activation.get("message") or "版本激活冲突"))
        post.add_event(task_id, "error", "version_activating", "版本激活失败", activation)
        return activation
    if supersede_version_id and old_version:
        try:
            trash_payload = move_managed_version_to_trash(old_version, settings_payload)
            post.update_version(
                supersede_version_id,
                status="trashed",
                trashed_at=time.time(),
                metadata={
                    "trashed_path": str((trash_payload or {}).get("target") or ""),
                    "trash_result": trash_payload,
                },
            )
            post.add_event(task_id, "info", "old_version_trashing", "旧 active version 已移动到 trash", {
                "version_id": supersede_version_id,
                "trash": trash_payload,
            })
        except Exception as exc:
            old_version_trash_failure = {
                "error_code": "old_version_trash_failed",
                "message": str(exc),
                "version_id": supersede_version_id,
                "path": old_version.get("path", ""),
            }
            post.add_event(task_id, "error", "old_version_trashing", "旧 active version 移动到 trash 失败，新版本保持激活", old_version_trash_failure)
    source_trash_payload: dict[str, Any] | None = None
    source_trash_failure: dict[str, Any] | None = None
    if source_type == "external_qb" and not bool(settings_payload.get("external_qb_trash_source_enabled")):
        source_trash_payload = {
            "status": "skipped",
            "reason": "外部 qB 接管任务默认保留源文件，避免影响做种",
            "input_path": task.get("input_path", ""),
        }
        post.add_event(task_id, "info", "source_trashing_skipped", "外部 qB 接管任务已跳过源文件清理", source_trash_payload)
    else:
        try:
            source_trash_payload = move_postprocess_source_to_trash(task, product_path, settings_payload)
            if source_trash_payload:
                post.add_event(task_id, "info", "source_trashing", "后处理源文件已移动到 trash", source_trash_payload)
        except Exception as exc:
            source_trash_failure = {
                "error_code": "source_trash_failed",
                "message": str(exc),
                "input_path": task.get("input_path", ""),
            }
            source_trash_payload = {"status": "failed", **source_trash_failure}
            post.add_event(task_id, "error", "source_trashing", "后处理源文件移动到 trash 失败，版本保持激活", source_trash_failure)
    post.update_task(task_id, status="jellyfin_refreshing", output_path=product_path, error_code="", error_message="", data={"version_id": version["id"], "activation": activation})
    jellyfin_refresh = refresh_jellyfin_library(get_system_settings_service().get().get("jellyfin", {}))
    post.add_event(
        task_id,
        "info" if jellyfin_refresh.get("status") in {"ok", "skipped"} else "error",
        "jellyfin_refreshing",
        "Jellyfin 媒体库刷新已处理",
        jellyfin_refresh,
    )
    completion_warnings = [warning for warning in (subtitle_failure, old_version_trash_failure, source_trash_failure) if warning]
    completion_error_code = ";".join(str(item.get("error_code") or "warning") for item in completion_warnings)
    completion_error_message = "；".join(str(item.get("message") or item.get("reason") or item.get("error_code") or "后处理告警") for item in completion_warnings)
    completion_message = "后处理任务已完成并激活版本，有告警" if completion_warnings else "后处理任务已完成并激活版本"
    post.update_task(
        task_id,
        status="completed",
        output_path=product_path,
        error_code=completion_error_code,
        error_message=completion_error_message,
        data={
            "jellyfin_refresh": jellyfin_refresh,
            "source_trash": source_trash_payload,
            "subtitle_failure": subtitle_failure,
            "old_version_trash_failure": old_version_trash_failure,
            "source_trash_failure": source_trash_failure,
            "metadata_sidecars": metadata_sidecars,
        },
    )
    post.add_event(task_id, "info", "completed", completion_message, {
        "version_id": version["id"],
        "output_path": product_path,
        "trash": trash_payload,
        "source_trash": source_trash_payload,
        "jellyfin_refresh": jellyfin_refresh,
        "subtitle_failure": subtitle_failure,
        "old_version_trash_failure": old_version_trash_failure,
        "source_trash_failure": source_trash_failure,
        "metadata_sidecars": metadata_sidecars,
    })
    if subtitle_failure and source_trash_failure:
        user_message = "后处理完成，字幕失败，源文件清理失败"
    elif subtitle_failure:
        user_message = "后处理完成，字幕失败"
    elif old_version_trash_failure:
        user_message = "后处理完成，旧版本清理失败"
    elif source_trash_failure:
        user_message = "后处理完成，源文件清理失败"
    else:
        user_message = "后处理完成"
    if source_type.startswith("wash_"):
        mode = "4k" if source_type == "wash_4k" else "chinese"
        get_subscription_service().update_av_wash(str(task.get("av_id") or ""), {
            "mode": mode,
            "status": "completed",
            "download_status": "completed",
            "download_message": user_message if subtitle_failure else "洗版后处理完成",
            "new_path": product_path,
            "task_id": task_id,
        })
    elif source_type == "subscription":
        get_subscription_service().update_av_download(str(task.get("av_id") or ""), {
            "status": "done",
            "download_status": "completed",
            "download_message": user_message,
            "downloaded_at": time.time(),
        })
    av_id_for_notice = str(task.get("av_id") or "")
    subscription_for_notice = next(
        (item for item in get_subscription_service().get_subscribed_av() if str(item.get("id") or "") == av_id_for_notice),
        {},
    )
    send_notification_event("jellyfin_in_library", {
        "status": "completed",
        "title": str(subscription_for_notice.get("title") or av_id_for_notice or task_id),
        "detail": f"{av_id_for_notice or task_id} 已完成后处理并刷新 Jellyfin",
        "av_id": av_id_for_notice,
        "task_id": task_id,
        "path": product_path,
        "file_name": notification_filename(product_path, av_id_for_notice or task_id),
        "save_path": notification_parent_path(product_path, ""),
        "cover": subscription_for_notice.get("cover") or subscription_for_notice.get("cover_url") or "",
    })
    if bool(task.get("needs_subtitle")):
        if subtitle_failure:
            send_notification_event("subtitle_failed", {
                "status": "failed",
                "title": f"字幕任务失败：{task.get('av_id') or task_id}",
                "detail": str(subtitle_failure.get("message") or subtitle_failure.get("reason") or "字幕阶段失败"),
                "av_id": task.get("av_id", ""),
                "task_id": task_id,
                "path": product_path,
            })
        elif has_chinese_subtitle:
            send_notification_event("subtitle_completed", {
                "status": "completed",
                "title": f"字幕任务完成：{task.get('av_id') or task_id}",
                "detail": f"中文字幕已生成并通过校验：{subtitle_path}",
                "av_id": task.get("av_id", ""),
                "task_id": task_id,
                "path": subtitle_path,
            })
    return {
        "status": "completed",
        "version": post.get_version(version["id"]),
        "activation": activation,
        "trash": trash_payload,
        "source_trash": source_trash_payload,
        "subtitle_failure": subtitle_failure,
        "old_version_trash_failure": old_version_trash_failure,
        "source_trash_failure": source_trash_failure,
    }


def poll_subtitle_postprocess_once() -> dict[str, Any]:
    post = get_postprocess_service()
    tasks = post.list_tasks(statuses=["subtitle_processing"], limit=200)
    results: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("id") or "")
        job_id = str((task.get("data") or {}).get("subtitle_job_id") or "")
        if not job_id:
            result = validate_and_activate_postprocess_task(
                task_id,
                output_path=str(task.get("output_path") or ""),
                subtitle_error={"error_code": "subtitle_job_missing", "message": "字幕阶段缺少 job_id"},
                worker_result={"subtitle_job_id": job_id},
            )
            results.append({"task_id": task_id, "status": result.get("status", "completed"), "reason": "subtitle_job_missing"})
            continue
        try:
            job = subtitle_job_status(job_id)
        except Exception as exc:
            post.add_event(task_id, "error", "subtitle_poll_failed", "字幕任务状态轮询失败", {"subtitle_job_id": job_id, "error": str(exc)})
            results.append({"task_id": task_id, "status": "poll_error", "error": str(exc)})
            continue
        if not job:
            result = validate_and_activate_postprocess_task(
                task_id,
                output_path=str(task.get("output_path") or ""),
                subtitle_error={"error_code": "subtitle_job_not_found", "message": "算力端找不到字幕任务", "subtitle_job_id": job_id},
                worker_result={"subtitle_job_id": job_id},
            )
            results.append({"task_id": task_id, "status": result.get("status", "completed"), "reason": "subtitle_job_not_found"})
            continue
        status = str(job.get("status") or "")
        post.update_task(task_id, data={"subtitle_status": job})
        if status in {"queued", "running", "translating"}:
            results.append({"task_id": task_id, "status": status, "subtitle_job_id": job_id})
            continue
        if status == "failed":
            message = str(job.get("error") or job.get("message") or "字幕任务失败")
            result = validate_and_activate_postprocess_task(
                task_id,
                output_path=str(task.get("output_path") or ""),
                subtitle_error={"error_code": "subtitle_failed", "message": message, "subtitle_job_id": job_id, "job": job},
                worker_result={"subtitle_job": job},
            )
            results.append({"task_id": task_id, "status": result.get("status", "completed"), "error": message})
            continue
        if status == "completed":
            subtitle_path = pick_subtitle_output(job)
            if not subtitle_path:
                result = validate_and_activate_postprocess_task(
                    task_id,
                    output_path=str(task.get("output_path") or ""),
                    subtitle_error={"error_code": "subtitle_output_missing", "message": "字幕任务完成但没有输出字幕路径", "subtitle_job_id": job_id, "job": job},
                    worker_result={"subtitle_job": job},
                )
                results.append({"task_id": task_id, "status": result.get("status", "completed"), "reason": "subtitle_output_missing"})
                continue
            result = validate_and_activate_postprocess_task(
                task_id,
                output_path=str(task.get("output_path") or job.get("video_path") or ""),
                subtitle_path=subtitle_path,
                worker_result={"subtitle_job": job},
            )
            results.append({"task_id": task_id, "status": result.get("status"), "subtitle_job_id": job_id})
            continue
        results.append({"task_id": task_id, "status": status or "unknown", "subtitle_job_id": job_id})
    return {"checked": len(tasks), "results": results}


def sync_completed_wash_postprocess_tasks(limit: int = 200) -> dict[str, Any]:
    post = get_postprocess_service()
    service = get_subscription_service()
    subscriptions = {
        str(item.get("id") or ""): item
        for item in service.get_subscribed_av()
        if isinstance(item, dict)
    }
    checked = 0
    synced: list[dict[str, str]] = []
    for task in post.list_tasks(statuses=["completed"], limit=limit):
        mode = wash_mode_from_task_type(str(task.get("task_type") or ""))
        av_id = str(task.get("av_id") or "")
        if not mode or not av_id:
            continue
        checked += 1
        output_path = str(task.get("output_path") or "")
        wash = subscriptions.get(av_id, {}).get("wash")
        wash = wash if isinstance(wash, dict) else {}
        if wash.get("status") == "completed" and str(wash.get("new_path") or "") == output_path:
            continue
        updated = service.update_av_wash(av_id, {
            "mode": mode,
            "status": "completed",
            "download_status": "completed",
            "download_message": "洗版后处理完成",
            "new_path": output_path,
            "task_id": str(task.get("id") or ""),
            "qb_hash": str(task.get("torrent_hash") or ""),
        })
        if updated:
            post.add_event(str(task.get("id") or ""), "info", "wash_status_synced", "洗版订阅状态已同步为完成", {
                "av_id": av_id,
                "mode": mode,
                "new_path": output_path,
            })
            synced.append({"task_id": str(task.get("id") or ""), "av_id": av_id, "mode": mode})
    return {"checked": checked, "synced": synced}


def refresh_worker_queue_readiness(worker_status: dict[str, Any] | None = None) -> dict[str, Any]:
    post = get_postprocess_service()
    status_payload = worker_status or subtitle_backend_status()
    online = bool(status_payload.get("online") or status_payload.get("status") == "ok")
    waiting = post.list_tasks(statuses=["waiting_worker"], limit=200)
    promoted: list[str] = []
    if online:
        for task in waiting:
            task_id = str(task.get("id") or "")
            held_item = hold_postprocess_task_missing_input(task)
            if held_item:
                continue
            post.update_task(task_id, status="ready_to_run", error_code="", error_message="")
            post.add_event(task_id, "info", "worker_ready", "算力端在线，任务已进入可执行队列", {"worker_status": status_payload})
            promoted.append(task_id)
    return {"online": online, "checked": len(waiting), "promoted": promoted, "worker_status": status_payload}


def poll_postprocess_once() -> dict[str, Any]:
    qb_result = poll_qb_postprocess_once()
    subtitle_result = poll_subtitle_postprocess_once()
    wash_sync_result = sync_completed_wash_postprocess_tasks()
    queue_result: dict[str, Any] | None = None
    post = get_postprocess_service()
    post_settings = post.get_settings()
    worker_status = subtitle_backend_status()
    recovered_finished = recover_finished_worker_jobs(worker_status)
    recovered_missing = recover_missing_worker_jobs(worker_status) if bool(worker_status.get("online") or worker_status.get("status") == "ok") else []
    worker_queue = refresh_worker_queue_readiness(worker_status)
    if bool(post_settings.get("worker_auto_run")):
        candidates = post.list_tasks(statuses=["waiting_worker", "ready_to_run"], limit=1, order="asc")
        if candidates:
            queue_result = run_postprocess_queue()
            app_log("info", "postprocess", "后处理队列自动执行已处理", {
                "stage": "postprocess_queue_auto_run",
                "status": queue_result.get("status"),
                "updated": queue_result.get("updated"),
            })
    return {
        "qb": qb_result,
        "subtitle": subtitle_result,
        "wash_sync": wash_sync_result,
        "worker_finished_recovery": recovered_finished,
        "worker_missing_recovery": recovered_missing,
        "worker_queue": worker_queue,
        "queue_auto_run": queue_result,
    }


def torrent_info_hash(torrent_bytes: bytes) -> str:
    try:
        parser = BencodeParser(torrent_bytes)
        root = parser.parse()
        if not isinstance(root, dict) or b"info" not in root:
            return ""
        return hashlib.sha1(bencode(root[b"info"])).hexdigest()
    except Exception:
        return ""


class BencodeParser:
    def __init__(self, data: bytes):
        self.data = data
        self.index = 0

    def parse(self) -> Any:
        token = self.data[self.index:self.index + 1]
        if token == b"d":
            self.index += 1
            result: dict[bytes, Any] = {}
            while self.data[self.index:self.index + 1] != b"e":
                key = self.parse()
                result[key] = self.parse()
            self.index += 1
            return result
        if token == b"l":
            self.index += 1
            result: list[Any] = []
            while self.data[self.index:self.index + 1] != b"e":
                result.append(self.parse())
            self.index += 1
            return result
        if token == b"i":
            self.index += 1
            end = self.data.index(b"e", self.index)
            value = int(self.data[self.index:end])
            self.index = end + 1
            return value
        sep = self.data.index(b":", self.index)
        length = int(self.data[self.index:sep])
        self.index = sep + 1
        value = self.data[self.index:self.index + length]
        self.index += length
        return value


def bencode(value: Any) -> bytes:
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    raw = str(value).encode("utf-8")
    return str(len(raw)).encode("ascii") + b":" + raw


def normalize_media_path(value: str) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").lower()


def normalized_media_path_is_under(path_value: str, root_value: str) -> bool:
    path_norm = normalize_media_path(path_value)
    root_norm = normalize_media_path(root_value)
    if not root_norm:
        return True
    return path_norm == root_norm or path_norm.startswith(root_norm + "/")


def find_jellyfin_match(av_id: str, title: str, config: dict[str, Any]) -> dict[str, str] | None:
    matches = find_jellyfin_matches(av_id, title, config)
    return matches[0] if matches else None


def refresh_jellyfin_library(config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    if not base_url or not api_key:
        return {"status": "skipped", "message": "未配置 Jellyfin URL 或 API Key"}
    headers = {"X-Emby-Token": api_key}
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            response = client.post(f"{base_url}/Library/Refresh", headers=headers)
            response.raise_for_status()
        return {"status": "ok", "message": "Jellyfin 媒体库刷新已触发"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def find_jellyfin_matches(av_id: str, title: str, config: dict[str, Any]) -> list[dict[str, str]]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    if not base_url or not api_key or not av_id:
        return []
    user_id = get_jellyfin_user_id(config)
    search_terms = [av_id]
    if title and title != av_id:
        search_terms.append(title)
    headers = {"X-Emby-Token": api_key}
    library_id = str(config.get("library_id") or "").strip()
    matches: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        with httpx.Client(timeout=12, follow_redirects=True) as client:
            for term in search_terms:
                path = f"/Users/{user_id}/Items" if user_id else "/Items"
                params = {
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Video,Episode",
                    "SearchTerm": term,
                    "Limit": "20",
                    "Fields": "Path,ProviderIds",
                }
                if library_id:
                    params["ParentId"] = library_id
                resp = client.get(
                    f"{base_url}{path}",
                    headers=headers,
                    params=params,
                )
                resp.raise_for_status()
                for item in resp.json().get("Items", []):
                    name = str(item.get("Name") or "")
                    path_value = str(item.get("Path") or "")
                    haystack = f"{name} {path_value}".lower()
                    if av_id.lower() in haystack:
                        item_id = str(item.get("Id") or "")
                        key = item_id or normalize_media_path(path_value)
                        if key and key not in seen:
                            seen.add(key)
                            matches.append({"id": item_id, "name": name, "path": path_value})
    except (httpx.HTTPError, ValueError):
        return []
    return matches


def get_jellyfin_libraries(config: dict[str, Any]) -> list[dict[str, str]]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    if not base_url or not api_key:
        return []
    user_id = get_jellyfin_user_id(config)
    headers = {"X-Emby-Token": api_key}
    params = {"IncludeItemTypes": "CollectionFolder", "Recursive": "false"}
    try:
        with httpx.Client(timeout=12, follow_redirects=True) as client:
            paths = [f"/Users/{user_id}/Items"] if user_id else []
            paths.append("/Items")
            for path in paths:
                resp = client.get(f"{base_url}{path}", headers=headers, params=params)
                resp.raise_for_status()
                items = resp.json().get("Items", [])
                libraries = [
                    {
                        "id": str(item.get("Id") or ""),
                        "name": str(item.get("Name") or ""),
                        "type": str(item.get("CollectionType") or item.get("Type") or ""),
                    }
                    for item in items
                    if item.get("Id") and item.get("Name")
                ]
                if libraries:
                    return libraries
    except (httpx.HTTPError, ValueError):
        return []
    return []


def get_jellyfin_user_id(config: dict[str, Any]) -> str:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    username = str(config.get("username") or "").strip()
    if not base_url or not api_key or not username:
        return ""
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.get(f"{base_url}/Users", headers={"X-Emby-Token": api_key})
            resp.raise_for_status()
            for user in resp.json():
                if str(user.get("Name") or "").lower() == username.lower():
                    return str(user.get("Id") or "")
    except (httpx.HTTPError, ValueError):
        return ""
    return ""


def jellyfin_config() -> dict[str, Any]:
    settings_data = get_system_settings_service().get()
    config = settings_data.get("jellyfin", {})
    return config if isinstance(config, dict) else {}


def jellyfin_auth_headers(config: dict[str, Any]) -> dict[str, str]:
    api_key = str(config.get("api_key") or "").strip()
    return {"X-Emby-Token": api_key} if api_key else {}


def jellyfin_media_source(item: dict[str, Any], media_source_id: str = "") -> dict[str, Any]:
    sources = item.get("MediaSources") if isinstance(item.get("MediaSources"), list) else []
    wanted = str(media_source_id or "").strip()
    if wanted:
        for source in sources:
            if isinstance(source, dict) and str(source.get("Id") or "") == wanted:
                return source
    for source in sources:
        if isinstance(source, dict) and str(source.get("Path") or "").strip():
            return source
    return sources[0] if sources and isinstance(sources[0], dict) else {}


def jellyfin_source_resolution(source: dict[str, Any]) -> str:
    streams = source.get("MediaStreams") if isinstance(source.get("MediaStreams"), list) else []
    video = next((item for item in streams if isinstance(item, dict) and str(item.get("Type") or "").lower() == "video"), {})
    width = int(video.get("Width") or source.get("Width") or 0) if isinstance(video, dict) else int(source.get("Width") or 0)
    height = int(video.get("Height") or source.get("Height") or 0) if isinstance(video, dict) else int(source.get("Height") or 0)
    if width and height:
        return f"{width}x{height}"
    return ""


def fetch_jellyfin_item(item_id: str, config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    clean_item_id = str(item_id or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin 地址")
    if not api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")
    if not clean_item_id:
        raise HTTPException(status_code=400, detail="缺少 Jellyfin item id")
    user_id = get_jellyfin_user_id(config)
    paths = [f"/Users/{user_id}/Items/{clean_item_id}"] if user_id else []
    paths.append(f"/Items/{clean_item_id}")
    params = {"Fields": "Path,People,MediaSources,MediaStreams,ProviderIds,Overview"}
    last_error = ""
    with httpx.Client(timeout=12, follow_redirects=True) as client:
        for path in paths:
            try:
                response = client.get(f"{base_url}{path}", headers=jellyfin_auth_headers(config), params=params)
                if response.status_code == 404:
                    last_error = "Jellyfin 未找到该媒体"
                    continue
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    return data
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code == 401:
                    raise HTTPException(status_code=502, detail="Jellyfin API Key 未授权或已失效") from exc
                last_error = f"Jellyfin 返回 HTTP {status_code}"
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
    raise HTTPException(status_code=404, detail=last_error or "Jellyfin 媒体解析失败")


def jellyfin_subscription_match(payload: JellyfinIntegrationRequest) -> dict[str, Any] | None:
    item_id = str(payload.item_id or "").strip()
    title = str(payload.title or "").strip()
    path = str(payload.path or "").strip()
    detected_av_id = canonical_av_id(detect_catalog_number(title or path))
    for av in get_subscription_service().get_subscribed_av():
        av_id = canonical_av_id(av.get("id") or "")
        jellyfin_item_id = str(av.get("jellyfin_item_id") or "").strip()
        jellyfin_path = str(av.get("jellyfin_path") or "").strip()
        wash = av.get("wash") if isinstance(av.get("wash"), dict) else {}
        wash_item_id = str(wash.get("new_jellyfin_item_id") or "").strip()
        wash_path = str(wash.get("new_path") or "").strip()
        matched_by = ""
        if item_id and item_id in {jellyfin_item_id, wash_item_id}:
            matched_by = "item_id"
        elif path and any(normalize_media_path(path) == normalize_media_path(candidate) for candidate in (jellyfin_path, wash_path) if candidate):
            matched_by = "path"
        elif detected_av_id and detected_av_id == av_id and jellyfin_path:
            matched_by = "av_id"
        if not matched_by:
            continue
        resolved_path = wash_path if item_id and item_id == wash_item_id and wash_path else jellyfin_path or wash_path
        if not resolved_path:
            continue
        return {
            "status": "ok",
            "source": "subscription_db",
            "matched_by": matched_by,
            "item_id": jellyfin_item_id or wash_item_id or item_id,
            "media_source_id": str(payload.media_source_id or ""),
            "title": str(av.get("title") or payload.title or Path(resolved_path).stem),
            "path": resolved_path,
            "size": 0,
            "resolution": "",
            "type": "Movie",
            "av_id": av_id,
            "library_status": str(av.get("library_status") or av.get("status") or ""),
        }
    return None


def resolve_jellyfin_media(payload: JellyfinIntegrationRequest) -> dict[str, Any]:
    config = jellyfin_config()
    direct_path = str(payload.path or "").strip()
    if direct_path:
        title = str(payload.title or Path(direct_path).stem).strip()
        return {
            "status": "ok",
            "source": "provided_path",
            "item_id": str(payload.item_id or ""),
            "media_source_id": str(payload.media_source_id or ""),
            "title": title,
            "path": direct_path,
            "size": 0,
            "resolution": "",
        }

    subscription_match = jellyfin_subscription_match(payload)
    if subscription_match:
        return subscription_match

    item = fetch_jellyfin_item(payload.item_id, config)
    source = jellyfin_media_source(item, payload.media_source_id)
    path = str(source.get("Path") or item.get("Path") or "").strip()
    title = str(payload.title or item.get("Name") or item.get("OriginalTitle") or "").strip()
    if not path:
        raise HTTPException(status_code=404, detail="Jellyfin 返回的媒体没有可用文件路径")
    return {
        "status": "ok",
        "source": "jellyfin",
        "item_id": str(item.get("Id") or payload.item_id or ""),
        "media_source_id": str(source.get("Id") or payload.media_source_id or ""),
        "title": title or Path(path).stem,
        "path": path,
        "size": int(source.get("Size") or 0),
        "resolution": jellyfin_source_resolution(source),
        "type": str(item.get("Type") or ""),
    }


def jellyfin_task_av_id(resolved: dict[str, Any]) -> str:
    for candidate in (resolved.get("title"), resolved.get("path")):
        detected = detect_catalog_number(str(candidate or ""))
        if detected:
            return canonical_av_id(detected)
    item_id = re.sub(r"[^A-Za-z0-9]+", "", str(resolved.get("item_id") or ""))[:12].upper()
    return f"JELLYFIN-{item_id or uuid.uuid4().hex[:8].upper()}"


def submit_jellyfin_transcode_job(resolved: dict[str, Any], target_codec: str | None = None) -> dict[str, object]:
    path = str(resolved.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="缺少媒体文件路径")
    post_settings = get_postprocess_service().get_settings()
    av_id = jellyfin_task_av_id(resolved)
    transcode_settings = normalize_transcode_settings_payload({
        **post_settings,
        "target_codec": target_codec or post_settings.get("target_codec"),
    })
    task_like = {
        "id": uuid.uuid4().hex,
        "av_id": av_id,
        "task_type": "jellyfin",
        "input_path": path,
    }
    output_path = avoid_output_conflict(build_postprocess_output_path(task_like, post_settings), str(task_like["id"]))
    job_payload_data = {
        "task_id": str(task_like["id"]),
        "av_id": av_id,
        "input_path": rewrite_proxy_path(path) if backend_url() else path,
        "output_path": rewrite_proxy_path(output_path) if backend_url() else output_path,
        "console_input_path": path,
        "console_output_path": output_path,
        "target_codec": transcode_settings.get("target_codec"),
        "target_encoder": transcode_settings.get("target_encoder"),
        "crf": transcode_settings.get("crf"),
        "preset": transcode_settings.get("preset"),
        "preset_flag": transcode_settings.get("preset_flag"),
        "ffmpeg_mode": transcode_settings.get("ffmpeg_mode"),
        "ffmpeg_standard_enabled": transcode_settings.get("ffmpeg_standard_enabled"),
        "ffmpeg_custom_enabled": transcode_settings.get("ffmpeg_custom_enabled"),
        "ffmpeg_custom_template": transcode_settings.get("ffmpeg_custom_template"),
        "ffmpeg_standard_command": build_ffmpeg_preview(transcode_settings),
        "source": "jellyfin",
        "jellyfin_item_id": resolved.get("item_id", ""),
        "jellyfin_title": resolved.get("title", ""),
    }
    if backend_url():
        result = remote_post_json("/api/transcode/jobs", job_payload_data, timeout=60)
        job = result.get("job") if isinstance(result.get("job"), dict) else result
        job_id = str(result.get("job_id") or (job.get("id") if isinstance(job, dict) else ""))
    else:
        job = create_transcode_job(job_payload_data)
        job_id = str(job.get("id") or "")
        result = {"status": "queued", "job_id": job_id, "job": job}
    return {
        "status": "queued",
        "job_id": job_id,
        "av_id": av_id,
        "input_path": path,
        "output_path": output_path,
        "resolved": resolved,
        "result": result,
    }


@app.on_event("startup")
def start_subscription_polling() -> None:
    global subscription_poll_thread
    apply_system_proxy_settings()
    if subscription_poll_thread is None:
        subscription_poll_thread = threading.Thread(target=subscription_poll_loop, name="subscription-poll", daemon=True)
        subscription_poll_thread.start()


@app.on_event("shutdown")
def stop_subscription_polling() -> None:
    subscription_poll_stop.set()


@app.get("/subscriptions", response_class=HTMLResponse)
def subscriptions_page(request: Request, legacy: int = 0) -> Response:
    """订阅管理页面"""
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


@app.get("/subscription-search", response_class=HTMLResponse)
def subscription_search_page(request: Request, legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


@app.get("/settings", response_class=HTMLResponse)
def legacy_settings_page() -> RedirectResponse:
    return RedirectResponse("/subscription-settings", status_code=307)


@app.get("/subscription-settings", response_class=HTMLResponse)
def settings_page(request: Request, legacy: int = 0) -> Response:
    """订阅设置页面"""
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


@app.get("/subscription-wash", response_class=HTMLResponse)
def subscription_wash_page(request: Request, legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request, legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


@app.get("/makers", response_class=HTMLResponse)
def makers_page(request: Request, legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


@app.get("/rankings", response_class=HTMLResponse)
def rankings_page(request: Request, legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


@app.get("/subscription-tasks", response_class=HTMLResponse)
def subscription_tasks_page(request: Request, legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


def postprocess_page_payload() -> dict[str, Any]:
    post = get_postprocess_service()
    tasks = post.list_tasks(limit=200)
    qb_rows = {row["task_id"]: row for row in post.list_qb_torrents(limit=500)}
    settings_payload = post.get_settings()
    worker_status = subtitle_backend_status()
    _, _, data_dir = settings()
    compute_config = load_compute_config(data_dir)
    waiting_count = sum(1 for task in tasks if task.get("status") == "waiting_worker")
    ready_count = sum(1 for task in tasks if task.get("status") == "ready_to_run")
    transcode_running_statuses = {
        "sent_to_worker",
        "transcoding",
        "worker_done",
        "transcode_validating",
        "subtitle_processing",
        "subtitle_validating",
        "downloading",
    }
    transcode_waiting_statuses = {"waiting_worker", "ready_to_run", "created"}
    transcode_failed_statuses = {"failed", "ignored", "conflict", "expired"}
    return {
        "tasks": tasks,
        "qb_torrents": qb_rows,
        "postprocess_settings": settings_payload,
        "compute_config": compute_config,
        "worker_status": worker_status,
        "waiting_count": waiting_count,
        "ready_count": ready_count,
        "transcode_waiting_count": sum(1 for task in tasks if str(task.get("status") or "") in transcode_waiting_statuses),
        "transcode_running_count": sum(1 for task in tasks if str(task.get("status") or "") in transcode_running_statuses),
        "transcode_failed_count": sum(1 for task in tasks if str(task.get("status") or "") in transcode_failed_statuses),
        "transcode_completed_count": sum(1 for task in tasks if str(task.get("status") or "") == "completed"),
    }


@app.get("/transcode", response_class=HTMLResponse)
def transcode_page(request: Request) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running. Use the Unraid console to manage transcode jobs.", media_type="text/plain")
    return RedirectResponse("/subtitles", status_code=303)


@app.get("/transcode-settings", response_class=HTMLResponse)
def transcode_settings_page(request: Request) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running. Use the Unraid console to manage transcode settings.", media_type="text/plain")
    return RedirectResponse("/transcode", status_code=303)


@app.get("/automation", response_class=HTMLResponse)
def automation_page(request: Request, legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running. Use the Unraid console to manage automation settings.", media_type="text/plain")
    return frontend_app_response()


@app.get("/api/postprocess/tasks")
def api_postprocess_tasks(status: str | None = None, limit: int = 200) -> dict[str, object]:
    statuses = [item.strip() for item in str(status or "").split(",") if item.strip()]
    post = get_postprocess_service()
    worker_status = subtitle_backend_status()
    tasks = post.list_tasks(statuses=statuses or None, limit=limit)
    qb_rows = post.list_qb_torrents(limit=500)
    return {
        "tasks": tasks,
        "qb_torrents": qb_rows,
        "settings": post.get_settings(),
        "worker_status": worker_status,
        "recovered_finished": [],
    }


@app.get("/api/postprocess/tasks/{task_id}/events")
def api_postprocess_task_events(task_id: str, limit: int = 200) -> dict[str, object]:
    post = get_postprocess_service()
    task = post.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="后处理任务不存在")
    return {"task": task, "events": post.list_events(task_id, limit=limit)}


@app.post("/api/postprocess/tasks/{task_id}/run")
def api_run_postprocess_task(task_id: str) -> dict[str, object]:
    post = get_postprocess_service()
    task = post.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="后处理任务不存在")
    status = str(task.get("status") or "")
    if status not in RUNNABLE_POSTPROCESS_STATUSES:
        return {"status": "skipped", "reason": "not_runnable", "task": task}
    settings_payload = post.get_settings()
    if postprocess_task_needs_worker(task, settings_payload) and worker_is_offline():
        updated = post.update_task(task_id, status="waiting_worker", error_code="", error_message="")
        post.add_event(task_id, "info", "worker_offline", "算力端离线，单任务执行已保留在等待队列")
        return {"status": "waiting_worker", "task": updated}
    claimed = claim_postprocess_task_for_dispatch(task)
    if not claimed:
        return {"status": "skipped", "reason": "status_changed", "task": post.get_task(task_id)}
    return dispatch_postprocess_task(claimed)


def reset_postprocess_task_for_retry(task: dict[str, Any]) -> str:
    post = get_postprocess_service()
    task_id = str(task.get("id") or "")
    input_path = str(task.get("input_path") or "").strip()
    torrent_hash = str(task.get("torrent_hash") or "").strip()
    if input_path:
        post.update_task(task_id, status="ready_to_run", error_code="", error_message="")
        return "ready_to_run"
    if torrent_hash:
        qb_row = post.get_qb_torrent(torrent_hash)
        qb_progress = float((qb_row or {}).get("progress") or 0)
        qb_status = "downloading" if qb_progress > 0 else "torrent_pushed"
        post.update_qb_torrent(torrent_hash, status=qb_status)
        post.update_task(task_id, status=qb_status, error_code="", error_message="")
        post.add_event(task_id, "info", "retry_waiting_qb", "任务缺少输入文件，已恢复 qB 轮询等待回写路径", {
            "torrent_hash": torrent_hash,
            "qb_status": qb_status,
            "qb_progress": qb_progress,
        })
        return qb_status
    post.update_task(task_id, status="ready_to_run", error_code="", error_message="")
    return "ready_to_run"


@app.post("/api/postprocess/tasks/{task_id}/retry")
def api_retry_postprocess_task(task_id: str) -> dict[str, object]:
    post = get_postprocess_service()
    task = post.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="后处理任务不存在")
    next_status = reset_postprocess_task_for_retry(task)
    updated = post.get_task(task_id) or post.update_task(task_id, status=next_status, error_code="", error_message="")
    post.add_event(task_id, "info", "task_retry", "用户手动重试后处理任务", {
        "previous_status": task.get("status", ""),
        "next_status": next_status,
    })
    return {"status": next_status, "task": updated}


@app.post("/api/postprocess/tasks/{task_id}/cancel")
def api_cancel_postprocess_task(task_id: str) -> dict[str, object]:
    post = get_postprocess_service()
    task = post.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="后处理任务不存在")
    if task.get("status") == "completed":
        raise HTTPException(status_code=400, detail="已完成任务不能取消")
    updated = post.update_task(task_id, status="ignored", error_code="user_cancelled", error_message="用户手动取消")
    torrent_hash = str(task.get("torrent_hash") or "")
    if torrent_hash:
        post.update_qb_torrent(torrent_hash, status="ignored")
    sync_wash_status_for_postprocess_task(task, "cancelled", "用户手动取消后处理任务")
    post.add_event(task_id, "info", "task_cancelled", "用户手动取消后处理任务", {"previous_status": task.get("status", "")})
    return {"status": "ignored", "task": updated}


@app.delete("/api/postprocess/tasks/{task_id}")
def api_delete_postprocess_task(task_id: str) -> dict[str, object]:
    post = get_postprocess_service()
    task = post.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="后处理任务不存在")
    if not postprocess_task_is_terminal(task):
        raise HTTPException(status_code=400, detail="运行中或排队中的后处理任务不能删除，请先取消或等待结束")
    deleted = post.delete_task(task_id)
    return {"status": "ok", "deleted": deleted}


@app.post("/api/postprocess/tasks/{task_id}/worker-done", dependencies=[Depends(require_subtitle_token)])
async def api_postprocess_worker_done(task_id: str, request: Request) -> dict[str, object]:
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(payload, dict):
        payload = {}
    status = str(payload.get("status") or "worker_done").lower()
    post = get_postprocess_service()
    task = post.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="后处理任务不存在")
    if postprocess_task_is_terminal(task):
        post.add_event(task_id, "info", "worker_callback_ignored", "算力端回调到达时任务已终止，忽略回调", {
            "current_status": task.get("status", ""),
            "worker_payload": payload,
        })
        return {"status": "ignored", "message": "任务已终止，忽略 worker 回调", "task": task}
    if status in {"failed", "error"}:
        message = str(payload.get("error") or payload.get("message") or "算力端任务失败")
        if str(task.get("status") or "") == "subtitle_processing":
            post.add_event(task_id, "error", "worker_done", "字幕算力端回报失败，继续保留并激活视频成品", payload)
            return validate_and_activate_postprocess_task(
                task_id,
                output_path=str(task.get("output_path") or payload.get("output_path") or payload.get("video_path") or ""),
                subtitle_error={"error_code": "subtitle_failed", "message": message, "worker_payload": payload},
                worker_result=payload,
            )
        post.update_task(task_id, status="failed", error_code="worker_failed", error_message=message, data={"worker_done": payload})
        post.add_event(task_id, "error", "worker_done", "算力端回报失败", payload)
        return {"status": "failed", "message": message}
    post.update_task(task_id, status="worker_done", data={"worker_done": payload})
    post.add_event(task_id, "info", "worker_done", "算力端回报完成，开始 Unraid 校验", payload)
    worker_payload = (task.get("data") or {}).get("worker_payload") if isinstance(task.get("data"), dict) else {}
    if not isinstance(worker_payload, dict):
        worker_payload = {}
    return validate_and_activate_postprocess_task(
        task_id,
        output_path=str(payload.get("console_output_path") or worker_payload.get("console_output_path") or payload.get("output_path") or payload.get("video_path") or ""),
        subtitle_path=str(payload.get("subtitle_path") or payload.get("srt_path") or ""),
        worker_result=payload,
    )


@app.post("/api/postprocess/tasks/{task_id}/validate")
async def api_validate_postprocess_task(task_id: str, request: Request) -> dict[str, object]:
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(payload, dict):
        payload = {}
    return validate_and_activate_postprocess_task(
        task_id,
        output_path=str(payload.get("output_path") or ""),
        subtitle_path=str(payload.get("subtitle_path") or ""),
        worker_result=payload,
    )


@app.get("/api/postprocess/versions")
def api_postprocess_versions(av_id: str | None = None, limit: int = 100) -> dict[str, object]:
    return {"versions": get_postprocess_service().list_versions(av_id, limit=limit)}


@app.get("/api/postprocess/settings")
def api_postprocess_settings() -> dict[str, object]:
    return {"settings": get_postprocess_service().get_settings()}


@app.post("/api/postprocess/settings")
async def api_update_postprocess_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="设置内容必须是对象")
    if "target_codec" in payload or "target_encoder" in payload:
        payload = normalize_transcode_settings_payload(payload)
    settings_payload = get_postprocess_service().update_settings(payload)
    app_log("info", "postprocess", "后处理设置已保存", {
        "stage": "postprocess_settings_saved",
        "auto_transcode_enabled": settings_payload.get("auto_transcode_enabled"),
        "auto_subtitle_enabled": settings_payload.get("auto_subtitle_enabled"),
        "target_codec": settings_payload.get("target_codec"),
    })
    return {"settings": settings_payload}


@app.post("/api/postprocess/ffmpeg-settings/apply")
async def api_apply_postprocess_ffmpeg_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="FFmpeg 设置内容必须是对象")
    payload = normalize_transcode_settings_payload(payload)
    settings_payload = get_postprocess_service().update_settings(payload)
    ffmpeg_payload = {
        "target_codec": settings_payload.get("target_codec"),
        "target_encoder": settings_payload.get("target_encoder"),
        "crf": settings_payload.get("crf"),
        "preset": settings_payload.get("preset"),
        "preset_flag": settings_payload.get("preset_flag"),
        "ffmpeg_mode": settings_payload.get("ffmpeg_mode"),
        "ffmpeg_standard_enabled": settings_payload.get("ffmpeg_standard_enabled"),
        "ffmpeg_custom_enabled": settings_payload.get("ffmpeg_custom_enabled"),
        "ffmpeg_custom_template": settings_payload.get("ffmpeg_custom_template"),
        "ffmpeg_standard_command": build_ffmpeg_preview(settings_payload),
    }
    remote_result: dict[str, Any] | None = None
    warning = ""
    if backend_url():
        try:
            remote_result = remote_post_json("/api/transcode/settings", ffmpeg_payload, timeout=30)
        except HTTPException as exc:
            warning = f"FFmpeg 设置已保存到控制台，但暂时无法同步算力端: {exc.detail}"
    app_log("info", "postprocess", "FFmpeg 设置已应用", {
        "stage": "ffmpeg_settings_applied",
        "target_codec": settings_payload.get("target_codec"),
        "preset": settings_payload.get("preset"),
        "ffmpeg_custom_enabled": settings_payload.get("ffmpeg_custom_enabled"),
        "synced": bool(remote_result),
    })
    return {
        "status": "ok",
        "settings": settings_payload,
        "ffmpeg": ffmpeg_payload,
        "remote": remote_result,
        "warning": warning,
    }


@app.post("/api/postprocess/queue/run")
def api_run_postprocess_queue() -> dict[str, object]:
    return run_postprocess_queue()


@app.post("/api/postprocess/tasks/run-selected")
async def api_run_selected_postprocess_tasks(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求内容必须是对象")
    task_ids = payload.get("task_ids")
    if not isinstance(task_ids, list):
        raise HTTPException(status_code=400, detail="task_ids 必须是数组")
    return run_selected_postprocess_tasks([str(item) for item in task_ids if str(item or "").strip()])


WORKER_ACTIVE_TASK_STATUSES = {
    "dispatching",
    "sent_to_worker",
    "transcoding",
    "worker_done",
    "transcode_validating",
    "transcode_done",
    "subtitle_processing",
    "subtitle_validating",
}


def worker_transcode_job_ids(worker_status: dict[str, Any]) -> set[str]:
    transcode_jobs_info = worker_status.get("transcode_jobs") if isinstance(worker_status, dict) else {}
    items = transcode_jobs_info.get("items", []) if isinstance(transcode_jobs_info, dict) else []
    ids: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("id"):
            ids.add(str(item.get("id")))
        if item.get("task_id"):
            ids.add(str(item.get("task_id")))
    return ids


def worker_transcode_jobs(worker_status: dict[str, Any]) -> list[dict[str, Any]]:
    if backend_url():
        payload, _ = remote_get_safe("/api/transcode/jobs?limit=500")
        if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
            return [item for item in payload["jobs"] if isinstance(item, dict)]
    transcode_jobs_info = worker_status.get("transcode_jobs") if isinstance(worker_status, dict) else {}
    items = transcode_jobs_info.get("items", []) if isinstance(transcode_jobs_info, dict) else []
    return [item for item in items if isinstance(item, dict)]


def worker_job_matches_task(job: dict[str, Any], task: dict[str, Any]) -> bool:
    data = task.get("data") if isinstance(task.get("data"), dict) else {}
    worker_result = data.get("worker_result") if isinstance(data.get("worker_result"), dict) else {}
    worker_job_id = str(data.get("worker_job_id") or worker_result.get("job_id") or "")
    task_id = str(task.get("id") or "")
    job_ids = {str(job.get("id") or ""), str(job.get("job_id") or ""), str(job.get("task_id") or "")}
    return bool((worker_job_id and worker_job_id in job_ids) or (task_id and task_id in job_ids))


def worker_numeric_value(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip().rstrip("%")
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def worker_transcode_job_done(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").strip().lower()
    if status in {"worker_done", "completed", "done", "success", "succeeded"}:
        return True
    progress = worker_numeric_value(job.get("progress"))
    progress_percent = worker_numeric_value(job.get("progress_percent"))
    message = str(job.get("message") or job.get("last_progress_line") or "")
    return (progress >= 1 or progress_percent >= 100) and ("完成" in message or "ended" in message.lower())


def worker_done_output_path(job: dict[str, Any], task: dict[str, Any]) -> str:
    data = task.get("data") if isinstance(task.get("data"), dict) else {}
    worker_payload = data.get("worker_payload") if isinstance(data.get("worker_payload"), dict) else {}
    candidates = [
        job.get("console_output_path"),
        worker_payload.get("console_output_path"),
        job.get("output_path"),
        job.get("video_path"),
        task.get("output_path"),
    ]
    for candidate in candidates:
        path = rewrite_backend_path_to_console(str(candidate or "")) or ""
        if path:
            return path
    return ""


def worker_done_payload(job: dict[str, Any], task: dict[str, Any], output_path: str) -> dict[str, Any]:
    data = task.get("data") if isinstance(task.get("data"), dict) else {}
    worker_payload = data.get("worker_payload") if isinstance(data.get("worker_payload"), dict) else {}
    return {
        "status": "worker_done",
        "job_id": str(job.get("id") or job.get("job_id") or worker_payload.get("job_id") or ""),
        "task_id": str(task.get("id") or job.get("task_id") or ""),
        "output_path": str(job.get("output_path") or worker_payload.get("output_path") or output_path),
        "input_path": str(job.get("input_path") or worker_payload.get("input_path") or task.get("input_path") or ""),
        "console_output_path": output_path,
        "console_input_path": str(worker_payload.get("console_input_path") or task.get("input_path") or ""),
        "target_codec": str(job.get("target_codec") or task.get("target_codec") or ""),
        "progress": worker_numeric_value(job.get("progress"), 1),
        "progress_percent": worker_numeric_value(job.get("progress_percent"), 100),
        "message": str(job.get("message") or "转码完成"),
        "recovered_by": "postprocess_worker_done_watchdog",
    }


def recover_finished_worker_jobs(worker_status: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(worker_status.get("online") or worker_status.get("status") == "ok"):
        return []
    done_grace_seconds = max(0, int(os.getenv("POSTPROCESS_WORKER_DONE_GRACE_SECONDS", "15")))
    missing_fail_seconds = max(done_grace_seconds, int(os.getenv("POSTPROCESS_WORKER_DONE_MISSING_SECONDS", "300")))
    now = time.time()
    jobs = [job for job in worker_transcode_jobs(worker_status) if worker_transcode_job_done(job)]
    if not jobs:
        return []
    post = get_postprocess_service()
    recovered: list[dict[str, Any]] = []
    for task in post.list_tasks(statuses=["sent_to_worker", "transcoding"], limit=500):
        job = next((item for item in jobs if worker_job_matches_task(item, task)), None)
        if not job:
            continue
        finished_at = worker_numeric_value(job.get("finished_at") or job.get("updated_at"))
        age = now - finished_at if finished_at else now - worker_numeric_value(task.get("updated_at") or task.get("created_at"))
        if age < done_grace_seconds:
            continue
        output_path = worker_done_output_path(job, task)
        data = task.get("data") if isinstance(task.get("data"), dict) else {}
        job_id = str(job.get("id") or job.get("job_id") or data.get("worker_job_id") or "")
        if not output_path or not Path(output_path).exists():
            if age >= missing_fail_seconds:
                message = f"算力端显示转码完成，但控制端找不到输出文件: {output_path or '未返回路径'}"
                post.update_task(
                    str(task["id"]),
                    status="failed",
                    error_code="worker_done_output_missing",
                    error_message=message,
                    data={"worker_done": worker_done_payload(job, task, output_path), "worker_done_missing_job_id": job_id},
                )
                post.add_event(str(task["id"]), "error", "worker_done_output_missing", "已完成的算力端任务缺少输出文件，任务已释放队列", {
                    "worker_job": job,
                    "output_path": output_path,
                    "age_seconds": round(age, 1),
                })
                recovered.append({"task_id": str(task["id"]), "status": "failed", "reason": "output_missing", "worker_job_id": job_id})
            elif data.get("worker_done_missing_job_id") != job_id:
                post.update_task(str(task["id"]), data={"worker_done_missing_job_id": job_id})
                post.add_event(str(task["id"]), "warning", "worker_done_output_waiting", "算力端显示转码完成，等待输出文件可见", {
                    "worker_job_id": job_id,
                    "output_path": output_path,
                    "age_seconds": round(age, 1),
                })
            continue
        payload = worker_done_payload(job, task, output_path)
        post.update_task(str(task["id"]), status="worker_done", data={"worker_done": payload})
        post.add_event(str(task["id"]), "warning", "worker_done_recovered", "检测到算力端已完成但回调未收尾，自动进入校验", payload)
        try:
            result = validate_and_activate_postprocess_task(str(task["id"]), output_path=output_path, worker_result=payload)
            recovered.append({"task_id": str(task["id"]), "status": str(result.get("status") or "completed"), "worker_job_id": job_id})
        except Exception as exc:
            post.update_task(str(task["id"]), status="failed", error_code="worker_done_recovery_failed", error_message=str(exc), data={"worker_done": payload})
            post.add_event(str(task["id"]), "error", "worker_done_recovery_failed", "已完成转码任务自动收尾失败，任务已释放队列", {
                "error": str(exc),
                "worker_job": job,
                "output_path": output_path,
            })
            recovered.append({"task_id": str(task["id"]), "status": "failed", "error": str(exc), "worker_job_id": job_id})
    return recovered


def recover_missing_worker_jobs(worker_status: dict[str, Any]) -> list[dict[str, Any]]:
    if not backend_url() or not bool(worker_status.get("online") or worker_status.get("status") == "ok"):
        return []
    known_ids = worker_transcode_job_ids(worker_status)
    transcode_info = worker_status.get("transcode_jobs") if isinstance(worker_status, dict) else {}
    total = int((transcode_info or {}).get("total") or 0) if isinstance(transcode_info, dict) else 0
    if total and not known_ids:
        return []
    grace_seconds = max(10, int(os.getenv("POSTPROCESS_WORKER_MISSING_GRACE_SECONDS", "60")))
    now = time.time()
    post = get_postprocess_service()
    recovered: list[dict[str, Any]] = []
    for task in post.list_tasks(statuses=["sent_to_worker", "transcoding"], limit=500):
        data = task.get("data") if isinstance(task.get("data"), dict) else {}
        worker_result = data.get("worker_result") if isinstance(data.get("worker_result"), dict) else {}
        worker_job_id = str(data.get("worker_job_id") or worker_result.get("job_id") or "")
        age = now - float(task.get("updated_at") or task.get("created_at") or 0)
        if not worker_job_id or worker_job_id in known_ids or age < grace_seconds:
            continue
        post.update_task(
            str(task["id"]),
            status="ready_to_run",
            error_code="worker_job_missing",
            error_message="算力端重启或任务丢失，已退回队列等待重新派发",
            data={"worker_job_id": "", "worker_result": {}, "worker_missing_job_id": worker_job_id},
        )
        post.add_event(str(task["id"]), "warning", "worker_job_missing", "算力端找不到已派发的转码任务，已退回队列", {
            "worker_job_id": worker_job_id,
            "age_seconds": round(age, 1),
            "worker_transcode_total": total,
        })
        recovered.append({"task_id": str(task["id"]), "status": "ready_to_run", "missing_worker_job_id": worker_job_id})
    return recovered


def active_postprocess_worker_count() -> int:
    return len(get_postprocess_service().list_tasks(statuses=sorted(WORKER_ACTIVE_TASK_STATUSES), limit=500))


def postprocess_task_needs_worker(task: dict[str, Any], settings_payload: dict[str, Any]) -> bool:
    if bool(settings_payload.get("auto_transcode_enabled")):
        return True
    return bool(settings_payload.get("auto_subtitle_enabled")) and bool(task.get("needs_subtitle"))


RUNNABLE_POSTPROCESS_STATUSES = {"waiting_worker", "ready_to_run"}
MISSING_INPUT_POSTPROCESS_STATUS = "waiting_input"
DISPATCHING_POSTPROCESS_STATUS = "dispatching"


def claim_postprocess_task_for_dispatch(task: dict[str, Any]) -> dict[str, Any] | None:
    task_id = str(task.get("id") or "")
    if not task_id:
        return None
    claimed = get_postprocess_service().claim_task_status(task_id, RUNNABLE_POSTPROCESS_STATUSES, DISPATCHING_POSTPROCESS_STATUS)
    if claimed:
        get_postprocess_service().add_event(task_id, "info", "task_dispatch_claimed", "后处理任务已进入派发保护状态")
    return claimed


def hold_postprocess_task_missing_input(task: dict[str, Any]) -> dict[str, Any] | None:
    if str(task.get("input_path") or "").strip():
        return None
    post = get_postprocess_service()
    task_id = str(task.get("id") or "")
    torrent_hash = str(task.get("torrent_hash") or "").strip()
    if torrent_hash:
        next_status = reset_postprocess_task_for_retry(task)
        return {
            "task_id": task_id,
            "status": next_status,
            "reason": "missing_input_waiting_qb",
            "torrent_hash": torrent_hash,
        }
    message = "等待下载完成或路径回写，当前任务缺少输入文件"
    updated = post.update_task(task_id, status=MISSING_INPUT_POSTPROCESS_STATUS, error_code="", error_message=message)
    if updated:
        post.add_event(task_id, "warning", "missing_input_waiting", message)
    return {
        "task_id": task_id,
        "status": MISSING_INPUT_POSTPROCESS_STATUS,
        "reason": "missing_input_no_torrent",
    }


def hold_postprocess_tasks_missing_input(tasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runnable: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for task in tasks:
        held_item = hold_postprocess_task_missing_input(task)
        if not held_item:
            runnable.append(task)
            continue
        held.append(held_item)
    return runnable, held


def run_selected_postprocess_tasks(task_ids: list[str]) -> dict[str, object]:
    post = get_postprocess_service()
    settings_payload = post.get_settings()
    unique_ids = list(dict.fromkeys(str(item or "").strip() for item in task_ids if str(item or "").strip()))
    worker_status = subtitle_backend_status()
    online = bool(worker_status.get("online") or worker_status.get("status") == "ok")
    recovered_finished = recover_finished_worker_jobs(worker_status) if online else []
    recovered_missing = recover_missing_worker_jobs(worker_status) if online else []
    tasks: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for task_id in unique_ids:
        task = post.get_task(task_id)
        if not task:
            skipped.append({"task_id": task_id, "reason": "not_found"})
            continue
        status = str(task.get("status") or "")
        if status not in RUNNABLE_POSTPROCESS_STATUSES:
            skipped.append({"task_id": task_id, "status": status, "reason": "not_runnable"})
            continue
        tasks.append(task)

    tasks, held_missing_input = hold_postprocess_tasks_missing_input(tasks)
    local_candidates = [task for task in tasks if not postprocess_task_needs_worker(task, settings_payload)]
    worker_candidates = [task for task in tasks if postprocess_task_needs_worker(task, settings_payload)]
    updated: list[dict[str, Any]] = []
    for task in local_candidates:
        claimed = claim_postprocess_task_for_dispatch(task)
        if not claimed:
            skipped.append({"task_id": str(task.get("id") or ""), "reason": "status_changed"})
            continue
        try:
            updated.append(dispatch_postprocess_task(claimed))
        except Exception as exc:
            message = str(exc)
            post.update_task(str(task["id"]), status="ready_to_run", error_code="local_postprocess_failed", error_message=message)
            post.add_event(str(task["id"]), "error", "local_postprocess_failed", "批量运行本地后处理失败，任务保留可重试", {"error": message})
            updated.append({"task_id": task["id"], "status": "ready_to_run", "error": message})

    if not online:
        for task in worker_candidates:
            post.update_task(str(task["id"]), status="waiting_worker", error_code="", error_message="")
            post.add_event(str(task["id"]), "info", "worker_offline", "算力端离线，批量运行已保留在等待队列", {"worker_status": worker_status})
        return {
            "status": "waiting_worker" if worker_candidates else "dispatched",
            "selected": len(unique_ids),
            "runnable": len(tasks),
            "updated": len(updated),
            "waiting": len(worker_candidates),
            "held": held_missing_input,
            "skipped": skipped,
            "recovered_finished": recovered_finished,
            "recovered_missing": recovered_missing,
            "worker_status": worker_status,
            "tasks": updated,
        }

    max_concurrency = max(1, min(8, int(settings_payload.get("max_concurrency") or 1)))
    active_count = active_postprocess_worker_count()
    available_slots = max(0, max_concurrency - active_count)
    if available_slots <= 0:
        for task in worker_candidates:
            if task.get("status") == "waiting_worker":
                post.update_task(str(task["id"]), status="ready_to_run", error_code="", error_message="")
            post.add_event(str(task["id"]), "info", "worker_concurrency_full", "后处理并发已满，选中任务保留在可执行队列", {
                "max_concurrency": max_concurrency,
                "active_count": active_count,
            })
        return {
            "status": "concurrency_full",
            "selected": len(unique_ids),
            "runnable": len(tasks),
            "updated": len(updated),
            "queued": len(worker_candidates),
            "held": held_missing_input,
            "skipped": skipped,
            "recovered_finished": recovered_finished,
            "recovered_missing": recovered_missing,
            "max_concurrency": max_concurrency,
            "active_count": active_count,
            "worker_status": worker_status,
            "tasks": updated,
        }

    deferred = max(0, len(worker_candidates) - available_slots)
    for task in worker_candidates[:available_slots]:
        claimed = claim_postprocess_task_for_dispatch(task)
        if not claimed:
            skipped.append({"task_id": str(task.get("id") or ""), "reason": "status_changed"})
            continue
        try:
            updated.append(dispatch_postprocess_task(claimed))
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            message = f"算力端接口返回 {status_code}: {exc.response.text[:200] if exc.response is not None else exc}"
            post.update_task(str(task["id"]), status="ready_to_run", error_code="worker_dispatch_failed", error_message=message)
            post.add_event(str(task["id"]), "error", "worker_dispatch_failed", "批量运行派发失败，任务保留可重试", {"error": message})
            updated.append({"task_id": task["id"], "status": "ready_to_run", "error": message})
        except Exception as exc:
            message = str(exc)
            post.update_task(str(task["id"]), status="waiting_worker", error_code="worker_dispatch_failed", error_message=message)
            post.add_event(str(task["id"]), "error", "worker_dispatch_failed", "批量运行派发失败，任务退回等待队列", {"error": message})
            updated.append({"task_id": task["id"], "status": "waiting_worker", "error": message})
    for task in worker_candidates[available_slots:]:
        if task.get("status") == "waiting_worker":
            post.update_task(str(task["id"]), status="ready_to_run", error_code="", error_message="")
        post.add_event(str(task["id"]), "info", "worker_concurrency_deferred", "后处理并发槽位不足，选中任务保留在可执行队列", {
            "max_concurrency": max_concurrency,
            "active_count": active_count,
            "available_slots": available_slots,
        })
    return {
        "status": "dispatched",
        "selected": len(unique_ids),
        "runnable": len(tasks),
        "updated": len(updated),
        "deferred": deferred,
        "held": held_missing_input,
        "skipped": skipped,
        "recovered_finished": recovered_finished,
        "recovered_missing": recovered_missing,
        "max_concurrency": max_concurrency,
        "active_count": active_count,
        "worker_status": worker_status,
        "tasks": updated,
    }


def wash_mode_from_task_type(task_type: str) -> str:
    if task_type == "wash_4k":
        return "4k"
    if task_type == "wash_chinese":
        return "chinese"
    return ""


def sync_wash_status_for_postprocess_task(task: dict[str, Any], status: str, message: str) -> dict[str, Any] | None:
    mode = wash_mode_from_task_type(str(task.get("task_type") or ""))
    av_id = str(task.get("av_id") or "")
    if not mode or not av_id:
        return None
    return get_subscription_service().update_av_wash(av_id, {
        "mode": mode,
        "status": status,
        "download_status": status,
        "download_message": message,
        "task_id": str(task.get("id") or ""),
        "qb_hash": str(task.get("torrent_hash") or ""),
    })


def run_postprocess_queue() -> dict[str, object]:
    post = get_postprocess_service()
    settings_payload = post.get_settings()
    worker_status = subtitle_backend_status()
    online = bool(worker_status.get("online") or worker_status.get("status") == "ok")
    recovered_finished = recover_finished_worker_jobs(worker_status) if online else []
    recovered_missing = recover_missing_worker_jobs(worker_status) if online else []
    candidates = post.list_tasks(statuses=["waiting_worker", "ready_to_run"], limit=500, order="asc")
    candidates, held_missing_input = hold_postprocess_tasks_missing_input(candidates)
    local_candidates = [task for task in candidates if not postprocess_task_needs_worker(task, settings_payload)]
    worker_candidates = [task for task in candidates if postprocess_task_needs_worker(task, settings_payload)]
    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for task in local_candidates:
        claimed = claim_postprocess_task_for_dispatch(task)
        if not claimed:
            skipped.append({"task_id": str(task.get("id") or ""), "reason": "status_changed"})
            continue
        try:
            updated.append(dispatch_postprocess_task(claimed))
        except Exception as exc:
            message = str(exc)
            post.update_task(task["id"], status="ready_to_run", error_code="local_postprocess_failed", error_message=message)
            post.add_event(task["id"], "error", "local_postprocess_failed", "本地后处理失败，任务保留可重试", {"error": message})
            updated.append({"task_id": task["id"], "status": "ready_to_run", "error": message})
    if not online:
        for task in worker_candidates:
            post.update_task(task["id"], status="waiting_worker")
            post.add_event(task["id"], "info", "worker_offline", "算力端离线，任务保留在等待队列", {"worker_status": worker_status})
        return {
            "status": "waiting_worker" if worker_candidates else "dispatched",
            "updated": len(updated),
            "waiting": len(worker_candidates),
            "held": held_missing_input,
            "skipped": skipped,
            "recovered_finished": recovered_finished,
            "recovered_missing": recovered_missing,
            "worker_status": worker_status,
            "tasks": updated,
        }

    max_concurrency = max(1, min(8, int(settings_payload.get("max_concurrency") or 1)))
    active_count = active_postprocess_worker_count()
    available_slots = max(0, max_concurrency - active_count)
    if available_slots <= 0:
        for task in worker_candidates:
            if task.get("status") == "waiting_worker":
                post.update_task(task["id"], status="ready_to_run")
            post.add_event(task["id"], "info", "worker_concurrency_full", "后处理并发已满，任务保留在可执行队列", {
                "max_concurrency": max_concurrency,
                "active_count": active_count,
            })
        return {
            "status": "concurrency_full",
            "updated": len(updated),
            "queued": len(worker_candidates),
            "held": held_missing_input,
            "skipped": skipped,
            "recovered_finished": recovered_finished,
            "recovered_missing": recovered_missing,
            "max_concurrency": max_concurrency,
            "active_count": active_count,
            "worker_status": worker_status,
            "tasks": updated,
        }

    deferred = max(0, len(worker_candidates) - available_slots)
    for task in worker_candidates[:available_slots]:
        claimed = claim_postprocess_task_for_dispatch(task)
        if not claimed:
            skipped.append({"task_id": str(task.get("id") or ""), "reason": "status_changed"})
            continue
        try:
            result = dispatch_postprocess_task(claimed)
            updated.append(result)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            message = f"算力端接口返回 {status_code}: {exc.response.text[:200] if exc.response is not None else exc}"
            post.update_task(task["id"], status="ready_to_run", error_code="worker_dispatch_failed", error_message=message)
            post.add_event(task["id"], "error", "worker_dispatch_failed", "算力端派发失败，任务保留可重试", {"error": message})
            updated.append({"task_id": task["id"], "status": "ready_to_run", "error": message})
        except Exception as exc:
            message = str(exc)
            post.update_task(task["id"], status="waiting_worker", error_code="worker_dispatch_failed", error_message=message)
            post.add_event(task["id"], "error", "worker_dispatch_failed", "算力端派发失败，任务退回等待队列", {"error": message})
            updated.append({"task_id": task["id"], "status": "waiting_worker", "error": message})
    for task in worker_candidates[available_slots:]:
        if task.get("status") == "waiting_worker":
            post.update_task(task["id"], status="ready_to_run")
        post.add_event(task["id"], "info", "worker_concurrency_deferred", "后处理并发槽位不足，任务保留在可执行队列", {
            "max_concurrency": max_concurrency,
            "active_count": active_count,
            "available_slots": available_slots,
        })
    return {
        "status": "dispatched",
        "updated": len(updated),
        "deferred": deferred,
        "held": held_missing_input,
        "skipped": skipped,
        "recovered_finished": recovered_finished,
        "recovered_missing": recovered_missing,
        "max_concurrency": max_concurrency,
        "active_count": active_count,
        "tasks": updated,
        "worker_status": worker_status,
    }


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, legacy: int = 0) -> Response:
    """日志系统页面"""
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


@app.get("/ui-preview", response_class=HTMLResponse)
def ui_preview_page() -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    frontend_index = frontend_index_response()
    if frontend_index:
        return frontend_index
    return Response("UI preview frontend is not built.", status_code=404, media_type="text/plain")


NOTIFICATION_EVENTS: tuple[dict[str, str], ...] = (
    {"key": "av_subscribed", "name": "番号已加入订阅", "description": "手动或自动新增番号订阅后发送。"},
    {"key": "mteam_found", "name": "MTeam 命中资源", "description": "订阅番号搜索到可下载资源时发送。"},
    {"key": "torrent_sent", "name": "种子已推送下载器", "description": "种子成功发送到 qBittorrent 后发送。"},
    {"key": "jellyfin_in_library", "name": "Jellyfin 已入库", "description": "订阅番号确认已经在媒体库中时发送。"},
    {"key": "task_failed", "name": "任务失败告警", "description": "订阅轮询、下载、集成测试等链路失败时发送。"},
    {"key": "scan_completed", "name": "重复视频扫描完成", "description": "重复视频扫描结束后发送摘要。"},
    {"key": "subtitle_completed", "name": "字幕任务完成", "description": "字幕生成或翻译完成后发送。"},
    {"key": "subtitle_failed", "name": "字幕任务失败", "description": "字幕生成、翻译失败后发送。"},
    {"key": "automation_actress_poll", "name": "女优订阅轮询完成", "description": "自动任务：女优订阅轮询执行完成后发送摘要。"},
    {"key": "automation_av_download", "name": "番号订阅下载完成", "description": "自动任务：订阅番号下载检查执行完成后发送摘要。"},
    {"key": "automation_wash_download", "name": "洗版轮询完成", "description": "自动任务：洗版资源轮询执行完成后发送摘要。"},
)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, legacy: int = 0) -> Response:
    if compute_node_only():
        return Response("MovieMuse compute node is running.", media_type="text/plain")
    return frontend_app_response()


@app.get("/api/notifications/events")
def api_notification_events() -> dict[str, object]:
    return {"events": list(NOTIFICATION_EVENTS)}


@app.post("/api/notifications/test/{channel}")
def api_test_notification(channel: str) -> dict[str, object]:
    settings_data = get_system_settings_service().get()
    result = send_test_notification(channel, settings_data.get("notifications", {}))
    app_log("info" if result.get("status") == "ok" else "error", "notification", "测试通知通道", {"channel": channel, **result})
    return result


@app.post("/api/notifications/test")
async def api_test_notification_payload(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("channel"), dict):
        raise HTTPException(status_code=400, detail="通知通道格式不正确")
    result = send_test_notification_channel(payload["channel"])
    app_log(
        "info" if result.get("status") == "ok" else "error",
        "notification",
        "测试通知通道",
        {"channel": payload["channel"].get("type", ""), **result},
    )
    return result


@app.post("/api/notifications/wechat-work/test-suite")
async def api_wechat_work_test_suite(request: Request) -> dict[str, object]:
    payload = await request.json()
    channel = payload.get("channel") if isinstance(payload, dict) else None
    if not isinstance(channel, dict):
        saved_channel = first_wechat_work_channel()
        if not saved_channel:
            raise HTTPException(status_code=400, detail="未找到已启用的企业微信通知通道")
        channel = saved_channel
    result = send_wechat_work_test_suite(channel)
    app_log(
        "info" if result.get("status") == "ok" else "error",
        "notification",
        "企业微信三类测试通知",
        {"channel": channel.get("id") or channel.get("type") or "", **result},
    )
    return result


@app.post("/api/wechat/menu")
async def api_create_wechat_menu(request: Request) -> dict[str, object]:
    payload = await request.json()
    channel = payload.get("channel") if isinstance(payload, dict) else None
    if isinstance(channel, dict):
        config = channel.get("config") if isinstance(channel.get("config"), dict) else channel
    else:
        saved_channel = first_wechat_work_channel()
        if not saved_channel:
            raise HTTPException(status_code=400, detail="未找到已启用的企业微信通知通道")
        config = saved_channel.get("config") if isinstance(saved_channel.get("config"), dict) else {}
    if not wechat_work_configured(config):
        raise HTTPException(status_code=400, detail="未配置企业微信 CorpID、Secret 或应用 ID")
    return create_wechat_work_menu(config)


@app.get("/api/v1/message")
def api_wechat_verify(
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
) -> Response:
    channel = first_wechat_work_channel(callback=True)
    if not channel:
        raise HTTPException(status_code=400, detail="未配置已启用的企业微信回调通道")
    config = channel.get("config") if isinstance(channel.get("config"), dict) else {}
    token = str(config.get("token") or "")
    if wechat_signature(token, timestamp, nonce, echostr) != msg_signature:
        raise HTTPException(status_code=403, detail="企业微信回调签名校验失败")
    plain = wechat_decrypt_message(config, echostr)
    return Response(plain, media_type="text/plain")


@app.post("/api/v1/message")
async def api_wechat_message(
    request: Request,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
) -> Response:
    channel = first_wechat_work_channel(callback=True)
    if not channel:
        raise HTTPException(status_code=400, detail="未配置已启用的企业微信回调通道")
    config = channel.get("config") if isinstance(channel.get("config"), dict) else {}
    body = (await request.body()).decode("utf-8", errors="ignore")
    try:
        encrypted = parse_xml_payload(body).get("Encrypt", "")
        if not encrypted:
            raise RuntimeError("企业微信回调缺少 Encrypt")
        if wechat_signature(str(config.get("token") or ""), timestamp, nonce, encrypted) != msg_signature:
            raise RuntimeError("企业微信回调签名校验失败")
        payload = parse_xml_payload(wechat_decrypt_message(config, encrypted))
        reply = process_wechat_callback_message(config, payload)
        user_id = payload.get("FromUserName") or str(config.get("touser") or "")
        if str(reply or "").strip():
            send_wechat_work_text(config, user_id, reply)
        app_log("info", "wechat", "企业微信消息已处理", {"user_id": user_id, "msg_type": payload.get("MsgType", ""), "reply": reply[:160]})
    except Exception as exc:
        app_log("error", "wechat", "企业微信消息处理失败", {"error": str(exc)})
    return Response("success", media_type="text/plain")


@app.get("/api/subscriptions/search")
def api_search_subscriptions(q: str = "", type: str = "av", include_mteam: bool = False) -> dict[str, object]:
    """搜索番号或女优。"""
    if not q.strip():
        return {"results": [], "type": type}
    try:
        mteam = search_mteam(q.strip(), get_system_settings_service().get()) if include_mteam else None
        search_type = "actress" if type == "actress" else "av"
        results = cached_subscription_search(q.strip(), search_type)
        if javdb_source_enabled() and not results and is_access_ban_error(javdb.stats().get("last_error")):
            target = "女优" if search_type == "actress" else "番号"
            return {"status": "error", "message": f"JavDB 当前访问被限制，DMM 预售数据源也没有找到该{target}。请暂停抓取或更换代理后再试。", "results": [], "type": search_type, "mteam": mteam}
        return {"results": results, "type": search_type, "mteam": mteam}
    except Exception as e:
        print(f"[API] search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/subscriptions/av")
async def api_subscribe_av(request: Request, background_tasks: BackgroundTasks) -> dict[str, object]:
    """订阅番号"""
    payload = await request.json()
    if not isinstance(payload, dict) or not payload.get("id"):
        raise HTTPException(status_code=400, detail="番号信息格式不正确")
    payload["id"] = canonical_av_id(payload.get("id"))
    app_log("info", "subscription", "开始订阅番号", {"stage": "subscribe_start", "av_id": payload.get("id")})
    payload = prepare_subscription_av_payload(payload, allow_live_detail=False)
    verification = actor_limit_verification(payload, context="manual_subscribe")
    if not verification["ok"]:
        raise HTTPException(status_code=400, detail=verification["reason"] or f"该番号超过全局 {GLOBAL_MAX_COACTORS} 人共演限制，已自动过滤")
    payload = verification["payload"]
    if not payload.get("download_status"):
        payload["download_status"] = "queued"
    if not payload.get("download_message"):
        payload["download_message"] = "已加入订阅，后台检查 Jellyfin 与 MTeam"
    service = get_subscription_service()
    result = service.subscribe_av(payload)
    download_result = {"status": "queued", "message": "后台检查 Jellyfin 与 MTeam"}
    if result.get("status") != "in_library":
        background_tasks.add_task(background_download_subscription_av, str(result.get("id") or ""))
    send_notification_event("av_subscribed", {
        "status": result.get("status") or "subscribed",
        "title": str(result.get("title") or result.get("id") or ""),
        "detail": f"已订阅 {result.get('id', '')}：{result.get('title') or ''}".strip(),
        "av_id": result.get("id", ""),
        "cover": result.get("cover") or result.get("cover_url") or "",
        "release_date": result.get("date") or result.get("release_date") or "",
    })
    if result.get("status") == "in_library":
        send_notification_event("jellyfin_in_library", {
            "status": "in_library",
            "title": str(result.get("title") or result.get("id") or ""),
            "detail": f"{result.get('id', '')} 已在 Jellyfin 媒体库中",
            "av_id": result.get("id", ""),
            "path": result.get("jellyfin_path", ""),
            "file_name": notification_filename(result.get("jellyfin_path"), str(result.get("id") or "")),
            "save_path": notification_parent_path(result.get("jellyfin_path"), ""),
            "cover": result.get("cover") or result.get("cover_url") or "",
        })
    app_log("info", "subscription", "订阅番号已写入，后台检查已入队", {"stage": "subscribe_queued", "av_id": result.get("id"), "status": result.get("status"), "download_status": download_result.get("status")})
    return {"status": "ok", "subscription": result, "download": download_result}


@app.delete("/api/subscriptions/av/{av_id}")
def api_unsubscribe_av(av_id: str) -> dict[str, object]:
    """取消订阅番号"""
    service = get_subscription_service()
    if not service.unsubscribe_av(av_id):
        raise HTTPException(status_code=404, detail="番号未订阅")
    return {"status": "ok"}


@app.get("/api/subscriptions/av")
def api_get_subscribed_av() -> dict[str, object]:
    """获取已订阅番号列表"""
    service = get_subscription_service()
    expire_wash_requests_with_postprocess()
    return {"subscriptions": subscribed_avs_with_global_filter("subscription_list")}


@app.post("/api/subscriptions/av/{av_id}/download")
def api_download_subscription_av(av_id: str) -> dict[str, object]:
    service = get_subscription_service()
    av = next((item for item in service.get_subscribed_av() if item.get("id") == av_id), None)
    if not av:
        raise HTTPException(status_code=404, detail="番号未订阅")
    result = download_av_from_mteam(av)
    latest = next((item for item in service.get_subscribed_av() if item.get("id") == av_id), av)
    return {"status": "ok", "result": result, "subscription": latest}


@app.post("/api/subscriptions/av/download-pending")
def api_download_pending_av() -> dict[str, object]:
    return {"status": "ok", **download_pending_subscriptions()}


@app.post("/api/subscriptions/av/cleanup-dirty")
async def api_cleanup_dirty_subscriptions(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    dry_run = bool(payload.get("dry_run", True))
    return {"status": "ok", "result": cleanup_dirty_subscriptions(dry_run=dry_run)}


@app.post("/api/subscriptions/av/{av_id}/status")
async def api_update_av_status(av_id: str, request: Request) -> dict[str, object]:
    """更新番号状态（pending/done）"""
    payload = await request.json()
    status = payload.get("status", "pending")
    if status not in ("pending", "done", "in_library"):
        raise HTTPException(status_code=400, detail="status 必须是 pending、done 或 in_library")
    service = get_subscription_service()
    if not service.update_av_status(av_id, status):
        raise HTTPException(status_code=404, detail="番号未订阅")
    return {"status": "ok"}


@app.post("/api/subscriptions/av/{av_id}/wash")
async def api_update_av_wash(av_id: str, request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="洗版信息格式不正确")
    mode = str(payload.get("mode") or "").strip().lower()
    status = str(payload.get("status") or "requested").strip().lower()
    if mode not in {"chinese", "4k"}:
        raise HTTPException(status_code=400, detail="洗版类型必须是 chinese 或 4k")
    if status not in {"requested", "downloading", "completed", "expired", "cancelled", "error"}:
        raise HTTPException(status_code=400, detail="洗版状态不正确")
    service = get_subscription_service()
    av = next((item for item in service.get_subscribed_av() if item.get("id") == av_id), None)
    if not av:
        raise HTTPException(status_code=404, detail="番号未订阅")
    if status == "completed":
        raise HTTPException(status_code=400, detail="洗版完成必须由下载、后处理校验和版本激活流程自动判定")
    existing_wash = av.get("wash") if isinstance(av.get("wash"), dict) else {}
    existing_task_id = str(existing_wash.get("task_id") or "")
    task = ensure_wash_postprocess_task(av, mode) if status == "requested" else (get_postprocess_service().get_task(existing_task_id) if existing_task_id else None)
    if status in {"cancelled", "expired"} and task and not postprocess_task_is_terminal(task):
        task_status = "expired" if status == "expired" else "ignored"
        error_code = "wash_expired" if status == "expired" else "user_cancelled"
        message = "洗版任务超过设置期限，已自动取消" if status == "expired" else "用户取消洗版跟踪"
        get_postprocess_service().update_task(task["id"], status=task_status, error_code=error_code, error_message=message)
        get_postprocess_service().add_event(task["id"], "info", "wash_status_cancelled", message, {"av_id": av_id, "status": status})
    wash_payload: dict[str, Any] = {
        "mode": mode,
        "status": status,
        "task_id": task["id"] if task else existing_task_id,
    }
    if status == "requested":
        wash_payload.update({
            "requested_at": time.time(),
            "download_status": "waiting",
            "download_message": "已加入洗版轮询，等待定时任务匹配资源",
            "mteam_torrent_id": "",
            "mteam_torrent_title": "",
            "qb_hash": "",
        })
    elif status in {"cancelled", "expired"}:
        wash_payload.update({
            "download_status": status,
            "download_message": "洗版任务已过期" if status == "expired" else "洗版任务已取消",
        })
    result = service.update_av_wash(av_id, wash_payload)
    if not result:
        raise HTTPException(status_code=404, detail="番号未订阅")
    app_log("info", "wash", "洗版状态已更新", {
        "stage": "wash_request_queued",
        "av_id": av_id,
        "mode": mode,
        "status": status,
    })
    download_result = {
        "status": "queued" if status == "requested" else status,
        "message": "已加入洗版轮询，等待定时任务匹配资源" if status == "requested" else str(wash_payload.get("download_message") or ""),
    }
    latest = next((item for item in service.get_subscribed_av() if item.get("id") == av_id), result)
    return {"status": "ok", "subscription": latest, "download": download_result}


def bad_actress_name(value: object) -> bool:
    text = str(value or "").strip().lower()
    return not text or "404" in text or "页面未找到" in text or "頁面未找到" in text or "page not found" in text


def latest_actress_work_summary(actress: dict[str, Any] | str) -> dict[str, str]:
    payload = {"id": actress, "name": actress} if isinstance(actress, str) else dict(actress)
    try:
        works = subscription_avs_for_actress(payload, limit=20)
    except Exception as exc:
        app_log("warning", "subscription", "女优作品封面兜底失败", {"actress_id": payload.get("id", ""), "error": str(exc)})
        return {}
    for item in works:
        cover = normalize_cover_url(str(item.get("cover") or "").strip())
        if cover:
            return {
                "latest_cover": cover,
                "latest_av_id": str(item.get("id") or ""),
                "latest_title": str(item.get("title") or ""),
                "latest_date": str(item.get("date") or item.get("release_date") or ""),
            }
    return {}


def first_actress_work_cover(actress_id: str) -> str:
    return latest_actress_work_summary(actress_id).get("latest_cover", "")


def av_cover_used_as_actress_cover(value: object) -> bool:
    url = str(value or "").strip()
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    host = parsed.netloc.lower()
    return "dmm.co.jp" in host and "/mono/movie/adult/" in path


def enriched_actress_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = normalize_image_fields(payload)
    if av_cover_used_as_actress_cover(result.get("cover")):
        result.setdefault("latest_cover", result.get("cover"))
        result["cover"] = ""
    actress_ref = str(result.get("id") or result.get("name") or "").strip()
    if not actress_ref:
        return result
    actress_name = str(result.get("name") or actress_ref).strip()
    identity: dict[str, Any] = {}
    try:
        identity = cached_actress_identity(actress_name or actress_ref)
    except Exception as exc:
        app_log("warning", "subscription", "女优双源身份补全失败", {"actress_id": actress_ref, "name": actress_name, "error": str(exc)})

    if identity:
        if identity.get("javdb_id"):
            result["javdb_id"] = identity.get("javdb_id")
        elif identity.get("source") == "javdb" and identity.get("id"):
            result["javdb_id"] = identity.get("id")
        if identity.get("dmm_name"):
            result["dmm_name"] = identity.get("dmm_name")
        elif identity.get("name"):
            result["dmm_name"] = identity.get("name")
        if identity.get("dmm_url"):
            result["dmm_url"] = identity.get("dmm_url")
        if identity.get("source"):
            result["source"] = identity.get("source")
        if identity.get("source_chain"):
            result["source_chain"] = identity.get("source_chain")
        if identity.get("match_reason"):
            result.setdefault("match_reason", identity.get("match_reason"))
        if identity.get("confidence"):
            result.setdefault("confidence", identity.get("confidence"))
        if identity.get("javlibrary_star_id"):
            result["javlibrary_star_id"] = identity.get("javlibrary_star_id")
        if not result.get("cover") and identity.get("source") != "dmm" and identity.get("cover"):
            result["cover"] = normalize_cover_url(str(identity.get("cover") or ""))
        if bad_actress_name(result.get("name")) and identity.get("name"):
            result["name"] = identity.get("name")

    profile: dict[str, Any] = {}
    try:
        profile_ref = str(result.get("javdb_id") or actress_ref)
        profile = cached_actress_profile(profile_ref) or {}
    except Exception as exc:
        app_log("warning", "subscription", "女优资料补全失败", {"actress_id": actress_ref, "error": str(exc)})

    profile_id = str(profile.get("id") or "").strip()
    profile_name = str(profile.get("name") or "").strip()
    profile_cover = normalize_cover_url(str(profile.get("cover") or "").strip())

    if profile_id and profile_id != actress_ref and re.fullmatch(r"[A-Za-z0-9]+", profile_id):
        result["id"] = profile_id
        result["javdb_id"] = profile_id
    if profile_name and bad_actress_name(result.get("name")) and not bad_actress_name(profile_name):
        result["name"] = profile_name
    elif bad_actress_name(result.get("name")):
        result["name"] = actress_ref

    if not result.get("cover") and profile_cover:
        result["cover"] = profile_cover
    if not result.get("cover") and not result.get("latest_cover"):
        result.update(latest_actress_work_summary(result))
    if not result.get("dmm_name") and not bad_actress_name(result.get("name")):
        result["dmm_name"] = result.get("name")
    if not result.get("dmm_url") and str(result.get("url") or "").strip() and allowed_external_url(str(result.get("url") or ""), DMM_HOSTS):
        result["dmm_url"] = str(result.get("url") or "").strip()
    result = explain_match(normalize_image_fields(result), default_reason="actor_identity_enriched", default_confidence="medium")
    remember_actor_identity(result, match_reason=str(result.get("match_reason") or ""), confidence=str(result.get("confidence") or ""))
    return result


def hydrate_actress_subscriptions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    service = get_subscription_service()
    hydrated: list[dict[str, Any]] = []
    for item in items:
        current = dict(item)
        needs_name = bad_actress_name(current.get("name"))
        bad_cover = av_cover_used_as_actress_cover(current.get("cover"))
        needs_cover = not current.get("cover") or bad_cover
        enriched = enriched_actress_payload(current)
        enriched_id = str(enriched.get("id") or "").strip()
        current_id = str(current.get("id") or "").strip()
        if enriched_id and enriched_id != current_id:
            current = service.subscribe_actress({**current, **enriched})
            needs_name = False
            needs_cover = False
        if needs_name or needs_cover:
            patch: dict[str, Any] = {}
            if needs_name and not bad_actress_name(enriched.get("name")):
                patch["name"] = enriched.get("name")
            if needs_cover and (bad_cover or enriched.get("cover") or enriched.get("latest_cover")):
                patch["cover"] = enriched.get("cover") or ""
                patch["latest_cover"] = enriched.get("latest_cover") or ""
                patch["latest_av_id"] = enriched.get("latest_av_id") or ""
                patch["latest_title"] = enriched.get("latest_title") or ""
                patch["latest_date"] = enriched.get("latest_date") or ""
            if patch:
                updated = service.update_actress_subscription(str(current.get("id") or ""), patch)
                current = updated or {**current, **patch}
        hydrated.append(current)
    return hydrated


def actor_identity_public_record(payload: dict[str, Any], *, origin: str, cache_key: str = "") -> dict[str, Any]:
    data = normalize_image_fields(dict(payload or {}))
    canonical_id = str(actor_identity_canonical_id(data) or data.get("canonical_id") or "").strip()
    aliases = normalize_alias_list(data.get("aliases"))
    display_name = str(data.get("display_name") or data.get("name") or data.get("dmm_name") or data.get("id") or "").strip()
    return {
        "canonical_id": canonical_id,
        "cache_key": cache_key,
        "origin": origin,
        "manual": bool(data.get("manual") or origin == "manual"),
        "locked": bool(data.get("locked")),
        "id": str(data.get("id") or data.get("javdb_id") or display_name).strip(),
        "display_name": display_name,
        "name": str(data.get("name") or display_name).strip(),
        "aliases": aliases,
        "preferred_source": str(data.get("preferred_source") or "").strip(),
        "source": str(data.get("source") or "").strip(),
        "source_chain": source_chain_for_item(data),
        "javdb_id": str(data.get("javdb_id") or "").strip(),
        "dmm_name": str(data.get("dmm_name") or "").strip(),
        "dmm_url": str(data.get("dmm_url") or "").strip(),
        "javlibrary_star_id": str(data.get("javlibrary_star_id") or data.get("star_id") or "").strip(),
        "cover": str(data.get("cover") or "").strip(),
        "latest_cover": str(data.get("latest_cover") or "").strip(),
        "latest_av_id": str(data.get("latest_av_id") or "").strip(),
        "latest_title": str(data.get("latest_title") or "").strip(),
        "latest_date": str(data.get("latest_date") or "").strip(),
        "match_reason": str(data.get("match_reason") or "").strip(),
        "confidence": str(data.get("confidence") or "").strip(),
        "updated_at": float(data.get("updated_at") or 0),
    }


def actor_identity_group_keys(record: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    canonical_id = str(record.get("canonical_id") or "").strip().lower()
    if canonical_id:
        keys.add(f"canonical:{canonical_id}")
    javdb_id = str(record.get("javdb_id") or "").strip().lower()
    if javdb_id:
        keys.add(f"javdb:{javdb_id}")
    javlibrary_star_id = str(record.get("javlibrary_star_id") or "").strip().lower()
    if javlibrary_star_id:
        keys.add(f"javlibrary:{javlibrary_star_id}")
    dmm_url = str(record.get("dmm_url") or "").strip()
    if dmm_url:
        keys.add(f"dmm_url:{hashlib.sha256(dmm_url.encode('utf-8')).hexdigest()}")
    for value in [
        record.get("display_name"),
        record.get("name"),
        record.get("dmm_name"),
        record.get("id"),
        *(record.get("aliases") or []),
    ]:
        normalized = normalized_person_name(value)
        if normalized:
            keys.add(f"name:{normalized}")
    return keys


def merge_actor_identity_public_records(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    if incoming.get("manual") and not result.get("manual"):
        result = {**incoming, "related": result.get("related", [])}
    for field in ("javdb_id", "dmm_name", "dmm_url", "javlibrary_star_id", "cover", "latest_cover", "latest_av_id", "latest_title", "latest_date"):
        if not result.get(field) and incoming.get(field):
            result[field] = incoming.get(field)
    if incoming.get("manual"):
        result["manual"] = True
        result["locked"] = bool(incoming.get("locked"))
        result["preferred_source"] = incoming.get("preferred_source") or result.get("preferred_source", "")
    aliases = normalize_alias_list([*(result.get("aliases") or []), *(incoming.get("aliases") or []), incoming.get("display_name"), incoming.get("name"), incoming.get("dmm_name")])
    result["aliases"] = aliases
    chain = []
    for item in [*(result.get("source_chain") or []), *(incoming.get("source_chain") or [])]:
        if item and item not in chain:
            chain.append(item)
    result["source_chain"] = chain
    related = result.setdefault("related", [])
    if incoming.get("cache_key") and incoming.get("cache_key") not in {row.get("cache_key") for row in related if isinstance(row, dict)}:
        related.append({"origin": incoming.get("origin"), "cache_key": incoming.get("cache_key")})
    result["updated_at"] = max(float(result.get("updated_at") or 0), float(incoming.get("updated_at") or 0))
    return result


def list_actor_identity_records(query: str = "", limit: int = 300) -> list[dict[str, Any]]:
    service = get_subscription_service()
    records: dict[str, dict[str, Any]] = {}
    key_index: dict[str, str] = {}

    def add(record: dict[str, Any]) -> None:
        key = str(record.get("canonical_id") or actor_identity_canonical_id(record) or record.get("cache_key") or "").strip()
        if not key:
            return
        group_keys = actor_identity_group_keys(record)
        candidate_ids = [key_index[group_key] for group_key in group_keys if group_key in key_index and key_index[group_key] in records]
        if key in records:
            candidate_ids.append(key)
        if not candidate_ids:
            records[key] = record
            for group_key in group_keys:
                key_index[group_key] = key
            return
        primary = candidate_ids[0]
        merged = records.get(primary, {})
        for candidate in list(dict.fromkeys(candidate_ids[1:])):
            merged = merge_actor_identity_public_records(merged, records.pop(candidate))
            for index_key, index_value in list(key_index.items()):
                if index_value == candidate:
                    key_index[index_key] = primary
        merged = merge_actor_identity_public_records(merged, record)
        records[primary] = merged
        for group_key in actor_identity_group_keys(merged) | group_keys:
            key_index[group_key] = primary

    for row in service.list_metadata_cache("actor_identity", limit=50000):
        data = row.get("data") if isinstance(row, dict) else {}
        if isinstance(data, dict):
            add(actor_identity_public_record(data, origin="auto", cache_key=str(row.get("cache_key") or "")))

    for actress in service.get_subscribed_actresses():
        if isinstance(actress, dict):
            add(actor_identity_public_record({**actress, "manual": False, "source": actress.get("source") or "subscription"}, origin="subscription", cache_key=f"subscription:{actress.get('id', '')}"))

    for row in service.list_metadata_cache("actor_identity_manual", limit=50000):
        data = row.get("data") if isinstance(row, dict) else {}
        if isinstance(data, dict):
            add(actor_identity_public_record(data, origin="manual", cache_key=str(row.get("cache_key") or "")))

    values = list(records.values())
    needle = normalized_person_name(query)
    if needle:
        values = [
            item for item in values
            if any(needle in normalized_person_name(value) for value in [
                item.get("display_name"), item.get("name"), item.get("dmm_name"), item.get("javdb_id"), item.get("javlibrary_star_id"), *(item.get("aliases") or [])
            ])
        ]
    values.sort(key=lambda item: (1 if item.get("manual") else 0, float(item.get("updated_at") or 0), str(item.get("display_name") or "")), reverse=True)
    return values[: max(1, min(1000, int(limit or 300)))]


def merge_manual_actor_identity(payload: dict[str, Any]) -> dict[str, Any]:
    target = payload.get("target") if isinstance(payload.get("target"), dict) else payload
    source_ids = payload.get("source_ids") if isinstance(payload.get("source_ids"), list) else []
    source_records = [
        item for item in list_actor_identity_records(limit=1000)
        if str(item.get("canonical_id") or "") in {str(value) for value in source_ids}
    ]
    merged = dict(target or {})
    for source in source_records:
        for field in ("javdb_id", "dmm_name", "dmm_url", "javlibrary_star_id", "cover", "latest_cover", "latest_av_id", "latest_title", "latest_date"):
            if not merged.get(field) and source.get(field):
                merged[field] = source.get(field)
        merged["aliases"] = normalize_alias_list([*(merged.get("aliases") or []), *(source.get("aliases") or []), source.get("display_name"), source.get("name"), source.get("dmm_name")])
    return save_manual_actor_identity(merged)


@app.get("/api/subscriptions/actor-identities")
def api_list_actor_identities(q: str = "", limit: int = 300) -> dict[str, object]:
    return {"status": "ok", "identities": list_actor_identity_records(q, limit)}


@app.post("/api/subscriptions/actor-identities")
async def api_save_actor_identity(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="身份锚点格式不正确")
    try:
        identity = save_manual_actor_identity(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "identity": actor_identity_public_record(identity, origin="manual")}


@app.post("/api/subscriptions/actor-identities/merge")
async def api_merge_actor_identity(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="身份合并格式不正确")
    try:
        identity = merge_manual_actor_identity(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "identity": actor_identity_public_record(identity, origin="manual")}


@app.post("/api/subscriptions/actor-identities/delete-alias")
async def api_delete_actor_identity_alias(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求格式不正确")
    canonical_id = str(payload.get("canonical_id") or "").strip()
    alias = str(payload.get("alias") or "").strip()
    if not canonical_id or not alias:
        raise HTTPException(status_code=400, detail="缺少 canonical_id 或 alias")
    current = next((item for item in list_actor_identity_records(limit=1000) if str(item.get("canonical_id") or "") == canonical_id), None)
    if not current:
        raise HTTPException(status_code=404, detail="身份锚点不存在")
    alias_key = normalized_person_name(alias)
    current["aliases"] = [item for item in current.get("aliases") or [] if normalized_person_name(item) != alias_key]
    identity = save_manual_actor_identity(current)
    return {"status": "ok", "identity": actor_identity_public_record(identity, origin="manual")}


@app.delete("/api/subscriptions/actor-identities/{canonical_id}")
def api_delete_actor_identity(canonical_id: str) -> dict[str, object]:
    target = str(canonical_id or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="缺少 canonical_id")
    service = get_subscription_service()
    removed = 0
    for row in service.list_metadata_cache("actor_identity_manual", limit=50000):
        data = row.get("data") if isinstance(row, dict) else {}
        if isinstance(data, dict) and str(data.get("canonical_id") or "").strip() == target:
            if service.delete_metadata_cache("actor_identity_manual", str(row.get("cache_key") or "")):
                removed += 1
    if not removed:
        raise HTTPException(status_code=404, detail="人工身份锚点不存在")
    return {"status": "ok", "removed": removed}


@app.post("/api/subscriptions/actress")
async def api_subscribe_actress(request: Request, background_tasks: BackgroundTasks) -> dict[str, object]:
    """订阅女优"""
    payload = await request.json()
    if not isinstance(payload, dict) or not payload.get("id"):
        raise HTTPException(status_code=400, detail="女优信息格式不正确")
    service = get_subscription_service()
    payload = cached_only_enriched_actress_payload(payload)
    result = service.subscribe_actress(payload)
    remember_actor_identity(result, match_reason=str(result.get("match_reason") or "subscribed_actor"), confidence=str(result.get("confidence") or "medium"))
    app_log("info", "subscription", "订阅女优", {"actress_id": result.get("id"), "name": result.get("name"), "since_date": result.get("since_date")})
    background_tasks.add_task(background_enrich_and_subscribe_latest_actress, str(result.get("id") or ""), future_only=True, download=True)
    latest = {"status": "queued", "message": "后台补全身份并扫描未发售番号", "added": [], "skipped": [], "errors": []}
    app_log("info", "subscription", "订阅女优已写入，后台扫描已入队", {
        "stage": "actress_subscribe_queued",
        "actress_id": result.get("id"),
        "name": result.get("name"),
    })
    return {"status": "ok", "subscription": result, "latest": latest}


@app.post("/api/subscriptions/actress/{actress_id}")
async def api_update_actress_subscription(actress_id: str, request: Request) -> dict[str, object]:
    """更新女优订阅配置"""
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="女优订阅配置格式不正确")
    service = get_subscription_service()
    result = service.update_actress_subscription(actress_id, payload)
    if not result:
        raise HTTPException(status_code=404, detail="女优未订阅")
    return {"status": "ok", "subscription": result}


@app.post("/api/subscriptions/actress/{actress_id}/subscribe-latest")
def api_subscribe_actress_latest(actress_id: str, background_tasks: BackgroundTasks) -> dict[str, object]:
    service = get_subscription_service()
    actress = next((item for item in service.get_subscribed_actresses() if item.get("id") == actress_id), None)
    if not actress:
        raise HTTPException(status_code=404, detail="女优未订阅")
    background_tasks.add_task(background_enrich_and_subscribe_latest_actress, actress_id, future_only=True, download=True)
    result = {"status": "queued", "message": "后台扫描未发售番号", "added": [], "skipped": [], "errors": []}
    return {"status": "ok", "result": result}


@app.delete("/api/subscriptions/actress/{actress_id}")
def api_unsubscribe_actress(actress_id: str) -> dict[str, object]:
    """取消订阅女优"""
    service = get_subscription_service()
    if not service.unsubscribe_actress(actress_id):
        raise HTTPException(status_code=404, detail="女优未订阅")
    return {"status": "ok"}


@app.get("/api/subscriptions/actress")
def api_get_subscribed_actresses() -> dict[str, object]:
    """获取已订阅女优列表"""
    service = get_subscription_service()
    return {"subscriptions": hydrate_actress_subscriptions_cached(service.get_subscribed_actresses())}


@app.get("/api/subscriptions/actress/{actress_id}/profile")
def api_get_actress_profile(actress_id: str) -> dict[str, object]:
    if not actress_id:
        raise HTTPException(status_code=400, detail="actress_id required")
    profile = cached_actress_profile(actress_id)
    if not profile:
        profile = {"id": actress_id, "name": "", "cover": ""}
    service = get_subscription_service()
    if service.is_actress_subscribed(actress_id) and (profile.get("name") or profile.get("cover")):
        service.update_actress_subscription(actress_id, {
            "name": profile.get("name") or "",
            "cover": profile.get("cover") or "",
        })
    return {"profile": profile}


@app.get("/api/subscriptions/actress/{actress_id}/avs")
def api_get_actress_avs(actress_id: str) -> dict[str, object]:
    """获取女优全部作品。"""
    try:
        service = get_subscription_service()
        actress = next((item for item in service.get_subscribed_actresses() if item.get("id") == actress_id), None)
        if not actress:
            actress = {"id": actress_id, "name": actress_id}
        results = subscription_avs_for_actress(actress, limit=100)
        if javdb_source_enabled() and not results and is_access_ban_error(javdb.stats().get("last_error")):
            return {"status": "error", "message": "JavDB 当前访问被限制，DMM 预售数据源也没有找到作品。请暂停抓取或更换代理后再试。", "results": []}
        return {"results": results}
    except Exception as e:
        print(f"[API] actress_avs error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/subscriptions/av/actresses")
def api_get_av_actresses(url: str = "", profiles: bool = True) -> dict[str, object]:
    """从番号详情页获取女优列表"""
    if not url:
        raise HTTPException(status_code=400, detail="url 参数不能为空")
    if not (allowed_external_url(url, JAVDB_HOSTS) or allowed_external_url(url, DMM_HOSTS)):
        raise HTTPException(status_code=403, detail="只允许访问 javdb 或 DMM 详情页")
    try:
        if allowed_external_url(url, DMM_HOSTS):
            actresses = cached_detail_for_url(url).get("actresses") or []
        else:
            detail = cached_detail_for_url(url)
            actresses = detail.get("actors") or detail.get("actresses") or []
            if not actresses and javdb_source_enabled():
                actresses = javdb.get_av_actresses(url, include_profiles=profiles)
        return {"actresses": actresses}
    except Exception as e:
        print(f"[API] get_av_actresses error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/subscriptions/av/detail")
def api_get_av_detail(url: str = "") -> dict[str, object]:
    if not url:
        raise HTTPException(status_code=400, detail="url 参数不能为空")
    if not (allowed_external_url(url, JAVDB_HOSTS) or allowed_external_url(url, DMM_HOSTS)):
        raise HTTPException(status_code=403, detail="只允许访问 javdb 或 DMM 详情页")
    try:
        return {"detail": cached_detail_for_url(url)}
    except Exception as e:
        print(f"[API] get_av_detail error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/javdb/listing")
def api_get_javdb_listing(url: str = "", force: bool = False, limit: int = 16, name: str = "") -> dict[str, object]:
    if not url:
        raise HTTPException(status_code=400, detail="url 参数不能为空")
    if not (allowed_external_url(url, JAVDB_HOSTS) or allowed_external_url(url, DMM_HOSTS)):
        raise HTTPException(status_code=403, detail="只允许访问 javdb 或 DMM 页面")
    try:
        safe_limit = max(1, min(60, int(limit or 16)))
        return {"results": cached_listing(url, limit=safe_limit, force_refresh=force, maker_name=name)}
    except Exception as e:
        print(f"[API] get_javdb_listing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dmm/ranking")
def api_get_dmm_ranking(kind: str = "movie", term: str = "daily", force: bool = False, limit: int = 100) -> dict[str, object]:
    try:
        safe_limit = max(1, min(100, int(limit or 100)))
        ranking = cached_dmm_ranking(kind, term, safe_limit, force_refresh=force)
        return {"status": "ok", "ranking": ranking}
    except Exception as e:
        print(f"[API] get_dmm_ranking error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/javdb/status")
def api_get_javdb_status() -> dict[str, object]:
    return {
        "status": "ok",
        "javdb": javdb.stats(),
        "javdb_source_enabled": javdb_source_enabled(),
        "dmm": dmm.stats(),
        "javlibrary": javlibrary.stats(),
        "metadata_cache": get_subscription_service().metadata_cache_stats(),
        "asset_cache": get_subscription_service().asset_cache_stats(),
    }


@app.get("/api/subscriptions/metadata-cache/status")
def api_subscription_metadata_cache_status(clean: bool = True) -> dict[str, object]:
    service = get_subscription_service()
    deleted = service.delete_expired_metadata_cache() if clean else 0
    return {
        "status": "ok",
        "deleted_expired": deleted,
        "metadata_cache": service.metadata_cache_stats(),
        "asset_cache": service.asset_cache_stats(),
        "javdb": javdb.stats(),
        "javdb_source_enabled": javdb_source_enabled(),
        "dmm": dmm.stats(),
        "javlibrary": javlibrary.stats(),
    }


@app.get("/api/subscriptions/asset-cache/status")
def api_subscription_asset_cache_status() -> dict[str, object]:
    settings_payload = get_subscription_service().get_settings()
    return {
        "status": "ok",
        "asset_cache": get_subscription_service().asset_cache_stats(),
        "max_mb": settings_payload.get("asset_cache_max_mb", 2048),
    }


@app.post("/api/subscriptions/asset-cache/maintenance")
async def api_subscription_asset_cache_maintenance(request: Request) -> dict[str, object]:
    payload = await request.json()
    max_mb = payload.get("max_mb") if isinstance(payload, dict) else None
    max_bytes = None
    if max_mb not in (None, ""):
        max_bytes = max(0, int(float(max_mb) * 1024 * 1024))
    return {"status": "ok", "result": maintain_asset_cache(max_bytes)}


@app.post("/api/subscriptions/asset-cache/cleanup")
async def api_subscription_asset_cache_cleanup(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    freeze = bool(payload.get("freeze", True))
    max_mb = payload.get("max_mb", 0)
    max_bytes = max(0, int(float(max_mb or 0) * 1024 * 1024))
    freeze_result = freeze_released_asset_cache() if freeze else {"checked": 0, "frozen": 0}
    cleanup_result = get_subscription_service().cleanup_asset_cache(max_bytes)
    stats = get_subscription_service().asset_cache_stats()
    return {
        "status": "ok",
        "result": {
            "freeze": freeze_result,
            "cleanup": cleanup_result,
            "asset_cache": stats,
            "max_bytes": max_bytes,
        },
    }


@app.get("/api/subscriptions/settings")
def api_get_subscription_settings() -> dict[str, object]:
    service = get_subscription_service()
    return {"settings": service.get_settings()}


@app.post("/api/subscriptions/settings")
async def api_update_subscription_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="设置格式不正确")
    service = get_subscription_service()
    return {"status": "ok", "settings": service.update_settings(payload)}


@app.post("/api/subscriptions/poll")
def api_poll_subscriptions() -> dict[str, object]:
    """手动执行一次女优订阅轮询"""
    return {"status": "ok", "result": run_subscription_task("actress_poll")}


@app.get("/api/subscriptions/tasks")
def api_get_subscription_tasks() -> dict[str, object]:
    return {"tasks": subscription_tasks_payload()}


@app.post("/api/subscriptions/tasks/{task_id}/run")
def api_run_subscription_task(task_id: str) -> dict[str, object]:
    return {"status": "ok", "result": run_subscription_task(task_id)}


@app.get("/api/auth/me")
def api_auth_me(request: Request) -> dict[str, object]:
    username = current_console_user(request)
    auth = get_system_settings_service().auth()
    return {
        "authenticated": bool(username),
        "username": username or auth["username"],
    }


@app.post("/api/auth/login")
async def api_auth_login(request: Request, response: Response) -> dict[str, object]:
    payload = await request.json()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    auth = get_system_settings_service().auth()
    if username != auth["username"] or not secrets.compare_digest(console_password_hash(password), auth["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    token = create_console_session(username)
    response.set_cookie(
        CONSOLE_SESSION_COOKIE,
        token,
        max_age=CONSOLE_SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    return {"status": "ok", "authenticated": True, "username": username}


@app.post("/api/auth/logout")
def api_auth_logout(request: Request, response: Response) -> dict[str, object]:
    token = str(request.cookies.get(CONSOLE_SESSION_COOKIE) or "")
    if token:
        with console_sessions_lock:
            console_sessions.pop(token, None)
    response.delete_cookie(CONSOLE_SESSION_COOKIE)
    return {"status": "ok"}


@app.get("/api/system-settings")
def api_get_system_settings() -> dict[str, object]:
    return {"settings": get_system_settings_service().get()}


@app.post("/api/system-settings")
async def api_update_system_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="设置格式不正确")
    updated = get_system_settings_service().update(payload)
    apply_system_proxy_settings()
    return {"status": "ok", "settings": updated}


@app.get("/api/system-proxy/status")
def api_system_proxy_status() -> dict[str, object]:
    return proxy_status_payload()


@app.post("/api/system-proxy/test")
async def api_system_proxy_test(request: Request) -> dict[str, object]:
    payload = await request.json()
    test_url = str(payload.get("url") or "https://api.ipify.org?format=json").strip()
    if not test_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="测试地址必须是 http 或 https")
    apply_system_proxy_settings()
    try:
        with httpx.Client(timeout=12, follow_redirects=True, trust_env=True) as client:
            resp = client.get(test_url)
            resp.raise_for_status()
            text = resp.text.strip()
            return {
                "status": "ok",
                "url": test_url,
                "status_code": resp.status_code,
                "body": text[:500],
                "proxy": proxy_status_payload(),
            }
    except Exception as exc:
        return {
            "status": "error",
            "url": test_url,
            "message": str(exc),
            "proxy": proxy_status_payload(),
        }


@app.post("/api/system-flaresolverr/test")
def api_system_flaresolverr_test() -> dict[str, object]:
    apply_system_proxy_settings()
    service_url = configured_flaresolverr_url()
    if not service_url:
        return {"status": "error", "message": "FlareSolverr URL 未配置", "proxy": proxy_status_payload()}
    session = f"moviemuse-test-{uuid.uuid4().hex[:8]}"
    started_at = time.time()
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            create_resp = client.post(service_url, json={"cmd": "sessions.create", "session": session})
            create_resp.raise_for_status()
            create_payload = create_resp.json()
            if create_payload.get("status") != "ok":
                return {"status": "error", "message": str(create_payload.get("message") or create_payload), "proxy": proxy_status_payload()}
            try:
                client.post(service_url, json={"cmd": "sessions.destroy", "session": session})
            except Exception:
                pass
        javlibrary.fetch_with_flaresolverr(f"{JAVLIBRARY_BASE_URL}/cn/", retries=0, timeout_ms=120000, cooldown=0)
        elapsed = round(time.time() - started_at, 2)
        return {
            "status": "ok",
            "message": f"FlareSolverr 可用，JavLibrary 预热成功（{elapsed}s）",
            "url": service_url,
            "elapsed": elapsed,
            "proxy": proxy_status_payload(),
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "url": service_url,
            "elapsed": round(time.time() - started_at, 2),
            "proxy": proxy_status_payload(),
        }


@app.get("/api/jellyfin/libraries")
def api_jellyfin_libraries() -> dict[str, object]:
    settings_data = get_system_settings_service().get()
    return {"libraries": get_jellyfin_libraries(settings_data.get("jellyfin", {}))}


@app.get("/api/jellyfin/nfo-actor-repair")
def api_jellyfin_nfo_actor_repair_preview() -> dict[str, object]:
    return {"result": nfo_actor_repair_candidates(apply=False)}


@app.post("/api/jellyfin/nfo-actor-repair")
def api_jellyfin_nfo_actor_repair_apply() -> dict[str, object]:
    return {"result": nfo_actor_repair_candidates(apply=True)}


@app.get("/api/jellyfin/actor-refresh")
def api_jellyfin_actor_refresh_preview() -> dict[str, object]:
    return {"result": jellyfin_actor_refresh_candidates(apply=False)}


@app.post("/api/jellyfin/actor-refresh")
def api_jellyfin_actor_refresh_apply() -> dict[str, object]:
    return {"result": jellyfin_actor_refresh_candidates(apply=True)}


@app.post("/api/integrations/jellyfin/resolve", dependencies=[Depends(require_subtitle_token)])
def api_integration_jellyfin_resolve(payload: JellyfinIntegrationRequest) -> dict[str, object]:
    return {"status": "ok", "media": resolve_jellyfin_media(payload)}


@app.post("/api/integrations/jellyfin/subtitle", dependencies=[Depends(require_subtitle_token)])
def api_integration_jellyfin_subtitle(payload: JellyfinIntegrationRequest) -> dict[str, object]:
    media = resolve_jellyfin_media(payload)
    path = str(media.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="缺少媒体文件路径")
    job = submit_subtitle_job_for_path(path)
    return {
        "status": "queued",
        "media": media,
        "job_id": str(job.get("id") or job.get("job_id") or ""),
        "job": job,
    }


@app.post("/api/integrations/jellyfin/transcode", dependencies=[Depends(require_subtitle_token)])
def api_integration_jellyfin_transcode(payload: JellyfinIntegrationRequest) -> dict[str, object]:
    media = resolve_jellyfin_media(payload)
    return submit_jellyfin_transcode_job(media, payload.target_codec)


@app.get("/api/mteam/search")
def api_search_mteam(q: str = "") -> dict[str, object]:
    if not q.strip():
        return {"enabled": False, "results": [], "message": "请输入关键词"}
    return search_mteam(q.strip(), get_system_settings_service().get())


@app.post("/api/mteam/download")
async def api_download_mteam(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求格式不正确")
    torrent_id = str(payload.get("id") or payload.get("torrent_id") or "").strip()
    title = str(payload.get("title") or "")
    if not torrent_id:
        raise HTTPException(status_code=400, detail="缺少 MTeam 种子 ID")
    settings_data = get_system_settings_service().get()
    app_log("info", "mteam", "手动下载 MTeam 资源", {"stage": "mteam_manual_download_start", "torrent_id": torrent_id, "title": title})
    try:
        torrent_bytes, filename = download_mteam_torrent(torrent_id, settings_data)
        result = add_torrent_to_qbittorrent(torrent_bytes, filename, settings_data.get("qbittorrent", {}))
        app_log("info", "qbittorrent", "MTeam 资源处理完成", {"stage": "mteam_manual_download_done", "torrent_id": torrent_id, "filename": filename, "status": result.get("status"), "message": result.get("message")})
        if str(result.get("status") or "") in {"ok", "exists", "sent"}:
            send_notification_event("torrent_sent", {
                "status": result.get("status") or "sent",
                "title": title or torrent_id,
                "detail": f"手动 MTeam 资源已推送到 qBittorrent：{result.get('message') or ''}",
                "torrent_id": torrent_id,
                "torrent_title": title,
                "qb_hash": result.get("hash", ""),
            })
        else:
            send_notification_event("task_failed", {
                "status": result.get("status") or "failed",
                "title": "手动 MTeam 下载失败",
                "detail": str(result.get("message") or "qBittorrent 未接受种子"),
                "torrent_id": torrent_id,
                "torrent_title": title,
            })
        return {"status": "ok", "message": result.get("message", "已发送到 qBittorrent")}
    except Exception as exc:
        app_log("error", "qbittorrent", "MTeam 资源下载失败", {"stage": "mteam_manual_download_error", "torrent_id": torrent_id, "error": str(exc)})
        send_notification_event("task_failed", {
            "status": "failed",
            "title": "手动 MTeam 下载失败",
            "detail": str(exc),
            "torrent_id": torrent_id,
            "torrent_title": title,
        })
        return {"status": "error", "message": str(exc)}


@app.get("/api/logs")
def api_get_logs(limit: int = 200) -> dict[str, object]:
    return {"logs": get_app_log_service().recent(max(1, min(limit, 1000)))}


@app.post("/api/integrations/test/{name}")
def api_test_integration(name: str) -> dict[str, object]:
    settings_data = get_system_settings_service().get()
    if name == "mteam":
        result = search_mteam("test", settings_data, limit=1)
        status = "ok" if not result.get("message") else "error"
        app_log(status if status == "error" else "info", "mteam", "测试 MTeam 连接", {"status": status, "message": result.get("message", "")})
        return {"status": status, "detail": result}
    if name == "qbittorrent":
        return test_qbittorrent(settings_data.get("qbittorrent", {}))
    if name == "jellyfin":
        return test_jellyfin(settings_data.get("jellyfin", {}))
    raise HTTPException(status_code=404, detail="未知集成")


@app.get("/api/integrations/qbittorrent/options")
def api_qbittorrent_options() -> dict[str, object]:
    settings_data = get_system_settings_service().get()
    return qbittorrent_options(settings_data.get("qbittorrent", {}))


def qbittorrent_options(config: dict[str, Any]) -> dict[str, object]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    if not base_url:
        return {"status": "error", "message": "未配置 qBittorrent Web UI 地址", "categories": [], "tags": []}
    try:
        def operation(client: httpx.Client, _auth_method: str) -> dict[str, object]:
            categories_resp = client.get(f"{base_url}/api/v2/torrents/categories")
            categories_resp.raise_for_status()
            tags_resp = client.get(f"{base_url}/api/v2/torrents/tags")
            tags_resp.raise_for_status()
            raw_categories = categories_resp.json()
            categories: list[str] = []
            if isinstance(raw_categories, dict):
                categories = sorted(str(key) for key in raw_categories if str(key).strip())
            raw_tags = tags_resp.json()
            tags = sorted(str(item) for item in raw_tags if str(item).strip()) if isinstance(raw_tags, list) else []
            return {"status": "ok", "categories": categories, "tags": tags}
        return with_qbittorrent_client(base_url, config, operation, timeout=10)
    except Exception as exc:
        app_log("error", "qbittorrent", "读取 qBittorrent 分类/标签失败", {"stage": "qb_options_failed", "error": str(exc)})
        return {"status": "error", "message": str(exc), "categories": [], "tags": []}


def notification_event_name(event_key: str) -> str:
    event = next((item for item in NOTIFICATION_EVENTS if item.get("key") == event_key), None)
    return str((event or {}).get("name") or event_key)


def default_notification_template(event_key: str) -> dict[str, str]:
    event_name = notification_event_name(event_key)
    return {
        "title": f"MovieMuse：{event_name}",
        "message": "{event_name}\n状态：{status}\n详情：{detail}\n时间：{time}",
    }


def notification_template_looks_broken(template: dict[str, Any]) -> bool:
    text = f"{template.get('title') or ''}\n{template.get('message') or ''}"
    broken_markers = ("锛", "鐘", "歿", "亄", "閫", "绯", "�")
    return any(marker in text for marker in broken_markers)


def render_notification_template(template: str, data: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = data.get(key)
        if value is None:
            return match.group(0)
        return str(value)

    return re.sub(r"\{([a-zA-Z0-9_]+)\}", replace, str(template or ""))


def notification_detail(data: dict[str, Any]) -> str:
    detail = str(data.get("detail") or data.get("message") or "").strip()
    if detail and not notification_text_looks_broken(detail):
        return detail
    parts: list[str] = []
    for key in ("av_id", "title", "actress", "torrent_title", "task_id", "path"):
        value = str(data.get(key) or "").strip()
        if value:
            parts.append(f"{key}: {value}")
    return "；".join(parts) or "事件已处理"


def notification_text_looks_broken(value: object) -> bool:
    text = str(value or "")
    if text.count("?") >= 3:
        return True
    broken_markers = ("锛", "歿", "鐘", "閫", "绯荤", "娑堟", "浠诲", "灏侀")
    return any(marker in text for marker in broken_markers)


def notification_safe_text(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    if not text or notification_text_looks_broken(text):
        return fallback
    return text


def notification_short_text(value: object, fallback: str = "-") -> str:
    text = notification_safe_text(value, fallback)
    text = re.sub(r"\s+", " ", text).strip()
    return text or fallback


def notification_filename(path: object, fallback: str = "-") -> str:
    text = str(path or "").strip()
    if not text:
        return fallback
    return Path(text.replace("\\", "/")).name or fallback


def notification_parent_path(path: object, fallback: str = "-") -> str:
    text = str(path or "").strip()
    if not text:
        return fallback
    normalized = text.replace("\\", "/")
    parent = str(Path(normalized).parent).replace("\\", "/")
    return parent if parent and parent != "." else fallback


def notification_dedupe_key(event_key: str, data: dict[str, Any]) -> str:
    identity = (
        data.get("av_id")
        or data.get("torrent_id")
        or data.get("task_id")
        or data.get("path")
        or data.get("detail")
        or data.get("title")
        or ""
    )
    return f"{event_key}:{str(identity)[:180]}:{data.get('status') or ''}"


def notification_recently_sent(event_key: str, data: dict[str, Any], window_seconds: int = 90) -> bool:
    key = notification_dedupe_key(event_key, data)
    now = time.time()
    with notification_dedupe_lock:
        expired = [item for item, ts in notification_dedupe.items() if now - ts > window_seconds]
        for item in expired:
            notification_dedupe.pop(item, None)
        previous = notification_dedupe.get(key)
        if previous and now - previous <= window_seconds:
            return True
        notification_dedupe[key] = now
    return False


def wechat_http_client(config: dict[str, Any], timeout: float = 12) -> httpx.Client:
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    http_proxy = str(config.get("http_proxy") or "").strip()
    if http_proxy:
        kwargs["proxy"] = http_proxy
    return httpx.Client(**kwargs)


def wechat_api_url(config: dict[str, Any], path: str) -> str:
    api_base = str(config.get("proxy") or "").strip().rstrip("/")
    if api_base:
        return f"{api_base}{path}"
    return f"https://qyapi.weixin.qq.com{path}"


def wechat_access_token(config: dict[str, Any]) -> str:
    corp_id = str(config.get("corp_id") or "").strip()
    corp_secret = str(config.get("corp_secret") or "").strip()
    if not corp_id or not corp_secret:
        raise RuntimeError("未配置企业微信 CorpID 或 Secret")
    cache_key = hashlib.sha256(f"{corp_id}:{corp_secret}".encode("utf-8")).hexdigest()
    now = time.time()
    with wechat_token_lock:
        cached = wechat_token_cache.get(cache_key)
        if cached and str(cached.get("token") or "") and float(cached.get("expires_at") or 0) > now + 120:
            return str(cached["token"])
    with wechat_http_client(config) as client:
        resp = client.get(wechat_api_url(config, "/cgi-bin/gettoken"), params={"corpid": corp_id, "corpsecret": corp_secret})
        resp.raise_for_status()
        payload = resp.json()
    if int(payload.get("errcode") or 0) != 0:
        raise RuntimeError(str(payload.get("errmsg") or payload))
    token = str(payload.get("access_token") or "")
    expires_in = int(payload.get("expires_in") or 7200)
    with wechat_token_lock:
        wechat_token_cache[cache_key] = {"token": token, "expires_at": now + expires_in}
    return token


def send_wechat_work_payload(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, object]:
    token = wechat_access_token(config)
    with wechat_http_client(config) as client:
        resp = client.post(
            wechat_api_url(config, "/cgi-bin/message/send"),
            params={"access_token": token},
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()
    if int(result.get("errcode") or 0) != 0:
        return {"status": "error", "message": str(result.get("errmsg") or result)}
    return {"status": "ok", "message": "企业微信通知已发送", "detail": result}


def wechat_notification_cover_url(config: dict[str, Any], payload: dict[str, Any]) -> str:
    for key in ("cover", "cover_url", "cover_proxy", "latest_cover", "image", "poster", "picurl"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return str(config.get("default_image_url") or "").strip()


def wechat_notification_article_url(config: dict[str, Any], payload: dict[str, Any], image_url: str = "") -> str:
    for value in (
        config.get("article_url"),
        payload.get("article_url"),
        payload.get("url"),
        payload.get("page_url"),
        payload.get("detail_url"),
        payload.get("dmm_url"),
        payload.get("javdb_url"),
        image_url,
    ):
        text = str(value or "").strip()
        if text.startswith(("http://", "https://")):
            return text
    return "https://work.weixin.qq.com"


def wechat_force_news_event(event_key: str) -> bool:
    return event_key in {"av_subscribed", "torrent_sent", "jellyfin_in_library"}


def wechat_text_only_event(event_key: str) -> bool:
    return event_key in {"automation_actress_poll", "automation_av_download", "automation_wash_download"}


def wechat_notification_text(event_key: str, title: str, message: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    av_id = notification_short_text(payload.get("av_id") or payload.get("id"), "")
    item_title = notification_short_text(payload.get("title") or title, av_id or "MovieMuse")
    if event_key == "av_subscribed":
        heading = f"番号{av_id}已加入订阅" if av_id else "番号已加入订阅"
        release_date = notification_short_text(payload.get("release_date") or payload.get("date"), "")
        body_lines = [item_title if item_title and item_title != av_id else notification_short_text(payload.get("detail") or message, "")]
        if release_date:
            body_lines.append(f"发售日期：{release_date}")
        body = "\n".join(line for line in body_lines if line)
        content = f"{heading}\n\n{body}".strip()
        return heading, body, content
    if event_key == "torrent_sent":
        heading = f"番号{av_id}开始下载" if av_id else "番号开始下载"
        site = notification_short_text(payload.get("site") or payload.get("source") or "馒头")
        size = notification_short_text(payload.get("size") or payload.get("size_text") or payload.get("torrent_size"), "-")
        seeders = notification_short_text(payload.get("seeders") or payload.get("seed_count") or payload.get("seeds"), "-")
        downloader = notification_short_text(payload.get("downloader") or "qbittorrent")
        body = "\n".join([
            f"站点：{site}",
            f"标题：{item_title}",
            f"大小：{size}",
            f"做种：{seeders}",
            f"下载器：{downloader}",
        ])
        return heading, body, f"{heading}\n\n{body}"
    if event_key == "jellyfin_in_library":
        heading = f"番号{av_id}入库成功" if av_id else "入库成功"
        path = payload.get("path") or payload.get("output_path") or payload.get("jellyfin_path") or ""
        file_name = notification_short_text(payload.get("file_name") or notification_filename(path, ""), "-")
        save_path = notification_short_text(payload.get("save_path") or notification_parent_path(path, ""), "-")
        body = "\n".join([
            f"文件名称：{file_name}",
            f"保存路径：{save_path}",
        ])
        return heading, body, f"{heading}\n\n{body}"
    fallback = str(message or title or "").strip()
    return title, message, fallback


def wechat_work_message_payload(
    config: dict[str, Any],
    title: str,
    message: str,
    *,
    event_key: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    agent_id = int(str(config.get("agent_id") or "0") or 0)
    touser = str(config.get("touser") or "@all").strip() or "@all"
    payload = payload if isinstance(payload, dict) else {}
    wechat_title, wechat_description, content = wechat_notification_text(event_key, title, message, payload)
    image_url = wechat_notification_cover_url(config, payload)
    force_news = wechat_force_news_event(event_key)
    text_only = wechat_text_only_event(event_key)
    cover_enabled = bool(config.get("cover_enabled", False))
    if not text_only and (force_news or (cover_enabled and image_url)):
        article = {
            "title": wechat_title[:128],
            "description": wechat_description[:512],
            "url": wechat_notification_article_url(config, payload, image_url),
        }
        if cover_enabled and image_url:
            article["picurl"] = image_url
        return {
            "touser": touser,
            "msgtype": "news",
            "agentid": agent_id,
            "news": {"articles": [article]},
            "safe": 0,
            "enable_id_trans": 0,
            "enable_duplicate_check": 0,
        }
    return {
        "touser": touser,
        "msgtype": "text",
        "agentid": agent_id,
        "text": {"content": content[:2048]},
        "safe": 0,
        "enable_id_trans": 0,
        "enable_duplicate_check": 0,
    }


def send_wechat_work_text(config: dict[str, Any], touser: str, content: str) -> dict[str, object]:
    payload = {
        "touser": str(touser or "").strip() or str(config.get("touser") or "@all"),
        "msgtype": "text",
        "agentid": int(str(config.get("agent_id") or "0") or 0),
        "text": {"content": content[:2048]},
        "safe": 0,
    }
    return send_wechat_work_payload(config, payload)


def wechat_work_configured(config: dict[str, Any], *, callback: bool = False) -> bool:
    required = ("corp_id", "corp_secret", "agent_id")
    if callback:
        required = (*required, "token", "aes_key")
    return all(str(config.get(key) or "").strip() for key in required)


def wechat_work_channels(enabled_only: bool = True) -> list[dict[str, Any]]:
    notifications = get_system_settings_service().get().get("notifications", {})
    channels = notifications.get("channels") if isinstance(notifications, dict) else []
    result: list[dict[str, Any]] = []
    for channel in channels if isinstance(channels, list) else []:
        if not isinstance(channel, dict) or str(channel.get("type") or "") != "wechat_work":
            continue
        if enabled_only and not channel.get("enabled"):
            continue
        result.append(channel)
    return result


def first_wechat_work_channel(*, callback: bool = False) -> dict[str, Any] | None:
    for channel in wechat_work_channels(enabled_only=True):
        config = channel.get("config") if isinstance(channel.get("config"), dict) else {}
        if wechat_work_configured(config, callback=callback):
            return channel
    return None


def create_wechat_work_menu(config: dict[str, Any]) -> dict[str, object]:
    token = wechat_access_token(config)
    agent_id = str(config.get("agent_id") or "").strip()
    menu = {
        "button": [
            {"type": "click", "name": "下载", "key": "MM_REFRESH_SUBSCRIPTIONS"},
            {"type": "click", "name": "最新", "key": "MM_REFRESH_ACTRESS"},
            {"type": "click", "name": "帮助", "key": "MM_HELP"},
        ]
    }
    with wechat_http_client(config) as client:
        resp = client.post(
            wechat_api_url(config, "/cgi-bin/menu/create"),
            params={"access_token": token, "agentid": agent_id},
            json=menu,
        )
        resp.raise_for_status()
        result = resp.json()
    if int(result.get("errcode") or 0) != 0:
        return {"status": "error", "message": str(result.get("errmsg") or result)}
    return {"status": "ok", "message": "企业微信应用菜单已创建", "menu": menu}


def wechat_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    raw = "".join(sorted([token, timestamp, nonce, encrypt]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def wechat_aes_key(aes_key: str) -> bytes:
    key = str(aes_key or "").strip()
    if len(key) != 43:
        raise RuntimeError("企业微信 EncodingAESKey 必须为 43 位")
    return base64.b64decode(key + "=")


def wechat_decrypt_message(config: dict[str, Any], encrypt: str) -> str:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception as exc:
        raise RuntimeError("缺少 cryptography 依赖，无法解密企业微信回调") from exc
    key = wechat_aes_key(str(config.get("aes_key") or ""))
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    decryptor = cipher.decryptor()
    padded = decryptor.update(base64.b64decode(encrypt)) + decryptor.finalize()
    pad = padded[-1]
    if pad < 1 or pad > 32:
        raise RuntimeError("企业微信回调解密 padding 无效")
    plain = padded[:-pad]
    if len(plain) < 20:
        raise RuntimeError("企业微信回调解密内容无效")
    msg_len = struct.unpack("!I", plain[16:20])[0]
    xml_bytes = plain[20:20 + msg_len]
    receive_id = plain[20 + msg_len:].decode("utf-8", errors="ignore")
    corp_id = str(config.get("corp_id") or "")
    if corp_id and receive_id and receive_id != corp_id:
        raise RuntimeError("企业微信回调 CorpID 不匹配")
    return xml_bytes.decode("utf-8")


def parse_xml_payload(xml_text: str) -> dict[str, str]:
    root = ET.fromstring(xml_text)
    return {child.tag: str(child.text or "") for child in root}


def wechat_session_key(user_id: str) -> str:
    return str(user_id or "").strip() or "unknown"


def update_wechat_session(user_id: str, **values: Any) -> None:
    key = wechat_session_key(user_id)
    with wechat_session_lock:
        session = wechat_sessions.setdefault(key, {})
        session.update(values)
        session["updated_at"] = time.time()


def get_wechat_session(user_id: str) -> dict[str, Any]:
    key = wechat_session_key(user_id)
    with wechat_session_lock:
        session = dict(wechat_sessions.get(key) or {})
    if session and time.time() - float(session.get("updated_at") or 0) < 3600:
        return session
    return {}


def summarize_search_result(item: dict[str, Any], kind: str) -> str:
    if kind == "actress":
        return (
            f"找到女优：{item.get('name') or item.get('id')}\n"
            f"最新番号：{item.get('latest_av_id') or item.get('latest', {}).get('id') or '-'}\n"
            f"最新标题：{item.get('latest_title') or item.get('latest', {}).get('title') or '-'}\n\n"
            "点击菜单“刷新女优最新”可订阅该女优并刷新未发售番号。"
        )
    return (
        f"找到番号：{item.get('id')}\n"
        f"标题：{item.get('title') or '-'}\n"
        f"日期：{item.get('date') or item.get('release_date') or '-'}\n\n"
        "发送番号会搜索 MTeam；回复数字可选择种子下载。也可以直接回复：订阅 番号"
    )


def wechat_download_stats(download_result: dict[str, Any] | None, subscription: dict[str, Any] | None = None) -> dict[str, int]:
    result = download_result if isinstance(download_result, dict) else {}
    subscription = subscription if isinstance(subscription, dict) else {}
    status = str(result.get("status") or "")
    pushed = 1 if status in {"ok", "exists", "sent"} else 0
    completed = 1 if str(subscription.get("status") or "") in {"done", "in_library"} else 0
    predownload = 1 if str(subscription.get("subscription_mode") or "") == "predownload" and pushed else 0
    return {"pushed": pushed, "completed": completed, "predownload": predownload}


def format_wechat_subscription_stats(stats: dict[str, int], *, prefix: str = "订阅下载任务完成") -> str:
    return (
        f"{prefix}\n"
        f"推送番号下载：{int(stats.get('pushed') or 0)} 个\n"
        f"完成订阅：{int(stats.get('completed') or 0)} 个\n"
        f"完成预下载：{int(stats.get('predownload') or 0)} 个"
    )


def merge_wechat_stats(items: list[dict[str, int]]) -> dict[str, int]:
    return {
        "pushed": sum(int(item.get("pushed") or 0) for item in items),
        "completed": sum(int(item.get("completed") or 0) for item in items),
        "predownload": sum(int(item.get("predownload") or 0) for item in items),
    }


def reserve_wechat_action(user_id: str, action: str, ttl: float = 30) -> bool:
    key = f"{wechat_session_key(user_id)}:{action}"
    now = time.time()
    with wechat_action_dedupe_lock:
        for old_key, old_ts in list(wechat_action_dedupe.items()):
            if now - old_ts > 300:
                wechat_action_dedupe.pop(old_key, None)
        last = float(wechat_action_dedupe.get(key) or 0)
        if now - last < ttl:
            return False
        wechat_action_dedupe[key] = now
        return True


def run_wechat_background_reply(config: dict[str, Any], user_id: str, action: str, build_reply: Callable[[], str]) -> bool:
    if not reserve_wechat_action(user_id, action):
        return False

    def worker() -> None:
        try:
            reply = build_reply()
        except Exception as exc:
            reply = f"操作失败：{exc}"
            app_log("error", "wechat", "企业微信后台操作失败", {"user_id": user_id, "action": action, "error": str(exc)})
        if not str(reply or "").strip():
            return
        try:
            send_wechat_work_text(config, user_id, reply)
        except Exception as exc:
            app_log("error", "wechat", "企业微信后台结果推送失败", {"user_id": user_id, "action": action, "error": str(exc)})

    threading.Thread(target=worker, name=f"wechat-action-{action}", daemon=True).start()
    return True


def wechat_numeric_choice(text: str) -> int | None:
    match = re.fullmatch(r"(?:下载|选择)?\s*(\d{1,2})", str(text or "").strip(), re.I)
    if not match:
        return None
    return int(match.group(1))


def likely_av_id_query(text: str) -> str:
    normalized = canonical_av_id(text)
    if re.fullmatch(r"[A-Z]{2,}-\d{2,}[A-Z]?", normalized):
        return normalized
    return ""


def mteam_candidate_score(query: str, item: dict[str, Any]) -> float:
    title = str(item.get("title") or "").lower()
    haystack = mteam_item_text(item)
    normalized = canonical_av_id(query).lower()
    score = 0.0
    if normalized and normalized in title:
        score += 100
    if contains_any(haystack, ("免费", "免費", "free", "freeleech")):
        score += 20
    if contains_any(haystack, ("中字", "中文", "字幕", "chinese", "chs", "cht", "sub")):
        score += 10
    if contains_any(haystack, ("uhd", "4k", "2160", "2160p")):
        score += 4
    score += min(mteam_size_mb(item.get("size")) / 1024, 20)
    return score


def sorted_mteam_candidates(query: str, torrents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = [(index, item) for index, item in enumerate(torrents) if isinstance(item, dict)]
    indexed.sort(key=lambda pair: (-mteam_candidate_score(query, pair[1]), pair[0]))
    return [item for _, item in indexed]


def compact_wechat_text(value: Any, limit: int = 72) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def format_wechat_mteam_size(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str) and re.search(r"[kmgt]b$", value.strip(), re.I):
        return value.strip()
    size_mb = mteam_size_mb(value)
    if not size_mb:
        return str(value)
    if size_mb >= 1024 * 1024:
        return f"{size_mb / 1024 / 1024:.1f} TB"
    if size_mb >= 1024:
        return f"{size_mb / 1024:.1f} GB"
    return f"{size_mb:.0f} MB"


def format_wechat_mteam_candidates(query: str, torrents: list[dict[str, Any]], *, limit: int = 8) -> str:
    visible = torrents[:limit]
    if not visible:
        return f"MTeam 没有找到资源：{query}"
    lines = [
        f"MTeam 搜索结果：{query}",
        "回复数字选择种子下载，例如：1",
    ]
    for index, item in enumerate(visible, 1):
        title = compact_wechat_text(item.get("title") or item.get("id") or "未命名种子")
        meta: list[str] = []
        size_text = format_wechat_mteam_size(item.get("size"))
        if size_text:
            meta.append(size_text)
        labels = item.get("labels")
        if isinstance(labels, list) and labels:
            meta.append("/".join(compact_wechat_text(label, 10) for label in labels[:3]))
        if item.get("discount"):
            meta.append(compact_wechat_text(item.get("discount"), 12))
        meta_text = f"（{' | '.join(meta)}）" if meta else ""
        lines.append(f"{index}. {title}{meta_text}")
    if len(torrents) > len(visible):
        lines.append(f"仅显示前 {len(visible)} 个，共 {len(torrents)} 个。")
    return "\n".join(lines)


def wechat_search_mteam_candidates(user_id: str, query: str) -> str:
    settings_data = get_system_settings_service().get()
    result = search_mteam(query, settings_data, limit=12)
    torrents = sorted_mteam_candidates(query, result.get("results") or [])
    update_wechat_session(
        user_id,
        last_query=query,
        mteam_query=query,
        mteam_candidates=torrents[:8],
        mteam_pending_query="",
    )
    if not torrents:
        message = str(result.get("message") or "")
        return f"MTeam 没有找到资源：{query}" + (f"\n{message}" if message else "")
    return format_wechat_mteam_candidates(query, torrents)


def schedule_wechat_mteam_search(config: dict[str, Any], user_id: str, query: str) -> None:
    def worker() -> None:
        try:
            reply = wechat_search_mteam_candidates(user_id, query)
        except Exception as exc:
            reply = f"MTeam 搜索失败：{query}\n{exc}"
            update_wechat_session(user_id, mteam_pending_query="", mteam_candidates=[])
            app_log("error", "wechat", "企业微信 MTeam 搜索失败", {"query": query, "user_id": user_id, "error": str(exc)})
        try:
            send_wechat_work_text(config, user_id, reply)
        except Exception as exc:
            app_log("error", "wechat", "企业微信 MTeam 搜索结果推送失败", {"query": query, "user_id": user_id, "error": str(exc)})

    threading.Thread(target=worker, name=f"wechat-mteam-search-{query}", daemon=True).start()


def wechat_download_mteam_candidate(user_id: str, choice: int) -> str:
    session = get_wechat_session(user_id)
    pending_query = str(session.get("mteam_pending_query") or "").strip()
    candidates = session.get("mteam_candidates") if isinstance(session.get("mteam_candidates"), list) else []
    if pending_query and not candidates:
        return f"MTeam 正在搜索：{pending_query}\n结果出来后再回复数字选择。"
    if not candidates:
        return "当前没有可选择的 MTeam 结果。请先发送番号搜索，例如：SNOS-250"
    if choice < 1 or choice > len(candidates):
        return f"数字超出范围，请回复 1 到 {len(candidates)}。"
    torrent = candidates[choice - 1] if isinstance(candidates[choice - 1], dict) else {}
    torrent_id = str(torrent.get("id") or "").strip()
    title = str(torrent.get("title") or torrent_id or "").strip()
    query = str(session.get("mteam_query") or "").strip()
    if not torrent_id:
        return f"第 {choice} 个结果缺少 MTeam 种子 ID，不能直接下载。请换一个编号。"
    settings_data = get_system_settings_service().get()
    app_log("info", "wechat", "企业微信选择 MTeam 种子下载", {
        "stage": "wechat_mteam_choice_download_start",
        "user_id": user_id,
        "query": query,
        "choice": choice,
        "torrent_id": torrent_id,
        "title": title,
    })
    try:
        torrent_bytes, filename = download_mteam_torrent(torrent_id, settings_data)
        qb_result = add_torrent_to_qbittorrent(torrent_bytes, filename, settings_data.get("qbittorrent", {}))
        status = str(qb_result.get("status") or "ok")
        accepted = status in {"ok", "exists", "sent"}
        update_wechat_session(user_id, mteam_candidates=[], mteam_selected=torrent)
        app_log("info" if accepted else "error", "wechat", "企业微信选择 MTeam 种子处理完成", {
            "stage": "wechat_mteam_choice_download_done",
            "user_id": user_id,
            "query": query,
            "choice": choice,
            "torrent_id": torrent_id,
            "status": status,
            "message": qb_result.get("message", ""),
        })
        prefix = "已推送到 qBittorrent" if accepted else "qBittorrent 未接受种子"
        return (
            f"已选择：{choice}. {compact_wechat_text(title, 90)}\n"
            f"{prefix}\n"
            f"状态：{status}\n"
            f"详情：{qb_result.get('message') or ''}"
        ).strip()
    except Exception as exc:
        app_log("error", "wechat", "企业微信选择 MTeam 种子下载失败", {
            "stage": "wechat_mteam_choice_download_error",
            "user_id": user_id,
            "query": query,
            "choice": choice,
            "torrent_id": torrent_id,
            "error": str(exc),
        })
        return f"下载失败：{compact_wechat_text(title, 90)}\n{exc}"


def wechat_subscribe_av_result(query: str) -> dict[str, Any]:
    results = cached_subscription_search(query, "av")
    if not results:
        return {"status": "not_found", "message": f"没有找到番号：{query}", "stats": {"pushed": 0, "completed": 0, "predownload": 0}}
    av = prepare_subscription_av_payload(results[0])
    verification = actor_limit_verification(av, context="wechat_subscribe")
    if not verification["ok"]:
        return {"status": "skipped", "message": f"{av.get('id') or query} {verification['reason'] or '超过共演人数限制'}，已跳过。", "stats": {"pushed": 0, "completed": 0, "predownload": 0}}
    av = verification["payload"]
    apply_jellyfin_status(av)
    saved = get_subscription_service().subscribe_av(av)
    download_result = {"status": "skipped", "message": ""}
    if saved.get("status") != "in_library":
        download_result = download_av_from_mteam(saved)
    stats = wechat_download_stats(download_result, saved)
    return {
        "status": "ok",
        "subscription": saved,
        "download": download_result,
        "stats": stats,
        "message": (
        f"已订阅：{saved.get('id')}\n"
        f"标题：{saved.get('title') or '-'}\n"
        f"状态：{saved.get('status') or '-'}\n"
        f"下载：{download_result.get('status')} {download_result.get('message') or ''}".strip()
        ),
    }


def wechat_subscribe_av(query: str) -> str:
    result = wechat_subscribe_av_result(query)
    return f"{format_wechat_subscription_stats(result.get('stats') or {})}\n{result.get('message') or ''}".strip()


def wechat_refresh_actress_result(query: str) -> dict[str, Any]:
    service = get_subscription_service()
    actress = next(
        (
            item for item in service.get_subscribed_actresses()
            if str(item.get("id") or "").lower() == query.lower() or str(item.get("name") or "").lower() == query.lower()
        ),
        None,
    )
    if not actress:
        results = cached_subscription_search(query, "actress")
        if not results:
            return {"status": "not_found", "message": f"没有找到女优：{query}", "stats": {"pushed": 0, "completed": 0, "predownload": 0}}
        actress = service.subscribe_actress(enriched_actress_payload(results[0]))
    result = subscribe_latest_for_actress(actress, future_only=True, download=False)
    stats_items: list[dict[str, int]] = []
    for item in result.get("added") or []:
        if not isinstance(item, dict):
            continue
        av_id = str(item.get("id") or "")
        latest = next((row for row in service.get_subscribed_av() if row.get("id") == av_id), item)
        download_result = {
            "status": latest.get("download_status") or ("skipped" if latest.get("status") == "in_library" else ""),
            "message": latest.get("download_message") or "",
        }
        stats_items.append(wechat_download_stats(download_result, latest))
    stats = merge_wechat_stats(stats_items)
    return {
        "status": "ok",
        "actress": actress,
        "result": result,
        "stats": stats,
        "message": (
        f"女优最新轮询完成\n"
        f"已刷新：{actress.get('name') or actress.get('id')}\n"
        f"新增订阅：{len(result.get('added') or [])}\n"
        f"跳过：{len(result.get('skipped') or [])}\n"
        f"错误：{len(result.get('errors') or [])}"
        ),
    }


def wechat_refresh_actress(query: str) -> str:
    result = wechat_refresh_actress_result(query)
    return str(result.get("message") or "").strip()


def wechat_refresh_subscriptions_result() -> dict[str, Any]:
    result = download_pending_subscriptions()
    stats_items: list[dict[str, int]] = []
    service = get_subscription_service()
    for row in result.get("results") or []:
        if not isinstance(row, dict):
            continue
        av_id = str(row.get("av_id") or "")
        subscription = next((item for item in service.get_subscribed_av() if item.get("id") == av_id), {})
        stats_items.append(wechat_download_stats(row, subscription))
    stats = merge_wechat_stats(stats_items)
    return {
        "status": "ok",
        "stats": stats,
        "result": result,
        "message": (
            f"检查订阅番号：{result.get('checked') or 0}\n"
            f"推送下载：{result.get('sent') or 0}\n"
            f"结果数：{len(result.get('results') or [])}"
        ),
    }


def wechat_refresh_subscriptions() -> str:
    result = wechat_refresh_subscriptions_result()
    return f"{format_wechat_subscription_stats(result.get('stats') or {})}\n{result.get('message') or ''}".strip()


def wechat_refresh_all_actresses_result() -> dict[str, Any]:
    service = get_subscription_service()
    actresses = [item for item in service.get_subscribed_actresses() if item.get("poll_enabled", True)]
    stats_items: list[dict[str, int]] = []
    refreshed = 0
    added = 0
    skipped = 0
    errors: list[str] = []
    for actress in actresses:
        try:
            result = subscribe_latest_for_actress(actress, future_only=True, download=False)
            refreshed += 1
            added += len(result.get("added") or [])
            skipped += len(result.get("skipped") or [])
            errors.extend(str(item) for item in result.get("errors") or [])
            for item in result.get("added") or []:
                if not isinstance(item, dict):
                    continue
                av_id = str(item.get("id") or "")
                latest = next((row for row in service.get_subscribed_av() if row.get("id") == av_id), item)
                download_result = {
                    "status": latest.get("download_status") or ("skipped" if latest.get("status") == "in_library" else ""),
                    "message": latest.get("download_message") or "",
                }
                stats_items.append(wechat_download_stats(download_result, latest))
        except Exception as exc:
            errors.append(f"{actress.get('name') or actress.get('id')}: {exc}")
    stats = merge_wechat_stats(stats_items)
    return {
        "status": "ok",
        "stats": stats,
        "message": (
            f"女优最新轮询完成\n"
            f"已刷新订阅女优：{refreshed}/{len(actresses)}\n"
            f"新增订阅：{added}\n"
            f"跳过：{skipped}\n"
            f"错误：{len(errors)}"
        ),
    }


def handle_wechat_text_message(config: dict[str, Any], user_id: str, content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return "请输入番号或女优名。"
    choice = wechat_numeric_choice(text)
    if choice is not None:
        return wechat_download_mteam_candidate(user_id, choice)
    subscribe_match = re.match(r"^(订阅|subscribe)\s+(.+)$", text, re.I)
    if subscribe_match:
        return wechat_subscribe_av(subscribe_match.group(2).strip())
    refresh_match = re.match(r"^(刷新|refresh)\s+(.+)$", text, re.I)
    if refresh_match:
        return wechat_refresh_actress(refresh_match.group(2).strip())
    av_query = likely_av_id_query(text)
    if av_query:
        session = get_wechat_session(user_id)
        if str(session.get("mteam_pending_query") or "") == av_query:
            return f"MTeam 正在搜索：{av_query}\n结果出来后会推送编号列表。"
        update_wechat_session(user_id, last_query=av_query, mteam_pending_query=av_query, mteam_candidates=[])
        schedule_wechat_mteam_search(dict(config), user_id, av_query)
        return f"正在搜索 MTeam：{av_query}\n结果出来后会推送编号列表，回复数字即可选择下载。"
    av_results = cached_subscription_search(text, "av")
    if av_results:
        update_wechat_session(user_id, last_av=av_results[0], last_query=text)
        return summarize_search_result(av_results[0], "av")
    actress_results = cached_subscription_search(text, "actress")
    if actress_results:
        update_wechat_session(user_id, last_actress=actress_results[0], last_query=text)
        return summarize_search_result(actress_results[0], "actress")
    return f"没有找到：{text}\n可以发送番号、女优名，或发送“订阅 番号”“刷新 女优名”。"


def handle_wechat_menu_event(config: dict[str, Any], user_id: str, event_key: str) -> str:
    session = get_wechat_session(user_id)
    if event_key == "MM_REFRESH_SUBSCRIPTIONS":
        run_wechat_background_reply(
            dict(config),
            user_id,
            "refresh_subscriptions",
            lambda: f"{format_wechat_subscription_stats((result := wechat_refresh_subscriptions_result()).get('stats') or {})}\n{result.get('message') or ''}".strip(),
        )
        return ""
    if event_key == "MM_SUBSCRIBE_LAST":
        av = session.get("last_av") if isinstance(session.get("last_av"), dict) else {}
        av_id = str(av.get("id") or session.get("last_query") or "").strip()
        if not av_id:
            return "请先发送一个番号搜索，再点击“一键订阅”。"
        result = wechat_subscribe_av_result(av_id)
        return f"{format_wechat_subscription_stats(result.get('stats') or {})}\n{result.get('message') or ''}".strip()
    if event_key == "MM_REFRESH_ACTRESS":
        actress = session.get("last_actress") if isinstance(session.get("last_actress"), dict) else {}
        query = str(actress.get("id") or actress.get("name") or "").strip()
        action = f"refresh_actress:{query or 'all'}"
        if query:
            run_wechat_background_reply(
                dict(config),
                user_id,
                action,
                lambda: str((result := wechat_refresh_actress_result(query)).get("message") or "").strip(),
            )
            return ""
        run_wechat_background_reply(
            dict(config),
            user_id,
            action,
            lambda: str((result := wechat_refresh_all_actresses_result()).get("message") or "").strip(),
        )
        return ""
    return "用法：发送番号搜索 MTeam，回复数字选择下载；菜单“下载”检查已订阅番号的 MTeam 资源；菜单“最新”轮询已订阅女优的新番号。"


def process_wechat_callback_message(config: dict[str, Any], payload: dict[str, str]) -> str:
    user_id = payload.get("FromUserName") or ""
    msg_type = payload.get("MsgType") or ""
    if msg_type == "text":
        return handle_wechat_text_message(config, user_id, payload.get("Content") or "")
    if msg_type == "event":
        return handle_wechat_menu_event(config, user_id, payload.get("EventKey") or payload.get("Event") or "")
    return "已收到。请发送番号搜索 MTeam，或发送女优名进行搜索。"


def send_notification_channel_message(
    channel_config: dict[str, Any],
    title: str,
    message: str,
    *,
    event_key: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, object]:
    channel_type = str(channel_config.get("type") or "").strip().lower()
    config = channel_config.get("config") if isinstance(channel_config.get("config"), dict) else channel_config
    if channel_type == "serverchan":
        send_key = str(config.get("send_key") or "").strip()
        if not send_key:
            return {"status": "error", "message": "未配置 Server 酱 SendKey"}
        try:
            with httpx.Client(timeout=12, follow_redirects=True) as client:
                resp = client.post(f"https://sctapi.ftqq.com/{send_key}.send", data={"title": title, "desp": message})
                resp.raise_for_status()
                return {"status": "ok", "message": "Server 酱通知已发送"}
        except httpx.HTTPError as exc:
            return {"status": "error", "message": f"Server 酱发送失败: {exc}"}
    if channel_type == "gotify":
        base_url = str(config.get("url") or "").strip().rstrip("/")
        token = str(config.get("token") or "").strip()
        if not base_url or not token:
            return {"status": "error", "message": "未配置 Gotify 地址或 Token"}
        try:
            priority = int(config.get("priority") or 5)
        except (TypeError, ValueError):
            priority = 5
        try:
            with httpx.Client(timeout=12, follow_redirects=True) as client:
                resp = client.post(
                    f"{base_url}/message",
                    params={"token": token},
                    json={"title": title, "message": message, "priority": priority},
                )
                resp.raise_for_status()
                return {"status": "ok", "message": "Gotify 通知已发送"}
        except httpx.HTTPError as exc:
            return {"status": "error", "message": f"Gotify 发送失败: {exc}"}
    if channel_type == "wechat_work":
        if not wechat_work_configured(config):
            return {"status": "error", "message": "未配置企业微信 CorpID、Secret 或应用 ID"}
        try:
            return send_wechat_work_payload(
                config,
                wechat_work_message_payload(config, title, message, event_key=event_key, payload=payload or {}),
            )
        except Exception as exc:
            return {"status": "error", "message": f"企业微信发送失败: {exc}"}
    return {"status": "error", "message": "未知通知通道"}


def send_notification_event(event_key: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    settings_data = get_system_settings_service().get()
    notifications = settings_data.get("notifications", {}) if isinstance(settings_data.get("notifications"), dict) else {}
    events = notifications.get("events") if isinstance(notifications.get("events"), dict) else {}
    if events.get(event_key) is False:
        return {"status": "skipped", "reason": "event_disabled", "event": event_key}
    channels = [item for item in notifications.get("channels", []) if isinstance(item, dict) and item.get("enabled")]
    if not channels:
        return {"status": "skipped", "reason": "no_enabled_channels", "event": event_key}

    event_name = notification_event_name(event_key)
    payload = dict(data or {})
    payload.setdefault("event_name", event_name)
    payload.setdefault("title", event_name)
    payload.setdefault("status", "ok")
    payload.setdefault("detail", notification_detail(payload))
    payload.setdefault("time", local_time_text())
    payload["title"] = notification_safe_text(payload.get("title"), event_name)
    payload["detail"] = notification_safe_text(payload.get("detail"), notification_detail({**payload, "detail": ""}))
    if notification_recently_sent(event_key, payload):
        return {"status": "skipped", "reason": "deduped", "event": event_key}
    templates = notifications.get("templates") if isinstance(notifications.get("templates"), dict) else {}
    template = templates.get(event_key) if isinstance(templates.get(event_key), dict) else default_notification_template(event_key)
    if notification_template_looks_broken(template):
        template = default_notification_template(event_key)
    title = render_notification_template(str(template.get("title") or ""), payload)
    message = render_notification_template(str(template.get("message") or ""), payload)

    results: list[dict[str, Any]] = []
    for channel in channels:
        result = send_notification_channel_message(channel, title, message, event_key=event_key, payload=payload)
        results.append({"channel": channel.get("id") or channel.get("type"), **result})
    ok = any(item.get("status") == "ok" for item in results)
    app_log("info" if ok else "error", "notification", f"通知事件：{event_name}", {"event": event_key, "results": results, "payload": payload})
    return {"status": "ok" if ok else "error", "event": event_key, "results": results}


def send_test_notification(channel: str, config: dict[str, Any]) -> dict[str, object]:
    channels = config.get("channels", []) if isinstance(config, dict) else []
    if isinstance(channels, list):
        channel_config = next(
            (
                item
                for item in channels
                if isinstance(item, dict) and str(item.get("id") or item.get("type") or "") == channel
            ),
            None,
        )
        if channel_config:
            return send_test_notification_channel(channel_config)
    if isinstance(channels, dict):
        legacy = channels.get(channel)
        if isinstance(legacy, dict):
            return send_test_notification_channel(
                {
                    "id": channel,
                    "type": channel,
                    "name": channel,
                    "enabled": legacy.get("enabled", True),
                    "config": {key: value for key, value in legacy.items() if key != "enabled"},
                }
            )
    return {"status": "error", "message": "未找到通知通道"}


def send_test_notification_channel(channel_config: dict[str, Any]) -> dict[str, object]:
    title = "MovieMuse 通知测试"
    message = "这是一条测试通知，用于确认通知通道可以正常发送。"
    result = send_notification_channel_message(channel_config, title, message)
    if result.get("status") == "ok":
        channel_type = str(channel_config.get("type") or "").strip().lower()
        label = "Gotify" if channel_type == "gotify" else "Server 酱" if channel_type == "serverchan" else "企业微信" if channel_type == "wechat_work" else "通知"
        return {"status": "ok", "message": f"{label} 测试通知已发送"}
    return result


def wechat_work_test_suite_samples() -> list[dict[str, Any]]:
    cover = "https://pics.dmm.co.jp/mono/movie/adult/ipzz828/ipzz828pl.jpg"
    sample_title = "IPZZ-828 露出水着で晒された水泳部顧問の肉感健康的ボディ"
    now_text = local_time_text()
    return [
        {
            "event_key": "av_subscribed",
            "payload": {
                "status": "subscribed",
                "title": sample_title,
                "detail": sample_title,
                "av_id": "IPZZ-828",
                "release_date": "2026-06-12",
                "cover": cover,
                "time": now_text,
            },
        },
        {
            "event_key": "mteam_found",
            "payload": {
                "status": "found",
                "title": sample_title,
                "detail": "MTeam 命中 3 个候选资源，已按做种数排序。",
                "av_id": "IPZZ-828",
                "site": "馒头",
                "size": "5993.0MB",
                "seeders": "90",
                "cover": cover,
                "time": now_text,
            },
        },
        {
            "event_key": "torrent_sent",
            "payload": {
                "status": "sent",
                "title": sample_title,
                "detail": "测试种子已推送到 qBittorrent",
                "av_id": "IPZZ-828",
                "site": "馒头",
                "size": "5993.0MB",
                "seeders": "90",
                "downloader": "qbittorrent",
                "cover": cover,
                "time": now_text,
            },
        },
        {
            "event_key": "jellyfin_in_library",
            "payload": {
                "status": "completed",
                "title": sample_title,
                "detail": "IPZZ-828 已完成后处理并刷新 Jellyfin",
                "av_id": "IPZZ-828",
                "path": "/media/study3/IPZZ-828.mp4",
                "file_name": "IPZZ-828.mp4",
                "save_path": "/media/study3",
                "cover": cover,
                "time": now_text,
            },
        },
        {
            "event_key": "task_failed",
            "payload": {
                "status": "failed",
                "title": "任务失败告警",
                "detail": "测试失败通知：下载器连接异常。",
                "av_id": "IPZZ-999",
                "time": now_text,
            },
        },
        {
            "event_key": "scan_completed",
            "payload": {
                "status": "completed",
                "title": "重复视频扫描完成",
                "detail": "扫描 128 个媒体文件，发现重复组 2 个。",
                "time": now_text,
            },
        },
        {
            "event_key": "subtitle_completed",
            "payload": {
                "status": "completed",
                "title": "字幕任务完成",
                "detail": "IPZZ-828 字幕生成完成，已保存到媒体目录。",
                "av_id": "IPZZ-828",
                "path": "/media/study3/IPZZ-828.srt",
                "time": now_text,
            },
        },
        {
            "event_key": "subtitle_failed",
            "payload": {
                "status": "failed",
                "title": "字幕任务失败",
                "detail": "IPZZ-828 字幕生成失败：测试错误消息。",
                "av_id": "IPZZ-828",
                "path": "/media/study3/IPZZ-828.mp4",
                "time": now_text,
            },
        },
        {
            "event_key": "automation_actress_poll",
            "payload": {
                "status": "completed",
                "title": "女优订阅轮询完成",
                "detail": "检查女优：7 个\n新增番号：1 个\n错误：0 个",
                "task": "actress_poll",
                "task_name": "女优订阅轮询",
                "checked": 7,
                "added": 1,
                "errors": 0,
                "time": now_text,
            },
        },
        {
            "event_key": "automation_av_download",
            "payload": {
                "status": "completed",
                "title": "番号订阅下载完成",
                "detail": "检查订阅番号：12 个\n推送下载：2 个\n未找到资源：3 个\n错误：0 个",
                "task": "av_download",
                "task_name": "番号订阅下载",
                "checked": 12,
                "sent": 2,
                "errors": 0,
                "time": now_text,
            },
        },
        {
            "event_key": "automation_wash_download",
            "payload": {
                "status": "completed",
                "title": "洗版轮询完成",
                "detail": "检查洗版番号：5 个\n推送下载：1 个\n未匹配：2 个\n过期：0 个\n错误：0 个",
                "task": "wash_download",
                "task_name": "洗版轮询",
                "checked": 5,
                "sent": 1,
                "errors": 0,
                "time": now_text,
            },
        },
    ]


def send_wechat_work_test_suite(channel_config: dict[str, Any]) -> dict[str, object]:
    channel_type = str(channel_config.get("type") or "").strip().lower()
    if channel_type != "wechat_work":
        return {"status": "error", "message": "请选择企业微信通知通道", "sent": 0, "total": 0, "results": []}
    results: list[dict[str, Any]] = []
    notifications = get_system_settings_service().get().get("notifications", {})
    templates = notifications.get("templates") if isinstance(notifications, dict) and isinstance(notifications.get("templates"), dict) else {}
    for sample in wechat_work_test_suite_samples():
        event_key = str(sample.get("event_key") or "")
        event_name = notification_event_name(event_key)
        payload = sample.get("payload") if isinstance(sample.get("payload"), dict) else {}
        payload = dict(payload)
        payload.setdefault("event_name", event_name)
        payload.setdefault("status", "ok")
        payload.setdefault("title", event_name)
        payload.setdefault("detail", notification_detail(payload))
        payload.setdefault("time", local_time_text())
        template = templates.get(event_key) if isinstance(templates.get(event_key), dict) else default_notification_template(event_key)
        if notification_template_looks_broken(template):
            template = default_notification_template(event_key)
        title = render_notification_template(str(template.get("title") or ""), payload)
        message = render_notification_template(str(template.get("message") or ""), payload)
        result = send_notification_channel_message(
            channel_config,
            title,
            message,
            event_key=event_key,
            payload=payload,
        )
        results.append({"event": event_key, **result})
    sent = sum(1 for item in results if item.get("status") == "ok")
    total = len(results)
    return {
        "status": "ok" if sent == total else "error",
        "message": f"企业微信测试通知已发送 {sent}/{total}",
        "sent": sent,
        "total": total,
        "results": results,
    }


def test_qbittorrent(config: dict[str, Any]) -> dict[str, object]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    if not base_url:
        return {"status": "error", "message": "未配置 qBittorrent Web UI 地址"}
    try:
        def operation(client: httpx.Client, auth_method: str) -> dict[str, object]:
            resp = client.get(f"{base_url}/api/v2/app/version")
            resp.raise_for_status()
            categories = client.get(f"{base_url}/api/v2/torrents/categories")
            categories.raise_for_status()
            category = str(config.get("category") or "")
            category_note = ""
            if category and category not in categories.json():
                category_note = f"，分类 {category} 尚未创建"
            auth_label = {"api_key": "API Key", "password": "账号密码", "none": "免登录"}.get(auth_method, auth_method)
            message = f"qBittorrent {resp.text.strip()}（{auth_label}）{category_note}"
            app_log("info", "qbittorrent", "测试 qBittorrent 连接", {"message": message, "auth_method": auth_method})
            return {"status": "ok", "message": message}
        return with_qbittorrent_client(base_url, config, operation, timeout=10)
    except httpx.HTTPError as exc:
        message = f"qBittorrent 连接失败: {exc}"
        app_log("error", "qbittorrent", "测试 qBittorrent 连接失败", {"error": str(exc)})
        return {"status": "error", "message": message}
    except RuntimeError as exc:
        message = f"qBittorrent 连接失败: {exc}"
        app_log("error", "qbittorrent", "测试 qBittorrent 连接失败", {"error": str(exc)})
        return {"status": "error", "message": message}


def test_jellyfin(config: dict[str, Any]) -> dict[str, object]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    if not base_url:
        return {"status": "error", "message": "未配置 Jellyfin 地址"}
    if not api_key:
        return {"status": "error", "message": "未配置 Jellyfin API Key"}
    headers = {"X-Emby-Token": api_key} if api_key else {}
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.get(f"{base_url}/System/Info", headers=headers)
            resp.raise_for_status()
            info = resp.json()
            name = info.get("ServerName") or info.get("LocalAddress") or "Jellyfin"
            version = info.get("Version") or ""
            user_name = str(config.get("username") or "").strip()
            if user_name:
                user_id = get_jellyfin_user_id(config)
                if not user_id:
                    message = f"Jellyfin 已连接，但未找到用户 {user_name}"
                    app_log("error", "jellyfin", "测试 Jellyfin 用户失败", {"username": user_name})
                    return {"status": "error", "message": message}
            library_note = ""
            library_id = str(config.get("library_id") or "").strip()
            if library_id:
                libraries = get_jellyfin_libraries(config)
                found = next((item for item in libraries if item.get("id") == library_id), None)
                if not found:
                    message = f"Jellyfin 已连接，但未找到媒体库 {library_id}"
                    app_log("error", "jellyfin", "测试 Jellyfin 媒体库失败", {"library_id": library_id})
                    return {"status": "error", "message": message}
                library_note = f" / 媒体库: {found.get('name')}"
            message = f"{name} {version}{library_note}".strip()
            app_log("info", "jellyfin", "测试 Jellyfin 连接", {"message": message})
            return {"status": "ok", "message": message}
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 401:
            message = "Jellyfin 地址可访问，但 API Key 未授权或已失效（401）。请在 Jellyfin 后台重新生成 API Key 后保存。"
        else:
            message = f"Jellyfin 连接失败: HTTP {status_code}"
        app_log("error", "jellyfin", "测试 Jellyfin 连接失败", {"status_code": status_code, "error": str(exc)})
        return {"status": "error", "message": message}
    except (httpx.HTTPError, ValueError) as exc:
        message = f"Jellyfin 连接失败: {exc}"
        app_log("error", "jellyfin", "测试 Jellyfin 连接失败", {"error": str(exc)})
        return {"status": "error", "message": message}


def asset_kind_dir(kind: str) -> str:
    return ASSET_KIND_DIRS.get(str(kind or "").strip().lower(), "images")


def asset_file_extension(media_type: str, source_url: str = "") -> str:
    content_type = str(media_type or "").split(";")[0].strip().lower()
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if content_type == "video/mp4":
        return ".mp4"
    if content_type == "video/webm":
        return ".webm"
    if content_type == "video/ogg":
        return ".ogg"
    if content_type in {"video/quicktime", "video/mov"}:
        return ".mov"
    suffix = Path(urlparse(source_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".ogg", ".mov"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def safe_asset_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip().upper()).strip(".-_")
    return (stem or hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16])[:96]


def asset_file_from_record(data_dir: Path, asset: dict[str, Any]) -> Path | None:
    local_path = str(asset.get("local_path") or "").strip()
    if not local_path:
        return None
    target = (data_dir / local_path).resolve()
    if not is_relative_to(target, data_dir.resolve()) or not target.exists() or not target.is_file():
        return None
    return target


def asset_cache_headers(immutable: bool = False) -> dict[str, str]:
    if immutable:
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    return {"Cache-Control": "public, max-age=86400"}


def image_source_rank(url: str) -> int:
    path = urlparse(str(url or "")).path.lower()
    name = Path(path).name
    if name.endswith("pl.jpg") or name.endswith("pl.png") or name.endswith("pl.webp"):
        return 40
    if name.endswith("ps.jpg") or name.endswith("ps.png") or name.endswith("ps.webp"):
        return 20
    if any(token in name for token in ("large", "big", "cover")):
        return 15
    return 10


def fetch_image_bytes(url: str) -> tuple[bytes, str]:
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            resp.raise_for_status()
            media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
            if not media_type.lower().startswith("image/"):
                raise HTTPException(status_code=415, detail="远端资源不是图片")
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > ASSET_IMAGE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="图片超过本地缓存大小限制")
                chunks.append(chunk)
    content = b"".join(chunks)
    if dmm_placeholder_image(url, content):
        remember_dmm_placeholder_cover(url, True)
        raise HTTPException(status_code=404, detail="DMM 当前只返回占位封面")
    remember_dmm_placeholder_cover(url, False)
    return content, media_type


def dmm_placeholder_image(url: str, content: bytes) -> bool:
    target = str(url or "").lower()
    if "pics.dmm.co.jp/mono/movie/adult/" not in target:
        return False
    return len(content or b"") <= 4096


def dmm_cover_url_is_placeholder(url: str) -> bool:
    target = str(url or "").strip().lower()
    if "pics.dmm.co.jp/mono/movie/adult/" not in target:
        return False
    try:
        with httpx.Client(timeout=8, follow_redirects=True) as client:
            response = client.head(str(url), headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            content_length = int(response.headers.get("content-length") or 0)
            result = content_type.startswith("image/") and 0 < content_length <= 4096
            remember_dmm_placeholder_cover(url, result)
            return result
    except Exception:
        return False


def fetch_media_bytes(url: str) -> tuple[bytes, str]:
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            resp.raise_for_status()
            media_type = resp.headers.get("content-type", "video/mp4").split(";")[0].strip() or "video/mp4"
            suffix = Path(urlparse(url).path).suffix.lower()
            if not media_type.lower().startswith("video/") and suffix not in {".mp4", ".webm", ".ogg", ".mov"}:
                raise HTTPException(status_code=415, detail="远端资源不是可持久化视频")
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > ASSET_MEDIA_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="预告超过本地缓存大小限制")
                chunks.append(chunk)
    return b"".join(chunks), media_type


def serve_asset_record(data_dir: Path, asset: dict[str, Any]) -> FileResponse | None:
    asset_path = asset_file_from_record(data_dir, asset)
    if not asset_path:
        return None
    try:
        if dmm_placeholder_image(str(asset.get("source_url") or ""), asset_path.read_bytes()):
            return None
    except Exception:
        return None
    return FileResponse(
        asset_path,
        media_type=str(asset.get("media_type") or "image/jpeg"),
        headers=asset_cache_headers(bool(asset.get("immutable"))),
    )


def persist_image_asset(url: str, entity_id: str, kind: str, immutable: bool) -> FileResponse:
    if not allowed_external_url(url, IMAGE_PROXY_HOSTS):
        raise HTTPException(status_code=403, detail="只允许代理指定域名图片")
    _, _, data_dir = settings()
    safe_kind = str(kind or "cover").strip().lower()
    entity_id = canonical_av_id(entity_id) if safe_kind == "cover" else safe_asset_stem(entity_id)
    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id 参数不能为空")
    content, media_type = fetch_image_bytes(url)
    digest = hashlib.sha256(content).hexdigest()
    target_dir = data_dir / "subscription-assets" / asset_kind_dir(safe_kind)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{safe_asset_stem(entity_id)}{asset_file_extension(media_type, url)}"
    tmp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    tmp_path.write_bytes(content)
    tmp_path.replace(target_path)
    relative_path = str(target_path.relative_to(data_dir))
    asset = get_subscription_service().set_asset_cache(
        entity_id,
        safe_kind,
        url,
        relative_path,
        media_type,
        digest,
        len(content),
        immutable=immutable,
    )
    app_log("info", "asset-cache", "封面资产已写入本地", {
        "stage": "asset_cache_store",
        "entity_id": entity_id,
        "kind": safe_kind,
        "bytes": len(content),
        "immutable": immutable,
    })
    return FileResponse(
        target_path,
        media_type=asset.get("media_type") or media_type,
        headers=asset_cache_headers(bool(asset.get("immutable"))),
    )


def persist_media_asset(url: str, entity_id: str, immutable: bool) -> FileResponse:
    if not allowed_external_url(url, IMAGE_PROXY_HOSTS):
        raise HTTPException(status_code=403, detail="只允许代理指定域名媒体")
    _, _, data_dir = settings()
    entity_id = canonical_av_id(entity_id) or safe_asset_stem(entity_id)
    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id 参数不能为空")
    content, media_type = fetch_media_bytes(url)
    digest = hashlib.sha256(content).hexdigest()
    target_dir = data_dir / "subscription-assets" / asset_kind_dir("trailer")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{safe_asset_stem(entity_id)}{asset_file_extension(media_type, url)}"
    tmp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    tmp_path.write_bytes(content)
    tmp_path.replace(target_path)
    relative_path = str(target_path.relative_to(data_dir))
    asset = get_subscription_service().set_asset_cache(
        entity_id,
        "trailer",
        url,
        relative_path,
        media_type,
        digest,
        len(content),
        immutable=immutable,
    )
    app_log("info", "asset-cache", "预告资产已写入本地", {
        "stage": "asset_cache_store",
        "entity_id": entity_id,
        "kind": "trailer",
        "bytes": len(content),
        "immutable": immutable,
    })
    return FileResponse(
        target_path,
        media_type=asset.get("media_type") or media_type,
        headers=asset_cache_headers(bool(asset.get("immutable"))),
    )


@app.get("/api/proxy/image")
def proxy_image(url: str = "", av_id: str = "", entity_id: str = "", kind: str = "image", immutable: bool = False) -> Response:
    """代理图片请求；带 entity_id/kind 时会持久化为本地资产。"""
    safe_kind = str(kind or "image").strip().lower()
    normalized_av_id = canonical_av_id(av_id)
    normalized_entity_id = normalized_av_id or (safe_asset_stem(entity_id) if entity_id else "")
    _, _, data_dir = settings()
    if normalized_entity_id:
        asset = get_subscription_service().get_asset_cache(normalized_entity_id, safe_kind)
        if asset:
            local = serve_asset_record(data_dir, asset)
            existing_url = str(asset.get("source_url") or "")
            can_upgrade = bool(url) and image_source_rank(url) > image_source_rank(existing_url)
            if local and (not url or existing_url == str(url or "") or (bool(asset.get("immutable")) and not can_upgrade)):
                return local
    if not url:
        raise HTTPException(status_code=400, detail="url 参数不能为空")
    if not allowed_external_url(url, IMAGE_PROXY_HOSTS):
        raise HTTPException(status_code=403, detail="只允许代理指定域名图片")
    try:
        if normalized_entity_id and safe_kind in {"cover", "actor", "screenshot"}:
            return persist_image_asset(url, normalized_entity_id, safe_kind, immutable)
        cache_dir = data_dir / "image-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_file = cache_dir / f"{cache_key}.bin"
        meta_file = cache_dir / f"{cache_key}.type"
        cache_ttl = int(os.getenv("IMAGE_PROXY_CACHE_TTL_SECONDS", "2592000"))
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime <= cache_ttl:
            media_type = "image/jpeg"
            if meta_file.exists():
                media_type = meta_file.read_text(encoding="utf-8").strip() or media_type
            return FileResponse(cache_file, media_type=media_type, headers=asset_cache_headers(False))
        content, media_type = fetch_image_bytes(url)
        cache_file.write_bytes(content)
        meta_file.write_text(media_type, encoding="utf-8")
        return Response(
            content=content,
            media_type=media_type,
            headers=asset_cache_headers(False),
        )
    except httpx.HTTPStatusError as exc:
        stale_asset = get_subscription_service().get_asset_cache(normalized_entity_id, safe_kind) if normalized_entity_id else None
        if stale_asset:
            local = serve_asset_record(data_dir, stale_asset)
            if local:
                return local
        raise HTTPException(status_code=exc.response.status_code, detail="图片获取失败") from exc
    except httpx.HTTPError as exc:
        stale_asset = get_subscription_service().get_asset_cache(normalized_entity_id, safe_kind) if normalized_entity_id else None
        if stale_asset:
            local = serve_asset_record(data_dir, stale_asset)
            if local:
                return local
        raise HTTPException(status_code=502, detail=f"图片代理失败: {exc}") from exc


@app.get("/api/proxy/media")
def proxy_media(url: str = "", av_id: str = "", entity_id: str = "", immutable: bool = False) -> Response:
    """代理可直接下载的预告视频，并持久化到本地资产缓存。"""
    normalized_entity_id = canonical_av_id(av_id) or (safe_asset_stem(entity_id) if entity_id else "")
    _, _, data_dir = settings()
    if normalized_entity_id:
        asset = get_subscription_service().get_asset_cache(normalized_entity_id, "trailer")
        if asset:
            local = serve_asset_record(data_dir, asset)
            existing_url = str(asset.get("source_url") or "")
            if local and (not url or existing_url == str(url or "") or bool(asset.get("immutable"))):
                return local
    if not url:
        raise HTTPException(status_code=400, detail="url 参数不能为空")
    if not allowed_external_url(url, IMAGE_PROXY_HOSTS):
        raise HTTPException(status_code=403, detail="只允许代理指定域名媒体")
    try:
        if normalized_entity_id:
            return persist_media_asset(url, normalized_entity_id, immutable)
        content, media_type = fetch_media_bytes(url)
        return Response(content=content, media_type=media_type, headers=asset_cache_headers(False))
    except httpx.HTTPStatusError as exc:
        stale_asset = get_subscription_service().get_asset_cache(normalized_entity_id, "trailer") if normalized_entity_id else None
        if stale_asset:
            local = serve_asset_record(data_dir, stale_asset)
            if local:
                return local
        raise HTTPException(status_code=exc.response.status_code, detail="媒体获取失败") from exc
    except httpx.HTTPError as exc:
        stale_asset = get_subscription_service().get_asset_cache(normalized_entity_id, "trailer") if normalized_entity_id else None
        if stale_asset:
            local = serve_asset_record(data_dir, stale_asset)
            if local:
                return local
        raise HTTPException(status_code=502, detail=f"媒体代理失败: {exc}") from exc
