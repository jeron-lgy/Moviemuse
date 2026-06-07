from __future__ import annotations

import os
import platform
import re
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
from pydantic import BaseModel, Field

from .log_service import AppLogService
from .scanner import ScanResult, scan_libraries
from .scan_state import scan_cache
from .storage import MoveRequest, MoveResult, Storage
from .mteam_service import download_mteam_torrent, search_mteam
from .postprocess_service import PostprocessService
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
        print("[MovieMuse] settings saved; restart required after active jobs finish", flush=True)
    else:
        print("[MovieMuse] settings saved and reloaded", flush=True)
    return {"status": "ok", "restart_required": not restarted}


def save_console_compute_config(payload: dict[str, Any]) -> dict[str, Any]:
    _, _, data_dir = settings()
    config = load_compute_config(data_dir)
    config.update(payload)
    save_compute_config(data_dir, config)
    return config


app = FastAPI(title="媒体工具箱")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST), name="frontend-assets")
move_jobs: dict[str, dict[str, Any]] = {}
move_jobs_lock = threading.Lock()


def frontend_index_response() -> FileResponse | None:
    frontend_index = FRONTEND_DIST / "index.html"
    if frontend_index.exists():
        return FileResponse(frontend_index)
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


def transcode_encoder(target_codec: str) -> str:
    codec = str(target_codec or "av1").lower()
    if codec == "av1":
        return os.getenv("TRANSCODE_AV1_ENCODER", "av1_nvenc")
    return os.getenv("TRANSCODE_H265_ENCODER", "libx265")


def transcode_ffmpeg_command(job: dict[str, Any]) -> list[str]:
    ffmpeg = os.getenv("FFMPEG_BIN", "ffmpeg")
    input_path = str(job.get("input_path") or "")
    output_path = str(job.get("output_path") or "")
    target_codec = str(job.get("target_codec") or "av1")
    encoder = transcode_encoder(target_codec)
    crf = str(job.get("crf") or 36)
    preset = str(job.get("preset") or "p1")
    quality_flag = "-cq" if encoder.endswith("_nvenc") or encoder in {"av1_nvenc", "hevc_nvenc"} else "-crf"
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
        "-preset",
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
    input_path = str(payload.get("input_path") or payload.get("video_path") or "").strip()
    output_path = str(payload.get("output_path") or "").strip()
    if not input_path:
        raise HTTPException(status_code=400, detail="缺少 input_path")
    if not output_path:
        raise HTTPException(status_code=400, detail="缺少 output_path")
    job_id = str(payload.get("job_id") or uuid.uuid4().hex)
    job = {
        "id": job_id,
        "task_id": str(payload.get("task_id") or ""),
        "av_id": str(payload.get("av_id") or ""),
        "status": "queued",
        "input_path": input_path,
        "output_path": output_path,
        "target_codec": str(payload.get("target_codec") or "av1"),
        "crf": int(payload.get("crf") or 36),
        "preset": str(payload.get("preset") or "p1"),
        "callback_url": str(payload.get("callback_url") or ""),
        "callback_token": str(payload.get("callback_token") or ""),
        "error": "",
        "command": [],
        "stderr_tail": "",
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
        job = set_transcode_job(job_id, status="running", started_at=time.time())
        output_path = Path(str(job.get("output_path") or ""))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = transcode_ffmpeg_command(job)
        set_transcode_job(job_id, command=command)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=int(os.getenv("TRANSCODE_TIMEOUT_SECONDS", "43200")),
            check=False,
        )
        stderr_tail = (result.stderr or "")[-4000:]
        if result.returncode != 0:
            message = f"ffmpeg 退出码 {result.returncode}"
            job = set_transcode_job(
                job_id,
                status="failed",
                error=message,
                stderr_tail=stderr_tail,
                returncode=result.returncode,
                finished_at=time.time(),
            )
            callback_payload = {"status": "failed", "job_id": job_id, "error": message, "stderr_tail": stderr_tail}
        else:
            job = set_transcode_job(
                job_id,
                status="worker_done",
                stderr_tail=stderr_tail,
                returncode=result.returncode,
                finished_at=time.time(),
            )
            callback_payload = {
                "status": "worker_done",
                "job_id": job_id,
                "output_path": job.get("output_path", ""),
                "input_path": job.get("input_path", ""),
                "target_codec": job.get("target_codec", ""),
            }
    except Exception as exc:
        try:
            job = set_transcode_job(job_id, status="failed", error=str(exc), finished_at=time.time())
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
        "transcode_jobs": {
            "total": len(all_transcode_items),
            "active": len(active_transcode),
            "items": transcode_items,
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
    post = get_postprocess_service()
    post_settings = post.get_settings()
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

    recent_tasks: list[dict[str, Any]] = []
    for task in post.list_tasks(limit=10):
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
        ],
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


