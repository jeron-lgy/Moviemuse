"""DMM/FANZA preorder data source."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


class DMMService:
    BASE_URL = "https://www.dmm.co.jp"

    def __init__(self) -> None:
        self._logger: Callable[[str, str, str, dict[str, Any] | None], None] | None = None
        self._proxy_provider: Callable[[], str] | None = None
        cache_root = os.getenv("DMM_CACHE_DIR") or str(Path(os.getenv("APP_DATA_DIR", "data")) / "dmm-cache")
        self._cache_dir = Path(cache_root)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._search_cache_ttl = int(os.getenv("DMM_SEARCH_CACHE_TTL_SECONDS", "21600"))
        self._detail_cache_ttl = int(os.getenv("DMM_DETAIL_CACHE_TTL_SECONDS", "2592000"))
        self._timeout = int(os.getenv("DMM_REQUEST_TIMEOUT_SECONDS", "30"))
        self._last_error = ""
        self._requests_started = 0
        self._requests_failed = 0
        self._cache_hits = 0
        self._cache_misses = 0

    def set_logger(self, logger: Callable[[str, str, str, dict[str, Any] | None], None]) -> None:
        self._logger = logger

    def set_proxy_provider(self, provider: Callable[[], str]) -> None:
        self._proxy_provider = provider

    def _proxy_url(self) -> str:
        if self._proxy_provider:
            try:
                return str(self._proxy_provider() or "").strip()
            except Exception:
                return ""
        return str(os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or "").strip()

    def _log(self, level: str, message: str, data: dict[str, Any] | None = None) -> None:
        payload = data or {}
        if self._logger:
            try:
                self._logger(level, "dmm", message, payload)
                return
            except Exception:
                pass
        print(f"[DMM] {level}: {message} {payload}", flush=True)

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _cached(self, key: str, fetch: Callable[[], Any], ttl: int) -> Any:
        now = time.time()
        path = self._cache_path(key)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if float(payload.get("expires_at") or 0) > now:
                    self._cache_hits += 1
                    return payload.get("value")
            except Exception:
                pass
        self._cache_misses += 1
        value = fetch()
        if value:
            try:
                path.write_text(
                    json.dumps({"key": key, "expires_at": now + ttl, "value": value}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                self._log("warning", "DMM cache write failed", {"stage": "dmm_cache_write_error", "key": key, "error": str(exc)})
        return value

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
            }
        )
        proxy_url = self._proxy_url()
        if proxy_url:
            session.proxies.update({"http": proxy_url, "https": proxy_url})
        return session

    def _get(self, session: requests.Session, url: str) -> requests.Response:
        self._requests_started += 1
        self._log("info", "DMM fetch start", {"stage": "dmm_fetch_start", "url": url})
        try:
            response = session.get(url, timeout=self._timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            self._requests_failed += 1
            self._last_error = str(exc)
            self._log("error", "DMM fetch failed", {"stage": "dmm_fetch_error", "url": url, "error": str(exc)})
            raise

    def _pass_age_gate(self, session: requests.Session) -> None:
        rurl = quote(f"{self.BASE_URL}/", safe="")
        try:
            self._get(session, f"{self.BASE_URL}/age_check/=/declared=yes/?rurl={rurl}")
        except Exception:
            pass

    @staticmethod
    def _compact(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _normalize_date(value: str) -> str:
        match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", value or "")
        if not match:
            return ""
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _cid_from_url(url: str) -> str:
        match = re.search(r"/cid=([^/?]+)/?", html.unescape(url or ""))
        return match.group(1) if match else ""

    @staticmethod
    def _abs_url(value: str) -> str:
        url = html.unescape(str(value or "").strip())
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return f"{DMMService.BASE_URL}{url}"
        return url

    @staticmethod
    def normalize_av_id_from_cid(cid: str) -> str:
        raw = str(cid or "").lower().strip()
        raw = re.sub(r"(?:bod|ec|r)$", "", raw)
        raw = re.sub(r"^\d+", "", raw)
        for prefix in ("ftkt", "ztkt", "tkt", "tk"):
            if raw.startswith(prefix) and re.fullmatch(rf"{prefix}[a-z]{{2,}}\d{{2,}}", raw):
                raw = raw[len(prefix) :]
                break
        match = re.search(r"([a-z]{2,})(\d{2,})$", raw)
        if not match:
            return raw.upper()
        return f"{match.group(1).upper()}-{match.group(2)}"

    @classmethod
    def _clean_actor_names(cls, value: str) -> list[dict[str, str]]:
        text = re.sub(r"\u25b6.*$", "", value or "")
        text = text.replace("\u30b5\u30f3\u30d7\u30eb\u518d\u751f", "")
        parts = [cls._compact(item) for item in re.split(r"\s+\|\s+|\u3001|,|\s{2,}", text) if cls._compact(item)]
        if len(parts) <= 1 and cls._compact(text):
            space_parts = [cls._compact(item) for item in re.split(r"\s+", cls._compact(text)) if cls._compact(item)]
            if 2 <= len(space_parts) <= 80 and all(1 <= len(item) <= 32 for item in space_parts):
                parts = space_parts
            else:
                parts = [cls._compact(text)]
        return [{"name": item} for item in parts]

    @classmethod
    def _parse_search_hits(cls, content: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(content, "html.parser")
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        for img in soup.select('img[src*="pics.dmm.co.jp"]'):
            card = img.find_parent("div")
            for _ in range(7):
                if card and card.find("a", href=re.compile(r"/detail/=/cid=")) and card.find("p"):
                    break
                card = card.parent if card else None
            if not card:
                continue
            link = card.find("a", href=re.compile(r"/detail/=/cid="))
            if not link:
                continue
            url = cls._abs_url(link.get("href") or "")
            if "/mono/dvd/" not in url and "/rental/ppr/" not in url:
                continue
            if url in seen:
                continue
            seen.add(url)
            text = cls._compact(card.get_text(" ", strip=True))
            title = cls._compact(str(img.get("alt") or "").replace("\u3010\u4e88\u7d04\u3011", ""))
            if not title:
                title_candidates = [
                    cls._compact(node.get_text(" ", strip=True)).replace("\u3010\u4e88\u7d04\u3011", "")
                    for node in card.find_all("p")
                ]
                title_candidates = [
                    value
                    for value in title_candidates
                    if value and value not in {"DVD", "\u4e88\u7d04", "\u8fd1\u65e5"} and not re.search(r"\d[\d,]*\u5186", value)
                ]
                title = max(title_candidates, key=len, default="")
            release = ""
            match = re.search(r"(?:\u767a\u58f2\u65e5|\u8cb8\u51fa\u65e5)\s*[:\uff1a]?\s*(\d{4}/\d{1,2}/\d{1,2})", text)
            if match:
                release = cls._normalize_date(match.group(1))
            actors: list[dict[str, str]] = []
            actor_match = re.search(r"\u51fa\u6f14\u8005\s*[:\uff1a]\s*(.+?)(?:\s+\d[\d,]*\u5186|\s+\u25b6|$)", text)
            if actor_match:
                actors = cls._clean_actor_names(actor_match.group(1))
            cid = cls._cid_from_url(url)
            av_id = cls.normalize_av_id_from_cid(cid)
            status = "preorder" if "\u4e88\u7d04" in text else ("coming_soon" if "\u8fd1\u65e5" in text else "")
            hits.append(
                {
                    "id": av_id,
                    "title": title,
                    "cover": cls._abs_url(img.get("src") or ""),
                    "date": release,
                    "release_date": release,
                    "actresses": actors,
                    "url": url,
                    "source": "dmm",
                    "source_status": status,
                    "cid": cid,
                    "_dmm_text": text,
                }
            )
        return hits

    @staticmethod
    def _variant_score(item: dict[str, Any]) -> tuple[int, int, int]:
        text = str(item.get("_dmm_text") or "")
        url = str(item.get("url") or "")
        cid = str(item.get("cid") or "").lower()
        limited = (
            "\u9650\u5b9a" in text
            or "FANZA\u9650\u5b9a" in text
            or cid.startswith(("tk", "tks", "tkt", "ftkt", "ztkt"))
            or cid.endswith("bod")
        )
        normal = 1 if "/mono/dvd/" in url and not limited else 0
        preorder = 1 if item.get("source_status") == "preorder" else 0
        has_cover = 1 if item.get("cover") else 0
        return normal, preorder, has_cover

    @classmethod
    def _dedupe_hits(cls, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for item in hits:
            av_id = str(item.get("id") or "").strip().upper()
            if not av_id:
                continue
            current = grouped.get(av_id)
            if current is None or cls._variant_score(item) > cls._variant_score(current):
                grouped[av_id] = item
        return sorted(
            grouped.values(),
            key=lambda item: (str(item.get("date") or ""), 1 if item.get("source_status") in {"preorder", "coming_soon"} else 0),
            reverse=True,
        )

    @staticmethod
    def _page_lines(soup: BeautifulSoup) -> list[str]:
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        return [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]

    @classmethod
    def _field(cls, lines: list[str], label: str, stop_labels: tuple[str, ...]) -> str:
        for index, line in enumerate(lines):
            if line in {label, f"{label}:", f"{label}\uff1a"}:
                pass
            elif line.startswith(f"{label}:") or line.startswith(f"{label}\uff1a"):
                return cls._compact(line.split(":", 1)[-1] if ":" in line else line.split("\uff1a", 1)[-1])
            else:
                continue
            values: list[str] = []
            for item in lines[index + 1 :]:
                if any(item in {stop, f"{stop}:", f"{stop}\uff1a"} or item.startswith(f"{stop}:") or item.startswith(f"{stop}\uff1a") for stop in stop_labels):
                    break
                if item in {"----", "-", "\u95a2\u9023\u5546\u54c1\uff1a", "\u30e1\u30c7\u30a3\u30a2", "DVD"}:
                    break
                values.append(item)
            return cls._compact(" ".join(values))
        return ""

    @classmethod
    def _detail_people_links(cls, soup: BeautifulSoup, article: str) -> list[dict[str, str]]:
        people: list[dict[str, str]] = []
        seen: set[str] = set()
        for link in soup.find_all("a", href=re.compile(rf"article={re.escape(article)}/id=")):
            name = cls._compact(link.get_text(" ", strip=True))
            url = cls._abs_url(link.get("href") or "")
            key = (url or name).lower()
            if not name or not key or key in seen:
                continue
            seen.add(key)
            people.append({"name": name, "url": cls._dated_list_url(url), "source": "dmm", "dmm_name": name})
        return people

    @staticmethod
    def _dated_list_url(url: str) -> str:
        value = DMMService._abs_url(url)
        if "/list/=" not in value or "sort=" in value:
            return value
        return value.rstrip("/") + "/sort=date/"

    def _detail_sync(self, item: dict[str, Any]) -> dict[str, Any]:
        url = str(item.get("url") or "").split("?")[0]
        if not url:
            return item
        session = self._session()
        self._pass_age_gate(session)
        response = self._get(session, url)
        soup = BeautifulSoup(response.text, "html.parser")
        title_node = soup.find("h1")
        title = self._compact(title_node.get_text(" ", strip=True) if title_node else item.get("title", ""))
        cover_node = soup.select_one('img[src*="pics.dmm.co.jp"][src$="pl.jpg"]') or soup.select_one('img[src*="pics.dmm.co.jp"]')
        lines = self._page_lines(soup)
        label_release = "\u767a\u58f2\u65e5"
        label_rental = "\u8cb8\u51fa\u65e5"
        label_duration = "\u53ce\u9332\u6642\u9593"
        label_actors = "\u51fa\u6f14\u8005"
        label_director = "\u76e3\u7763"
        label_series = "\u30b7\u30ea\u30fc\u30ba"
        label_maker = "\u30e1\u30fc\u30ab\u30fc"
        label_label = "\u30ec\u30fc\u30d9\u30eb"
        label_genre = "\u30b8\u30e3\u30f3\u30eb"
        label_code = "\u54c1\u756a"
        label_related = "\u95a2\u9023\u5546\u54c1"
        label_media = "\u30e1\u30c7\u30a3\u30a2"
        raw_cid = self._field(lines, label_code, (label_related, label_media)) or str(item.get("cid") or "")
        actor_links = self._detail_people_links(soup, "actress")
        actors = self._field(lines, label_actors, (label_director, label_series, label_maker))
        genres = self._field(lines, label_genre, (label_code, label_related, label_media))
        release = self._normalize_date(self._field(lines, label_release, (label_rental,))) or self._normalize_date(self._field(lines, label_rental, (label_duration,)))
        detail = {
            **item,
            "id": str(item.get("id") or self.normalize_av_id_from_cid(raw_cid)).upper(),
            "title": title or item.get("title", ""),
            "cover": self._abs_url(cover_node.get("src") if cover_node else item.get("cover", "")),
            "cover_thumb": item.get("cover", ""),
            "date": release or item.get("date", ""),
            "release_date": release or item.get("release_date", ""),
            "duration": self._field(lines, label_duration, (label_actors,)),
            "actresses": actor_links or self._clean_actor_names(actors) or item.get("actresses", []),
            "director": self._field(lines, label_director, (label_series,)),
            "maker": self._field(lines, label_maker, (label_label,)),
            "label": self._field(lines, label_label, (label_genre,)),
            "tags": [{"name": name} for name in re.split(r"\s+", genres) if name],
            "cid": raw_cid,
            "url": response.url,
            "source": "dmm",
        }
        detail["detail"] = {key: value for key, value in detail.items() if not key.startswith("_")}
        return detail

    def get_av_detail(self, item_or_url: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(item_or_url, dict):
            item = dict(item_or_url)
        else:
            url = str(item_or_url or "")
            item = {
                "id": self.normalize_av_id_from_cid(self._cid_from_url(url)),
                "url": url,
                "source": "dmm",
            }
        key = f"dmm_detail:v7:{item.get('url') or item.get('id')}"
        result = self._cached(key, lambda: self._detail_sync(item), self._detail_cache_ttl)
        return result if isinstance(result, dict) else {}

    def search_av(self, keyword: str, limit: int = 40, *, include_detail: bool = False) -> list[dict[str, Any]]:
        query = str(keyword or "").strip()
        if not query:
            return []

        def fetch() -> list[dict[str, Any]]:
            session = self._session()
            self._pass_age_gate(session)
            hits: list[dict[str, Any]] = []
            for url in (
                f"{self.BASE_URL}/mono/dvd/-/search/=/searchstr={quote(query)}/sort=date/",
                f"{self.BASE_URL}/search/=/searchstr={quote(query)}/",
            ):
                response = self._get(session, url)
                hits.extend(self._parse_search_hits(response.text))
            return self._dedupe_hits(hits)

        results = self._cached(f"dmm_search_av:v8:{query.lower()}", fetch, self._search_cache_ttl)
        items = (results if isinstance(results, list) else [])[:limit]
        if include_detail:
            return [self.get_av_detail(item) for item in items]
        return [{key: value for key, value in item.items() if not key.startswith("_")} for item in items]

    def get_actress_avs(self, actress_name: str, limit: int = 100, *, include_detail: bool = False) -> list[dict[str, Any]]:
        query = str(actress_name or "").strip()
        if not query:
            return []

        def fetch() -> list[dict[str, Any]]:
            session = self._session()
            self._pass_age_gate(session)
            hits: list[dict[str, Any]] = []
            for url in (
                f"{self.BASE_URL}/mono/dvd/-/search/=/searchstr={quote(query)}/sort=date/",
                f"{self.BASE_URL}/search/=/searchstr={quote(query)}/sort=date/",
            ):
                response = self._get(session, url)
                hits.extend(self._parse_search_hits(response.text))
            return self._dedupe_hits(hits)

        results = self._cached(f"dmm_actress_avs:v8:{query.lower()}", fetch, self._search_cache_ttl)
        items = (results if isinstance(results, list) else [])[:limit]
        if include_detail:
            return [self.get_av_detail(item) for item in items]
        return [{key: value for key, value in item.items() if not key.startswith("_")} for item in items]

    def get_maker_avs(self, maker_name: str, limit: int = 60, *, include_detail: bool = False) -> list[dict[str, Any]]:
        query = str(maker_name or "").strip()
        if not query:
            return []

        def fetch() -> list[dict[str, Any]]:
            session = self._session()
            self._pass_age_gate(session)
            hits: list[dict[str, Any]] = []
            for url in (
                f"{self.BASE_URL}/mono/dvd/-/search/=/searchstr={quote(query)}/sort=date/",
                f"{self.BASE_URL}/search/=/searchstr={quote(query)}/sort=date/",
            ):
                response = self._get(session, url)
                hits.extend(self._parse_search_hits(response.text))
            return self._dedupe_hits(hits)

        results = self._cached(f"dmm_maker_avs:v5:{query.lower()}", fetch, self._search_cache_ttl)
        items = (results if isinstance(results, list) else [])[:limit]
        if include_detail:
            return [self.get_av_detail(item) for item in items]
        return [{key: value for key, value in item.items() if not key.startswith("_")} for item in items]

    def get_listing_avs(self, page_url: str, limit: int = 60, *, include_detail: bool = False) -> list[dict[str, Any]]:
        url = self._dated_list_url(page_url)
        if not url:
            return []

        def fetch() -> list[dict[str, Any]]:
            session = self._session()
            self._pass_age_gate(session)
            response = self._get(session, url)
            return self._dedupe_hits(self._parse_search_hits(response.text))

        results = self._cached(f"dmm_listing_avs:v3:{url}", fetch, self._search_cache_ttl)
        items = (results if isinstance(results, list) else [])[:limit]
        if include_detail:
            return [self.get_av_detail(item) for item in items]
        return [{key: value for key, value in item.items() if not key.startswith("_")} for item in items]

    def stats(self) -> dict[str, Any]:
        try:
            disk_cache_size = len(list(self._cache_dir.glob("*.json")))
        except Exception:
            disk_cache_size = 0
        return {
            "disk_cache_size": disk_cache_size,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "requests_started": self._requests_started,
            "requests_failed": self._requests_failed,
            "last_error": self._last_error,
        }


dmm = DMMService()
