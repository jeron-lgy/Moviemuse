from __future__ import annotations

import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.worker_service import (
    DownloadControl,
    MODEL_BY_ID,
    WorkerModelService,
    WorkerRuntimeControl,
    whisper_model_recommendation,
)


class WorkerModelServiceTests(unittest.TestCase):
    def test_large_v3_turbo_uses_current_public_repository(self) -> None:
        self.assertEqual(
            MODEL_BY_ID["large-v3-turbo"]["repo_id"],
            "dropbox-dash/faster-whisper-large-v3-turbo",
        )

    def test_gpu_memory_recommends_matching_whisper_model(self) -> None:
        high = whisper_model_recommendation([{"name": "RTX 5090", "memory_total_mb": 32768}])
        medium = whisper_model_recommendation([{"name": "RTX 2060", "memory_total_mb": 6144}])
        entry = whisper_model_recommendation([{"name": "GTX 1650", "memory_total_mb": 4096}])
        cpu = whisper_model_recommendation([])

        self.assertEqual(high["recommended_model"], "large-v3-turbo")
        self.assertEqual(medium["recommended_model"], "medium")
        self.assertEqual(entry["recommended_model"], "small")
        self.assertEqual(cpu["recommended_model"], "base")

    def test_worker_runtime_starts_enabled_and_can_toggle(self) -> None:
        runtime = WorkerRuntimeControl()

        self.assertTrue(runtime.snapshot()["compute_enabled"])
        runtime.set_enabled(False)
        self.assertFalse(runtime.snapshot()["compute_enabled"])
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            runtime.require_enabled()
        runtime.set_enabled(True)
        runtime.require_enabled()

    def create_model(self, root: Path, model_id: str) -> Path:
        model_dir = root / model_id
        model_dir.mkdir(parents=True)
        for name in ("config.json", "model.bin", "tokenizer.json"):
            (model_dir / name).write_bytes(b"model-data")
        return model_dir

    def test_installed_model_is_detected_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_model(root, "large-v3-turbo")
            service = WorkerModelService(root)

            model = next(item for item in service.models("large-v3-turbo") if item["id"] == "large-v3-turbo")

            self.assertTrue(model["installed"])
            self.assertTrue(model["verified"])
            self.assertTrue(model["active"])
            self.assertGreater(model["actual_size_bytes"], 0)

    def test_validation_reports_missing_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "medium").mkdir()
            (root / "medium" / "config.json").write_text("{}", encoding="utf-8")
            service = WorkerModelService(root)

            result = service.validate("medium")

            self.assertFalse(result["valid"])
            self.assertEqual(result["missing_files"], ["model.bin", "tokenizer.json"])

    def test_active_model_cannot_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_model(root, "large-v3")
            service = WorkerModelService(root)

            with self.assertRaises(PermissionError):
                service.remove("large-v3", "large-v3")

            self.assertTrue((root / "large-v3").exists())

    def test_inactive_model_can_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_model(root, "small")
            service = WorkerModelService(root)

            service.remove("small", "large-v3")

            self.assertFalse((root / "small").exists())

    def test_model_version_check_detects_available_update_and_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = self.create_model(root, "medium")
            (model_dir / ".moviemuse-model.json").write_text(
                '{"model_id":"medium","repo_id":"Systran/faster-whisper-medium","revision":"old-revision"}',
                encoding="utf-8",
            )
            service = WorkerModelService(root)

            with patch.object(service, "_remote_revision", return_value="new-revision") as remote:
                service.check_updates(force=True)
                service.check_updates(force=False)

            model = next(item for item in service.models() if item["id"] == "medium")
            self.assertEqual(model["version_status"], "update_available")
            self.assertEqual(model["local_revision"], "old-revision")
            self.assertEqual(model["latest_revision"], "new-revision")
            self.assertEqual(remote.call_count, 1)

    def test_external_model_without_manifest_reports_unknown_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_model(root, "small")
            service = WorkerModelService(root)

            with patch.object(service, "_remote_revision", return_value="remote-revision"):
                service.check_updates(force=True)

            model = next(item for item in service.models() if item["id"] == "small")
            self.assertEqual(model["version_status"], "local_version_unknown")
            self.assertEqual(model["latest_revision"], "remote-revision")

    def test_incomplete_model_download_is_prepared_as_atomic_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            incomplete = root / "medium"
            incomplete.mkdir()
            (incomplete / "config.json").write_text("{}", encoding="utf-8")
            service = WorkerModelService(root)

            with patch.object(service, "_start_thread"):
                job = service.start_download("medium")

            self.assertTrue(job["replace_existing"])
            self.assertEqual(job["state"], "queued")
            self.assertTrue(service.jobs_file.exists())

    def test_interrupted_job_is_restored_as_resumable_pause(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = WorkerModelService(root)
            with patch.object(service, "_start_thread"):
                job = service.start_download("small")
            service._update(job["id"], state="downloading")

            restored = WorkerModelService(root)
            snapshot = restored.downloads()[0]

            self.assertEqual(snapshot["state"], "paused")
            self.assertIn("重启", snapshot["error"])

    def test_legacy_turbo_download_job_migrates_repository_and_retry_uses_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = WorkerModelService(root)
            with patch.object(service, "_start_thread"):
                job = service.start_download("large-v3-turbo")
            service._update(
                job["id"],
                state="failed",
                repo_id="Systran/faster-whisper-large-v3-turbo",
                error="Repository Not Found",
            )

            restored = WorkerModelService(root)
            migrated = restored.downloads()[0]
            with patch.object(restored, "_start_thread"):
                retried = restored.resume(migrated["id"])

            expected = "dropbox-dash/faster-whisper-large-v3-turbo"
            self.assertEqual(migrated["repo_id"], expected)
            self.assertEqual(retried["repo_id"], expected)
            persisted = restored.jobs_file.read_text(encoding="utf-8")
            self.assertIn(expected, persisted)
            self.assertNotIn("Systran/faster-whisper-large-v3-turbo", persisted)

    def test_partial_file_resumes_with_http_range(self) -> None:
        class FakeResponse:
            status_code = 206

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_bytes(self, chunk_size: int):
                self.chunk_size = chunk_size
                yield b"def"

        class FakeClient:
            def __init__(self):
                self.headers = None

            def stream(self, _method: str, _url: str, headers: dict[str, str]):
                self.headers = headers
                return FakeResponse()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = WorkerModelService(root)
            staging = service.download_dir / "resume.partial"
            staging.mkdir(parents=True)
            partial = staging / "model.bin.moviemuse-part"
            partial.write_bytes(b"abc")
            job_id = "resume-job"
            service._jobs[job_id] = {
                "id": job_id,
                "model_id": "base",
                "repo_id": "Systran/faster-whisper-base",
                "revision": "revision",
                "state": "downloading",
                "created_at": 1,
            }
            control = DownloadControl(threading.Event(), threading.Event())
            service._controls[job_id] = control
            client = FakeClient()

            downloaded = service._download_file(
                job_id=job_id,
                control=control,
                client=client,  # type: ignore[arg-type]
                staging=staging,
                filename="model.bin",
                expected_size=6,
                completed_bytes=0,
                total_bytes=6,
            )

            self.assertEqual(client.headers, {"Range": "bytes=3-"})
            self.assertEqual(downloaded, 6)
            self.assertEqual((staging / "model.bin").read_bytes(), b"abcdef")

    def test_atomic_update_replaces_existing_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = self.create_model(root, "base")
            (target / "version.txt").write_text("old", encoding="utf-8")
            staging = root / ".downloads" / "base-update.partial"
            staging.mkdir(parents=True)
            for name in ("config.json", "model.bin", "tokenizer.json"):
                (staging / name).write_bytes(b"new-model")
            (staging / "version.txt").write_text("new", encoding="utf-8")
            service = WorkerModelService(root)

            service._install_staging("update-job", staging, target, True)

            self.assertEqual((target / "version.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse(any(service.download_dir.glob("*.backup")))

    def test_download_pipeline_installs_verified_flat_model(self) -> None:
        files = {
            "config.json": b"{}",
            "model.bin": b"model-bytes",
            "tokenizer.json": b"{}",
        }

        class FakeResponse:
            status_code = 200

            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_bytes(self, chunk_size: int):
                self.chunk_size = chunk_size
                yield self.payload

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def stream(self, _method: str, url: str, headers: dict[str, str]):
                self.headers = headers
                filename = url.rsplit("/", 1)[-1]
                return FakeResponse(files[filename])

        class FakeApi:
            def __init__(self, **_kwargs):
                pass

            def model_info(self, _repo_id: str, files_metadata: bool):
                self.files_metadata = files_metadata
                return SimpleNamespace(
                    sha="revision",
                    siblings=[SimpleNamespace(rfilename=name, size=len(payload)) for name, payload in files.items()],
                )

        module = types.ModuleType("huggingface_hub")
        module.HfApi = FakeApi  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = WorkerModelService(root)
            with patch.object(service, "_start_thread"):
                job = service.start_download("base")

            with patch.dict("sys.modules", {"huggingface_hub": module}), patch(
                "app.worker_service.httpx.Client", return_value=FakeClient()
            ):
                service._download(job["id"])

            snapshot = next(item for item in service.downloads() if item["id"] == job["id"])
            self.assertEqual(snapshot["state"], "completed")
            self.assertTrue(service.validate("base")["valid"])
            self.assertTrue((root / "base" / ".moviemuse-model.json").exists())


if __name__ == "__main__":
    unittest.main()
