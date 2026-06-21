from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from app.subtitle_service import SubtitleJob, SubtitleService, SubtitleSettings


class SubtitleServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="moviemuse-subtitle-service-"))
        self.service = SubtitleService(SubtitleSettings(data_dir=self.root, max_workers=0))

    def tearDown(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
