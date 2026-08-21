from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.worker_update_service import WorkerSoftwareUpdateService, newer_version, version_key


class WorkerSoftwareUpdateServiceTests(unittest.TestCase):
    def test_semantic_version_comparison(self) -> None:
        self.assertIsNotNone(version_key("v2.1.0"))
        self.assertTrue(newer_version("v2.2.0", "v2.1.0"))
        self.assertFalse(newer_version("v2.1.0", "v2.1.0"))
        self.assertFalse(newer_version("v2.0.9", "v2.1.0"))

    def test_release_payload_selects_highest_version(self) -> None:
        result = WorkerSoftwareUpdateService._release_payload([
            {"name": "v2.1.0"},
            {"name": "v2.3.0"},
            {"name": "v2.4.0-beta.1"},
            {"name": "not-a-version"},
        ])

        self.assertEqual(result["version"], "v2.3.0")

    def test_check_detects_update_and_reuses_cache(self) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return [{"name": "v2.2.0", "html_url": "https://example.test/releases/v2.2.0"}]

        class FakeClient:
            calls = 0

            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _url: str):
                FakeClient.calls += 1
                return FakeResponse()

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.worker_update_service.httpx.Client", FakeClient
        ):
            service = WorkerSoftwareUpdateService("v2.1.0", Path(temp_dir) / "update.json")
            first = service.check(force=True)
            second = service.check(force=False)

        self.assertTrue(first["update_available"])
        self.assertEqual(first["latest_version"], "v2.2.0")
        self.assertEqual(second["version_status"], "update_available")
        self.assertEqual(FakeClient.calls, 1)


if __name__ == "__main__":
    unittest.main()
