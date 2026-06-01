from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
import json
import uuid
import hashlib
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .log_service import AppLogService
from .scanner import ScanResult, scan_libraries
from .scan_state import scan_cache
from .storage import MoveRequest, MoveResult, Storage
from .mteam_service import download_mteam_torrent, search_mteam
from .subscription_service import SubscriptionService, date_is_after
from .system_settings import SystemSettingsService
from .javdb_service import javdb
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


def backend_headers() -> dict[str, str]:
    _, _, data_dir = settings()
    token = str(load_compute_config(data_dir).get("subtitle_backend_token", "")).strip()
    if not token:
        token = os.getenv("SUBTITLE_BACKEND_TOKEN", "").strip()
    return {"X-API-Key": token} if token else {}


def frontend_api_token() -> str:
    return os.getenv("SUBTITLE_API_TOKEN", "").strip()


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
        if not item.strip() or "=" not in item:
            continue
        source, target = item.split("=", 1)
        pairs.append((source.strip().replace("\\", "/").rstrip("/"), target.strip().rstrip("\\/")))
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
        with httpx.Client(timeout=30) as client:
            response = client.get(f"{backend_url()}{path}", headers=backend_headers())
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise_remote_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接字幕后端: {exc}") from exc


def remote_get_with_timeout(path: str, timeout: float = 3.0) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=timeout) as client:
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
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{backend_url()}{path}", headers=backend_headers(), json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise_remote_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接字幕后端: {exc}") from exc


def compute_settings_payload(settings_obj: Any, config: dict[str, Any] | None = None) -> dict[str, object]:
    config = config or {}
    return {
        "whisper_model": getattr(settings_obj, "default_model", config.get("whisper_model", "large-v3")),
        "whisper_model_dir": str(getattr(settings_obj, "model_dir", "") or config.get("whisper_model_dir", "")),
        "whisper_device": getattr(settings_obj, "device", config.get("whisper_device", "cuda")),
        "whisper_compute_type": getattr(settings_obj, "compute_type", config.get("whisper_compute_type", "float16")),
        "subtitle_max_workers": getattr(settings_obj, "max_workers", config.get("subtitle_max_workers", 1)),
        "subtitle_output_dir": str(getattr(settings_obj, "default_output_dir", "") or config.get("subtitle_output_dir", "")),
        "subtitle_path_map": config.get("subtitle_path_map", ""),
        "default_translate_backend": getattr(settings_obj, "default_translate_backend", config.get("default_translate_backend", "google")),
        "google_translate_url": getattr(settings_obj, "google_translate_url", config.get("google_translate_url", "https://translate.google.com/translate_a/single")),
        "deepl_api_url": getattr(settings_obj, "deepl_api_url", config.get("deepl_api_url", "https://api-free.deepl.com/v2/translate")),
        "deepl_api_key": getattr(settings_obj, "deepl_api_key", config.get("deepl_api_key", "")),
        "openai_base_url": getattr(settings_obj, "openai_base_url", config.get("openai_base_url", "")),
        "openai_api_key": getattr(settings_obj, "openai_api_key", config.get("openai_api_key", "")),
        "openai_model": getattr(settings_obj, "openai_model", config.get("openai_model", "gpt-4.1-mini")),
        "openai_batch_size": getattr(settings_obj, "openai_batch_size", config.get("openai_batch_size", 12)),
        "openai_max_concurrency": getattr(
            settings_obj,
            "openai_max_concurrency",
            config.get("openai_max_concurrency", 2),
        ),
        "openai_translation_style": getattr(
            settings_obj,
            "openai_translation_style",
            config.get("openai_translation_style", "adult_natural"),
        ),
        "openai_style_intensity": getattr(
            settings_obj,
            "openai_style_intensity",
            config.get("openai_style_intensity", "medium"),
        ),
        "openai_context_lines": getattr(settings_obj, "openai_context_lines", config.get("openai_context_lines", 2)),
        "openai_glossary": getattr(settings_obj, "openai_glossary", config.get("openai_glossary", "")),
        "ollama_url": getattr(settings_obj, "ollama_url", config.get("ollama_url", "")),
        "ollama_model": getattr(settings_obj, "ollama_model", config.get("ollama_model", "qwen2.5:7b")),
        "subtitle_api_token": getattr(settings_obj, "api_token", config.get("subtitle_api_token", "")),
    }


def console_settings_payload() -> dict[str, object]:
    """Return saved console settings without requiring a live worker."""
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    saved_settings = load_subtitle_settings(data_dir)
    return {
        **compute_settings_payload(saved_settings, config),
        "default_model": saved_settings.default_model,
        "model_dir": str(saved_settings.model_dir) if saved_settings.model_dir else "",
        "device": saved_settings.device,
        "compute_type": saved_settings.compute_type,
        "max_workers": saved_settings.max_workers,
        "default_output_dir": str(saved_settings.default_output_dir) if saved_settings.default_output_dir else "",
        "translation_backends": translation_backend_options(saved_settings),
        "local_models": [],
    }


def save_local_compute_settings(payload: dict[str, Any]) -> dict[str, object]:
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    config.update(payload)
    save_compute_config(data_dir, config)
    restarted = reset_subtitle_service_if_idle()
    if not restarted:
        print("[Media Toolbox] settings saved; restart required after active jobs finish", flush=True)
    else:
        print("[Media Toolbox] settings saved and reloaded", flush=True)
    return {"status": "ok", "restart_required": not restarted}


def save_console_compute_config(payload: dict[str, Any]) -> dict[str, Any]:
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    config.update(payload)
    save_compute_config(data_dir, config)
    return config


app = FastAPI(title="媒体工具箱")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

move_jobs: dict[str, dict[str, Any]] = {}
move_jobs_lock = threading.Lock()


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
        return remote_post_json("/api/subtitle/jobs", rewrite_subtitle_payload(payload))
    service = get_subtitle_service()
    job = service.create_job(**payload)
    return job_payload(job)


