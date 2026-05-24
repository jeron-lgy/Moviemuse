from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .scanner import ScanResult, scan_libraries


@dataclass
class ScanSnapshot:
    status: str = "idle"
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: ScanResult | None = None
    scanned_dirs: tuple[Path, ...] = ()
    processed_files: int = 0
    total_files: int = 0
    current_path: str | None = None

    @property
    def progress(self) -> float:
        if self.total_files <= 0:
            return 0.0
        return min(1.0, self.processed_files / self.total_files)


class ScanCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = ScanSnapshot()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> ScanSnapshot:
        with self._lock:
            return self._snapshot

    def start(self, media_dirs: list[Path], force: bool = False, excluded_dirs: list[Path] | None = None) -> bool:
        with self._lock:
            if self._snapshot.status == "running" and not force:
                return False
            excluded_dirs = list(excluded_dirs or [])
            self._snapshot = ScanSnapshot(
                status="running",
                started_at=time.time(),
                result=self._snapshot.result,
                scanned_dirs=tuple(media_dirs),
            )
            self._thread = threading.Thread(target=self._run, args=(list(media_dirs), excluded_dirs), daemon=True)
            self._thread.start()
            return True

    def _run(self, media_dirs: list[Path], excluded_dirs: list[Path]) -> None:
        def progress(processed: int, total: int, current_path: Path | None) -> None:
            with self._lock:
                self._snapshot.processed_files = processed
                self._snapshot.total_files = total
                self._snapshot.current_path = str(current_path) if current_path else None

        try:
            result = scan_libraries(media_dirs, excluded_dirs=excluded_dirs, progress=progress)
        except Exception as exc:
            with self._lock:
                self._snapshot.status = "failed"
                self._snapshot.finished_at = time.time()
                self._snapshot.error = str(exc)
                self._snapshot.scanned_dirs = tuple(media_dirs)
            return
        with self._lock:
            self._snapshot.status = "completed"
            self._snapshot.finished_at = time.time()
            self._snapshot.error = None
            self._snapshot.result = result
            self._snapshot.scanned_dirs = tuple(media_dirs)
            self._snapshot.processed_files = result.total_files
            self._snapshot.total_files = result.total_files
            self._snapshot.current_path = None


scan_cache = ScanCache()
