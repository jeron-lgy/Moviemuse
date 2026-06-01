"""轻量系统日志。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class AppLogService:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.log_file = data_dir / "system_logs.jsonl"
        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def write(self, level: str, source: str, message: str, data: dict[str, Any] | None = None) -> None:
        entry = {
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "level": level,
            "source": source,
            "message": message,
            "data": data or {},
        }
        with self._lock:
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.log_file.exists():
            return []
        with self._lock:
            lines = self.log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        entries: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    entries.append(item)
            except json.JSONDecodeError:
                pass
        return list(reversed(entries))
