"""历史离线许可证兼容逻辑与开源版隔离回归。"""

from base64 import b64encode
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import config
import device_identity
import license_manager
import service_ports


DEVICE_ID = "QFD1-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAA"
OTHER_DEVICE_ID = "QFD1-BAAAAAAA-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAAAAAA-AAAA"


class LicenseFixture:
    def __init__(self, root: Path):
        self.root = root
        self.private = Ed25519PrivateKey.generate()
        self.public_path = root / "license_public_key.pem"
        self.license_path = root / "license.qflicense"
        self.public_path.write_bytes(self.private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ))

    def document(self, *, schema: int = 2, **patch) -> dict:
        payload = {
            "product": "quizforge",
            "license_id": "beta-test-001",
            "licensee": "内部测试者",
            "edition": "beta",
            "device_id": DEVICE_ID,
            "issued_at": "2026-08-01",
            "not_before": "2026-08-01",
            "expires_at": "2027-02-28",
            "updates_until": "2027-02-28",
            "features": ["export"],
        }
        if schema == license_manager.LEGACY_LICENSE_SCHEMA:
            payload.pop("device_id")
        payload.update(patch)
        signature = self.private.sign(license_manager.canonical_payload(payload))
        return {
            "schema": schema,
            "payload": payload,
            "signature": b64encode(signature).decode("ascii"),
        }

    def raw(self, *, schema: int = 2, **patch) -> bytes:
        return json.dumps(
            self.document(schema=schema, **patch), ensure_ascii=False
        ).encode("utf-8")

    def patch_config(self):
        return mock.patch.multiple(
            config,
            LICENSE_PUBLIC_KEY_PATH=self.public_path,
            LICENSE_PATH=self.license_path,
        )

    def identity(self) -> device_identity.DeviceIdentity:
        return device_identity.DeviceIdentity(
            True, "ready", "设备身份可用", "test", DEVICE_ID
        )


class OfflineLicenseTests(unittest.TestCase):
    def test_valid_signed_license_for_expected_device(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LicenseFixture(Path(td))
            with fixture.patch_config():
                state = license_manager.verify_bytes(
                    fixture.raw(), today=date(2026, 8, 11),
                    expected_device_id=DEVICE_ID, require_device=True,
                )
            self.assertTrue(state.valid)
            self.assertEqual(state.licensee, "内部测试者")
            self.assertEqual(state.device_id, DEVICE_ID)
            self.assertTrue(license_manager.export_allowed(state))

    def test_wrong_device_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LicenseFixture(Path(td))
            with fixture.patch_config():
                state = license_manager.verify_bytes(
                    fixture.raw(), expected_device_id=OTHER_DEVICE_ID,
                    require_device=True,
                )
            self.assertEqual(state.status, "wrong_device")

    def test_legacy_license_only_works_when_device_binding_is_not_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LicenseFixture(Path(td))
            with fixture.patch_config():
                source_state = license_manager.verify_bytes(
                    fixture.raw(schema=1), require_device=False
                )
                desktop_state = license_manager.verify_bytes(
                    fixture.raw(schema=1), expected_device_id=DEVICE_ID,
                    require_device=True,
                )
            self.assertTrue(source_state.valid)
            self.assertEqual(desktop_state.status, "device_required")

    def test_tampering_and_expiry_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LicenseFixture(Path(td))
            document = fixture.document()
            document["payload"]["licensee"] = "被篡改"
            with fixture.patch_config():
                tampered = license_manager.verify_bytes(
                    json.dumps(document).encode("utf-8"), today=date(2026, 8, 11)
                )
                expired = license_manager.verify_bytes(
                    fixture.raw(expires_at="2026-08-10"), today=date(2026, 8, 11)
                )
            self.assertEqual(tampered.status, "invalid")
            self.assertEqual(expired.status, "expired")

    def test_invalid_import_does_not_replace_current_license(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LicenseFixture(Path(td))
            with fixture.patch_config():
                self.assertTrue(license_manager.install(fixture.raw()).valid)
                original = fixture.license_path.read_bytes()
                state = license_manager.install(b"not-json")
            self.assertFalse(state.valid)
            self.assertEqual(fixture.license_path.read_bytes(), original)

    def test_historical_license_environment_cannot_gate_export(self):
        with tempfile.TemporaryDirectory() as td:
            service_path = Path(td) / "missing-services.json"
            with (mock.patch.object(config, "SERVICE_PORTS_PATH", service_path),
                  mock.patch.dict("os.environ", {"QUIZFORGE_LICENSE_ENFORCED": "1"}),
                  mock.patch.object(
                      service_ports.exporter, "export", return_value=Path("x.zip")
                  ) as call):
                result = service_ports.export_document([{"id": "q1"}], title="测试")
            self.assertEqual(result, Path("x.zip"))
            call.assert_called_once()

    def test_settings_has_no_legacy_license_write_route(self):
        from app import _WRITE_TOKEN, app

        with tempfile.TemporaryDirectory() as td:
            fixture = LicenseFixture(Path(td))
            fixture.license_path.write_bytes(b"legacy-license")
            with fixture.patch_config():
                response = app.test_client().post(
                    "/settings/license",
                    headers={"X-CSRF-Token": _WRITE_TOKEN},
                )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(fixture.license_path.read_bytes(), b"legacy-license")

    def test_settings_page_does_not_expose_legacy_license_controls(self):
        import app as quiz_app

        response = quiz_app.app.test_client().get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b".qflicense", response.data)
        self.assertNotIn(b"settings-license.js", response.data)
        self.assertNotIn("邀请码".encode(), response.data)
        self.assertNotIn(b"activation.js", response.data)
        self.assertIn(b"GPL-3.0", response.data)

    def test_legacy_activation_and_cloud_routes_are_removed(self):
        import app as quiz_app

        client = quiz_app.app.test_client()
        headers = {"X-CSRF-Token": quiz_app._WRITE_TOKEN}
        self.assertEqual(client.post("/settings/activation", headers=headers).status_code, 404)
        self.assertEqual(client.post("/settings/cloud", headers=headers).status_code, 404)


if __name__ == "__main__":
    unittest.main()
