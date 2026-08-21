from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path


class TranscodeTimestampTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="moviemuse-timestamp-test-"))
        os.environ["APP_DATA_DIR"] = str(self.root / "data")
        import app.main as main

        self.main = main

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_copy_transcode_output_timestamps_preserves_modified_time(self) -> None:
        source = self.root / "source.mp4"
        target = self.root / "target.mp4"
        source.write_bytes(b"source")
        target.write_bytes(b"target")
        source_mtime = time.time() - 86400
        source_atime = source_mtime - 60
        os.utime(source, (source_atime, source_mtime))

        result = self.main.copy_transcode_output_timestamps(source, target)

        self.assertTrue(result["ok"])
        self.assertAlmostEqual(target.stat().st_mtime, source.stat().st_mtime, delta=1.0)
        self.assertIn("creation_time_copied", result)
        if os.name == "nt":
            self.assertTrue(result["creation_time_copied"], result.get("creation_time_error"))


if __name__ == "__main__":
    unittest.main()
