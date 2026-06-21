from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .scanner import (
    MovieFile,
    MovieGroup,
    ScanResult,
    SubtitleMatch,
    analyze_video,
    build_scan_result,
    find_cover,
    find_nfo,
    find_subtitles,
    iter_video_files,
    normalized_roots,
    scan_libraries,
)

SCAN_STALE_SECONDS = 10 * 60
SCAN_INTERRUPTED_ERROR = "上次扫描异常中断，已保留旧结果，可重新扫描。"
SCAN_RESET_ERROR = "扫描状态已重置，旧扫描结果会被忽略。"
SCAN_CANCELLED_ERROR = "扫描已终止，旧扫描结果会被忽略。"


class ScanCancelled(RuntimeError):
    pass


@dataclass
class ScanSnapshot:
    status: str = "idle"
    mode: str = "incremental"
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: ScanResult | None = None
    scanned_dirs: tuple[Path, ...] = ()
    processed_files: int = 0
    total_files: int = 0
    reused_files: int = 0
    changed_files: int = 0
    missing_files: int = 0
    changed_paths: tuple[str, ...] = ()
    current_path: str | None = None
    last_progress_at: float | None = None

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
        "mode": snapshot.mode,
        "started_at": snapshot.started_at,
        "finished_at": snapshot.finished_at,
        "error": snapshot.error,
        "result": _result_to_dict(snapshot.result) if snapshot.result else None,
        "scanned_dirs": [str(path) for path in snapshot.scanned_dirs],
        "processed_files": snapshot.processed_files,
        "total_files": snapshot.total_files,
        "reused_files": snapshot.reused_files,
        "changed_files": snapshot.changed_files,
        "missing_files": snapshot.missing_files,
        "changed_paths": list(snapshot.changed_paths),
        "current_path": snapshot.current_path,
        "last_progress_at": snapshot.last_progress_at,
    }


def _snapshot_from_dict(data: dict[str, Any]) -> ScanSnapshot:
    result_data = data.get("result")
    return ScanSnapshot(
        status=str(data.get("status") or "idle"),
        mode=str(data.get("mode") or "incremental"),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        error=data.get("error"),
        result=_result_from_dict(result_data) if isinstance(result_data, dict) else None,
        scanned_dirs=tuple(Path(str(path)) for path in data.get("scanned_dirs") or []),
        processed_files=int(data.get("processed_files") or 0),
        total_files=int(data.get("total_files") or 0),
        reused_files=int(data.get("reused_files") or 0),
        changed_files=int(data.get("changed_files") or 0),
        missing_files=int(data.get("missing_files") or 0),
        changed_paths=tuple(str(path) for path in data.get("changed_paths") or []),
        current_path=data.get("current_path"),
        last_progress_at=data.get("last_progress_at"),
    )


class ScanCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = ScanSnapshot()
        self._thread: threading.Thread | None = None
        self._db_path: Path | None = None
        self._run_id = 0

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
            if loaded.status == "running":
                loaded = self._interrupted_snapshot(loaded, SCAN_INTERRUPTED_ERROR)
                self._save_snapshot(loaded)
            with self._lock:
                if self._snapshot.status != "running" and self._snapshot.result is None:
                    self._snapshot = loaded

    def snapshot(self) -> ScanSnapshot:
        self._mark_dead_scan_interrupted()
        with self._lock:
            return self._snapshot

    def scan_alive(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive() and self._snapshot.status == "running")

    def scan_stale(self, now: float | None = None) -> bool:
        now = now or time.time()
        with self._lock:
            if self._snapshot.status != "running":
                return False
            last_progress_at = self._snapshot.last_progress_at or self._snapshot.started_at
            if not last_progress_at:
                return False
            return now - float(last_progress_at) >= SCAN_STALE_SECONDS

    def reset_running(self, message: str = SCAN_RESET_ERROR) -> bool:
        with self._lock:
            if self._snapshot.status != "running":
                return False
            self._run_id += 1
            self._snapshot = self._interrupted_snapshot(self._snapshot, message)
            snapshot = self._snapshot
        self._save_snapshot(snapshot)
        return True

    def cancel_running(self, message: str = SCAN_CANCELLED_ERROR) -> bool:
        with self._lock:
            if self._snapshot.status != "running":
                return False
            self._run_id += 1
            self._snapshot = self._terminal_snapshot(self._snapshot, "cancelled", message)
            snapshot = self._snapshot
        self._save_snapshot(snapshot)
        return True

    def start(
        self,
        media_dirs: list[Path],
        force: bool = False,
        mode: str = "incremental",
        excluded_dirs: list[Path] | None = None,
        completion_callback: Callable[[ScanSnapshot], None] | None = None,
    ) -> bool:
        normalized_mode = "full" if mode == "full" else "incremental"
        self._mark_dead_scan_interrupted()
        with self._lock:
            if self._snapshot.status == "running" and not force:
                return False
            self._run_id += 1
            run_id = self._run_id
            excluded_dirs = list(excluded_dirs or [])
            started_at = time.time()
            self._snapshot = ScanSnapshot(
                status="running",
                mode=normalized_mode,
                started_at=started_at,
                result=self._snapshot.result,
                scanned_dirs=tuple(media_dirs),
                last_progress_at=started_at,
            )
            snapshot = self._snapshot
            self._thread = threading.Thread(
                target=self._run,
                args=(run_id, list(media_dirs), excluded_dirs, normalized_mode, completion_callback),
                daemon=True,
            )
            self._thread.start()
        self._save_snapshot(snapshot)
        return True

    def _run(
        self,
        run_id: int,
        media_dirs: list[Path],
        excluded_dirs: list[Path],
        mode: str,
        completion_callback: Callable[[ScanSnapshot], None] | None = None,
    ) -> None:
        def progress(processed: int, total: int, current_path: Path | None) -> None:
            with self._lock:
                if run_id != self._run_id:
                    return
                now = time.time()
                self._snapshot.processed_files = processed
                self._snapshot.total_files = total
                self._snapshot.current_path = str(current_path) if current_path else None
                self._snapshot.last_progress_at = now

        try:
            if mode == "full":
                result, reused_files, changed_files, missing_files, changed_paths = self._scan_with_cache(
                    media_dirs,
                    excluded_dirs=excluded_dirs,
                    progress=progress,
                    force_refresh=True,
                    should_continue=lambda: self._run_is_current(run_id),
                )
            else:
                result, reused_files, changed_files, missing_files, changed_paths = self._scan_with_cache(
                    media_dirs,
                    excluded_dirs=excluded_dirs,
                    progress=progress,
                    force_refresh=False,
                    should_continue=lambda: self._run_is_current(run_id),
                )
        except ScanCancelled:
            return
        except Exception as exc:
            with self._lock:
                if run_id != self._run_id:
                    return
                self._snapshot.status = "failed"
                self._snapshot.finished_at = time.time()
                self._snapshot.error = str(exc)
                self._snapshot.scanned_dirs = tuple(media_dirs)
                self._snapshot.last_progress_at = time.time()
                snapshot = self._snapshot
            self._save_snapshot(snapshot)
            if completion_callback:
                completion_callback(snapshot)
            return
        with self._lock:
            if run_id != self._run_id:
                return
            self._snapshot.status = "completed"
            self._snapshot.finished_at = time.time()
            self._snapshot.error = None
            self._snapshot.result = result
            self._snapshot.scanned_dirs = tuple(media_dirs)
            self._snapshot.processed_files = result.total_files
            self._snapshot.total_files = result.total_files
            self._snapshot.reused_files = reused_files
            self._snapshot.changed_files = changed_files
            self._snapshot.missing_files = missing_files
            self._snapshot.changed_paths = tuple(changed_paths) if mode != "full" else ()
            self._snapshot.current_path = None
            self._snapshot.last_progress_at = time.time()
            snapshot = self._snapshot
        self._save_snapshot(snapshot)
        if completion_callback:
            completion_callback(snapshot)

    def _scan_with_cache(
        self,
        media_dirs: list[Path],
        excluded_dirs: list[Path],
        progress: Callable[[int, int, Path | None], None] | None,
        force_refresh: bool,
        should_continue: Callable[[], bool] | None = None,
    ) -> tuple[ScanResult, int, int, int, tuple[str, ...]]:
        def ensure_active() -> None:
            if should_continue and not should_continue():
                raise ScanCancelled()

        if not self._db_path:
            return scan_libraries(media_dirs, excluded_dirs=excluded_dirs, progress=progress), 0, 0, 0, ()

        files: list[MovieFile] = []
        missing_dirs: list[Path] = []
        videos: list[Path] = []
        excluded_roots = normalized_roots(excluded_dirs)
        for media_dir in media_dirs:
            ensure_active()
            if not media_dir.exists():
                missing_dirs.append(media_dir)
                continue
            for video in iter_video_files(media_dir, excluded_roots):
                ensure_active()
                videos.append(video)

        total = len(videos)
        if progress:
            progress(0, total, None)

        cached = self._cached_files_for_roots(media_dirs)
        current_paths: set[str] = set()
        reused_files = 0
        changed_files = 0
        changed_paths: list[str] = []

        for index, video in enumerate(videos, start=1):
            ensure_active()
            path_key = str(video)
            current_paths.add(path_key)
            try:
                stat = video.stat()
                size_bytes = int(stat.st_size)
                mtime_ns = int(stat.st_mtime_ns)
                sidecar_signature = self._sidecar_signature(video)
            except OSError:
                if progress:
                    progress(index, total, video)
                continue

            row = cached.get(path_key)
            movie: MovieFile | None = None
            if (
                row
                and not force_refresh
                and row["size_bytes"] == size_bytes
                and row["mtime_ns"] == mtime_ns
                and row["sidecar_signature"] == sidecar_signature
            ):
                try:
                    movie = _file_from_dict(json.loads(str(row["payload"] or "{}")))
                    reused_files += 1
                except (TypeError, ValueError, json.JSONDecodeError):
                    movie = None

            if movie is None:
                ensure_active()
                movie = analyze_video(video)
                changed_files += 1
                changed_paths.append(path_key)

            files.append(movie)
            ensure_active()
            self._upsert_cached_file(movie, self._scan_root_for(video, media_dirs), size_bytes, mtime_ns, sidecar_signature)
            if progress:
                progress(index, total, video)

        ensure_active()
        missing_files = self._mark_missing(media_dirs, current_paths)
        result = build_scan_result(files, media_dirs, missing_dirs)
        return result, reused_files, changed_files, missing_files, tuple(changed_paths)

    def _scan_root_for(self, path: Path, media_dirs: list[Path]) -> str:
        try:
            resolved_path = path.resolve()
        except OSError:
            resolved_path = path.absolute()
        for media_dir in media_dirs:
            try:
                resolved_root = media_dir.resolve()
                resolved_path.relative_to(resolved_root)
                return str(media_dir)
            except (OSError, ValueError):
                continue
        return str(path.parent)

    def _sidecar_signature(self, video: Path) -> str:
        sidecars = [path for path in (find_nfo(video), find_cover(video)) if path]
        sidecars.extend(subtitle.path for subtitle in find_subtitles(video))
        parts: list[str] = []
        for sidecar in sorted(set(sidecars), key=lambda item: str(item).lower()):
            try:
                stat = sidecar.stat()
            except OSError:
                continue
            parts.append(f"{sidecar.name}\0{stat.st_size}\0{stat.st_mtime_ns}")
        return "\n".join(parts)

    def _interrupted_snapshot(self, snapshot: ScanSnapshot, message: str) -> ScanSnapshot:
        return self._terminal_snapshot(snapshot, "interrupted", message)

    def _terminal_snapshot(self, snapshot: ScanSnapshot, status: str, message: str) -> ScanSnapshot:
        return ScanSnapshot(
            status=status,
            mode=snapshot.mode,
            started_at=snapshot.started_at,
            finished_at=time.time(),
            error=message,
            result=snapshot.result,
            scanned_dirs=snapshot.scanned_dirs,
            processed_files=snapshot.processed_files,
            total_files=snapshot.total_files,
            reused_files=snapshot.reused_files,
            changed_files=snapshot.changed_files,
            missing_files=snapshot.missing_files,
            changed_paths=snapshot.changed_paths,
            current_path=snapshot.current_path,
            last_progress_at=snapshot.last_progress_at,
        )

    def _run_is_current(self, run_id: int) -> bool:
        with self._lock:
            return run_id == self._run_id and self._snapshot.status == "running"

    def _mark_dead_scan_interrupted(self) -> bool:
        with self._lock:
            if self._snapshot.status != "running":
                return False
            if self._thread and self._thread.is_alive():
                return False
            self._run_id += 1
            self._snapshot = self._interrupted_snapshot(self._snapshot, SCAN_INTERRUPTED_ERROR)
            snapshot = self._snapshot
        self._save_snapshot(snapshot)
        return True

    def _cached_files_for_roots(self, media_dirs: list[Path]) -> dict[str, dict[str, Any]]:
        conn = self._connect()
        if not conn:
            return {}
        roots = [str(path) for path in media_dirs]
        if not roots:
            return {}
        placeholders = ",".join("?" for _ in roots)
        try:
            rows = conn.execute(
                f"""
                SELECT path, size_bytes, mtime_ns, sidecar_signature, payload
                FROM duplicate_scan_files
                WHERE scan_root IN ({placeholders}) AND missing = 0
                """,
                roots,
            ).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        return {
            str(row[0]): {
                "size_bytes": int(row[1] or 0),
                "mtime_ns": int(row[2] or 0),
                "sidecar_signature": str(row[3] or ""),
                "payload": str(row[4] or "{}"),
            }
            for row in rows
        }

    def _upsert_cached_file(
        self,
        movie: MovieFile,
        scan_root: str,
        size_bytes: int,
        mtime_ns: int,
        sidecar_signature: str,
    ) -> None:
        conn = self._connect()
        if not conn:
            return
        payload = json.dumps(_file_to_dict(movie), ensure_ascii=False)
        now = time.time()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO duplicate_scan_files (
                        path, scan_root, size_bytes, mtime_ns, sidecar_signature, suffix, name, group_key,
                        resolution, subtitle_kind, payload, missing, last_seen_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        scan_root = excluded.scan_root,
                        size_bytes = excluded.size_bytes,
                        mtime_ns = excluded.mtime_ns,
                        sidecar_signature = excluded.sidecar_signature,
                        suffix = excluded.suffix,
                        name = excluded.name,
                        group_key = excluded.group_key,
                        resolution = excluded.resolution,
                        subtitle_kind = excluded.subtitle_kind,
                        payload = excluded.payload,
                        missing = 0,
                        last_seen_at = excluded.last_seen_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(movie.path),
                        scan_root,
                        size_bytes,
                        mtime_ns,
                        sidecar_signature,
                        movie.path.suffix.lower(),
                        movie.path.name,
                        movie.group_key,
                        movie.resolution,
                        movie.subtitle_kind,
                        payload,
                        now,
                        now,
                    ),
                )
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def _mark_missing(self, media_dirs: list[Path], current_paths: set[str]) -> int:
        conn = self._connect()
        if not conn:
            return 0
        roots = [str(path) for path in media_dirs]
        if not roots:
            return 0
        placeholders = ",".join("?" for _ in roots)
        try:
            rows = conn.execute(
                f"SELECT path FROM duplicate_scan_files WHERE scan_root IN ({placeholders}) AND missing = 0",
                roots,
            ).fetchall()
            missing_paths = [str(row[0]) for row in rows if str(row[0]) not in current_paths]
            if missing_paths:
                mark_placeholders = ",".join("?" for _ in missing_paths)
                with conn:
                    conn.execute(
                        f"""
                        UPDATE duplicate_scan_files
                        SET missing = 1, updated_at = ?
                        WHERE path IN ({mark_placeholders})
                        """,
                        [time.time(), *missing_paths],
                    )
            return len(missing_paths)
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS duplicate_scan_files (
                    path TEXT PRIMARY KEY,
                    scan_root TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sidecar_signature TEXT NOT NULL DEFAULT '',
                    suffix TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    group_key TEXT NOT NULL DEFAULT '',
                    resolution TEXT NOT NULL DEFAULT '',
                    subtitle_kind TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    missing INTEGER NOT NULL DEFAULT 0,
                    last_seen_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_duplicate_scan_files_root_missing ON duplicate_scan_files(scan_root, missing)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_duplicate_scan_files_group ON duplicate_scan_files(group_key)"
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(duplicate_scan_files)").fetchall()
            }
            if "sidecar_signature" not in columns:
                conn.execute("ALTER TABLE duplicate_scan_files ADD COLUMN sidecar_signature TEXT NOT NULL DEFAULT ''")
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