def submit_subtitle_jobs_bulk(payloads: list[dict[str, object]]) -> dict[str, object]:
    if not payloads:
        return {"status": "ok", "submitted": 0, "jobs": []}
    if backend_url():
        rewritten = [rewrite_subtitle_payload(dict(payload)) for payload in payloads]
        return remote_post_json("/api/subtitle/jobs/bulk", {"jobs": rewritten}, timeout=120)
    service = get_subtitle_service()
    jobs = service.create_jobs([dict(payload) for payload in payloads])
    return {"status": "ok", "submitted": len(jobs), "jobs": [job_payload(job) for job in jobs]}


def submit_subtitle_jobs_bulk_background(payloads: list[dict[str, object]]) -> None:
    try:
        result = submit_subtitle_jobs_bulk(payloads)
        print(
            f"[Media Toolbox] bulk subtitle submission accepted submitted={result.get('submitted', 0)}",
            flush=True,
        )
    except Exception as exc:
        print(f"[Media Toolbox] bulk subtitle submission failed: {exc}", flush=True)


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
    return {
        "status": "ok",
        "online": True,
        "mode": "local",
        "settings": {
            **compute_settings,
            "default_model": service.settings.default_model,
            "model_dir": str(service.settings.model_dir) if service.settings.model_dir else "",
            "device": service.settings.device,
            "compute_type": service.settings.compute_type,
            "max_workers": service.settings.max_workers,
            "default_output_dir": str(service.settings.default_output_dir) if service.settings.default_output_dir else "",
            "translation_backends": translation_backend_options(service.settings),
            "local_models": local_model_dirs(service.settings.model_dir),
        },
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
        "path_map": service.settings.path_map,
        "updated_at": time.time(),
    }


def offline_backend_status(error: str) -> dict[str, object]:
    return {
        "status": "offline",
        "online": False,
        "mode": "remote",
        "backend_url": backend_url(),
        "error": error,
        "settings": console_settings_payload(),
        "hardware": None,
        "jobs": {"total": 0, "active": 0, "items": []},
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
        status["online"] = True
        status["mode"] = "remote"
        status["backend_url"] = backend_url()
        return status
    except HTTPException as exc:
        return {
            "status": "offline",
            "online": False,
            "mode": "remote",
            "backend_url": backend_url(),
            "error": str(exc.detail),
            "settings": console_settings_payload(),
            "hardware": None,
            "jobs": {"total": 0, "active": 0, "items": []},
            "path_map": [],
            "updated_at": time.time(),
        }


def subtitle_console_payload() -> dict[str, object]:
    _, _, data_dir = settings()
    console_config = load_compute_config(data_dir)
    status = subtitle_backend_status()
    jobs: list[Any] = []
    backend_error = None
    if backend_url():
        if status.get("online"):
            payload, backend_error = remote_get_safe("/api/subtitle/jobs?limit=0")
            jobs = list(payload.get("jobs", [])) if payload else []
        else:
            backend_error = str(status.get("error") or "后端暂不可用")
    else:
        jobs = [job_payload(job) for job in get_subtitle_service().list_jobs()]
    visible_settings = status.get("settings") or console_settings_payload()
    return {
        "connection": {
            "subtitle_backend_url": backend_url() or str(console_config.get("subtitle_backend_url", "")),
            "subtitle_backend_token": str(console_config.get("subtitle_backend_token", "")),
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
        ],
        "integrations": integrations,
        "logs": logs[:6],
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> Response:
    if compute_node_only():
        return Response("Media Toolbox compute node is running. Use the Unraid console to manage settings.", media_type="text/plain")
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"dashboard": dashboard_payload()},
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, view: str = "duplicates") -> Response:
    if compute_node_only():
        return Response("Media Toolbox compute node is running. Use the Unraid console to manage settings.", media_type="text/plain")
    media_dirs, trash_dir, data_dir = settings()
    scan_cache.configure(data_dir)
    scan_dirs = selectable_scan_dirs(media_dirs, [trash_dir])
    snapshot = scan_cache.snapshot()
    result = snapshot.result or ScanResult(tuple(), 0, 0, tuple(media_dirs), tuple(), tuple())
    store = Storage(data_dir, trash_dir, media_dirs)
    recent_moves = store.recent_moves()
    duplicate_keys = {group.key for group in result.groups}
    single_files = tuple(file for file in result.files if file.group_key not in duplicate_keys)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "media_dirs": media_dirs,
            "trash_dir": trash_dir,
            "result": result,
            "single_files": single_files,
            "scan_status": snapshot,
            "scan_dirs": scan_dirs,
            "recent_moves": recent_moves,
        },
    )


@app.get("/api/scan")
def api_scan() -> dict[str, object]:
    media_dirs, trash_dir, data_dir = settings()
    scan_cache.configure(data_dir)
    snapshot = scan_cache.snapshot()
    result = snapshot.result or ScanResult(tuple(), 0, 0, tuple(media_dirs), tuple(), tuple())
    return {
        "status": snapshot.status,
        "started_at": snapshot.started_at,
        "finished_at": snapshot.finished_at,
        "error": snapshot.error,
        "progress": snapshot.progress,
        "processed_files": snapshot.processed_files,
        "scan_total_files": snapshot.total_files,
        "current_path": snapshot.current_path,
        "active_scan_dirs": [str(path) for path in snapshot.scanned_dirs],
        "selectable_scan_dirs": [str(path) for path in selectable_scan_dirs(media_dirs, [trash_dir])],
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
            if file.group_key not in {group.key for group in result.groups}
        ],
    }


@app.post("/api/scan/run")
def api_scan_run(paths: list[str] = Form(default=[])) -> dict[str, object]:
    media_dirs, trash_dir, data_dir = settings()
    scan_cache.configure(data_dir)
    scan_dirs = selected_scan_dirs(media_dirs, paths, [trash_dir])
    if not scan_dirs:
        raise HTTPException(status_code=400, detail="请至少选择一个媒体子目录")
    started = scan_cache.start(scan_dirs, force=True, excluded_dirs=[trash_dir])
    return {"status": "running", "started": started, "scan_dirs": [str(path) for path in scan_dirs]}


