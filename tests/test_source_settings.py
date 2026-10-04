"""本地来源配置的基础约束。"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
