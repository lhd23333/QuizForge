from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import source_profiles


class SourceProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "圆梦杯.png"
        self.source.write_bytes(b"image")

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_number_removes_only_main_number(self):
        number, body = source_profiles.extract_question_number(
            "11. 已知函数\n（1）求值\nA. 1")
        self.assertEqual(number, 11)
        self.assertEqual(body, "已知函数\n（1）求值\nA. 1")

    def test_good_question_uses_source_name_and_review_warning(self):
        workspace = self.root / "stage"
        with mock.patch.object(source_profiles, "_ocr",
                               return_value="11. 已知函数\n（1）求值\nA. 1"):
            result = source_profiles.convert_good_question(
                self.source, workspace, ocr_backend="mineru", engine="block")
        card = workspace / "圆梦杯第11题.md"
        self.assertTrue(card.exists())
        self.assertIn("已知函数", card.read_text(encoding="utf-8"))
        self.assertNotIn("11. 已知", card.read_text(encoding="utf-8"))
        self.assertIn("（1）", card.read_text(encoding="utf-8"))
        self.assertEqual(result["warnings"], [])

    def test_good_paper_and_material_write_independent_cards_with_metadata(self):
        raw = "1. 题目一\n【解析】因为一\n2. 题目二"
        with mock.patch.object(source_profiles, "_ocr", return_value=raw):
            paper = source_profiles.convert_good_paper(
                self.source, self.root / "paper", ocr_backend="mineru", engine="block")
        self.assertEqual(len(paper["cards"]), 3)
        with mock.patch.object(source_profiles, "_ocr",
                               return_value="正文段落"):
            material = source_profiles.convert_good_material(
                self.source, self.root / "material", ocr_backend="mineru", engine="block")
        material_cards = list((self.root / "material" / "圆梦杯").glob("*.md"))
        self.assertEqual(len(material_cards), 1)
        text = material_cards[0].read_text(encoding="utf-8")
        self.assertIn("source_block_type", text)
        self.assertIn("source_version_id", text)

    def test_manifest_has_relative_sha256_files_and_names_are_safe(self):
        self.assertTrue(source_profiles.sanitize_output_name("CON?.pdf").startswith("CON"))
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "card.md").write_text("card", encoding="utf-8")
        manifest = source_profiles.build_output_manifest("good_question", self.source, stage)
        self.assertEqual(manifest["files"][0]["path"], "card.md")
        self.assertEqual(len(manifest["files"][0]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