@app.post("/scan/run")
def scan_run(paths: list[str] = Form(default=[])) -> RedirectResponse:
    media_dirs, trash_dir, data_dir = settings()
    scan_cache.configure(data_dir)
    scan_dirs = selected_scan_dirs(media_dirs, paths, [trash_dir])
    if scan_dirs:
        scan_cache.start(scan_dirs, force=True, excluded_dirs=[trash_dir])
    return RedirectResponse("/", status_code=303)


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
        print(f"[Media Toolbox] failed to submit subtitle jobs: {exc}", flush=True)
        submitted = 0
        failed = len(payloads)
    return RedirectResponse(f"/subtitles?submitted={submitted}&failed={failed}", status_code=303)


@app.get("/terminal", response_class=HTMLResponse)
def terminal_console(request: Request, saved: str = "", restart: str = "") -> HTMLResponse:
    if compute_node_only():
        raise HTTPException(status_code=404, detail="Windows 算力端不提供 Web 控制台，请在 Unraid 字幕算力控制台管理设置。")
    _, _, data_dir = settings()
    service = get_subtitle_service()
    status = local_node_status()
    local_models = [
        {**item, "size_label": bytes_label(int(item.get("size") or 0))}
        for item in local_model_dirs(service.settings.model_dir)
    ]
    return templates.TemplateResponse(
        "terminal.html",
        {
            "request": request,
            "status": status,
            "settings": service.settings,
            "config": load_compute_config(data_dir),
            "translation_backends": translation_backend_options(service.settings),
            "model_options": whisper_model_options(),
            "local_models": local_models,
            "saved": saved == "1",
            "restart_pending": restart == "1",
        },
    )


@app.post("/terminal/settings")
def save_terminal_settings(
    whisper_model: str = Form(default="large-v3"),
    whisper_model_dir: str = Form(default=""),
    whisper_device: str = Form(default="cuda"),
    whisper_compute_type: str = Form(default="float16"),
    subtitle_max_workers: int = Form(default=1),
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
    return RedirectResponse(f"/terminal?{suffix}", status_code=303)


@app.get("/subtitles", response_class=HTMLResponse)
def subtitles(
    request: Request,
    batch: str | None = None,
    submitted: int = 0,
    failed: int = 0,
) -> Response:
    if compute_node_only():
        return Response("Media Toolbox compute node is running. Use the Unraid console to manage subtitle jobs.", media_type="text/plain")
    frontend_index = FRONTEND_DIST / "index.html"
    if frontend_index.exists():
        return FileResponse(frontend_index)
    backend_error = None
    _, _, data_dir = settings()
    console_config = load_compute_config(data_dir)
    pending_batch = None
    backend_status = subtitle_backend_status()
    if backend_url():
        if backend_status.get("online"):
            payload, backend_error = remote_get_safe("/api/subtitle/jobs?limit=0")
            jobs = payload.get("jobs", []) if payload else []
        else:
            backend_error = str(backend_status.get("error") or "后端暂不可用")
            jobs = []
        current_settings: Any = (backend_status.get("settings") or remote_settings())
    else:
        service = get_subtitle_service()
        jobs = service.list_jobs()
        current_settings = service.settings
    return templates.TemplateResponse(
        "subtitles.html",
        {
            "request": request,
            "jobs": jobs,
            "settings": current_settings,
            "backend_url": backend_url(),
            "backend_url_locked": False,
            "backend_token_locked": False,
            "console_backend_url": backend_url() or str(console_config.get("subtitle_backend_url", "")),
            "console_backend_token": str(console_config.get("subtitle_backend_token", "")),
            "subtitle_public_url": subtitle_public_url(),
            "backend_error": backend_error,
            "backend_status": backend_status,
            "path_preview": backend_path_preview(remote_status=backend_status if backend_status.get("online") else None),
            "pending_batch": pending_batch,
            "submitted_count": submitted,
            "failed_count": failed,
            "translation_backends": (backend_status.get("settings", {}) or {}).get("translation_backends")
            or translation_backend_options(None),
            "compute_settings": (backend_status.get("settings", {}) or {}),
            "model_options": whisper_model_options(),
        },
    )


@app.get("/subtitles/compare", response_class=HTMLResponse)
def subtitle_compare_page() -> Response:
    if compute_node_only():
        return Response("Media Toolbox compute node is running. Use the Unraid console to compare subtitle translations.", media_type="text/plain")
    frontend_index = FRONTEND_DIST / "index.html"
    if frontend_index.exists():
        return FileResponse(frontend_index)
    raise HTTPException(status_code=404, detail="前端资源不存在")


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
    save_console_compute_config(
        {
            "subtitle_backend_url": str(payload.get("subtitle_backend_url", "")).strip(),
            "subtitle_backend_token": str(payload.get("subtitle_backend_token", "")).strip(),
        }
    )
    return {
        "status": "ok",
        "connection": {
            "subtitle_backend_url": backend_url(),
            "subtitle_backend_token": backend_headers().get("X-API-Key", ""),
        },
        "backend_status": subtitle_backend_status(),
    }


@app.post("/api/subtitle/settings")
async def api_save_subtitle_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="算力端设置格式不正确")
    save_console_compute_config(payload)
    if backend_url():
        try:
            result = remote_post_json("/api/compute/settings", payload)
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
            "remote": result,
            "settings": result.get("settings", payload),
            "backend_status": subtitle_backend_status(),
        }
    result = save_local_compute_settings(payload)
    service = get_subtitle_service()
    _, _, data_dir = settings()
    return {
        **result,
        "settings": compute_settings_payload(service.settings, load_compute_config(data_dir)),
        "backend_status": subtitle_backend_status(),
    }


@app.post("/api/subtitle/translate/test", dependencies=[Depends(require_subtitle_token)])
async def api_test_translate_backend(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="翻译测试请求格式不正确")
    if backend_url():
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
        with httpx.Client(timeout=8.0) as client:
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


@app.post("/preview", response_class=HTMLResponse)
def preview(request: Request, paths: list[str] = Form(default=[])) -> HTMLResponse:
    media_dirs, trash_dir, data_dir = settings()
    store = Storage(data_dir, trash_dir, media_dirs)
    selected = store.preview(paths)
    return templates.TemplateResponse(
        "preview.html",
        {
            "request": request,
            "trash_dir": trash_dir,
            "selected": selected,
        },
    )


