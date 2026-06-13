"""MTeam RSS/API 搜索集成。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx

DEFAULT_MTEAM_API_URL = "https://api.m-team.cc/api/torrent/search"
DEFAULT_MTEAM_API_BASE = "https://api.m-team.cc/api"
KNOWN_LIST_KEYS = ("data", "results", "list", "items", "records", "rows")


def search_mteam(keyword: str, settings: dict[str, Any], limit: int = 12) -> dict[str, Any]:
    mteam = settings.get("mteam", {}) if isinstance(settings, dict) else {}
    if not mteam.get("enabled"):
        return {"enabled": False, "results": [], "message": "MTeam 搜索未启用"}

    mode = str(mteam.get("mode") or "rss")
    if mode == "api":
        return search_mteam_api(keyword, mteam, limit)

    rss_url = str(mteam.get("rss_url") or "").strip()
    if not rss_url:
        return {"enabled": True, "results": [], "message": "尚未配置 MTeam RSS 地址"}

    url = build_search_url(rss_url, keyword)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Media-Toolbox/1.0"})
            resp.raise_for_status()
        results = parse_rss(resp.text, keyword, limit)
        if not results and str(mteam.get("api_key") or "").strip():
            api_result = search_mteam_api(keyword, mteam, limit)
            if api_result.get("results") or api_result.get("message"):
                return api_result
        message = ""
        if not results and "{keyword}" not in rss_url and "{q}" not in rss_url:
            message = "RSS 当前列表没有匹配资源；如需按关键词搜索，建议切换 MTeam API 模式"
        return {"enabled": True, "results": results, "message": message}
    except (httpx.HTTPError, ET.ParseError) as exc:
        return {"enabled": True, "results": [], "message": f"MTeam 请求失败: {exc}"}


def search_mteam_api(keyword: str, mteam: dict[str, Any], limit: int) -> dict[str, Any]:
    api_url = str(mteam.get("api_url") or "").strip()
    site_url = str(mteam.get("site_url") or "").strip()
    api_key = str(mteam.get("api_key") or "").strip()
    api_urls = api_candidates(api_url, site_url)
    if not api_urls:
        return {"enabled": True, "results": [], "message": "尚未配置 MTeam API 地址"}
    headers = {"User-Agent": "Media-Toolbox/1.0", "Accept": "application/json"}
    method = str(mteam.get("api_method") or "POST").upper()
    search_mode = str(mteam.get("search_mode") or "adult").strip() or "adult"
    if api_key:
        headers["x-api-key"] = api_key
    last_error = ""
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            for candidate in api_urls:
                url = build_search_url(candidate, keyword)
                try:
                    if method == "GET":
                        params = {} if ("{keyword}" in candidate or "{q}" in candidate) else {
                            "keyword": keyword,
                            "mode": search_mode,
                            "pageNumber": 1,
                            "pageSize": limit,
                        }
                        resp = client.get(url, headers=headers, params=params)
                    else:
                        resp = client.post(
                            url,
                            headers=headers,
                            json={"keyword": keyword, "mode": search_mode, "pageNumber": 1, "pageSize": limit},
                        )
                    resp.raise_for_status()
                    payload = resp.json()
                    results = parse_api(payload, keyword, limit, site_url)
                    if results:
                        return {"enabled": True, "results": results, "message": ""}
                    if isinstance(payload, dict) and str(payload.get("message") or "").upper() not in ("", "SUCCESS"):
                        last_error = str(payload.get("message"))
                    else:
                        last_error = "MTeam API 没有返回匹配资源"
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = str(exc)
                    continue
        return {"enabled": True, "results": [], "message": last_error or "MTeam API 没有返回匹配资源"}
    except (httpx.HTTPError, ValueError) as exc:
        return {"enabled": True, "results": [], "message": f"MTeam API 请求失败: {exc}"}


def download_mteam_torrent(torrent_id: str, settings: dict[str, Any]) -> tuple[bytes, str]:
    mteam = settings.get("mteam", {}) if isinstance(settings, dict) else {}
    api_key = str(mteam.get("api_key") or "").strip()
    if not torrent_id:
        raise ValueError("缺少 MTeam 种子 ID")
    token_headers = {
        "User-Agent": "Media-Toolbox/1.0",
        "accept": "*/*",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    if api_key:
        token_headers["x-api-key"] = api_key
    errors: list[str] = []
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for token_url in download_token_candidates(mteam):
            try:
                resp = client.post(token_url, headers=token_headers, data={"id": torrent_id})
                resp.raise_for_status()
                payload = resp.json()
                if str(payload.get("message") or "").upper() != "SUCCESS" or not payload.get("data"):
                    errors.append(str(payload.get("message") or payload))
                    continue
                download_url = token_download_url(payload["data"])
                if not download_url:
                    errors.append("genDlToken 未返回下载地址")
                    continue
                torrent_resp = client.get(download_url, headers={"User-Agent": "Media-Toolbox/1.0"})
                torrent_resp.raise_for_status()
                filename = filename_from_headers(torrent_resp.headers.get("content-disposition", "")) or f"{torrent_id}.torrent"
                return torrent_resp.content, filename
            except httpx.HTTPError as exc:
                errors.append(str(exc))
    raise RuntimeError("MTeam 种子下载失败: " + "；".join(errors[-2:]))


def api_candidates(api_url: str, site_url: str) -> list[str]:
    urls: list[str] = []
    if api_url:
        urls.append(api_url)
    elif site_url:
        urls.append(urljoin(site_url.rstrip("/") + "/", "api/torrent/search"))
    if DEFAULT_MTEAM_API_URL not in urls:
        urls.append(DEFAULT_MTEAM_API_URL)
    return urls


def download_token_candidates(mteam: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    site_url = str(mteam.get("site_url") or "").strip()
    api_url = str(mteam.get("api_url") or "").strip()
    if api_url:
        base = api_url.split("/torrent/search", 1)[0].rstrip("/")
        urls.append(f"{base}/torrent/genDlToken")
    if site_url:
        urls.append(urljoin(site_url.rstrip("/") + "/", "api/torrent/genDlToken"))
    default_url = f"{DEFAULT_MTEAM_API_BASE}/torrent/genDlToken"
    if default_url not in urls:
        urls.append(default_url)
    return urls


def build_search_url(url: str, keyword: str) -> str:
    if "{keyword}" in url:
        return url.replace("{keyword}", quote_plus(keyword))
    if "{q}" in url:
        return url.replace("{q}", quote_plus(keyword))
    return url


def parse_rss(content: str, keyword: str, limit: int) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    keyword_lower = keyword.lower()
    results: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = text_of(item, "title")
        link = text_of(item, "link")
        pub_date = text_of(item, "pubDate")
        enclosure = item.find("enclosure")
        torrent_url = enclosure.attrib.get("url", "") if enclosure is not None else ""
        size = enclosure.attrib.get("length", "") if enclosure is not None else ""
        if keyword_lower and keyword_lower not in title.lower():
            continue
        results.append({"title": title, "link": link, "pubDate": pub_date, "torrent": torrent_url, "size": size})
        if len(results) >= limit:
            break
    return results


def parse_api(payload: Any, keyword: str, limit: int, site_url: str = "") -> list[dict[str, Any]]:
    items = find_items(payload)
    if not isinstance(items, list):
        return []
    keyword_lower = keyword.lower()
    results: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = pick_text(item, "title", "name", "subject", "smallDescr", "descr")
        if keyword_lower and keyword_lower not in title.lower():
            continue
        torrent_id = pick_text(item, "id", "tid", "torrentId")
        link = pick_text(item, "link", "url", "details", "detailUrl")
        torrent = pick_text(item, "torrent", "download", "downloadUrl", "download_url", "rssUrl")
        pub_date = pick_text(item, "pubDate", "createdAt", "createdDate", "date", "addedDate")
        labels = normalize_labels(item.get("labelsNew") or item.get("labels") or item.get("tags"))
        size = item.get("size") or item.get("sizeBytes") or item.get("bytes") or ""
        seeders = pick_text(item, "seeders", "seeds", "seedCount", "seed_count", "uploadCount", "upload_count")
        status = item.get("status")
        if not seeders and isinstance(status, dict):
            seeders = pick_text(status, "seeders", "seeds", "seedCount", "seed_count", "uploadCount", "upload_count")
        if not link:
            link = build_detail_link(site_url, torrent_id)
        results.append({
            "id": torrent_id,
            "title": title,
            "link": link,
            "pubDate": pub_date,
            "torrent": torrent,
            "size": size,
            "smallDescr": pick_text(item, "smallDescr", "description", "descr"),
            "labels": labels,
            "seeders": seeders,
            "category": pick_text(item, "category"),
            "discount": item.get("discount", ""),
            "standard": pick_text(item, "standard"),
            "medium": pick_text(item, "medium"),
            "videoCodec": pick_text(item, "videoCodec"),
            "source": pick_text(item, "source"),
        })
        if len(results) >= limit:
            break
    return results


def find_items(payload: Any) -> Any:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in KNOWN_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for key in KNOWN_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            found = find_items(value)
            if isinstance(found, list):
                return found
    return []


def pick_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            nested = pick_text(value, "url", "href", "name", "title", "value")
            if nested:
                return nested
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_labels(value: Any) -> list[str]:
    labels: list[str] = []
    if isinstance(value, list):
        values = value
    elif value:
        values = [value]
    else:
        values = []
    for item in values:
        if isinstance(item, dict):
            text = pick_text(item, "name", "title", "label", "value")
        else:
            text = str(item).strip()
        if text and text not in labels:
            labels.append(text)
    return labels


def build_detail_link(site_url: str, torrent_id: str) -> str:
    if not site_url or not torrent_id:
        return ""
    return urljoin(site_url.rstrip("/") + "/", f"detail/{torrent_id}")


def filename_from_headers(value: str) -> str:
    for part in value.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip().strip('"')
    return ""


def token_download_url(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("url", "downloadUrl", "download_url", "link"):
            value = data.get(key)
            if value:
                return str(value)
    return ""


def text_of(item: ET.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None else ""
