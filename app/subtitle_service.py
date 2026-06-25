from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".ts", ".webm"}
COMPUTE_SETTINGS_FILE = "compute_settings.json"


@dataclass
class SubtitleSettings:
    data_dir: Path
    default_model: str = "large-v3"
    model_dir: Path | None = None
    device: str = "cuda"
    compute_type: str = "float16"
    default_output_dir: Path | None = None
    api_token: str = ""
    path_map: list[tuple[str, str]] = field(default_factory=list)
    max_workers: int = 1
    translation_max_workers: int = 1
    default_translate_backend: str = "google"
    google_translate_url: str = "https://translate.google.com/translate_a/single"
    deepl_api_url: str = "https://api-free.deepl.com/v2/translate"
    deepl_api_key: str = ""
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    openai_batch_size: int = 12
    openai_max_concurrency: int = 2
    openai_translation_style: str = "adult_natural"
    openai_style_intensity: str = "medium"
    openai_context_lines: int = 2
    openai_glossary: str = ""
    ollama_url: str = ""
    ollama_model: str = "qwen2.5:7b"


@dataclass
class SubtitleSegment:
    start: float
    end: float
    text: str
    translated_text: str = ""


@dataclass
class SubtitleJob:
    id: str
    video_path: str
    output_dir: str
    source_language: str | None
    target_language: str
    model: str
    translate: bool
    translate_backend: str = "google"
    status: str = "queued"
    progress: float = 0.0
    message: str = "等待处理"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    original_srt: str | None = None
    translated_srt: str | None = None
    bilingual_srt: str | None = None
    original_vtt: str | None = None
    translated_vtt: str | None = None
    detected_language: str | None = None
    duration: float | None = None
    error: str | None = None


def compute_settings_path(data_dir: Path) -> Path:
    return data_dir / COMPUTE_SETTINGS_FILE


def load_compute_config(data_dir: Path) -> dict[str, Any]:
    path = compute_settings_path(data_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_compute_config(data_dir: Path, payload: dict[str, Any]) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = compute_settings_path(data_dir)
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if value in (None, ""):
            continue
        if isinstance(value, str):
            value = value.strip()
        if key in {"deepl_api_key", "openai_api_key", "subtitle_api_token"}:
            value = clean_api_secret(value)
        elif key == "openai_base_url":
            value = normalize_openai_base_url(value)
        elif key == "ollama_url":
            value = str(value or "").strip().rstrip("/")
        cleaned[key] = value
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def config_value(config: dict[str, Any], key: str, env_name: str, default: str = "") -> str:
    value = config.get(key)
    if value not in (None, ""):
        return str(value)
    return os.getenv(env_name, default).strip()


def clean_api_secret(value: Any) -> str:
    secret = str(value or "").strip().strip('"').strip("'")
    if secret.lower().startswith("bearer "):
        secret = secret[7:].strip()
    return secret


def normalize_openai_base_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    suffix = "/chat/completions"
    if url.lower().endswith(suffix):
        url = url[: -len(suffix)].rstrip("/")
    if url.lower() in {"https://api.deepseek.com/anthropic", "https://api.deepseek.com/anthropic/v1"}:
        url = "https://api.deepseek.com"
    return url


def config_int(config: dict[str, Any], key: str, env_name: str, default: int, minimum: int = 1) -> int:
    raw = config.get(key)
    if raw in (None, ""):
        raw = os.getenv(env_name, str(default))
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def load_subtitle_settings(data_dir: Path) -> SubtitleSettings:
    config = load_compute_config(data_dir)
    output_dir = config_value(config, "subtitle_output_dir", "SUBTITLE_OUTPUT_DIR")
    model_dir = config_value(config, "whisper_model_dir", "WHISPER_MODEL_DIR", str(data_dir / "whisper-models"))
    return SubtitleSettings(
        data_dir=data_dir,
        default_model=config_value(config, "whisper_model", "WHISPER_MODEL", "large-v3"),
        model_dir=Path(model_dir) if model_dir else None,
        device=config_value(config, "whisper_device", "WHISPER_DEVICE", "cuda"),
        compute_type=config_value(config, "whisper_compute_type", "WHISPER_COMPUTE_TYPE", "float16"),
        default_output_dir=Path(output_dir) if output_dir else None,
        api_token=config_value(config, "subtitle_api_token", "SUBTITLE_API_TOKEN"),
        path_map=parse_path_map(config_value(config, "subtitle_path_map", "SUBTITLE_PATH_MAP")),
        max_workers=config_int(config, "subtitle_max_workers", "SUBTITLE_MAX_WORKERS", 1),
        translation_max_workers=config_int(
            config,
            "translation_max_workers",
            "TRANSLATION_MAX_WORKERS",
            config_int(config, "openai_max_concurrency", "TRANSLATE_OPENAI_MAX_CONCURRENCY", 1),
        ),
        default_translate_backend=config_value(config, "default_translate_backend", "DEFAULT_TRANSLATE_BACKEND", "google"),
        google_translate_url=config_value(
            config,
            "google_translate_url",
            "GOOGLE_TRANSLATE_URL",
            "https://translate.google.com/translate_a/single",
        ),
        deepl_api_url=config_value(config, "deepl_api_url", "DEEPL_API_URL", "https://api-free.deepl.com/v2/translate"),
        deepl_api_key=clean_api_secret(config_value(config, "deepl_api_key", "DEEPL_API_KEY")),
        openai_base_url=normalize_openai_base_url(config_value(config, "openai_base_url", "TRANSLATE_OPENAI_BASE_URL")),
        openai_api_key=clean_api_secret(config_value(config, "openai_api_key", "TRANSLATE_OPENAI_API_KEY")),
        openai_model=config_value(config, "openai_model", "TRANSLATE_OPENAI_MODEL", "gpt-4.1-mini"),
        openai_batch_size=config_int(config, "openai_batch_size", "TRANSLATE_OPENAI_BATCH_SIZE", 12),
        openai_max_concurrency=config_int(
            config,
            "openai_max_concurrency",
            "TRANSLATE_OPENAI_MAX_CONCURRENCY",
            2,
        ),
        openai_translation_style=config_value(
            config,
            "openai_translation_style",
            "TRANSLATE_OPENAI_STYLE",
            "adult_natural",
        ),
        openai_style_intensity=config_value(
            config,
            "openai_style_intensity",
            "TRANSLATE_OPENAI_STYLE_INTENSITY",
            "medium",
        ),
        openai_context_lines=config_int(config, "openai_context_lines", "TRANSLATE_OPENAI_CONTEXT_LINES", 2, minimum=0),
        openai_glossary=config_value(config, "openai_glossary", "TRANSLATE_OPENAI_GLOSSARY"),
        ollama_url=config_value(config, "ollama_url", "OLLAMA_URL").rstrip("/"),
        ollama_model=config_value(config, "ollama_model", "OLLAMA_TRANSLATE_MODEL", "qwen2.5:7b"),
    )


def parse_path_map(raw: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in raw.split(";"):
        if not item.strip() or "=" not in item:
            continue
        source, target = item.split("=", 1)
        pairs.append((source.strip().replace("\\", "/"), target.strip()))
    return pairs


def map_remote_path(path: str, settings: SubtitleSettings) -> Path:
    normalized = path.strip().replace("\\", "/")
    for remote_prefix, local_prefix in settings.path_map:
        if normalized == remote_prefix or normalized.startswith(remote_prefix.rstrip("/") + "/"):
            suffix = normalized[len(remote_prefix.rstrip("/")) :].lstrip("/")
            return Path(local_prefix) / Path(*suffix.split("/"))
    return Path(path)


def add_nvidia_dll_dirs() -> None:
    try:
        import nvidia
    except ImportError:
        return

    nvidia_roots = list(getattr(nvidia, "__path__", []))
    if not nvidia_roots:
        return
    bin_dirs = [
        Path(nvidia_roots[0]) / "cublas" / "bin",
        Path(nvidia_roots[0]) / "cudnn" / "bin",
        Path(nvidia_roots[0]) / "cuda_nvrtc" / "bin",
    ]
    existing_path = os.environ.get("PATH", "")
    additions = [str(path) for path in bin_dirs if path.exists() and str(path) not in existing_path]
    if additions:
        os.environ["PATH"] = ";".join(additions + [existing_path])


def srt_timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def vtt_timestamp(seconds: float) -> str:
    return srt_timestamp(seconds).replace(",", ".")


def parse_subtitle_timestamp(value: str) -> float:
    raw = value.strip().replace(",", ".")
    hours, minutes, rest = raw.split(":", 2)
    seconds, milliseconds = (rest.split(".", 1) + ["0"])[:2]
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds[:3].ljust(3, "0")) / 1000
    )