@app.post("/move")
def move(paths: list[str] = Form(default=[])) -> RedirectResponse:
    job_id = create_move_job(paths)
    return RedirectResponse(f"/move/jobs/{job_id}", status_code=303)


@app.post("/move/jobs")
def start_move_job(paths: list[str] = Form(default=[])) -> RedirectResponse:
    job_id = create_move_job(paths)
    return RedirectResponse(f"/move/jobs/{job_id}", status_code=303)


@app.get("/move/jobs/{job_id}", response_class=HTMLResponse)
def move_job_page(request: Request, job_id: str) -> HTMLResponse:
    job = move_job_snapshot(job_id)
    return templates.TemplateResponse(
        "move_job.html",
        {
            "request": request,
            "job": job,
        },
    )


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
        "settings": compute_settings_payload(service.settings, load_compute_config(data_dir)),
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
        "settings": compute_settings_payload(service.settings, load_compute_config(data_dir)),
    }


@app.get("/api/subtitle/jobs", dependencies=[Depends(require_subtitle_token)])
def api_list_subtitle_jobs(limit: int = 0) -> dict[str, object]:
    if backend_url():
        suffix = f"?limit={limit}" if limit else "?limit=0"
        return remote_get(f"/api/subtitle/jobs{suffix}")
    service = get_subtitle_service()
    jobs = [job_payload(job) for job in service.list_jobs(limit or None)]
    active = [job for job in jobs if job.get("status") in {"queued", "running", "translating"}]
    return {"jobs": jobs, "total": len(jobs), "active": len(active)}


@app.post("/api/subtitle/jobs", dependencies=[Depends(require_subtitle_token)])
def api_create_subtitle_job(payload: SubtitleJobCreate) -> dict[str, object]:
    if backend_url():
        return remote_post_json("/api/subtitle/jobs", rewrite_subtitle_payload(payload.model_dump()))
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
        return remote_post_json("/api/subtitle/jobs/bulk", {"jobs": rewritten}, timeout=120)
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
        return remote_post_json("/api/subtitle/jobs/retry-failed", retry_payload)
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
        return remote_post_json(f"/api/subtitle/jobs/{job_id}/retry", retry_payload)
    try:
        job = get_subtitle_service().retry_job(job_id, translate_backend=translate_backend)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "job": job_payload(job)}


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
            with httpx.Client(timeout=None) as client:
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
        with httpx.Client(timeout=120) as client:
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
# 订阅功能 API（纯 javdb 数据源）
# ============================================================

subscription_service: SubscriptionService | None = None
system_settings_service: SystemSettingsService | None = None
app_log_service: AppLogService | None = None
subscription_poll_thread: threading.Thread | None = None
subscription_poll_stop = threading.Event()
IMAGE_PROXY_HOSTS = ("javbus.com", "javdb.com", "jdbstatic.com", "dmm.co.jp", "libredmm.com")
JAVDB_HOSTS = ("javdb.com",)


def allowed_external_url(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def get_subscription_service() -> SubscriptionService:
    global subscription_service
    if subscription_service is None:
        _, _, data_dir = settings()
        subscription_service = SubscriptionService(data_dir)
    return subscription_service


def get_system_settings_service() -> SystemSettingsService:
    global system_settings_service
    if system_settings_service is None:
        _, _, data_dir = settings()
        system_settings_service = SystemSettingsService(data_dir)
    return system_settings_service


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


javdb.set_logger(app_log)


def is_vr_work(av: dict[str, Any]) -> bool:
    return "VR" in str(av.get("title") or "").upper()


def poll_subscriptions_once() -> dict[str, Any]:
    service = get_subscription_service()
    sub_settings = service.get_settings()
    max_coactors = int(sub_settings.get("max_coactors") or 2)
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
            avs = javdb.get_actress_avs(actress_id, limit=100)
            since_date = str(actress.get("since_date") or "")
            for av in avs:
                if not date_is_after(str(av.get("date") or ""), since_date):
                    continue
                av_id = str(av.get("id") or "")
                if not av_id or service.is_av_subscribed(av_id):
                    continue
                if not actress.get("include_vr", False) and is_vr_work(av):
                    app_log("info", "subscription", "跳过 VR 女优作品", {"av_id": av_id, "actress_id": actress_id})
                    continue
                actors = javdb.get_av_actresses(str(av.get("url") or "")) if av.get("url") else []
                if actors and len(actors) > max_coactors:
                    app_log("info", "subscription", "跳过超过共演人数限制的番号", {"av_id": av_id, "actor_count": len(actors)})
                    continue
                payload = dict(av)
                payload["auto_subscribed"] = True
                payload["source_actress_id"] = actress_id
                payload["source_actress_name"] = actress.get("name", "")
                payload["actresses"] = [actor.get("name", "") for actor in actors] or [actress.get("name", "")]
                apply_jellyfin_status(payload)
                saved = service.subscribe_av(payload)
                added.append(saved)
                app_log("info", "subscription", "自动订阅新增番号", {"av_id": av_id, "status": saved.get("status"), "actress": actress.get("name", "")})
                new_count += 1
            service.mark_actress_polled(actress_id, new_count)
        except Exception as exc:
            errors.append(f"{actress.get('name') or actress_id}: {exc}")
            app_log("error", "subscription", "女优轮询失败", {"actress_id": actress_id, "error": str(exc)})
    service.mark_global_poll()
    app_log("info", "subscription", "订阅轮询完成", {"checked": len(actresses), "added": len(added), "errors": len(errors)})
    return {"checked": len(actresses), "added": added, "errors": errors}


def subscribe_latest_for_actress(actress: dict[str, Any], *, future_only: bool = False) -> dict[str, Any]:
    service = get_subscription_service()
    sub_settings = service.get_settings()
    max_coactors = int(sub_settings.get("max_coactors") or 2)
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
        "include_vr": bool(actress.get("include_vr", False)),
    })
    try:
        avs = javdb.get_actress_avs(actress_id, limit=100)
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
        av_id = str(av.get("id") or "")
        release_date = str(av.get("date") or "")
        if not av_id:
            continue
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
            actors = javdb.get_av_actresses(str(av.get("url") or ""), include_profiles=False) if av.get("url") else []
            if actors and len(actors) > max_coactors:
                skipped.append({"id": av_id, "reason": f"共演人数 {len(actors)} 超过限制"})
                continue
            payload = dict(av)
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
                "release_date": release_date,
                "status": saved.get("status"),
            })
            if saved.get("status") != "in_library":
                download_av_from_mteam(saved)
        except Exception as exc:
            errors.append(f"{av_id}: {exc}")
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
    items = [item for item in service.get_subscribed_av() if item.get("status") != "in_library"][:limit]
    for item in items:
        updated = refresh_subscription_library_status(item)
        if updated.get("status") == "in_library":
            changed += 1
    if changed:
        app_log("info", "jellyfin", "刷新订阅入库状态完成", {"stage": "jellyfin_refresh_done", "changed": changed})
    return changed


