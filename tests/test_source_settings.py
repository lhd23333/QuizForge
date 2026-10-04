"""本地来源配置的基础约束。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import source_settings


class SourceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.multiple(
            config,
            BANK_DIR=self.root / "bank",
            SOURCE_SETTINGS_PATH=self.root / "state" / "source_settings.json",
            SOURCE_WORKSPACE_DIR=self.root / "state" / "source_workspace",
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_defaults_use_bank_categories_and_are_disabled(self):
        profiles = source_settings.load_profiles()
        self.assertEqual(set(profiles), {"好题", "好卷", "好资料"})
        for name in profiles:
            self.assertEqual(profiles[name]["input_dir"], str(config.BANK_DIR / name))
            self.assertEqual(profiles[name]["output_dir"], str(config.BANK_DIR / name))
            self.assertFalse(profiles[name]["enabled"])

    def test_custom_local_paths_are_created_and_persisted(self):
        input_dir = self.root / "外接盘" / "输入"
        output_dir = self.root / "外接盘" / "输出"
        saved = source_settings.save_profile(
            "自定义", input_dir=input_dir, output_dir=output_dir, enabled=True)
        self.assertTrue(input_dir.is_dir())
        self.assertTrue(output_dir.is_dir())
        self.assertEqual(source_settings.load_profiles()["自定义"], saved)

    def test_unc_and_file_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            source_settings.validate_local_directory(r"\\server\share")
        file_path = self.root / "file.txt"
        file_path.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            source_settings.validate_local_directory(file_path)

    def test_symlink_that_escapes_workspace_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "state" / "source_workspace" / "escape"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink is unavailable")
        with self.assertRaises(ValueError):
            source_settings.validate_local_directory(link)

    def test_workspace_symlink_itself_is_rejected(self):
        target = self.root / "real-workspace"
        target.mkdir()
        link = self.root / "workspace-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink is unavailable")
        with mock.patch.object(config, "SOURCE_WORKSPACE_DIR", link):
            with self.assertRaises(ValueError):
                source_settings.validate_local_directory(target / "inside")

    def test_corrupt_settings_are_quarantined_and_never_overwritten(self):
        path = config.SOURCE_SETTINGS_PATH
        path.parent.mkdir(parents=True)
        original = b"{broken"
        path.write_bytes(original)
        with self.assertRaises(source_settings.SourceSettingsCorruptError):
            source_settings.save_profile(
                "x", input_dir=self.root / "input", output_dir=self.root / "output",
                enabled=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(path.name + ".corrupt-*")), [
            mock.ANY
        ])
        self.assertFalse((self.root / "input").exists())

    def test_invalid_profile_path_is_skipped_and_audited_without_path_details(self):
        path = config.SOURCE_SETTINGS_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"profiles": {
            "unsafe": {"input_dir": r"\\host\share", "output_dir": str(self.root),
                       "enabled": True},
        }}), encoding="utf-8")
        with self.assertLogs("source_settings", level="WARNING") as captured:
            profiles = source_settings.load_profiles()
        self.assertNotIn("unsafe", profiles)
        self.assertTrue(any("profile rejected" in line for line in captured.output))
        self.assertNotIn("host", "\n".join(captured.output))
        audit = path.with_suffix(".audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("invalid_directory", audit)
        self.assertNotIn("host", audit)

    def test_saving_another_profile_preserves_raw_invalid_entry(self):
        path = config.SOURCE_SETTINGS_PATH
        path.parent.mkdir(parents=True)
        raw_entry = {"input_dir": r"\\host\share", "output_dir": "bad", "enabled": True}
        path.write_text(json.dumps({"profiles": {"unsafe": raw_entry}}), encoding="utf-8")
        source_settings.save_profile(
            "new", input_dir=self.root / "input", output_dir=self.root / "output",
            enabled=True)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["profiles"]["unsafe"], raw_entry)
        self.assertIn("new", persisted["profiles"])


if __name__ == "__main__":
    unittest.main()