def read_srt(path: Path) -> list[SubtitleSegment]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    segments: list[SubtitleSegment] = []
    for block in blocks:
        items = block[1:] if block and block[0].strip().isdigit() else block
        timing_index = next((index for index, item in enumerate(items) if "-->" in item), -1)
        if timing_index < 0:
            continue
        start_raw, end_raw = [item.strip() for item in items[timing_index].split("-->", 1)]
        text = "\n".join(items[timing_index + 1 :]).strip()
        if not text:
            continue
        try:
            segments.append(
                SubtitleSegment(
                    start=parse_subtitle_timestamp(start_raw),
                    end=parse_subtitle_timestamp(end_raw.split()[0]),
                    text=text,
                )
            )
        except (ValueError, IndexError):
            continue
    return segments


def translation_source_text(
    text: str,
    source_language: str | None = "auto",
    target_language: str = "zh",
) -> tuple[str, bool]:
    """Extract Japanese source lines when a segment looks like Chinese/Japanese bilingual output."""
    source = str(source_language or "auto").lower()
    target = str(target_language or "").lower()
    if not target.startswith("zh") or source not in {"auto", "ja", "jp", "jpn", "japanese"}:
        return text, False
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return text, False
    kana_indexes = [index for index, line in enumerate(lines) if re.search(r"[\u3040-\u30ff]", line)]
    target_indexes = [
        index
        for index, line in enumerate(lines)
        if index not in kana_indexes and re.search(r"[\u4e00-\u9fff]", line)
    ]
    if not kana_indexes or not target_indexes:
        return text, False

    first_source = min(kana_indexes)
    last_source = max(kana_indexes)
    if any(index < first_source for index in target_indexes):
        extracted = "\n".join(lines[first_source:]).strip()
    elif any(index > last_source for index in target_indexes):
        extracted = "\n".join(lines[: last_source + 1]).strip()
    else:
        extracted = "\n".join(lines[index] for index in kana_indexes).strip()
    return (extracted or text), bool(extracted and extracted != str(text).strip())


def translation_source_segments(
    segments: list[SubtitleSegment],
    source_language: str | None = "auto",
    target_language: str = "zh",
) -> tuple[list[SubtitleSegment], int]:
    prepared: list[SubtitleSegment] = []
    extracted_count = 0
    for segment in segments:
        text, extracted = translation_source_text(segment.text, source_language, target_language)
        prepared.append(replace(segment, text=text))
        extracted_count += int(extracted)
    return prepared, extracted_count