def download_pending_subscriptions() -> dict[str, Any]:
    service = get_subscription_service()
    items = [item for item in service.get_subscribed_av() if item.get("status", "pending") == "pending"]
    app_log("info", "download", "开始一键下载订阅中番号", {"stage": "bulk_download_start", "count": len(items)})
    results = [download_av_from_mteam(item) for item in items]
    sent = len([item for item in results if item.get("status") in {"ok", "exists", "sent"}])
    app_log("info", "download", "一键下载完成", {"stage": "bulk_download_done", "count": len(results), "sent": sent})
    return {"results": results, "checked": len(items), "sent": sent}


def refresh_pinned_makers() -> dict[str, Any]:
    makers = get_subscription_service().get_settings().get("pinned_makers") or []
    refreshed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    app_log("info", "maker", "开始刷新常驻厂牌", {"stage": "maker_refresh_start", "count": len(makers)})
    for maker in makers:
        name = str(maker.get("name") or "")
        url = str(maker.get("url") or "")
        try:
            results = javdb.get_listing(url, force_refresh=True)
            refreshed.append({"name": name, "url": url, "count": len(results)})
            app_log("info", "maker", "厂牌刷新完成", {"stage": "maker_refresh_item_done", "name": name, "count": len(results)})
        except Exception as exc:
            errors.append({"name": name, "error": str(exc)})
            app_log("error", "maker", "厂牌刷新失败", {"stage": "maker_refresh_item_error", "name": name, "error": str(exc)})
    app_log("info", "maker", "常驻厂牌刷新完成", {"stage": "maker_refresh_done", "refreshed": len(refreshed), "errors": len(errors)})
    return {"refreshed": refreshed, "errors": errors}


def subscription_tasks_payload() -> list[dict[str, Any]]:
    sub_settings = get_subscription_service().get_settings()
    return [
        {
            "id": "actress_poll",
            "name": "女优订阅轮询",
            "cron": sub_settings.get("actress_cron") or "0 21 * * *",
            "last_run_at": sub_settings.get("last_poll_at") or 0,
            "description": "检查已订阅女优在限制日期之后的新番号。",
        },
        {
            "id": "av_download",
            "name": "番号订阅下载",
            "cron": sub_settings.get("av_cron") or "0 22 * * *",
            "last_run_at": sub_settings.get("last_av_poll_at") or 0,
            "description": "为订阅中的番号检查 Jellyfin、搜索 MTeam 并推送 qBittorrent。",
        },
        {
            "id": "maker_refresh",
            "name": "厂牌发售更新",
            "cron": sub_settings.get("maker_cron") or "0 */6 * * *",
            "last_run_at": sub_settings.get("last_maker_poll_at") or 0,
            "description": "刷新订阅设置中常驻厂牌的最近发售缓存。",
        },
    ]


def run_subscription_task(task_id: str, *, minute_key: str | None = None) -> dict[str, Any]:
    service = get_subscription_service()
    app_log("info", "task", "开始执行定时任务", {"stage": "task_start", "task_id": task_id})
    if task_id == "actress_poll":
        result = poll_subscriptions_once()
    elif task_id == "av_download":
        result = download_pending_subscriptions()
    elif task_id == "maker_refresh":
        result = refresh_pinned_makers()
    else:
        raise HTTPException(status_code=404, detail="未知订阅任务")
    service.mark_task_poll(task_id, minute_key)
    app_log("info", "task", "定时任务执行完成", {"stage": "task_done", "task_id": task_id})
    return result


def subscription_poll_loop() -> None:
    while not subscription_poll_stop.is_set():
        try:
            service = get_subscription_service()
            sub_settings = service.get_settings()
            if sub_settings.get("poll_enabled", True):
                now = datetime.now()
                minute_key = now.strftime("%Y-%m-%d %H:%M")
                schedules = (
                    ("actress_poll", "actress_cron", "last_poll_minute"),
                    ("av_download", "av_cron", "last_av_poll_minute"),
                    ("maker_refresh", "maker_cron", "last_maker_poll_minute"),
                )
                for task_id, cron_key, last_minute_key in schedules:
                    if sub_settings.get(last_minute_key) != minute_key and cron_matches(str(sub_settings.get(cron_key) or ""), now):
                        run_subscription_task(task_id, minute_key=minute_key)
        except Exception as exc:
            print(f"[SubscriptionPoll] error: {exc}", flush=True)
        subscription_poll_stop.wait(300)


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
    app_log("info", "jellyfin", "Jellyfin 查重命中，标记已入库", {"av_id": av.get("id", ""), "item": match.get("name", "")})


