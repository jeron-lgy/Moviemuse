from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.javdb_service import JavDBService


class JavDBServiceLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="moviemuse-javdb-service-"))
        with patch.dict(
            "os.environ",
            {
                "JAVDB_CACHE_DIR": str(self.root / "cache"),
                "JAVDB_QUEUE_MAX_SIZE": "2",
            },
        ):
            self.service = JavDBService()

    def tearDown(self) -> None:
        self.service.close(timeout=2)
        shutil.rmtree(self.root, ignore_errors=True)

    def test_worker_starts_lazily_and_can_restart_after_shutdown(self) -> None:
        self.assertFalse(self.service.stats()["worker_running"])
        self.assertEqual(self.service._run(lambda: "first"), "first")
        self.assertTrue(self.service.stats()["worker_running"])
        self.assertTrue(self.service.close(timeout=2))
        self.assertFalse(self.service.stats()["accepting"])

        self.service.start()
        self.assertEqual(self.service._run(lambda: "second"), "second")
        self.assertTrue(self.service.stats()["worker_running"])


if __name__ == "__main__":
    unittest.main()
