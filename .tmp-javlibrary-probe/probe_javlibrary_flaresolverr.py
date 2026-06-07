"""
Probe JavLibrary via a local FlareSolverr service.

Default service URL for this workspace probe:
    http://127.0.0.1:8281/v1
"""

from __future__ import annotations

import argparse
import importlib.util
import random
import sys
import time
import uuid
from pathlib import Path

import requests


BASE_URL = "https://www.javlibrary.com"
DEFAULT_FLARESOLVERR_URL = "http://127.0.0.1:8281/v1"
RETRYABLE_STATUS_CODES = {429, 520, 522, 524}


class RetryableFetchError(RuntimeError):
    pass


def load_parser_module():
    parser_path = Path(__file__).resolve().parent / "probe_javlibrary.py"
    spec = importlib.util.spec_from_file_location("probe_javlibrary", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load parser module: {parser_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def actress_url(star_id: str, lang: str = "cn") -> str:
    return f"{BASE_URL}/{lang}/vl_star.php?s={star_id}"


def flaresolverr_command(service_url: str, payload: dict, timeout_ms: int) -> dict:
    response = requests.post(service_url, json=payload, timeout=(10, timeout_ms / 1000 + 20))
    if response.status_code in RETRYABLE_STATUS_CODES:
        raise RetryableFetchError(f"FlareSolverr API returned HTTP {response.status_code}")
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr failed: {data.get('message') or data}")
    return data


def backoff_sleep(attempt: int, base_delay: float, max_delay: float) -> None:
    delay = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    jitter = random.uniform(0, delay * 0.35)
    time.sleep(delay + jitter)


def fetch_once(
    service_url: str,
    target_url: str,
    timeout_ms: int,
    session: str,
    warmup_url: str | None,
    parser_module,
) -> str:
    flaresolverr_command(
        service_url,
        {"cmd": "sessions.create", "session": session},
        timeout_ms,
    )

    if warmup_url:
        flaresolverr_command(
            service_url,
            {
                "cmd": "request.get",
                "url": warmup_url,
                "session": session,
                "maxTimeout": timeout_ms,
            },
            timeout_ms,
        )

    payload = {
        "cmd": "request.get",
        "url": target_url,
        "maxTimeout": timeout_ms,
        "session": session,
    }
    data = flaresolverr_command(service_url, payload, timeout_ms)

    solution = data.get("solution") or {}
    status_code = int(solution.get("status") or 0)
    html = solution.get("response") or ""
    if status_code in RETRYABLE_STATUS_CODES:
        raise RetryableFetchError(f"Target returned HTTP {status_code}")
    if not html:
        raise RetryableFetchError(f"FlareSolverr returned no HTML. Solution keys: {sorted(solution)}")

    parser_module.assert_not_challenge(html, status_code)
    return html


def fetch_with_flaresolverr(
    service_url: str,
    target_url: str,
    timeout_ms: int,
    session: str | None,
    warmup_url: str | None,
    retries: int,
    base_delay: float,
    max_delay: float,
    cooldown: float,
) -> str:
    parser_module = load_parser_module()
    owned_session = session is None
    last_error: Exception | None = None

    for attempt in range(1, retries + 2):
        current_session = session or f"jlprobe-{uuid.uuid4().hex[:10]}"
        try:
            html = fetch_once(
                service_url,
                target_url,
                timeout_ms,
                current_session,
                warmup_url,
                parser_module,
            )
            if cooldown > 0:
                time.sleep(cooldown)
            return html
        except (RetryableFetchError, parser_module.ChallengePage, requests.Timeout) as exc:
            last_error = exc
            if attempt > retries:
                break
            print(
                f"retryable fetch issue on attempt {attempt}: {exc}; rotating session and backing off",
                file=sys.stderr,
            )
            backoff_sleep(attempt, base_delay, max_delay)
        finally:
            if owned_session:
                try:
                    flaresolverr_command(
                        service_url,
                        {"cmd": "sessions.destroy", "session": current_session},
                        timeout_ms,
                    )
                except Exception:
                    pass

    raise RuntimeError(f"Fetch failed after {retries + 1} attempts: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--star-id", help="JavLibrary star id from vl_star.php?s=<id>")
    parser.add_argument("--url", help="Full JavLibrary actress page URL")
    parser.add_argument("--lang", default="cn")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--service-url", default=DEFAULT_FLARESOLVERR_URL)
    parser.add_argument("--timeout-ms", type=int, default=120000)
    parser.add_argument("--session", help="Reuse an existing FlareSolverr session id")
    parser.add_argument("--warmup-url", default=f"{BASE_URL}/cn/", help="URL to request before the target page")
    parser.add_argument("--retries", type=int, default=3, help="Retries for 429/520/challenge responses")
    parser.add_argument("--base-delay", type=float, default=8.0, help="Initial retry delay in seconds")
    parser.add_argument("--max-delay", type=float, default=90.0, help="Maximum retry delay in seconds")
    parser.add_argument("--cooldown", type=float, default=2.0, help="Delay after a successful request")
    args = parser.parse_args()

    if not args.url and not args.star_id:
        parser.error("pass --url or --star-id")

    target_url = args.url or actress_url(args.star_id, args.lang)
    parser_module = load_parser_module()

    html = fetch_with_flaresolverr(
        args.service_url,
        target_url,
        args.timeout_ms,
        args.session,
        args.warmup_url,
        args.retries,
        args.base_delay,
        args.max_delay,
        args.cooldown,
    )
    videos = parser_module.parse_videos(html, target_url)

    if not videos:
        print("No videos parsed from FlareSolverr response.", file=sys.stderr)
        return 2

    for item in videos[: args.limit]:
        print(f"{item.code}\t{item.title}\t{item.url}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
