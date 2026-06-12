from __future__ import annotations

import unittest
from unittest.mock import patch

from app.javlibrary_service import JavLibraryService


class FakeResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def ok_html(html: str) -> dict[str, object]:
    return {"status": "ok", "solution": {"status": 200, "response": html}}


class JavLibraryServiceSessionTest(unittest.TestCase):
    def test_reuses_warmed_flaresolverr_session(self) -> None:
        service = JavLibraryService()
        service.set_service_url_provider(lambda: "http://flaresolverr.local/v1")
        commands: list[dict[str, object]] = []

        def fake_post(url: str, json: dict[str, object], timeout: object) -> FakeResponse:
            commands.append(dict(json))
            cmd = str(json.get("cmd") or "")
            if cmd == "sessions.create":
                return FakeResponse({"status": "ok"})
            if cmd == "request.get":
                target = str(json.get("url") or "")
                if target.endswith("/cn/"):
                    return FakeResponse(ok_html("<html><title>JavLibrary</title></html>"))
                return FakeResponse(ok_html("<html><div id='content'>target</div></html>"))
            if cmd == "sessions.destroy":
                return FakeResponse({"status": "ok"})
            return FakeResponse({"status": "error", "message": cmd})

        with patch("app.javlibrary_service.requests.post", side_effect=fake_post):
            first = service.fetch_with_flaresolverr("https://www.javlibrary.com/cn/vl_star.php?s=abc", cooldown=0)
            second = service.fetch_with_flaresolverr("https://www.javlibrary.com/cn/vl_star.php?s=def", cooldown=0)

        self.assertIn("target", first)
        self.assertIn("target", second)
        self.assertEqual([item["cmd"] for item in commands].count("sessions.create"), 1)
        self.assertEqual([item["cmd"] for item in commands].count("sessions.destroy"), 0)
        homepage_hits = [item for item in commands if item.get("url") == "https://www.javlibrary.com/cn/"]
        self.assertEqual(len(homepage_hits), 1)
        self.assertEqual(service.stats()["session_uses"], 2)


if __name__ == "__main__":
    unittest.main()