def refresh_subscription_library_status(av: dict[str, Any]) -> dict[str, Any]:
    probe = dict(av)
    apply_jellyfin_status(probe)
    if probe.get("status") == "in_library":
        saved = get_subscription_service().update_av_download(str(probe.get("id") or ""), {
            "status": "in_library",
            "library_status": "in_library",
            "jellyfin_item_id": probe.get("jellyfin_item_id", ""),
            "jellyfin_item_name": probe.get("jellyfin_item_name", ""),
        })
        return saved or probe
    return av


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

    settings_data = get_system_settings_service().get()
    service = get_subscription_service()
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
    if not torrent_id:
        message = "MTeam 结果缺少种子 ID"
        app_log("error", "mteam", message, {"stage": "mteam_missing_id", "av_id": av_id, "title": torrent_title})
        if save_to_subscription:
            service.update_av_download(av_id, {"download_status": "error", "download_message": message})
        return {"av_id": av_id, "status": "error", "message": message}

    app_log("info", "mteam", "开始下载 MTeam 种子文件", {"stage": "mteam_torrent_download_start", "av_id": av_id, "torrent_id": torrent_id, "title": torrent_title})
    try:
        torrent_bytes, filename = download_mteam_torrent(torrent_id, settings_data)
        app_log("info", "qbittorrent", "开始推送种子到 qBittorrent", {"stage": "qb_add_start", "av_id": av_id, "torrent_id": torrent_id, "filename": filename})
        qb_result = add_torrent_to_qbittorrent(torrent_bytes, filename, settings_data.get("qbittorrent", {}))
        qb_status = str(qb_result.get("status") or "ok")
        payload = {
            "status": "done" if qb_status in ("ok", "exists", "sent") else str(av.get("status") or "pending"),
            "download_status": qb_status,
            "download_message": qb_result.get("message", "已发送到 qBittorrent"),
            "mteam_torrent_id": torrent_id,
            "mteam_torrent_title": torrent_title,
            "downloaded_at": time.time(),
        }
        if save_to_subscription:
            service.update_av_download(av_id, payload)
        app_log("info", "download", "下载链路完成", {"stage": "download_done", "av_id": av_id, "torrent_id": torrent_id, "status": qb_status, "message": payload["download_message"]})
        return {"av_id": av_id, "status": qb_status, "message": payload["download_message"], "torrent": torrent}
    except Exception as exc:
        message = str(exc)
        if save_to_subscription:
            service.update_av_download(av_id, {"download_status": "error", "download_message": message, "mteam_torrent_id": torrent_id, "mteam_torrent_title": torrent_title})
        app_log("error", "download", "下载链路失败", {"stage": "download_error", "av_id": av_id, "torrent_id": torrent_id, "error": message})
        return {"av_id": av_id, "status": "error", "message": message, "torrent": torrent}


def choose_mteam_torrent(av_id: str, torrents: list[dict[str, Any]]) -> dict[str, Any]:
    av_lower = av_id.lower()
    for item in torrents:
        if av_lower in str(item.get("title") or "").lower():
            return item
    return torrents[0]


def filter_mteam_results(torrents: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    if not filters:
        return torrents
    return [item for item in torrents if mteam_item_matches_filters(item, filters)]


def mteam_item_matches_filters(item: dict[str, Any], filters: dict[str, Any]) -> bool:
    haystack = mteam_item_text(item)
    if filters.get("only_chinese") and not contains_any(haystack, ("中字", "中文", "字幕", "chinese", "chs", "cht", "sub")):
        return False
    if filters.get("only_uncensored") and not contains_any(haystack, ("无码", "無碼", "uncensored")):
        return False
    if filters.get("exclude_uncensored") and contains_any(haystack, ("无码", "無碼", "uncensored")):
        return False
    if filters.get("only_free") and not contains_any(haystack, ("免费", "免費", "free", "freeleech")):
        return False
    if filters.get("only_uhd") and not contains_any(haystack, ("uhd", "4k", "2160", "2160p")):
        return False
    if filters.get("exclude_uhd") and contains_any(haystack, ("uhd", "4k", "2160", "2160p")):
        return False
    size_mb = mteam_size_mb(item.get("size"))
    min_size = int(filters.get("min_size_mb") or 0)
    max_size = int(filters.get("max_size_mb") or 0)
    if min_size and (not size_mb or size_mb < min_size):
        return False
    if max_size and size_mb and size_mb > max_size:
        return False
    return True


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
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        username = str(config.get("username") or "")
        password = str(config.get("password") or "")
        if username or password:
            login = client.post(f"{base_url}/api/v2/auth/login", data={"username": username, "password": password})
            login.raise_for_status()
            if "Ok." not in login.text:
                raise RuntimeError(f"qBittorrent 登录失败: {login.text.strip()}")
        if info_hash:
            existing = client.get(f"{base_url}/api/v2/torrents/info", params={"hashes": info_hash})
            existing.raise_for_status()
            items = existing.json()
            if items:
                name = items[0].get("name") or filename
                return {"status": "exists", "message": f"qBittorrent 已存在: {name}", "hash": info_hash}
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
                    return {"status": "exists", "message": f"qBittorrent 已存在: {name}", "hash": info_hash}
            raise RuntimeError(f"qBittorrent 添加失败: {text}")
    return {"status": "ok", "message": "已发送到 qBittorrent", "hash": info_hash}


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


def find_jellyfin_match(av_id: str, title: str, config: dict[str, Any]) -> dict[str, str] | None:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    if not base_url or not api_key or not av_id:
        return None
    user_id = get_jellyfin_user_id(config)
    search_terms = [av_id]
    if title and title != av_id:
        search_terms.append(title)
    headers = {"X-Emby-Token": api_key}
    library_id = str(config.get("library_id") or "").strip()
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
                        return {"id": str(item.get("Id") or ""), "name": name}
    except (httpx.HTTPError, ValueError):
        return None
    return None


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


@app.on_event("startup")
def start_subscription_polling() -> None:
    global subscription_poll_thread
    if subscription_poll_thread is None:
        subscription_poll_thread = threading.Thread(target=subscription_poll_loop, name="subscription-poll", daemon=True)
        subscription_poll_thread.start()


@app.on_event("shutdown")
def stop_subscription_polling() -> None:
    subscription_poll_stop.set()


@app.get("/subscriptions", response_class=HTMLResponse)
def subscriptions_page(request: Request) -> Response:
    """订阅管理页面"""
    if compute_node_only():
        return Response("Media Toolbox compute node is running.", media_type="text/plain")
    service = get_subscription_service()
    refresh_library_status_for_subscriptions()
    return templates.TemplateResponse(
        request=request,
        name="subscriptions.html",
        context={
            "page_mode": "subscriptions",
            "subscribed_av": service.get_subscribed_av(),
            "subscribed_actresses": service.get_subscribed_actresses(),
            "subscription_settings": service.get_settings(),
        },
    )


@app.get("/subscription-search", response_class=HTMLResponse)
def subscription_search_page(request: Request) -> Response:
    if compute_node_only():
        return Response("Media Toolbox compute node is running.", media_type="text/plain")
    service = get_subscription_service()
    return templates.TemplateResponse(
        request=request,
        name="subscriptions.html",
        context={
            "page_mode": "search",
            "subscribed_av": service.get_subscribed_av(),
            "subscribed_actresses": service.get_subscribed_actresses(),
            "subscription_settings": service.get_settings(),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def legacy_settings_page() -> RedirectResponse:
    return RedirectResponse("/subscription-settings", status_code=307)


@app.get("/subscription-settings", response_class=HTMLResponse)
def settings_page(request: Request) -> Response:
    """订阅设置页面"""
    if compute_node_only():
        return Response("Media Toolbox compute node is running.", media_type="text/plain")
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "system_settings": get_system_settings_service().get(),
            "subscription_settings": get_subscription_service().get_settings(),
        },
    )


@app.get("/makers", response_class=HTMLResponse)
def makers_page(request: Request) -> Response:
    if compute_node_only():
        return Response("Media Toolbox compute node is running.", media_type="text/plain")
    return templates.TemplateResponse(
        request=request,
        name="makers.html",
        context={"makers": get_subscription_service().get_settings().get("pinned_makers") or []},
    )


@app.get("/subscription-tasks", response_class=HTMLResponse)
def subscription_tasks_page(request: Request) -> Response:
    if compute_node_only():
        return Response("Media Toolbox compute node is running.", media_type="text/plain")
    return templates.TemplateResponse(
        request=request,
        name="subscription_tasks.html",
        context={
            "tasks": subscription_tasks_payload(),
            "subscription_settings": get_subscription_service().get_settings(),
        },
    )


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> Response:
    """日志系统页面"""
    if compute_node_only():
        return Response("Media Toolbox compute node is running.", media_type="text/plain")
    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={"logs": get_app_log_service().recent(300)},
    )


