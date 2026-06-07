"""javdb 数据源 - Playwright 抓取，复用浏览器"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError, sync_playwright


class JavDBService:
    """javdb 数据源。

    Playwright sync API 不能安全地跨线程复用。FastAPI 的同步路由会在线程池里执行，
    所以这里用一个专门的抓取线程持有 Playwright，所有请求串行投递给它。
    """

    BASE_URL = "https://javdb.com"

    def __init__(self):
        self._pw = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._age_passed = False
        self._logger: Callable[[str, str, str, dict[str, Any] | None], None] | None = None
        self._proxy_provider: Callable[[], str] | None = None
        self._ctx_proxy = ""
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.RLock()
        cache_root = os.getenv("JAVDB_CACHE_DIR") or str(Path(os.getenv("APP_DATA_DIR", "data")) / "javdb-cache")
        self._cache_dir = Path(cache_root)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._default_cache_ttl = int(os.getenv("JAVDB_CACHE_TTL_SECONDS", "1800"))
        self._search_cache_ttl = int(os.getenv("JAVDB_SEARCH_CACHE_TTL_SECONDS", "900"))
        self._listing_cache_ttl = int(os.getenv("JAVDB_LISTING_CACHE_TTL_SECONDS", "43200"))
        self._detail_cache_ttl = int(os.getenv("JAVDB_DETAIL_CACHE_TTL_SECONDS", "2592000"))
        self._actress_cache_ttl = int(os.getenv("JAVDB_ACTRESS_CACHE_TTL_SECONDS", "21600"))
        self._min_interval = float(os.getenv("JAVDB_MIN_INTERVAL_SECONDS", "0.5"))
        self._request_timeout_ms = int(os.getenv("JAVDB_REQUEST_TIMEOUT_MS", "22000"))
        self._run_timeout = int(os.getenv("JAVDB_RUN_TIMEOUT_SECONDS", "70"))
        self._max_context_uses = int(os.getenv("JAVDB_CONTEXT_MAX_USES", "8"))
        self._max_context_age = int(os.getenv("JAVDB_CONTEXT_MAX_AGE_SECONDS", "900"))
        self._last_request_at = 0.0
        self._ctx_created_at = 0.0
        self._ctx_uses = 0
        self._rebuild_requested = False
        self._last_error = ""
        self._requests_started = 0
        self._requests_failed = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._disk_cache_hits = 0
        self._tasks: queue.Queue[tuple[Callable[[], Any], Future[Any]] | None] = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, name="javdb-worker", daemon=True)
        self._worker.start()

    def set_logger(self, logger: Callable[[str, str, str, dict[str, Any] | None], None]) -> None:
        self._logger = logger

    def set_proxy_provider(self, provider: Callable[[], str]) -> None:
        self._proxy_provider = provider
        self._rebuild_requested = True

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
                self._logger(level, "javdb", message, payload)
                return
            except Exception:
                pass
        print(f"[JavDB] {level}: {message} {payload}", flush=True)

    def _worker_loop(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                break
            fn, future = task
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except Exception as exc:
                    future.set_exception(exc)
        self._close_browser()

    def _run(self, fn: Callable[[], Any], timeout: int | None = None) -> Any:
        future: Future[Any] = Future()
        self._tasks.put((fn, future))
        try:
            return future.result(timeout=timeout or self._run_timeout)
        except FutureTimeoutError:
            self._rebuild_requested = True
            self._last_error = "worker timeout"
            self._log("error", "JavDB 抓取任务超时，已标记重建浏览器", {
                "stage": "javdb_worker_timeout",
                "queue_size": self._tasks.qsize(),
                "timeout": timeout or self._run_timeout,
            })
            raise

    def _ensure_ctx(self) -> BrowserContext:
        proxy_url = self._proxy_url()
        # 如果 context 还能用就复用。
        if self._ctx and not self._rebuild_requested:
            try:
                _ = self._ctx.pages
                age = time.monotonic() - self._ctx_created_at
                if self._ctx_proxy == proxy_url and self._ctx_uses < self._max_context_uses and age < self._max_context_age:
                    return self._ctx
                self._log("info", "JavDB 浏览器上下文达到回收条件", {
                    "stage": "javdb_context_recycle",
                    "uses": self._ctx_uses,
                    "age_seconds": round(age, 1),
                })
            except PlaywrightError:
                self._ctx = None

        self._close_browser()

        self._pw = sync_playwright().start()
        launch_args: dict[str, Any] = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        chromium_path = os.getenv("JAVDB_CHROMIUM_PATH")
        if chromium_path:
            launch_args["executable_path"] = chromium_path
        if proxy_url:
            launch_args["proxy"] = {"server": proxy_url}
        self._browser = self._pw.chromium.launch(**launch_args)
        self._ctx = self._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="zh-CN",
            viewport={"width": 1365, "height": 900},
        )
        self._age_passed = False
        self._ctx_created_at = time.monotonic()
        self._ctx_uses = 0
        self._ctx_proxy = proxy_url
        self._rebuild_requested = False
        self._log("info", "JavDB Playwright 浏览器已启动", {"stage": "javdb_browser_start"})
        return self._ctx

    def _close_browser(self) -> None:
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._age_passed = False

    def _do_search_once(self, url: str, js_code: str, timeout: int | None = None) -> Any:
        timeout = timeout or self._request_timeout_ms
        page = self._ensure_ctx().new_page()
        self._ctx_uses += 1
        self._requests_started += 1
        try:
            page.set_default_timeout(timeout)
            page.set_default_navigation_timeout(timeout)
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request_at = time.monotonic()
            started = time.monotonic()
            self._log("info", "JavDB 开始抓取页面", {
                "stage": "javdb_fetch_start",
                "url": url,
                "queue_size": self._tasks.qsize(),
                "context_uses": self._ctx_uses,
            })
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            try:
                body_text = page.locator("body").inner_text(timeout=1000)
            except Exception:
                body_text = ""
            if "banned your access" in body_text or "管理員禁止了你的訪問" in body_text or "異常行為" in body_text:
                raise RuntimeError("JavDB 已触发访问限制，请暂停抓取或更换代理后再试")

            if not self._age_passed:
                self._pass_age_gate(page, timeout)

            try:
                page.wait_for_selector(
                    '.movie-list .item, .movie-list a.box, a[href*="/actors/"], .empty-message, .message',
                    timeout=3000,
                )
                page.wait_for_timeout(300)
            except Exception:
                page.wait_for_timeout(300)
            result = page.evaluate(js_code)
            if isinstance(result, list):
                result_count = len(result)
            elif isinstance(result, dict):
                result_count = len(result)
            else:
                result_count = None
            self._log("info", "JavDB 页面解析完成", {
                "stage": "javdb_fetch_done",
                "url": url,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "result_count": result_count,
            })
            return result
        except Exception as exc:
            self._requests_failed += 1
            self._last_error = str(exc)
            self._log("error", "JavDB 页面抓取失败", {
                "stage": "javdb_fetch_error",
                "url": url,
                "error": str(exc),
            })
            raise
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _pass_age_gate(self, page: Any, timeout: int) -> None:
        for selector in (
            'button:has-text("已滿18")',
            'a:has-text("已滿18")',
            'button:has-text("已满18")',
            'a:has-text("已满18")',
            'button:has-text("Yes, I am")',
            'a:has-text("Yes, I am")',
        ):
            try:
                btn = page.locator(selector)
                if btn.count() <= 0:
                    continue
                btn.first.click(timeout=3000)
                page.wait_for_load_state("domcontentloaded", timeout=timeout)
                self._age_passed = True
                self._log("info", "JavDB 年龄确认已通过", {"stage": "javdb_age_passed"})
                return
            except Exception:
                continue

    def _do_search(self, url: str, js_code: str) -> Any:
        try:
            return self._do_search_once(url, js_code)
        except Exception as exc:
            if is_access_ban_error(exc):
                self._last_error = str(exc)
                self._log("error", "JavDB access is currently limited; skip retry", {
                    "stage": "javdb_access_banned",
                    "url": url,
                    "error": str(exc),
                })
                return None
            self._log("warning", "JavDB 首次抓取失败，重建 Playwright 后重试", {
                "stage": "javdb_retry_rebuild",
                "url": url,
                "error": str(exc),
            })
            self._close_browser()
            try:
                return self._do_search_once(url, js_code)
            except Exception as retry_exc:
                self._last_error = str(retry_exc)
                self._log("error", "JavDB 重试后仍失败", {
                    "stage": "javdb_retry_failed",
                    "url": url,
                    "error": str(retry_exc),
                })
                return None

    def _fetch_list_resilient(self, key: str, url: str, js_code: str, ttl: int, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        if force_refresh:
            with self._cache_lock:
                self._cache.pop(key, None)
            self._cache_path(key).unlink(missing_ok=True)

        def fetch() -> Any:
            result = self._do_search(url, js_code)
            if isinstance(result, list) and result:
                return result
            if is_access_ban_error(self._last_error):
                return result
            self._log("warning", "JavDB 列表解析为空，重建 Playwright 后重试", {
                "stage": "javdb_empty_retry",
                "key": key,
                "url": url,
            })
            self._close_browser()
            retry = self._do_search(url, js_code)
            if isinstance(retry, list) and retry:
                return retry
            self._log("warning", "JavDB 列表重试后仍为空", {
                "stage": "javdb_empty_after_retry",
                "key": key,
                "url": url,
            })
            return retry

        result = self._cached(key, fetch, ttl)
        return result if isinstance(result, list) else []

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _read_disk_cache(self, key: str, now: float) -> tuple[float, Any] | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expires_at = float(payload.get("expires_at") or 0)
            value = payload.get("value")
        except Exception:
            return None
        if value is None:
            return None
        if expires_at > now:
            self._disk_cache_hits += 1
            self._log("info", "JavDB disk cache hit", {"stage": "javdb_disk_cache_hit", "key": key})
        return expires_at, value

    def _write_disk_cache(self, key: str, expires_at: float, value: Any) -> None:
        try:
            path = self._cache_path(key)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"key": key, "expires_at": expires_at, "value": value}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:
            self._log("warning", "JavDB disk cache write failed", {"stage": "javdb_disk_cache_write_error", "key": key, "error": str(exc)})

    def _cached(self, key: str, fetch: Callable[[], Any], ttl: int | None = None) -> Any:
        now = time.time()
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached and cached[0] > now:
            self._cache_hits += 1
            self._log("info", "JavDB 缓存命中", {"stage": "javdb_cache_hit", "key": key})
            return cached[1]

        disk_cached = self._read_disk_cache(key, now)
        if disk_cached and disk_cached[0] > now:
            with self._cache_lock:
                self._cache[key] = disk_cached
            return disk_cached[1]

        self._cache_misses += 1
        self._log("info", "JavDB 缓存未命中，准备抓取", {"stage": "javdb_cache_miss", "key": key})
        result = fetch()
        if result:
            expires_at = now + (ttl or self._default_cache_ttl)
            with self._cache_lock:
                self._cache[key] = (expires_at, result)
            self._write_disk_cache(key, expires_at, result)
            self._log("info", "JavDB 写入缓存", {"stage": "javdb_cache_store", "key": key, "ttl": ttl or self._default_cache_ttl})
            return result
        if cached:
            self._log("warning", "JavDB 抓取失败，返回过期缓存", {"stage": "javdb_cache_stale", "key": key})
            return cached[1]
        if disk_cached:
            self._log("warning", "JavDB 抓取失败，返回过期磁盘缓存", {"stage": "javdb_disk_cache_stale", "key": key})
            return disk_cached[1]
        return result

    def stats(self) -> dict[str, Any]:
        with self._cache_lock:
            cache_size = len(self._cache)
        try:
            disk_cache_size = len(list(self._cache_dir.glob("*.json")))
        except Exception:
            disk_cache_size = 0
        return {
            "queue_size": self._tasks.qsize(),
            "cache_size": cache_size,
            "disk_cache_size": disk_cache_size,
            "cache_hits": self._cache_hits,
            "disk_cache_hits": self._disk_cache_hits,
            "cache_misses": self._cache_misses,
            "requests_started": self._requests_started,
            "requests_failed": self._requests_failed,
            "context_uses": self._ctx_uses,
            "last_error": self._last_error,
            "rebuild_requested": self._rebuild_requested,
        }

    @staticmethod
    def _looks_like_actor_id(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9]+", str(value or "").strip()))

    @staticmethod
    def _is_bad_profile_name(value: str) -> bool:
        text = str(value or "").strip().lower()
        return not text or "404" in text or "页面未找到" in text or "page not found" in text

    def _search_actress_sync(self, keyword: str, limit: int = 20) -> list[dict[str, Any]]:
        url = f"{self.BASE_URL}/search?q={quote_plus(keyword)}&f=actor"
        js = """() => {
            const abs = (value) => {
                if (!value) return '';
                try { return new URL(value, location.origin).href; } catch { return value; }
            };
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const imageOf = (node) => {
                const scope = node.closest('.item, .box, .actor-box, .actors, .grid-item, .column') || node;
                const img = scope.querySelector('img') || node.querySelector('img');
                if (!img) return '';
                return abs(img.getAttribute('src') || img.getAttribute('data-src') || img.getAttribute('data-original') || '');
            };
            const results = [];
            const seen = new Set();
            document.querySelectorAll('a[href*="/actors/"]').forEach(a => {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\\/actors\\/([A-Za-z0-9]+)$/);
                if (!m) return;
                const id = m[1];
                if (['censored','uncensored','western'].includes(id) || seen.has(id)) return;
                seen.add(id);
                const name = clean(a.textContent || a.getAttribute('title') || '');
                if (name) results.push({ id, name, cover: imageOf(a) });
            });
            return results.slice(0, 20);
        }"""
        result = self._fetch_list_resilient(f"search_actress:v2:{keyword.lower()}", url, js, self._search_cache_ttl)
        return (result or [])[:limit]

    def _resolve_actress_ref_sync(self, actress_ref: str) -> dict[str, str]:
        value = str(actress_ref or "").strip()
        if not value:
            return {"id": "", "name": "", "cover": ""}
        if self._looks_like_actor_id(value):
            return {"id": value, "name": "", "cover": ""}
        matches = self._search_actress_sync(value, limit=5)
        exact = next((item for item in matches if str(item.get("name") or "").strip() == value), None)
        found = exact or (matches[0] if matches else {})
        return {
            "id": str(found.get("id") or value).strip(),
            "name": str(found.get("name") or value).strip(),
            "cover": str(found.get("cover") or "").strip(),
        }

    def _get_actress_profile_sync(self, actress_id: str) -> dict[str, str]:
        resolved = self._resolve_actress_ref_sync(actress_id)
        actor_id = resolved.get("id") or str(actress_id or "").strip()
        if not self._looks_like_actor_id(actor_id):
            return {"id": actor_id, "name": resolved.get("name", ""), "cover": resolved.get("cover", "")}

        url = f"{self.BASE_URL}/actors/{actor_id}"
        js = """() => {
            const abs = (value) => {
                if (!value) return '';
                try { return new URL(value, location.origin).href; } catch { return value; }
            };
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const nameEl = document.querySelector('.actor-section-name, .actor-box strong, h1, .actor-section .title, .title');
            const img = Array.from(document.querySelectorAll('.actor-box img.avatar, img.avatar, img[src*="/avatars/"]'))
                .find(node => !/logo/i.test(node.getAttribute('src') || ''));
            return {
                id: location.pathname.split('/').filter(Boolean).pop() || '',
                name: nameEl ? clean(nameEl.textContent) : '',
                cover: img ? abs(img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
            };
        }"""
        def fetch() -> dict[str, str]:
            profile = self._do_search(url, js) or {}
            name = str(profile.get("name") or "").strip()
            if self._is_bad_profile_name(name):
                name = ""
            if name:
                name = " ".join(name.split())
                name = name.split(" 部影片")[0].strip()
                name = name.split(" 部作品")[0].strip()
                name = name.split(" 影片")[0].strip()
                name = name.split(" 作品")[0].strip()
                name = re.sub(r"\s+\d+\s*$", "", name).strip()
            cover = str(profile.get("cover") or "").strip()
            if name and (not cover or "logo_" in cover):
                search_url = f"{self.BASE_URL}/search?q={quote_plus(name)}&f=actor"
                search_js = """() => {
                    const abs = (value) => {
                        if (!value) return '';
                        try { return new URL(value, location.origin).href; } catch { return value; }
                    };
                    const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const actor = Array.from(document.querySelectorAll('.actor-box a[href*="/actors/"], a[href*="/actors/"]'))
                        .map(a => {
                            const href = a.getAttribute('href') || '';
                            const match = href.match(/\\/actors\\/([A-Za-z0-9]+)$/);
                            const box = a.closest('.actor-box, .box, .column') || a;
                            const img = box.querySelector('img.avatar, img');
                            const strong = box.querySelector('strong');
                            return {
                                id: match ? match[1] : '',
                                name: clean((strong ? strong.textContent : a.textContent) || a.getAttribute('title') || ''),
                                cover: img ? abs(img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
                            };
                        })
                        .find(item => item.id && item.cover);
                    return actor || {};
                }"""
                found = self._do_search(search_url, search_js) or {}
                if str(found.get("id") or "") == actor_id:
                    cover = str(found.get("cover") or cover).strip()
                    name = str(found.get("name") or name).strip()
            return {
                "id": actor_id,
                "name": name or resolved.get("name", ""),
                "cover": cover or resolved.get("cover", ""),
            }

        result = self._cached(f"actress_profile:v5:{actress_id}", fetch, self._detail_cache_ttl)
        return result or {"id": actor_id, "name": resolved.get("name", ""), "cover": resolved.get("cover", "")}

    def get_actress_profile(self, actress_id: str) -> dict[str, str]:
        return self._run(lambda: self._get_actress_profile_sync(actress_id))

    # ========== 搜索番号 ==========

    def search_av(self, keyword: str, limit: int = 40) -> list[dict[str, Any]]:
        url = f"{self.BASE_URL}/search?q={quote_plus(keyword)}&f=all"
        js = """() => {
            const items = document.querySelectorAll('.movie-list .item');
            return Array.from(items).slice(0, 40).map(item => {
                const link = item.querySelector('a.box');
                const img = item.querySelector('.cover img');
                const strong = item.querySelector('.video-title strong');
                const dateEl = item.querySelector('.meta');
                const href = link ? link.getAttribute('href') : '';
                const actresses = [];
                const seenActors = new Set();
                item.querySelectorAll('a[href*="/actors/"]').forEach(a => {
                    const actorHref = a.getAttribute('href') || '';
                    const m = actorHref.match(/\\/actors\\/([A-Za-z0-9]+)$/);
                    if (!m || seenActors.has(m[1])) return;
                    const name = a.textContent.trim();
                    if (!name) return;
                    seenActors.add(m[1]);
                    actresses.push({ id: m[1], name });
                });
                let av_id = '', title = '';
                if (strong) {
                    av_id = strong.textContent.trim();
                    const titleEl = item.querySelector('.video-title');
                    if (titleEl) title = titleEl.textContent.replace(av_id, '').trim();
                }
                return {
                    id: av_id,
                    title: title,
                    cover: img ? img.src : '',
                    date: dateEl ? dateEl.textContent.trim() : '',
                    actresses: actresses,
                    url: href ? 'https://javdb.com' + href : '',
                };
            }).filter(item => item.id);
        }"""
        result = self._run(lambda: self._fetch_list_resilient(f"search_av:{keyword.lower()}", url, js, self._search_cache_ttl))
        return (result or [])[:limit]

    # ========== 搜索女优 ==========

    def search_actress(self, keyword: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._run(lambda: self._search_actress_sync(keyword, limit))

    # ========== 女优全部作品 ==========

    def get_actress_avs(self, actress_id: str, limit: int = 100) -> list[dict[str, Any]]:
        resolved = self._run(lambda: self._resolve_actress_ref_sync(actress_id))
        actor_id = resolved.get("id") or str(actress_id or "").strip()
        if not self._looks_like_actor_id(actor_id):
            return []

        url = f"{self.BASE_URL}/actors/{actor_id}"
        js = """() => {
            const items = document.querySelectorAll('.movie-list .item');
            return Array.from(items).map(item => {
                const link = item.querySelector('a.box');
                const img = item.querySelector('.cover img');
                const strong = item.querySelector('.video-title strong');
                const dateEl = item.querySelector('.meta');
                const href = link ? link.getAttribute('href') : '';
                let av_id = '', title = '';
                if (strong) {
                    av_id = strong.textContent.trim();
                    const titleEl = item.querySelector('.video-title');
                    if (titleEl) title = titleEl.textContent.replace(av_id, '').trim();
                }
                return {
                    id: av_id,
                    title: title,
                    cover: img ? img.src : '',
                    date: dateEl ? dateEl.textContent.trim() : '',
                    url: href ? 'https://javdb.com' + href : '',
                };
            }).filter(item => item.id);
        }"""
        result = self._run(lambda: self._fetch_list_resilient(f"actress_avs:v2:{actor_id}", url, js, self._actress_cache_ttl))
        return (result or [])[:limit]

    # ========== 番号详情页 → 提取女优 ==========

    def get_av_actresses(self, av_url: str, include_profiles: bool = True) -> list[dict[str, Any]]:
        js = """() => {
            const results = [];
            const seen = new Set();
            document.querySelectorAll('a[href*="/actors/"]').forEach(a => {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\\/actors\\/([A-Za-z0-9]+)$/);
                if (!m) return;
                const id = m[1];
                if (['censored','uncensored','western'].includes(id) || seen.has(id)) return;
                seen.add(id);
                const name = a.textContent.trim();
                if (name) results.push({ id, name, cover: '' });
            });
            return results;
        }"""
        def fetch() -> list[dict[str, Any]]:
            items = self._cached(f"av_actresses:{av_url}", lambda: self._do_search(av_url, js), self._detail_cache_ttl) or []
            if not include_profiles:
                return items
            for item in items[:8]:
                if item.get("cover"):
                    continue
                profile = self._get_actress_profile_sync(str(item.get("id") or ""))
                item["cover"] = profile.get("cover", "")
                if not item.get("name") and profile.get("name"):
                    item["name"] = profile["name"]
            return items

        result = self._run(fetch)
        return result or []

    def get_av_detail(self, av_url: str) -> dict[str, Any]:
        js = """() => {
            const abs = (value) => {
                if (!value) return '';
                try { return new URL(value, location.origin).href; } catch { return value; }
            };
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const field = (labels) => {
                const blocks = Array.from(document.querySelectorAll('.movie-panel-info .panel-block, .panel-block'));
                for (const block of blocks) {
                    const text = clean(block.textContent);
                    const label = labels.find(item => text.toLowerCase().startsWith(item.toLowerCase()));
                    if (!label) continue;
                    const links = Array.from(block.querySelectorAll('a')).map(a => {
                        const rawUrl = a.getAttribute('href') || '';
                        const match = rawUrl.match(/\\/actors\\/([A-Za-z0-9]+)$/);
                        return {
                            id: match ? match[1] : '',
                            name: clean(a.textContent),
                            url: abs(rawUrl),
                        };
                    }).filter(x => x.name);
                    let value = text.replace(new RegExp('^' + label + '\\\\s*:?\\\\s*', 'i'), '').trim();
                    if (links.length) value = links.map(x => x.name).join(', ');
                    return { value, links };
                }
                return { value: '', links: [] };
            };
            const movieOf = (source) => {
                const link = source.matches && source.matches('a[href*="/v/"]') ? source : source.querySelector('a.box, a[href*="/v/"]');
                const item = source.closest ? (source.closest('.item, .box, .column, .tile-item') || source) : source;
                const href = link ? link.getAttribute('href') : '';
                const img = (link && link.querySelector ? link.querySelector('img') : null) || (item.querySelector ? item.querySelector('.cover img, img') : null);
                const strong = item.querySelector ? item.querySelector('.video-title strong, strong') : null;
                const titleEl = item.querySelector ? item.querySelector('.video-title, .title') : null;
                const dateEl = item.querySelector ? item.querySelector('.meta') : null;
                const text = clean(
                    (titleEl ? titleEl.textContent : '')
                    || (link ? link.getAttribute('title') : '')
                    || (img ? img.getAttribute('alt') : '')
                    || (item.textContent || '')
                );
                const matchedId = text.match(/[A-Za-z]{2,}(?:-[A-Za-z]+)?-?\\d{2,}/);
                const id = strong ? clean(strong.textContent) : (matchedId ? matchedId[0] : '');
                return {
                    id,
                    title: text.replace(id, '').trim(),
                    cover: img ? abs(img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
                    date: dateEl ? clean(dateEl.textContent) : '',
                    url: href ? abs(href) : '',
                };
            };
            const titleEl = document.querySelector('h2.title, .movie-panel-info .title, h1');
            const cover = document.querySelector('.column-video-cover img, .video-cover img, .movie-cover img, .cover img');
            const idField = field(['ID', '番號', '番号']);
            const dateField = field(['Released Date', '日期', '發行日期', '发行日期']);
            const durationField = field(['Duration', '時長', '时长']);
            const directorField = field(['Director', '導演', '导演']);
            const makerField = field(['Maker', '片商', '廠商', '厂商']);
            const ratingField = field(['Rating', '評分', '评分']);
            const tagsField = field(['Tags', '類別', '类别', '標籤', '标签']);
            const actorsField = field(['Actor(s)', '演員', '演员']);
            const screenshots = Array.from(document.querySelectorAll('.tile-images a, .sample-box a, a[href*="/samples/"]'))
                .map(a => abs(a.getAttribute('href') || a.querySelector('img')?.getAttribute('src') || ''))
                .filter(url => /\\.(jpe?g|png|webp)(\\?|$)/i.test(url))
                .filter((url, i, arr) => arr.indexOf(url) === i)
                .slice(0, 20);
            const trailerAttrs = ['src', 'data-src', 'data-video-url', 'data-trailer', 'data-video-preview', 'data-url', 'href'];
            let trailer = '';
            for (const node of document.querySelectorAll('video source, video, iframe, [data-src], [data-video-url], [data-trailer], [data-video-preview], [data-url], a[href]')) {
                for (const attribute of trailerAttrs) {
                    const value = node.getAttribute(attribute) || '';
                    if (/^(https?:)?\\/\\//i.test(value) && /\\.m3u8(\\?|$)|\\.mp4(\\?|$)|litevideo|freepv|trailer/i.test(value)) {
                        trailer = abs(value);
                        break;
                    }
                }
                if (trailer) break;
            }
            if (!trailer) {
                const scriptText = Array.from(document.scripts).map(script => script.textContent || '').join('\\n');
                const match = scriptText.match(/https?:[^"'\\s]+(?:\\.m3u8|\\.mp4)[^"'\\s]*/i);
                if (match) trailer = abs(match[0]);
            }
            const titleId = clean(titleEl ? titleEl.textContent : document.title).match(/[A-Za-z]{2,}(?:-[A-Za-z]+)?-?\\d{2,}/);
            const currentId = titleId ? titleId[0] : (idField.value || '');
            const recommendations = Array.from(document.querySelectorAll('a[href*="/v/"]'))
                .map(movieOf)
                .filter(item => {
                    if (!item.url || !item.cover) return false;
                    try { return /^\\/v\\/[^/]+$/.test(new URL(item.url).pathname); } catch { return false; }
                })
                .filter(item => item.id !== currentId && item.url !== location.href)
                .filter((item, index, items) => items.findIndex(candidate => candidate.url === item.url) === index)
                .slice(0, 12);
            return {
                id: currentId,
                title: clean(titleEl ? titleEl.textContent : document.title),
                cover: cover ? abs(cover.getAttribute('src') || cover.getAttribute('data-src') || '') : '',
                release_date: dateField.value,
                duration: durationField.value,
                director: directorField,
                maker: makerField,
                rating: ratingField.value,
                tags: tagsField.links.length ? tagsField.links : tagsField.value.split(',').map(x => ({name: clean(x), url: ''})).filter(x => x.name),
                actors: actorsField.links,
                screenshots,
                trailer,
                recommendations,
                url: location.href,
            };
        }"""
        def fetch_detail() -> dict[str, Any] | None:
            detail = self._do_search(av_url, js)
            if isinstance(detail, dict) and not self._is_bad_detail(detail):
                return detail
            self._log("warning", "JavDB 详情疑似验证页，重建浏览器后重试", {
                "stage": "javdb_bad_detail_retry",
                "url": av_url,
                "title": (detail or {}).get("title", "") if isinstance(detail, dict) else "",
            })
            self._close_browser()
            retry = self._do_search(av_url, js)
            if isinstance(retry, dict) and not self._is_bad_detail(retry):
                return retry
            return None

        result = self._run(lambda: self._cached(f"av_detail:v2:{av_url}", fetch_detail, self._detail_cache_ttl))
        return result or {}

    @staticmethod
    def _is_bad_detail(detail: dict[str, Any]) -> bool:
        title = str(detail.get("title") or "").strip().lower()
        av_id = str(detail.get("id") or "").strip().lower()
        return (
            not detail
            or "security verification" in title
            or "security verification" in av_id
            or (not detail.get("cover") and not detail.get("release_date") and not detail.get("actors"))
        )

    def get_listing(self, page_url: str, limit: int = 60, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        js = """() => {
            const abs = (value) => {
                if (!value) return '';
                try { return new URL(value, location.origin).href; } catch { return value; }
            };
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            return Array.from(document.querySelectorAll('.movie-list .item')).map(item => {
                const link = item.querySelector('a.box, a[href*="/v/"]');
                const img = item.querySelector('.cover img, img');
                const strong = item.querySelector('.video-title strong');
                const dateEl = item.querySelector('.meta');
                const href = link ? link.getAttribute('href') : '';
                const id = strong ? clean(strong.textContent) : '';
                const titleEl = item.querySelector('.video-title');
                return {
                    id,
                    title: titleEl ? clean(titleEl.textContent).replace(id, '').trim() : '',
                    cover: img ? abs(img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
                    date: dateEl ? clean(dateEl.textContent) : '',
                    url: href ? abs(href) : '',
                };
            }).filter(item => item.id && item.url);
        }"""
        key = f"listing:{page_url}"
        result = self._run(lambda: self._fetch_list_resilient(key, page_url, js, self._listing_cache_ttl, force_refresh=force_refresh))
        return (result or [])[:limit]

    def close(self) -> None:
        self._tasks.put(None)
        self._worker.join(timeout=5)


def is_access_ban_error(exc: object) -> bool:
    text = str(exc or "")
    lowered = text.lower()
    return (
        ("access" in lowered and "banned" in lowered)
        or "访问限制" in text
        or "访问被限制" in text
        or "禁止" in text
        or "訪問" in text
        or "異常行為" in text
    )


javdb = JavDBService()
