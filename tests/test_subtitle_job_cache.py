from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient


class SubtitleJobCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="moviemuse-subtitle-cache-"))
        os.environ["APP_DATA_DIR"] = str(self.root / "data")
        os.environ["MEDIA_DIRS"] = str(self.root)
        os.environ["TRASH_DIR"] = str(self.root / "trash")
        import app.main as main

        self.main = main

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_remote_subtitle_jobs_fall_back_to_unraid_cache(self) -> None:
        self.main.save_subtitle_jobs_cache([
            {
                "id": "job-1",
                "status": "completed",
                "video_path": "/media/processed/MIDA-001.mp4",
                "created_at": 1,
                "updated_at": 2,
            },
            {
                "id": "job-2",
                "status": "translating",
                "video_path": "/media/processed/MIDA-002.mp4",
                "created_at": 3,
                "updated_at": 4,
            },
        ])

        with patch.object(self.main, "backend_url", return_value="http://worker"), \
            patch.object(self.main, "remote_get", side_effect=HTTPException(status_code=502, detail="worker offline")):
            payload = self.main.api_list_subtitle_jobs(limit=0)

        self.assertTrue(payload["cached"])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["active"], 1)
        self.assertEqual([job["id"] for job in payload["jobs"]], ["job-1", "job-2"])
        self.assertIn("worker offline", payload["backend_error"])

    def test_remote_subtitle_job_delete_removes_cache_item(self) -> None:
        self.main.save_subtitle_jobs_cache([
            {"id": "job-1", "status": "completed"},
            {"id": "job-2", "status": "completed"},
        ])

        with patch.object(self.main, "backend_url", return_value="http://worker"), \
            patch.object(self.main, "remote_delete", return_value={"status": "ok"}):
            payload = self.main.api_delete_subtitle_job("job-1")

        self.assertEqual(payload["status"], "ok")
        cached = self.main.load_subtitle_jobs_cache(limit=0)
        self.assertEqual([job["id"] for job in cached["jobs"]], ["job-2"])

    def test_direct_remote_job_result_can_be_cached(self) -> None:
        self.main.upsert_subtitle_job_cache(
            self.main.subtitle_job_from_result({"id": "job-direct", "status": "queued"})
        )

        cached = self.main.load_subtitle_jobs_cache(limit=0)
        self.assertEqual(cached["jobs"][0]["id"], "job-direct")

    def test_secret_placeholder_restores_existing_or_keeps_current_setting(self) -> None:
        restored = self.main.restore_secret_placeholders(
            {"openai_api_key": "********", "openai_model": "deepseek-chat"},
            {"openai_api_key": "sk-real"},
        )
        self.assertEqual(restored["openai_api_key"], "sk-real")

        without_existing = self.main.restore_secret_placeholders(
            {"openai_api_key": "********", "openai_model": "deepseek-chat"},
            {},
        )
        self.assertNotIn("openai_api_key", without_existing)
        self.assertEqual(without_existing["openai_model"], "deepseek-chat")

    def test_controller_node_status_does_not_start_local_compute_service(self) -> None:
        with patch.object(self.main, "backend_url", return_value="http://worker"), \
            patch.object(self.main, "compute_node_only", return_value=False), \
            patch.object(self.main, "local_node_status", side_effect=AssertionError("must stay lazy")):
            payload = self.main.api_subtitle_node_status()

        self.assertEqual(payload["status"], "controller")
        self.assertEqual(payload["mode"], "remote_controller")
        self.assertFalse(payload["online"])

    def test_remote_compute_settings_include_path_map_for_worker_boundary(self) -> None:
        payload = self.main.remote_compute_settings_payload(
            {
                "whisper_model": "large-v3-turbo",
                "subtitle_path_map": r"/media=\\NAS\media",
                "subtitle_backend_url": "http://worker",
            }
        )

        self.assertEqual(payload["subtitle_path_map"], r"/media=\\NAS\media")
        self.assertNotIn("subtitle_backend_url", payload)

    def test_connection_sync_uses_saved_controller_path_map(self) -> None:
        self.main.save_console_compute_config(
            {
                "subtitle_backend_url": "http://worker:18181",
                "subtitle_backend_token": "worker-token",
                "subtitle_path_map": r"/media=\\NAS\media",
                "whisper_model": "large-v3-turbo",
            }
        )
        captured: dict[str, object] = {}

        def fake_remote_post(path: str, payload: dict[str, object], timeout: float = 30) -> dict[str, object]:
            captured.update({"path": path, "payload": payload, "timeout": timeout})
            return {"status": "ok"}

        with patch.object(self.main, "backend_url", return_value="http://worker:18181"), \
            patch.object(self.main, "remote_post_json", side_effect=fake_remote_post):
            result = self.main.sync_remote_compute_settings()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(captured["path"], "/api/compute/settings")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["subtitle_path_map"], r"/media=\\NAS\media")
        self.assertEqual(payload["whisper_model"], "large-v3-turbo")
        self.assertNotIn("subtitle_backend_url", payload)
        self.assertNotIn("subtitle_backend_token", payload)

    def test_subtitle_upload_rejects_file_over_configured_limit(self) -> None:
        with patch.dict(os.environ, {"SUBTITLE_UPLOAD_MAX_BYTES": str(1024 ** 2)}), \
            patch.object(self.main, "backend_url", return_value=""), \
            TestClient(self.main.app) as client:
            response = client.post(
                "/api/subtitle/upload",
                files={"file": ("large.mp4", b"x" * (1024 ** 2 + 1), "video/mp4")},
            )

        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