NOTIFICATION_EVENTS: tuple[dict[str, str], ...] = (
    {"key": "actress_new_av", "name": "女优订阅发现新番号", "description": "轮询订阅女优时发现符合条件的新作品。"},
    {"key": "av_subscribed", "name": "番号已加入订阅", "description": "手动或自动新增番号订阅后发送。"},
    {"key": "mteam_found", "name": "MTeam 命中资源", "description": "订阅番号搜索到可下载资源时发送。"},
    {"key": "torrent_sent", "name": "种子已推送下载器", "description": "种子成功发送到 qBittorrent 后发送。"},
    {"key": "jellyfin_in_library", "name": "Jellyfin 已入库", "description": "订阅番号确认已经在媒体库中时发送。"},
    {"key": "task_failed", "name": "任务失败告警", "description": "订阅轮询、下载、集成测试等链路失败时发送。"},
    {"key": "scan_completed", "name": "重复视频扫描完成", "description": "重复视频扫描结束后发送摘要。"},
    {"key": "subtitle_completed", "name": "字幕任务完成", "description": "字幕生成或翻译完成后发送。"},
    {"key": "subtitle_failed", "name": "字幕任务失败", "description": "字幕生成、翻译失败后发送。"},
)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request) -> Response:
    if compute_node_only():
        return Response("Media Toolbox compute node is running.", media_type="text/plain")
    return templates.TemplateResponse(
        request=request,
        name="notifications.html",
        context={
            "system_settings": get_system_settings_service().get(),
            "notification_events": NOTIFICATION_EVENTS,
        },
    )


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


@app.get("/api/subscriptions/search")
def api_search_subscriptions(q: str = "", type: str = "av", include_mteam: bool = False) -> dict[str, object]:
    """搜索番号或女优（数据来自 javdb）"""
    if not q.strip():
        return {"results": [], "type": type}
    try:
        mteam = search_mteam(q.strip(), get_system_settings_service().get()) if include_mteam else None
        if type == "actress":
            results = javdb.search_actress(q.strip())
            return {"results": results, "type": "actress", "mteam": mteam}
        else:
            results = javdb.search_av(q.strip())
            return {"results": results, "type": "av", "mteam": mteam}
    except Exception as e:
        print(f"[API] search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/subscriptions/av")
async def api_subscribe_av(request: Request) -> dict[str, object]:
    """订阅番号"""
    payload = await request.json()
    if not isinstance(payload, dict) or not payload.get("id"):
        raise HTTPException(status_code=400, detail="番号信息格式不正确")
    app_log("info", "subscription", "开始订阅番号", {"stage": "subscribe_start", "av_id": payload.get("id")})
    apply_jellyfin_status(payload)
    service = get_subscription_service()
    result = service.subscribe_av(payload)
    download_result = {"status": "skipped", "message": ""}
    if result.get("status") != "in_library":
        download_result = download_av_from_mteam(result)
        latest = next((item for item in service.get_subscribed_av() if item.get("id") == result.get("id")), result)
        result = latest
    app_log("info", "subscription", "订阅番号完成", {"stage": "subscribe_done", "av_id": result.get("id"), "status": result.get("status"), "download_status": download_result.get("status")})
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
    return {"subscriptions": service.get_subscribed_av()}


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


