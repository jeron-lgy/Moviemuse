from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


MODEL_CATALOG: tuple[dict[str, Any], ...] = (
    {"id": "large-v3-turbo", "repo_id": "dropbox-dash/faster-whisper-large-v3-turbo", "label": "large-v3-turbo", "size_bytes": 1_621_665_983},
    {"id": "large-v3", "repo_id": "Systran/faster-whisper-large-v3", "label": "large-v3", "size_bytes": 3_220_000_000},
    {"id": "medium", "repo_id": "Systran/faster-whisper-medium", "label": "medium", "size_bytes": 1_530_000_000},
    {"id": "small", "repo_id": "Systran/faster-whisper-small", "label": "small", "size_bytes": 520_000_000},
    {"id": "base", "repo_id": "Systran/faster-whisper-base", "label": "base", "size_bytes": 290_000_000},
)
MODEL_BY_ID = {item["id"]: item for item in MODEL_CATALOG}
REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")
ACTIVE_DOWNLOAD_STATES = {"queued", "downloading", "pausing", "paused", "cancelling"}
TERMINAL_DOWNLOAD_STATES = {"completed", "failed", "cancelled"}
MODEL_VERSION_CHECK_TTL_SECONDS = 6 * 60 * 60


def whisper_model_recommendation(gpus: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [item for item in gpus if isinstance(item, dict)]
    gpu = max(candidates, key=lambda item: int(item.get("memory_total_mb") or 0), default={})
    memory_mb = int(gpu.get("memory_total_mb") or 0)
    if memory_mb >= 8 * 1024:
        model_id = "large-v3-turbo"
        reason = "显存充足，推荐使用 large-v3-turbo 兼顾识别质量与速度"
        tier = "high"
    elif memory_mb >= 6 * 1024:
        model_id = "medium"
        reason = "显存适中，推荐使用 medium 保持质量和稳定性"
        tier = "medium"
    elif memory_mb >= 4 * 1024:
        model_id = "small"
        reason = "显存有限，推荐使用 small 降低显存压力"
        tier = "entry"
    else:
        model_id = "base"
        reason = "未检测到足够的 NVIDIA 显存，推荐从 base 开始使用"
        tier = "cpu" if not gpu else "low"
    return {
        "gpu_detected": bool(gpu),
        "gpu_name": str(gpu.get("name") or ""),
        "memory_total_mb": memory_mb,
        "recommended_model": model_id,
        "recommended_label": str(MODEL_BY_ID[model_id]["label"]),
        "reason": reason,
        "tier": tier,
    }


class WorkerRuntimeControl:
    """Process-local switch for accepting new compute jobs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._enabled = True
        self._changed_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "compute_enabled": self._enabled,
                "runtime_changed_at": self._changed_at,
            }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            next_value = bool(enabled)
            if self._enabled != next_value:
                self._enabled = next_value
                self._changed_at = time.time()
            return {
                "compute_enabled": self._enabled,
                "runtime_changed_at": self._changed_at,
            }

    def require_enabled(self) -> None:
        with self._lock:
            if not self._enabled:
                raise RuntimeError("算力端已关闭，请先在 Worker 界面启动算力")


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


@dataclass
class DownloadControl:
    pause: threading.Event
    cancel: threading.Event


class WorkerModelService:
    """Manage model files only. Model activation remains owned by MovieMuse."""

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir
        self.download_dir = model_dir / ".downloads"
        self.jobs_file = self.download_dir / "jobs.json"
        self.version_checks_file = self.download_dir / "version-checks.json"
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._controls: dict[str, DownloadControl] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._version_checks = self._load_version_checks()
        self._load_jobs()

    def _load_version_checks(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.version_checks_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): dict(value) for key, value in payload.items() if key in MODEL_BY_ID and isinstance(value, dict)}

    def _persist_version_checks_locked(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.version_checks_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._version_checks, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.version_checks_file)

    def _model_manifest(self, model_id: str) -> dict[str, Any]:
        path = self.model_dir / model_id / ".moviemuse-model.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict) or str(payload.get("model_id") or model_id) != model_id:
            return {}
        return dict(payload)

    def _version_payload(self, model_id: str, installed: bool) -> dict[str, Any]:
        if not installed:
            return {
                "local_revision": "",
                "latest_revision": "",
                "version_status": "not_installed",
                "version_checked_at": 0,
                "version_error": "",
            }
        manifest = self._model_manifest(model_id)
        local_revision = str(manifest.get("revision") or "")
        with self._lock:
            cached = dict(self._version_checks.get(model_id) or {})
        if cached and str(cached.get("local_revision") or "") == local_revision:
            return cached
        return {
            "local_revision": local_revision,
            "latest_revision": "",
            "version_status": "not_checked" if local_revision else "local_version_unknown",
            "version_checked_at": 0,
            "version_error": "",
        }

    @staticmethod
    def _remote_revision(repo_id: str) -> str:
        from huggingface_hub import HfApi

        endpoint = os.getenv("HF_ENDPOINT", "https://huggingface.co").strip() or "https://huggingface.co"
        token = os.getenv("HF_TOKEN", "").strip() or None
        info = HfApi(endpoint=endpoint, token=token).model_info(repo_id, files_metadata=False)
        revision = str(getattr(info, "sha", None) or "")
        if not revision:
            raise RuntimeError("模型仓库没有返回版本信息")
        return revision

    def check_updates(self, *, force: bool = False) -> dict[str, Any]:
        now = time.time()
        results: dict[str, dict[str, Any]] = {}
        for item in MODEL_CATALOG:
            model_id = str(item["id"])
            installed = (self.model_dir / model_id).is_dir()
            if not installed:
                continue
            current = self._version_payload(model_id, installed=True)
            if (
                not force
                and float(current.get("version_checked_at") or 0) > now - MODEL_VERSION_CHECK_TTL_SECONDS
                and current.get("version_status") != "not_checked"
            ):
                results[model_id] = current
                continue
            local_revision = str(current.get("local_revision") or "")
            try:
                latest_revision = self._remote_revision(str(item["repo_id"]))
                if not local_revision:
                    status = "local_version_unknown"
                elif latest_revision == local_revision:
                    status = "up_to_date"
                else:
                    status = "update_available"
                result = {
                    "local_revision": local_revision,
                    "latest_revision": latest_revision,
                    "version_status": status,
                    "version_checked_at": now,
                    "version_error": "",
                }
            except Exception as exc:
                result = {
                    **current,
                    "version_status": "check_failed",
                    "version_checked_at": now,
                    "version_error": str(exc),
                }
            results[model_id] = result
        with self._lock:
            self._version_checks.update(results)
            self._persist_version_checks_locked()
        return {"models": self.models(), "checked_at": now}

    def _load_jobs(self) -> None:
        try:
            payload = json.loads(self.jobs_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(payload, list):
            return
        changed = False
        with self._lock:
            for raw in payload:
                if not isinstance(raw, dict) or not raw.get("id") or not raw.get("model_id"):
                    continue
                job = dict(raw)
                job_id = str(job["id"])
                if not job_id or not job_id.replace("-", "").isalnum():
                    continue
                model_id = str(job["model_id"])
                catalog = MODEL_BY_ID.get(model_id)
                if not catalog:
                    continue
                if job.get("repo_id") != catalog["repo_id"]:
                    changed = True
                job["repo_id"] = catalog["repo_id"]
                if str(job.get("state")) not in {"completed", "cancelled"}:
                    job["staging_dir"] = str(self.download_dir / f"{model_id}-{job_id}.partial")
                if str(job.get("state")) in {"queued", "downloading", "pausing", "cancelling"}:
                    job["state"] = "paused"
                    job["error"] = "算力端曾重启，下载已暂停，可继续下载"
                    job["updated_at"] = time.time()
                    changed = True
                self._jobs[job_id] = job
                pause = threading.Event()
                if str(job.get("state")) == "paused":
                    pause.set()
                self._controls[job_id] = DownloadControl(pause, threading.Event())
            if changed:
                self._persist_locked()

    def _persist_locked(self) -> None:
        if len(self._jobs) > 50:
            terminal = sorted(
                (
                    (job_id, job)
                    for job_id, job in self._jobs.items()
                    if str(job.get("state")) in TERMINAL_DOWNLOAD_STATES
                ),
                key=lambda item: float(item[1].get("created_at") or 0),
            )
            for job_id, _job in terminal[: max(0, len(self._jobs) - 50)]:
                self._jobs.pop(job_id, None)
                self._controls.pop(job_id, None)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.jobs_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(list(self._jobs.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.jobs_file)

    def _catalog_item(self, model_id: str) -> dict[str, Any]:
        item = MODEL_BY_ID.get(model_id)
        if not item:
            raise ValueError("不支持的 Whisper 模型")
        return item

    def _job_staging(self, job: dict[str, Any]) -> Path:
        model_id = str(job.get("model_id") or "")
        job_id = str(job.get("id") or "")
        self._catalog_item(model_id)
        if not job_id or not job_id.replace("-", "").isalnum():
            raise ValueError("无效下载任务")
        return self.download_dir / f"{model_id}-{job_id}.partial"

    def validate(self, model_id: str) -> dict[str, Any]:
        self._catalog_item(model_id)
        target = self.model_dir / model_id
        missing = [name for name in REQUIRED_MODEL_FILES if not (target / name).is_file()]
        return {
            "valid": target.is_dir() and not missing,
            "missing_files": missing,
            "checked_at": time.time(),
        }

    def models(self, active_model: str = "") -> list[dict[str, Any]]:
        active_name = Path(str(active_model or "")).name
        jobs = {str(job.get("model_id")): job for job in self.downloads() if job.get("state") not in TERMINAL_DOWNLOAD_STATES}
        result: list[dict[str, Any]] = []
        for item in MODEL_CATALOG:
            model_id = str(item["id"])
            path = self.model_dir / model_id
            validation = self.validate(model_id)
            installed = path.is_dir()
            version = self._version_payload(model_id, installed)
            job = jobs.get(model_id)
            result.append({
                **item,
                "path": str(path),
                "installed": installed,
                "available": True,
                "verified": bool(validation["valid"]),
                "missing_files": validation["missing_files"],
                "active": model_id == active_name,
                "actual_size_bytes": directory_size(path),
                "modified_at": path.stat().st_mtime if installed else None,
                "status": str(job.get("state")) if job else ("installed" if installed else "not_downloaded"),
                "download": job,
                **version,
            })
        return result

    def storage(self) -> dict[str, Any]:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.model_dir)
        return {
            "path": str(self.model_dir),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "model_bytes": directory_size(self.model_dir) - directory_size(self.download_dir),
        }

    def downloads(self) -> list[dict[str, Any]]:
        with self._lock:
            snapshots = [dict(value) for value in self._jobs.values()]
        for item in snapshots:
            staging = Path(str(item.get("staging_dir") or ""))
            if staging.exists() and item.get("state") in ACTIVE_DOWNLOAD_STATES:
                item["downloaded_bytes"] = min(directory_size(staging), int(item.get("total_bytes") or 0) or directory_size(staging))
                total = int(item.get("total_bytes") or 0)
                item["progress"] = round((int(item["downloaded_bytes"]) / total) * 100, 1) if total else 0
        return sorted(snapshots, key=lambda item: float(item.get("created_at") or 0), reverse=True)

    def start_download(self, model_id: str, *, replace: bool = False) -> dict[str, Any]:
        item = self._catalog_item(model_id)
        target = self.model_dir / model_id
        if self.validate(model_id)["valid"] and not replace:
            raise FileExistsError("模型已经安装")
        with self._lock:
            for existing in self._jobs.values():
                if existing.get("model_id") == model_id and existing.get("state") not in TERMINAL_DOWNLOAD_STATES:
                    return dict(existing)
            job_id = uuid.uuid4().hex
            staging = self.download_dir / f"{model_id}-{job_id}.partial"
            job = {
                "id": job_id,
                "model_id": model_id,
                "repo_id": item["repo_id"],
                "state": "queued",
                "progress": 0,
                "downloaded_bytes": 0,
                "total_bytes": int(item["size_bytes"]),
                "current_file": "",
                "error": "",
                "speed_bytes_per_second": 0,
                "eta_seconds": None,
                "files_completed": 0,
                "files_total": 0,
                "replace_existing": bool(replace or target.exists()),
                "created_at": time.time(),
                "updated_at": time.time(),
                "staging_dir": str(staging),
            }
            self._jobs[job_id] = job
            self._controls[job_id] = DownloadControl(threading.Event(), threading.Event())
            self._persist_locked()
        self._start_thread(job_id)
        return dict(job)

    def _start_thread(self, job_id: str) -> None:
        with self._lock:
            existing = self._threads.get(job_id)
            if existing and existing.is_alive():
                return
            model_id = str(self._jobs[job_id]["model_id"])
            thread = threading.Thread(
                target=self._download,
                args=(job_id,),
                name=f"whisper-download-{model_id}",
                daemon=True,
            )
            self._threads[job_id] = thread
        thread.start()

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)
                self._jobs[job_id]["updated_at"] = time.time()
                self._persist_locked()

    @staticmethod
    def _download_url(repo_id: str, revision: str, filename: str, endpoint: str = "https://huggingface.co") -> str:
        return (
            f"{endpoint.rstrip('/')}/{quote(repo_id, safe='/')}/resolve/"
            f"{quote(revision, safe='')}/{quote(filename, safe='/')}"
        )

    @staticmethod
    def _target_file(staging: Path, filename: str) -> Path:
        root = staging.resolve()
        target = (staging / Path(filename)).resolve()
        if target != root and root not in target.parents:
            raise ValueError("模型仓库包含无效文件路径")
        return target

    def _download_file(
        self,
        *,
        job_id: str,
        control: DownloadControl,
        client: httpx.Client,
        staging: Path,
        filename: str,
        expected_size: int,
        completed_bytes: int,
        total_bytes: int,
    ) -> int:
        target = self._target_file(staging, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.name}.moviemuse-part")
        if target.exists():
            current_size = target.stat().st_size
            if expected_size <= 0 or current_size == expected_size:
                return current_size
            if current_size < expected_size and not partial.exists():
                target.replace(partial)
            else:
                target.unlink()

        with self._lock:
            current_job = dict(self._jobs[job_id])
        revision = str(current_job.get("revision") or "main")
        repo_id = str(current_job["repo_id"])
        endpoint = os.getenv("HF_ENDPOINT", "https://huggingface.co").strip() or "https://huggingface.co"
        url = self._download_url(repo_id, revision, filename, endpoint)
        last_update = 0.0
        while True:
            if control.cancel.is_set():
                raise InterruptedError("下载已取消")
            while control.pause.is_set():
                self._update(job_id, state="paused", speed_bytes_per_second=0, eta_seconds=None)
                if control.cancel.wait(0.25):
                    raise InterruptedError("下载已取消")

            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            restart_for_pause = False
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 416 and expected_size and offset >= expected_size:
                    partial.replace(target)
                    return expected_size
                response.raise_for_status()
                mode = "ab"
                if offset and response.status_code == 200:
                    offset = 0
                    mode = "wb"
                elif not offset:
                    mode = "wb"
                downloaded = offset
                transfer_started = time.monotonic()
                transfer_base = offset
                self._update(job_id, state="downloading", current_file=filename, error="")
                with partial.open(mode) as output:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        if control.cancel.is_set():
                            raise InterruptedError("下载已取消")
                        if control.pause.is_set():
                            restart_for_pause = True
                            break
                        if not chunk:
                            continue
                        output.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_update >= 0.4:
                            current_total = completed_bytes + downloaded
                            elapsed = max(0.001, now - transfer_started)
                            speed = max(0, int((downloaded - transfer_base) / elapsed))
                            remaining = max(0, total_bytes - current_total)
                            self._update(
                                job_id,
                                downloaded_bytes=current_total,
                                progress=round(current_total / total_bytes * 100, 1) if total_bytes else 0,
                                speed_bytes_per_second=speed,
                                eta_seconds=round(remaining / speed) if speed else None,
                            )
                            last_update = now
            if restart_for_pause:
                continue
            actual_size = partial.stat().st_size if partial.exists() else 0
            if expected_size and actual_size != expected_size:
                raise RuntimeError(f"{filename} 大小不完整：期望 {expected_size}，实际 {actual_size}")
            partial.replace(target)
            return actual_size

    def _install_staging(self, job_id: str, staging: Path, target: Path, replace_existing: bool) -> None:
        if not target.exists():
            staging.replace(target)
            return
        if not replace_existing:
            raise FileExistsError("目标模型目录已经存在")
        backup = self.download_dir / f"{target.name}-{job_id}.backup"
        shutil.rmtree(backup, ignore_errors=True)
        target.replace(backup)
        try:
            staging.replace(target)
        except Exception:
            if not target.exists() and backup.exists():
                backup.replace(target)
            raise
        shutil.rmtree(backup, ignore_errors=True)

    def _download(self, job_id: str) -> None:
        with self._lock:
            job = dict(self._jobs[job_id])
            control = self._controls[job_id]
        staging = self._job_staging(job)
        target = self.model_dir / str(job["model_id"])
        try:
            from huggingface_hub import HfApi

            staging.mkdir(parents=True, exist_ok=True)
            self._update(job_id, state="downloading", started_at=float(job.get("started_at") or time.time()))
            endpoint = os.getenv("HF_ENDPOINT", "https://huggingface.co").strip() or "https://huggingface.co"
            token = os.getenv("HF_TOKEN", "").strip() or None
            info = HfApi(endpoint=endpoint, token=token).model_info(str(job["repo_id"]), files_metadata=True)
            siblings = [
                sibling for sibling in (info.siblings or [])
                if sibling.rfilename and not sibling.rfilename.startswith(".") and sibling.rfilename not in {"README.md", "LICENSE"}
            ]
            total = sum(int(sibling.size or 0) for sibling in siblings) or int(job["total_bytes"])
            revision = str(getattr(info, "sha", None) or "main")
            self._update(job_id, total_bytes=total, files_total=len(siblings), revision=revision)
            completed_bytes = 0
            timeout = httpx.Timeout(connect=20, read=60, write=60, pool=60)
            headers = {"User-Agent": "MovieMuse-Worker/1.0"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
                for index, sibling in enumerate(siblings):
                    filename = str(sibling.rfilename)
                    downloaded = self._download_file(
                        job_id=job_id,
                        control=control,
                        client=client,
                        staging=staging,
                        filename=filename,
                        expected_size=int(sibling.size or 0),
                        completed_bytes=completed_bytes,
                        total_bytes=total,
                    )
                    completed_bytes += downloaded
                    self._update(job_id, files_completed=index + 1, downloaded_bytes=completed_bytes)
            missing = [name for name in REQUIRED_MODEL_FILES if not (staging / name).is_file()]
            if missing:
                raise RuntimeError(f"模型文件不完整：{', '.join(missing)}")
            manifest = {
                "model_id": job["model_id"],
                "repo_id": job["repo_id"],
                "revision": getattr(info, "sha", ""),
                "installed_at": time.time(),
            }
            (staging / ".moviemuse-model.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            self._install_staging(job_id, staging, target, bool(job.get("replace_existing")))
            with self._lock:
                installed_revision = str(manifest["revision"] or "")
                self._version_checks[str(job["model_id"])] = {
                    "local_revision": installed_revision,
                    "latest_revision": installed_revision,
                    "version_status": "up_to_date" if installed_revision else "not_checked",
                    "version_checked_at": time.time() if installed_revision else 0,
                    "version_error": "",
                }
                self._persist_version_checks_locked()
            self._update(
                job_id,
                state="completed",
                progress=100,
                downloaded_bytes=directory_size(target),
                speed_bytes_per_second=0,
                eta_seconds=0,
                current_file="",
                finished_at=time.time(),
                staging_dir="",
            )
        except InterruptedError:
            shutil.rmtree(staging, ignore_errors=True)
            self._update(job_id, state="cancelled", error="", speed_bytes_per_second=0, eta_seconds=None, finished_at=time.time(), staging_dir="")
        except Exception as exc:
            self._update(job_id, state="failed", error=str(exc), speed_bytes_per_second=0, eta_seconds=None, finished_at=time.time())
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    def pause(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            control = self._controls.get(job_id)
            if not control or job_id not in self._jobs:
                raise KeyError(job_id)
            if self._jobs[job_id].get("state") not in {"queued", "downloading", "pausing", "paused"}:
                raise ValueError("当前下载状态不能暂停")
            control.pause.set()
            self._jobs[job_id]["state"] = "pausing"
            self._jobs[job_id]["speed_bytes_per_second"] = 0
            self._jobs[job_id]["eta_seconds"] = None
            self._persist_locked()
            return dict(self._jobs[job_id])

    def resume(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            control = self._controls.get(job_id)
            if not control or job_id not in self._jobs:
                raise KeyError(job_id)
            if self._jobs[job_id].get("state") not in {"paused", "pausing", "failed"}:
                raise ValueError("当前下载状态不能继续")
            model_id = str(self._jobs[job_id].get("model_id") or "")
            self._jobs[job_id]["repo_id"] = self._catalog_item(model_id)["repo_id"]
            control.cancel.clear()
            control.pause.clear()
            self._jobs[job_id]["state"] = "downloading"
            self._jobs[job_id]["error"] = ""
            self._jobs[job_id]["finished_at"] = None
            self._persist_locked()
            snapshot = dict(self._jobs[job_id])
        self._start_thread(job_id)
        return snapshot

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            control = self._controls.get(job_id)
            if not control or job_id not in self._jobs:
                raise KeyError(job_id)
            if self._jobs[job_id].get("state") in {"completed", "cancelled"}:
                raise ValueError("当前下载状态不能取消")
            control.cancel.set()
            control.pause.clear()
            thread = self._threads.get(job_id)
            staging = self._job_staging(self._jobs[job_id])
            if thread and thread.is_alive():
                self._jobs[job_id]["state"] = "cancelling"
            else:
                self._jobs[job_id].update({"state": "cancelled", "error": "", "finished_at": time.time(), "staging_dir": ""})
            self._persist_locked()
            snapshot = dict(self._jobs[job_id])
        if snapshot["state"] == "cancelled":
            shutil.rmtree(staging, ignore_errors=True)
        return snapshot

    def remove(self, model_id: str, active_model: str) -> None:
        self._catalog_item(model_id)
        if Path(str(active_model or "")).name == model_id:
            raise PermissionError("当前生效模型由 MovieMuse 控制端管理，不能在算力端删除")
        target = (self.model_dir / model_id).resolve()
        root = self.model_dir.resolve()
        if target.parent != root:
            raise ValueError("无效模型路径")
        if target.exists():
            shutil.rmtree(target)
        with self._lock:
            if self._version_checks.pop(model_id, None) is not None:
                self._persist_version_checks_locked()
