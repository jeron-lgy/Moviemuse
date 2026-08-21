from __future__ import annotations

import atexit
import ctypes
import html
import json
import os
import socket
import subprocess
import sys
import threading
import time
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


APP_TITLE = "MovieMuse Worker"
DEFAULT_PORT = 18181
STARTUP_TIMEOUT_SECONDS = 45.0
SINGLE_INSTANCE_NAME = "Local\\MovieMuseWorkerDesktop"
LEGACY_MIGRATION_EXCLUDED_DIRECTORIES = {"logs", "webview"}

_backend_process: subprocess.Popen[bytes] | None = None
_backend_log_handle: Any | None = None
_owns_backend = False
_shutdown_lock = threading.Lock()
_instance_mutex: int | None = None
_url_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def package_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def configured_port() -> int:
    try:
        port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def worker_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/worker"


def _worker_status(port: int, timeout: float = 0.8) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/worker/status",
        headers={"Accept": "application/json", "User-Agent": "MovieMuseWorkerDesktop"},
    )
    try:
        with _url_opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read(1_048_576).decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("status") != "ok" or not payload.get("hostname"):
        return None
    if "effective_config" not in payload or "hardware" not in payload:
        return None
    return payload


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _backend_python(root: Path) -> Path | None:
    candidates = [
        root / "python" / "pythonw.exe",
        root / "python" / "python.exe",
        root / ".venv" / "Scripts" / "pythonw.exe",
        root / ".venv" / "Scripts" / "python.exe",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _default_data_root(root: Path, env: dict[str, str]) -> Path:
    local_app_data = str(env.get("LOCALAPPDATA") or "").strip()
    if os.name == "nt" and local_app_data:
        return Path(local_app_data) / "MovieMuse Worker"
    return root / "data" / "local-backend"


def _legacy_data_candidates(root: Path) -> list[Path]:
    candidates = [root / "data" / "local-backend"]
    try:
        siblings = sorted(
            (
                item for item in root.parent.iterdir()
                if item.is_dir() and item != root and item.name.lower().startswith("moviemuse-windows-worker-")
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        siblings = []
    candidates.extend(item / "data" / "local-backend" for item in siblings)
    return candidates


def _has_migratable_legacy_data(candidate: Path) -> bool:
    try:
        return any(
            not (item.is_dir() and item.name.lower() in LEGACY_MIGRATION_EXCLUDED_DIRECTORIES)
            for item in candidate.iterdir()
        )
    except OSError:
        return False


def _migrate_legacy_data(root: Path, data_root: Path) -> None:
    target = data_root.resolve()
    marker = target / ".migrated-from-package-data"
    if marker.exists():
        return
    legacy = next(
        (
            candidate.resolve()
            for candidate in _legacy_data_candidates(root)
            if (
                candidate.is_dir()
                and candidate.resolve() != target
                and _has_migratable_legacy_data(candidate)
            )
        ),
        None,
    )
    if legacy is None:
        return
    target.mkdir(parents=True, exist_ok=True)
    for source in legacy.iterdir():
        if source.is_dir() and source.name.lower() in LEGACY_MIGRATION_EXCLUDED_DIRECTORIES:
            continue
        destination = target / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        elif not destination.exists():
            shutil.copy2(source, destination)
    marker.write_text(str(legacy), encoding="utf-8")


def _backend_environment(root: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    configured_data = str(env.get("APP_DATA_DIR") or "").strip()
    data_root = Path(configured_data).resolve() if configured_data else _default_data_root(root, env).resolve()
    if not configured_data:
        _migrate_legacy_data(root, data_root)
    model_root = Path(env.get("WHISPER_MODEL_DIR") or data_root / "whisper-models").resolve()
    env.setdefault("APP_DATA_DIR", str(data_root))
    env.setdefault("WHISPER_MODEL_DIR", str(model_root))
    env.setdefault("WHISPER_MODEL", "large-v3-turbo")
    env.setdefault("WHISPER_DEVICE", "cuda")
    env.setdefault("WHISPER_COMPUTE_TYPE", "float16")
    env.setdefault("SUBTITLE_MAX_WORKERS", "1")
    env.setdefault("SUBTITLE_PATH_MAP", "")
    env.setdefault("COMPUTE_NODE_ONLY", "1")
    env["HOST"] = "0.0.0.0"
    env["PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"

    local_gpu_runtime = data_root / "gpu-runtime"
    nvidia_bins = [
        local_gpu_runtime / "nvidia" / "cublas" / "bin",
        local_gpu_runtime / "nvidia" / "cudnn" / "bin",
        local_gpu_runtime / "nvidia" / "cuda_nvrtc" / "bin",
        root / "python" / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin",
        root / "python" / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin",
        root / "python" / "Lib" / "site-packages" / "nvidia" / "cuda_nvrtc" / "bin",
        root / ".venv" / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin",
        root / ".venv" / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin",
        root / ".venv" / "Lib" / "site-packages" / "nvidia" / "cuda_nvrtc" / "bin",
    ]
    existing_bins = [str(path) for path in nvidia_bins if path.is_dir()]
    if existing_bins:
        env["PATH"] = os.pathsep.join(existing_bins + [env.get("PATH", "")])
    return env


def _log_path(root: Path, env: dict[str, str]) -> Path:
    data_root = Path(env["APP_DATA_DIR"])
    log_root = data_root / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    return log_root / "worker-backend.log"


def _start_backend(root: Path, port: int) -> subprocess.Popen[bytes]:
    global _backend_log_handle, _backend_process, _owns_backend

    python_exe = _backend_python(root)
    worker_script = root / "run_worker.py"
    if python_exe is None:
        raise RuntimeError("未找到内置 Python 运行环境，请重新解压完整的 Worker 安装包。")
    if not worker_script.is_file():
        raise RuntimeError("未找到 run_worker.py，请重新解压完整的 Worker 安装包。")

    env = _backend_environment(root, port)
    model_root = Path(env["WHISPER_MODEL_DIR"])
    model_root.mkdir(parents=True, exist_ok=True)
    log_path = _log_path(root, env)
    _backend_log_handle = log_path.open("ab", buffering=0)

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        _backend_process = subprocess.Popen(
            [str(python_exe), str(worker_script)],
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=_backend_log_handle,
            stderr=subprocess.STDOUT,
            startupinfo=startupinfo,
            creationflags=creation_flags,
            close_fds=True,
        )
    except Exception:
        _backend_log_handle.close()
        _backend_log_handle = None
        raise
    _owns_backend = True
    return _backend_process


def _log_tail(root: Path, limit: int = 3500) -> str:
    env = _backend_environment(root, configured_port())
    path = _log_path(root, env)
    try:
        data = path.read_bytes()[-limit:]
        return data.decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _stop_owned_backend() -> None:
    global _backend_log_handle, _backend_process, _owns_backend
    with _shutdown_lock:
        process = _backend_process
        if _owns_backend and process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if _backend_log_handle is not None:
            try:
                _backend_log_handle.close()
            except OSError:
                pass
        _backend_log_handle = None
        _backend_process = None
        _owns_backend = False


def _loading_html() -> str:
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{height:100%;margin:0;background:#111318;color:#f4f4f6;font-family:"Microsoft YaHei UI","Segoe UI",sans-serif}
body{display:grid;place-items:center}.card{text-align:center}.mark{width:58px;height:58px;margin:0 auto 22px;border-radius:16px;
background:linear-gradient(145deg,#ff6b77,#d9344c);display:grid;place-items:center;font-size:24px;font-weight:700;box-shadow:0 16px 50px #e4485a35}
h1{font-size:21px;margin:0 0 10px;font-weight:600;letter-spacing:.2px}p{margin:0;color:#969ba8;font-size:13px}
.loader{width:190px;height:3px;background:#262a32;border-radius:3px;margin:24px auto 0;overflow:hidden}.loader:after{content:"";display:block;width:42%;height:100%;
background:linear-gradient(90deg,#f45d70,#ff8190);border-radius:3px;animation:move 1.25s ease-in-out infinite}@keyframes move{0%{transform:translateX(-110%)}100%{transform:translateX(350%)}}
</style></head><body><div class="card"><div class="mark">M</div><h1>MovieMuse Worker</h1><p>正在启动算力服务…</p><div class="loader"></div></div></body></html>"""


def _error_html(message: str, detail: str = "") -> str:
    safe_message = html.escape(message)
    safe_detail = html.escape(detail)
    detail_block = f"<pre>{safe_detail}</pre>" if safe_detail else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{{height:100%;margin:0;background:#111318;color:#f4f4f6;font-family:"Microsoft YaHei UI","Segoe UI",sans-serif}}
body{{display:grid;place-items:center;padding:36px;box-sizing:border-box}}.card{{width:min(660px,100%);padding:30px;border:1px solid #343842;border-radius:14px;background:#181b22}}
.badge{{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:10px;background:#4b222a;color:#ff6f7f;font-size:20px;margin-bottom:16px}}
h1{{font-size:20px;margin:0 0 12px}}p{{font-size:14px;line-height:1.75;color:#c0c4ce;margin:0}}pre{{margin:18px 0 0;padding:14px;max-height:230px;overflow:auto;white-space:pre-wrap;
background:#101217;border:1px solid #2b2e36;border-radius:9px;color:#9fa5b1;font:12px/1.55 Consolas,monospace}}
</style></head><body><div class="card"><div class="badge">!</div><h1>启动失败</h1><p>{safe_message}</p>{detail_block}</div></body></html>"""


def _bootstrap(window: Any) -> None:
    # pywebview starts this callback before the initial document has necessarily
    # finished loading. Navigating during that interval is ignored by WebView2,
    # leaving the splash page visible indefinitely.
    if not window.events.loaded.wait(15):
        return

    root = package_root()
    port = configured_port()

    status = _worker_status(port)
    if status is not None:
        window.load_url(worker_url(port))
        return
    if _port_is_open(port):
        window.load_html(
            _error_html(
                f"端口 {port} 已被其他程序占用，且它不是可识别的 MovieMuse Worker。"
                "请关闭占用该端口的程序后重试。"
            )
        )
        return

    try:
        process = _start_backend(root, port)
    except Exception as exc:
        window.load_html(_error_html(str(exc)))
        return

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = _worker_status(port)
        if status is not None:
            window.load_url(worker_url(port))
            return
        if process.poll() is not None:
            window.load_html(
                _error_html(
                    "Worker 后台进程在启动期间退出。",
                    _log_tail(root) or "未生成详细日志。",
                )
            )
            return
        time.sleep(0.25)

    window.load_html(
        _error_html(
            f"Worker 在 {int(STARTUP_TIMEOUT_SECONDS)} 秒内未完成启动。",
            _log_tail(root) or "未生成详细日志。",
        )
    )


def _set_windows_app_identity() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MovieMuse.Worker.Desktop")
    except (AttributeError, OSError):
        pass


def _acquire_single_instance() -> bool:
    global _instance_mutex
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_NAME)
    if not handle:
        return True
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return False
    _instance_mutex = handle
    return True


def _release_single_instance() -> None:
    global _instance_mutex
    if _instance_mutex:
        try:
            ctypes.windll.kernel32.ReleaseMutex(_instance_mutex)
            ctypes.windll.kernel32.CloseHandle(_instance_mutex)
        except (AttributeError, OSError):
            pass
        _instance_mutex = None


def main() -> None:
    _set_windows_app_identity()
    if not _acquire_single_instance():
        ctypes.windll.user32.MessageBoxW(
            None,
            "MovieMuse Worker 已经在运行。",
            APP_TITLE,
            0x00000040,
        )
        return

    atexit.register(_stop_owned_backend)
    atexit.register(_release_single_instance)

    try:
        import webview

        root = package_root()
        # Keep WebView2's live profile outside the release directory. Besides
        # surviving upgrades, this prevents the migration process from trying
        # to copy its own locked Cookies database during startup.
        storage_path = _default_data_root(root, os.environ) / "webview"
        storage_path.mkdir(parents=True, exist_ok=True)
        window = webview.create_window(
            APP_TITLE,
            html=_loading_html(),
            width=1280,
            height=820,
            min_size=(1024, 680),
            background_color="#111318",
            text_select=True,
            zoomable=False,
        )
        if window is None:
            raise RuntimeError("无法创建 Worker 窗口。")
        window.events.closed += _stop_owned_backend
        webview.start(
            _bootstrap,
            (window,),
            gui="edgechromium",
            debug=False,
            private_mode=False,
            storage_path=str(storage_path),
        )
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"MovieMuse Worker 无法启动：\n\n{exc}\n\n请确认 Microsoft Edge WebView2 Runtime 已安装。",
            APP_TITLE,
            0x00000010,
        )
    finally:
        _stop_owned_backend()
        _release_single_instance()


if __name__ == "__main__":
    main()
