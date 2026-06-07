from __future__ import annotations

import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tmp-tests" / "dmm-preorder-output"
sys.path.insert(0, str(ROOT))

from app.subscription_service import SubscriptionService  # noqa: E402

HTTP_PROXY = "http://127.0.0.1:7897"
os.environ.setdefault("HTTP_PROXY", HTTP_PROXY)
os.environ.setdefault("HTTPS_PROXY", HTTP_PROXY)


@dataclass
class SearchHit:
    title: str
    url: str
    cover: str
    status: str
    release_date: str
    actresses: list[str]
    text: str


def compact(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def normalize_date(value: str) -> str:
    match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", value or "")
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    return date(year, month, day).isoformat()


def normalize_av_id_from_cid(cid: str) -> str:
    raw = str(cid or "").lower().strip()
    raw = re.sub(r"r$", "", raw)
    raw = re.sub(r"^\d+", "", raw)
    match = re.search(r"([a-z]{2,})(\d{2,})$", raw)
    if not match:
        return raw.upper()
    return f"{match.group(1).upper()}-{match.group(2)}"


def cid_from_url(url: str) -> str:
    match = re.search(r"/cid=([^/?]+)/?", html.unescape(url))
    return match.group(1) if match else ""


def session() -> requests.Session:
    client = requests.Session()
    client.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            ),
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
        }
    )
    client.proxies.update({"http": HTTP_PROXY, "https": HTTP_PROXY})
    return client


def pass_age_gate(client: requests.Session) -> None:
    rurl = quote("https://www.dmm.co.jp/", safe="")
    client.get(f"https://www.dmm.co.jp/age_check/=/declared=yes/?rurl={rurl}", timeout=30)


def parse_search_hits(content: str) -> list[SearchHit]:
    soup = BeautifulSoup(content, "html.parser")
    hits: list[SearchHit] = []
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
        url = html.unescape(link.get("href") or "")
        if url in seen:
            continue
        seen.add(url)
        text = compact(card.get_text(" ", strip=True))
        title = compact((card.find("p").get_text(" ", strip=True) if card.find("p") else "").replace("【予約】", ""))
        release = ""
        match = re.search(r"(?:発売日|貸出日)：\s*(\d{4}/\d{1,2}/\d{1,2})", text)
        if match:
            release = normalize_date(match.group(1))
        actresses: list[str] = []
        actor_match = re.search(r"出演者：(.+?)(?:\s*\| |$)", text)
        if actor_match:
            actresses = [compact(item) for item in actor_match.group(1).split("|") if compact(item)]
        status = "preorder" if "予約" in text else ("coming_soon" if "近日" in text else "")
        hits.append(
            SearchHit(
                title=title,
                url=url,
                cover=img.get("src") or "",
                status=status,
                release_date=release,
                actresses=actresses,
                text=text,
            )
        )
    return hits


def search_by_actress(client: requests.Session, actress_name: str) -> list[SearchHit]:
    url = f"https://www.dmm.co.jp/search/=/searchstr={quote(actress_name)}/sort=date/"
    response = client.get(url, timeout=30)
    response.raise_for_status()
    return parse_search_hits(response.text)


def search_by_code(client: requests.Session, av_id: str) -> list[SearchHit]:
    url = f"https://www.dmm.co.jp/search/=/searchstr={quote(av_id)}/"
    response = client.get(url, timeout=30)
    response.raise_for_status()
    return parse_search_hits(response.text)


def canonical_hit_for_code(hits: list[SearchHit], av_id: str) -> SearchHit | None:
    wanted = av_id.replace("-", "").lower()
    candidates = []
    for hit in hits:
        cid = cid_from_url(hit.url)
        normalized = normalize_av_id_from_cid(cid).replace("-", "").lower()
        if normalized == wanted and "/mono/dvd/" in hit.url and "限定" not in hit.text and "FANZA限定" not in hit.text:
            candidates.append(hit)
    return candidates[0] if candidates else None


def page_lines(soup: BeautifulSoup) -> list[str]:
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    return [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]


def field(lines: list[str], label: str, stop_labels: tuple[str, ...] = ()) -> str:
    needle = f"{label}："
    for index, line in enumerate(lines):
        if line == needle or line == label:
            values: list[str] = []
            for item in lines[index + 1 :]:
                if any(item == f"{stop}：" or item == stop for stop in stop_labels):
                    break
                if item in {"----", "-", "関連タグ：", "メディア", "DVD"}:
                    break
                values.append(item)
            return compact(" ".join(values))
    return ""


def parse_detail(client: requests.Session, hit: SearchHit, av_id: str) -> dict[str, Any]:
    response = client.get(hit.url.split("?")[0], timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    title = compact(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else hit.title)
    cover_node = soup.select_one('img[src*="pics.dmm.co.jp"][src$="pl.jpg"]') or soup.select_one('img[src*="pics.dmm.co.jp"]')
    cover = cover_node.get("src") if cover_node else hit.cover
    lines = page_lines(soup)
    labels = ("発売日", "収録時間", "出演者", "監督", "シリーズ", "メーカー", "レーベル", "ジャンル", "品番", "関連タグ", "メディア")
    raw_cid = field(lines, "品番", ("関連タグ", "メディア")) or cid_from_url(hit.url)
    genres = field(lines, "ジャンル", ("品番", "関連タグ", "メディア"))
    actors = field(lines, "出演者", ("監督", "シリーズ", "メーカー"))
    return {
        "id": av_id,
        "title": title.replace(av_id, "").strip(),
        "release_date": normalize_date(field(lines, "発売日", ("収録時間",))),
        "date": normalize_date(field(lines, "発売日", ("収録時間",))),
        "duration": field(lines, "収録時間", ("出演者",)),
        "actresses": [{"name": name} for name in re.split(r"\s+", actors) if name],
        "director": field(lines, "監督", ("シリーズ",)),
        "maker": field(lines, "メーカー", ("レーベル",)),
        "label": field(lines, "レーベル", ("ジャンル",)),
        "tags": [{"name": name} for name in re.split(r"\s+", genres) if name],
        "cid": raw_cid,
        "status": hit.status,
        "cover": cover,
        "cover_thumb": hit.cover,
        "url": response.url,
        "source": "dmm",
    }


def subscription_payload(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": detail["id"],
        "title": detail["title"],
        "cover": detail["cover"],
        "date": detail["date"],
        "actresses": detail["actresses"],
        "url": detail["url"],
        "status": "pending",
        "detail": detail,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    actress_name = "\u6dbc\u68ee\u308c\u3080"
    expected_id = "ABF-358"
    client = session()
    pass_age_gate(client)

    actress_hits = search_by_actress(client, actress_name)
    code_hits = search_by_code(client, expected_id)
    target = canonical_hit_for_code(code_hits, expected_id)
    if not target:
        print(f"Could not find canonical {expected_id}", file=sys.stderr)
        return 2
    detail = parse_detail(client, target, expected_id)
    payload = subscription_payload(detail)
    temp_store = SubscriptionService(OUT_DIR / "subscription-data")
    saved_subscription = temp_store.subscribe_av(payload)

    result = {
        "query": {"actress": actress_name, "expected_latest_solo_id": expected_id},
        "actress_search_top": [hit.__dict__ for hit in actress_hits[:12]],
        "code_search_hits": [hit.__dict__ for hit in code_hits[:8]],
        "selected_detail": detail,
        "local_subscription_payload": payload,
        "saved_subscription": saved_subscription,
        "temp_subscription_db": str(OUT_DIR / "subscription-data" / "subscriptions.sqlite3"),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUT_DIR / "abf358-result.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nWrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
