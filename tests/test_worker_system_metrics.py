from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase, mock


class WorkerSystemMetricsTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="moviemuse-worker-metrics-")
        os.environ["APP_DATA_DIR"] = self.temporary.name
        import app.main as main

        self.main = main

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_worker_discovery_only_probes_one_loopback_address(self) -> None:
        network = self.main.worker_discovery_network("127.0.0.1")

        self.assertEqual(str(network), "127.0.0.1/32")
        self.assertEqual([str(address) for address in network.hosts()], ["127.0.0.1"])

    def test_worker_discovery_resolves_local_hostname(self) -> None:
        with mock.patch.object(self.main.socket, "gethostbyname", return_value="192.168.2.9"):
            network = self.main.worker_discovery_network("moviemuse.local")

        self.assertEqual(str(network), "192.168.2.0/24")

    def test_controller_reports_reachable_worker_as_waiting_for_pairing(self) -> None:
        with (
            mock.patch.object(self.main, "backend_url", return_value="http://192.168.2.46:18181"),
            mock.patch.object(
                self.main,
                "remote_get_with_timeout",
                side_effect=self.main.HTTPException(status_code=401, detail="字幕 API token 不正确"),
            ),
            mock.patch.object(self.main, "load_subtitle_jobs_cache", return_value={}),
            mock.patch.object(self.main, "console_settings_payload", return_value={}),
        ):
            status = self.main.subtitle_backend_status()

        self.assertFalse(status["online"])
        self.assertTrue(status["reachable"])
        self.assertTrue(status["pairing_required"])
        self.assertEqual(status["status"], "pairing_required")

    def test_backend_connection_test_restores_masked_pairing_token(self) -> None:
        response = mock.MagicMock()
        response.json.return_value = {"hardware": {}, "settings": {}, "jobs": []}
        client = mock.MagicMock()
        client.get.return_value = response
        context = mock.MagicMock()
        context.__enter__.return_value = client

        with (
            mock.patch.object(
                self.main,
                "load_compute_config",
                return_value={"subtitle_backend_token": "paired-token"},
            ),
            mock.patch.object(self.main, "remote_http_client", return_value=context),
        ):
            result = self.main.test_subtitle_backend(
                subtitle_backend_url="http://192.168.2.46:18181",
                subtitle_backend_token=self.main.SECRET_PLACEHOLDER,
            )

        self.assertTrue(result["online"])
        client.get.assert_called_once_with(
            "http://192.168.2.46:18181/api/subtitle/node/status",
            headers={"X-API-Key": "paired-token"},
        )

    def test_cpu_usage_uses_system_time_deltas(self) -> None:
        usage = self.main.cpu_usage_from_system_times(
            (100, 1_000, 1_000),
            (150, 1_100, 1_100),
        )
        self.assertEqual(usage, 75)

    def test_gpu_probe_never_creates_a_windows_console(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["nvidia-smi"],
            returncode=0,
            stdout="NVIDIA GeForce RTX 5090, 32768, 1024, 12, 45, 610.88\n",
        )
        with (
            mock.patch.object(self.main.shutil, "which", return_value="C:/Windows/nvidia-smi.exe"),
            mock.patch.object(self.main.subprocess, "run", return_value=completed) as run,
        ):
            result = self.main.gpu_summary()

        self.assertEqual(result[0]["name"], "NVIDIA GeForce RTX 5090")
        self.assertEqual(run.call_args.kwargs["creationflags"], self.main.WINDOWS_CREATE_NO_WINDOW)

    def test_ffprobe_never_creates_a_windows_console(self) -> None:
        completed = subprocess.CompletedProcess(args=["ffprobe"], returncode=0, stdout='{"streams":[]}')
        with (
            mock.patch.object(self.main.shutil, "which", return_value="C:/ffmpeg/ffprobe.exe"),
            mock.patch.object(self.main.subprocess, "run", return_value=completed) as run,
        ):
            result = self.main.run_ffprobe(Path("C:/Media/movie.mkv"))

        self.assertEqual(result, {"streams": []})
        self.assertEqual(run.call_args.kwargs["creationflags"], self.main.WINDOWS_CREATE_NO_WINDOW)

    def test_running_transcode_progress_is_not_reported_as_an_error(self) -> None:
        progress_line = "frame=87974 fps=260 time=00:46:35.02 speed=8.66x"
        with (
            mock.patch.object(self.main, "transcode_jobs_payload", return_value=[{
                "id": "transcode-1",
                "input_path": "C:/Media/movie.mkv",
                "status": "running",
                "progress_percent": 33,
                "message": progress_line,
                "stderr_tail": progress_line,
                "updated_at": 2,
            }]),
            mock.patch.object(self.main, "get_subtitle_service") as subtitle_service,
        ):
            subtitle_service.return_value.list_jobs.return_value = []
            items = self.main.worker_activity_items()

        self.assertEqual(items[0]["status"], "running")
        self.assertEqual(items[0]["error"], "")
        self.assertEqual(items[0]["message"], progress_line)

    def test_failed_transcode_uses_stderr_as_error_fallback(self) -> None:
        failure_line = "Error while opening encoder"
        with (
            mock.patch.object(self.main, "transcode_jobs_payload", return_value=[{
                "id": "transcode-2",
                "input_path": "C:/Media/movie.mkv",
                "status": "failed",
                "stderr_tail": failure_line,
                "updated_at": 2,
            }]),
            mock.patch.object(self.main, "get_subtitle_service") as subtitle_service,
        ):
            subtitle_service.return_value.list_jobs.return_value = []
            items = self.main.worker_activity_items()

        self.assertEqual(items[0]["error"], failure_line)

    def test_controller_payload_does_not_override_worker_local_settings(self) -> None:
        payload = self.main.remote_compute_settings_payload({
            "whisper_model": "large-v3-turbo",
            "whisper_model_dir": "C:/wrong-machine/models",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "subtitle_max_workers": 4,
            "subtitle_workers_auto": True,
        })
        self.assertEqual(payload["whisper_model"], "large-v3-turbo")
        self.assertEqual(payload["subtitle_max_workers"], 999)
        self.assertNotIn("whisper_model_dir", payload)
        self.assertNotIn("whisper_device", payload)
        self.assertNotIn("whisper_compute_type", payload)

    def test_worker_clamps_controller_concurrency_to_safe_limits(self) -> None:
        gpu = [{"name": "RTX", "memory_total_mb": 12 * 1024}]
        with mock.patch.object(self.main, "gpu_summary", return_value=gpu):
            payload = self.main.normalize_worker_compute_payload({
                "subtitle_max_workers": 4,
                "translation_max_workers": 4,
                "whisper_device": "cpu",
            }, {})
        self.assertEqual(payload["whisper_device"], "cuda")
        self.assertEqual(payload["whisper_compute_type"], "float16")
        self.assertEqual(payload["subtitle_max_workers"], 1)
        self.assertLessEqual(payload["translation_max_workers"], 2)
        self.assertEqual(payload["controller_requested_subtitle_max_workers"], 4)

    def test_postprocess_limit_uses_worker_safe_cap(self) -> None:
        limit = self.main.effective_postprocess_worker_limit(
            {"max_concurrency": 8},
            {"effective_config": {"safe_limits": {"transcode": 2}}},
        )
        self.assertEqual(limit, 2)
