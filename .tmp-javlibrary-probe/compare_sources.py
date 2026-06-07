from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.dmm_service import dmm  # noqa: E402
from app.javdb_service import javdb  # noqa: E402


DATA_DIR = ROOT / "data"
SQLITE_PATH = DATA_DIR / "subscriptions.sqlite3"
FLARESOLVERR_URL = "http://127.0.0.1:8281/v1"
JAVLIBRARY_BASE = "https://www.javlibrary.com/cn/"


@dataclass
class Trial:
    source: str
    operation: str
    target: str
    ok: bool
    seconds: float
    count: int = 0
    top_ids: list[str] | None = None
    cache_before: dict[str, Any] | None = None
    cache_after: dict[str, Any] | None = None
    error: str = ""


def load_javlibrary_parser():
    parser_path = PROBE_DIR / "probe_javlibrary.py"
    spec = importlib.util.spec_from_file_location("probe_javlibrary", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {parser_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


jl_parser = load_javlibrary_parser()


def ids(items: list[dict[str, Any]] | list[Any], limit: int = 5) -> list[str]:
    result: list[str] = []
    for item in items[:limit]:
        if isinstance(item, dict):
            value = str(item.get("id") or item.get("code") or "").strip()
        else:
            value = str(getattr(item, "code", "") or "").strip()
        if value:
            result.append(value)
    return result


def dmm_stats() -> dict[str, Any]:
    return dmm.stats()


def javdb_stats() -> dict[str, Any]:
    return javdb.stats()


def sqlite_stats() -> dict[str, Any]:
    if not SQLITE_PATH.exists():
        return {}
    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT namespace, COUNT(*) AS count FROM subscription_metadata_cache GROUP BY namespace ORDER BY namespace"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS count FROM subscription_metadata_cache").fetchone()["count"]
    return {"total": int(total or 0), "namespaces": {row["namespace"]: int(row["count"] or 0) for row in rows}}


def fs(payload: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
    payload.setdefault("maxTimeout", 120000)
    response = requests.post(FLARESOLVERR_URL, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr failed: {data}")
    return data


def jl_fetch(session: str, url: str) -> str:
    data = fs({"cmd": "request.get", "url": url, "session": session})
    solution = data.get("solution") or {}
    status = int(solution.get("status") or 0)
    html = solution.get("response") or ""
    jl_parser.assert_not_challenge(html, status)
    return html


def jl_actor(star_id: str, limit: int = 20) -> list[Any]:
    session = f"compare-jl-{uuid.uuid4().hex[:8]}"
    fs({"cmd": "sessions.create", "session": session}, timeout=30)
    try:
        jl_fetch(session, JAVLIBRARY_BASE)
        time.sleep(2)
        url = f"{JAVLIBRARY_BASE}vl_star.php?s={star_id}"
        return jl_parser.parse_videos(jl_fetch(session, url), url)[:limit]
    finally:
        try:
            fs({"cmd": "sessions.destroy", "session": session}, timeout=30)
        except Exception:
            pass


def jl_listing(url: str, limit: int = 20) -> list[Any]:
    session = f"compare-jl-{uuid.uuid4().hex[:8]}"
    fs({"cmd": "sessions.create", "session": session}, timeout=30)
    try:
        jl_fetch(session, JAVLIBRARY_BASE)
        time.sleep(2)
        return jl_parser.parse_videos(jl_fetch(session, url), url)[:limit]
    finally:
        try:
            fs({"cmd": "sessions.destroy", "session": session}, timeout=30)
        except Exception:
            pass


def javdb_actor(query: str, limit: int = 20) -> list[dict[str, Any]]:
    matches = javdb.search_actress(query, limit=5)
    if not matches:
        return []
    actor_id = str(matches[0].get("id") or "")
    return javdb.get_actress_avs(actor_id, limit=limit)


def timed(source: str, operation: str, target: str, func: Callable[[], list[Any]], stats_func: Callable[[], dict[str, Any]] | None = None) -> Trial:
    before = stats_func() if stats_func else {}
    started = time.perf_counter()
    try:
        items = func()
        elapsed = time.perf_counter() - started
        after = stats_func() if stats_func else {}
        return Trial(
            source=source,
            operation=operation,
            target=target,
            ok=bool(items),
            seconds=round(elapsed, 3),
            count=len(items),
            top_ids=ids(items, 8),
            cache_before=before,
            cache_after=after,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - started
        after = stats_func() if stats_func else {}
        return Trial(
            source=source,
            operation=operation,
            target=target,
            ok=False,
            seconds=round(elapsed, 3),
            cache_before=before,
            cache_after=after,
            error=str(exc),
        )


def main() -> int:
    cases: list[tuple[str, str, str, Callable[[], list[Any]], Callable[[], dict[str, Any]] | None]] = [
        ("javlibrary", "actor_latest", "桜空もも", lambda: jl_actor("aemco", 10), None),
        ("javlibrary", "actor_latest", "野々浦暖", lambda: jl_actor("aesse", 10), None),
        ("javlibrary", "maker_latest", "IDEA POCKET(label from latest: tissue)", lambda: jl_listing("https://www.javlibrary.com/cn/vl_label.php?l=buwq", 20), None),
        ("javlibrary", "maker_latest", "PRESTIGE(label from latest: ABSOLUTELY FANTASIA)", lambda: jl_listing("https://www.javlibrary.com/cn/vl_label.php?l=aqmuc", 20), None),
        ("dmm", "actor_latest", "桜空もも", lambda: dmm.get_actress_avs("桜空もも", limit=20), dmm_stats),
        ("dmm", "actor_latest", "野々浦暖", lambda: dmm.get_actress_avs("野々浦暖", limit=20), dmm_stats),
        ("dmm", "maker_latest", "IDEA POCKET", lambda: dmm.get_listing_avs("https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=1219/sort=date/", limit=20), dmm_stats),
        ("dmm", "maker_latest", "PRESTIGE", lambda: dmm.get_listing_avs("https://www.dmm.co.jp/mono/dvd/-/list/=/article=maker/id=40136/sort=date/", limit=20), dmm_stats),
        ("javdb", "actor_latest", "桜空もも", lambda: javdb_actor("桜空もも", 20), javdb_stats),
        ("javdb", "actor_latest", "野々浦暖", lambda: javdb_actor("野々浦暖", 20), javdb_stats),
        ("javdb", "maker_latest", "IDEA POCKET", lambda: javdb.get_listing("https://javdb.com/makers/ZXX?f=download", limit=20), javdb_stats),
        ("javdb", "maker_latest", "PRESTIGE", lambda: javdb.get_listing("https://javdb.com/makers/6M?f=download", limit=20), javdb_stats),
    ]
    trials: list[Trial] = []
    print("sqlite_cache", json.dumps(sqlite_stats(), ensure_ascii=False), flush=True)
    for source, operation, target, func, stats_func in cases:
        print(f"running {source} {operation} {target}", flush=True)
        trial = timed(source, operation, target, func, stats_func)
        trials.append(trial)
        print(json.dumps(asdict(trial), ensure_ascii=False), flush=True)
        time.sleep(2)
    out = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sqlite_cache": sqlite_stats(),
        "trials": [asdict(trial) for trial in trials],
    }
    report_path = PROBE_DIR / "compare_sources_result.json"
    report_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