@app.post("/api/subscriptions/actress")
async def api_subscribe_actress(request: Request) -> dict[str, object]:
    """订阅女优"""
    payload = await request.json()
    if not isinstance(payload, dict) or not payload.get("id"):
        raise HTTPException(status_code=400, detail="女优信息格式不正确")
    service = get_subscription_service()
    result = service.subscribe_actress(payload)
    app_log("info", "subscription", "订阅女优", {"actress_id": result.get("id"), "name": result.get("name"), "since_date": result.get("since_date")})
    latest = subscribe_latest_for_actress(result, future_only=True)
    app_log("info", "subscription", "订阅女优并扫描未发售番号完成", {
        "stage": "actress_subscribe_done",
        "actress_id": result.get("id"),
        "name": result.get("name"),
        "added": len(latest.get("added") or []),
        "skipped": len(latest.get("skipped") or []),
        "errors": len(latest.get("errors") or []),
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
def api_subscribe_actress_latest(actress_id: str) -> dict[str, object]:
    service = get_subscription_service()
    actress = next((item for item in service.get_subscribed_actresses() if item.get("id") == actress_id), None)
    if not actress:
        raise HTTPException(status_code=404, detail="女优未订阅")
    result = subscribe_latest_for_actress(actress, future_only=True)
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
    return {"subscriptions": service.get_subscribed_actresses()}


@app.get("/api/subscriptions/actress/{actress_id}/profile")
def api_get_actress_profile(actress_id: str) -> dict[str, object]:
    if not actress_id:
        raise HTTPException(status_code=400, detail="actress_id required")
    profile = javdb.get_actress_profile(actress_id)
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
    """获取女优全部作品（javdb 女优页面）"""
    try:
        results = javdb.get_actress_avs(actress_id)
        return {"results": results}
    except Exception as e:
        print(f"[API] actress_avs error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/subscriptions/av/actresses")
def api_get_av_actresses(url: str = "", profiles: bool = True) -> dict[str, object]:
    """从番号详情页获取女优列表"""
    if not url:
        raise HTTPException(status_code=400, detail="url 参数不能为空")
    if not allowed_external_url(url, JAVDB_HOSTS):
        raise HTTPException(status_code=403, detail="只允许访问 javdb 详情页")
    try:
        actresses = javdb.get_av_actresses(url, include_profiles=profiles)
        return {"actresses": actresses}
    except Exception as e:
        print(f"[API] get_av_actresses error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/subscriptions/av/detail")
def api_get_av_detail(url: str = "") -> dict[str, object]:
    if not url:
        raise HTTPException(status_code=400, detail="url 参数不能为空")
    if not allowed_external_url(url, JAVDB_HOSTS):
        raise HTTPException(status_code=403, detail="只允许访问 javdb 详情页")
    try:
        return {"detail": javdb.get_av_detail(url)}
    except Exception as e:
        print(f"[API] get_av_detail error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/javdb/listing")
def api_get_javdb_listing(url: str = "", force: bool = False, limit: int = 16) -> dict[str, object]:
    if not url:
        raise HTTPException(status_code=400, detail="url 参数不能为空")
    if not allowed_external_url(url, JAVDB_HOSTS):
        raise HTTPException(status_code=403, detail="只允许访问 javdb 页面")
    try:
        safe_limit = max(1, min(60, int(limit or 16)))
        return {"results": javdb.get_listing(url, limit=safe_limit, force_refresh=force)}
    except Exception as e:
        print(f"[API] get_javdb_listing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/javdb/status")
def api_get_javdb_status() -> dict[str, object]:
    return {"status": "ok", "javdb": javdb.stats()}


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


@app.get("/api/system-settings")
def api_get_system_settings() -> dict[str, object]:
    return {"settings": get_system_settings_service().get()}


@app.post("/api/system-settings")
async def api_update_system_settings(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="设置格式不正确")
    return {"status": "ok", "settings": get_system_settings_service().update(payload)}


@app.get("/api/jellyfin/libraries")
def api_jellyfin_libraries() -> dict[str, object]:
    settings_data = get_system_settings_service().get()
    return {"libraries": get_jellyfin_libraries(settings_data.get("jellyfin", {}))}


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
        return {"status": "ok", "message": result.get("message", "已发送到 qBittorrent")}
    except Exception as exc:
        app_log("error", "qbittorrent", "MTeam 资源下载失败", {"stage": "mteam_manual_download_error", "torrent_id": torrent_id, "error": str(exc)})
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
    title = "Media Toolbox 通知测试"
    message = "这是一条测试通知，用于确认通知通道可以正常发送。"
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
                return {"status": "ok", "message": "Server 酱测试通知已发送"}
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
                return {"status": "ok", "message": "Gotify 测试通知已发送"}
        except httpx.HTTPError as exc:
            return {"status": "error", "message": f"Gotify 发送失败: {exc}"}
    return {"status": "error", "message": "未知通知通道"}


def test_qbittorrent(config: dict[str, Any]) -> dict[str, object]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    if not base_url:
        return {"status": "error", "message": "未配置 qBittorrent Web UI 地址"}
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            username = str(config.get("username") or "")
            password = str(config.get("password") or "")
            if username or password:
                login = client.post(f"{base_url}/api/v2/auth/login", data={"username": username, "password": password})
                login.raise_for_status()
            resp = client.get(f"{base_url}/api/v2/app/version")
            resp.raise_for_status()
            categories = client.get(f"{base_url}/api/v2/torrents/categories")
            categories.raise_for_status()
            category = str(config.get("category") or "")
            category_note = ""
            if category and category not in categories.json():
                category_note = f"，分类 {category} 尚未创建"
            message = f"qBittorrent {resp.text.strip()}{category_note}"
            app_log("info", "qbittorrent", "测试 qBittorrent 连接", {"message": message})
            return {"status": "ok", "message": message}
    except httpx.HTTPError as exc:
        message = f"qBittorrent 连接失败: {exc}"
        app_log("error", "qbittorrent", "测试 qBittorrent 连接失败", {"error": str(exc)})
        return {"status": "error", "message": message}


def test_jellyfin(config: dict[str, Any]) -> dict[str, object]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    if not base_url:
        return {"status": "error", "message": "未配置 Jellyfin 地址"}
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
    except (httpx.HTTPError, ValueError) as exc:
        message = f"Jellyfin 连接失败: {exc}"
        app_log("error", "jellyfin", "测试 Jellyfin 连接失败", {"error": str(exc)})
        return {"status": "error", "message": message}


@app.get("/api/proxy/image")
def proxy_image(url: str) -> Response:
    """代理图片请求"""
    if not url:
        raise HTTPException(status_code=400, detail="url 参数不能为空")
    if not allowed_external_url(url, IMAGE_PROXY_HOSTS):
        raise HTTPException(status_code=403, detail="只允许代理指定域名图片")
    try:
        _, _, data_dir = settings()
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
            return FileResponse(cache_file, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
            cache_file.write_bytes(resp.content)
            meta_file.write_text(media_type, encoding="utf-8")
            return Response(
                content=resp.content,
                media_type=media_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail="图片获取失败") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"图片代理失败: {exc}") from exc
