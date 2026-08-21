from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path
from unittest import TestCase, mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "windows-backend" / "desktop_launcher.py"
SPEC = importlib.util.spec_from_file_location("worker_desktop_launcher", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class WorkerDesktopLauncherTests(TestCase):
    def test_configured_port_falls_back_for_invalid_values(self) -> None:
        with mock.patch.dict(os.environ, {"PORT": "invalid"}):
            self.assertEqual(launcher.configured_port(), 18181)
        with mock.patch.dict(os.environ, {"PORT": "70000"}):
            self.assertEqual(launcher.configured_port(), 18181)

    def test_backend_environment_keeps_controller_authority(self) -> None:
        root = Path("C:/MovieMuse-Worker")
        with mock.patch.dict(os.environ, {}, clear=True):
            env = launcher._backend_environment(root, 18184)
        self.assertEqual(env["COMPUTE_NODE_ONLY"], "1")
        self.assertEqual(env["HOST"], "0.0.0.0")
        self.assertEqual(env["PORT"], "18184")
        self.assertEqual(env["WHISPER_MODEL"], "large-v3-turbo")
        self.assertTrue(env["WHISPER_MODEL_DIR"].endswith("whisper-models"))

    def test_backend_environment_loads_downloaded_gpu_runtime(self) -> None:
        root = Path("C:/MovieMuse-Worker")
        data_root = Path("C:/MovieMuse-Data")
        cublas = data_root / "gpu-runtime" / "nvidia" / "cublas" / "bin"
        cudnn = data_root / "gpu-runtime" / "nvidia" / "cudnn" / "bin"
        with (
            mock.patch.dict(os.environ, {"APP_DATA_DIR": str(data_root), "PATH": "C:/Windows/System32"}, clear=True),
            mock.patch.object(Path, "is_dir", autospec=True, side_effect=lambda path: path in {cublas, cudnn}),
        ):
            env = launcher._backend_environment(root, 18181)
        path_items = env["PATH"].split(os.pathsep)
        self.assertEqual(path_items[:2], [str(cublas), str(cudnn)])

    def test_default_data_root_uses_local_app_data(self) -> None:
        root = Path("C:/MovieMuse-Worker")
        result = launcher._default_data_root(root, {"LOCALAPPDATA": "C:/Users/test/AppData/Local"})
        self.assertEqual(result, Path("C:/Users/test/AppData/Local/MovieMuse Worker"))

    def test_legacy_worker_data_is_copied_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="moviemuse-worker-migration-") as temporary:
            root = Path(temporary) / "release"
            legacy = root / "data" / "local-backend"
            target = Path(temporary) / "local-app-data" / "MovieMuse Worker"
            model = legacy / "whisper-models" / "large-v3-turbo" / "model.bin"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"model")

            launcher._migrate_legacy_data(root, target)
            self.assertEqual((target / "whisper-models" / "large-v3-turbo" / "model.bin").read_bytes(), b"model")
            self.assertTrue((target / ".migrated-from-package-data").is_file())

            model.write_bytes(b"newer-legacy-copy")
            launcher._migrate_legacy_data(root, target)
            self.assertEqual((target / "whisper-models" / "large-v3-turbo" / "model.bin").read_bytes(), b"model")

    def test_legacy_worker_data_skips_live_webview_and_logs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="moviemuse-worker-transient-migration-") as temporary:
            root = Path(temporary) / "release"
            legacy = root / "data" / "local-backend"
            target = Path(temporary) / "local-app-data" / "MovieMuse Worker"
            (legacy / "webview" / "EBWebView" / "Default" / "Network").mkdir(parents=True)
            (legacy / "webview" / "EBWebView" / "Default" / "Network" / "Cookies").write_bytes(b"locked")
            (legacy / "logs").mkdir()
            (legacy / "logs" / "worker-backend.log").write_text("old log", encoding="utf-8")
            (legacy / "compute_settings.json").write_text("{}", encoding="utf-8")

            launcher._migrate_legacy_data(root, target)

            self.assertTrue((target / "compute_settings.json").is_file())
            self.assertFalse((target / "webview").exists())
            self.assertFalse((target / "logs").exists())
            self.assertTrue((target / ".migrated-from-package-data").is_file())

    def test_legacy_worker_data_is_discovered_in_sibling_release(self) -> None:
        with tempfile.TemporaryDirectory(prefix="moviemuse-worker-sibling-migration-") as temporary:
            releases = Path(temporary)
            old_root = releases / "Moviemuse-Windows-Worker-v2.3.3-win64"
            transient_root = releases / "Moviemuse-Windows-Worker-v2.4.0-win64"
            target = releases / "local-app-data" / "MovieMuse Worker"
            runtime = old_root / "data" / "local-backend" / "gpu-runtime" / "runtime.dll"
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b"runtime")
            transient_webview = transient_root / "data" / "local-backend" / "webview"
            transient_webview.mkdir(parents=True)
            (transient_webview / "Cookies").write_bytes(b"transient")
            new_root = releases / "Moviemuse-Windows-Worker-v2.4.1-win64"
            new_root.mkdir(parents=True)

            launcher._migrate_legacy_data(new_root, target)

            self.assertEqual((target / "gpu-runtime" / "runtime.dll").read_bytes(), b"runtime")
            self.assertTrue((target / ".migrated-from-package-data").is_file())

    def test_error_page_escapes_runtime_details(self) -> None:
        page = launcher._error_html("<failed>", "token=<secret>")
        self.assertIn("&lt;failed&gt;", page)
        self.assertIn("token=&lt;secret&gt;", page)
        self.assertNotIn("token=<secret>", page)

    def test_worker_url_never_enables_demo_mode(self) -> None:
        self.assertEqual(launcher.worker_url(18181), "http://127.0.0.1:18181/worker")
        self.assertNotIn("demo", launcher.worker_url(18181))

    def test_worker_status_accepts_worker_started_before_runtime_switch(self) -> None:
        payload = {
            "status": "ok",
            "hostname": "WINDOWS-WORKER",
            "hardware": {"gpus": []},
            "effective_config": {"source": "controller"},
        }
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = __import__("json").dumps(payload).encode("utf-8")
        response.__enter__.return_value = response
        with mock.patch.object(launcher._url_opener, "open", return_value=response):
            self.assertEqual(launcher._worker_status(18181), payload)
