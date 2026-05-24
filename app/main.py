from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .scanner import ScanResult, scan_libraries
from .scan_state import scan_cache
from .storage import MoveRequest, MoveResult, Storage
from .subtitle_service import (
    SubtitleJob,
    SubtitleService,
    load_compute_config,
    load_subtitle_settings,
    save_compute_config,
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
                return f"{target}\\{suffix.replace('/', '\\')}" if suffix else target
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
        "openai_batch_size": getattr(settings_obj, "openai_batch_size", config.get("openai_batch_size", 40)),
        "openai_max_concurrency": getattr(
            settings_obj,
            "openai_max_concurrency",
            config.get("openai_max_concurrency", 3),
        ),
        "ollama_url": getattr(settings_obj, "ollama_url", config.get("ollama_url", "")),
        "ollama_model": getattr(settings_obj, "ollama_model", config.get("ollama_model", "qwen2.5:7b")),
        "subtitle_api_token": getattr(settings_obj, "api_token", config.get("subtitle_api_token", "")),
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
            "settings": remote_settings(),
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
    return {
        "connection": {
            "subtitle_backend_url": backend_url() or str(console_config.get("subtitle_backend_url", "")),
            "subtitle_backend_token": str(console_config.get("subtitle_backend_token", "")),
        },
        "backend_status": status,
        "backend_error": backend_error,
        "jobs": jobs,
        "compute_settings": status.get("settings") or remote_settings(),
        "path_preview": backend_path_preview(remote_status=status if status.get("online") else None),
        "translation_backends": (status.get("settings", {}) or {}).get("translation_backends")
        or translation_backend_options(None),
        "model_options": whisper_model_options(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request, view: str = "duplicates") -> Response:
    if compute_node_only():
        return Response("Media Toolbox compute node is running. Use the Unraid console to manage settings.", media_type="text/plain")
    media_dirs, trash_dir, data_dir = settings()
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
    media_dirs, trash_dir, _ = settings()
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
    media_dirs, trash_dir, _ = settings()
    scan_dirs = selected_scan_dirs(media_dirs, paths, [trash_dir])
    if not scan_dirs:
        raise HTTPException(status_code=400, detail="请至少选择一个媒体子目录")
    started = scan_cache.start(scan_dirs, force=True, excluded_dirs=[trash_dir])
    return {"status": "running", "started": started, "scan_dirs": [str(path) for path in scan_dirs]}


@app.post("/scan/run")
def scan_run(paths: list[str] = Form(default=[])) -> RedirectResponse:
    media_dirs, trash_dir, _ = settings()
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
        result = remote_post_json("/api/compute/settings", payload)
        return {
            "status": "ok",
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
