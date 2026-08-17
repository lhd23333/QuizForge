"""开源版不读取历史激活数据的回归。"""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import config
import exporter
import service_ports

class OpenSourceAccessTests(unittest.TestCase):
    def test_missing_activation_does_not_block_export(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "activation.json"
            with (mock.patch.object(config, "ACTIVATION_PATH", path),
                  mock.patch.object(config, "SERVICE_PORTS_PATH", path.parent / "services.json"),
                  mock.patch.object(
                      service_ports.exporter, "export", return_value=Path("x.pdf")
                  ) as call):
                result = service_ports.export_document([{"id": "q1"}])
            self.assertEqual(result, Path("x.pdf"))
            self.assertFalse(path.exists())
            call.assert_called_once_with([{"id": "q1"}])

    def test_existing_activation_file_is_preserved_but_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "activation.json"
            original = b'{"entitlement":"legacy-token"}'
            path.write_bytes(original)
            with (mock.patch.object(config, "ACTIVATION_PATH", path),
                  mock.patch.object(config, "SERVICE_PORTS_PATH", path.parent / "services.json"),
                  mock.patch.object(
                      service_ports.exporter, "export", return_value=Path("x.zip")
                  )):
                result = service_ports.export_document([], fmt="zip")
            self.assertEqual(result, Path("x.zip"))
            self.assertEqual(path.read_bytes(), original)

    def test_cloud_tex_stays_disabled(self):
        with self.assertRaisesRegex(exporter.ExportError, "云 TeX 已停用"):
            service_ports.export_document([], tex_backend="cloud")


if __name__ == "__main__":
    unittest.main()
