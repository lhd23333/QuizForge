"""MiKTeX 固定安装器的下载、校验、安装和路由边界。"""

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import config
import tex_installer


class _Response:
    def __init__(self, body: bytes, *, url: str = tex_installer.MIKTEX_URL):
        self.body = body
        self.url = url
        self.history = []
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        stream = io.BytesIO(self.body)
        while chunk := stream.read(chunk_size):
            yield chunk


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class TexInstallerTests(unittest.TestCase):
    def test_unsigned_official_installer_disables_one_click_install(self):
        with mock.patch.object(tex_installer, "find_miktex_tool", return_value=None):
            state = tex_installer.snapshot()
        self.assertFalse(state["available"])
        self.assertIn("Authenticode", state["blocked_reason"])
        with mock.patch.object(tex_installer.threading, "Thread") as thread:
            with self.assertRaisesRegex(tex_installer.TexInstallError, "安全关闭"):
                tex_installer.start_install()
        thread.assert_not_called()

    def test_download_requires_https_exact_size_and_fixed_hash(self):
        body = b"signed-installer-fixture"
        response = _Response(body)
        session = _Session(response)
        progress = []
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(tex_installer, "MIKTEX_SIZE", len(body)), \
                mock.patch.object(
                    tex_installer, "MIKTEX_SHA256", hashlib.sha256(body).hexdigest()
                ):
            destination = Path(td) / "miktex.exe"
            result = tex_installer.download_installer(
                destination, session=session,
                progress=lambda done, total: progress.append((done, total)),
            )
            self.assertEqual(body, result.read_bytes())
            self.assertEqual((len(body), len(body)), progress[-1])
            self.assertFalse(destination.with_suffix(".exe.part").exists())
        self.assertEqual(tex_installer.MIKTEX_URL, session.calls[0][0])
        self.assertTrue(session.calls[0][1]["allow_redirects"])

    def test_download_rejects_hash_mismatch_without_publishing_file(self):
        body = b"tampered"
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(tex_installer, "MIKTEX_SIZE", len(body)), \
                mock.patch.object(tex_installer, "MIKTEX_SHA256", "0" * 64):
            destination = Path(td) / "miktex.exe"
            with self.assertRaisesRegex(tex_installer.TexInstallError, "SHA-256"):
                tex_installer.download_installer(
                    destination, session=_Session(_Response(body))
                )
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".exe.part").exists())

    def test_download_rejects_non_https_redirect(self):
        body = b"fixture"
        response = _Response(body, url="http://mirror.example/miktex.exe")
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(tex_installer, "MIKTEX_SIZE", len(body)), \
                mock.patch.object(
                    tex_installer, "MIKTEX_SHA256", hashlib.sha256(body).hexdigest()
                ):
            with self.assertRaisesRegex(tex_installer.TexInstallError, "非 HTTPS"):
                tex_installer.download_installer(
                    Path(td) / "miktex.exe", session=_Session(response)
                )

    def test_authenticode_must_be_valid_and_have_thumbprint(self):
        valid = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "Status": "Valid", "Subject": "CN=MiKTeX",
                "Thumbprint": "ABC123",
            }),
            stderr="",
        )
        with mock.patch.object(tex_installer.os, "name", "nt"):
            signature = tex_installer.verify_authenticode(
                Path("miktex.exe"), runner=lambda *_args, **_kwargs: valid
            )
        self.assertEqual("ABC123", signature["Thumbprint"])

        invalid = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"Status": "HashMismatch", "Thumbprint": "ABC123"}),
            stderr="",
        )
        with mock.patch.object(tex_installer.os, "name", "nt"):
            with self.assertRaisesRegex(tex_installer.TexInstallError, "签名无效"):
                tex_installer.verify_authenticode(
                    Path("miktex.exe"), runner=lambda *_args, **_kwargs: invalid
                )

    def test_install_is_private_unattended_and_refreshes_tools(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        xelatex = Path(r"C:\Users\test\MiKTeX\xelatex.exe")
        dvisvgm = Path(r"C:\Users\test\MiKTeX\dvisvgm.exe")
        with mock.patch.object(
            tex_installer, "find_miktex_tool", side_effect=[xelatex, dvisvgm]
        ), mock.patch.object(config, "XELATEX", "xelatex"), \
                mock.patch.object(config, "DVISVGM", "dvisvgm"):
            result = tex_installer.install_miktex(Path("miktex.exe"), runner=runner)
            self.assertEqual(str(xelatex), config.XELATEX)
            self.assertEqual(str(dvisvgm), config.DVISVGM)
        self.assertEqual(xelatex, result)
        self.assertEqual(
            ["miktex.exe", "--unattended", "--private", "--package-set=basic"],
            calls[0][0],
        )
        self.assertFalse(calls[0][1]["check"])

    def test_settings_routes_start_and_report_install(self):
        from app import _WRITE_TOKEN, app

        state = {
            "status": "queued", "installed": False, "downloaded": 0,
            "total": tex_installer.MIKTEX_SIZE, "message": "准备下载",
            "error": "", "version": tex_installer.MIKTEX_VERSION,
            "xelatex": "", "available": True, "blocked_reason": "",
        }
        with mock.patch.object(tex_installer, "start_install", return_value=state), \
                mock.patch.object(tex_installer, "snapshot", return_value=state):
            client = app.test_client()
            started = client.post(
                "/settings/tex/install",
                headers={"X-CSRF-Token": _WRITE_TOKEN},
            )
            status = client.get("/settings/tex/status")
        self.assertEqual(202, started.status_code)
        self.assertEqual("queued", started.get_json()["install"]["status"])
        self.assertEqual("queued", status.get_json()["install"]["status"])


if __name__ == "__main__":
    unittest.main()
