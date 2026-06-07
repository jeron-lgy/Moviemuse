"""
Probe JavLibrary actress pages for the newest visible video IDs.

This is intentionally isolated from the main project. It does not import app
code, write project data, or bypass access-control challenges.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.javlibrary.com"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class VideoItem:
    code: str
    title: str
    url: str


class ChallengePage(RuntimeError):
    pass


def actress_url(star_id: str, lang: str = "cn") -> str:
    return f"{BASE_URL}/{lang}/vl_star.php?s={star_id}"


def assert_not_challenge(html: str, status_code: int) -> None:
    title = BeautifulSoup(html, "html.parser").title
    title_text = title.get_text(strip=True).lower() if title else ""
    lower_html = html.lower()
    challenge_markers = (
        "just a moment",
        "cf-challenge",
        "challenges.cloudflare.com",
    )
    if (
        status_code in {403, 429, 520}
        or any(marker in lower_html for marker in challenge_markers)
        or "just a moment" in title_text
        or "\u8bf7\u7a0d\u5019" in title_text
    ):
        raise ChallengePage(
            "JavLibrary returned a Cloudflare/challenge page instead of content. "
            "Use a normal browser session and complete any required verification, "
            "or choose a data source/API that permits automated access."
        )


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=25)
    assert_not_challenge(response.text, response.status_code)
    response.raise_for_status()
    return response.text


def parse_videos(html: str, page_url: str) -> list[VideoItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[VideoItem] = []

    for video in soup.select("div.video"):
        anchor = video.select_one(
            "a[href*='?v='], a[href*='vl_searchbyid.php?keyword='], a[href$='.html']"
        )
        if not anchor:
            continue

        href = anchor.get("href") or ""
        url = urljoin(page_url, href)

        code_node = video.select_one(".id")
        title_node = video.select_one(".title")
        raw_code = code_node.get_text(" ", strip=True) if code_node else ""
        title = title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True)

        code = normalize_code(raw_code or title or href)
        if code:
            items.append(VideoItem(code=code, title=title, url=url))

    return items


def normalize_code(text: str) -> str:
    match = re.search(r"\b([A-Z]{2,10})[-_ ]?(\d{2,5})\b", text.upper())
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--star-id", help="JavLibrary star id from vl_star.php?s=<id>")
    parser.add_argument("--url", help="Full JavLibrary actress page URL")
    parser.add_argument("--lang", default="cn", help="Path language, default: cn")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if not args.url and not args.star_id:
        parser.error("pass --url or --star-id")

    url = args.url or actress_url(args.star_id, args.lang)
    html = fetch_html(url)
    videos = parse_videos(html, url)

    if not videos:
        print("No videos found. The page structure may have changed, or this page has no visible works.")
        return 2

    for item in videos[: args.limit]:
        print(f"{item.code}\t{item.title}\t{item.url}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ChallengePage as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(3)
