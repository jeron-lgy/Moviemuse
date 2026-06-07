"""
Playwright probe for JavLibrary actress pages.

Run this when direct requests are blocked. The script opens a normal browser
profile and parses the loaded DOM. If the site presents a challenge page, finish
the verification in the opened browser and press Enter in the terminal.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "https://www.javlibrary.com"
PROFILE_DIR = Path(__file__).resolve().parent / "browser-profile"


def actress_url(star_id: str, lang: str = "cn") -> str:
    return f"{BASE_URL}/{lang}/vl_star.php?s={star_id}"


def looks_like_challenge(html: str) -> bool:
    lower = html.lower()
    return any(
        marker in lower
        for marker in (
            "just a moment",
            "请稍候",
            "cf-challenge",
            "challenges.cloudflare.com",
        )
    )


def normalize_code(text: str) -> str:
    match = re.search(r"\b([A-Z]{2,10})[-_ ]?(\d{2,5})\b", text.upper())
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}"


def parse_videos(html: str, page_url: str) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[tuple[str, str, str]] = []

    for video in soup.select("div.video"):
        anchor = video.select_one("a[href*='?v='], a[href*='vl_searchbyid.php?keyword=']")
        if not anchor:
            continue

        code_text = video.select_one(".id")
        title_text = video.select_one(".title")
        title = title_text.get_text(" ", strip=True) if title_text else anchor.get_text(" ", strip=True)
        code = normalize_code(code_text.get_text(" ", strip=True) if code_text else title)
        href = anchor.get("href") or ""

        if code:
            rows.append((code, title, urljoin(page_url, href)))

    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--star-id", help="JavLibrary star id from vl_star.php?s=<id>")
    parser.add_argument("--url", help="Full JavLibrary actress page URL")
    parser.add_argument("--lang", default="cn")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--headless", action="store_true", help="Run without an interactive browser window")
    args = parser.parse_args()

    if not args.url and not args.star_id:
        parser.error("pass --url or --star-id")

    url = args.url or actress_url(args.star_id, args.lang)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=args.headless,
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        try:
            page.wait_for_selector("div.video", timeout=15000)
        except PlaywrightTimeoutError:
            html = page.content()
            if looks_like_challenge(html) and not args.headless:
                input("Challenge page detected. Complete verification in the browser, then press Enter here...")
                page.wait_for_selector("div.video", timeout=60000)
            else:
                print("No video list found. Current title:", page.title(), file=sys.stderr)
                context.close()
                return 3

        html = page.content()
        context.close()

    if looks_like_challenge(html):
        print("Still on challenge page; no content parsed.", file=sys.stderr)
        return 3

    rows = parse_videos(html, url)
    if not rows:
        print("No videos parsed from loaded page.", file=sys.stderr)
        return 2

    for code, title, item_url in rows[: args.limit]:
        print(f"{code}\t{title}\t{item_url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
