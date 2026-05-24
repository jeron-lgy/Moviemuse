from __future__ import annotations

import html
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from app.scanner import IMAGE_EXTS, MovieFile, MovieGroup, scan_libraries
from app.storage import MoveRequest, Storage


HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8080"))
MEDIA_DIRS = [Path(item.strip()) for item in os.getenv("MEDIA_DIRS", "sample-media").split(";") if item.strip()]
TRASH_DIR = Path(os.getenv("TRASH_DIR", "trash"))
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "data"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.respond_html(render_index())
            return
        if parsed.path == "/cover":
            params = parse_qs(parsed.query)
            self.respond_file(params.get("path", [""])[0])
            return
        if parsed.path == "/static/app.js":
            self.respond_static(Path("app/static/app.js"), "application/javascript; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)
        paths = form.get("paths", [])
        if self.path == "/preview":
            self.respond_html(render_preview(paths))
            return
        if self.path == "/move":
            store = Storage(APP_DATA_DIR, TRASH_DIR, MEDIA_DIRS)
            store.move_to_trash([MoveRequest(source=Path(path)) for path in paths])
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def respond_html(self, content: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def respond_static(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        encoded = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def respond_file(self, raw_path: str) -> None:
        image_path = Path(unquote(raw_path)).resolve()
        if (
            not image_path.exists()
            or not image_path.is_file()
            or image_path.suffix.lower() not in IMAGE_EXTS
            or not is_under_media_dirs(image_path)
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = image_path.read_bytes()
        content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def render_index() -> str:
    result = scan_libraries(MEDIA_DIRS)
    store = Storage(APP_DATA_DIR, TRASH_DIR, MEDIA_DIRS)
    rows = "\n".join(render_group(group) for group in result.groups)
    missing = ""
    if result.missing_dirs:
        missing_dirs = " ".join(f"<code>{escape_path(item)}</code>" for item in result.missing_dirs)
        missing = f'<section class="warning">未找到目录：{missing_dirs}</section>'
    history = render_history(store.recent_moves())
    media_dirs = " ".join(f"<code>{escape_path(item)}</code>" for item in MEDIA_DIRS)
    empty = '<section class="empty">暂时没有发现重复组。</section>' if not result.groups else ""
    return page(
        "电影去重助手",
        f"""
        <header class="topbar">
          <div>
            <h1>电影去重助手</h1>
            <p>扫描 {result.total_files} 个视频文件，发现 {len(result.groups)} 组重复，共 {result.duplicate_files} 个版本。</p>
          </div>
          <a class="button ghost" href="/">重新扫描</a>
        </header>
        <section class="config">
          <div><span>媒体目录</span>{media_dirs}</div>
          <div><span>回收站</span><code>{escape_path(TRASH_DIR)}</code></div>
        </section>
        {missing}
        <form method="post" action="/preview">
          {render_toolbar()}
          <div class="selection-bar">已选择 <strong id="selectedCount">0</strong> 个文件，当前显示 <strong id="visibleCount">0</strong> 个文件。</div>
          <section class="groups" id="groups">{rows}</section>
          {empty}
        </form>
        {history}
        """,
    )


def render_toolbar() -> str:
    return """
    <section class="toolbar">
      <div class="tool-block">
        <label>搜索<input id="searchText" type="search" placeholder="标题 / 文件名 / 路径"></label>
        <label>分辨率<select id="resolutionFilter"><option value="">全部</option><option value="4K">4K</option><option value="1080p">1080p</option><option value="720p">720p</option><option value="未知">未知</option></select></label>
        <label>字幕<select id="subtitleFilter"><option value="">全部</option><option value="chinese">有中字标记</option><option value="subtitle">有字幕但未知</option><option value="none">未发现外挂字幕</option></select></label>
        <label>来源<select id="sourceFilter"><option value="">全部</option><option value="Remux">Remux</option><option value="BluRay">BluRay</option><option value="WEB-DL">WEB-DL</option><option value="WEBRip">WEBRip</option><option value="未知">未知</option></select></label>
      </div>
      <div class="tool-block">
        <button type="button" class="button" data-view="list">列表</button>
        <button type="button" class="button" data-view="cover">封面</button>
        <button type="button" class="button" data-select="visible">选择当前筛选</button>
        <button type="button" class="button" data-select="no-chinese">选择无中字</button>
        <button type="button" class="button" data-select="chinese">选择有中字</button>
        <button type="button" class="button" data-select="1080p">选择 1080p</button>
        <button type="button" class="button" data-select="clear">清空选择</button>
        <button type="submit" class="button primary">预览移动</button>
      </div>
    </section>
    """


def render_group(group: MovieGroup) -> str:
    files = "\n".join(render_file(file) for file in group.files)
    year = f" ({html.escape(group.year)})" if group.year else ""
    cover = render_cover(group)
    return f"""
    <article class="group dup-group" data-title="{escape_attr(group.title)} {escape_attr(group.year)}" data-source="{escape_attr(group.source)}">
      {cover}
      <div class="group-body">
        <div class="group-head">
          <h2>{html.escape(group.title)}{year}</h2>
          <p>匹配来源：{html.escape(group.source)} · {len(group.files)} 个版本</p>
        </div>
        <div class="files">{files}</div>
      </div>
    </article>
    """


def render_cover(group: MovieGroup) -> str:
    if group.cover_path:
        return f'<div class="cover-pane"><img src="/cover?path={quote(str(group.cover_path))}" alt="{escape_attr(group.title)} 封面"></div>'
    return '<div class="cover-pane"><div class="cover-placeholder">无封面</div></div>'


def render_file(file: MovieFile) -> str:
    subtitles = ""
    if file.subtitles:
        names = "，".join(html.escape(item.path.name) for item in file.subtitles)
        subtitles = f"<small>字幕文件：{names}</small>"
    good = "good" if file.subtitle_kind == "chinese" else ""
    return f"""
    <label
      class="file-row"
      data-name="{escape_attr(file.path.name)} {escape_attr(file.path)}"
      data-resolution="{escape_attr(file.resolution)}"
      data-source="{escape_attr(file.source_tag)}"
      data-subtitle-kind="{escape_attr(file.subtitle_kind)}"
    >
      <input class="file-check" type="checkbox" name="paths" value="{escape_attr(file.path)}">
      <div class="file-main">
        <strong>{html.escape(file.path.name)}</strong>
        <span>{escape_path(file.path)}</span>
        {subtitles}
      </div>
      <div class="badges">
        <em>{html.escape(file.size_label)}</em>
        <em>{html.escape(file.resolution)}</em>
        <em>{html.escape(file.source_tag)}</em>
        <em class="{good}">{html.escape(file.subtitle_label)}</em>
      </div>
    </label>
    """


def render_preview(paths: list[str]) -> str:
    store = Storage(APP_DATA_DIR, TRASH_DIR, MEDIA_DIRS)
    previews = store.preview(paths)
    rows = "\n".join(
        f"""
        <article class="preview-row {'blocked' if not item.allowed else ''}">
          <input type="hidden" name="paths" value="{escape_attr(item.source)}">
          <div>
            <strong>{html.escape(item.source.name)}</strong>
            <span>{escape_path(item.source)}</span>
            <span>{escape_path(item.target)}</span>
          </div>
          <em>{html.escape(item.reason)}</em>
        </article>
        """
        for item in previews
    )
    empty = '<section class="empty">还没有选择任何文件。</section>' if not previews else ""
    return page(
        "预览移动",
        f"""
        <header class="topbar">
          <div>
            <h1>预览移动</h1>
            <p>确认后文件会移动到 {escape_path(TRASH_DIR)}，不会直接删除。</p>
          </div>
          <a class="button ghost" href="/">返回</a>
        </header>
        <form method="post" action="/move">
          <section class="preview-list">{rows}</section>
          {empty}
          <div class="actions"><button type="submit" class="button danger">确认移动到回收站</button></div>
        </form>
        """,
    )


def render_history(rows: list[tuple[str, str, str]]) -> str:
    if not rows:
        return ""
    content = "\n".join(
        f"""
        <div class="history-row">
          <span>{html.escape(moved_at)}</span>
          <code>{html.escape(source)}</code>
          <code>{html.escape(target)}</code>
        </div>
        """
        for source, target, moved_at in rows
    )
    return f'<section class="history"><h2>最近移动</h2>{content}</section>'


def page(title: str, body: str) -> str:
    css = Path("app/static/style.css").read_text(encoding="utf-8")
    return f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{html.escape(title)}</title>
      <style>{css}</style>
    </head>
    <body>
      <main class="shell">{body}</main>
      <script src="/static/app.js"></script>
    </body>
    </html>
    """


def escape_path(path: Path | str) -> str:
    return html.escape(str(path))


def escape_attr(value: Path | str) -> str:
    return html.escape(str(value), quote=True)


def is_under_media_dirs(path: Path) -> bool:
    return any(media_dir.exists() and is_relative_to(path, media_dir.resolve()) for media_dir in MEDIA_DIRS)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def main() -> None:
    print("Movie Dedupe dev server")
    print(f"URL: http://{HOST}:{PORT}")
    print(f"MEDIA_DIRS: {'; '.join(str(item) for item in MEDIA_DIRS)}")
    print(f"TRASH_DIR: {TRASH_DIR}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
