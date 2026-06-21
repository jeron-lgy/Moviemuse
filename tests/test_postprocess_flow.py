from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe is required for postprocess flow tests")


def make_sample_video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=5:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


class PostprocessFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        require_ffmpeg()
        self.root = Path(tempfile.mkdtemp(prefix="moviemuse-postprocess-test-"))
        os.environ["MEDIA_DIRS"] = str(self.root)
        os.environ["APP_DATA_DIR"] = str(self.root / "data")
        os.environ["TRASH_DIR"] = str(self.root / "trash")
        os.environ["SUBTITLE_BACKEND_URL"] = ""
        os.environ["SUBTITLE_API_TOKEN"] = ""
        os.environ["TRANSCODE_TIMEOUT_SECONDS"] = "120"
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

    def wait_for_task(self, task_id: str) -> dict[str, object]:
        post = self.main.get_postprocess_service()
        for _ in range(80):
            task = post.get_task(task_id)
            assert task is not None
            if task["status"] not in {"ready_to_run", "sent_to_worker", "worker_done", "transcode_validating", "jellyfin_refreshing"}:
                return task
            time.sleep(0.25)
        task = post.get_task(task_id)
        assert task is not None
        return task

    def configure_postprocess(self, output_dir: Path) -> None:
        self.main.get_postprocess_service().update_settings(
            {
                "auto_transcode_enabled": True,
                "auto_subtitle_enabled": False,
                "download_dir": str(self.root),
                "output_dir": str(output_dir),
                "target_codec": "h265",
                "crf": 32,
                "preset": "ultrafast",
            }
        )

    def test_build_postprocess_output_path_omits_codec_suffix(self) -> None:
        output_root = self.root / "out"
        settings = {"output_dir": str(output_root), "target_codec": "h265"}

        subscription = self.main.build_postprocess_output_path(
            {"av_id": "IPZZ-872", "input_path": str(self.root / "IPZZ-872.mp4"), "target_codec": "av1"},
            settings,
        )
        wash = self.main.build_postprocess_output_path(
            {"av_id": "ABF-359", "task_type": "wash_chinese", "input_path": str(self.root / "ABF-359.mkv")},
            settings,
        )

        self.assertEqual(subscription, str(output_root / "IPZZ-872" / "IPZZ-872.mp4"))
        self.assertEqual(wash, str(output_root / "ABF-359" / "ABF-359.mkv"))

        leading_zero = self.main.build_postprocess_output_path(
            {"av_id": "SNOS-71", "task_type": "external_qb", "input_path": str(self.root / "SNOS-071-4K.mkv")},
            settings,
        )
        self.assertEqual(leading_zero, str(output_root / "SNOS-071" / "SNOS-071-4K.mkv"))

    def test_transcode_validation_copies_metadata_sidecars(self) -> None:
        source_dir = self.root / "study3"
        source_dir.mkdir(parents=True)
        input_path = source_dir / "IPZZ-872.mp4"
        input_path.write_bytes(b"input-video")
        sidecars = {
            "nfo": source_dir / "IPZZ-872.nfo",
            "fanart": source_dir / "IPZZ-872-fanart.jpg",
            "poster": source_dir / "IPZZ-872-poster.jpg",
            "thumb": source_dir / "IPZZ-872-thumb.jpg",
        }
        for kind, path in sidecars.items():
            path.write_text(f"{kind}-content", encoding="utf-8")
        output_path = self.root / "out" / "IPZZ-872" / "IPZZ-872.mp4"
        output_path.parent.mkdir(parents=True)
        output_path.write_bytes(b"output-video")

        self.configure_postprocess(self.root / "out")
        post = self.main.get_postprocess_service()
        task = post.create_task(
            av_id="IPZZ-872",
            task_type="subscription",
            status="worker_done",
            target_codec="av1",
        )
        post.update_task(task["id"], input_path=str(input_path), output_path=str(output_path))

        original_validate = self.main.validate_video_output

        def fake_validate(path: str, **kwargs: object) -> dict[str, object]:
            return {
                "ok": True,
                "path": path,
                "file_size": Path(path).stat().st_size,
                "mtime": Path(path).stat().st_mtime,
                "codec_name": "av1",
            }

        self.main.validate_video_output = fake_validate
        try:
            result = self.main.validate_and_activate_postprocess_task(task["id"], output_path=str(output_path))
        finally:
            self.main.validate_video_output = original_validate

        self.assertEqual(result["status"], "completed")
        for filename in ("IPZZ-872.nfo", "IPZZ-872-fanart.jpg", "IPZZ-872-poster.jpg", "IPZZ-872-thumb.jpg"):
            self.assertTrue((output_path.parent / filename).exists(), filename)
            self.assertFalse((source_dir / filename).exists(), filename)
        updated = post.get_task(task["id"])
        assert updated is not None
        self.assertEqual(updated["data"]["metadata_sidecars"]["status"], "ok")
        self.assertEqual(len(updated["data"]["metadata_sidecars"]["moved"]), 4)
        events = post.list_events(task["id"])
        self.assertTrue(any(event["stage"] == "metadata_sidecars_moved" for event in events))

    def test_nfo_actor_repair_syncs_actor_within_single_movie_directory(self) -> None:
        output_root = self.root / "out"
        movie_dir = output_root / "MIDA-609"
        movie_dir.mkdir(parents=True)
        (movie_dir / "MIDA-609.nfo").write_text(
            '<?xml version="1.0" encoding="utf-8"?><movie><title>MIDA-609</title><actor><name>神宫寺奈绪</name><type>Actor</type></actor><fileinfo /></movie>',
            encoding="utf-8",
        )
        (movie_dir / "movie.nfo").write_text(
            '<?xml version="1.0" encoding="utf-8"?><movie><title>MIDA-609</title><tag>神宫寺奈绪</tag><fileinfo /></movie>',
            encoding="utf-8",
        )
        self.main.get_postprocess_service().update_settings({"output_dir": str(output_root)})

        preview = self.main.nfo_actor_repair_candidates(apply=False)
        self.assertEqual(preview["repairable_dirs"], 1)
        self.assertEqual(preview["target_files"], 1)
        self.assertEqual(preview["items"][0]["target_files"], ["movie.nfo"])

        applied = self.main.nfo_actor_repair_candidates(apply=True)
        self.assertEqual(applied["repaired_files"], 1)
        repaired_text = (movie_dir / "movie.nfo").read_text(encoding="utf-8")
        self.assertIn("<actor>", repaired_text)
        self.assertIn("<name>神宫寺奈绪</name>", repaired_text)
        self.assertTrue(list(movie_dir.glob("movie.nfo.bak-*")))

    @unittest.skipIf(os.name == "nt", "POSIX readonly replacement semantics are tested on Linux")
    def test_nfo_actor_repair_replaces_readonly_target_file(self) -> None:
        output_root = self.root / "out"
        movie_dir = output_root / "ABF-358"
        movie_dir.mkdir(parents=True)
        (movie_dir / "ABF-358.av1.nfo").write_text(
            '<movie><title>ABF-358</title><actor><name>凉森玲梦</name><type>Actor</type></actor></movie>',
            encoding="utf-8",
        )
        target = movie_dir / "movie.nfo"
        target.write_text(
            '<movie><title>ABF-358</title><tag>凉森玲梦</tag></movie>',
            encoding="utf-8",
        )
        target.chmod(0o444)
        self.main.get_postprocess_service().update_settings({"output_dir": str(output_root)})

        try:
            applied = self.main.nfo_actor_repair_candidates(apply=True)
        finally:
            target.chmod(0o644)

        self.assertEqual(applied["repaired_files"], 1)
        self.assertIn("<name>凉森玲梦</name>", target.read_text(encoding="utf-8"))
        self.assertTrue(list(movie_dir.glob("movie.nfo.bak-*")))

    def test_nfo_actor_repair_skips_output_root_to_avoid_cross_movie_copy(self) -> None:
        output_root = self.root / "out"
        output_root.mkdir(parents=True)
        (output_root / "ABF-361.nfo").write_text(
            '<movie><title>ABF-361</title><actor><name>中森ななみ</name><type>Actor</type></actor></movie>',
            encoding="utf-8",
        )
        (output_root / "SNOS-209.nfo").write_text(
            '<movie><title>SNOS-209</title><tag>瀬戸環奈</tag></movie>',
            encoding="utf-8",
        )
        self.main.get_postprocess_service().update_settings({"output_dir": str(output_root)})

        preview = self.main.nfo_actor_repair_candidates(apply=False)

        self.assertEqual(preview["repairable_dirs"], 0)
        self.assertEqual(preview["target_files"], 0)
        self.assertTrue(any(item["reason"] == "skip_output_root" for item in preview["skipped"]))

    def test_jellyfin_actor_refresh_finds_item_with_missing_people(self) -> None:
        output_root = self.root / "out"
        output_root.mkdir(parents=True)
        (output_root / "SONE-250.nfo").write_text(
            '<movie><title>SONE-250</title><actor><name>葵司</name><type>Actor</type></actor></movie>',
            encoding="utf-8",
        )
        (output_root / "SONE-250.mp4").write_bytes(b"video")
        self.main.get_postprocess_service().update_settings({"output_dir": str(output_root)})

        with patch.object(self.main, "jellyfin_config", return_value={"url": "http://jf", "api_key": "key"}), \
            patch.object(self.main, "find_jellyfin_item_for_video", return_value={
                "Id": "item-1",
                "Name": "SONE-250",
                "Path": str(output_root / "SONE-250.mp4"),
                "People": [],
            }):
            preview = self.main.jellyfin_actor_refresh_candidates(apply=False)

        self.assertEqual(preview["actor_nfos"], 1)
        self.assertEqual(preview["matched_items"], 1)
        self.assertEqual(preview["target_items"], 1)
        self.assertEqual(preview["items"][0]["actors"], ["葵司"])

    def test_jellyfin_actor_refresh_triggers_only_missing_people_items(self) -> None:
        output_root = self.root / "out"
        output_root.mkdir(parents=True)
        (output_root / "SONE-250.nfo").write_text(
            '<movie><title>SONE-250</title><actor><name>葵司</name><type>Actor</type></actor></movie>',
            encoding="utf-8",
        )
        (output_root / "SONE-250.mp4").write_bytes(b"video")
        self.main.get_postprocess_service().update_settings({"output_dir": str(output_root)})

        with patch.object(self.main, "jellyfin_config", return_value={"url": "http://jf", "api_key": "key"}), \
            patch.object(self.main, "find_jellyfin_item_for_video", return_value={
                "Id": "item-1",
                "Name": "SONE-250",
                "Path": str(output_root / "SONE-250.mp4"),
                "People": [],
            }), \
            patch.object(self.main, "refresh_jellyfin_item_metadata", return_value={"status": "refreshed"}) as refresh:
            applied = self.main.jellyfin_actor_refresh_candidates(apply=True)

        self.assertEqual(applied["target_items"], 1)
        self.assertEqual(applied["refreshed_items"], 1)
        refresh.assert_called_once()

    def test_postprocess_schema_migrates_existing_partial_table(self) -> None:
        from app.postprocess_service import PostprocessService

        data_dir = self.root / "migrate-data"
        data_dir.mkdir()
        db_file = data_dir / "subscriptions.sqlite3"
        with sqlite3.connect(db_file) as conn:
            conn.execute("CREATE TABLE postprocess_tasks (id TEXT PRIMARY KEY)")

        service = PostprocessService(data_dir)
        task = service.create_task(av_id="TEST-MIGRATE", task_type="subscription")
        with sqlite3.connect(db_file) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(postprocess_tasks)").fetchall()}

        self.assertIn("torrent_hash", columns)
        self.assertIn("finished_at", columns)
        self.assertEqual(task["av_id"], "TEST-MIGRATE")

    def test_subscription_transcode_activates_version(self) -> None:
        input_path = self.root / "input.mp4"
        output_path = self.root / "out" / "TEST-001.h265.mkv"
        make_sample_video(input_path)
        self.configure_postprocess(self.root / "out")
        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="TEST-001", task_type="subscription", status="ready_to_run", target_codec="h265")
        post.update_task(task["id"], input_path=str(input_path), output_path=str(output_path))

        result = self.main.dispatch_postprocess_task(post.get_task(task["id"]))
        self.assertEqual(result["status"], "sent_to_worker")
        final_task = self.wait_for_task(task["id"])

        self.assertEqual(final_task["status"], "completed")
        self.assertFalse(input_path.exists())
        self.assertTrue(output_path.exists())
        active = post.active_version("TEST-001")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active["path"], str(output_path))
        self.assertEqual(active["status"], "active")

    def test_video_validation_rejects_duration_mismatch(self) -> None:
        output_path = self.root / "duration-output.mp4"
        source_path = self.root / "duration-source.mp4"
        make_sample_video(output_path)
        make_sample_video(source_path)
        original_probe_duration = self.main.probe_video_duration
        self.main.probe_video_duration = lambda path: 60.0
        try:
            result = self.main.validate_video_output(str(output_path), source_path=str(source_path))
        finally:
            self.main.probe_video_duration = original_probe_duration

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "输出时长与源文件差异过大")
        self.assertEqual(result["source_duration"], 60.0)

    def test_video_validation_rejects_abnormal_output_shape_and_size(self) -> None:
        tiny_resolution = self.root / "tiny-resolution.mkv"
        tiny_file = self.root / "tiny-file.mkv"
        tiny_resolution.write_bytes(b"0" * 4096)
        tiny_file.write_bytes(b"0")
        original_probe = self.main.run_ffprobe

        def fake_probe_low_resolution(path: Path) -> dict[str, object]:
            return {
                "format": {"duration": "10"},
                "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 32, "height": 18}],
            }

        def fake_probe_normal_resolution(path: Path) -> dict[str, object]:
            return {
                "format": {"duration": "10"},
                "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 1280, "height": 720}],
            }

        try:
            self.main.run_ffprobe = fake_probe_low_resolution
            low_resolution = self.main.validate_video_output(str(tiny_resolution), target_codec="h265")
            self.main.run_ffprobe = fake_probe_normal_resolution
            low_size = self.main.validate_video_output(str(tiny_file), target_codec="h265")
        finally:
            self.main.run_ffprobe = original_probe

        self.assertFalse(low_resolution["ok"])
        self.assertEqual(low_resolution["width"], 32)
        self.assertFalse(low_size["ok"])
        self.assertEqual(low_size["file_size"], 1)

    def test_output_conflict_checks_registered_tasks_and_versions(self) -> None:
        output_path = self.root / "out" / "TEST-001.h265.mkv"
        self.configure_postprocess(self.root / "out")
        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="TEST-001", task_type="subscription", status="ready_to_run")
        other = post.create_task(av_id="TEST-001", task_type="subscription", status="ready_to_run")
        post.update_task(task["id"], output_path=str(output_path))

        task_conflict = self.main.avoid_output_conflict(str(output_path), other["id"])
        self.assertNotEqual(task_conflict, str(output_path))
        self.assertIn(other["id"][:8], task_conflict)

        version_path = self.root / "out" / "TEST-002.h265.mkv"
        post.add_version(
            av_id="TEST-002",
            path=str(version_path),
            source_type="subscription",
            codec="h265",
            status="active",
            generated_by="moviemuse",
        )
        version_conflict = self.main.avoid_output_conflict(str(version_path), other["id"])
        self.assertNotEqual(version_conflict, str(version_path))
        self.assertIn(other["id"][:8], version_conflict)

    def test_output_conflict_ignores_terminal_records_without_files(self) -> None:
        output_path = self.root / "out" / "TEST-003.h265.mkv"
        self.configure_postprocess(self.root / "out")
        post = self.main.get_postprocess_service()
        failed_task = post.create_task(av_id="TEST-003", task_type="subscription", status="failed")
        post.update_task(failed_task["id"], output_path=str(output_path), status="failed")
        failed_version = post.add_version(
            av_id="TEST-003",
            path=str(output_path),
            source_type="subscription",
            codec="h265",
            status="failed",
            generated_by="moviemuse",
        )

        self.assertFalse(self.main.output_path_conflicts(str(output_path), "new-task"))

        post.update_version(failed_version["id"], status="active")
        self.assertTrue(self.main.output_path_conflicts(str(output_path), "new-task"))

    def test_activate_version_requires_supersede_when_active_exists(self) -> None:
        post = self.main.get_postprocess_service()
        old = post.add_version(
            av_id="TEST-ACTIVE",
            path=str(self.root / "old-active.mkv"),
            source_type="subscription",
            codec="h265",
            status="active",
            generated_by="moviemuse",
        )
        new = post.add_version(
            av_id="TEST-ACTIVE",
            path=str(self.root / "new-ready.mkv"),
            source_type="subscription",
            codec="h265",
            status="ready",
            generated_by="moviemuse",
        )

        result = post.activate_version(new["id"])

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(post.get_version(old["id"])["status"], "active")
        self.assertEqual(post.get_version(new["id"])["status"], "ready")

    def test_postprocess_activation_conflict_marks_new_version_failed(self) -> None:
        input_path = self.root / "conflict-input.mp4"
        output_root = self.root / "conflict-out"
        make_sample_video(input_path)
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": False,
                "download_dir": str(self.root),
                "output_dir": str(output_root),
            }
        )
        old = post.add_version(
            av_id="TEST-CONFLICT",
            path=str(self.root / "existing-active.mkv"),
            source_type="subscription",
            codec="h265",
            status="active",
            generated_by="moviemuse",
        )
        task = post.create_task(av_id="TEST-CONFLICT", task_type="subscription", status="ready_to_run")
        post.update_task(task["id"], input_path=str(input_path))

        result = self.main.dispatch_postprocess_task(post.get_task(task["id"]))

        versions = post.list_versions("TEST-CONFLICT")
        failed_versions = [version for version in versions if version["status"] == "failed"]
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(post.get_task(task["id"])["status"], "conflict")
        self.assertEqual(post.get_version(old["id"])["status"], "active")
        self.assertTrue(failed_versions)
        self.assertIn("activation_conflict", failed_versions[0]["metadata"])

    def test_wash_replaces_only_managed_active_version(self) -> None:
        input_path = self.root / "download" / "TEST-002.mp4"
        output_root = self.root / "out"
        old_path = output_root / "TEST-002" / "TEST-002.h265.mkv"
        new_path = output_root / "TEST-002" / "TEST-002.chinese.h265.mkv"
        make_sample_video(input_path)
        old_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, old_path)
        self.configure_postprocess(output_root)
        post = self.main.get_postprocess_service()
        stat = old_path.stat()
        old = post.add_version(
            av_id="TEST-002",
            path=str(old_path),
            source_type="subscription",
            codec="h265",
            status="active",
            generated_by="moviemuse",
            file_size=stat.st_size,
            mtime=stat.st_mtime,
        )
        task = post.create_task(
            av_id="TEST-002",
            task_type="wash_chinese",
            status="ready_to_run",
            supersede_version_id=old["id"],
            supersede_path=str(old_path),
            target_codec="h265",
        )
        post.update_task(task["id"], input_path=str(input_path), output_path=str(new_path))

        result = self.main.dispatch_postprocess_task(post.get_task(task["id"]))
        self.assertEqual(result["status"], "sent_to_worker")
        final_task = self.wait_for_task(task["id"])

        self.assertEqual(final_task["status"], "completed")
        self.assertFalse(input_path.exists())
        self.assertFalse(old_path.exists())
        self.assertTrue(new_path.exists())
        versions = post.list_versions("TEST-002")
        statuses = {version["id"]: version["status"] for version in versions}
        self.assertEqual(statuses[old["id"]], "trashed")
        self.assertTrue(any(version["status"] == "active" and version["path"] == str(new_path) for version in versions))
        trash_files = [path for path in (self.root / "trash").rglob("*") if path.is_file()]
        self.assertTrue(trash_files)

    def test_activation_failure_does_not_move_old_active_file_first(self) -> None:
        input_path = self.root / "download" / "TEST-018.mp4"
        output_root = self.root / "activation-fail-out"
        old_path = output_root / "TEST-018" / "TEST-018.h265.mkv"
        new_path = output_root / "TEST-018" / "TEST-018.chinese.h265.mkv"
        make_sample_video(input_path)
        old_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, old_path)
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": False,
                "download_dir": str(input_path.parent),
                "output_dir": str(output_root),
            }
        )
        stat = old_path.stat()
        old = post.add_version(
            av_id="TEST-018",
            path=str(old_path),
            source_type="subscription",
            codec="h265",
            status="active",
            generated_by="moviemuse",
            file_size=stat.st_size,
            mtime=stat.st_mtime,
        )
        task = post.create_task(
            av_id="TEST-018",
            task_type="wash_chinese",
            status="ready_to_run",
            supersede_version_id=old["id"],
            supersede_path=str(old_path),
        )
        post.update_task(task["id"], input_path=str(input_path), output_path=str(new_path))
        original_activate = post.activate_version

        def fake_activate(*args: object, **kwargs: object) -> dict[str, object]:
            return {"status": "conflict", "message": "unit activation failure"}

        post.activate_version = fake_activate
        try:
            result = self.main.dispatch_postprocess_task(post.get_task(task["id"]))
        finally:
            post.activate_version = original_activate

        self.assertEqual(result["status"], "conflict")
        self.assertTrue(old_path.exists())
        self.assertEqual(post.get_version(old["id"])["status"], "active")
        failed_versions = [version for version in post.list_versions("TEST-018") if version["status"] == "failed"]
        self.assertTrue(failed_versions)
        self.assertIn("activation_conflict", failed_versions[0]["metadata"])

    def test_legacy_jellyfin_wash_replacement_is_disabled(self) -> None:
        old_path = self.root / "library-old.mp4"
        make_sample_video(old_path)

        result = self.main.complete_wash_replacement(
            "TEST-LEGACY",
            "chinese",
            new_item={"id": "new", "name": "new", "path": str(self.root / "library-new.mp4")},
        )
        waiting = self.main.complete_wash_if_jellyfin_ready(
            {
                "id": "TEST-LEGACY",
                "wash": {"mode": "chinese", "status": "downloading", "old_path": str(old_path)},
                "jellyfin_path": str(old_path),
            }
        )

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(waiting["status"], "ignored")
        self.assertTrue(old_path.exists())

    def test_qb_protection_requires_category_tags_and_download_dir(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "download_dir": "/study3",
                "allowed_categories": ["study3"],
                "required_tags": ["moviemuse", "auto-postprocess", "jav"],
            }
        )
        row = {
            "category": "study3",
            "tags": "moviemuse,auto-postprocess,jav",
            "save_path": "/study3",
        }

        accepted = self.main.qb_protection_check(row, {}, "/study3/TEST-003/TEST-003.mkv")
        self.assertTrue(accepted["ok"])

        wrong_category = self.main.qb_protection_check({**row, "category": "manual"}, {}, "/study3/TEST-003.mkv")
        self.assertFalse(wrong_category["ok"])

        missing_tag = self.main.qb_protection_check({**row, "tags": "moviemuse,jav"}, {}, "/study3/TEST-003.mkv")
        self.assertFalse(missing_tag["ok"])

        wrong_dir = self.main.qb_protection_check(row, {}, "/downloads/TEST-003.mkv")
        self.assertFalse(wrong_dir["ok"])

        sibling_prefix = self.main.qb_protection_check(row, {}, "/study33/TEST-003.mkv")
        self.assertFalse(sibling_prefix["ok"])

        wrong_selected = self.main.qb_protection_check(row, {}, "/study3/TEST-003", "/downloads/TEST-003.mkv")
        self.assertFalse(wrong_selected["ok"])
        self.assertIn("选中视频路径不在", wrong_selected["reason"])

        wrong_save_prefix = self.main.qb_protection_check({**row, "save_path": "/study3-other"}, {}, "/study3/TEST-003.mkv")
        self.assertFalse(wrong_save_prefix["ok"])

    def test_mteam_filter_audit_records_reject_reasons(self) -> None:
        torrents = [
            {"id": "1", "title": "SNOS-233 1080p", "smallDescr": "", "labels": [], "size": "2 GB"},
            {"id": "2", "title": "SNOS-233 CHS 2160p", "smallDescr": "", "labels": ["中字"], "size": "80 GB"},
            {"id": "3", "title": "SNOS-233 CHS 2160p", "smallDescr": "", "labels": ["中字"], "size": "10 GB"},
        ]

        chinese_audit = self.main.mteam_filter_audit(torrents, {"only_chinese": True})
        fourk_limited = self.main.mteam_filter_audit(torrents, {"only_uhd": True, "max_size_mb": 60 * 1024})

        self.assertFalse(chinese_audit[0]["matched"])
        self.assertEqual(chinese_audit[0]["reasons"][0]["code"], "missing_chinese")
        self.assertFalse(fourk_limited[0]["matched"])
        self.assertEqual(fourk_limited[0]["reasons"][0]["code"], "missing_uhd")
        self.assertFalse(fourk_limited[1]["matched"])
        self.assertEqual(fourk_limited[1]["reasons"][0]["code"], "size_too_large")
        self.assertTrue(fourk_limited[2]["matched"])

    def test_qb_existing_torrent_labels_are_applied(self) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self) -> None:
                self.posts: list[tuple[str, dict[str, str]]] = []

            def post(self, url: str, data: dict[str, str]) -> FakeResponse:
                self.posts.append((url, data))
                return FakeResponse()

        client = FakeClient()

        result = self.main.ensure_qb_torrent_labels(
            client,
            "http://qb",
            "abc123",
            {"category": "study3", "tags": "moviemuse,auto-postprocess,jav"},
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(client.posts[0][0], "http://qb/api/v2/torrents/setCategory")
        self.assertEqual(client.posts[0][1], {"hashes": "abc123", "category": "study3"})
        self.assertEqual(client.posts[1][0], "http://qb/api/v2/torrents/addTags")
        self.assertEqual(client.posts[1][1], {"hashes": "abc123", "tags": "moviemuse,auto-postprocess,jav"})

    def test_qb_hash_cannot_be_rebound_to_another_task(self) -> None:
        post = self.main.get_postprocess_service()
        first = post.create_task(av_id="TEST-A", task_type="subscription", status="created")
        second = post.create_task(av_id="TEST-B", task_type="subscription", status="created")
        qb_config = {"category": "study3", "tags": "moviemuse,auto-postprocess,jav", "save_path": "/study3"}

        first_bind = self.main.bind_qb_to_postprocess_task(first, {"status": "ok", "hash": "samehash"}, qb_config)
        second_bind = self.main.bind_qb_to_postprocess_task(second, {"status": "ok", "hash": "samehash"}, qb_config)

        rebound = post.get_task(second["id"])
        qb_row = post.get_qb_torrent("samehash")
        self.assertEqual(first_bind["status"], "bound")
        self.assertEqual(second_bind["status"], "conflict")
        self.assertEqual(rebound["status"], "conflict")
        self.assertEqual(rebound["error_code"], "torrent_hash_conflict")
        self.assertEqual(qb_row["task_id"], first["id"])

    def test_postprocess_qb_config_merges_required_tags(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "download_dir": str(self.root / "downloads"),
                "allowed_categories": ["study3"],
                "required_tags": ["moviemuse", "auto-postprocess", "jav"],
            }
        )

        config = self.main.postprocess_qb_config({"tags": "custom,jav"})
        tags = {item.strip().lower() for item in str(config["tags"]).split(",") if item.strip()}

        self.assertEqual(Path(config["save_path"]), self.root / "downloads")
        self.assertEqual(config["category"], "study3")
        self.assertEqual(tags, {"custom", "jav", "moviemuse", "auto-postprocess"})

    def test_poll_qb_adopts_external_tagged_torrent(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "external_qb_adopt_enabled": True,
                "download_dir": str(self.root / "downloads"),
                "allowed_categories": ["study3"],
                "required_tags": ["moviemuse"],
                "target_codec": "av1",
            }
        )

        class FakeResponse:
            def __init__(self, payload: object) -> None:
                self.payload = payload

            def json(self) -> object:
                return self.payload

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def get(self, url: str, params: dict[str, str] | None = None) -> FakeResponse:
                if url.endswith("/api/v2/torrents/info"):
                    return FakeResponse([
                        {
                            "hash": "external-hash",
                            "name": "ABF-359.mp4",
                            "progress": 0.42,
                            "state": "downloading",
                            "content_path": str(self_path / "downloads" / "ABF-359.mp4"),
                            "save_path": str(self_path / "downloads"),
                            "category": "study3",
                            "tags": "moviemuse",
                            "size": 1234,
                        }
                    ])
                return FakeResponse([])

            def post(self, url: str, data: dict[str, str] | None = None) -> FakeResponse:
                return FakeResponse("Ok.")

        self_path = self.root
        self.main.get_system_settings_service().update({"qbittorrent": {"url": "http://qb"}})
        original_client = self.main.httpx.Client
        self.main.httpx.Client = FakeClient
        try:
            result = self.main.poll_qb_postprocess_once()
        finally:
            self.main.httpx.Client = original_client

        task = post.list_tasks(limit=10)[0]
        qb_row = post.get_qb_torrent("external-hash")
        self.assertEqual(result["adoption"]["adopted"], 1)
        self.assertEqual(task["av_id"], "ABF-359")
        self.assertEqual(task["task_type"], "external_qb")
        self.assertEqual(task["status"], "downloading")
        self.assertEqual(qb_row["task_id"], task["id"])
        self.assertEqual(qb_row["status"], "downloading")

    def test_external_qb_task_keeps_source_file_after_completion(self) -> None:
        post = self.main.get_postprocess_service()
        download_dir = self.root / "downloads"
        output_dir = self.root / "output"
        input_path = download_dir / "ABF-359.mp4"
        make_sample_video(input_path)
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": False,
                "download_dir": str(download_dir),
                "output_dir": str(output_dir),
            }
        )
        task = post.create_task(av_id="ABF-359", task_type="external_qb", status="ready_to_run")
        post.update_task(task["id"], input_path=str(input_path))

        result = self.main.validate_and_activate_postprocess_task(task["id"])

        completed = post.get_task(task["id"])
        events = post.list_events(task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(input_path.exists())
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["data"]["source_trash"]["status"], "skipped")
        self.assertTrue(any(event["stage"] == "source_trashing_skipped" for event in events))

    def test_external_qb_task_trashes_source_when_enabled(self) -> None:
        post = self.main.get_postprocess_service()
        download_dir = self.root / "downloads"
        output_dir = self.root / "output"
        input_path = download_dir / "ABF-360.mp4"
        make_sample_video(input_path)
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": False,
                "external_qb_trash_source_enabled": True,
                "download_dir": str(download_dir),
                "output_dir": str(output_dir),
            }
        )
        task = post.create_task(av_id="ABF-360", task_type="external_qb", status="ready_to_run")
        post.update_task(task["id"], input_path=str(input_path))

        result = self.main.validate_and_activate_postprocess_task(task["id"])

        completed = post.get_task(task["id"])
        events = post.list_events(task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(input_path.exists())
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["data"]["source_trash"]["status"], "moved")
        self.assertTrue(Path(completed["data"]["source_trash"]["target"]).exists())
        self.assertTrue(any(event["stage"] == "source_trashing" for event in events))

    def test_postprocess_cancel_syncs_wash_subscription(self) -> None:
        service = self.main.get_subscription_service()
        post = self.main.get_postprocess_service()
        av_id = "TEST-WASH-CANCEL"
        service.subscribe_av({"id": av_id, "title": "wash cancel", "status": "in_library"})
        task = post.create_task(av_id=av_id, task_type="wash_chinese", status="ready_to_run")
        service.update_av_wash(av_id, {"mode": "chinese", "status": "requested", "task_id": task["id"]})

        with patch.object(self.main, "current_console_user", return_value="test"):
            with TestClient(self.main.app) as client:
                response = client.post(f"/api/postprocess/tasks/{task['id']}/cancel")

        subscription = next(item for item in service.get_subscribed_av() if item["id"] == av_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.get_task(task["id"])["status"], "ignored")
        self.assertEqual(subscription["wash"]["status"], "cancelled")
        self.assertEqual(subscription["wash"]["task_id"], task["id"])

    def test_completed_wash_postprocess_syncs_subscription_status(self) -> None:
        service = self.main.get_subscription_service()
        post = self.main.get_postprocess_service()
        av_id = "TEST-WASH-SYNC"
        output_path = str(self.root / "out" / av_id / f"{av_id}.chinese.mp4")
        service.subscribe_av({"id": av_id, "title": "wash sync", "status": "in_library"})
        task = post.create_task(av_id=av_id, task_type="wash_chinese", status="completed")
        post.update_task(task["id"], output_path=output_path)
        service.update_av_wash(av_id, {"mode": "chinese", "status": "downloading", "task_id": task["id"]})

        result = self.main.sync_completed_wash_postprocess_tasks()

        subscription = next(item for item in service.get_subscribed_av() if item["id"] == av_id)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["synced"][0]["task_id"], task["id"])
        self.assertEqual(subscription["wash"]["status"], "completed")
        self.assertEqual(subscription["wash"]["download_status"], "completed")
        self.assertEqual(subscription["wash"]["new_path"], output_path)
        self.assertEqual(subscription["wash"]["task_id"], task["id"])
        self.assertTrue(any(event["stage"] == "wash_status_synced" for event in post.list_events(task["id"])))

    def test_postprocess_delete_removes_terminal_task_record(self) -> None:
        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="DELETE-FAILED-001", task_type="subscription", status="failed")
        post.add_event(task["id"], "error", "failed", "测试失败任务")

        with patch.object(self.main, "current_console_user", return_value="test"):
            with TestClient(self.main.app) as client:
                response = client.delete(f"/api/postprocess/tasks/{task['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(post.get_task(task["id"]))
        self.assertEqual(post.list_events(task["id"]), [])

    def test_postprocess_delete_rejects_active_task(self) -> None:
        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="DELETE-ACTIVE-001", task_type="subscription", status="ready_to_run")

        with patch.object(self.main, "current_console_user", return_value="test"):
            with TestClient(self.main.app) as client:
                response = client.delete(f"/api/postprocess/tasks/{task['id']}")

        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(post.get_task(task["id"]))

    def test_wash_api_cancel_stops_existing_postprocess_task(self) -> None:
        service = self.main.get_subscription_service()
        post = self.main.get_postprocess_service()
        av_id = "TEST-WASH-API-CANCEL"
        service.subscribe_av({"id": av_id, "title": "wash api cancel", "status": "in_library"})
        task = post.create_task(av_id=av_id, task_type="wash_chinese", status="ready_to_run")
        service.update_av_wash(av_id, {"mode": "chinese", "status": "requested", "task_id": task["id"]})

        with patch.object(self.main, "current_console_user", return_value="test"):
            with TestClient(self.main.app) as client:
                response = client.post(f"/api/subscriptions/av/{av_id}/wash", json={"mode": "chinese", "status": "cancelled"})

        subscription = next(item for item in service.get_subscribed_av() if item["id"] == av_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.get_task(task["id"])["status"], "ignored")
        self.assertEqual(subscription["wash"]["status"], "cancelled")
        self.assertEqual(subscription["wash"]["task_id"], task["id"])

    def test_worker_done_requires_token_and_ignores_terminal_task(self) -> None:
        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="TEST-CALLBACK", task_type="subscription", status="ignored")
        os.environ["SUBTITLE_API_TOKEN"] = "secret"
        try:
            with TestClient(self.main.app) as client:
                unauth = client.post(f"/api/postprocess/tasks/{task['id']}/worker-done", json={"status": "worker_done"})
                auth = client.post(
                    f"/api/postprocess/tasks/{task['id']}/worker-done",
                    headers={"X-API-Key": "secret"},
                    json={"status": "worker_done", "output_path": str(self.root / "missing.mp4")},
                )
        finally:
            os.environ["SUBTITLE_API_TOKEN"] = ""

        self.assertEqual(unauth.status_code, 401)
        self.assertEqual(auth.status_code, 200)
        self.assertEqual(auth.json()["status"], "ignored")
        self.assertEqual(post.get_task(task["id"])["status"], "ignored")

    def test_transcode_job_keeps_callback_token_for_remote_callback(self) -> None:
        job = self.main.create_transcode_job(
            {
                "input_path": str(self.root / "input.mp4"),
                "output_path": str(self.root / "output.mp4"),
                "callback_url": "http://controller/api/postprocess/tasks/task-id/worker-done",
                "callback_token": "secret",
            },
            start=False,
        )

        self.assertEqual(job["callback_token"], "secret")

    def test_subscription_postprocess_task_fails_when_mteam_result_has_no_id(self) -> None:
        service = self.main.get_subscription_service()
        post = self.main.get_postprocess_service()
        av_id = "TEST-MTEAM-NO-ID"
        service.subscribe_av({"id": av_id, "title": "missing torrent id", "status": "pending"})
        original_search = self.main.search_mteam
        self.main.search_mteam = lambda *args, **kwargs: {"results": [{"id": "", "title": f"{av_id} no id"}]}
        try:
            result = self.main.download_av_from_mteam({"id": av_id, "title": "missing torrent id"}, save_to_subscription=True)
        finally:
            self.main.search_mteam = original_search

        tasks = [task for task in post.list_tasks(limit=20) if task["av_id"] == av_id]
        self.assertEqual(result["status"], "error")
        self.assertTrue(tasks)
        self.assertEqual(tasks[0]["status"], "failed")
        self.assertEqual(tasks[0]["error_code"], "mteam_missing_id")

    def test_qb_file_pick_failure_updates_qb_row_status(self) -> None:
        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="TEST-QB", task_type="subscription", status="torrent_pushed")
        post.bind_qb_torrent(
            task_id=task["id"],
            av_id="TEST-QB",
            torrent_hash="hash-file-pick",
            category="study3",
            tags="moviemuse,auto-postprocess,jav",
            save_path=str(self.root),
        )
        post.update_qb_torrent("hash-file-pick", size=100)

        class FakeResponse:
            def __init__(self, payload: object) -> None:
                self.payload = payload

            def json(self) -> object:
                return self.payload

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def get(self, url: str, params: dict[str, str] | None = None) -> FakeResponse:
                if url.endswith("/api/v2/torrents/info"):
                    return FakeResponse([
                        {
                            "progress": 1.0,
                            "state": "completed",
                            "content_path": str(self_path / "TEST-QB"),
                            "save_path": str(self_path),
                            "category": "study3",
                            "tags": "moviemuse,auto-postprocess,jav",
                            "size": 100,
                        }
                    ])
                if url.endswith("/api/v2/torrents/files"):
                    return FakeResponse([])
                return FakeResponse({})

            def post(self, url: str, data: dict[str, str] | None = None) -> FakeResponse:
                return FakeResponse("Ok.")

        self_path = self.root
        original_client = self.main.httpx.Client
        self.main.httpx.Client = FakeClient
        try:
            result = self.main.refresh_qb_torrent_status(
                post.get_qb_torrent("hash-file-pick"),
                post.get_task(task["id"]),
                {"url": "http://qb", "username": "", "password": ""},
            )
        finally:
            self.main.httpx.Client = original_client

        updated_task = post.get_task(task["id"])
        updated_qb = post.get_qb_torrent("hash-file-pick")
        self.assertEqual(result["status"], "file_pick_failed")
        self.assertEqual(updated_task["status"], "failed")
        self.assertEqual(updated_qb["status"], "file_pick_failed")

    def test_qb_category_is_created_when_missing(self) -> None:
        class FakeResponse:
            def __init__(self, payload: dict[str, object] | None = None, status_code: int = 200, text: str = "") -> None:
                self.payload = payload or {}
                self.status_code = status_code
                self.text = text

            def json(self) -> dict[str, object]:
                return self.payload

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self) -> None:
                self.posts: list[tuple[str, dict[str, str]]] = []

            def get(self, url: str) -> FakeResponse:
                self.last_get = url
                return FakeResponse({})

            def post(self, url: str, data: dict[str, str]) -> FakeResponse:
                self.posts.append((url, data))
                return FakeResponse()

        client = FakeClient()

        result = self.main.ensure_qb_category(
            client,
            "http://qb",
            {"category": "study3", "save_path": "/study3"},
        )

        self.assertEqual(result["status"], "created")
        self.assertEqual(client.last_get, "http://qb/api/v2/torrents/categories")
        self.assertEqual(client.posts, [("http://qb/api/v2/torrents/createCategory", {"category": "study3", "savePath": "/study3"})])

    def test_qb_category_skips_when_existing(self) -> None:
        class FakeResponse:
            status_code = 200
            text = ""

            def json(self) -> dict[str, object]:
                return {"study3": {"savePath": "/study3"}}

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self) -> None:
                self.posts: list[tuple[str, dict[str, str]]] = []

            def get(self, url: str) -> FakeResponse:
                self.last_get = url
                return FakeResponse()

            def post(self, url: str, data: dict[str, str]) -> FakeResponse:
                self.posts.append((url, data))
                return FakeResponse()

        client = FakeClient()

        result = self.main.ensure_qb_category(
            client,
            "http://qb",
            {"category": "study3", "save_path": "/study3"},
        )

        self.assertEqual(result["status"], "exists")
        self.assertEqual(client.last_get, "http://qb/api/v2/torrents/categories")
        self.assertEqual(client.posts, [])

    def test_local_postprocess_file_ready_requires_stable_readable_file(self) -> None:
        video_path = self.root / "SNOS-233.mkv"
        video_path.write_bytes(b"0" * 4096)

        ready = self.main.local_postprocess_file_ready(str(video_path), expected_size=4096)
        missing = self.main.local_postprocess_file_ready(str(self.root / "missing.mkv"), expected_size=4096)
        mismatch = self.main.local_postprocess_file_ready(str(video_path), expected_size=8 * 1024 * 1024)

        self.assertTrue(ready["ok"])
        self.assertFalse(missing["ok"])
        self.assertIn("控制端无法读取下载文件", missing["reason"])
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["reason"], "下载文件本地大小与 qB 记录差异过大")

    def test_pick_main_video_handles_single_file_content_path(self) -> None:
        picked = self.main.pick_main_video_file(
            [{"name": "SNOS-233.mkv", "size": 1234}],
            "SNOS-233",
            "/study3/SNOS-233.mkv",
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["path"], "/study3/SNOS-233.mkv")

        nested = self.main.pick_main_video_file(
            [{"name": "Disc/SNOS-233.mkv", "size": 1234}],
            "SNOS-233",
            "/study3/SNOS-233",
        )
        self.assertIsNotNone(nested)
        assert nested is not None
        self.assertEqual(nested["path"], "/study3/SNOS-233/Disc/SNOS-233.mkv")

        leading_zero = self.main.pick_main_video_file(
            [{"name": "ACHJ-083.mp4", "size": 1234}],
            "ACHJ-83",
            "/study3/ACHJ-083.mp4",
        )
        self.assertIsNotNone(leading_zero)
        assert leading_zero is not None
        self.assertEqual(leading_zero["path"], "/study3/ACHJ-083.mp4")

        external_av_id = self.main.infer_external_qb_av_id({"name": "SNOS-071-4K.mkv"})
        self.assertEqual(external_av_id, "SNOS-071")

        repeated_root = self.main.pick_main_video_file(
            [{"name": "MIDA-528-U/MIDA-528-U.mp4", "size": 1234}],
            "MIDA-528",
            "/study3/MIDA-528-U",
        )
        self.assertIsNotNone(repeated_root)
        assert repeated_root is not None
        self.assertEqual(repeated_root["path"], "/study3/MIDA-528-U/MIDA-528-U.mp4")

    def test_subtitle_validation_marks_active_version_with_chinese_subtitle(self) -> None:
        input_path = self.root / "input-subtitle.mp4"
        subtitle_path = self.root / "input-subtitle.zh.srt"
        make_sample_video(input_path)
        subtitle_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n你好，字幕校验。\n\n",
            encoding="utf-8",
        )
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": True,
                "download_dir": str(self.root),
                "output_dir": str(self.root / "out-subtitle"),
                "target_codec": "h265",
            }
        )
        task = post.create_task(
            av_id="TEST-004",
            task_type="subscription",
            status="subtitle_processing",
            target_codec="h265",
            needs_subtitle=True,
        )
        post.update_task(task["id"], input_path=str(input_path), output_path=str(input_path))

        result = self.main.validate_and_activate_postprocess_task(
            task["id"],
            output_path=str(input_path),
            subtitle_path=str(subtitle_path),
            worker_result={"subtitle_job": {"status": "completed"}},
        )

        self.assertEqual(result["status"], "completed")
        self.assertFalse(input_path.exists())
        active = post.active_version("TEST-004")
        self.assertIsNotNone(active)
        assert active is not None
        active_path = Path(active["path"])
        self.assertTrue(active_path.exists())
        self.assertTrue(str(active_path).startswith(str(self.root / "out-subtitle")))
        self.assertTrue(active["has_chinese_subtitle"])
        self.assertTrue(active_path.with_suffix(".srt").exists())
        self.assertFalse(active_path.with_name(f"{active_path.stem}.zh.srt").exists())

    def test_subtitle_artifacts_cleanup_keeps_video_named_srt_and_vtt(self) -> None:
        product_path = self.root / "out-clean" / "ABF-359.av1.mp4"
        original_srt = product_path.with_name("ABF-359.srt")
        original_vtt = product_path.with_name("ABF-359.vtt")
        translated_srt = product_path.with_suffix(".srt")
        translated_vtt = product_path.with_suffix(".vtt")
        legacy_zh = product_path.with_name(f"{product_path.stem}.zh.srt")
        make_sample_video(product_path)
        for path, content in [
            (original_srt, "1\n00:00:00,000 --> 00:00:01,000\noriginal\n\n"),
            (original_vtt, "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\noriginal\n"),
            (translated_srt, "1\n00:00:00,000 --> 00:00:01,000\n你好，清理字幕。\n\n"),
            (translated_vtt, "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好，清理字幕。\n"),
            (legacy_zh, "1\n00:00:00,000 --> 00:00:01,000\n旧的中文字幕。\n\n"),
        ]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "auto_transcode_enabled": True,
                "auto_subtitle_enabled": True,
                "download_dir": str(self.root),
                "output_dir": str(self.root / "out-clean"),
                "target_codec": "h264",
            }
        )
        task = post.create_task(
            av_id="ABF-359",
            task_type="subscription",
            status="subtitle_processing",
            target_codec="h264",
            needs_subtitle=True,
        )
        post.update_task(task["id"], input_path=str(product_path), output_path=str(product_path))

        result = self.main.validate_and_activate_postprocess_task(
            task["id"],
            output_path=str(product_path),
            subtitle_path=str(translated_srt),
            worker_result={
                "subtitle_job": {
                    "status": "completed",
                    "original_srt": str(original_srt),
                    "original_vtt": str(original_vtt),
                    "translated_srt": str(translated_srt),
                    "translated_vtt": str(translated_vtt),
                }
            },
        )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(product_path.exists())
        self.assertTrue(translated_srt.exists())
        self.assertTrue(translated_vtt.exists())
        self.assertFalse(original_srt.exists())
        self.assertFalse(original_vtt.exists())
        self.assertFalse(legacy_zh.exists())
        trash_files = [path.name for path in (self.root / "trash").rglob("*") if path.is_file()]
        self.assertIn("ABF-359.srt", trash_files)
        self.assertIn("ABF-359.vtt", trash_files)
        self.assertIn("ABF-359.av1.zh.srt", trash_files)

    def test_subtitle_validation_failure_keeps_video_version_active(self) -> None:
        input_path = self.root / "input-bad-subtitle.mp4"
        subtitle_path = self.root / "input-bad-subtitle.srt"
        make_sample_video(input_path)
        subtitle_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nhello only\n\n",
            encoding="utf-8",
        )
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": True,
                "download_dir": str(self.root),
                "output_dir": str(self.root / "out-bad-subtitle"),
            }
        )
        task = post.create_task(
            av_id="TEST-013",
            task_type="subscription",
            status="subtitle_processing",
            needs_subtitle=True,
        )
        post.update_task(task["id"], input_path=str(input_path), output_path=str(input_path))

        result = self.main.validate_and_activate_postprocess_task(
            task["id"],
            output_path=str(input_path),
            subtitle_path=str(subtitle_path),
            worker_result={"subtitle_job": {"status": "completed"}},
        )

        updated = post.get_task(task["id"])
        active = post.active_version("TEST-013")
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertFalse(active["has_chinese_subtitle"])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["error_code"], "subtitle_validation_failed")
        self.assertTrue(Path(active["path"]).exists())
        self.assertFalse(input_path.exists())

    def test_subtitle_validation_rejects_short_coverage(self) -> None:
        subtitle_path = self.root / "short-coverage.zh.srt"
        subtitle_path.write_text(
            "1\n00:00:00,000 --> 00:00:10,000\n这是一段中文字幕，但是只覆盖开头。\n\n",
            encoding="utf-8",
        )
        original_probe_duration = self.main.probe_video_duration
        self.main.probe_video_duration = lambda path: 100.0
        try:
            result = self.main.validate_subtitle_output(str(subtitle_path), video_path=str(self.root / "video.mp4"))
        finally:
            self.main.probe_video_duration = original_probe_duration

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "字幕覆盖时长过短")
        self.assertEqual(result["video_duration"], 100.0)

    def test_subtitle_only_dispatch_records_planned_managed_output(self) -> None:
        input_path = self.root / "subtitle-only-dispatch.mp4"
        output_root = self.root / "subtitle-only-out"
        make_sample_video(input_path)
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": True,
                "download_dir": str(self.root),
                "output_dir": str(output_root),
            }
        )
        task = post.create_task(
            av_id="TEST-008",
            task_type="subscription",
            status="ready_to_run",
            needs_subtitle=True,
        )
        post.update_task(task["id"], input_path=str(input_path))
        original_submit = self.main.submit_subtitle_job_for_path

        def fake_submit(path: str) -> dict[str, object]:
            return {"id": "subtitle-job-1", "video_path": path, "status": "queued"}

        self.main.submit_subtitle_job_for_path = fake_submit
        try:
            result = self.main.dispatch_postprocess_task(post.get_task(task["id"]))
        finally:
            self.main.submit_subtitle_job_for_path = original_submit

        updated = post.get_task(task["id"])
        self.assertEqual(result["status"], "subtitle_processing")
        self.assertEqual(updated["status"], "subtitle_processing")
        self.assertTrue(str(updated["output_path"]).startswith(str(output_root)))
        self.assertEqual(updated["data"]["planned_output_path"], updated["output_path"])

    def test_no_worker_options_activate_managed_original(self) -> None:
        input_path = self.root / "no-worker-original.mp4"
        output_root = self.root / "no-worker-out"
        make_sample_video(input_path)
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": False,
                "download_dir": str(self.root),
                "output_dir": str(output_root),
            }
        )
        task = post.create_task(
            av_id="TEST-014",
            task_type="subscription",
            status="ready_to_run",
        )
        post.update_task(task["id"], input_path=str(input_path))

        result = self.main.dispatch_postprocess_task(post.get_task(task["id"]))

        updated = post.get_task(task["id"])
        active = post.active_version("TEST-014")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(updated["status"], "completed")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertTrue(str(active["path"]).startswith(str(output_root)))
        self.assertTrue(Path(active["path"]).exists())
        self.assertFalse(input_path.exists())

    def test_source_trash_failure_keeps_activated_version_completed(self) -> None:
        input_path = self.root / "outside-download.mp4"
        output_root = self.root / "cleanup-warning-out"
        make_sample_video(input_path)
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": False,
                "download_dir": str(self.root / "download-only"),
                "output_dir": str(output_root),
            }
        )
        task = post.create_task(
            av_id="TEST-017",
            task_type="subscription",
            status="ready_to_run",
        )
        post.update_task(task["id"], input_path=str(input_path))

        result = self.main.dispatch_postprocess_task(post.get_task(task["id"]))

        updated = post.get_task(task["id"])
        active = post.active_version("TEST-017")
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(updated["status"], "completed")
        self.assertIn("source_trash_failed", updated["error_code"])
        self.assertTrue(input_path.exists())
        self.assertTrue(Path(active["path"]).exists())

    def test_transcode_page_marks_completed_task_with_warning(self) -> None:
        from fastapi.testclient import TestClient

        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="TEST-WARN", task_type="subscription", status="ready_to_run")
        post.update_task(task["id"], status="completed", error_code="source_trash_failed", error_message="源文件清理失败")

        with patch.object(self.main, "current_console_user", return_value="test"):
            with TestClient(self.main.app) as client:
                response = client.get("/api/postprocess/tasks?limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        returned = next(item for item in payload["tasks"] if item["id"] == task["id"])
        self.assertEqual(returned["status"], "completed")
        self.assertIn("source_trash_failed", response.text)

    def test_queue_runs_local_task_when_remote_worker_offline(self) -> None:
        input_path = self.root / "offline-local-original.mp4"
        output_root = self.root / "offline-local-out"
        make_sample_video(input_path)
        post = self.main.get_postprocess_service()
        post.update_settings(
            {
                "auto_transcode_enabled": False,
                "auto_subtitle_enabled": True,
                "download_dir": str(self.root),
                "output_dir": str(output_root),
            }
        )
        task = post.create_task(
            av_id="TEST-015",
            task_type="subscription",
            status="ready_to_run",
            needs_subtitle=False,
        )
        post.update_task(task["id"], input_path=str(input_path))
        original_status = self.main.subtitle_backend_status
        self.main.subtitle_backend_status = lambda: {"status": "offline", "online": False}
        try:
            result = self.main.run_postprocess_queue()
        finally:
            self.main.subtitle_backend_status = original_status

        updated = post.get_task(task["id"])
        active = post.active_version("TEST-015")
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(updated["status"], "completed")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertTrue(Path(active["path"]).exists())
        self.assertFalse(input_path.exists())

    def test_single_worker_task_run_stays_waiting_when_offline(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings({"auto_transcode_enabled": True})
        task = post.create_task(
            av_id="TEST-016",
            task_type="subscription",
            status="ready_to_run",
        )
        original_status = self.main.subtitle_backend_status
        self.main.subtitle_backend_status = lambda: {"status": "offline", "online": False}
        try:
            result = self.main.api_run_postprocess_task(task["id"])
        finally:
            self.main.subtitle_backend_status = original_status

        updated = post.get_task(task["id"])
        self.assertEqual(result["status"], "waiting_worker")
        self.assertEqual(updated["status"], "waiting_worker")

    def test_postprocess_task_claim_only_succeeds_once(self) -> None:
        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="TEST-017", task_type="subscription", status="ready_to_run")

        first = post.claim_task_status(task["id"], self.main.RUNNABLE_POSTPROCESS_STATUSES, self.main.DISPATCHING_POSTPROCESS_STATUS)
        second = post.claim_task_status(task["id"], self.main.RUNNABLE_POSTPROCESS_STATUSES, self.main.DISPATCHING_POSTPROCESS_STATUS)

        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first["status"], "dispatching")
        self.assertIsNone(second)
        self.assertEqual(post.get_task(task["id"])["status"], "dispatching")

    def test_postprocess_task_list_is_read_only(self) -> None:
        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="TEST-018", task_type="subscription", status="transcoding")
        original_recover = self.main.recover_finished_worker_jobs

        def fail_if_called(_worker_status: dict[str, object]) -> list[dict[str, object]]:
            raise AssertionError("task list endpoint must not recover worker jobs")

        self.main.recover_finished_worker_jobs = fail_if_called
        try:
            payload = self.main.api_postprocess_tasks()
        finally:
            self.main.recover_finished_worker_jobs = original_recover

        self.assertTrue(any(item["id"] == task["id"] for item in payload["tasks"]))
        self.assertEqual(payload["recovered_finished"], [])
        self.assertEqual(post.get_task(task["id"])["status"], "transcoding")

    def test_task_events_are_visible_in_system_logs(self) -> None:
        post = self.main.get_postprocess_service()
        task = post.create_task(av_id="TEST-005", task_type="subscription", status="created")

        post.add_event(task["id"], "info", "unit_stage", "单元测试事件", {"av_id": "TEST-005"})

        logs = self.main.get_app_log_service().recent(20)
        mirrored = [item for item in logs if item.get("data", {}).get("task_id") == task["id"]]
        self.assertTrue(mirrored)
        self.assertEqual(mirrored[0]["source"], "postprocess")
        self.assertEqual(mirrored[0]["data"]["stage"], "unit_stage")

    def test_worker_auto_run_dispatches_ready_queue_when_enabled(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings({"worker_auto_run": True, "auto_transcode_enabled": True})
        task = post.create_task(av_id="TEST-006", task_type="subscription", status="ready_to_run")
        post.update_task(task["id"], input_path=str(self.root / "TEST-006.mp4"))
        calls: list[str] = []
        original_dispatch = self.main.dispatch_postprocess_task

        def fake_dispatch(task_payload: dict[str, object]) -> dict[str, object]:
            calls.append(str(task_payload["id"]))
            post.update_task(str(task_payload["id"]), status="sent_to_worker")
            return {"task_id": task_payload["id"], "status": "sent_to_worker"}

        self.main.dispatch_postprocess_task = fake_dispatch
        try:
            result = self.main.poll_postprocess_once()
        finally:
            self.main.dispatch_postprocess_task = original_dispatch

        self.assertEqual(calls, [task["id"]])
        self.assertEqual(result["queue_auto_run"]["status"], "dispatched")
        self.assertEqual(post.get_task(task["id"])["status"], "sent_to_worker")

    def test_waiting_worker_promotes_to_ready_without_auto_dispatch(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings({"worker_auto_run": False, "auto_transcode_enabled": True})
        task = post.create_task(av_id="TEST-007", task_type="subscription", status="waiting_worker")
        post.update_task(task["id"], input_path=str(self.root / "TEST-007.mp4"))
        calls: list[str] = []
        original_dispatch = self.main.dispatch_postprocess_task

        def fake_dispatch(task_payload: dict[str, object]) -> dict[str, object]:
            calls.append(str(task_payload["id"]))
            return {"task_id": task_payload["id"], "status": "sent_to_worker"}

        self.main.dispatch_postprocess_task = fake_dispatch
        try:
            result = self.main.poll_postprocess_once()
        finally:
            self.main.dispatch_postprocess_task = original_dispatch

        self.assertEqual(calls, [])
        self.assertEqual(result["worker_queue"]["promoted"], [task["id"]])
        self.assertIsNone(result["queue_auto_run"])
        self.assertEqual(post.get_task(task["id"])["status"], "ready_to_run")

    def test_waiting_worker_without_input_is_not_promoted(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings({"worker_auto_run": False, "auto_transcode_enabled": True})
        task = post.create_task(av_id="TEST-008", task_type="subscription", status="waiting_worker")

        result = self.main.refresh_worker_queue_readiness({"status": "ok", "online": True})

        updated = post.get_task(task["id"])
        self.assertEqual(result["promoted"], [])
        self.assertEqual(updated["status"], "waiting_input")
        self.assertIn("路径回写", updated["error_message"])

    def test_postprocess_queue_respects_max_concurrency(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings({"max_concurrency": 1, "auto_transcode_enabled": True})
        active = post.create_task(av_id="TEST-009", task_type="subscription", status="sent_to_worker")
        ready = post.create_task(av_id="TEST-010", task_type="subscription", status="ready_to_run")
        post.update_task(ready["id"], input_path=str(self.root / "TEST-010.mp4"))
        calls: list[str] = []
        original_dispatch = self.main.dispatch_postprocess_task

        def fake_dispatch(task_payload: dict[str, object]) -> dict[str, object]:
            calls.append(str(task_payload["id"]))
            post.update_task(str(task_payload["id"]), status="sent_to_worker")
            return {"task_id": task_payload["id"], "status": "sent_to_worker"}

        self.main.dispatch_postprocess_task = fake_dispatch
        try:
            result = self.main.run_postprocess_queue()
        finally:
            self.main.dispatch_postprocess_task = original_dispatch

        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "concurrency_full")
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(post.get_task(active["id"])["status"], "sent_to_worker")
        self.assertEqual(post.get_task(ready["id"])["status"], "ready_to_run")

    def test_postprocess_queue_dispatches_only_available_slots(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings({"max_concurrency": 1, "auto_transcode_enabled": True})
        first = post.create_task(av_id="TEST-011", task_type="subscription", status="ready_to_run")
        post.update_task(first["id"], input_path=str(self.root / "TEST-011.mp4"))
        time.sleep(0.01)
        second = post.create_task(av_id="TEST-012", task_type="subscription", status="ready_to_run")
        post.update_task(second["id"], input_path=str(self.root / "TEST-012.mp4"))
        calls: list[str] = []
        original_dispatch = self.main.dispatch_postprocess_task

        def fake_dispatch(task_payload: dict[str, object]) -> dict[str, object]:
            calls.append(str(task_payload["id"]))
            post.update_task(str(task_payload["id"]), status="sent_to_worker")
            return {"task_id": task_payload["id"], "status": "sent_to_worker"}

        self.main.dispatch_postprocess_task = fake_dispatch
        try:
            result = self.main.run_postprocess_queue()
        finally:
            self.main.dispatch_postprocess_task = original_dispatch

        self.assertEqual(calls, [first["id"]])
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(post.get_task(first["id"])["status"], "sent_to_worker")
        self.assertEqual(post.get_task(second["id"])["status"], "ready_to_run")

    def test_postprocess_queue_skips_missing_input_before_dispatch(self) -> None:
        post = self.main.get_postprocess_service()
        post.update_settings({"max_concurrency": 1, "auto_transcode_enabled": True})
        stale = post.create_task(av_id="TEST-MISSING", task_type="subscription", status="ready_to_run")
        time.sleep(0.01)
        ready = post.create_task(av_id="TEST-READY", task_type="subscription", status="ready_to_run")
        post.update_task(ready["id"], input_path=str(self.root / "TEST-READY.mp4"))
        calls: list[str] = []
        original_dispatch = self.main.dispatch_postprocess_task

        def fake_dispatch(task_payload: dict[str, object]) -> dict[str, object]:
            calls.append(str(task_payload["id"]))
            post.update_task(str(task_payload["id"]), status="sent_to_worker")
            return {"task_id": task_payload["id"], "status": "sent_to_worker"}

        self.main.dispatch_postprocess_task = fake_dispatch
        try:
            result = self.main.run_postprocess_queue()
        finally:
            self.main.dispatch_postprocess_task = original_dispatch

        self.assertEqual(calls, [ready["id"]])
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(post.get_task(stale["id"])["status"], "waiting_input")
        self.assertEqual(post.get_task(ready["id"])["status"], "sent_to_worker")


if __name__ == "__main__":
    unittest.main()
