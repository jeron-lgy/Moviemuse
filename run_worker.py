from __future__ import annotations

import asyncio
import os
import socket

from uvicorn.config import Config
from uvicorn.server import Server


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except OSError:
        pass

    candidates: list[str] = []
    try:
        host_name = socket.gethostname()
        candidates.extend(socket.gethostbyname_ex(host_name)[2])
    except OSError:
        pass
    for ip in candidates:
        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
            return ip
    return "WINDOWS-IP"


def main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = _env_int("PORT", 18181)
    public_host = _lan_ip() if host in {"0.0.0.0", "::"} else host

    print("", flush=True)
    print(f"Listening: http://{host}:{port}", flush=True)
    print(f"LAN URL:   http://{public_host}:{port}", flush=True)
    print(f"Health:    http://{public_host}:{port}/health", flush=True)
    print("", flush=True)

    config = Config("app.main:app", host=host, port=port, log_level="info")
    server = Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
