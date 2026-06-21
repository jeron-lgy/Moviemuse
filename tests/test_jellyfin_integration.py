from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class JellyfinIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="moviemuse-jellyfin-integration-"))
        os.environ["MEDIA_DIRS"] = str(self.root)
        os.environ["APP_DATA_DIR"] = str(self.root / "data")
        os.environ["TRASH_DIR"] = str(self.root / "trash")
        os.environ["SUBTITLE_BACKEND_URL"] = ""
        os.environ["SUBTITLE_API_TOKEN"] = ""
        import app.main as main

        self.main = main
        main.postprocess_service = None
        main.subscription_service = None
        main.system_settings_service = None
        main.subtitle_service = None
        main.app_log_service = None
        with main.transcode_jobs_lock:
            main.transcode_jobs.clear()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_resolve_jellyfin_media_prefers_matching_media_source(self) -> None:
        request = self.main.JellyfinIntegrationRequest(item_id="item-1", media_source_id="source-b", title="ABF-358")
        item = {
            "Id": "item-1",
            "Name": "Ignored",
            "MediaSources": [
                {"Id": "source-a", "Path": "/media/A.mkv", "Size": 10},
                {
                    "Id": "source-b",
                    "Path": "/media/ABF-358.mkv",
                    "Size": 20,
                    "MediaStreams": [{"Type": "Video", "Width": 1920, "Height": 1080}],
                },
            ],
        }

        with patch.object(self.main, "jellyfin_config", return_value={"url": "http://jf", "api_key": "key"}), \
            patch.object(self.main, "fetch_jellyfin_item", return_value=item):
            media = self.main.resolve_jellyfin_media(request)

        self.assertEqual(media["path"], "/media/ABF-358.mkv")
        self.assertEqual(media["media_source_id"], "source-b")
        self.assertEqual(media["resolution"], "1920x1080")

    def test_resolve_jellyfin_media_uses_subscription_db_before_api(self) -> None:
        self.main.get_subscription_service().subscribe_av({
            "id": "SNOS-250",
            "title": "known title",
            "status": "in_library",
            "jellyfin_item_id": "jf-known",
            "jellyfin_item_name": "SNOS-250",
            "jellyfin_path": "/media/study3/SNOS-250.mp4",
        })
        request = self.main.JellyfinIntegrationRequest(item_id="jf-known", title="SNOS-250")

        with patch.object(self.main, "fetch_jellyfin_item", side_effect=AssertionError("Jellyfin API should not be called")):
            media = self.main.resolve_jellyfin_media(request)

        self.assertEqual(media["source"], "subscription_db")
        self.assertEqual(media["matched_by"], "item_id")
        self.assertEqual(media["path"], "/media/study3/SNOS-250.mp4")
        self.assertEqual(media["av_id"], "SNOS-250")

    def test_transcode_integration_uses_postprocess_output_settings(self) -> None:
        video = self.root / "ABF-358.mkv"
        video.write_text("sample", encoding="utf-8")
        self.main.get_postprocess_service().update_settings({
            "output_dir": str(self.root / "encoded"),
            "target_codec": "h265",
            "target_encoder": "libx265",
            "preset": "ultrafast",
        })

        with patch.object(self.main, "create_transcode_job", return_value={"id": "job-1", "status": "queued"}):
            result = self.main.submit_jellyfin_transcode_job({
                "item_id": "item-1",
                "title": "ABF-358",
                "path": str(video),
            })

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["job_id"], "job-1")
        self.assertIn(str(self.root / "encoded" / "ABF-358"), result["output_path"])


if __name__ == "__main__":
    unittest.main()
