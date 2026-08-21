from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.worker_readiness_service import WorkerReadinessService


class WorkerReadinessServiceTest(unittest.TestCase):
    def ready_inputs(self, root: Path) -> dict[str, object]:
        return {
            "settings": SimpleNamespace(path_map=[("/media", str(root))]),
            "compute_enabled": True,
            "controller_synced_at": 123.0,
            "gpus": [{"name": "NVIDIA GeForce RTX 5090", "memory_total_mb": 32768}],
            "gpu_runtime": {"status": "ready"},
            "models": [{"id": "large-v3-turbo", "label": "large-v3-turbo", "active": True, "installed": True, "verified": True}],
            "recommended_model": "large-v3-turbo",
            "ffmpeg_bin": "ffmpeg",
        }

    def test_ready_scan_verifies_read_write_and_removes_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "media.mkv").write_bytes(b"video")
            service = WorkerReadinessService()
            encoder_output = " V..... av1_nvenc NVIDIA NVENC av1 encoder\n V..... hevc_nvenc NVIDIA NVENC hevc encoder"
            with patch("app.worker_readiness_service.shutil.which", return_value="ffmpeg"), patch(
                "app.worker_readiness_service.subprocess.run",
                return_value=subprocess.CompletedProcess(["ffmpeg"], 0, stdout=encoder_output, stderr=""),
            ):
                result = service.scan(**self.ready_inputs(root))

            self.assertTrue(result["ready"])
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["next_action"]["id"], "none")
            self.assertFalse(list(root.glob(".moviemuse-worker-write-test-*.tmp")))
            checks = {item["id"]: item for item in result["checks"]}
            self.assertEqual(checks["media_read"]["status"], "pass")
            self.assertEqual(checks["media_write"]["status"], "pass")
            self.assertEqual(checks["ffmpeg"]["status"], "pass")

    def test_missing_path_map_is_blocking_and_points_to_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = WorkerReadinessService()
            payload = self.ready_inputs(Path(temp_dir))
            payload["settings"] = SimpleNamespace(path_map=[])
            with patch("app.worker_readiness_service.shutil.which", return_value="ffmpeg"), patch(
                "app.worker_readiness_service.subprocess.run",
                return_value=subprocess.CompletedProcess(["ffmpeg"], 0, stdout="av1_nvenc", stderr=""),
            ):
                result = service.scan(**payload)

            self.assertFalse(result["ready"])
            self.assertGreaterEqual(result["blocking_count"], 3)
            self.assertEqual(result["next_action"]["id"], "controller")
            self.assertEqual(result["next_action"]["check_id"], "path_map")

    def test_disabled_compute_is_first_action_when_environment_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = WorkerReadinessService()
            payload = self.ready_inputs(Path(temp_dir))
            payload["compute_enabled"] = False
            with patch("app.worker_readiness_service.shutil.which", return_value="ffmpeg"), patch(
                "app.worker_readiness_service.subprocess.run",
                return_value=subprocess.CompletedProcess(["ffmpeg"], 0, stdout="av1_nvenc", stderr=""),
            ):
                result = service.scan(**payload)

            self.assertFalse(result["ready"])
            self.assertEqual(result["blocking_count"], 0)
            self.assertEqual(result["next_action"]["id"], "start")

    def test_blocking_runtime_is_repaired_before_starting_compute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = WorkerReadinessService()
            payload = self.ready_inputs(Path(temp_dir))
            payload["compute_enabled"] = False
            payload["gpu_runtime"] = {"status": "installing", "job": {"message": "正在安装 GPU 运行环境"}}
            with patch("app.worker_readiness_service.shutil.which", return_value="ffmpeg"), patch(
                "app.worker_readiness_service.subprocess.run",
                return_value=subprocess.CompletedProcess(["ffmpeg"], 0, stdout="av1_nvenc", stderr=""),
            ):
                result = service.scan(**payload)

            checks = {item["id"]: item for item in result["checks"]}
            self.assertEqual(checks["gpu_runtime"]["status"], "fail")
            self.assertEqual(result["next_action"]["id"], "gpu_runtime")


if __name__ == "__main__":
    unittest.main()
