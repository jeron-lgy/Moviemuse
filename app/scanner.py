from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


VIDEO_EXTS = {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".ts", ".webm"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sup", ".idx", ".sub"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
CHINESE_SUB_SUFFIXES = {"-c", ".zh", ".zho", ".chi", ".chs", ".cht", ".sc", ".tc", ".中字", ".简体", ".繁体"}
CHINESE_KEYWORDS = ("中字", "中文", "简中", "繁中", "chs", "cht", "chinese")


@dataclass(frozen=True)
class SubtitleMatch:
    path: Path
    label: str
    confidence: str


@dataclass(frozen=True)
class MovieFile:
    path: Path
    title: str
    year: str
    group_key: str
    group_source: str
    size_bytes: int
    nfo_path: Path | None
    imdb_id: str
    tmdb_id: str
    catalog_number: str
    resolution: str
    source_tag: str
    uncensored: bool
    ignored: bool
    chinese_markers: tuple[str, ...]
    subtitles: tuple[SubtitleMatch, ...]
    cover_path: Path | None

    @property
    def size_label(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} TB"

    @property
    def subtitle_label(self) -> str:
        labels: list[str] = []
        if self.chinese_markers:
            labels.append("文件名标记中字")
        if any(item.label == "外挂中字" for item in self.subtitles):
            labels.append("外挂中字")
        elif self.subtitles:
            labels.append("外挂字幕")
        if self.srt_count:
            labels.append(self.srt_label)
        return " + ".join(labels) if labels else "无字幕"

    @property
    def subtitle_kind(self) -> str:
        if self.chinese_markers or any(item.label == "外挂中字" for item in self.subtitles):
            return "chinese"
        if self.subtitles:
            return "subtitle"
        return "none"

    @property
    def srt_count(self) -> int:
        return sum(1 for item in self.subtitles if item.path.suffix.lower() == ".srt")

    @property
    def srt_label(self) -> str:
        if self.srt_count == 0:
            return "无 SRT"
        if self.srt_count == 1:
            return "SRT"
        return f"SRT {self.srt_count} 个"


@dataclass(frozen=True)
class MovieGroup:
    key: str
    title: str
    year: str
    source: str
    cover_path: Path | None
    files: tuple[MovieFile, ...]


@dataclass(frozen=True)
class ScanResult:
    groups: tuple[MovieGroup, ...]
    total_files: int
    duplicate_files: int
    scanned_dirs: tuple[Path, ...]
    files: tuple[MovieFile, ...] = field(default_factory=tuple)
    missing_dirs: tuple[Path, ...] = field(default_factory=tuple)


ScanProgress = Callable[[int, int, Path | None], None]


def scan_libraries(
    media_dirs: list[Path],
    excluded_dirs: list[Path] | None = None,
    progress: ScanProgress | None = None,
) -> ScanResult:
    files: list[MovieFile] = []
    missing_dirs: list[Path] = []
    videos: list[Path] = []
    excluded_roots = normalized_roots(excluded_dirs or [])
    for media_dir in media_dirs:
        if not media_dir.exists():
            missing_dirs.append(media_dir)
            continue
        videos.extend(iter_video_files(media_dir, excluded_roots))

    total = len(videos)
    if progress:
        progress(0, total, None)
    for index, video in enumerate(videos, start=1):
        files.append(analyze_video(video))
        if progress:
            progress(index, total, video)

    return build_scan_result(files, media_dirs, missing_dirs)


def build_scan_result(
    files: Iterable[MovieFile],
    media_dirs: list[Path],
    missing_dirs: list[Path] | None = None,
) -> ScanResult:
    file_list = list(files)
    grouped: dict[str, list[MovieFile]] = {}
    for item in file_list:
        grouped.setdefault(item.group_key, []).append(item)

    groups = []
    for key, items in grouped.items():
        if len(items) < 2:
            continue
        sorted_items = sorted(items, key=lambda file: (file.title, file.path.name.lower()))
        first = sorted_items[0]
        groups.append(
            MovieGroup(
                key=key,
                title=first.title,
                year=first.year,
                source=first.group_source,
                cover_path=next((file.cover_path for file in sorted_items if file.cover_path), None),
                files=tuple(sorted_items),
            )
        )
    groups.sort(key=lambda group: (group.title.lower(), group.year))
    duplicate_files = sum(len(group.files) for group in groups)
    sorted_files = tuple(sorted(file_list, key=lambda file: str(file.path).lower()))
    return ScanResult(tuple(groups), len(file_list), duplicate_files, tuple(media_dirs), sorted_files, tuple(missing_dirs or []))


def iter_video_files(root: Path, excluded_roots: tuple[Path, ...]) -> Iterable[Path]:
    try:
        resolved_root = root.resolve()
    except OSError:
        resolved_root = root.absolute()
    if is_under_any(resolved_root, excluded_roots):
        return

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        for child in children:
            try:
                resolved_child = child.resolve()
            except OSError:
                resolved_child = child.absolute()
            if is_under_any(resolved_child, excluded_roots):
                continue
            if child.is_dir():
                stack.append(child)
            elif child.is_file() and child.suffix.lower() in VIDEO_EXTS:
                yield child


def normalized_roots(paths: list[Path]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for path in paths:
        try:
            roots.append(path.resolve())
        except OSError:
            roots.append(path.absolute())
    return tuple(roots)


def is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(is_relative_to(path, root) for root in roots)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def analyze_video(path: Path) -> MovieFile:
    nfo = find_nfo(path)
    info = read_nfo(nfo) if nfo else {}
    ignored = detect_ignored_name(path)
    parsed_title, parsed_year = parse_title_year(path.stem)
    title = info.get("title") or info.get("originaltitle") or parsed_title
    year = info.get("year") or parsed_year
    imdb_id = normalize_id(info.get("imdbid") or info.get("imdb_id") or "")
    tmdb_id = normalize_id(info.get("tmdbid") or info.get("tmdb_id") or info.get("id") or "")
    catalog_number = normalize_catalog_number(info.get("num") or detect_catalog_number(path.stem) or detect_catalog_number(title))
    if ignored:
        group_key, group_source = f"ignored:{str(path.resolve()).lower()}", "未知"
    else:
        group_key, group_source = build_group_key(title, year, imdb_id, tmdb_id, catalog_number)
    subtitles = tuple(find_subtitles(path))
    return MovieFile(
        path=path,
        title=title,
        year=year,
        group_key=group_key,
        group_source=group_source,
        size_bytes=path.stat().st_size,
        nfo_path=nfo,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        catalog_number=catalog_number,
        resolution="未知" if ignored else detect_resolution(path.name),
        source_tag="未知" if ignored else detect_source(path.name),
        uncensored=detect_uncensored(path),
        ignored=ignored,
        chinese_markers=tuple(detect_chinese_markers(path)),
        subtitles=subtitles,
        cover_path=find_cover(path),
    )


def find_nfo(video: Path) -> Path | None:
    return find_sibling(video, f"{video.stem}.nfo")


def find_cover(video: Path) -> Path | None:
    for ext in IMAGE_EXTS:
        match = find_sibling(video, f"{video.stem}-poster{ext}")
        if match:
            return match
    return None


def find_sibling(video: Path, name: str) -> Path | None:
    candidate = video.parent / name
    if candidate.exists() and candidate.is_file():
        return candidate
    expected = name.lower()
    for item in video.parent.iterdir():
        if item.is_file() and item.name.lower() == expected:
            return item
    return None


def read_nfo(path: Path) -> dict[str, str]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}
    values: dict[str, str] = {}
    for tag in ("title", "originaltitle", "year", "imdbid", "imdb_id", "tmdbid", "tmdb_id", "id", "num"):
        node = root.find(tag)
        if node is not None and node.text:
            values[tag] = node.text.strip()
    uniqueids = root.findall("uniqueid")
    for node in uniqueids:
        kind = (node.attrib.get("type") or "").lower()
        if node.text and kind in {"imdb", "tmdb"}:
            values[f"{kind}id"] = node.text.strip()
    return values


def normalize_id(value: str) -> str:
    return value.strip().lower()


def build_group_key(title: str, year: str, imdb_id: str, tmdb_id: str, catalog_number: str) -> tuple[str, str]:
    if tmdb_id:
        return f"tmdb:{tmdb_id}", "TMDb ID"
    if imdb_id:
        return f"imdb:{imdb_id}", "IMDb ID"
    if catalog_number:
        return f"num:{catalog_number}", "NFO 编号"
    clean_title = normalize_title(title)
    return f"title:{clean_title}:{year}", "标题 + 年份"


def parse_title_year(stem: str) -> tuple[str, str]:
    cleaned = stem.replace(".", " ").replace("_", " ")
    match = re.search(r"(19\d{2}|20\d{2})", cleaned)
    year = match.group(1) if match else ""
    title = cleaned[: match.start()] if match else cleaned
    title = re.sub(r"\s+", " ", title).strip(" -[]()")
    return title or stem, year


def normalize_title(title: str) -> str:
    return re.sub(r"\W+", "", title, flags=re.UNICODE).lower()


def normalize_catalog_digits(value: str) -> str:
    number = str(value or "").strip()
    if not number:
        return ""
    if len(number) <= 3:
        return number.zfill(3)
    if number.startswith("0"):
        stripped = number.lstrip("0") or number
        if len(stripped) <= 3:
            return stripped.zfill(3)
        return stripped
    return number


def detect_catalog_number(value: str) -> str:
    match = re.search(r"(?<![a-zA-Z0-9])([a-zA-Z]{2,10})[-_ ]?(\d{2,6})(?:[-_ ]?[cC])?(?![a-zA-Z0-9])", value)
    if not match:
        return ""
    return f"{match.group(1)}-{normalize_catalog_digits(match.group(2))}"


def normalize_catalog_number(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    detected = detect_catalog_number(value)
    if detected:
        value = detected
    value = re.sub(r"[-_ ]?[cC]$", "", value)
    value = value.replace("_", "-").replace(" ", "-")
    value = re.sub(r"-+", "-", value)
    return value.strip("-").lower()


def detect_resolution(name: str) -> str:
    lower = name.lower()
    if any(token in lower for token in ("2160p", "4k", "uhd")):
        return "4K"
    if "1080p" in lower:
        return "1080p"
    if "720p" in lower:
        return "720p"
    return "未知"


def detect_source(name: str) -> str:
    lower = name.lower()
    if "remux" in lower:
        return "Remux"
    if "blu-ray" in lower or "bluray" in lower or "bdrip" in lower:
        return "BluRay"
    if "web-dl" in lower or "webdl" in lower:
        return "WEB-DL"
    if "webrip" in lower:
        return "WEBRip"
    return "未知"


def detect_ignored_name(path: Path) -> bool:
    stem = path.stem.lower()
    return bool(re.search(r"(^|[^a-z0-9])(vr|cd\d*)($|[^a-z0-9])", stem))


def detect_uncensored(path: Path) -> bool:
    return path.stem.lower().endswith("-uc")


def detect_chinese_markers(path: Path) -> list[str]:
    stem = path.stem
    lower = stem.lower()
    markers = []
    if lower.endswith("-c"):
        markers.append("-c")
    for keyword in CHINESE_KEYWORDS:
        if keyword in lower:
            markers.append(keyword)
    return sorted(set(markers))


def find_subtitles(video: Path) -> list[SubtitleMatch]:
    matches: list[SubtitleMatch] = []
    video_stem_lower = video.stem.lower()
    for candidate in video.parent.iterdir():
        if not candidate.is_file() or candidate.suffix.lower() not in SUBTITLE_EXTS:
            continue
        candidate_stem_lower = candidate.stem.lower()
        if not candidate_stem_lower.startswith(video_stem_lower):
            continue
        if is_chinese_subtitle_stem(candidate.stem):
            matches.append(SubtitleMatch(candidate, "外挂中字", "高"))
        else:
            matches.append(SubtitleMatch(candidate, "外挂字幕", "中"))
    return matches


def is_chinese_subtitle_stem(stem: str) -> bool:
    lower = stem.lower()
    if any(lower.endswith(suffix) for suffix in CHINESE_SUB_SUFFIXES):
        return True
    return any(keyword in lower for keyword in CHINESE_KEYWORDS)
