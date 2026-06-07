from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7897")
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7897")
os.environ.setdefault("JAVDB_CACHE_DIR", str(ROOT / "tmp-tests" / ".javdb-cache"))
os.environ.setdefault("JAVDB_MIN_INTERVAL_SECONDS", "0.2")
os.environ.setdefault("JAVDB_REQUEST_TIMEOUT_MS", "35000")
os.environ.setdefault("JAVDB_RUN_TIMEOUT_SECONDS", "90")

from app.javdb_service import javdb  # noqa: E402


def dump(label: str, value: object) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    keyword = "涼森れむ"
    actresses = javdb.search_actress(keyword, limit=5)
    dump("actresses", actresses)
    if not actresses:
        return 2

    actress = actresses[0]
    actress_id = actress.get("id") or keyword
    profile = javdb.get_actress_profile(str(actress_id))
    dump("profile", profile)

    avs = javdb.get_actress_avs(str(actress_id), limit=20)
    dump("avs", avs)

    target = next((item for item in avs if str(item.get("id", "")).upper() == "ABF-358"), None)
    if target is None:
        target = avs[0] if avs else None
    dump("target", target)

    if isinstance(target, dict) and target.get("url"):
        detail = javdb.get_av_detail(str(target["url"]))
        dump("detail", detail)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        javdb.close()
