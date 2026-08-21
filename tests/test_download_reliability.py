from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from app.log_service import AppLogService
from app.mteam_service import download_mteam_torrent


class DownloadReliabilityTest(unittest.TestCase):
    def test_mteam_download_retries_transient_server_error(self) -> None:
        request = httpx.Request("POST", "https://api.example/api/torrent/genDlToken")
        server_error = httpx.Response(503, request=request)
        token_ok = httpx.Response(200, request=request, json={"message": "SUCCESS", "data": "https://download.example/file"})
        torrent_request = httpx.Request("GET", "https://download.example/file")
        torrent_ok = httpx.Response(
            200,
            request=torrent_request,
            content=b"torrent-bytes",
            headers={"content-disposition": 'attachment; filename="sample.torrent"'},
        )
        client = MagicMock()
        client.__enter__.return_value = client
        client.post.side_effect = [server_error, token_ok]
        client.get.return_value = torrent_ok
        settings = {"mteam": {"api_url": "https://api.example/api/torrent/search", "api_key": "secret"}}

        with patch("app.mteam_service.httpx.Client", return_value=client), \
            patch("app.mteam_service.time.sleep") as sleep:
            content, filename = download_mteam_torrent("123", settings)

        self.assertEqual(content, b"torrent-bytes")
        self.assertEqual(filename, "sample.torrent")
        self.assertEqual(client.post.call_count, 2)
        sleep.assert_called_once()

    def test_mteam_download_labels_forbidden_as_configuration_error(self) -> None:
        def forbidden(url: str, **_kwargs: object) -> httpx.Response:
            return httpx.Response(403, request=httpx.Request("POST", url))

        client = MagicMock()
        client.__enter__.return_value = client
        client.post.side_effect = forbidden
        settings = {"mteam": {"site_url": "https://zp.example/", "api_key": "secret"}}

        with patch("app.mteam_service.httpx.Client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "API Key 无下载令牌权限"):
                download_mteam_torrent("123", settings)

    def test_system_log_rotates_and_recent_reads_current_tail(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="moviemuse-log-test-"))
        service = AppLogService(root)
        payload = {"text": "x" * 700_000}
        try:
            with patch.dict(os.environ, {"SYSTEM_LOG_MAX_BYTES": str(1024 * 1024), "SYSTEM_LOG_BACKUPS": "2"}):
                service.write("info", "test", "first", payload)
                service.write("info", "test", "second", payload)
            self.assertTrue((root / "system_logs.jsonl.1").exists())
            recent = service.recent(1)
            self.assertEqual(recent[0]["message"], "second")
            json.dumps(recent[0], ensure_ascii=False)
        finally:
            for path in root.glob("*"):
                path.unlink()
            root.rmdir()


if __name__ == "__main__":
    unittest.main()
