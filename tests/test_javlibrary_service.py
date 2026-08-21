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

    def test_single_flaresolverr_500_retries_same_session(self) -> None:
        service = JavLibraryService()
        service.set_service_url_provider(lambda: "http://flaresolverr.local/v1")
        commands: list[dict[str, object]] = []
        target_attempts = 0

        def fake_post(url: str, json: dict[str, object], timeout: object) -> FakeResponse:
            nonlocal target_attempts
            commands.append(dict(json))
            if json.get("cmd") == "sessions.create":
                return FakeResponse({"status": "ok"})
            if json.get("cmd") == "sessions.destroy":
                return FakeResponse({"status": "ok"})
            if json.get("cmd") == "request.get":
                if str(json.get("url") or "").endswith("/cn/"):
                    return FakeResponse(ok_html("<html><title>JavLibrary</title></html>"))
                target_attempts += 1
                if target_attempts == 1:
                    return FakeResponse({}, status_code=500)
                return FakeResponse(ok_html("<html><div>recovered</div></html>"))
            return FakeResponse({"status": "error", "message": "unexpected"})

        with patch("app.javlibrary_service.requests.post", side_effect=fake_post):
            html = service.fetch_with_flaresolverr(
                "https://www.javlibrary.com/cn/vl_star.php?s=abc",
                retries=1,
                base_delay=0,
                max_delay=0,
                cooldown=0,
            )

        self.assertIn("recovered", html)
        self.assertEqual([item["cmd"] for item in commands].count("sessions.create"), 1)
        self.assertEqual([item["cmd"] for item in commands].count("sessions.destroy"), 0)

    def test_consecutive_flaresolverr_500_rebuilds_once(self) -> None:
        service = JavLibraryService()
        service.set_service_url_provider(lambda: "http://flaresolverr.local/v1")
        commands: list[dict[str, object]] = []
        target_attempts = 0

        def fake_post(url: str, json: dict[str, object], timeout: object) -> FakeResponse:
            nonlocal target_attempts
            commands.append(dict(json))
            if json.get("cmd") in {"sessions.create", "sessions.destroy"}:
                return FakeResponse({"status": "ok"})
            if json.get("cmd") == "request.get":
                if str(json.get("url") or "").endswith("/cn/"):
                    return FakeResponse(ok_html("<html><title>JavLibrary</title></html>"))
                target_attempts += 1
                if target_attempts <= 2:
                    return FakeResponse({}, status_code=500)
                return FakeResponse(ok_html("<html><div>recovered</div></html>"))
            return FakeResponse({"status": "error", "message": "unexpected"})

        with patch("app.javlibrary_service.requests.post", side_effect=fake_post):
            html = service.fetch_with_flaresolverr(
                "https://www.javlibrary.com/cn/vl_star.php?s=abc",
                retries=2,
                base_delay=0,
                max_delay=0,
                cooldown=0,
            )

        self.assertIn("recovered", html)
        self.assertEqual([item["cmd"] for item in commands].count("sessions.create"), 2)
        self.assertEqual([item["cmd"] for item in commands].count("sessions.destroy"), 1)

    def test_destroy_failure_is_reported_and_keeps_session_owned(self) -> None:
        service = JavLibraryService()
        service.set_service_url_provider(lambda: "http://flaresolverr.local/v1")

        def fake_post(url: str, json: dict[str, object], timeout: object) -> FakeResponse:
            if json.get("cmd") == "sessions.create":
                return FakeResponse({"status": "ok"})
            if json.get("cmd") == "sessions.destroy":
                return FakeResponse({}, status_code=500)
            return FakeResponse(ok_html("<html><div>ok</div></html>"))

        with patch("app.javlibrary_service.requests.post", side_effect=fake_post), \
            patch("app.javlibrary_service.time.sleep", return_value=None):
            service.fetch_with_flaresolverr(
                "https://www.javlibrary.com/cn/vl_star.php?s=abc",
                cooldown=0,
            )
            closed = service.close(timeout_ms=1000)

        self.assertFalse(closed)
        self.assertTrue(service.stats()["session_active"])
        self.assertTrue(service.stats()["session_destroy_pending"])
        self.assertEqual(service.stats()["session_destroy_failures"], 1)


if __name__ == "__main__":
    unittest.main()
