import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

import update_client


class _Response:
    def __init__(self, payload, url="https://updates.example.test/manifest.json"):
        self._payload = payload
        self._url = url
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self, size=-1):
        if not self._payload:
            return b""
        chunk = self._payload[:size]
        self._payload = self._payload[len(chunk):]
        return chunk


class UpdateClientTests(unittest.TestCase):
    def test_disabled_manifest_does_not_open_network(self):
        with mock.patch.object(update_client, "urlopen") as open_url:
            result = update_client.check("0.17.0-beta", "")
        self.assertFalse(result["enabled"])
        self.assertEqual("未配置更新地址", result["message"])
        open_url.assert_not_called()

    def test_valid_manifest_reports_new_version(self):
        payload = json.dumps({
            "latest_version": "0.18.0",
            "download_url": "https://download.example.test/qf.exe",
            "sha256": "a" * 64,
            "signer_thumbprint": "b" * 40,
            "notes": "修复本地导出",
        }).encode("utf-8")
        with mock.patch.object(update_client, "urlopen",
                               return_value=_Response(payload)) as open_url:
            result = update_client.check(
                "0.17.0-beta", "https://updates.example.test/manifest.json")
        self.assertTrue(result["available"])
        self.assertTrue(result["installable"])
        self.assertEqual("0.18.0", result["latest_version"])
        request = open_url.call_args.args[0]
        self.assertEqual("GET", request.method)
        self.assertNotIn("bank", request.headers)
        self.assertNotIn("token", request.headers)

    def test_http_manifest_is_rejected(self):
        with self.assertRaises(update_client.UpdateCheckError):
            update_client.check("0.17.0-beta", "http://updates.example.test/manifest.json")

    def test_missing_published_manifest_reports_no_update(self):
        error = HTTPError(
            "https://updates.example.test/manifest.json", 404,
            "Not Found", {}, None,
        )
        with mock.patch.object(update_client, "urlopen", side_effect=error):
            result = update_client.check(
                "0.17.0-beta", "https://updates.example.test/manifest.json")
        self.assertTrue(result["enabled"])
        self.assertFalse(result["available"])
        self.assertEqual("尚未发布可用更新", result["message"])

    def test_oversized_manifest_is_rejected(self):
        payload = b"{" + b"a" * update_client.MAX_MANIFEST_BYTES + b"}"
        with mock.patch.object(update_client, "urlopen",
                               return_value=_Response(payload)):
            with self.assertRaisesRegex(update_client.UpdateCheckError, "过大"):
                update_client.check(
                    "0.17.0-beta", "https://updates.example.test/manifest.json")

    def test_download_requires_matching_sha256(self):
        payload = b"signed installer bytes"
        response = _Response(payload, "https://download.example.test/qf.exe")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "setup.exe"
            with mock.patch.object(update_client, "urlopen", return_value=response):
                result = update_client._download_installer(
                    "https://download.example.test/qf.exe", target,
                    hashlib.sha256(payload).hexdigest(),
                )
            self.assertEqual(payload, result.read_bytes())

    def test_authenticode_requires_valid_pinned_certificate(self):
        with mock.patch.object(update_client, "_authenticode_signature", return_value={
            "Status": "Valid", "Thumbprint": "AB" * 20, "Subject": "CN=QuizForge",
        }):
            result = update_client.verify_authenticode(Path("setup.exe"), "ab" * 20)
        self.assertEqual("CN=QuizForge", result["Subject"])
        with mock.patch.object(update_client, "_authenticode_signature", return_value={
            "Status": "Valid", "Thumbprint": "CD" * 20,
        }):
            with self.assertRaisesRegex(update_client.UpdateCheckError, "证书"):
                update_client.verify_authenticode(Path("setup.exe"), "ab" * 20)

    def test_launcher_waits_for_parent_and_runs_silent_current_user_install(self):
        script = update_client._LAUNCHER_SCRIPT
        self.assertIn("Get-Process -Id $ParentPid", script)
        self.assertIn("'/VERYSILENT'", script)
        self.assertIn("'/CURRENTUSER'", script)
        self.assertIn("('/DIR=\"{0}\"' -f $InstallDir)", script)
        self.assertIn("Start-Process -FilePath $AppExe", script)


if __name__ == "__main__":
    unittest.main()
