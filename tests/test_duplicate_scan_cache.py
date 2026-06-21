from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.scan_state import SCAN_STALE_SECONDS, ScanCache, ScanSnapshot
from app.scanner import ScanResult


class DuplicateScanCacheTest(unittest.TestCase):
    def wait_for_scan(self, cache: ScanCache, timeout: float = 5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = cache.snapshot()
            if snapshot.status != "running":
                thread = cache._thread
                if thread:
                    thread.join(1)
                return snapshot
            time.sleep(0.02)
        self.fail("scan did not finish")

    def test_incremental_scan_refreshes_when_sidecar_subtitle_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            data = root / "data"
            media.mkdir()
            first = media / "ABF-336.mp4"
            second = media / "ABF-336-1080p.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            cache = ScanCache()
            cache.configure(data)

            self.assertTrue(cache.start([media], mode="full"))
            full_snapshot = self.wait_for_scan(cache)
            self.assertEqual(full_snapshot.status, "completed")
            self.assertEqual(full_snapshot.changed_files, 2)

            (media / "ABF-336.zh.srt").write_text("subtitle", encoding="utf-8")

            self.assertTrue(cache.start([media], mode="incremental"))
            incremental_snapshot = self.wait_for_scan(cache)
            self.assertEqual(incremental_snapshot.status, "completed")
            self.assertEqual(incremental_snapshot.changed_files, 1)
            self.assertEqual(incremental_snapshot.reused_files, 1)
            self.assertIn(str(first), incremental_snapshot.changed_paths)

            files = {file.path.name: file for file in incremental_snapshot.result.files}
            self.assertEqual(files["ABF-336.mp4"].subtitle_kind, "chinese")
            self.assertEqual(files["ABF-336-1080p.mp4"].subtitle_kind, "none")

    def test_start_rejects_second_scan_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "media"
            data = Path(tmp) / "data"
            media.mkdir()
            started = threading.Event()
            release = threading.Event()

            cache = ScanCache()
            cache.configure(data)

            def slow_scan(media_dirs, excluded_dirs, progress, force_refresh, should_continue=None):
                started.set()
                release.wait(2)
                return ScanResult(tuple(), 0, 0, tuple(media_dirs), tuple(), tuple()), 0, 0, 0, ()

            cache._scan_with_cache = slow_scan  # type: ignore[method-assign]
            self.assertTrue(cache.start([media], mode="incremental"))
            self.assertTrue(started.wait(2))
            self.assertFalse(cache.start([media], mode="incremental"))
            release.set()
            snapshot = self.wait_for_scan(cache)
            self.assertEqual(snapshot.status, "completed")

    def test_configure_marks_persisted_running_scan_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            media = Path(tmp) / "media"
            media.mkdir()

            cache = ScanCache()
            cache.configure(data)
            cache._snapshot = ScanSnapshot(
                status="running",
                mode="incremental",
                started_at=time.time() - 30,
                result=ScanResult(tuple(), 0, 0, (media,), tuple(), tuple()),
                scanned_dirs=(media,),
                processed_files=3,
                total_files=10,
                last_progress_at=time.time() - 20,
            )
            cache._save_snapshot(cache._snapshot)

            restored = ScanCache()
            restored.configure(data)
            snapshot = restored.snapshot()
            self.assertEqual(snapshot.status, "interrupted")
            self.assertEqual(snapshot.processed_files, 3)
            self.assertEqual(snapshot.total_files, 10)
            self.assertIn("异常中断", snapshot.error)

    def test_running_scan_becomes_stale_after_heartbeat_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "media"
            data = Path(tmp) / "data"
            media.mkdir()
            started = threading.Event()
            release = threading.Event()

            cache = ScanCache()
            cache.configure(data)

            def slow_scan(media_dirs, excluded_dirs, progress, force_refresh, should_continue=None):
                started.set()
                release.wait(2)
                return ScanResult(tuple(), 0, 0, tuple(media_dirs), tuple(), tuple()), 0, 0, 0, ()

            cache._scan_with_cache = slow_scan  # type: ignore[method-assign]
            self.assertTrue(cache.start([media], mode="incremental"))
            self.assertTrue(started.wait(2))
            with cache._lock:
                cache._snapshot.last_progress_at = time.time() - SCAN_STALE_SECONDS - 1
            self.assertTrue(cache.scan_stale())
            release.set()
            snapshot = self.wait_for_scan(cache)
            self.assertEqual(snapshot.status, "completed")

    def test_reset_running_scan_prevents_old_thread_from_overwriting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "media"
            data = Path(tmp) / "data"
            media.mkdir()
            started = threading.Event()
            release = threading.Event()

            cache = ScanCache()
            cache.configure(data)

            def slow_scan(media_dirs, excluded_dirs, progress, force_refresh, should_continue=None):
                started.set()
                release.wait(2)
                return ScanResult(tuple(), 0, 0, tuple(media_dirs), tuple(), tuple()), 0, 0, 0, ()

            cache._scan_with_cache = slow_scan  # type: ignore[method-assign]
            self.assertTrue(cache.start([media], mode="incremental"))
            self.assertTrue(started.wait(2))
            self.assertTrue(cache.reset_running())
            self.assertEqual(cache.snapshot().status, "interrupted")

            release.set()
            if cache._thread:
                cache._thread.join(1)
            time.sleep(0.1)
            snapshot = cache.snapshot()
            self.assertEqual(snapshot.status, "interrupted")
            self.assertIn("重置", snapshot.error)

    def test_cancel_running_scan_prevents_old_thread_from_overwriting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "media"
            data = Path(tmp) / "data"
            media.mkdir()
            started = threading.Event()
            release = threading.Event()

            cache = ScanCache()
            cache.configure(data)

            def slow_scan(media_dirs, excluded_dirs, progress, force_refresh, should_continue=None):
                started.set()
                release.wait(2)
                return ScanResult(tuple(), 0, 0, tuple(media_dirs), tuple(), tuple()), 0, 0, 0, ()

            cache._scan_with_cache = slow_scan  # type: ignore[method-assign]
            self.assertTrue(cache.start([media], mode="incremental"))
            self.assertTrue(started.wait(2))
            self.assertTrue(cache.cancel_running())
            self.assertEqual(cache.snapshot().status, "cancelled")

            release.set()
            if cache._thread:
                cache._thread.join(1)
            time.sleep(0.1)
            snapshot = cache.snapshot()
            self.assertEqual(snapshot.status, "cancelled")
            self.assertIn("终止", snapshot.error)


if __name__ == "__main__":
    unittest.main()
