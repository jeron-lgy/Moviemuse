"""轻量系统日志。"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_FILE_LOG_LOCK = threading.RLock()


def append_json_log(log_file: Path, entry: dict[str, Any]) -> None:
    max_bytes = max(1024 * 1024, int(os.getenv("SYSTEM_LOG_MAX_BYTES", str(64 * 1024 * 1024))))
    backups = max(1, min(10, int(os.getenv("SYSTEM_LOG_BACKUPS", "2"))))
    encoded = json.dumps(entry, ensure_ascii=False) + "\n"
    with _FILE_LOG_LOCK:
        try:
            if log_file.exists() and log_file.stat().st_size + len(encoded.encode("utf-8")) > max_bytes:
                oldest = log_file.with_name(f"{log_file.name}.{backups}")
                if oldest.exists():
                    oldest.unlink()
                for index in range(backups - 1, 0, -1):
                    source = log_file.with_name(f"{log_file.name}.{index}")
                    if source.exists():
                        source.replace(log_file.with_name(f"{log_file.name}.{index + 1}"))
                log_file.replace(log_file.with_name(f"{log_file.name}.1"))
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(encoded)
        except OSError:
            raise


def tail_text_lines(path: Path, limit: int) -> list[str]:
    if not path.exists() or limit <= 0:
        return []
    chunk_size = 64 * 1024
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        while position > 0 and newline_count <= limit:
            read_size = min(chunk_size, position)
            position -= read_size
            fh.seek(position)
            chunk = fh.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    content = b"".join(reversed(chunks)).decode("utf-8", errors="ignore")
    return content.splitlines()[-limit:]


def app_timezone() -> tzinfo:
    name = os.getenv("APP_TIMEZONE") or os.getenv("TZ") or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


def format_log_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, app_timezone()).strftime("%Y-%m-%d %H:%M:%S")


class AppLogService:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.log_file = data_dir / "system_logs.jsonl"
        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def write(self, level: str, source: str, message: str, data: dict[str, Any] | None = None) -> None:
        ts = time.time()
        entry = {
            "ts": ts,
            "time": format_log_time(ts),
            "level": level,
            "source": source,
            "message": message,
            "data": data or {},
        }
        with self._lock:
            append_json_log(self.log_file, entry)

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.log_file.exists():
            return []
        with self._lock:
            lines = tail_text_lines(self.log_file, max(1, limit))
        entries: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    try:
                        item["time"] = format_log_time(float(item.get("ts") or 0))
                    except (TypeError, ValueError):
                        pass
                    entries.append(item)
            except json.JSONDecodeError:
                pass
        return list(reversed(entries))
