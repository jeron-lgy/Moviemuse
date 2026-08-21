from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import TestCase, mock

from app.worker_gpu_runtime_service import REQUIRED_DLLS, WorkerGpuRuntimeService, gpu_runtime_bin_dirs


def create_runtime_files(root: Path) -> None:
    bins = gpu_runtime_bin_dirs(root)
    bins[0].mkdir(parents=True, exist_ok=True)
    bins[1].mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_DLLS:
        target = bins[0] / name if name.startswith("cublas") else bins[1] / name
        target.write_bytes(b"test")


class WorkerGpuRuntimeServiceTests(TestCase):
    def test_missing_runtime_reports_lightweight_install_details(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = root / "package"
            package.mkdir()
            (package / "requirements-windows-worker-gpu.lock.txt").write_text("test==1\n", encoding="utf-8")
            service = WorkerGpuRuntimeService(root / "data", package)
            with mock.patch.dict(os.environ, {"PATH": ""}, clear=False):
                status = service.status()
            self.assertEqual(status["status"], "missing")
            self.assertTrue(status["installable"])
            self.assertGreater(status["estimated_download_bytes"], 1_000_000_000)
            self.assertEqual(set(status["missing_dlls"]), set(REQUIRED_DLLS))

    def test_downloaded_runtime_requires_restart_until_bins_are_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service = WorkerGpuRuntimeService(root / "data", root / "package")
            create_runtime_files(service.runtime_dir)
            with mock.patch.dict(os.environ, {"PATH": ""}, clear=False):
                status = service.status()
            self.assertEqual(status["status"], "installed_restart_required")
            self.assertTrue(status["restart_required"])

            runtime_path = os.pathsep.join(str(path) for path in gpu_runtime_bin_dirs(service.runtime_dir))
            with mock.patch.dict(os.environ, {"PATH": runtime_path}, clear=False):
                status = service.status()
            self.assertEqual(status["status"], "ready")
            self.assertEqual(status["source"], "local")

    def test_install_runs_in_background_and_activates_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = root / "package"
            package.mkdir()
            (package / "requirements-windows-worker-gpu.lock.txt").write_text("test==1\n", encoding="utf-8")
            service = WorkerGpuRuntimeService(root / "data", package)

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                target = Path(command[command.index("--target") + 1])
                create_runtime_files(target)
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.dict(os.environ, {"PATH": ""}, clear=False),
                mock.patch("app.worker_gpu_runtime_service.subprocess.run", side_effect=fake_run),
            ):
                started = service.start_install()
                self.assertIn(started["status"], {"installing", "installed_restart_required"})
                deadline = time.monotonic() + 3
                while service.status()["status"] == "installing" and time.monotonic() < deadline:
                    time.sleep(0.01)
                finished = service.status()
            self.assertEqual(finished["status"], "installed_restart_required")
            self.assertEqual(finished["job"]["state"], "completed")
            self.assertTrue((service.runtime_dir / "nvidia" / "cudnn" / "bin" / "cudnn64_9.dll").is_file())
