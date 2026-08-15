"""发布者签发器与本地许可证台账回归。"""

import argparse
from contextlib import closing, redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import config
from tools import license_admin, license_signer


DEVICE_ID = "QFD1-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAA"


def _issue_args(private_key: Path) -> argparse.Namespace:
    return argparse.Namespace(
        private_key=private_key,
        output=Path("unused.qflicense"),
        licensee="一周内测用户",
        device_id=DEVICE_ID,
        license_id="",
        edition="beta",
        issued="2026-08-12",
        not_before="",
        valid_days=license_signer.DEFAULT_VALID_DAYS,
        expires="",
        perpetual=False,
        updates_until="",
        feature=["export"],
        password_env="",
        no_password=True,
    )


class LicenseToolTests(unittest.TestCase):
    def _keys(self, root: Path) -> tuple[Path, Path]:
        private = root / "keys" / "license_private.pem"
        public = root / "keys" / "license_public.pem"
        license_signer.init_key(private, public, None)
        return private, public

    def test_signer_defaults_to_seven_calendar_days(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            private, public = self._keys(root)
            with mock.patch.object(config, "LICENSE_PUBLIC_KEY_PATH", public):
                document = license_signer.issue_license(_issue_args(private))
            payload = document["payload"]
            days = (
                date.fromisoformat(payload["expires_at"])
                - date.fromisoformat(payload["not_before"])
            ).days + 1
            self.assertEqual(days, 7)
            self.assertEqual(payload["device_id"], DEVICE_ID)

    def test_admin_records_issued_license_outside_repository(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _private, public = self._keys(root)
            args = argparse.Namespace(
                command="issue",
                publisher_dir=str(root),
                licensee="台账测试用户",
                device_id=DEVICE_ID,
                edition="beta",
                feature=None,
                note="默认一周",
                issued="2026-08-12",
                not_before="",
                valid_days=7,
                expires="",
                perpetual=False,
                updates_until="",
                password_env="",
                no_password=True,
            )
            with (mock.patch.object(config, "LICENSE_PUBLIC_KEY_PATH", public),
                  redirect_stdout(StringIO())):
                self.assertEqual(license_admin._run(args), 0)
            with closing(sqlite3.connect(root / license_admin.DB_NAME)) as connection:
                row = connection.execute(
                    "SELECT licensee, status, expires_at, file_path FROM licenses"
                ).fetchone()
            self.assertEqual(row[:3], ("台账测试用户", "active", "2026-08-18"))
            self.assertTrue((root / row[3]).is_file())

    def test_admin_can_adopt_the_key_pair_already_used_by_client(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_private, source_public = self._keys(root / "source")
            publisher = root / "publisher"
            args = argparse.Namespace(
                command="adopt-key",
                publisher_dir=str(publisher),
                private_key_source=source_private,
                public_key_source=source_public,
                password_env="",
                no_password=True,
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(license_admin._run(args), 0)
            adopted_private, adopted_public = license_admin._key_paths(publisher)
            self.assertEqual(adopted_private.read_bytes(), source_private.read_bytes())
            self.assertEqual(adopted_public.read_bytes(), source_public.read_bytes())
            self.assertTrue((publisher / license_admin.DB_NAME).is_file())

    def test_admin_rejects_publisher_directory_inside_source_tree(self):
        with self.assertRaisesRegex(ValueError, "源码仓库之外"):
            license_admin._publisher_dir(str(license_admin.PROJECT_DIR / "data" / "publisher"))


if __name__ == "__main__":
    unittest.main()