def write_srt(path: Path, segments: list[SubtitleSegment], translated: bool = False, bilingual: bool = False) -> None:
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        text = segment.translated_text if translated else segment.text
        if bilingual:
            text = "\n".join(part for part in [segment.translated_text, segment.text] if part)
        lines.extend(
            [
                str(index),
                f"{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}",
                text.strip(),
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_vtt(path: Path, segments: list[SubtitleSegment], translated: bool = False) -> None:
    lines = ["WEBVTT", ""]
    for segment in segments:
        text = segment.translated_text if translated else segment.text
        lines.extend(
            [
                f"{vtt_timestamp(segment.start)} --> {vtt_timestamp(segment.end)}",
                text.strip(),
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


class SubtitleService:
    def __init__(self, settings: SubtitleSettings):
        self.settings = settings
        self.jobs_path = settings.data_dir / "subtitle_jobs.json"
        self.upload_dir = settings.data_dir / "subtitle_uploads"
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()
        self.translation_queue: queue.Queue[str] = queue.Queue()
        self.jobs: dict[str, SubtitleJob] = {}
        self.cancelled_jobs: set[str] = set()
        self._model_cache: dict[tuple[str, str, str, str], Any] = {}
        self._load_jobs()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(settings.max_workers):
            worker = threading.Thread(target=self._worker_loop, daemon=True)
            worker.start()
        for _ in range(max(1, min(4, int(settings.translation_max_workers or 1)))):
            translation_worker = threading.Thread(target=self._translation_worker_loop, daemon=True)
            translation_worker.start()
        if os.getenv("COMPUTE_NODE_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}:
            status_worker = threading.Thread(target=self._status_loop, daemon=True)
            status_worker.start()

    def create_job(
        self,
        video_path: str,
        output_dir: str | None = None,
        source_language: str | None = None,
        target_language: str = "zh",
        model: str | None = None,
        translate: bool = True,
        translate_backend: str = "google",
    ) -> SubtitleJob:
        job = self._prepare_job(video_path, output_dir, source_language, target_language, model, translate, translate_backend)
        with self.lock:
            self.jobs[job.id] = job
            self._save_jobs_locked()
        self.queue.put(job.id)
        return job

    def create_jobs(self, payloads: list[dict[str, Any]]) -> list[SubtitleJob]:
        jobs = [
            self._prepare_job(
                str(payload.get("video_path") or ""),
                payload.get("output_dir"),
                payload.get("source_language"),
                str(payload.get("target_language") or "zh"),
                payload.get("model"),
                bool(payload.get("translate", True)),
                str(payload.get("translate_backend") or "google"),
            )
            for payload in payloads
        ]
        with self.lock:
            for job in jobs:
                self.jobs[job.id] = job
            self._save_jobs_locked()
        for job in jobs:
            self.queue.put(job.id)
        return jobs

    def _prepare_job(
        self,
        video_path: str,
        output_dir: str | None = None,
        source_language: str | None = None,
        target_language: str = "zh",
        model: str | None = None,
        translate: bool = True,
        translate_backend: str = "google",
    ) -> SubtitleJob:
        resolved_video = map_remote_path(video_path, self.settings).resolve()
        if not resolved_video.exists() or not resolved_video.is_file():
            raise FileNotFoundError(f"视频文件不存在: {resolved_video}")
        if resolved_video.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"不支持的视频格式: {resolved_video.suffix}")
        if resolved_video.stat().st_size < 1024:
            raise ValueError(f"视频文件太小，可能是占位文件或损坏文件: {resolved_video}")

        if output_dir:
            resolved_output = map_remote_path(output_dir, self.settings).resolve()
        elif self.settings.default_output_dir:
            resolved_output = self.settings.default_output_dir.resolve()
        else:
            resolved_output = resolved_video.parent

        resolved_output.mkdir(parents=True, exist_ok=True)
        job = SubtitleJob(
            id=uuid.uuid4().hex,
            video_path=str(resolved_video),
            output_dir=str(resolved_output),
            source_language=source_language or None,
            target_language=target_language or "zh",
            model=model or self.settings.default_model,
            translate=translate,
            translate_backend=translate_backend or "google",
        )
        return job

    def save_upload(self, filename: str, content: bytes) -> Path:
        suffix = Path(filename).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            raise ValueError(f"不支持的视频格式: {suffix}")
        safe_name = f"{uuid.uuid4().hex}{suffix}"
        target = self.upload_dir / safe_name
        target.write_bytes(content)
        return target

    def get_job(self, job_id: str) -> SubtitleJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def list_jobs(self, limit: int | None = None) -> list[SubtitleJob]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)
            return jobs[:limit] if limit else jobs

    def delete_job(self, job_id: str) -> SubtitleJob:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise FileNotFoundError("任务不存在")
            if job.status in {"queued", "running", "translating"}:
                self.cancelled_jobs.add(job_id)
            removed = self.jobs.pop(job_id)
            self._save_jobs_locked()
            return removed

    def cancel_job(self, job_id: str, message: str = "用户手动取消") -> SubtitleJob:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise FileNotFoundError("任务不存在")
            if job.status == "completed":
                raise ValueError("已完成任务不能取消")
            self.cancelled_jobs.add(job_id)
            job.status = "cancelled"
            job.progress = 1.0
            job.message = message
            job.error = message
            job.finished_at = time.time()
            job.updated_at = time.time()
            self._save_jobs_locked()
            return replace(job)

    def retry_job(self, job_id: str, translate_backend: str | None = None) -> SubtitleJob:
        source = self.get_job(job_id)
        if not source:
            raise FileNotFoundError("任务不存在")
        if source.status not in {"failed", "completed"}:
            raise ValueError("只有失败或已完成的任务可以重新提交")
        retry_backend = translate_backend or self.settings.default_translate_backend or source.translate_backend or "google"
        if source.translate:
            original_srt = self._existing_original_srt(source)
            if original_srt:
                return self._create_translation_retry(source, original_srt, retry_backend)
        return self.create_job(
            video_path=source.video_path,
            output_dir=source.output_dir,
            source_language=source.source_language,
            target_language=source.target_language,
            model=source.model,
            translate=source.translate,
            translate_backend=retry_backend,
        )

    def retry_failed_jobs(self, translate_backend: str | None = None) -> list[SubtitleJob]:
        with self.lock:
            failed_ids = [job.id for job in self.jobs.values() if job.status == "failed"]
        retried: list[SubtitleJob] = []
        for job_id in failed_ids:
            try:
                retried.append(self.retry_job(job_id, translate_backend=translate_backend))
            except Exception:
                continue
        return retried

    def _existing_original_srt(self, job: SubtitleJob) -> Path | None:
        candidates: list[Path] = []
        if job.original_srt:
            candidates.append(Path(job.original_srt))
        candidates.append(Path(job.output_dir) / f"{Path(job.video_path).stem}.srt")
        for path in candidates:
            if path.exists() and path.is_file():
                return path
        return None

    def _existing_original_vtt(self, job: SubtitleJob) -> Path | None:
        candidates: list[Path] = []
        if job.original_vtt:
            candidates.append(Path(job.original_vtt))
        candidates.append(Path(job.output_dir) / f"{Path(job.video_path).stem}.vtt")
        for path in candidates:
            if path.exists() and path.is_file():
                return path
        return None

    def _create_translation_retry(self, source: SubtitleJob, original_srt: Path, translate_backend: str) -> SubtitleJob:
        original_vtt = self._existing_original_vtt(source)
        job = SubtitleJob(
            id=uuid.uuid4().hex,
            video_path=source.video_path,
            output_dir=source.output_dir,
            source_language=source.source_language,
            target_language=source.target_language,
            model=source.model,
            translate=True,
            translate_backend=translate_backend or "google",
            status="queued",
            progress=0.82,
            message="等待翻译字幕",
            started_at=time.time(),
            original_srt=str(original_srt),
            original_vtt=str(original_vtt) if original_vtt else source.original_vtt,
            detected_language=source.detected_language,
            duration=source.duration,
        )
        with self.lock:
            self.jobs[job.id] = job
            self._save_jobs_locked()
        self.translation_queue.put(job.id)
        return job

    def file_for(self, job_id: str, kind: str) -> Path:
        job = self.get_job(job_id)
        if not job:
            raise FileNotFoundError("任务不存在")
        value = getattr(job, kind, None)
        if not value:
            raise FileNotFoundError("文件尚未生成")
        path = Path(value)
        if not path.exists():
            raise FileNotFoundError("文件不存在")
        return path

    def _worker_loop(self) -> None:
        while True:
            job_id = self.queue.get()
            try:
                self._run_job(job_id)
            finally:
                self.queue.task_done()

    def _translation_worker_loop(self) -> None:
        while True:
            job_id = self.translation_queue.get()
            try:
                self._run_translation_job(job_id)
            finally:
                self.translation_queue.task_done()

    def _status_loop(self) -> None:
        while True:
            time.sleep(30)
            with self.lock:
                jobs = list(self.jobs.values())
            active = sum(1 for job in jobs if job.status in {"queued", "running", "translating"})
            print(
                f"[MovieMuse] status active={active} total={len(jobs)} "
                f"model={self.settings.default_model} device={self.settings.device}/{self.settings.compute_type}",
                flush=True,
            )

    def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job or self._is_cancelled(job_id):
            return
        print(f"[MovieMuse] job started id={job.id} file={job.video_path}", flush=True)
        self._update(job_id, status="running", progress=0.02, message="加载 Whisper 模型", started_at=time.time())
        try:
            if self._is_cancelled(job_id):
                return
            video_path = Path(job.video_path)
            output_dir = Path(job.output_dir)
            output_stem = output_dir / video_path.stem
            model = self._get_model(job.model)

            if self._is_cancelled(job_id):
                return
            self._update(job_id, progress=0.08, message="正在识别语音")
            segments_iter, info = model.transcribe(
                str(video_path),
                language=job.source_language,
                vad_filter=True,
                beam_size=5,
            )
            segments: list[SubtitleSegment] = []
            duration = float(getattr(info, "duration", 0.0) or 0.0)
            detected_language = getattr(info, "language", None)
            for raw_segment in segments_iter:
                if self._is_cancelled(job_id):
                    return
                text = " ".join(str(raw_segment.text).split())
                if text:
                    segments.append(SubtitleSegment(start=float(raw_segment.start), end=float(raw_segment.end), text=text))
                if duration:
                    self._update(job_id, progress=min(0.78, 0.10 + (float(raw_segment.end) / duration) * 0.68))

            if self._is_cancelled(job_id):
                return
            original_srt = output_stem.with_suffix(".srt")
            original_vtt = output_stem.with_suffix(".vtt")
            write_srt(original_srt, segments)
            write_vtt(original_vtt, segments)
            self._update(
                job_id,
                progress=0.82,
                message="原文字幕已生成",
                original_srt=str(original_srt),
                original_vtt=str(original_vtt),
                detected_language=detected_language,
                duration=duration or None,
            )

            if job.translate:
                if self._is_cancelled(job_id):
                    return
                self._update(job_id, status="translating", progress=0.84, message=f"等待翻译字幕 ({job.translate_backend})")
                self.translation_queue.put(job_id)
                print(f"[MovieMuse] transcription completed id={job.id} output={original_srt}", flush=True)
                return

            if self._is_cancelled(job_id):
                return
            self._update(
                job_id,
                status="completed",
                progress=1.0,
                message="字幕任务完成",
                finished_at=time.time(),
            )
            print(f"[MovieMuse] job completed id={job.id} output={original_srt}", flush=True)
        except Exception as exc:
            self._fail_job(job_id, job, exc)
            message = self.get_job(job_id).error if self.get_job(job_id) else str(exc)
            print(f"[MovieMuse] job failed id={job.id} error={message}", flush=True)

    def _run_translation_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job or self._is_cancelled(job_id):
            return
        try:
            original_srt = self._existing_original_srt(job)
            if not original_srt:
                raise FileNotFoundError("原文 SRT 不存在，无法只重跑翻译")
            segments = read_srt(original_srt)
            if not segments:
                raise RuntimeError(f"原文 SRT 没有可翻译内容: {original_srt}")
            segments, extracted_count = translation_source_segments(
                segments,
                job.detected_language or job.source_language or "auto",
                job.target_language,
            )

            video_path = Path(job.video_path)
            output_dir = Path(job.output_dir)
            output_stem = output_dir / video_path.stem
            if self._is_cancelled(job_id):
                return
            self._update(
                job_id,
                status="translating",
                progress=0.86,
                message=(
                    f"正在翻译字幕 ({job.translate_backend})"
                    + (f"，已从 {extracted_count} 段双语字幕提取原文" if extracted_count else "")
                ),
                original_srt=str(original_srt),
                original_vtt=str(self._existing_original_vtt(job)) if self._existing_original_vtt(job) else job.original_vtt,
            )
            self._translate_segments(
                segments,
                job.detected_language or job.source_language or "auto",
                job.target_language,
                job.translate_backend,
            )
            if self._is_cancelled(job_id):
                return
            translated_srt = output_stem.with_name(f"{output_stem.name}.{job.target_language}").with_suffix(".srt")
            translated_vtt = output_stem.with_name(f"{output_stem.name}.{job.target_language}").with_suffix(".vtt")
            bilingual_srt = output_stem.with_name(f"{output_stem.name}.bilingual").with_suffix(".srt")
            write_srt(translated_srt, segments, translated=True)
            write_vtt(translated_vtt, segments, translated=True)
            write_srt(bilingual_srt, segments, bilingual=True)
            self._update(
                job_id,
                status="completed",
                progress=1.0,
                message="字幕任务完成",
                finished_at=time.time(),
                translated_srt=str(translated_srt),
                translated_vtt=str(translated_vtt),
                bilingual_srt=str(bilingual_srt),
                error=None,
            )
            print(f"[MovieMuse] translation completed id={job.id} output={translated_srt}", flush=True)
        except Exception as exc:
            self._fail_job(job_id, job, exc, message="翻译失败")
            current = self.get_job(job_id)
            print(f"[MovieMuse] translation failed id={job.id} error={current.error if current else exc}", flush=True)

    def _fail_job(self, job_id: str, job: SubtitleJob, exc: Exception, message: str = "任务失败") -> None:
        if self._is_cancelled(job_id):
            return
        error = str(exc)
        if "Invalid data found when processing input" in error:
            error = f"视频无法解码，可能不是有效媒体文件或文件已损坏: {job.video_path}"
        self._update(
            job_id,
            status="failed",
            progress=1.0,
            message=message,
            error=error,
            finished_at=time.time(),
        )

    def _get_model(self, model_name: str) -> Any:
        model_dir = self.settings.model_dir
        cache_key = (model_name, self.settings.device, self.settings.compute_type, str(model_dir or ""))
        if cache_key not in self._model_cache:
            if self.settings.device == "cuda":
                add_nvidia_dll_dirs()
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError("缺少 faster-whisper，请先安装 requirements.txt 里的依赖") from exc
            if model_dir:
                model_dir.mkdir(parents=True, exist_ok=True)
            self._model_cache[cache_key] = WhisperModel(
                model_name,
                device=self.settings.device,
                compute_type=self.settings.compute_type,
                download_root=str(model_dir) if model_dir else None,
            )
        return self._model_cache[cache_key]

    def _translate_segments(
        self,
        segments: list[SubtitleSegment],
        source_language: str,
        target_language: str,
        translate_backend: str = "google",
    ) -> None:
        if not segments:
            return
        if source_language == target_language:
            for segment in segments:
                segment.translated_text = segment.text
            return
        backend = (translate_backend or "google").lower()
        if backend == "none":
            for segment in segments:
                segment.translated_text = segment.text
            return
        if backend == "google":
            self._translate_with_google(segments, source_language, target_language)
            return
        if backend == "deepl":
            self._translate_with_deepl(segments, source_language, target_language)
            return
        if backend in {"deepseek", "openai"}:
            self._translate_with_openai(segments, source_language, target_language)
            return
        if backend == "ollama":
            self._translate_with_ollama(segments, source_language, target_language)
            return

        errors: list[str] = []
        for name, runner, available in [
            ("google", self._translate_with_google, True),
            ("deepl", self._translate_with_deepl, bool(self.settings.deepl_api_key)),
            ("deepseek", self._translate_with_openai, bool(self.settings.openai_base_url and self.settings.openai_api_key)),
            ("ollama", self._translate_with_ollama, bool(self.settings.ollama_url)),
        ]:
            if not available:
                errors.append(f"{name}: 未配置")
                continue
            try:
                runner(segments, source_language, target_language)
                return
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError("没有可用翻译后端。" + "；".join(errors))

    def test_translation_backend(
        self,
        backend: str,
        text: str = "クッションがいっぱいある、かわいい",
        source_language: str = "ja",
        target_language: str = "zh",
        settings_override: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        tester = object.__new__(SubtitleService)
        tester.settings = self._settings_with_override(settings_override or {})
        segment = SubtitleSegment(start=0, end=1, text=text)
        tester._translate_segments([segment], source_language or "auto", target_language or "zh", backend or "google")
        return {
            "backend": backend or "google",
            "source_text": text,
            "translated_text": segment.translated_text,
            "source_language": source_language or "auto",
            "target_language": target_language or "zh",
        }

    def translate_sample(
        self,
        segments: list[SubtitleSegment],
        backend: str,
        source_language: str = "ja",
        target_language: str = "zh",
        settings_override: dict[str, Any] | None = None,
    ) -> list[SubtitleSegment]:
        tester = object.__new__(SubtitleService)
        tester.settings = self._settings_with_override(settings_override or {})
        translated = [replace(segment) for segment in segments]
        tester._translate_segments(translated, source_language or "auto", target_language or "zh", backend or "deepseek")
        return translated

    def _settings_with_override(self, payload: dict[str, Any]) -> SubtitleSettings:
        values: dict[str, Any] = {}
        mapping = {
            "whisper_model": "default_model",
            "whisper_model_dir": "model_dir",
            "whisper_device": "device",
            "whisper_compute_type": "compute_type",
            "subtitle_output_dir": "default_output_dir",
            "subtitle_api_token": "api_token",
            "subtitle_max_workers": "max_workers",
            "subtitle_path_map": "path_map",
        }
        for key, value in payload.items():
            target = mapping.get(key, key)
            if target not in SubtitleSettings.__dataclass_fields__:
                continue
            if target in {"model_dir", "default_output_dir"}:
                values[target] = Path(str(value)) if value not in (None, "") else None
            elif target == "path_map":
                values[target] = parse_path_map(str(value or ""))
            elif target in {"max_workers", "translation_max_workers", "openai_batch_size", "openai_max_concurrency"}:
                try:
                    values[target] = max(1, int(value))
                except (TypeError, ValueError):
                    pass
            elif target == "openai_context_lines":
                try:
                    values[target] = max(0, int(value))
                except (TypeError, ValueError):
                    pass
            elif target in {"api_token", "deepl_api_key", "openai_api_key"}:
                values[target] = clean_api_secret(value)
            elif target in {"openai_base_url", "ollama_url"}:
                values[target] = normalize_openai_base_url(value) if target == "openai_base_url" else str(value or "").strip().rstrip("/")
            else:
                values[target] = str(value).strip() if isinstance(value, str) else value
        return replace(self.settings, **values)

    def _translate_with_google(
        self, segments: list[SubtitleSegment], source_language: str, target_language: str
    ) -> None:
        import httpx

        source = "auto" if source_language == "auto" else source_language
        endpoints = list(
            dict.fromkeys(
                [
                    self.settings.google_translate_url,
                    "https://translate.google.com/translate_a/single",
                    "https://translate.googleapis.com/translate_a/single",
                ]
            )
        )
        translated_count = 0
        errors: list[str] = []
        headers = {
            "User-Agent": "Mozilla/5.0 MediaToolbox/1.0",
            "Accept": "application/json,text/plain,*/*",
        }
        with httpx.Client(timeout=60, headers=headers, follow_redirects=True) as client:
            for segment in segments:
                segment_errors: list[str] = []
                for endpoint in endpoints:
                    try:
                        response = client.get(
                            endpoint,
                            params={
                                "client": "gtx",
                                "sl": source,
                                "tl": target_language,
                                "dt": "t",
                                "q": segment.text,
                            },
                        )
                        response.raise_for_status()
                        payload = response.json()
                        parts = payload[0] if payload and isinstance(payload[0], list) else []
                        translated = "".join(str(item[0]) for item in parts if item and item[0])
                        segment.translated_text = translated.strip() or segment.text
                        translated_count += 1
                        break
                    except Exception as exc:
                        segment_errors.append(f"{endpoint}: {exc}")
                if not segment.translated_text:
                    segment.translated_text = segment.text
                    errors.extend(segment_errors[:1])
        if translated_count == 0 and errors:
            raise RuntimeError("Google 免费翻译不可用：" + "；".join(errors[:3]))

    def _translate_with_deepl(
        self, segments: list[SubtitleSegment], source_language: str, target_language: str
    ) -> None:
        import httpx

        if not self.settings.deepl_api_key:
            raise RuntimeError("DeepL API 未配置 DEEPL_API_KEY")
        target = self._deepl_lang(target_language)
        source = "" if source_language == "auto" else self._deepl_lang(source_language)
        headers = {"Authorization": f"DeepL-Auth-Key {self.settings.deepl_api_key}"}
        with httpx.Client(timeout=120) as client:
            for segment in segments:
                data = {
                    "text": segment.text,
                    "target_lang": target,
                }
                if source:
                    data["source_lang"] = source
                response = client.post(self.settings.deepl_api_url, headers=headers, data=data)
                response.raise_for_status()
                translations = response.json().get("translations") or []
                segment.translated_text = (translations[0].get("text", "") if translations else "").strip() or segment.text

    @staticmethod
    def _deepl_lang(language: str) -> str:
        value = (language or "").split("-", 1)[0].upper()
        aliases = {"ZH": "ZH", "ZHO": "ZH", "CHI": "ZH", "JA": "JA", "JP": "JA", "EN": "EN", "KO": "KO"}
        return aliases.get(value, value or "ZH")

    def _translate_with_ollama(
        self, segments: list[SubtitleSegment], source_language: str, target_language: str
    ) -> None:
        import httpx

        if not self.settings.ollama_url:
            raise RuntimeError("Ollama 未配置 OLLAMA_URL")
        endpoint = f"{self.settings.ollama_url}/api/generate"
        with httpx.Client(timeout=180) as client:
            for segment in segments:
                prompt = (
                    f"Translate the subtitle from {source_language} to {target_language}. "
                    "Return only the translated subtitle text, no explanation.\n\n"
                    f"{segment.text}"
                )
                response = client.post(
                    endpoint,
                    json={
                        "model": self.settings.ollama_model,
                        "prompt": prompt,
                        "stream": False,
                    },
                )
                response.raise_for_status()
                segment.translated_text = str(response.json().get("response", "")).strip() or segment.text

    def _translate_with_openai(self, segments: list[SubtitleSegment], source_language: str, target_language: str) -> None:
        import httpx

        base_url = normalize_openai_base_url(self.settings.openai_base_url)
        api_key = clean_api_secret(self.settings.openai_api_key)
        if not base_url or not api_key:
            raise RuntimeError("DeepSeek/OpenAI 兼容 API 未配置")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        endpoint = f"{base_url}/chat/completions"
        batch_size = min(12, max(1, int(self.settings.openai_batch_size or 12)))
        max_concurrency = min(2, max(1, int(self.settings.openai_max_concurrency or 1)))
        context_lines = min(4, max(0, int(self.settings.openai_context_lines or 0)))
        batches = [(start, segments[start : start + batch_size]) for start in range(0, len(segments), batch_size)]

        limits = httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=max_concurrency)
        with httpx.Client(timeout=180, limits=limits) as client:
            if max_concurrency == 1 or len(batches) <= 1:
                translated_batches = [
                    self._translate_openai_batch_resilient(
                        client,
                        endpoint,
                        headers,
                        start,
                        batch,
                        source_language,
                        target_language,
                        segments[max(0, start - context_lines) : start],
                        segments[start + len(batch) : start + len(batch) + context_lines],
                    )
                    for start, batch in batches
                ]
            else:
                workers = min(max_concurrency, len(batches))
                translated_batches = []
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = [
                        executor.submit(
                            self._translate_openai_batch_resilient,
                            client,
                            endpoint,
                            headers,
                            start,
                            batch,
                            source_language,
                            target_language,
                            segments[max(0, start - context_lines) : start],
                            segments[start + len(batch) : start + len(batch) + context_lines],
                        )
                        for start, batch in batches
                    ]
                    for future in as_completed(futures):
                        translated_batches.append(future.result())

        for start, translations in sorted(translated_batches, key=lambda item: item[0]):
            batch = segments[start : start + len(translations)]
            for segment, translated in zip(batch, translations):
                segment.translated_text = str(translated).strip() or segment.text

    def _translate_openai_batch_resilient(
        self,
        client: Any,
        endpoint: str,
        headers: dict[str, str],
        start: int,
        batch: list[SubtitleSegment],
        source_language: str,
        target_language: str,
        context_before: list[SubtitleSegment] | None = None,
        context_after: list[SubtitleSegment] | None = None,
    ) -> tuple[int, list[str]]:
        try:
            return self._translate_openai_batch(
                client,
                endpoint,
                headers,
                start,
                batch,
                source_language,
                target_language,
                context_before,
                context_after,
            )
        except Exception as exc:
            if "authentication failed" in str(exc).lower() or len(batch) <= 1:
                raise
            midpoint = len(batch) // 2
            _, left = self._translate_openai_batch_resilient(
                client, endpoint, headers, start, batch[:midpoint], source_language, target_language, context_before, context_after
            )
            _, right = self._translate_openai_batch_resilient(
                client, endpoint, headers, start + midpoint, batch[midpoint:], source_language, target_language, context_before, context_after
            )
            return start, left + right

    def _translate_openai_batch(
        self,
        client: Any,
        endpoint: str,
        headers: dict[str, str],
        start: int,
        batch: list[SubtitleSegment],
        source_language: str,
        target_language: str,
        context_before: list[SubtitleSegment] | None = None,
        context_after: list[SubtitleSegment] | None = None,
    ) -> tuple[int, list[str]]:
        items = [{"id": start + index, "text": segment.text} for index, segment in enumerate(batch)]
        expected_ids = [str(item["id"]) for item in items]
        context = {
            "before": [segment.text for segment in context_before or []],
            "after": [segment.text for segment in context_after or []],
        }
        payload = {
            "model": self.settings.openai_model,
            "messages": [
                {
                    "role": "system",
                    "content": self._openai_translation_instruction(),
                },
                {
                    "role": "user",
                    "content": (
                        f"Translate from {source_language} to {target_language}. "
                        "Context is reference only; return translations only for Target items. "
                        f"Context JSON:\n{json.dumps(context, ensure_ascii=False)}\n"
                        f"Target JSON array:\n{json.dumps(items, ensure_ascii=False)}"
                    ),
                },
            ],
            "temperature": 0,
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.post(endpoint, headers=headers, json=payload)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if response.status_code in {401, 403}:
                    body = (response.text or "").strip().replace("\n", " ")[:240]
                    raise RuntimeError(
                        "DeepSeek/OpenAI authentication failed "
                        f"(HTTP {response.status_code}). Check API Key, account permission, and Base URL. "
                        f"Response: {body or 'empty response'}"
                    )
                response.raise_for_status()
                try:
                    response_payload = response.json()
                except ValueError as exc:
                    body = (response.text or "").strip().replace("\n", " ")[:240]
                    raise RuntimeError(f"DeepSeek/OpenAI 返回的不是 JSON：{body or '空响应'}") from exc
                try:
                    content = response_payload["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    body = json.dumps(response_payload, ensure_ascii=False)[:240]
                    raise RuntimeError(f"DeepSeek/OpenAI 返回格式异常：{body}") from exc
                if not str(content or "").strip():
                    raise RuntimeError("DeepSeek/OpenAI 返回了空翻译内容")
                try:
                    translations = self._parse_openai_translation_payload(content, expected_ids)
                except (json.JSONDecodeError, RuntimeError) as exc:
                    snippet = str(content or "").strip().replace("\n", " ")[:240]
                    raise RuntimeError(f"DeepSeek/OpenAI 没有返回可解析的 JSON 数组：{snippet}") from exc
                if len(translations) != len(batch):
                    raise RuntimeError("翻译返回数量和字幕段数量不一致")
                cleaned_translations = [str(item).strip() for item in translations]
                untranslated = [
                    index
                    for index, (segment, translated) in enumerate(zip(batch, cleaned_translations))
                    if self._looks_untranslated_japanese(segment.text, translated, target_language)
                ]
                if untranslated:
                    repaired = self._repair_openai_untranslated_items(
                        client,
                        endpoint,
                        headers,
                        start,
                        batch,
                        cleaned_translations,
                        untranslated,
                        source_language,
                        target_language,
                    )
                    for index, value in repaired.items():
                        cleaned_translations[index] = value
                    untranslated = [
                        index
                        for index, (segment, translated) in enumerate(zip(batch, cleaned_translations))
                        if self._looks_untranslated_japanese(segment.text, translated, target_language)
                    ]
                if untranslated:
                    item_ids = [str(start + index) for index in untranslated[:5]]
                    snippets = " | ".join(batch[index].text.replace("\n", " ")[:36] for index in untranslated[:3])
                    raise RuntimeError(f"翻译后仍残留日文（字幕段 {', '.join(item_ids)}）：{snippets}")
                return start, cleaned_translations
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise last_error or RuntimeError("DeepSeek/OpenAI 翻译失败")

    def _repair_openai_untranslated_items(
        self,
        client: Any,
        endpoint: str,
        headers: dict[str, str],
        start: int,
        batch: list[SubtitleSegment],
        translations: list[str],
        indexes: list[int],
        source_language: str,
        target_language: str,
    ) -> dict[int, str]:
        items = [
            {
                "id": start + index,
                "source": batch[index].text,
                "previous_translation": translations[index],
            }
            for index in indexes
        ]
        expected_ids = [str(item["id"]) for item in items]
        payload = {
            "model": self.settings.openai_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        self._openai_translation_instruction()
                        + " This is a correction pass: the previous result incorrectly retained Japanese kana. "
                        "Translate every supplied source completely into Simplified Chinese. "
                        "Do not copy any Japanese kana into the output, including short interjections, slang, or sound words."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Correct translations from {source_language} to {target_language}. "
                        "Return translations only for these exact ids as one JSON object.\n"
                        f"Items JSON:\n{json.dumps(items, ensure_ascii=False)}"
                    ),
                },
            ],
            "temperature": 0.2,
        }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = client.post(endpoint, headers=headers, json=payload)
                if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                    time.sleep(1.5)
                    continue
                response.raise_for_status()
                response_payload = response.json()
                content = response_payload["choices"][0]["message"]["content"]
                repaired = self._parse_openai_translation_payload(content, expected_ids)
                return {index: str(value).strip() for index, value in zip(indexes, repaired)}
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1.0)
        raise last_error or RuntimeError("DeepSeek/OpenAI 日文残留纠正失败")

    def _openai_translation_instruction(self) -> str:
        base = (
            "You translate subtitles. Return valid JSON only. "
            "Return one JSON object. Keys must be the exact input ids as strings. "
            "Values must be translated subtitle text. Translate every item, including short, repeated, or unclear lines. "
            "Do not merge, omit, renumber, add markdown, add notes, or keep source text unless it is already in the target language. "
            "When translating to Chinese, output Chinese only and do not retain Japanese kana, including interjections or sound words. "
            "Preserve the original meaning; never invent actions, relationships, or plot details not present in the source. "
        )
        style = str(self.settings.openai_translation_style or "adult_natural").strip().lower()
        intensity = str(self.settings.openai_style_intensity or "medium").strip().lower()
        if style == "faithful":
            style_prompt = "Use faithful, concise and natural spoken Chinese without additional embellishment."
        elif style == "seductive":
            strength = {
                "restrained": "Keep the wording suggestive but restrained.",
                "strong": "When the source supports it, use notably teasing, sensual and emotionally charged spoken Chinese.",
            }.get(intensity, "When the source supports it, use playful and sensual spoken Chinese with clear emotional tone.")
            style_prompt = (
                "The material may contain consensual adult dialogue between adults. "
                "Render intimate or flirtatious lines as natural adult spoken Chinese. "
                + strength
                + " Do not intensify neutral lines or add explicit details absent from the source."
            )
        else:
            style_prompt = (
                "The material may contain consensual adult dialogue between adults. "
                "When the source is intimate, flirtatious, commanding, or emotionally expressive, "
                "use natural adult spoken Chinese that preserves that tone without adding new detail."
            )
        glossary = str(self.settings.openai_glossary or "").strip()
        if glossary:
            style_prompt += (
                " Apply the following preferred terminology only when it matches the source meaning; "
                f"do not force it into unrelated lines:\n{glossary}"
            )
        return base + style_prompt

    @staticmethod
    def _parse_openai_translation_payload(content: str, expected_ids: list[str] | None = None) -> list[Any]:
        text = str(content or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        object_start = text.find("{")
        object_end = text.rfind("}")
        array_start = text.find("[")
        array_end = text.rfind("]")
        if object_start >= 0 and object_end > object_start and (array_start < 0 or object_start < array_start):
            text = text[object_start : object_end + 1]
        elif array_start >= 0 and array_end > array_start:
            text = text[array_start : array_end + 1]
        payload = json.loads(text)
        if isinstance(payload, dict):
            if expected_ids is None:
                return list(payload.values())
            missing = [item_id for item_id in expected_ids if item_id not in payload]
            if missing:
                raise RuntimeError(f"translation response missing ids: {', '.join(missing[:5])}")
            return [payload[item_id] for item_id in expected_ids]
        if not isinstance(payload, list):
            raise RuntimeError("翻译返回不是 JSON array")
        return payload

    @staticmethod
    def _parse_openai_translation_array(content: str) -> list[Any]:
        return SubtitleService._parse_openai_translation_payload(content)

    @staticmethod
    def _looks_untranslated_japanese(source: str, translated: str, target_language: str) -> bool:
        if not str(target_language or "").lower().startswith("zh"):
            return False
        kana = set(
            "ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねの"
            "はばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをん"
            "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノ"
            "ハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヰヱヲンヴー"
        )
        return any(char in kana for char in str(source or "")) and any(char in kana for char in str(translated or ""))

    def _update(self, job_id: str, **changes: Any) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job_id in self.cancelled_jobs or job.status == "cancelled":
                return
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = time.time()
            self._save_jobs_locked()

    def _is_cancelled(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            return job_id in self.cancelled_jobs or not job or job.status == "cancelled"

    def _load_jobs(self) -> None:
        if not self.jobs_path.exists():
            return
        try:
            raw_jobs = json.loads(self.jobs_path.read_text(encoding="utf-8"))
            for raw_job in raw_jobs:
                job = SubtitleJob(**raw_job)
                if job.status in {"queued", "running", "translating"}:
                    job.status = "failed"
                    job.message = "服务重启后任务已中断，请重新提交"
                self.jobs[job.id] = job
        except Exception:
            self.jobs = {}

    def _save_jobs_locked(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        payload = [asdict(job) for job in sorted(self.jobs.values(), key=lambda item: item.created_at)]
        self.jobs_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
