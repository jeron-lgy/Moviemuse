from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scanner import MovieFile, MovieGroup, ScanResult, SubtitleMatch, scan_libraries


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


def _path_to_str(path: Path | None) -> str | None:
    return str(path) if path else None


def _path_from_str(value: str | None) -> Path | None:
    return Path(value) if value else None


def _subtitle_to_dict(item: SubtitleMatch) -> dict[str, Any]:
    return {"path": str(item.path), "label": item.label, "confidence": item.confidence}


def _subtitle_from_dict(data: dict[str, Any]) -> SubtitleMatch:
    return SubtitleMatch(
        path=Path(str(data.get("path") or "")),
        label=str(data.get("label") or ""),
        confidence=str(data.get("confidence") or ""),
    )


def _file_to_dict(item: MovieFile) -> dict[str, Any]:
    return {
        "path": str(item.path),
        "title": item.title,
        "year": item.year,
        "group_key": item.group_key,
        "group_source": item.group_source,
        "size_bytes": item.size_bytes,
        "nfo_path": _path_to_str(item.nfo_path),
        "imdb_id": item.imdb_id,
        "tmdb_id": item.tmdb_id,
        "catalog_number": item.catalog_number,
        "resolution": item.resolution,
        "source_tag": item.source_tag,
        "uncensored": item.uncensored,
        "ignored": item.ignored,
        "chinese_markers": list(item.chinese_markers),
        "subtitles": [_subtitle_to_dict(subtitle) for subtitle in item.subtitles],
        "cover_path": _path_to_str(item.cover_path),
    }


def _file_from_dict(data: dict[str, Any]) -> MovieFile:
    return MovieFile(
        path=Path(str(data.get("path") or "")),
        title=str(data.get("title") or ""),
        year=str(data.get("year") or ""),
        group_key=str(data.get("group_key") or ""),
        group_source=str(data.get("group_source") or ""),
        size_bytes=int(data.get("size_bytes") or 0),
        nfo_path=_path_from_str(data.get("nfo_path")),
        imdb_id=str(data.get("imdb_id") or ""),
        tmdb_id=str(data.get("tmdb_id") or ""),
        catalog_number=str(data.get("catalog_number") or ""),
        resolution=str(data.get("resolution") or ""),
        source_tag=str(data.get("source_tag") or ""),
        uncensored=bool(data.get("uncensored")),
        ignored=bool(data.get("ignored")),
        chinese_markers=tuple(str(item) for item in data.get("chinese_markers") or []),
        subtitles=tuple(_subtitle_from_dict(item) for item in data.get("subtitles") or []),
        cover_path=_path_from_str(data.get("cover_path")),
    )


def _group_to_dict(item: MovieGroup) -> dict[str, Any]:
    return {
        "key": item.key,
        "title": item.title,
        "year": item.year,
        "source": item.source,
        "cover_path": _path_to_str(item.cover_path),
        "files": [_file_to_dict(file) for file in item.files],
    }


def _group_from_dict(data: dict[str, Any]) -> MovieGroup:
    return MovieGroup(
        key=str(data.get("key") or ""),
        title=str(data.get("title") or ""),
        year=str(data.get("year") or ""),
        source=str(data.get("source") or ""),
        cover_path=_path_from_str(data.get("cover_path")),
        files=tuple(_file_from_dict(item) for item in data.get("files") or []),
    )


def _result_to_dict(result: ScanResult) -> dict[str, Any]:
    return {
        "groups": [_group_to_dict(group) for group in result.groups],
        "total_files": result.total_files,
        "duplicate_files": result.duplicate_files,
        "scanned_dirs": [str(path) for path in result.scanned_dirs],
        "files": [_file_to_dict(file) for file in result.files],
        "missing_dirs": [str(path) for path in result.missing_dirs],
    }


def _result_from_dict(data: dict[str, Any]) -> ScanResult:
    return ScanResult(
        groups=tuple(_group_from_dict(item) for item in data.get("groups") or []),
        total_files=int(data.get("total_files") or 0),
        duplicate_files=int(data.get("duplicate_files") or 0),
        scanned_dirs=tuple(Path(str(path)) for path in data.get("scanned_dirs") or []),
        files=tuple(_file_from_dict(item) for item in data.get("files") or []),
        missing_dirs=tuple(Path(str(path)) for path in data.get("missing_dirs") or []),
    )


def _snapshot_to_dict(snapshot: ScanSnapshot) -> dict[str, Any]:
    return {
        "status": snapshot.status,
        "started_at": snapshot.started_at,
        "finished_at": snapshot.finished_at,
        "error": snapshot.error,
        "result": _result_to_dict(snapshot.result) if snapshot.result else None,
        "scanned_dirs": [str(path) for path in snapshot.scanned_dirs],
        "processed_files": snapshot.processed_files,
        "total_files": snapshot.total_files,
        "current_path": snapshot.current_path,
    }


def _snapshot_from_dict(data: dict[str, Any]) -> ScanSnapshot:
    result_data = data.get("result")
    return ScanSnapshot(
        status=str(data.get("status") or "idle"),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        error=data.get("error"),
        result=_result_from_dict(result_data) if isinstance(result_data, dict) else None,
        scanned_dirs=tuple(Path(str(path)) for path in data.get("scanned_dirs") or []),
        processed_files=int(data.get("processed_files") or 0),
        total_files=int(data.get("total_files") or 0),
        current_path=data.get("current_path"),
    )


class ScanCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = ScanSnapshot()
        self._thread: threading.Thread | None = None
        self._db_path: Path | None = None

    def configure(self, data_dir: Path) -> None:
        db_path = data_dir / "scan_cache.sqlite3"
        with self._lock:
            if self._db_path == db_path:
                return
            self._db_path = db_path
        data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db(db_path)
        loaded = self._load_snapshot(db_path)
        if loaded:
            with self._lock:
                if self._snapshot.status != "running" and self._snapshot.result is None:
                    self._snapshot = loaded

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
            snapshot = self._snapshot
            self._thread = threading.Thread(target=self._run, args=(list(media_dirs), excluded_dirs), daemon=True)
            self._thread.start()
        self._save_snapshot(snapshot)
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
                snapshot = self._snapshot
            self._save_snapshot(snapshot)
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
            snapshot = self._snapshot
        self._save_snapshot(snapshot)

    def _connect(self, db_path: Path | None = None) -> sqlite3.Connection | None:
        path = db_path or self._db_path
        if not path:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self, db_path: Path) -> None:
        conn = self._connect(db_path)
        if not conn:
            return
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_snapshots (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
        conn.close()

    def _load_snapshot(self, db_path: Path) -> ScanSnapshot | None:
        conn = self._connect(db_path)
        if not conn:
            return None
        try:
            row = conn.execute("SELECT payload FROM scan_snapshots WHERE id = 'latest'").fetchone()
        finally:
            conn.close()
        if not row:
            return None
        try:
            return _snapshot_from_dict(json.loads(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save_snapshot(self, snapshot: ScanSnapshot) -> None:
        payload = json.dumps(_snapshot_to_dict(snapshot), ensure_ascii=False)
        conn = self._connect()
        if not conn:
            return
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO scan_snapshots (id, status, started_at, finished_at, error, payload, updated_at)
                    VALUES ('latest', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status = excluded.status,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        error = excluded.error,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (
                        snapshot.status,
                        snapshot.started_at,
                        snapshot.finished_at,
                        snapshot.error,
                        payload,
                        time.time(),
                    ),
                )
        finally:
            conn.close()


scan_cache = ScanCache()
