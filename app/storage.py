from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class MoveRequest:
    source: Path


@dataclass(frozen=True)
class MovePreview:
    source: Path
    target: Path
    exists: bool
    allowed: bool
    reason: str
    mode: str = "normal"
    operation_source: Path | None = None
    operation_target: Path | None = None


@dataclass(frozen=True)
class MoveResult:
    source: Path
    target: Path
    status: str
    reason: str
    mode: str


class Storage:
    def __init__(self, data_dir: Path, trash_dir: Path, media_dirs: list[Path]) -> None:
        self.data_dir = data_dir
        self.trash_dir = trash_dir
        self.media_dirs = media_dirs
        self.unraid_root = Path(os.getenv("UNRAID_MOUNT_ROOT", "/unraid"))
        self.unraid_trash_relative = Path(os.getenv("UNRAID_TRASH_RELATIVE", "media/trash"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "movie_dedupe.sqlite3"
        self.init_db()

    def init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    create table if not exists moves (
                        id integer primary key autoincrement,
                        source text not null,
                        target text not null,
                        moved_at text not null
                    )
                    """
                )
        except sqlite3.Error:
            # Moving files must still work even if the optional history database
            # is owned by a previous container user or mounted read-only.
            pass

    def preview(self, paths: list[str]) -> list[MovePreview]:
        return [self.preview_one(Path(path)) for path in paths]

    def preview_one(self, source: Path) -> MovePreview:
        source = source.resolve()
        if not source.exists():
            return MovePreview(source, self.trash_dir / source.name, False, False, "源文件不存在")
        if not self.is_under_media_dir(source):
            return MovePreview(source, self.trash_dir / source.name, True, False, "不在媒体目录内")

        target = self.target_for(source)
        operation_source, operation_target = self.fast_move_paths(source)
        mode = "fast" if operation_source and operation_target else "normal"
        target_to_check = operation_target or target
        if target.exists() or target_to_check.exists():
            return MovePreview(
                source,
                target,
                True,
                False,
                "回收站中已存在同名目标",
                mode,
                operation_source,
                operation_target,
            )
        reason = "同盘快速移动" if mode == "fast" else "可以移动（跨挂载点可能较慢）"
        return MovePreview(source, target, False, True, reason, mode, operation_source, operation_target)

    def move_to_trash(
        self,
        requests: list[MoveRequest],
        on_progress: Callable[[int, int, MoveResult], None] | None = None,
    ) -> list[MoveResult]:
        results: list[MoveResult] = []
        total = len(requests)
        for index, request in enumerate(requests, start=1):
            preview = self.preview_one(request.source)
            if not preview.allowed:
                result = MoveResult(preview.source, preview.target, "skipped", preview.reason, preview.mode)
                results.append(result)
                if on_progress:
                    on_progress(index, total, result)
                continue

            try:
                result = self.execute_move(preview)
                record_error = self.record_move(preview.source, preview.target)
                if record_error:
                    result = MoveResult(
                        result.source,
                        result.target,
                        result.status,
                        f"{result.reason}; moved, but history database was not writable: {record_error}",
                        result.mode,
                    )
            except Exception as exc:
                result = MoveResult(preview.source, preview.target, "failed", str(exc), preview.mode)
            results.append(result)
            if on_progress:
                on_progress(index, total, result)
        return results

    def execute_move(self, preview: MovePreview) -> MoveResult:
        if preview.mode == "fast" and preview.operation_source and preview.operation_target:
            try:
                preview.operation_target.parent.mkdir(parents=True, exist_ok=True)
                preview.operation_source.rename(preview.operation_target)
                return MoveResult(preview.source, preview.target, "moved", preview.reason, preview.mode)
            except OSError as fast_exc:
                try:
                    preview.target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(preview.source), str(preview.target))
                    return MoveResult(
                        preview.source,
                        preview.target,
                        "moved",
                        f"fast move failed, used /media fallback: {fast_exc}",
                        "normal",
                    )
                except Exception as fallback_exc:
                    raise OSError(f"{fast_exc}; /media fallback failed: {fallback_exc}") from fallback_exc

        preview.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(preview.source), str(preview.target))
        return MoveResult(preview.source, preview.target, "moved", preview.reason, preview.mode)

    def record_move(self, source: Path, target: Path) -> str | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "insert into moves (source, target, moved_at) values (?, ?, ?)",
                    (str(source), str(target), datetime.now().isoformat(timespec="seconds")),
                )
        except sqlite3.Error as exc:
            return str(exc)
        return None

    def recent_moves(self) -> list[tuple[str, str, str]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "select source, target, moved_at from moves order by id desc limit 20"
                ).fetchall()
        except sqlite3.Error:
            return []
        return [(row[0], row[1], row[2]) for row in rows]

    def is_under_media_dir(self, source: Path) -> bool:
        return any(is_relative_to(source, media_dir.resolve()) for media_dir in self.media_dirs if media_dir.exists())

    def target_for(self, source: Path) -> Path:
        for media_dir in self.media_dirs:
            resolved_media_dir = media_dir.resolve()
            if media_dir.exists() and is_relative_to(source, resolved_media_dir):
                relative = source.relative_to(resolved_media_dir)
                return self.trash_dir / resolved_media_dir.name / relative
        return self.trash_dir / source.name

    def fast_move_paths(self, source: Path) -> tuple[Path | None, Path | None]:
        if os.name == "nt" or not self.unraid_root.exists():
            return None, None
        media_match = self.media_relative_path(source)
        if not media_match:
            return None, None
        media_share, relative = media_match
        for disk_root in self.unraid_disk_roots():
            physical_source = disk_root / media_share / relative
            if not physical_source.exists():
                continue
            physical_target = disk_root / self.unraid_trash_relative / media_share / relative
            return physical_source, physical_target
        return None, None

    def media_relative_path(self, source: Path) -> tuple[str, Path] | None:
        for media_dir in self.media_dirs:
            resolved_media_dir = media_dir.resolve()
            if media_dir.exists() and is_relative_to(source, resolved_media_dir):
                return resolved_media_dir.name, source.relative_to(resolved_media_dir)
        return None

    def unraid_disk_roots(self) -> list[Path]:
        if not self.unraid_root.exists():
            return []
        roots = []
        for child in self.unraid_root.iterdir():
            if not child.is_dir():
                continue
            name = child.name.lower()
            if name.startswith("disk") or name.startswith("cache"):
                roots.append(child)
        return sorted(roots, key=lambda item: item.name)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
