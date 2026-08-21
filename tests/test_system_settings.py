from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.system_settings import SystemSettingsService


class SystemSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="moviemuse-system-settings-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_legacy_library_notification_event_and_template_are_migrated(self) -> None:
        settings_file = self.root / "system_settings.json"
        settings_file.write_text(
            json.dumps({
                "notifications": {
                    "channels": [],
                    "events": {
                        "subscription_completed": False,
                        "jellyfin_in_library": True,
                    },
                    "templates": {
                        "subscription_completed": {
                            "title": "旧标题",
                            "message": "旧正文",
                        },
                    },
                },
                "auth": {
                    "username": "admin",
                    "password_hash": "configured",
                },
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        notifications = SystemSettingsService(self.root).get()["notifications"]
        persisted = json.loads(settings_file.read_text(encoding="utf-8"))["notifications"]

        self.assertFalse(notifications["events"]["subscription_in_library"])
        self.assertNotIn("subscription_completed", notifications["events"])
        self.assertNotIn("jellyfin_in_library", notifications["events"])
        self.assertEqual(notifications["templates"]["subscription_in_library"]["title"], "旧标题")
        self.assertNotIn("subscription_completed", notifications["templates"])
        self.assertEqual(persisted["events"], notifications["events"])
        self.assertEqual(persisted["templates"], notifications["templates"])


if __name__ == "__main__":
    unittest.main()
