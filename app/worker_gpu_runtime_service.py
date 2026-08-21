from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


GPU_REQUIREMENTS_FILENAME = "requirements-windows-worker-gpu.lock.txt"
GPU_RUNTIME_DIRNAME = "gpu-runtime"
GPU_DOWNLOAD_BYTES = 1_240_400_000
GPU_INSTALLED_BYTES = 1_826_900_000
REQUIRED_DLLS = (
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudnn64_9.dll",
    "cudnn_adv64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_engines_precompiled64_9.dll",
    "cudnn_engines_runtime_compiled64_9.dll",
    "cudnn_engines_tensor_ir64_9.dll",
    "cudnn_ext64_9.dll",
    "cudnn_graph64_9.dll",
    "cudnn_heuristic64_9.dll",
    "cudnn_ops64_9.dll",
)


def gpu_runtime_bin_dirs(root: Path) -> tuple[Path, ...]:
    return (
        root / "nvidia" / "cublas" / "bin",
        root / "nvidia" / "cudnn" / "bin",
        root / "nvidia" / "cuda_nvrtc" / "bin",
    )


def _path_key(path: Path | str) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(str(path)))


class WorkerGpuRuntimeService:
    def __init__(self, data_dir: Path, package_root: Path | None = None) -> None:
        self.data_dir = data_dir.resolve()
        self.package_root = (package_root or Path(__file__).resolve().parent.parent).resolve()
        self.runtime_dir = self.data_dir / GPU_RUNTIME_DIRNAME
        self.requirements_path = self.package_root / GPU_REQUIREMENTS_FILENAME
        self.pip_path = self.package_root / "pip.pyz"
        self.log_path = self.data_dir / "logs" / "gpu-runtime-install.log"
        self._lock = threading.RLock()
        self._job: dict[str, Any] = {}

    def _candidate_paths(self) -> list[tuple[str, Path]]:
        local_bins = gpu_runtime_bin_dirs(self.runtime_dir)
        bundled_root = self.package_root / "python" / "Lib" / "site-packages"
        development_root = self.package_root / ".venv" / "Lib" / "site-packages"
        candidates: list[tuple[str, Path]] = [
            *(("local", path) for path in local_bins),
            *(("bundled", path) for path in gpu_runtime_bin_dirs(bundled_root)),
            *(("bundled", path) for path in gpu_runtime_bin_dirs(development_root)),
        ]
        known = {_path_key(path) for _, path in candidates}
        for raw in os.environ.get("PATH", "").split(os.pathsep):
            if not raw.strip():
                continue
            path = Path(raw.strip().strip('"'))
            if _path_key(path) not in known:
                candidates.append(("system", path))
                known.add(_path_key(path))
        return candidates

    @staticmethod
    def _missing_in_paths(paths: list[Path]) -> list[str]:
        return [name for name in REQUIRED_DLLS if not any((path / name).is_file() for path in paths)]

    def _detection(self) -> dict[str, Any]:
        candidates = self._candidate_paths()
        active_keys = {
            _path_key(raw.strip().strip('"'))
            for raw in os.environ.get("PATH", "").split(os.pathsep)
            if raw.strip()
        }
        all_paths = [path for _, path in candidates]
        missing = self._missing_in_paths(all_paths)
        if missing:
            return {"ready": False, "source": "none", "missing": missing, "restart_required": False}

        dll_sources: set[str] = set()
        dll_paths: list[Path] = []
        for name in REQUIRED_DLLS:
            match = next(((source, path) for source, path in candidates if (path / name).is_file()), None)
            if match:
                dll_sources.add(match[0])
                dll_paths.append(match[1])
        source = next(iter(dll_sources)) if len(dll_sources) == 1 else "mixed"
        restart_required = source in {"local", "bundled", "mixed"} and any(
            _path_key(path) not in active_keys for path in dll_paths
        )
        return {
            "ready": not restart_required,
            "source": source,
            "missing": [],
            "restart_required": restart_required,
        }

    def status(self) -> dict[str, Any]:
        detected = self._detection()
        with self._lock:
            job = dict(self._job)
        if job.get("state") == "installing":
            state = "installing"
        elif job.get("state") == "failed":
            state = "failed"
        elif detected["restart_required"]:
            state = "installed_restart_required"
        elif detected["ready"]:
            state = "ready"
        else:
            state = "missing"
        return {
            "status": state,
            "ready": bool(detected["ready"]),
            "source": detected["source"],
            "required_dlls": list(REQUIRED_DLLS),
            "missing_dlls": detected["missing"],
            "restart_required": bool(detected["restart_required"]),
            "installable": self.requirements_path.is_file() and (
                self.pip_path.is_file() or importlib.util.find_spec("pip") is not None
            ),
            "estimated_download_bytes": GPU_DOWNLOAD_BYTES,
            "estimated_installed_bytes": GPU_INSTALLED_BYTES,
            "runtime_dir": str(self.runtime_dir),
            "job": job,
        }

    def start_install(self) -> dict[str, Any]:
        current = self.status()
        with self._lock:
            if self._job.get("state") == "installing":
                return current
            if current["ready"]:
                raise FileExistsError("GPU 运行环境已经就绪，无需重复安装")
            if not self.requirements_path.is_file():
                raise FileNotFoundError("安装包缺少 GPU 运行环境依赖清单，请重新下载完整安装包")
            if not self.pip_path.is_file() and importlib.util.find_spec("pip") is None:
                raise FileNotFoundError("安装包缺少 pip.pyz，请重新下载完整安装包")
            now = time.time()
            self._job = {
                "id": uuid.uuid4().hex,
                "state": "installing",
                "stage": "preparing",
                "message": "正在准备 GPU 运行环境下载",
                "started_at": now,
                "updated_at": now,
                "finished_at": 0,
                "error": "",
            }
            thread = threading.Thread(target=self._install, args=(self._job["id"],), daemon=True)
            thread.start()
            return self.status()

    def _update_job(self, job_id: str, **values: Any) -> None:
        with self._lock:
            if self._job.get("id") != job_id:
                return
            self._job.update(values)
            self._job["updated_at"] = time.time()

    def _install_command(self, target: Path) -> list[str]:
        if self.pip_path.is_file():
            command = [sys.executable, str(self.pip_path)]
        else:
            command = [sys.executable, "-m", "pip"]
        return command + [
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--target",
            str(target),
            "-r",
            str(self.requirements_path),
        ]

    def _install(self, job_id: str) -> None:
        staging = self.data_dir / f".{GPU_RUNTIME_DIRNAME}-installing-{job_id}"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            self._update_job(job_id, stage="downloading", message="正在下载并安装 cuBLAS 与 cuDNN（约 1.2 GB）")
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            with self.log_path.open("w", encoding="utf-8", errors="replace") as log:
                log.write(json.dumps({"started_at": time.time(), "target": str(self.runtime_dir)}, ensure_ascii=False) + "\n")
                result = subprocess.run(
                    self._install_command(staging),
                    cwd=str(self.package_root),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creation_flags,
                    check=False,
                )
            if result.returncode != 0:
                raise RuntimeError(f"依赖安装进程退出，错误代码 {result.returncode}")
            missing = self._missing_in_paths(list(gpu_runtime_bin_dirs(staging)))
            if missing:
                raise RuntimeError(f"安装完成但缺少必要文件：{', '.join(missing)}")
            self._update_job(job_id, stage="finalizing", message="正在完成安装")
            if self.runtime_dir.exists():
                shutil.rmtree(self.runtime_dir)
            os.replace(staging, self.runtime_dir)
            finished = time.time()
            self._update_job(
                job_id,
                state="completed",
                stage="restart_required",
                message="GPU 运行环境已安装，请关闭并重新打开 MovieMuse Worker",
                finished_at=finished,
            )
        except Exception as exc:
            self._update_job(
                job_id,
                state="failed",
                stage="failed",
                message="GPU 运行环境安装失败",
                error=str(exc),
                finished_at=time.time(),
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