@app.get("/api/transcode/jobs", dependencies=[Depends(require_subtitle_token)])
def api_list_transcode_jobs(limit: int = 100) -> dict[str, object]:
    jobs = transcode_jobs_payload(limit or None)
    active = [job for job in jobs if job.get("status") in {"queued", "running"}]
    return {"jobs": jobs, "total": len(transcode_jobs_payload()), "active": len(active)}


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
postprocess_service: PostprocessService | None = None
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


def download_pending_wash_subscriptions() -> dict[str, Any]:
    service = get_subscription_service()
    expired = expire_wash_requests_with_postprocess()
    items = [
        item for item in service.get_subscribed_av()
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
            "id": "wash_download",
            "name": "洗版轮询",
            "cron": sub_settings.get("wash_cron") or "0 22 * * *",
            "last_run_at": sub_settings.get("last_wash_poll_at") or 0,
            "description": "为等待中的洗版番号搜索中文或 4K 资源并推送 qBittorrent。",
        },
        {
            "id": "postprocess_qb",
            "name": "后处理下载检查",
            "cron": sub_settings.get("postprocess_cron") or "*/5 * * * *",
            "last_run_at": sub_settings.get("last_postprocess_poll_at") or 0,
            "description": "轮询系统绑定的 qB 种子，确认下载完成并进入转码/字幕队列。",
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
    elif task_id == "wash_download":
        result = download_pending_wash_subscriptions()
    elif task_id == "postprocess_qb":
        result = poll_postprocess_once()
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
                    ("wash_download", "wash_cron", "last_wash_poll_minute"),
                    ("postprocess_qb", "postprocess_cron", "last_postprocess_poll_minute"),
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
        app_log("info", "download", "下载链路完成", {"stage": "download_done", "av_id": av_id, "torrent_id": torrent_id, "status": qb_status, "message": payload["download_message"]})
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
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        username = str(config.get("username") or "")
        password = str(config.get("password") or "")
        if username or password:
            login = client.post(f"{base_url}/api/v2/auth/login", data={"username": username, "password": password})
            login.raise_for_status()
            login_text = login.text.strip()
            if login_text and "Ok." not in login_text:
                app_log("info", "qbittorrent", "qBittorrent 登录返回非标准文本，继续尝试添加种子", {
                    "stage": "qb_login_nonstandard",
                    "response": login_text[:120],
                })
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


def poll_qb_postprocess_once() -> dict[str, Any]:
    post = get_postprocess_service()
    settings_data = get_system_settings_service().get()
    qb_config = settings_data.get("qbittorrent", {})
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
    return {"checked": len(pending), "results": results}


def refresh_qb_torrent_status(row: dict[str, Any], task: dict[str, Any], qb_config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(qb_config.get("url") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("未配置 qBittorrent Web UI 地址")
    torrent_hash = str(row.get("torrent_hash") or "")
    post = get_postprocess_service()
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        login_qbittorrent(client, base_url, qb_config)
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


def login_qbittorrent(client: httpx.Client, base_url: str, config: dict[str, Any]) -> None:
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")
    if not username and not password:
        return
    login = client.post(f"{base_url}/api/v2/auth/login", data={"username": username, "password": password})
    login.raise_for_status()


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
    return (str(content / file_name) if content_path else file_name).replace("\\", "/")


def pick_main_video_file(files: list[dict[str, Any]], av_id: str, content_path: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    av_lower = av_id.lower()
    for item in files:
        name = str(item.get("name") or "")
        suffix = Path(name).suffix.lower()
        lower = name.lower()
        if suffix not in VIDEO_EXTENSIONS:
            continue
        if any(token in lower for token in ("sample", "trailer")):
            continue
        if av_lower and av_lower not in lower:
            continue
        size = int(item.get("size") or 0)
        full_path = resolve_qb_file_path(content_path, name)
        candidates.append({"path": full_path, "name": name, "size": size, "reason": "matched_av_largest_video"})
    if candidates:
        return sorted(candidates, key=lambda row: row["size"], reverse=True)[0]
    content = Path(content_path)
    if content.suffix.lower() in VIDEO_EXTENSIONS and av_lower in content.name.lower():
        return {"path": content_path, "name": content.name, "size": 0, "reason": "content_path_video"}
    return None


def local_postprocess_file_ready(path: str, expected_size: int = 0) -> dict[str, Any]:
    target = Path(path)
    if not target.exists() or not target.is_file():
        return {"ok": False, "reason": "控制端无法读取下载文件", "path": str(target)}
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
    av_id = str(task.get("av_id") or input_path.stem or "unknown").upper()
    codec = str(task.get("target_codec") or settings_payload.get("target_codec") or "av1").lower()
    suffix = input_path.suffix if input_path.suffix.lower() in VIDEO_EXTENSIONS else ".mkv"
    task_type = str(task.get("task_type") or "")
    variant = ""
    if task_type == "wash_chinese":
        variant = ".chinese"
    elif task_type == "wash_4k":
        variant = ".4k"
    output_dir = Path(str(settings_payload.get("output_dir") or "/压制")) / av_id
    return str(output_dir / f"{av_id}{variant}.{codec}{suffix}")


def build_postprocess_original_output_path(task: dict[str, Any], settings_payload: dict[str, Any], source_path: str) -> str:
    source = Path(str(source_path or task.get("input_path") or ""))
    av_id = str(task.get("av_id") or source.stem or "unknown").upper()
    suffix = source.suffix if source.suffix.lower() in VIDEO_EXTENSIONS else ".mkv"
    task_type = str(task.get("task_type") or "")
    variant = ""
    if task_type == "wash_chinese":
        variant = ".chinese"
    elif task_type == "wash_4k":
        variant = ".4k"
    output_dir = Path(str(settings_payload.get("output_dir") or "/压制")) / av_id
    return str(output_dir / f"{av_id}{variant}.original{suffix}")


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
    output_root = Path(str(settings_payload.get("output_dir") or "/压制"))
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
    target = product.with_name(f"{product.stem}.zh{subtitle.suffix or '.srt'}")
    try:
        if subtitle.resolve() == target.resolve():
            return str(subtitle)
    except OSError:
        pass
    if target.exists():
        target = Path(avoid_output_conflict(str(target), product.stem))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(subtitle, target)
    return str(target)


def dispatch_postprocess_task(task: dict[str, Any]) -> dict[str, Any]:
    post = get_postprocess_service()
    settings_payload = post.get_settings()
    task_id = str(task.get("id") or "")
    input_path = str(task.get("input_path") or "").strip()
    if not input_path:
        raise RuntimeError("任务缺少输入文件，不能派发到算力端")
    if bool(settings_payload.get("auto_transcode_enabled")):
        output_path = str(task.get("output_path") or avoid_output_conflict(build_postprocess_output_path(task, settings_payload), task_id))
        remote_worker = bool(backend_url())
        payload = {
            "task_id": task_id,
            "av_id": task.get("av_id", ""),
            "input_path": rewrite_proxy_path(input_path) if remote_worker else input_path,
            "output_path": rewrite_proxy_path(output_path) if remote_worker else output_path,
            "console_input_path": input_path,
            "console_output_path": output_path,
            "target_codec": settings_payload.get("target_codec"),
            "crf": settings_payload.get("crf"),
            "preset": settings_payload.get("preset"),
            "callback_url": f"{subtitle_public_url()}/api/postprocess/tasks/{task_id}/worker-done" if remote_worker else "",
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
            "target_codec": settings_payload.get("target_codec"),
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
    output_root = Path(str(settings_payload.get("output_dir") or "/压制"))
    if str(version.get("generated_by") or "") != "moviemuse":
        raise RuntimeError("旧版本不是 MovieMuse 托管版本，拒绝移动")
    if not path_under(source, output_root):
        raise RuntimeError("旧版本路径不在 /压制 托管目录，拒绝移动")
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
    download_root = Path(str(settings_payload.get("download_dir") or "/study3"))
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
        if bool(settings_payload.get("auto_subtitle_enabled")) and bool(task.get("needs_subtitle")) and not subtitle_path and not subtitle_failure:
            post.update_task(task_id, status="transcode_done", output_path=product_path, data={"transcode_validation": validation})
            post.add_event(task_id, "info", "transcode_done", "转码校验通过，继续派发字幕任务", {"output_path": product_path})
            return submit_postprocess_subtitle_task({**task, "output_path": product_path}, product_path)
        has_chinese_subtitle = False
    else:
        input_source = str(task.get("input_path") or "")
        source_product = chosen_output or input_source
        if chosen_output and not Path(chosen_output).exists() and input_source and Path(input_source).exists():
            source_product = input_source
        output_root = Path(str(settings_payload.get("output_dir") or "/压制"))
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


def refresh_worker_queue_readiness(worker_status: dict[str, Any] | None = None) -> dict[str, Any]:
    post = get_postprocess_service()
    status_payload = worker_status or subtitle_backend_status()
    online = bool(status_payload.get("online") or status_payload.get("status") == "ok")
    waiting = post.list_tasks(statuses=["waiting_worker"], limit=200)
    promoted: list[str] = []
    if online:
        for task in waiting:
            task_id = str(task.get("id") or "")
            post.update_task(task_id, status="ready_to_run", error_code="", error_message="")
            post.add_event(task_id, "info", "worker_ready", "算力端在线，任务已进入可执行队列", {"worker_status": status_payload})
            promoted.append(task_id)
    return {"online": online, "checked": len(waiting), "promoted": promoted, "worker_status": status_payload}


def poll_postprocess_once() -> dict[str, Any]:
    qb_result = poll_qb_postprocess_once()
    subtitle_result = poll_subtitle_postprocess_once()
    queue_result: dict[str, Any] | None = None
    post = get_postprocess_service()
    post_settings = post.get_settings()
    worker_queue = refresh_worker_queue_readiness()
    if bool(post_settings.get("worker_auto_run")):
        candidates = post.list_tasks(statuses=["waiting_worker", "ready_to_run"], limit=1)
        if candidates:
            queue_result = run_postprocess_queue()
            app_log("info", "postprocess", "后处理队列自动执行已处理", {
                "stage": "postprocess_queue_auto_run",
                "status": queue_result.get("status"),
                "updated": queue_result.get("updated"),
            })
    return {"qb": qb_result, "subtitle": subtitle_result, "worker_queue": worker_queue, "queue_auto_run": queue_result}


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
    tasks = post.list_tasks(statuses=statuses or None, limit=limit)
    qb_rows = post.list_qb_torrents(limit=500)
    return {
        "tasks": tasks,
        "qb_torrents": qb_rows,
        "settings": post.get_settings(),
        "worker_status": subtitle_backend_status(),
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
    settings_payload = post.get_settings()
    if postprocess_task_needs_worker(task, settings_payload) and worker_is_offline():
        updated = post.update_task(task_id, status="waiting_worker", error_code="", error_message="")
        post.add_event(task_id, "info", "worker_offline", "算力端离线，单任务执行已保留在等待队列")
        return {"status": "waiting_worker", "task": updated}
    return dispatch_postprocess_task(task)


@app.post("/api/postprocess/tasks/{task_id}/retry")
def api_retry_postprocess_task(task_id: str) -> dict[str, object]:
    post = get_postprocess_service()
    task = post.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="后处理任务不存在")
    updated = post.update_task(task_id, status="ready_to_run", error_code="", error_message="")
    post.add_event(task_id, "info", "task_retry", "用户手动重试后处理任务", {"previous_status": task.get("status", "")})
    return {"status": "ready_to_run", "task": updated}


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
    settings_payload = get_postprocess_service().update_settings(payload)
    app_log("info", "postprocess", "后处理设置已保存", {
        "stage": "postprocess_settings_saved",
        "auto_transcode_enabled": settings_payload.get("auto_transcode_enabled"),
        "auto_subtitle_enabled": settings_payload.get("auto_subtitle_enabled"),
        "target_codec": settings_payload.get("target_codec"),
    })
    return {"settings": settings_payload}


@app.post("/api/postprocess/queue/run")
def api_run_postprocess_queue() -> dict[str, object]:
    return run_postprocess_queue()


WORKER_ACTIVE_TASK_STATUSES = {
    "sent_to_worker",
    "transcoding",
    "worker_done",
    "transcode_validating",
    "transcode_done",
    "subtitle_processing",
    "subtitle_validating",
}


def active_postprocess_worker_count() -> int:
    return len(get_postprocess_service().list_tasks(statuses=sorted(WORKER_ACTIVE_TASK_STATUSES), limit=500))


def postprocess_task_needs_worker(task: dict[str, Any], settings_payload: dict[str, Any]) -> bool:
    if bool(settings_payload.get("auto_transcode_enabled")):
        return True
    return bool(settings_payload.get("auto_subtitle_enabled")) and bool(task.get("needs_subtitle"))


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
    candidates = sorted(
        post.list_tasks(statuses=["waiting_worker", "ready_to_run"], limit=200),
        key=lambda item: float(item.get("created_at") or 0),
    )
    local_candidates = [task for task in candidates if not postprocess_task_needs_worker(task, settings_payload)]
    worker_candidates = [task for task in candidates if postprocess_task_needs_worker(task, settings_payload)]
    updated: list[dict[str, Any]] = []
    for task in local_candidates:
        try:
            updated.append(dispatch_postprocess_task(task))
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
            "max_concurrency": max_concurrency,
            "active_count": active_count,
            "worker_status": worker_status,
            "tasks": updated,
        }

    deferred = max(0, len(worker_candidates) - available_slots)
    for task in worker_candidates[:available_slots]:
        try:
            result = dispatch_postprocess_task(task)
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
    expire_wash_requests_with_postprocess()
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


def first_actress_work_cover(actress_id: str) -> str:
    try:
        works = javdb.get_actress_avs(actress_id)
    except Exception as exc:
        app_log("warning", "subscription", "女优作品封面兜底失败", {"actress_id": actress_id, "error": str(exc)})
        return ""
    return next((str(item.get("cover") or "") for item in works if item.get("cover")), "")


def enriched_actress_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    actress_ref = str(result.get("id") or result.get("name") or "").strip()
    if not actress_ref:
        return result

    profile: dict[str, Any] = {}
    try:
        profile = javdb.get_actress_profile(actress_ref) or {}
    except Exception as exc:
        app_log("warning", "subscription", "女优资料补全失败", {"actress_id": actress_ref, "error": str(exc)})

    profile_id = str(profile.get("id") or "").strip()
    profile_name = str(profile.get("name") or "").strip()
    profile_cover = str(profile.get("cover") or "").strip()

    if profile_id and profile_id != actress_ref and re.fullmatch(r"[A-Za-z0-9]+", profile_id):
        result["id"] = profile_id
    if profile_name and bad_actress_name(result.get("name")) and not bad_actress_name(profile_name):
        result["name"] = profile_name
    elif bad_actress_name(result.get("name")):
        result["name"] = actress_ref

    if not result.get("cover"):
        result["cover"] = profile_cover or first_actress_work_cover(str(result.get("id") or actress_ref))
    return result


def hydrate_actress_subscriptions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    service = get_subscription_service()
    hydrated: list[dict[str, Any]] = []
    for item in items:
        current = dict(item)
        needs_name = bad_actress_name(current.get("name"))
        needs_cover = not current.get("cover")
        if needs_name or needs_cover:
            enriched = enriched_actress_payload(current)
            patch: dict[str, Any] = {}
            if needs_name and not bad_actress_name(enriched.get("name")):
                patch["name"] = enriched.get("name")
            if needs_cover and enriched.get("cover"):
                patch["cover"] = enriched.get("cover")
            if patch:
                updated = service.update_actress_subscription(str(current.get("id") or ""), patch)
                current = updated or {**current, **patch}
        hydrated.append(current)
    return hydrated


@app.post("/api/subscriptions/actress")
async def api_subscribe_actress(request: Request) -> dict[str, object]:
    """订阅女优"""
    payload = await request.json()
    if not isinstance(payload, dict) or not payload.get("id"):
        raise HTTPException(status_code=400, detail="女优信息格式不正确")
    service = get_subscription_service()
    payload = enriched_actress_payload(payload)
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
    return {"subscriptions": hydrate_actress_subscriptions(service.get_subscribed_actresses())}


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


@app.get("/api/integrations/qbittorrent/options")
def api_qbittorrent_options() -> dict[str, object]:
    settings_data = get_system_settings_service().get()
    return qbittorrent_options(settings_data.get("qbittorrent", {}))


def qbittorrent_options(config: dict[str, Any]) -> dict[str, object]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    if not base_url:
        return {"status": "error", "message": "未配置 qBittorrent Web UI 地址", "categories": [], "tags": []}
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            login_qbittorrent(client, base_url, config)
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
    except Exception as exc:
        app_log("error", "qbittorrent", "读取 qBittorrent 分类/标签失败", {"stage": "qb_options_failed", "error": str(exc)})
        return {"status": "error", "message": str(exc), "categories": [], "tags": []}


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
