from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any


WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class WorkerReadinessService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] = {
            "status": "not_checked",
            "ready": False,
            "summary": "等待自动体检",
            "checks": [],
            "checked_at": 0,
            "next_action": {"id": "scan", "label": "开始体检"},
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._snapshot,
                "checks": [dict(item) for item in self._snapshot.get("checks", [])],
                "next_action": dict(self._snapshot.get("next_action") or {}),
            }

    @staticmethod
    def _check(check_id: str, label: str, status: str, summary: str, detail: str = "", action: str = "") -> dict[str, str]:
        return {
            "id": check_id,
            "label": label,
            "status": status,
            "summary": summary,
            "detail": detail,
            "action": action,
        }

    @staticmethod
    def _mapped_roots(settings: Any) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        for _, target in list(getattr(settings, "path_map", []) or []):
            raw = str(target or "").strip()
            if not raw:
                continue
            key = os.path.normcase(os.path.normpath(raw))
            if key in seen:
                continue
            seen.add(key)
            roots.append(Path(raw))
        return roots

    @staticmethod
    def _media_access_check(roots: list[Path]) -> tuple[dict[str, str], dict[str, str]]:
        if not roots:
            missing = WorkerReadinessService._check(
                "media_read",
                "媒体目录",
                "fail",
                "尚未配置路径映射",
                "请在 MovieMuse 控制端配置 Unraid 到 Windows 的路径映射并重新保存连接。",
                "controller",
            )
            return missing, WorkerReadinessService._check(
                "media_write", "输出权限", "fail", "无法验证写入权限", action="controller"
            )

        read_errors: list[str] = []
        write_errors: list[str] = []
        for root in roots:
            try:
                if not root.exists() or not root.is_dir():
                    raise FileNotFoundError("目录不存在")
                next(root.iterdir(), None)
            except OSError as exc:
                read_errors.append(f"{root}: {exc}")
                continue

            probe = root / f".moviemuse-worker-write-test-{os.getpid()}-{uuid.uuid4().hex}.tmp"
            try:
                with probe.open("xb") as handle:
                    handle.write(b"MovieMuse Worker readiness probe\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                write_errors.append(f"{root}: {exc}")
            finally:
                try:
                    probe.unlink(missing_ok=True)
                except OSError as exc:
                    write_errors.append(f"{root}: 临时文件清理失败: {exc}")

        read_check = WorkerReadinessService._check(
            "media_read",
            "媒体目录",
            "pass" if not read_errors else "fail",
            f"{len(roots)} 个映射目录可读取" if not read_errors else "Windows 无法读取映射目录",
            "；".join(read_errors[:3]),
            "controller" if read_errors else "",
        )
        write_check = WorkerReadinessService._check(
            "media_write",
            "输出权限",
            "pass" if not write_errors and not read_errors else "fail",
            "共享目录可创建输出文件" if not write_errors and not read_errors else "共享目录不可写",
            "；".join((read_errors + write_errors)[:3]),
            "controller" if read_errors or write_errors else "",
        )
        return read_check, write_check

    @staticmethod
    def _ffmpeg_check(ffmpeg_bin: str) -> dict[str, str]:
        executable = shutil.which(ffmpeg_bin) or (ffmpeg_bin if Path(ffmpeg_bin).is_file() else "")
        if not executable:
            return WorkerReadinessService._check(
                "ffmpeg", "FFmpeg / NVENC", "fail", "未找到 FFmpeg", "请重新安装完整 Worker。", "reinstall"
            )
        try:
            result = subprocess.run(
                [executable, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=WINDOWS_CREATE_NO_WINDOW,
                check=False,
            )
            output = f"{result.stdout}\n{result.stderr}"
            if result.returncode != 0:
                raise RuntimeError(f"退出代码 {result.returncode}")
            nvenc = [name for name in ("av1_nvenc", "hevc_nvenc") if name in output]
            if not nvenc:
                return WorkerReadinessService._check(
                    "ffmpeg", "FFmpeg / NVENC", "fail", "FFmpeg 缺少 NVIDIA 编码器", action="reinstall"
                )
            return WorkerReadinessService._check(
                "ffmpeg", "FFmpeg / NVENC", "pass", f"可用编码器：{', '.join(nvenc)}"
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return WorkerReadinessService._check(
                "ffmpeg", "FFmpeg / NVENC", "fail", "FFmpeg 检测失败", str(exc), "reinstall"
            )

    @staticmethod
    def _next_action(checks: list[dict[str, str]]) -> dict[str, str]:
        priority = [
            ("gpu", "driver", "检查 NVIDIA 驱动"),
            ("gpu_runtime", "gpu_runtime", "安装 GPU 运行环境"),
            ("ffmpeg", "reinstall", "重新安装 Worker"),
            ("path_map", "controller", "检查控制端路径映射"),
            ("media_read", "controller", "修复媒体目录映射"),
            ("media_write", "controller", "修复共享目录权限"),
            ("model", "models", "下载或修复模型"),
            ("compute", "start", "启动算力"),
            ("controller", "controller", "连接 MovieMuse 控制端"),
        ]
        by_id = {str(item.get("id")): item for item in checks if item.get("status") != "pass"}
        for check_id, action, label in priority:
            if check_id in by_id:
                return {"id": action, "label": label, "check_id": check_id}
        return {"id": "none", "label": "已准备就绪"}

    def scan(
        self,
        *,
        settings: Any,
        compute_enabled: bool,
        controller_synced_at: float,
        gpus: list[dict[str, Any]],
        gpu_runtime: dict[str, Any],
        models: list[dict[str, Any]],
        recommended_model: str,
        ffmpeg_bin: str = "ffmpeg",
    ) -> dict[str, Any]:
        with self._lock:
            self._snapshot = {
                **self._snapshot,
                "status": "checking",
                "summary": "正在检测算力环境",
            }

        checks: list[dict[str, str]] = []
        checks.append(self._check(
            "compute",
            "算力开关",
            "pass" if compute_enabled else "warning",
            "正在接收新任务" if compute_enabled else "算力已关闭",
            action="start" if not compute_enabled else "",
        ))
        checks.append(self._check(
            "controller",
            "控制端配置",
            "pass" if controller_synced_at > 0 else "warning",
            "已接收 MovieMuse 配置" if controller_synced_at > 0 else "尚未收到控制端配置",
            action="controller" if controller_synced_at <= 0 else "",
        ))
        checks.append(self._check(
            "gpu",
            "NVIDIA 显卡",
            "pass" if gpus else "fail",
            str(gpus[0].get("name") or "已检测到 NVIDIA GPU") if gpus else "未检测到 NVIDIA GPU",
            action="driver" if not gpus else "",
        ))
        runtime_status = str(gpu_runtime.get("status") or "missing")
        runtime_ready = runtime_status == "ready"
        checks.append(self._check(
            "gpu_runtime",
            "CUDA / cuDNN",
            "pass" if runtime_ready else "fail",
            "GPU 运行环境可用" if runtime_ready else str(gpu_runtime.get("job", {}).get("message") or "需要安装 GPU 运行环境"),
            action="gpu_runtime" if not runtime_ready else "",
        ))
        checks.append(self._ffmpeg_check(ffmpeg_bin))

        mapped_roots = self._mapped_roots(settings)
        checks.append(self._check(
            "path_map",
            "路径映射",
            "pass" if mapped_roots else "fail",
            f"已配置 {len(mapped_roots)} 个 Windows 映射" if mapped_roots else "控制端路径映射未同步",
            action="controller" if not mapped_roots else "",
        ))
        read_check, write_check = self._media_access_check(mapped_roots)
        checks.extend([read_check, write_check])

        active_model = next((item for item in models if item.get("active")), None)
        model_ready = bool(active_model and active_model.get("installed") and active_model.get("verified"))
        checks.append(self._check(
            "model",
            "Whisper 模型",
            "pass" if model_ready else "fail",
            f"{active_model.get('label') or active_model.get('id')} 已验证" if model_ready else f"当前模型不可用，建议安装 {recommended_model or '推荐模型'}",
            action="models" if not model_ready else "",
        ))

        blocking = [item for item in checks if item["status"] == "fail"]
        warnings = [item for item in checks if item["status"] == "warning"]
        ready = not blocking and compute_enabled
        status = "ready" if ready else "needs_attention"
        if blocking:
            summary = f"需要处理 {len(blocking)} 项后才能接任务"
        elif not compute_enabled:
            summary = "环境正常，启动算力后即可接任务"
        elif warnings:
            summary = f"可以接任务，另有 {len(warnings)} 项提示"
        else:
            summary = "环境正常，可以接收任务"
        snapshot = {
            "status": status,
            "ready": ready,
            "summary": summary,
            "checks": checks,
            "blocking_count": len(blocking),
            "warning_count": len(warnings),
            "checked_at": time.time(),
            "next_action": self._next_action(checks),
        }
        with self._lock:
            self._snapshot = snapshot
        return self.snapshot()
