from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def split_configured_paths(value: str) -> list[Path]:
    return [Path(item.strip()) for item in value.replace("\n", ";").split(";") if item.strip()]


def resolved_roots(paths: Iterable[Path]) -> list[Path]:
    roots: list[Path] = []
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser().absolute()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def path_is_within(path: Path, roots: Iterable[Path]) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser().absolute()
    for root in resolved_roots(roots):
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def require_path_within(path: Path, roots: Iterable[Path], label: str) -> Path:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser().absolute()
    allowed = resolved_roots(roots)
    if not allowed:
        raise ValueError("未配置允许访问的媒体目录，请先设置路径映射或 COMPUTE_ALLOWED_MEDIA_DIRS")
    if not path_is_within(resolved, allowed):
        raise ValueError(f"{label}不在允许的媒体目录内: {resolved}")
    return resolved


def configured_compute_roots(path_map: Iterable[tuple[str, str]]) -> list[Path]:
    explicit = os.getenv("COMPUTE_ALLOWED_MEDIA_DIRS", "").strip()
    if explicit:
        return resolved_roots(split_configured_paths(explicit))

    roots = split_configured_paths(os.getenv("MEDIA_DIRS", ""))
    roots.extend(Path(target) for _, target in path_map if str(target or "").strip())
    return resolved_roots(roots)
