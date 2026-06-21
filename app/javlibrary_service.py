"""JavLibrary fallback source through FlareSolverr."""

from __future__ import annotations

import random
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.javlibrary.com"
DEFAULT_FLARESOLVERR_URL = "http://127.0.0.1:8281/v1"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 520, 522, 524}
SESSION_TTL_SECONDS = 45 * 60


@dataclass(frozen=True)
class JavLibraryVideo:
    code: str
    title: str
    url: str
    cover: str = ""


class JavLibraryChallenge(RuntimeError):
    pass


class RetryableFetchError(RuntimeError):
    pass


class JavLibraryService:
    def __init__(self) -> None:
        self._logger: Callable[[str, str, str, dict[str, Any] | None], None] | None = None
        self._service_url_provider: Callable[[], str] | None = None
        self._requests_started = 0
        self._requests_failed = 0
        self._last_error = ""
        self._last_success_at = 0.0
        self._session_lock = threading.RLock()
        self._session_service_url = ""
        self._session = ""
        self._session_created_at = 0.0
        self._session_warmed_at = 0.0
        self._session_uses = 0

    def set_logger(self, logger: Callable[[str, str, str, dict[str, Any] | None], None]) -> None:
        self._logger = logger

    def set_service_url_provider(self, provider: Callable[[], str]) -> None:
        self._service_url_provider = provider

    def stats(self) -> dict[str, Any]:
        return {
            "requests_started": self._requests_started,
            "requests_failed": self._requests_failed,
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
            "service_url": self._service_url(),
            "session_active": bool(self._session),
            "session_age_seconds": max(0, int(time.time() - self._session_created_at)) if self._session_created_at else 0,
            "session_warmed_at": self._session_warmed_at,
            "session_uses": self._session_uses,
        }

    def actress_url(self, star_id: str, lang: str = "cn") -> str:
        return f"{BASE_URL}/{lang}/vl_star.php?s={star_id}"

    def get_actor_avs(self, star_id: str, limit: int = 20, *, retries: int = 3, timeout_ms: int = 120000) -> list[dict[str, Any]]:
        star = str(star_id or "").strip()
        if not star:
            return []
        return self.get_listing_avs(self.actress_url(star), limit=limit, retries=retries, timeout_ms=timeout_ms)

    def get_video_actresses(self, av_id: str) -> list[dict[str, str]]:
        code = self.normalize_code(av_id)
        if not code:
            return []
        search_url = f"{BASE_URL}/cn/vl_searchbyid.php?keyword={quote(code)}"
        html = self.fetch_with_flaresolverr(search_url)
        actors = self.parse_video_actresses(html, search_url)
        if actors:
            return actors
        for video in self.parse_videos(html, search_url):
            if video.code != code:
                continue
            detail_html = self.fetch_with_flaresolverr(video.url)
            return self.parse_video_actresses(detail_html, video.url)
        return []

    def get_listing_avs(self, url: str, limit: int = 20, *, retries: int = 3, timeout_ms: int = 120000) -> list[dict[str, Any]]:
        target_url = str(url or "").strip()
        if not target_url:
            return []
        html = self.fetch_with_flaresolverr(target_url, retries=retries, timeout_ms=timeout_ms)
        return [self._video_to_dict(item) for item in self.parse_videos(html, target_url)[: max(1, int(limit or 20))]]

    def fetch_with_flaresolverr(
        self,
        target_url: str,
        *,
        retries: int = 3,
        timeout_ms: int = 120000,
        base_delay: float = 8.0,
        max_delay: float = 90.0,
        cooldown: float = 2.0,
    ) -> str:
        service_url = self._service_url()
        if not service_url:
            raise RuntimeError("JavLibrary FlareSolverr URL is not configured")
        last_error: Exception | None = None
        with self._session_lock:
            for attempt in range(1, retries + 2):
                session = ""
                try:
                    self._requests_started += 1
                    session = self._ensure_session(service_url, timeout_ms)
                    html = self._request(service_url, target_url, session, timeout_ms)
                    if cooldown > 0:
                        time.sleep(cooldown)
                    self._last_success_at = time.time()
                    self._session_uses += 1
                    return html
                except (RetryableFetchError, JavLibraryChallenge, requests.Timeout) as exc:
                    last_error = exc
                    self._last_error = str(exc)
                    self._reset_session(service_url, session, timeout_ms)
                    if attempt > retries:
                        break
                    self._log("warning", "JavLibrary fetch retry", {"stage": "javlibrary_retry", "attempt": attempt, "error": str(exc), "url": target_url})
                    self._backoff(attempt, base_delay, max_delay)
                except Exception as exc:
                    last_error = exc
                    self._last_error = str(exc)
                    self._reset_session(service_url, session, timeout_ms)
                    break
        self._requests_failed += 1
        raise RuntimeError(f"JavLibrary fetch failed: {last_error}") from last_error

    def parse_videos(self, html: str, page_url: str) -> list[JavLibraryVideo]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[JavLibraryVideo] = []
        seen: set[str] = set()
        for video in soup.select("div.video"):
            anchor = video.select_one("a[href*='?v='], a[href*='vl_searchbyid.php?keyword='], a[href$='.html']")
            if not anchor:
                continue
            href = anchor.get("href") or ""
            url = urljoin(page_url, href)
            code_node = video.select_one(".id")
            title_node = video.select_one(".title")
            img_node = video.select_one("img")
            raw_code = code_node.get_text(" ", strip=True) if code_node else ""
            title = title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True)
            code = self.normalize_code(raw_code or title or href)
            if not code or code in seen:
                continue
            seen.add(code)
            cover = self._abs_url(img_node.get("src") or "", page_url) if img_node else ""
            items.append(JavLibraryVideo(code=code, title=title, url=url, cover=cover))
        return items

    def parse_video_actresses(self, html: str, page_url: str) -> list[dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        actors: list[dict[str, str]] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href*='vl_star.php?s=']"):
            href = self._abs_url(anchor.get("href") or "", page_url)
            star_id = (parse_qs(urlparse(href).query).get("s") or [""])[0].strip()
            name = anchor.get_text(" ", strip=True)
            key = star_id or name
            if not key or key in seen:
                continue
            seen.add(key)
            actors.append({
                "id": star_id,
                "star_id": star_id,
                "name": name,
                "url": href,
                "source": "javlibrary",
            })
        return actors

    @staticmethod
    def normalize_code(text: str) -> str:
        match = re.search(r"\b([A-Z]{2,10})[-_ ]?(\d{2,5})\b", str(text or "").upper())
        if not match:
            return ""
        return f"{match.group(1)}-{match.group(2)}"

    def assert_not_challenge(self, html: str, status_code: int) -> None:
        title = BeautifulSoup(html or "", "html.parser").title
        title_text = title.get_text(strip=True).lower() if title else ""
        lower_html = (html or "").lower()
        markers = ("just a moment", "cf-challenge", "challenges.cloudflare.com")
        if status_code in RETRYABLE_STATUS_CODES or status_code in {403} or any(marker in lower_html for marker in markers) or "just a moment" in title_text:
            raise JavLibraryChallenge("JavLibrary returned a challenge page")

    def _video_to_dict(self, item: JavLibraryVideo) -> dict[str, Any]:
        return {
            "id": item.code,
            "code": item.code,
            "title": item.title,
            "url": item.url,
            "cover": item.cover,
            "source": "javlibrary",
        }

    def _service_url(self) -> str:
        if self._service_url_provider:
            try:
                return str(self._service_url_provider() or "").strip().rstrip("/")
            except Exception:
                return ""
        return DEFAULT_FLARESOLVERR_URL

    def _command(self, service_url: str, payload: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        response = requests.post(service_url, json=payload, timeout=(10, timeout_ms / 1000 + 20))
        if response.status_code in RETRYABLE_STATUS_CODES:
            raise RetryableFetchError(f"FlareSolverr API returned HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"FlareSolverr failed: {data.get('message') or data}")
        return data

    def _create_session(self, service_url: str, session: str, timeout_ms: int) -> None:
        self._command(service_url, {"cmd": "sessions.create", "session": session}, timeout_ms)

    def _ensure_session(self, service_url: str, timeout_ms: int) -> str:
        now = time.time()
        if (
            self._session
            and self._session_service_url == service_url
            and now - self._session_created_at < SESSION_TTL_SECONDS
        ):
            return self._session
        if self._session:
            self._destroy_session(self._session_service_url, self._session, timeout_ms)
        session = f"moviemuse-jl-{uuid.uuid4().hex[:10]}"
        started_at = time.time()
        try:
            self._create_session(service_url, session, timeout_ms)
            self._request(service_url, f"{BASE_URL}/cn/", session, timeout_ms)
        except Exception:
            self._destroy_session(service_url, session, timeout_ms)
            raise
        self._session_service_url = service_url
        self._session = session
        self._session_created_at = started_at
        self._session_warmed_at = time.time()
        self._session_uses = 0
        self._log("info", "JavLibrary FlareSolverr session warmed", {
            "stage": "javlibrary_session_warmed",
            "session": session,
            "elapsed": round(self._session_warmed_at - started_at, 3),
        })
        return session

    def _reset_session(self, service_url: str, session: str, timeout_ms: int) -> None:
        target_session = session or self._session
        target_service_url = service_url or self._session_service_url
        if target_session:
            self._destroy_session(target_service_url, target_session, timeout_ms)
        if not session or session == self._session:
            self._session_service_url = ""
            self._session = ""
            self._session_created_at = 0.0
            self._session_warmed_at = 0.0
            self._session_uses = 0

    def _destroy_session(self, service_url: str, session: str, timeout_ms: int) -> None:
        if not service_url or not session:
            return
        try:
            self._command(service_url, {"cmd": "sessions.destroy", "session": session}, timeout_ms)
        except Exception:
            pass

    def _request(self, service_url: str, url: str, session: str, timeout_ms: int) -> str:
        data = self._command(
            service_url,
            {"cmd": "request.get", "url": url, "session": session, "maxTimeout": timeout_ms},
            timeout_ms,
        )
        solution = data.get("solution") or {}
        status_code = int(solution.get("status") or 0)
        html = solution.get("response") or ""
        if status_code in RETRYABLE_STATUS_CODES:
            raise RetryableFetchError(f"Target returned HTTP {status_code}")
        if not html:
            raise RetryableFetchError(f"FlareSolverr returned no HTML. Solution keys: {sorted(solution)}")
        self.assert_not_challenge(html, status_code)
        return html

    @staticmethod
    def _backoff(attempt: int, base_delay: float, max_delay: float) -> None:
        delay = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
        time.sleep(delay + random.uniform(0, delay * 0.35))

    @staticmethod
    def _abs_url(value: str, page_url: str) -> str:
        url = str(value or "").strip()
        if not url:
            return ""
        if url.startswith("//"):
            return f"https:{url}"
        return urljoin(page_url, url)

    def _log(self, level: str, message: str, data: dict[str, Any] | None = None) -> None:
        payload = data or {}
        if self._logger:
            try:
                self._logger(level, "javlibrary", message, payload)
                return
            except Exception:
                pass
        print(f"[JavLibrary] {level}: {message} {payload}", flush=True)


javlibrary = JavLibraryService()
