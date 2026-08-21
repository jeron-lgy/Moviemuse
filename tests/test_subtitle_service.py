from __future__ import annotations

import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from app.subtitle_service import (
    SubtitleJob,
    SubtitleSegment,
    SubtitleService,
    SubtitleSettings,
    managed_whisper_model_source,
    normalize_subtitle_timing,
    read_srt,
    subtitle_output_paths,
    subtitle_word_bounds,
    write_srt,
)


class SubtitleServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="moviemuse-subtitle-service-"))
        self.service = SubtitleService(
            SubtitleSettings(data_dir=self.root, allowed_media_dirs=[self.root], max_workers=0)
        )

    def tearDown(self) -> None:
        self.service.close(timeout=2)
        shutil.rmtree(self.root, ignore_errors=True)

    def add_job(self, job_id: str, status: str) -> SubtitleJob:
        job = SubtitleJob(
            id=job_id,
            video_path=str(self.root / f"{job_id}.mp4"),
            output_dir=str(self.root),
            source_language=None,
            target_language="zh",
            model="large-v3",
            translate=True,
            status=status,
        )
        with self.service.lock:
            self.service.jobs[job.id] = job
            self.service._save_jobs_locked()
        return job

    def test_complete_worker_managed_model_directory_is_preferred(self) -> None:
        model_root = self.root / "whisper-models"
        managed = model_root / "large-v3"
        managed.mkdir(parents=True)
        for name in ("config.json", "model.bin", "tokenizer.json"):
            (managed / name).write_bytes(b"model")

        self.assertEqual(managed_whisper_model_source("large-v3", model_root), str(managed))
        self.assertEqual(managed_whisper_model_source("medium", model_root), "medium")

    def test_delete_running_job_prevents_late_worker_update(self) -> None:
        self.add_job("running-job", "running")

        deleted = self.service.delete_job("running-job")

        self.assertEqual(deleted.status, "running")
        self.assertIsNone(self.service.get_job("running-job"))
        self.service._update("running-job", status="completed", progress=1.0)
        self.assertIsNone(self.service.get_job("running-job"))

    def test_cancel_running_job_freezes_cancelled_status(self) -> None:
        self.add_job("cancel-job", "translating")

        cancelled = self.service.cancel_job("cancel-job")

        self.assertEqual(cancelled.status, "cancelled")
        self.service._update("cancel-job", status="completed", progress=1.0)
        current = self.service.get_job("cancel-job")
        self.assertIsNotNone(current)
        self.assertEqual(current.status, "cancelled")
        self.assertEqual(current.progress, 1.0)

    def test_close_stops_workers_and_rejects_new_jobs(self) -> None:
        self.assertTrue(self.service.close(timeout=2))
        with self.assertRaisesRegex(RuntimeError, "正在关闭"):
            self.service.create_job(str(self.root / "missing.mp4"))

    def test_prepare_job_rejects_input_and_output_outside_allowed_media_dirs(self) -> None:
        video = self.root / "allowed.mp4"
        video.write_bytes(b"video" * 300)
        outside = Path(tempfile.mkdtemp(prefix="moviemuse-subtitle-outside-"))
        outside_video = outside / "outside.mp4"
        outside_video.write_bytes(b"video" * 300)
        try:
            with self.assertRaisesRegex(ValueError, "视频路径不在允许的媒体目录内"):
                self.service._prepare_job(str(outside_video))
            with self.assertRaisesRegex(ValueError, "字幕输出目录不在允许的媒体目录内"):
                self.service._prepare_job(str(video), output_dir=str(outside))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_save_upload_stream_enforces_limit_and_removes_partial_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "超过"):
            self.service.save_upload_stream("large.mp4", BytesIO(b"x" * 2048), 1024)

        self.assertEqual(list(self.service.upload_dir.glob("*.mp4")), [])

    def test_subtitle_output_paths_keep_language_and_variant_suffixes_distinct(self) -> None:
        paths = subtitle_output_paths(
            self.root,
            Path("Movie.Name.mp4"),
            source_language="ja",
            target_language="zh",
        )

        self.assertEqual(paths["original_srt"].name, "Movie.Name.ja.srt")
        self.assertEqual(paths["translated_srt"].name, "Movie.Name.zh-CN.srt")
        self.assertEqual(paths["bilingual_srt"].name, "Movie.Name.bilingual.zh-CN.srt")
        self.assertEqual(len(set(paths.values())), len(paths))

    def test_word_bounds_use_first_and_last_valid_word_timestamps(self) -> None:
        raw_segment = SimpleNamespace(
            words=[
                SimpleNamespace(start=10.2, end=10.8),
                SimpleNamespace(start=10.9, end=11.7),
            ]
        )

        self.assertEqual(subtitle_word_bounds(raw_segment), (10.2, 11.7))

    def test_normalize_subtitle_timing_does_not_fill_long_silence(self) -> None:
        normalized = normalize_subtitle_timing(
            [
                SubtitleSegment(start=10, end=120, text="短句"),
                SubtitleSegment(start=120, end=123, text="下一句"),
            ]
        )

        self.assertEqual(len(normalized), 2)
        self.assertLessEqual(normalized[0].end, 12.0)
        self.assertGreater(normalized[1].start - normalized[0].end, 100)

    def test_normalize_word_timing_adds_small_tail_without_overlapping_next_cue(self) -> None:
        normalized = normalize_subtitle_timing(
            [
                SubtitleSegment(start=2.0, end=3.0, text="第一句", timing_from_words=True),
                SubtitleSegment(start=3.1, end=4.0, text="第二句", timing_from_words=True),
            ]
        )

        self.assertAlmostEqual(normalized[0].end, 3.02, places=2)
        self.assertLess(normalized[0].end, normalized[1].start)

    def test_run_job_uses_word_timestamps_and_explicit_vad_settings(self) -> None:
        video = self.root / "Movie.Name.mp4"
        video.write_bytes(b"video" * 300)
        job = SubtitleJob(
            id="transcribe-job",
            video_path=str(video),
            output_dir=str(self.root),
            source_language=None,
            target_language="zh",
            model="large-v3",
            translate=False,
        )
        with self.service.lock:
            self.service.jobs[job.id] = job

        captured: dict[str, object] = {}

        class FakeModel:
            def transcribe(self, path: str, **kwargs: object):
                captured.update(kwargs)
                segment = SimpleNamespace(
                    start=0.0,
                    end=120.0,
                    text=" テストです ",
                    words=[
                        SimpleNamespace(start=10.0, end=10.6),
                        SimpleNamespace(start=10.7, end=11.4),
                    ],
                )
                return iter([segment]), SimpleNamespace(duration=120.0, language="ja")

        original_get_model = self.service._get_model
        self.service._get_model = lambda _name: FakeModel()
        try:
            self.service._run_job(job.id)
        finally:
            self.service._get_model = original_get_model

        current = self.service.get_job(job.id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status, "completed")
        self.assertEqual(Path(current.original_srt or "").name, "Movie.Name.ja.srt")
        generated = read_srt(Path(current.original_srt or ""))
        self.assertEqual(len(generated), 1)
        self.assertAlmostEqual(generated[0].start, 10.0, places=2)
        self.assertAlmostEqual(generated[0].end, 11.6, places=2)
        self.assertTrue(captured["word_timestamps"])
        self.assertFalse(captured["condition_on_previous_text"])
        self.assertEqual(captured["hallucination_silence_threshold"], 2.0)
        self.assertEqual(
            captured["vad_parameters"],
            {"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        )

    def test_translation_outputs_do_not_overwrite_original_or_each_other(self) -> None:
        video = self.root / "Movie.Name.mp4"
        video.write_bytes(b"video")
        original_srt = self.root / "Movie.Name.ja.srt"
        write_srt(
            original_srt,
            [SubtitleSegment(start=1.0, end=2.0, text="原文です")],
        )
        job = SubtitleJob(
            id="translation-job",
            video_path=str(video),
            output_dir=str(self.root),
            source_language="ja",
            target_language="zh",
            model="large-v3",
            translate=True,
            translate_backend="deepseek",
            status="translating",
            original_srt=str(original_srt),
            detected_language="ja",
        )
        with self.service.lock:
            self.service.jobs[job.id] = job

        progress_updates: list[tuple[int, int]] = []

        def fake_translate(segments, _source_language, _target_language, _backend, progress_callback=None):
            for segment in segments:
                segment.translated_text = "中文译文"
            if progress_callback:
                progress_callback(len(segments), len(segments))
                progress_updates.append((len(segments), len(segments)))

        original_translate = self.service._translate_segments
        self.service._translate_segments = fake_translate
        try:
            self.service._run_translation_job(job.id)
        finally:
            self.service._translate_segments = original_translate

        current = self.service.get_job(job.id)
        self.assertIsNotNone(current)
        assert current is not None
        paths = {
            Path(current.original_srt or ""),
            Path(current.translated_srt or ""),
            Path(current.bilingual_srt or ""),
        }
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual(Path(current.translated_srt or "").name, "Movie.Name.zh-CN.srt")
        self.assertEqual(Path(current.bilingual_srt or "").name, "Movie.Name.bilingual.zh-CN.srt")
        self.assertEqual(read_srt(original_srt)[0].text, "原文です")
        self.assertEqual(read_srt(Path(current.translated_srt or ""))[0].text, "中文译文")
        self.assertEqual(progress_updates, [(1, 1)])
        self.assertEqual(
            read_srt(Path(current.bilingual_srt or ""))[0].text,
            "中文译文\n原文です",
        )


if __name__ == "__main__":
    unittest.main()
