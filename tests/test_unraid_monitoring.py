from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "deploy" / "unraid-frontend" / "monitoring"
COLLECTOR = MONITORING / "collect-moviemuse-health.sh"
SUMMARIZER = MONITORING / "summarize-moviemuse-health.sh"


def find_bash() -> str | None:
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)


class UnraidMonitoringLayoutTests(unittest.TestCase):
    def test_monitoring_files_have_clear_repository_boundary(self) -> None:
        self.assertTrue(COLLECTOR.is_file())
        self.assertTrue(SUMMARIZER.is_file())
        self.assertTrue((MONITORING / "README.md").is_file())

        project_map = (ROOT / "PROJECT_STRUCTURE.md").read_text(encoding="utf-8")
        self.assertIn("三个核心运行单元", project_map)
        self.assertIn("deploy/unraid-frontend/monitoring/", project_map)
        self.assertIn("/mnt/user/appdata/moviemuse/monitoring-data", project_map)

        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("PROJECT_STRUCTURE.md", agents)
        self.assertIn("Unraid 主机稳定性监控", agents)

        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", attributes)

    def test_collector_enforces_read_only_and_fixed_output_contract(self) -> None:
        source = COLLECTOR.read_text(encoding="utf-8")
        self.assertIn('DEFAULT_DATA_DIR="/mnt/user/appdata/moviemuse/monitoring-data"', source)
        self.assertIn("sqlite3 -readonly", source)
        self.assertIn("immutable=1", source)
        self.assertIn("PRAGMA query_only=ON", source)
        self.assertIn("flock -n", source)
        self.assertIn('--probe', source)

        forbidden = [
            "docker restart",
            "docker stop",
            "docker rm",
            "sessions.destroy",
            "VACUUM",
            "rm -rf",
        ]
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, source)

    def test_scripts_have_valid_bash_syntax(self) -> None:
        bash = find_bash()
        if not bash:
            self.skipTest("bash is not available")
        for script in (COLLECTOR, SUMMARIZER):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [bash, "-n"],
                    cwd=ROOT,
                    input=script.read_text(encoding="utf-8"),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_summarizer_is_read_only(self) -> None:
        source = SUMMARIZER.read_text(encoding="utf-8")
        forbidden = [">>", "rm ", "mv ", "mkdir ", "sqlite3 ", "docker "]
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, source)


if __name__ == "__main__":
    unittest.main()
