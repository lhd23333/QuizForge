import json
import tempfile
import unittest
from pathlib import Path

import blocksplit
import converter
import qualcheck


class ConverterBoundaryTests(unittest.TestCase):
    @staticmethod
    def _write_content_list(root: Path, rows) -> None:
        (root / "final_content_list.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    def test_page_breaks_include_header_and_image_page_starts(self):
        raw = (
            "第一页唯一开头\n\n1. 第一页题目\n\n"
            "第二页页眉唯一\n\n2. 第二页题目\n\n"
            "![](images/page-three.png)\n\n3. 第三页题目"
        )
        rows = [
            {"type": "text", "text": "第一页唯一开头", "page_idx": 0},
            {"type": "page_header", "text": "第二页页眉唯一", "page_idx": 1},
            {"type": "text", "text": "2. 第二页题目", "page_idx": 1},
            {"type": "image", "img_path": "images/page-three.png",
             "page_idx": 2},
            {"type": "text", "text": "3. 第三页题目", "page_idx": 2},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_content_list(root, rows)
            separated, count, error = converter._inject_source_page_breaks(
                raw, root, 3)

        marker = blocksplit.SOURCE_PAGE_BREAK
        self.assertEqual("", error)
        self.assertEqual(2, count)
        self.assertIn(f"{marker}\n第二页页眉唯一", separated)
        self.assertIn(
            f"{marker}\n![](images/page-three.png)", separated)

    def test_page_zero_anchor_must_precede_later_page_anchors(self):
        raw = "第二页唯一开头\n\n第一页唯一开头"
        rows = [
            {"type": "text", "text": "第一页唯一开头", "page_idx": 0},
            {"type": "text", "text": "第二页唯一开头", "page_idx": 1},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_content_list(root, rows)
            separated, count, error = converter._inject_source_page_breaks(
                raw, root, 2)

        self.assertEqual(raw, separated)
        self.assertEqual(0, count)
        self.assertIn("逆序", error)

    def test_duplicate_page_start_preserves_raw_and_requires_review(self):
        raw = "第一页唯一开头\n\n重复页眉内容\n\n重复页眉内容\n\n2. 第二题"
        rows = [
            {"type": "text", "text": "第一页唯一开头", "page_idx": 0},
            {"type": "page_header", "text": "重复页眉内容", "page_idx": 1},
            {"type": "text", "text": "2. 第二题", "page_idx": 1},
        ]
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_content_list(root, rows)
            separated, meta = converter._apply_image_page_boundaries(
                raw, root, boundary_mode="whitelist", image_page_count=2,
                ocr_backend="mineru", note_sink=notes.append)

        self.assertEqual(raw, separated)
        self.assertEqual("unavailable", meta["source_page_boundary_status"])
        self.assertTrue(qualcheck.requires_manual_review(notes))

    def test_doc2x_multi_image_whitelist_never_guesses_page_breaks(self):
        raw = "1. 第一题\n\n2. 第二题"
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            separated, meta = converter._apply_image_page_boundaries(
                raw, Path(tmp), boundary_mode="whitelist",
                image_page_count=2, ocr_backend="doc2x",
                note_sink=notes.append)

        self.assertEqual(raw, separated)
        self.assertEqual("unavailable", meta["source_page_boundary_status"])
        self.assertIn("Doc2X", notes[0])
        self.assertTrue(qualcheck.requires_manual_review(notes))

    def test_whitelist_number_gap_does_not_trigger_forced_ocr(self):
        raw = "\n\n".join(
            f"{number}. 第{number}题正文"
            for number in [1, 2, *range(4, 20)]
        )

        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, _path, **kwargs):
                self.calls.append(kwargs)
                return raw, "full.md"

        client = FakeMineru()
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("缺题卷.pdf"), Path(tmp) / "published",
                boundary_mode="whitelist")

        self.assertEqual(raw, result)
        self.assertEqual(1, len(client.calls))
        self.assertNotIn("force_ocr", client.calls[0])

    def test_whitelist_replacement_character_still_triggers_forced_ocr(self):
        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, _path, **kwargs):
                self.calls.append(kwargs)
                text = ("1. 强制 OCR 后正文完整" if kwargs.get("force_ocr")
                        else "1. 文本层含�乱码")
                return text, "full.md"

        client = FakeMineru()
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("乱码卷.pdf"), Path(tmp) / "published",
                boundary_mode="whitelist")

        self.assertEqual("1. 强制 OCR 后正文完整", result)
        self.assertEqual(2, len(client.calls))
        self.assertTrue(client.calls[1]["force_ocr"])

    def test_custom_whitelist_template_keeps_math_noise_retry(self):
        noisy = (
            r"【第1题】题干 $\dot{\delta}_{\scriptsize V}:="
            r"\dot{\delta}_{\scriptsize V}$"
        )

        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, _path, **kwargs):
                self.calls.append(kwargs)
                text = ("【第1题】强制 OCR 后正文完整"
                        if kwargs.get("force_ocr") else noisy)
                return text, "full.md"

        client = FakeMineru()
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("自定义题号卷.pdf"), Path(tmp) / "published",
                boundary_mode="whitelist", num_template="【第x题】")

        self.assertEqual("【第1题】强制 OCR 后正文完整", result)
        self.assertEqual(2, len(client.calls))
        self.assertTrue(client.calls[1]["force_ocr"])

    def test_pending_boundary_mode_controls_review_render_order(self):
        raw = (
            "2. 第二题\n\n"
            f"{blocksplit.SOURCE_PAGE_BREAK}\n"
            "第二页页眉\n\n1. 第一题"
        )
        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "乱序题号_raw.md"
            raw_path.write_text(raw, encoding="utf-8")
            pending = converter.convert_collection_unit_to_blocks(
                raw_path, keep_images=False, boundary_mode="whitelist")

        self.assertEqual("whitelist", pending["boundary_mode"])
        self.assertFalse(any(
            blocksplit.SOURCE_PAGE_BREAK in block["text"]
            for block in pending["blocks"]))
        self.assertFalse(any(
            "第二页页眉" in block["text"] for block in pending["blocks"]))
        # 本用例只验证审核后的配对/顺序，不落语料或清理已退出的临时目录。
        pending["extract_dirs"] = []

        rendered = converter.finish_block_review(
            pending, action="skip", include_solution=False)

        self.assertLess(rendered.index("2. 第二题"), rendered.index("1. 第一题"))


if __name__ == "__main__":
    unittest.main()
