"""界面主题偏好的默认值、兼容和输入校验回归测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ui_prefs


class UiPrefsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.prefs_path = Path(self.temp_dir.name) / "ui_prefs.json"
        self.path_patch = patch.object(ui_prefs.config, "UI_PREFS_PATH", self.prefs_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def write_prefs(self, value):
        self.prefs_path.write_text(json.dumps(value), encoding="utf-8")

    def test_new_user_uses_soft_graphite_dark_defaults(self):
        self.assertEqual(
            ui_prefs.load(),
            {"theme_mode": "dark", "theme_color": "#63b3ff", "wallpaper": None},
        )

    def test_light_mode_without_explicit_color_uses_light_default(self):
        self.write_prefs({"theme_mode": "light", "wallpaper": "wallpaper.png"})
        prefs = ui_prefs.load()
        self.assertEqual(prefs["theme_mode"], "light")
        self.assertEqual(prefs["theme_color"], "#2457d6")
        self.assertFalse(ui_prefs.is_custom_theme_color(prefs["theme_mode"], prefs["theme_color"]))
        self.assertEqual(prefs["wallpaper"], "wallpaper.png")

    def test_invalid_mode_and_color_fall_back_to_dark_defaults(self):
        self.write_prefs({"theme_mode": "sepia", "theme_color": "red"})
        prefs = ui_prefs.load()
        self.assertEqual(prefs["theme_mode"], "dark")
        self.assertEqual(prefs["theme_color"], "#63b3ff")

    def test_invalid_wallpaper_values_are_ignored(self):
        self.write_prefs({"wallpaper": ["wallpaper.mp4"], "theme_mode": "dark"})
        prefs = ui_prefs.load()
        self.assertIsNone(prefs["wallpaper"])
        self.assertFalse(ui_prefs.is_video_wallpaper(prefs["wallpaper"]))

    def test_video_wallpaper_check_rejects_non_strings(self):
        self.assertFalse(ui_prefs.is_video_wallpaper({"name": "wallpaper.mp4"}))

    def test_explicit_custom_color_is_preserved(self):
        self.write_prefs({"theme_mode": "light", "theme_color": "#0EA5E9"})
        prefs = ui_prefs.load()
        self.assertEqual(prefs["theme_color"], "#0ea5e9")
        self.assertTrue(ui_prefs.is_custom_theme_color("light", prefs["theme_color"]))

    def test_update_rejects_invalid_values_without_writing_css_payloads(self):
        ui_prefs.update(theme_mode="invalid", theme_color="red")
        self.assertEqual(ui_prefs.load()["theme_mode"], "dark")
        self.assertEqual(ui_prefs.load()["theme_color"], "#63b3ff")

    def test_accent_text_color_keeps_accessible_default_and_derives_custom_light_color(self):
        self.assertEqual(
            ui_prefs.accent_text_color("#2457d6", "light"),
            "#2457d6",
        )
        derived = ui_prefs.accent_text_color("#63b3ff", "light")
        self.assertNotEqual(derived, "#63b3ff")
        self.assertGreaterEqual(
            ui_prefs._contrast_ratio(derived, "#ffffff"),
            4.5,
        )

    def test_accent_text_color_handles_invalid_mode_and_color(self):
        self.assertEqual(
            ui_prefs.accent_text_color("not-a-color", "sepia"),
            ui_prefs.accent_text_color("#63b3ff", "dark"),
        )

    def test_accent_foreground_uses_strict_aa_candidate_at_boundary(self):
        for color in ("#2a867d", "#ae38f7"):
            foreground = ui_prefs.accent_foreground(color)
            self.assertGreaterEqual(
                ui_prefs._contrast_ratio(foreground, color),
                4.5,
            )


if __name__ == "__main__":
    unittest.main()
