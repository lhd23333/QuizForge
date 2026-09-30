"""统一 API 配置层回归测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import api_config
import config
import doc2x_store
import providers


class ApiConfigLayerTests(unittest.TestCase):
    def test_aggregation_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(config, "PROVIDERS_PATH",
                                   root / "providers.json"):
                pid = providers.add_llm_provider(
                    "中转", "http://127.0.0.1:3425/v1", "enc-secret",
                    "deepseek/deepseek-flash", 8192, purposes=("md",))
                rows = api_config.list_api_configs()
                llm = [row for row in rows if row["id"] == f"llm:{pid}"]
                self.assertEqual(len(llm), 1)
                row = llm[0]
                self.assertEqual(row["transport"], "magpie")
                self.assertTrue(row["active"])
                self.assertEqual(row["secret_state"], "stored")
                self.assertNotIn("enc-secret", repr(rows))

    def test_upsert_new_magpie_llm_uses_placeholder_key(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(config, "PROVIDERS_PATH",
                                   root / "providers.json"), \
                    mock.patch.object(api_config.crypto_utils, "encrypt_token",
                                      return_value="enc"):
                result = api_config.upsert_api_config(
                    purpose="md", name="网关",
                    base_url="http://127.0.0.1:3425/v1",
                    model="deepseek/deepseek-flash")
                self.assertTrue(result["created"])
                row = api_config.get_api_config(result["id"])
                self.assertIsNotNone(row)
                self.assertEqual(row["secret_state"], "stored")
                self.assertEqual(row["transport"], "magpie")

    def test_upsert_public_llm_without_secret_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(config, "PROVIDERS_PATH",
                                   root / "providers.json"):
                with self.assertRaises(api_config.ApiConfigError):
                    api_config.upsert_api_config(
                        purpose="md", name="云服务",
                        base_url="https://api.example.com/v1", model="m")

    def test_set_active_and_delete_llm(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(config, "PROVIDERS_PATH",
                                   root / "providers.json"):
                first = providers.add_llm_provider(
                    "A", "http://127.0.0.1:3425/v1", "enc", "m1", 8192,
                    purposes=("md",))
                second = providers.add_llm_provider(
                    "B", "http://127.0.0.1:3425/v1", "enc", "m2", 8192,
                    purposes=())
                api_config.set_active_api_config(f"llm:{second}", purpose="md")
                active = providers.get_active_llm_provider("md")
                self.assertEqual(active["id"], second)
                api_config.delete_api_config(f"llm:{first}")
                ids = {row["id"] for row in api_config.list_api_configs()}
                self.assertNotIn(f"llm:{first}", ids)

    def test_upsert_doc2x_returns_entry_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(config, "DOC2X_LOCAL_KEY_PATH",
                                   root / "doc2x_local.json"), \
                    mock.patch.object(config, "DOC2X_KEY_PATH",
                                      root / "doc2x.json"), \
                    mock.patch.object(doc2x_store.crypto_utils,
                                      "encrypt_token", return_value="enc"):
                result = api_config.upsert_api_config(
                    purpose="ocr-doc2x", secret="plain-key", label="主账号")
                self.assertTrue(result["created"])
                self.assertTrue(result["id"].startswith("doc2x:"))
                row = api_config.get_api_config(result["id"])
                self.assertEqual(row["protocol"], "doc2x")
                self.assertEqual(row["name"], "主账号")


if __name__ == "__main__":
    unittest.main()
