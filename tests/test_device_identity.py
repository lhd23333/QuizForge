"""设备随机身份的 Windows DPAPI 存储回归。"""

import os
from pathlib import Path
import tempfile
import unittest

import device_identity


@unittest.skipUnless(os.name == "nt", "DPAPI 仅在 Windows 桌面版启用")
class DeviceIdentityTests(unittest.TestCase):
    def test_round_trip_keeps_same_request_code(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "device_identity.dat"
            first = device_identity.get_or_create(path)
            second = device_identity.get_or_create(path)
            self.assertTrue(first.valid)
            self.assertEqual(first.device_id, second.device_id)
            self.assertEqual(device_identity.normalize_device_id(first.device_id), first.device_id)
            self.assertNotIn(first.device_id.encode("ascii"), path.read_bytes())

    def test_corrupt_identity_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "device_identity.dat"
            path.write_bytes(b"broken")
            before = path.read_bytes()
            state = device_identity.get_or_create(path)
            self.assertFalse(state.valid)
            self.assertEqual(path.read_bytes(), before)

    def test_invalid_request_code_is_rejected(self):
        with self.assertRaises(ValueError):
            device_identity.normalize_device_id("not-a-device-code")


if __name__ == "__main__":
    unittest.main()
