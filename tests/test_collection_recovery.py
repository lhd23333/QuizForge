"""合集缺题局部 MinerU 恢复回归：只用合成布局与 PDF，不调用网络。"""

from __future__ import annotations

import dataclasses
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from pypdf import PdfReader, PdfWriter

import collection_recovery
import collection_structure
import converter
import imgorder
import blockpipe
import blocksplit
import qualcheck


def _row(text: str, y0: float, y1: float, *, kind: str = "text") -> dict:
    return {"type": kind, "bbox": [0.05, y0, 0.95, y1], "content": text}


def _model_payload(*, cross_page: bool = False) -> list[list[dict]]:
    first = [
        _row("精练一：运动学基础", 0.03, 0.06, kind="doc_title"),
        _row("一、单选题", 0.07, 0.09, kind="paragraph_title"),
        _row("1. 第一题正文有足够多且唯一的文字用于定位锚点甲乙丙丁", 0.10, 0.18),
        _row("2. 第二题正文有足够多且唯一的文字用于定位前锚点甲乙丙丁", 0.25, 0.32),
        # 第 3 题题号被 OCR 吃掉，但其版面仍位于 2 与 4 之间。
        _row("这段布局属于漏掉题号的第三题正文与配图", 0.36, 0.52),
    ]
    second = []
    if cross_page:
        second.extend([
            _row("4. 第四题正文有足够多且唯一的文字用于定位后锚点甲乙丙丁", 0.20, 0.30),
            _row("5. 第五题正文有足够多且唯一的文字用于确认连续性甲乙丙丁", 0.55, 0.63),
        ])
    else:
        first.extend([
            _row("4. 第四题正文有足够多且唯一的文字用于定位后锚点甲乙丙丁", 0.60, 0.68),
            _row("5. 第五题正文有足够多且唯一的文字用于确认连续性甲乙丙丁", 0.80, 0.88),
        ])
    second.extend([
        _row("精练二：牛顿定律", 0.04, 0.07, kind="doc_title"),
        _row("1. 另一单元第一题正文足够长并且不会误匹配", 0.10, 0.20),
        _row("2. 另一单元第二题正文足够长并且不会误匹配", 0.30, 0.40),
        _row("3. 另一单元第三题正文足够长并且不会误匹配", 0.50, 0.60),
    ])
    return [first, second]


def _markdown_units():
    raw = (
        "# 精练一：运动学基础\n\n一、单选题\n\n"
        "1. 第一题正文有足够多且唯一的文字用于定位锚点甲乙丙丁\n\n"
        "2. 第二题正文有足够多且唯一的文字用于定位前锚点甲乙丙丁\n\n"
        "4. 第四题正文有足够多且唯一的文字用于定位后锚点甲乙丙丁\n\n"
        "5. 第五题正文有足够多且唯一的文字用于确认连续性甲乙丙丁\n\n"
        "# 精练二：牛顿定律\n\n"
        "1. 另一单元第一题正文足够长并且不会误匹配\n\n"
        "2. 另一单元第二题正文足够长并且不会误匹配\n\n"
        "3. 另一单元第三题正文足够长并且不会误匹配\n"
    )
    return collection_structure.split_markdown_units(raw)


def _write_fixture(root: Path, *, cross_page: bool = False) -> tuple[Path, Path]:
    model = root / "fixture_model.json"
    model.write_text(json.dumps(_model_payload(cross_page=cross_page),
                                ensure_ascii=False), encoding="utf-8")
    pdf = root / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=1000)
    writer.add_blank_page(width=600, height=1000)
    with pdf.open("wb") as stream:
        writer.write(stream)
    return model, pdf


class LayoutAndCropTests(unittest.TestCase):
    def test_locates_page_by_unique_cjk_window_without_title(self):
        question = (
            "7. 如图竖直放置的透明硬质管弯制成螺线结构，将光滑小球从上端"
            "由静止释放，研究它重复圆周运动的时间与速度变化规律（ ）")
        page_keys = (
            collection_recovery._key("第一页是完全无关的运动学题目"),
            collection_recovery._key("本页前文" + question + "本页后文"),
            collection_recovery._key("末页是完全无关的实验题"),
        )
        with mock.patch.object(
                collection_recovery, "_pdf_page_keys",
                return_value=page_keys):
            page = collection_recovery.locate_unique_question_page(
                "unused.pdf", question)

        self.assertEqual(1, page)

    def test_rejects_page_location_when_long_signature_is_ambiguous(self):
        question = (
            "7. 如图竖直放置的透明硬质管弯制成螺线结构，将光滑小球从上端"
            "由静止释放，研究它重复圆周运动的时间与速度变化规律（ ）")
        key = collection_recovery._key(question)
        with mock.patch.object(
                collection_recovery, "_pdf_page_keys",
                return_value=(key, key)):
            with self.assertRaisesRegex(
                    collection_recovery.CollectionRecoveryError,
                    "唯一定位到 0 个"):
                collection_recovery.locate_unique_question_page(
                    "unused.pdf", question)

    def test_exports_one_selected_pdf_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "three-pages.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=400)
            writer.add_blank_page(width=500, height=600)
            writer.add_blank_page(width=700, height=800)
            with source.open("wb") as stream:
                writer.write(stream)

            output = collection_recovery.export_pdf_page(
                source, 1, root / "out" / "page.pdf")
            result = PdfReader(str(output))

        self.assertEqual(1, len(result.pages))
        self.assertAlmostEqual(500.0, float(result.pages[0].mediabox.width))
        self.assertAlmostEqual(600.0, float(result.pages[0].mediabox.height))

    def test_exports_bounded_horizontal_prefix_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "one-page.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=600, height=800)
            with source.open("wb") as stream:
                writer.write(stream)

            output = collection_recovery.export_horizontal_prefix_crop(
                source, 0.625, root / "out" / "left-column.pdf")
            result = PdfReader(str(output))

        self.assertEqual(1, len(result.pages))
        self.assertAlmostEqual(375.0, float(result.pages[0].mediabox.width))
        self.assertAlmostEqual(800.0, float(result.pages[0].mediabox.height))

    def test_loads_chinese_title_units_and_question_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            model, _ = _write_fixture(Path(tmp))
            document = collection_recovery.load_layout_document(model)

        self.assertEqual(2, len(document.units))
        # collection_structure 会把全角标点做 NFKC 归一化。
        self.assertEqual("精练一:运动学基础", document.units[0].title)
        self.assertEqual([1, 2, 4, 5],
                         [q.number for q in document.units[0].questions])

    def test_exports_same_page_crop_from_previous_to_next_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, pdf = _write_fixture(root)
            crops = collection_recovery.create_gap_recovery_crops(
                pdf, model, _markdown_units()[0], [3], root / "out")
            result = PdfReader(str(crops[0].path))

            self.assertEqual(1, len(result.pages))
            self.assertAlmostEqual(350.0, float(result.pages[0].mediabox.height))
            self.assertAlmostEqual(600.0, float(result.pages[0].mediabox.width))
            self.assertEqual((3,), crops[0].plan.missing_numbers)
            self.assertEqual(0, crops[0].plan.slices[0].page_index)
            self.assertAlmostEqual(0.25, crops[0].plan.slices[0].top)
            self.assertAlmostEqual(0.60, crops[0].plan.slices[0].bottom)

    def test_cross_page_is_explicit_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, pdf = _write_fixture(root, cross_page=True)
            document = collection_recovery.load_layout_document(model)
            unit = _markdown_units()[0]
            with self.assertRaisesRegex(
                    collection_recovery.CollectionRecoveryError, "拒绝跨页"):
                collection_recovery.plan_gap_crops(document, unit, [3])
            plans = collection_recovery.plan_gap_crops(
                document, unit, [3], allow_cross_page=True)
            crops = collection_recovery.export_gap_crops(pdf, plans, root / "out")
            result = PdfReader(str(crops[0].path))

            self.assertEqual(2, len(result.pages))
            self.assertAlmostEqual(750.0, float(result.pages[0].mediabox.height))
            self.assertAlmostEqual(200.0, float(result.pages[1].mediabox.height))

    def test_global_body_signature_works_without_layout_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, _ = _write_fixture(root)
            payload = json.loads(model.read_text(encoding="utf-8"))
            payload[0][0]["content"] = "未识别出的普通栏眉"
            payload[1][-3]["content"] = "也不是结构标题"
            model.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            document = collection_recovery.load_layout_document(model)
            plans = collection_recovery.plan_gap_crops(
                document, _markdown_units()[0], [3])

        self.assertEqual(0, len(document.units))
        self.assertEqual((3,), plans[0].missing_numbers)

    def test_rejects_ambiguous_global_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, _ = _write_fixture(root)
            payload = json.loads(model.read_text(encoding="utf-8"))
            payload[1].insert(0, _row(
                "2. 第二题正文有足够多且唯一的文字用于定位前锚点甲乙丙丁",
                0.01, 0.03))
            model.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            document = collection_recovery.load_layout_document(model)

            with self.assertRaisesRegex(
                    collection_recovery.CollectionRecoveryError, "匹配到 2 处"):
                collection_recovery.plan_gap_crops(
                    document, _markdown_units()[0], [3])

    def test_groups_consecutive_missing_numbers_and_enforces_limit(self):
        unit = _markdown_units()[0]
        with tempfile.TemporaryDirectory() as tmp:
            model, _ = _write_fixture(Path(tmp))
            document = collection_recovery.load_layout_document(model)
            with self.assertRaisesRegex(
                    collection_recovery.CollectionRecoveryError, "一次最多"):
                collection_recovery.plan_gap_crops(
                    document, unit, [3, 6, 7, 8, 9])

    def test_end_gap_uses_next_unit_first_question_as_boundary(self):
        units = _markdown_units()
        # 构造第一单元末尾第 5 题缺失；第 4 题与下一单元第 1 题正文仍来自
        # model.json，边界由两段正文签名唯一定位，不靠专题页码。
        first = collection_structure.MarkdownUnit(
            title=units[0].title,
            topic=units[0].topic,
            ordinal=units[0].ordinal,
            markdown=units[0].markdown.rsplit("5.", 1)[0],
            start_line=units[0].start_line,
            question_numbers=(1, 2, 4),
        )
        with tempfile.TemporaryDirectory() as tmp:
            model, _ = _write_fixture(Path(tmp))
            document = collection_recovery.load_layout_document(model)
            plans = collection_recovery.plan_gap_crops(
                document, first, [5], next_unit=units[1],
                allow_cross_page=True)

        self.assertEqual((5,), plans[0].missing_numbers)
        self.assertEqual(4, plans[0].previous_number)
        self.assertEqual(1, plans[0].next_number)

    def test_crop_export_is_deterministic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, pdf = _write_fixture(root)
            args = (pdf, model, _markdown_units()[0], [3], root / "out")
            first = collection_recovery.create_gap_recovery_crops(*args)[0]
            first_hash = hashlib.sha256(first.path.read_bytes()).hexdigest()
            second = collection_recovery.create_gap_recovery_crops(*args)[0]
            second_hash = hashlib.sha256(second.path.read_bytes()).hexdigest()

        self.assertEqual(first.path, second.path)
        self.assertEqual(first_hash, second_hash)

    def test_vertical_suffix_crops_keep_original_bottom_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, pdf = _write_fixture(root)
            original = collection_recovery.create_gap_recovery_crops(
                pdf, model, _markdown_units()[0], [3], root / "out")[0]
            original_height = float(
                PdfReader(str(original.path)).pages[0].mediabox.height)

            refined = collection_recovery.export_vertical_suffix_crops(
                original.path, root / "refined")
            heights = [float(PdfReader(str(path)).pages[0].mediabox.height)
                       for path in refined]

        self.assertEqual(3, len(refined))
        self.assertAlmostEqual(original_height * 0.65, heights[0])
        self.assertAlmostEqual(original_height * 0.80, heights[1])
        self.assertAlmostEqual(original_height * 0.50, heights[2])


class RecoveredMarkdownTests(unittest.TestCase):
    def test_selects_only_expected_complete_choice_question(self):
        markdown = (
            "2. 上一道题只是用于上下文且正文也比较长。\n\n"
            "3. 下列关于物体运动的说法中正确的是（ ）\n"
            "A. 选项甲具有完整文字\nB. 选项乙具有完整文字\n"
            "C. 选项丙具有完整文字\nD. 选项丁具有完整文字\n\n"
            "4. 下一道题不应进入返回结果。")
        block = collection_recovery.select_recovered_question(markdown, 3)

        self.assertTrue(block.startswith("3."))
        self.assertIn("D. 选项丁", block)
        self.assertNotIn("4. 下一道题", block)

    def test_rejects_duplicate_expected_number(self):
        markdown = (
            "3. 第一份正文有足够多的文字并包含完整的上下文用于测试。\n"
            "3. 第二份正文也有足够多的文字并包含完整的上下文用于测试。")
        with self.assertRaisesRegex(
                collection_recovery.CollectionRecoveryError, "检出 2 次"):
            collection_recovery.select_recovered_question(markdown, 3)

    def test_rejects_choice_question_missing_option_d(self):
        markdown = (
            "3. 下列关于物体运动的说法中正确的是（ ）\n"
            "A. 选项甲具有完整文字\nB. 选项乙具有完整文字\n"
            "C. 选项丙具有完整文字")
        with self.assertRaisesRegex(
                collection_recovery.CollectionRecoveryError, "只检出选项 ABC"):
            collection_recovery.select_recovered_question(markdown, 3)

    def test_solution_role_does_not_require_repeated_choice_options(self):
        markdown = (
            "3. 【答案】D\n【详解】根据完整推导可知正确的是第二种情形，"
            "其余情况均不满足题设条件。")

        block = collection_recovery.select_recovered_question(
            markdown, 3, content_role="solution")

        self.assertIn("【答案】D", block)

    def test_solution_role_splits_unique_inline_number_with_detail_marker(self):
        markdown = (
            "上一题末尾仍有少量文字。11. B【详解】根据完整推导可知，"
            "地面对物体的摩擦力随角度增大而减小，其他选项不成立。")

        block = collection_recovery.select_recovered_question(
            markdown, 11, content_role="solution")

        self.assertTrue(block.startswith("11. B【详解】"))

    def test_solution_role_splits_inline_multiple_choice_answer(self):
        markdown = (
            "18. 第十八题解析正文完整且足够长，可以作为稳定的前锚。"
            "19. AC【详解】逐项判断以后可知 A、C 正确，"
            "其余两个选项均不满足题设条件。")

        normalized = collection_recovery.normalize_recovered_question_heads(
            markdown, [18, 19], content_role="solution")

        self.assertIn("\n\n19. AC【详解】", normalized)

    def test_solution_role_does_not_split_plain_inline_number(self):
        markdown = (
            "上一题计算得到 11.5 牛，后续分析文字足够长但没有解析标记。")

        with self.assertRaisesRegex(
                collection_recovery.CollectionRecoveryError, "检出 0 次"):
            collection_recovery.select_recovered_question(
                markdown, 11, content_role="solution")

    def test_solution_role_splits_unique_inline_multipart_number(self):
        markdown = (
            "15. (1) 第十五题解析正文足够完整，可以作为前锚点。"
            "后续推导结束。16. (1) 以整体为研究对象，根据牛顿第二定律"
            "列出方程并联立求解，所得结论具有完整的文字说明。")

        normalized = collection_recovery.normalize_recovered_question_heads(
            markdown, [15, 16], content_role="solution")
        selected = collection_recovery.select_recovered_questions(
            normalized, [15, 16], content_role="solution")

        self.assertTrue(selected[16].startswith("16. (1)"))
        self.assertNotIn("16. (1)", selected[15])

    def test_trims_only_exact_trailing_next_unit_title(self):
        markdown = (
            "16. (1) 当前单元最后一题的解析正文足够完整。\n\n"
            "## 《精练七：平衡问题继续强化》参考答案\n")

        trimmed = collection_recovery.trim_trailing_next_unit_title(
            markdown, "精练七：平衡问题继续强化")

        self.assertNotIn("精练七", trimmed)
        self.assertIn("当前单元最后一题", trimmed)

    def test_does_not_trim_nonmatching_trailing_title(self):
        markdown = (
            "16. (1) 当前单元最后一题的解析正文足够完整。\n\n"
            "## 《精练八：其他主题》参考答案\n")

        self.assertEqual(
            markdown,
            collection_recovery.trim_trailing_next_unit_title(
                markdown, "精练七：平衡问题继续强化"),
        )

    def test_trim_swallowed_solution_suffix_removes_only_proven_overlap(self):
        repeated = (
            "对两物体的整体分析可知拉力与摩擦力满足平衡方程，"
            "联立整理后可知角度增大时摩擦力逐渐减小，"
            "因此第二个选项正确而其余选项均不符合条件。"
            "再分别考察支持力和弹簧张力的方向，能够排除另外两种情况，"
            "整个判断过程具有足够长且唯一的可核对文字。")
        anchor = (
            "10. 第十题自身的完整解析内容足够长，用来确保前锚点不会被误删，"
            "并且还包含一段只属于第十题的独立结论。\n\n"
            "![](images/q10.jpg)\n\n" + repeated)
        recovered = "11. B【详解】" + repeated + "并完成第十一题的剩余推导。"

        cleaned = collection_recovery.trim_swallowed_solution_suffix(
            anchor, recovered, anchor_number=10)

        self.assertIn("images/q10.jpg", cleaned)
        self.assertNotIn(repeated, cleaned)

    def test_trim_swallowed_solution_suffix_rejects_image_in_removed_part(self):
        repeated = (
            "对两物体的整体分析可知拉力与摩擦力满足平衡方程，"
            "联立整理后可知角度增大时摩擦力逐渐减小，"
            "因此第二个选项正确而其余选项均不符合条件。"
            "再分别考察支持力和弹簧张力的方向，能够排除另外两种情况，"
            "整个判断过程具有足够长且唯一的可核对文字。")
        anchor = (
            "10. 第十题自身的完整解析内容足够长，用来确保前锚点不会被误删，"
            "并且还包含一段只属于第十题的独立结论。\n\n"
            + repeated + "\n\n![](images/unknown-owner.jpg)")
        recovered = "11. B【详解】" + repeated + "并完成第十一题的剩余推导。"

        with self.assertRaisesRegex(
                collection_recovery.CollectionRecoveryError, "后缀含图片"):
            collection_recovery.trim_swallowed_solution_suffix(
                anchor, recovered, anchor_number=10)

    def test_comparison_key_keeps_math_less_than_text(self):
        key, positions = collection_recovery._comparison_key_with_positions(
            "物块进入长度为 $x (x < L)$，随后有 $a > 0$。")

        self.assertIn("xxl随后有a0", key)
        self.assertEqual(len(key), len(positions))

    def test_proof_prompt_is_not_misclassified_as_choice_question(self):
        markdown = (
            "3. 请证明以下结论正确，并写出完整、严谨且可复核的推导过程。")

        block = collection_recovery.select_recovered_question(markdown, 3)

        self.assertTrue(block.startswith("3."))

    def test_rejects_too_little_visible_body(self):
        with self.assertRaisesRegex(
                collection_recovery.CollectionRecoveryError, "可见正文不足"):
            collection_recovery.select_recovered_question("3. 如图。", 3)


class ChoiceOptionRecoveryTests(unittest.TestCase):
    def test_right_figure_layout_is_required_for_text_column_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "fixture_content_list.json"
            content.write_text(json.dumps([
                {"type": "image", "page_idx": 0,
                 "bbox": [480, 200, 650, 620],
                 "img_path": "images/left-figure.png"},
                {"type": "chart", "page_idx": 0,
                 "bbox": [740, 180, 940, 650],
                 "img_path": "images/right-figure.png"},
            ]), encoding="utf-8")
            local = ("5. 题干（ ）\n\n"
                     "![](images/left-figure.png)\n\n"
                     "![](images/right-figure.png)")
            original = ("5. 原题题干（ ）\n\n"
                        "![](images/original-a.png)\n\n"
                        "![](images/original-b.png)")

            ratio = converter._right_figure_text_column_ratio(
                local, root, original)
            content.write_text(json.dumps([
                {"type": "image", "page_idx": 0,
                 "bbox": [80, 200, 330, 620],
                 "img_path": "images/left.png"},
                {"type": "image", "page_idx": 0,
                 "bbox": [650, 200, 900, 620],
                 "img_path": "images/right.png"},
            ]), encoding="utf-8")
            rejected = converter._right_figure_text_column_ratio(
                ("![](images/left.png)\n\n"
                 "![](images/right.png)"), root, original)

        self.assertAlmostEqual(0.64, ratio)
        self.assertIsNone(rejected)

    def test_strict_stem_signature_ignores_unlabelled_formula_shell(self):
        original = (
            "2. 下列关于物体沿斜面运动状态的说法中正确的是（ ）\n\n"
            "$$v_1$$\n\n$$v_2$$\n\n$$v_3$$")
        recovered = (
            "2. 下列关于物体沿斜面运动状态的说法中正确的是（ ）\n"
            "A. 第一项完整文字\nB. 第二项完整文字\n"
            "C. 第三项完整文字\nD. 第四项完整文字")

        self.assertEqual(converter._choice_stem_signature(original),
                         converter._choice_stem_signature(recovered))
        self.assertNotEqual(
            converter._choice_stem_signature(original),
            converter._choice_stem_signature(
                recovered.replace("沿斜面运动", "沿斜面静止")))

    def test_complete_local_candidate_replaces_only_target_block(self):
        raw = (
            "## 一、单选题\n\n"
            "1. 第一题正文足够长且选项完整（ ）\n"
            "A. 甲甲\nB. 乙乙\nC. 丙丙\nD. 丁丁\n\n"
            "2. 下列关于物体沿斜面运动状态的说法中正确的是（ ）\n\n"
            "$$v_1$$\n\n$$v_2$$\n\n$$v_3$$\n\n"
            "3. 第三题正文足够长且选项完整（ ）\n"
            "A. 甲甲\nB. 乙乙\nC. 丙丙\nD. 丁丁")
        recovered = (
            "2. 下列关于物体沿斜面运动状态的说法中正确的是（ ）\n"
            "A. 第一项完整文字\nB. 第二项完整文字\n"
            "C. 第三项完整文字\nD. 第四项完整文字")
        blocks = blockpipe.split_and_prep(raw, note_sink=None)
        before = {block.number: block.text for block in blocks}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"fixture")
            raw_path = root / "unit_raw.md"
            raw_path.write_text(raw, encoding="utf-8")
            page_pdf = root / "page.pdf"
            page_pdf.write_bytes(b"page")
            with mock.patch.object(
                    collection_recovery, "locate_unique_question_page",
                    return_value=4), \
                    mock.patch.object(
                        collection_recovery, "export_pdf_page",
                        return_value=page_pdf), \
                    mock.patch.object(
                        converter, "_load_config_for_user",
                        return_value=SimpleNamespace(
                            mineru_model_version="test", mineru_token="token")), \
                    mock.patch.object(
                        converter, "_recognize_collection_recovery_crop",
                        return_value=(recovered, root)):
                fixed = converter._recover_collection_choice_options(
                    blocks, raw_path=raw_path, source_pdf=source,
                    ocr_backend="mineru")

        after = {block.number: block.text for block in fixed}
        self.assertEqual(before[1], after[1])
        self.assertEqual(before[3], after[3])
        self.assertIn("第四项完整文字", after[2])
        self.assertEqual([], qualcheck.find_option_count_anomalies(fixed))

    def test_duplicate_local_number_uses_only_complete_candidate_and_original_shell(self):
        raw = (
            "2. 下列关于物体沿斜面运动状态的说法中正确的是（ ）\n\n"
            "A. 原识别只保留第一项\n\nD. 原识别只保留第四项\n\n"
            "![](images/original-a.png)\n\n"
            "![](images/original-b.png)")
        original = blockpipe.split_and_prep(raw, note_sink=None)[0]
        local = (
            "2. 下列关于物体沿斜面运动状态的说法中正确的是（ ）\n\n"
            "2. 下列关于物体沿斜面运动状态的说法中正确的是（ ）\n"
            "A. 第一项完整文字\nB. 第二项完整文字\n"
            "C. 第三项完整文字\nD. 第四项完整文字\n\n"
            "![](images/local.png)")

        selected, local_images = converter._select_choice_recovery_candidate(
            local, 2, original, keep_images=True)

        self.assertFalse(local_images)
        self.assertIn("下列关于物体沿斜面运动状态", selected)
        self.assertIn("第四项完整文字", selected)
        self.assertIn("images/original-a.png", selected)
        self.assertIn("images/original-b.png", selected)
        self.assertNotIn("images/local.png", selected)
        fixed = dataclasses.replace(original, text=selected)
        self.assertEqual([], qualcheck.find_option_count_anomalies([fixed]))

    def test_duplicate_local_number_rejects_two_complete_candidates(self):
        raw = (
            "2. 下列关于物体沿斜面运动状态的说法中正确的是（ ）\n\n"
            "A. 原识别第一项\nD. 原识别第四项")
        original = blockpipe.split_and_prep(raw, note_sink=None)[0]
        complete = (
            "2. 下列关于物体沿斜面运动状态的说法中正确的是（ ）\n"
            "A. 第一项完整文字\nB. 第二项完整文字\n"
            "C. 第三项完整文字\nD. 第四项完整文字")

        with self.assertRaisesRegex(
                collection_recovery.CollectionRecoveryError, "检出 2 次"):
            converter._select_choice_recovery_candidate(
                complete + "\n\n" + complete, 2, original,
                keep_images=True)


class RecoveryImageTests(unittest.TestCase):
    def test_no_image_reference_needs_no_images_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = "3. 这是没有图片引用的局部识别题块。"
            self.assertEqual(
                markdown,
                collection_recovery.copy_recovery_images(
                    markdown, root / "missing-local", root / "missing-main"),
            )

    def test_copies_images_by_content_hash_and_rewrites_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            main = root / "main"
            (local / "images").mkdir(parents=True)
            (local / "images" / "same.png").write_bytes(b"png-content")
            markdown = ("3. 正文足够长。\n\n![](images/same.png)\n\n"
                        "<img src=\"images/same.png\">")

            rewritten = collection_recovery.copy_recovery_images(
                markdown, local, main)
            files = list((main / "images").iterdir())

            self.assertEqual(1, len(files))
            self.assertEqual(b"png-content", files[0].read_bytes())
            self.assertNotIn("images/same.png", rewritten)
            self.assertEqual(2, rewritten.count(f"images/{files[0].name}"))

    def test_rejects_traversal_image_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "local" / "images").mkdir(parents=True)
            with self.assertRaisesRegex(
                    collection_recovery.CollectionRecoveryError, "路径不安全"):
                collection_recovery.copy_recovery_images(
                    "![](images/../secret.png)", root / "local", root / "main")


class OutOfCropImageRelocationTests(unittest.TestCase):
    @staticmethod
    def _plan():
        return collection_recovery.GapCropPlan(
            unit_title="精练二十七:功能关系",
            missing_numbers=(9,), previous_number=8, next_number=10,
            slices=(
                collection_recovery.PageSlice(0, 0.04, 1.0),
                collection_recovery.PageSlice(1, 0.0, 0.04),
            ),
        )

    @staticmethod
    def _layout_unit():
        questions = tuple(
            collection_recovery.LayoutQuestion(number, page, number,
                                               (0.05, y, 0.95, y + 0.03), "正文")
            for number, page, y in (
                (8, 0, 0.04), (9, 0, 0.49), (10, 1, 0.04),
                (11, 1, 0.34), (12, 1, 0.59),
            )
        )
        return collection_recovery.LayoutUnit(
            "精练二十七:功能关系", "功能关系", 27, 0, (), questions)

    @staticmethod
    def _next_layout_unit():
        line = collection_recovery.LayoutLine(
            1, 99, "doc_title", "精练二十八:下一专题",
            (0.05, 0.90, 0.95, 0.93))
        return collection_recovery.LayoutUnit(
            "精练二十八:下一专题", "下一专题", 28, 99, (line,), ())

    @staticmethod
    def _question(*, complete: bool, stray: bool = True) -> str:
        stem = (
            "9. 如图所示装置中物体运动，请判断下列说法正确的是（ ）\n\n"
            "![](images/stem-a.png)\n\na\n\n"
            "![](images/stem-b.png)\n\nb\n\n"
        )
        if complete:
            options = "\n\n".join(
                f"{label}) ![](images/option-{label.lower()}.png)"
                for label in "ABCD")
        else:
            # 六张本题图片仍都在，但 B 标签与第二张选项图的阅读顺序错开。
            options = (
                "![](images/option-a.png)\n\nA)\n\nB)\n\n"
                "![](images/option-b.png)\n\nC) ![](images/option-c.png)\n\n"
                "D) ![](images/option-d.png)")
        tail = "\n\n![](images/stray.png)" if stray else ""
        return stem + options + tail

    def test_reassigns_only_proven_outside_image_and_accepts_exact_stem_repair(self):
        images = {
            **{
                name: imgorder._Box(0, (100, 100 + index * 80, 300,
                                       150 + index * 80))
                for index, name in enumerate((
                    "stem-a.png", "stem-b.png", "option-a.png",
                    "option-b.png", "option-c.png", "option-d.png"))
            },
            "stray.png": imgorder._Box(1, (865, 766, 932, 815)),
        }
        primary_text = self._question(complete=False)
        comparison, relocated = converter._relocate_out_of_crop_images(
            primary_text, question_number=9, plan=self._plan(),
            source_layout=imgorder._Layout(images, ()),
            layout_unit=self._layout_unit(),
            next_layout_unit=self._next_layout_unit(),
            existing_numbers=range(1, 13),
        )
        primary = SimpleNamespace(number=9, text=comparison)
        recovered = SimpleNamespace(number=9, text=self._question(
            complete=True, stray=False).replace("\n\nb\n\n", "\n\n"))

        self.assertNotIn("stray.png", comparison)
        self.assertEqual({12: ["![](images/stray.png)"]}, relocated)
        self.assertEqual(6, converter._collection_block_image_count(comparison))
        self.assertTrue(converter._alternate_block_is_better(
            primary, recovered, require_matching_stem=True))

    def test_boundary_overlap_and_missing_coordinates_are_never_relocated(self):
        text = ("9. 这是正文足够长的题目。\n\n"
                "![](images/touch.png)\n\n![](images/unknown.png)")
        layout = imgorder._Layout({
            # 裁片在第二页到 y=40；矩形跨过边界，必须视为裁片内。
            "touch.png": imgorder._Box(1, (10, 39, 50, 50)),
        }, ())

        comparison, relocated = converter._relocate_out_of_crop_images(
            text, question_number=9, plan=self._plan(), source_layout=layout,
            layout_unit=self._layout_unit(),
            next_layout_unit=self._next_layout_unit(),
            existing_numbers=range(1, 13),
        )

        self.assertEqual(text, comparison)
        self.assertEqual({}, relocated)

    def test_cross_question_boundary_unordered_layout_and_unsafe_path_fail_closed(self):
        plan = self._plan()
        next_layout = self._next_layout_unit()
        cases = []
        cross_text = "9. 正文足够长且用于测试跨题边界。\n\n![](images/cross.png)"
        cases.append((
            "跨下一题起点",
            cross_text,
            imgorder._Layout({
                # top 仍在第 11 题，bottom 已跨过第 12 题 y=.59。
                "cross.png": imgorder._Box(1, (10, 580, 50, 610)),
            }, ()),
            self._layout_unit(),
        ))
        unsafe_text = (
            "9. 正文足够长且用于测试异常图片路径。\n\n"
            "![](images/../stray.png)")
        cases.append((
            "异常路径",
            unsafe_text,
            imgorder._Layout({
                "stray.png": imgorder._Box(1, (865, 766, 932, 815)),
            }, ()),
            self._layout_unit(),
        ))
        unit = self._layout_unit()
        unordered = list(unit.questions)
        unordered[1] = dataclasses.replace(unordered[1], number=10)
        unordered[2] = dataclasses.replace(unordered[2], number=9)
        cases.append((
            "题号顺序倒退",
            "9. 正文足够长且用于测试题号倒退。\n\n![](images/stray.png)",
            imgorder._Layout({
                "stray.png": imgorder._Box(1, (865, 766, 932, 815)),
            }, ()),
            dataclasses.replace(unit, questions=tuple(unordered)),
        ))
        for label, text, layout, layout_unit in cases:
            with self.subTest(label=label):
                comparison, relocated = converter._relocate_out_of_crop_images(
                    text, question_number=9, plan=plan,
                    source_layout=layout, layout_unit=layout_unit,
                    next_layout_unit=next_layout,
                    existing_numbers=range(1, 13), raw_source_text=text,
                )
                self.assertEqual(text, comparison)
                self.assertEqual({}, relocated)

    def test_rejects_repair_when_stem_signature_changes(self):
        primary = SimpleNamespace(
            number=9, text=self._question(complete=False, stray=False))
        changed = self._question(complete=True, stray=False).replace(
            "物体运动", "物体静止") + "\n\n" + "补充正文" * 100

        self.assertFalse(converter._alternate_block_is_better(
            primary, SimpleNamespace(number=9, text=changed),
            require_matching_stem=True))

    def test_end_to_end_recovery_moves_image_to_owner_and_preserves_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=600, height=1000)
            writer.add_blank_page(width=600, height=1000)
            with source_pdf.open("wb") as stream:
                writer.write(stream)
            workspace = root / "work"
            workspace.mkdir()
            image_names = (
                "stem-a.png", "stem-b.png", "option-a.png",
                "option-b.png", "option-c.png", "option-d.png",
            )
            content_rows = [
                {
                    "type": "image", "page_idx": 0,
                    "bbox": [100, 100 + index * 80, 300, 150 + index * 80],
                    "img_path": f"images/{name}",
                }
                for index, name in enumerate(image_names)
            ]
            content_rows.append({
                "type": "image", "page_idx": 1,
                "bbox": [865, 766, 932, 815],
                "img_path": "images/stray.png",
            })
            (workspace / "fixture_content_list.json").write_text(
                json.dumps(content_rows), encoding="utf-8")

            q8 = "8. 第八题作为局部识别前锚点，正文足够长且内容保持完全一致。"
            q10 = "10. 第十题作为局部识别后锚点，正文足够长且内容保持完全一致。"
            raw = "# 精练二十七：功能关系\n\n" + "\n\n".join([
                *(f"{number}. 第{number}题正文内容足够长并用于保持题号连续。"
                  for number in range(1, 8)),
                q8,
                self._question(complete=False),
                q10,
                "11. 第十一题正文内容足够长并用于确认后续归属。",
                "12. 第十二题正文内容足够长，齿轮图片应由程序归还到这里。",
            ])
            unit = collection_structure.MarkdownUnit(
                title="精练二十七:功能关系", topic="功能关系",
                ordinal=27, markdown=raw, start_line=1,
                question_numbers=tuple(range(1, 13)))
            layout_unit = self._layout_unit()
            # fixture 的标题必须与真实 Markdown 单元唯一匹配。
            layout_unit = dataclasses.replace(layout_unit, title=unit.title)
            layout_questions = list(layout_unit.questions)
            layout_questions[0] = dataclasses.replace(
                layout_questions[0], text=q8)
            layout_questions[2] = dataclasses.replace(
                layout_questions[2], text=q10)
            layout_unit = dataclasses.replace(
                layout_unit, questions=tuple(layout_questions))
            next_layout_unit = self._next_layout_unit()
            model = collection_recovery.LayoutDocument(
                2, (), tuple(layout_questions),
                (layout_unit, next_layout_unit))
            next_unit = collection_structure.MarkdownUnit(
                title=next_layout_unit.title, topic=next_layout_unit.topic,
                ordinal=next_layout_unit.ordinal,
                markdown="# 精练二十八：下一专题\n\n1. 下一专题第一题。",
                start_line=1, question_numbers=(1,))
            notes = []

            def fake_crop(_crop, recovery_dir):
                local = recovery_dir / "fake-extract"
                images = local / "images"
                images.mkdir(parents=True)
                recovered = self._question(complete=True, stray=False)
                for index, name in enumerate(image_names):
                    recovered_name = f"recovered-{name}"
                    recovered = recovered.replace(name, recovered_name)
                    (images / recovered_name).write_bytes(
                        f"image-{index}".encode("ascii"))
                return q8 + "\n\n" + recovered, local

            rebuilt, used = converter._recover_collection_unit_markdown(
                source_pdf, workspace, model, unit, [9],
                next_unit=next_unit,
                replace_existing=True,
                cfg=SimpleNamespace(
                    mineru_model_version="test", mineru_token="token"),
                source_sha256="fixture", content_role="stem",
                note_sink=notes.append, crop_recognizer=fake_crop,
            )

        rebuilt_unit = dataclasses.replace(unit, markdown=rebuilt)
        blocks = {block.number: block for block in
                  converter._collection_unit_blocks(rebuilt_unit)}
        self.assertEqual(1, used)
        self.assertNotIn("stray.png", blocks[9].text)
        self.assertIn("stray.png", blocks[12].text)
        self.assertIn("第十二题正文内容足够长，齿轮图片应由程序归还到这里。",
                      blocks[12].text)
        self.assertEqual(1, rebuilt.count("images/stray.png"))
        self.assertEqual(
            converter._collection_block_image_count(raw),
            converter._collection_block_image_count(rebuilt),
        )
        self.assertTrue(any("归还至第 12 题 1 张" in note for note in notes))


class CollectionRecoveryIntegrationTests(unittest.TestCase):
    @staticmethod
    def _book(*, missing: bool, solution: bool = False) -> str:
        prefix = "解析" if solution else "题干"
        blocks = [
            f"1. {prefix}第一题的完整正文，用于确认合集边界。",
            f"2. {prefix}第二题的完整前锚点正文，用于唯一定位并保证文字数量充分。",
        ]
        if missing:
            # 模拟第 3 题题号被吞后，正文错挂在第 2 题尾部。
            blocks[-1] += f"\n\n这是被上一题吞入的旧第三题{prefix}内容。"
        else:
            blocks.append(f"3. {prefix}第三题的完整正文，它应当只出现一次。")
        blocks.extend([
            f"4. {prefix}第四题的完整后锚点正文，用于限定裁片。",
            f"5. {prefix}第五题的完整正文，用于验证连续性。",
            f"6. {prefix}第六题的完整正文，用于验证单元末尾。",
        ])
        first = "\n\n".join(blocks)
        second = "\n\n".join(
            f"{number}. {prefix}第二专题第{number}题完整正文。"
            for number in (1, 2, 3))
        return (f"# 精练一：运动学基础\n\n{first}\n\n"
                f"# 精练二：动力学基础\n\n{second}")

    @staticmethod
    def _model(solution: bool = False):
        prefix = "解析" if solution else "题干"
        rows = [_row("精练一：运动学基础", 0.02, 0.04,
                     kind="doc_title")]
        y = 0.06
        chinese_number = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}
        for number in (1, 2):
            rows.append(_row(
                f"{number}. {prefix}第{chinese_number[number]}题的完整"
                + ("前锚点正文，用于唯一定位并保证文字数量充分。" if number == 2
                   else "正文，用于确认合集边界。"),
                y, y + 0.04))
            y += 0.07
        rows.append(_row(
            f"这是被上一题吞入的旧第三题{prefix}内容。",
            y, y + 0.04))
        y += 0.07
        for number, tail in ((4, "完整后锚点正文，用于限定裁片。"),
                             (5, "完整正文，用于验证连续性。"),
                             (6, "完整正文，用于验证单元末尾。")):
            rows.append(_row(
                f"{number}. {prefix}第{chinese_number[number]}题的{tail}",
                             y, y + 0.04))
            y += 0.07
        rows.append(_row("精练二：动力学基础", y, y + 0.03,
                         kind="doc_title"))
        y += 0.05
        for number in (1, 2, 3):
            rows.append(_row(
                f"{number}. {prefix}第二专题第{number}题完整正文。",
                y, y + 0.035))
            y += 0.045
        return [rows]

    def test_programmatically_recovers_stem_and_solution_and_replaces_swallowed_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_pdf = root / "exam.pdf"
            solution_pdf = root / "solution.pdf"
            for path in (exam_pdf, solution_pdf):
                writer = PdfWriter()
                writer.add_blank_page(width=600, height=1000)
                with path.open("wb") as stream:
                    writer.write(stream)
            exam_dir = root / "exam-work"
            solution_dir = root / "solution-work"
            exam_dir.mkdir()
            solution_dir.mkdir()
            (exam_dir / "exam_model.json").write_text(
                json.dumps(self._model(False), ensure_ascii=False), encoding="utf-8")
            (solution_dir / "solution_model.json").write_text(
                json.dumps(self._model(True), ensure_ascii=False), encoding="utf-8")
            calls = []

            def fake_crop(crop, recovery_dir):
                side = "解析" if solution_dir in recovery_dir.parents else "题干"
                calls.append((side, crop.plan.missing_numbers))
                local = recovery_dir / "fake-extract"
                detail = ("\n【答案】D\n【详解】根据推导可知正确的是第二种情形。"
                          if side == "解析" else "")
                return (
                    f"2. {side}第二题的完整前锚点正文，用于唯一定位并保证文字数量充分。"
                    f"{detail}\n\n"
                    f"3. {side}第三题经局部 OCR 恢复的完整正文，"
                    f"它应当只出现一次。{detail}",
                    local,
                )

            exam_raw, solution_raw = converter._recover_mineru_collection(
                self._book(missing=True),
                self._book(missing=True, solution=True),
                exam_path=exam_pdf, solution_path=solution_pdf,
                exam_dir=exam_dir, solution_dir=solution_dir,
                cfg_getter=lambda: SimpleNamespace(
                    mineru_model_version="test", mineru_token="token"),
                crop_recognizer=fake_crop,
            )

        self.assertEqual([("题干", (3,)), ("解析", (3,))], calls)
        self.assertNotIn("被上一题吞入的旧第三题", exam_raw)
        self.assertNotIn("被上一题吞入的旧第三题", solution_raw)
        self.assertEqual(1, exam_raw.count("3. 题干第三题经局部 OCR"))
        self.assertEqual(1, solution_raw.count("3. 解析第三题经局部 OCR"))

    def test_solution_uses_suffix_crop_when_full_crop_still_loses_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solution_pdf = root / "solution.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=600, height=1000)
            with solution_pdf.open("wb") as stream:
                writer.write(stream)
            workspace = root / "solution-work"
            workspace.mkdir()
            unit = collection_structure.split_markdown_units(
                self._book(missing=True, solution=True))[0]
            model_path = root / "solution-model.json"
            model_path.write_text(
                json.dumps(self._model(True), ensure_ascii=False),
                encoding="utf-8")
            model = collection_recovery.load_layout_document(model_path)
            calls = []

            def fake_crop(crop, recovery_dir):
                calls.append(crop.path.name)
                local = recovery_dir / "fake-extract"
                local.mkdir(parents=True, exist_ok=True)
                if crop.path.name.startswith("refined_"):
                    repeated = (
                        "第三题经缩窄 MinerU 裁片恢复出完整解析正文，"
                        "其推导过程包含足够多且能够逐字核对的稳定文字，"
                        "最终结论明确且不会与前后题混淆。"
                        "继续联立两个独立方程以后可以得到唯一数值，"
                        "并据此完成对所有物理量的逐项验证。")
                    return (
                        "上一题末尾。3. D【详解】" + repeated,
                        local,
                    )
                repeated = (
                    "第三题经缩窄 MinerU 裁片恢复出完整解析正文，"
                    "其推导过程包含足够多且能够逐字核对的稳定文字，"
                    "最终结论明确且不会与前后题混淆。"
                    "继续联立两个独立方程以后可以得到唯一数值，"
                    "并据此完成对所有物理量的逐项验证。")
                return (
                    "2. 解析第二题的完整前锚点正文，用于唯一定位并保证"
                    "文字数量充分，且这一部分只属于第二题。" + repeated,
                    local,
                )

            rebuilt, used = converter._recover_collection_unit_markdown(
                solution_pdf, workspace, model, unit, [3],
                replace_existing=False,
                cfg=SimpleNamespace(
                    mineru_model_version="test", mineru_token="token"),
                source_sha256="fixture", content_role="solution",
                crop_recognizer=fake_crop,
            )

        self.assertEqual(1, used)
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[1].startswith("refined_"))
        self.assertIn("3. D【详解】第三题经缩窄", rebuilt)
        self.assertNotIn("被上一题吞入的旧第三题", rebuilt)
        self.assertEqual(1, rebuilt.count("其推导过程包含足够多且能够逐字核对"))

    def test_leading_image_is_attached_to_proven_multipart_missing_solution(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            image_name = "q16.jpg"
            (local / "sample_content_list.json").write_text(json.dumps([
                {"type": "image", "page_idx": 0,
                 "bbox": [580, 520, 890, 880],
                 "img_path": f"images/{image_name}"},
            ]), encoding="utf-8")
            (local / "sample_model.json").write_text(json.dumps([[
                {"type": "text", "bbox": [0.05, 0.00, 0.94, 0.50],
                 "content": (
                     "15. (1) 第十五题完整解析。16. (1) 以物块和绳整体为"
                     "研究对象，根据牛顿第二定律列式并完成第一问。")},
                {"type": "text", "bbox": [0.06, 0.505, 0.47, 0.536],
                 "content": "(2) 对整体受力分析，如图所示合成到三角形中"},
                {"type": "text", "bbox": [0.05, 0.539, 0.50, 0.83],
                 "content": "继续计算两端张力并得到完整的第二问结论。"},
            ]]), encoding="utf-8")
            raw = (
                f"![](images/{image_name})\n\n"
                "15. (1) 第十五题的完整前锚解析正文足够长，能够稳定定位。"
                "16. (1) 以物块和绳整体为研究对象，根据牛顿第二定律列式，"
                "并完成第一问的全部推导过程。\n\n"
                "(2) 对整体受力分析，如图所示合成到三角形中\n\n"
                "继续计算两端张力并得到完整的第二问结论。")
            normalized = collection_recovery.normalize_recovered_question_heads(
                raw, [15, 16], content_role="solution")
            selected = collection_recovery.select_recovered_questions(
                normalized, [15, 16], content_role="solution")

            updated = converter._attach_proven_leading_solution_images(
                normalized, selected, local,
                missing_numbers=(16,), content_role="solution")

        self.assertNotIn(image_name, updated[15])
        self.assertEqual(1, updated[16].count(image_name))
        self.assertLess(updated[16].index("(2)"), updated[16].index(image_name))

    def test_unheaded_choice_solution_uses_complete_verdicts_and_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            image_name = "q6.jpg"
            (local / "sample_content_list.json").write_text(json.dumps([
                {"type": "image", "page_idx": 0,
                 "bbox": [700, 80, 920, 300],
                 "img_path": f"images/{image_name}"},
            ]), encoding="utf-8")
            q7_start = (
                "由速度图像计算位移得到八米，故 A 正确；"
                "再由两个阶段的加速度联立得到摩擦因数，故 B 错误；"
                "返回时间的计算结果与选项不符，故 C 错误；")
            q7_end = (
                "最后比较煤块与传送带的相对位移和重复痕迹，"
                "可知最长痕迹取后一阶段结果，故 D 正确。")
            (local / "sample_model.json").write_text(json.dumps([
                [
                    {"type": "text", "bbox": [0.05, 0.00, 0.68, 0.22],
                     "content": "6. C【详解】第六题开头的完整解析。"},
                    {"type": "text", "bbox": [0.05, 0.24, 0.90, 0.35],
                     "content": "继续推导并在这里得到前一问的最终结论。"},
                    {"type": "text", "bbox": [0.05, 0.40, 0.94, 0.90],
                     "content": q7_start},
                ],
                [
                    {"type": "text", "bbox": [0.05, 0.10, 0.94, 0.80],
                     "content": q7_end},
                ],
            ]), encoding="utf-8")
            raw = (
                "6. C【详解】第六题自身的解析正文足够长，可以稳定定位"
                "且不会与第七题内容混淆。\n\n"
                f"![](images/{image_name})\n\n"
                "继续推导并在这里得到前一问的最终结论。\n\n"
                + q7_start + "\n\n" + q7_end)

            cleaned, recovered = converter._recover_unheaded_choice_solution(
                raw, local, previous_number=6, missing_number=7)

        self.assertIn(image_name, cleaned)
        self.assertNotIn("故 A 正确", cleaned)
        self.assertTrue(recovered.startswith("7. AD【详解】"))
        self.assertEqual(1, (cleaned + recovered).count("故 A 正确"))

    def test_clipped_solution_uses_local_head_and_original_complete_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            (local / "sample_model.json").write_text(json.dumps([[
                {"type": "text", "bbox": [0.05, 0.00, 0.92, 0.70],
                 "content": "5. C【详解】第五题完整解析正文。"},
                {"type": "text", "bbox": [0.05, 0.80, 0.94, 1.00],
                 "content": (
                     "6. D【详解】小球以 v_0 运动，根据机械能守恒列出"
                     "三个方程，随后开始讨论下一种情况。")},
            ]]), encoding="utf-8")
            original = (
                "5. C【详解】第五题逐项判断如下。\n\n"
                "![](images/q5.jpg)\n\nA错误，B错误，C正确，D错误。\n\n"
                "$$v_{0}$$\n\n$$mgH+0=mgH'+0$$\n\n$$H=H'$$\n\n"
                "后半段来自整本识别，继续完成第六题其余选项的分析，"
                "并在结尾给出清楚且完整的物理结论。")
            clipped = (
                "6. D【详解】小球以 $v_{0}$ 运动并到达最高点。"
                "根据机械能守恒有 $mgH+0=mgH'+0$，再得 $H=H'$，"
                "随后开始讨论下一种情况")

            cleaned, recovered = (
                converter._recover_clipped_solution_from_original(
                    original, clipped, local,
                    anchor_number=5, missing_number=6))

        self.assertIn("images/q5.jpg", cleaned)
        self.assertNotIn("v_{0}", cleaned)
        self.assertTrue(recovered.startswith("6. D【详解】"))
        self.assertIn("后半段来自整本识别", recovered)

    def test_refined_solution_uses_proven_crop_and_two_formula_suffix(self):
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_dir = root / "original"
            local_dir = root / "local"
            (original_dir / "images").mkdir(parents=True)
            (local_dir / "images").mkdir(parents=True)

            source = Image.new("RGB", (240, 260), "white")
            draw = ImageDraw.Draw(source)
            draw.ellipse((35, 25, 205, 215), outline="black", width=4)
            draw.line((120, 0, 120, 260), fill="black", width=4)
            draw.line((0, 140, 240, 140), fill="black", width=4)
            draw.arc((20, 55, 95, 225), 250, 110, fill="black", width=5)
            draw.arc((145, 55, 220, 225), 70, 290, fill="black", width=5)
            source_path = original_dir / "images" / "q5.jpg"
            source.save(source_path, quality=95)
            crop = source.crop((0, 88, 240, 260)).resize(
                (244, 175), Image.Resampling.LANCZOS)
            crop_path = local_dir / "images" / "q5_crop.jpg"
            crop.save(crop_path, quality=91)
            source.close()
            crop.close()

            (local_dir / "sample_model.json").write_text(json.dumps([[
                {"type": "text", "bbox": [0.05, 0.00, 0.68, 0.985],
                 "content": (
                     "第五题末尾仍在左栏。6. D【详解】由 v=lambda f 可得"
                     " lambda=3.4m，由波的干涉可知完整结论贴近页底。")},
            ]]), encoding="utf-8")
            original = (
                "5. C【详解】第五题逐项分析的正文足够长，最后得到圆周上共有六个"
                "振动加强点这一完整结论。\n\n"
                "![](images/q5.jpg)\n\n"
                "$$v = \\lambda f$$\n\n"
                "$$\\lambda = 3.4\\mathrm{m}$$\n\n"
                "中听不到扩音机声音的次数可以通过路程差条件逐点计算，往返各有十个"
                "位置，因此沿椭圆完整运动一周共听不到二十次。")
            clipped = (
                "6. D【详解】由 $v=\\lambda f$ 可得 "
                "$\\lambda=3.4\\mathrm m$，由波的干涉可知人沿椭圆运动一周过程"
                "\n\n![](images/q5_crop.jpg)")

            cleaned, recovered = (
                converter._recover_refined_solution_from_original_formula_suffix(
                    original, clipped, original_dir, local_dir,
                    anchor_number=5, missing_number=6))

            self.assertIn("images/q5.jpg", cleaned)
            self.assertNotIn("v = \\lambda f", cleaned)
            self.assertTrue(recovered.startswith("6. D【详解】"))
            self.assertNotIn("q5_crop.jpg", recovered)
            self.assertIn("沿椭圆运动一周过程中听不到", recovered)
            self.assertIn("共听不到二十次", recovered)
            self.assertEqual(1, recovered.count("v=\\lambda f"))

            unrelated = Image.new("RGB", (244, 175), "white")
            unrelated_draw = ImageDraw.Draw(unrelated)
            unrelated_draw.rectangle((15, 15, 225, 155), outline="black", width=8)
            unrelated_draw.line((15, 15, 225, 155), fill="black", width=8)
            unrelated.save(crop_path, quality=91)
            unrelated.close()
            with self.assertRaisesRegex(converter.ConvertError, "不能唯一证明"):
                converter._recover_refined_solution_from_original_formula_suffix(
                    original, clipped, original_dir, local_dir,
                    anchor_number=5, missing_number=6)

    def test_locates_normalized_blocks_by_unique_raw_question_headings(self):
        raw = (
            "# 精练一：运动学基础\n\n一、单项选择题\n\n"
            "1．第一题保留原始全角题号。\n\n"
            "2．第二题原文含有尚未规范化的版式。\n\n"
            "二、多项选择题\n\n"
            "3．第三题正文必须保持原样。\n\n卷尾说明也必须保留。\n"
        )
        blocks = [
            SimpleNamespace(number=1, text="1. 第一题保留原始全角题号。"),
            SimpleNamespace(number=2, text="2. 第二题已经被机械规范化。"),
            SimpleNamespace(number=3, text="3. 第三题正文必须保持原样。"),
        ]

        spans = converter._exact_block_spans(raw, blocks)
        self.assertIsNotNone(spans)
        rebuilt = converter._rebuild_collection_unit(
            raw, blocks, spans, {2: "2. 第二题由局部 MinerU 稳定恢复。"})

        self.assertIn("# 精练一：运动学基础", rebuilt)
        self.assertIn("一、单项选择题", rebuilt)
        self.assertIn("二、多项选择题", rebuilt)
        self.assertIn("2. 第二题由局部 MinerU 稳定恢复。", rebuilt)
        self.assertNotIn("第二题原文含有尚未规范化的版式", rebuilt)
        self.assertIn("3．第三题正文必须保持原样。", rebuilt)
        self.assertIn("卷尾说明也必须保留。", rebuilt)

    def test_raw_question_heading_fallback_rejects_duplicate_or_wrong_order(self):
        blocks = [
            SimpleNamespace(number=1, text="规范化第一题"),
            SimpleNamespace(number=2, text="规范化第二题"),
            SimpleNamespace(number=3, text="规范化第三题"),
        ]
        duplicate = "1．第一题\n\n2．第二题甲\n\n2．第二题乙\n\n3．第三题"
        wrong_order = "1．第一题\n\n3．第三题\n\n2．第二题"

        self.assertIsNone(converter._exact_block_spans(duplicate, blocks))
        self.assertIsNone(converter._exact_block_spans(wrong_order, blocks))

    def test_raw_question_heading_fallback_ignores_code_and_table_numbers(self):
        raw = (
            "1．第一题正文\n\n```text\n2. 代码示例不是题号\n```\n\n"
            "| 2. 表格内容也不是题号 |\n\n2．第二题正文\n"
        )
        blocks = [
            SimpleNamespace(number=1, text="机械第一题"),
            SimpleNamespace(number=2, text="机械第二题"),
        ]

        spans = converter._exact_block_spans(raw, blocks)

        self.assertIsNotNone(spans)
        self.assertEqual("1．", raw[slice(*spans[1])][:2])
        self.assertEqual("2．", raw[slice(*spans[2])][:2])

    def test_inserts_missing_question_before_existing_raw_heading(self):
        raw = (
            "# 精练一：运动学基础\n\n"
            "1．第一题原文正文。\n\n二、实验题\n\n3．第三题原文正文。\n"
        )
        blocks = [
            SimpleNamespace(number=1, text="机械规范化后的第一题"),
            SimpleNamespace(number=3, text="机械规范化后的第三题"),
        ]
        spans = converter._exact_block_spans(raw, blocks)

        self.assertIsNotNone(spans)
        rebuilt = converter._rebuild_collection_unit(
            raw, blocks, spans, {2: "2. 局部 MinerU 恢复出的第二题。"})

        self.assertLess(rebuilt.index("2. 局部 MinerU"), rebuilt.index("3．第三题"))
        self.assertEqual(1, rebuilt.count("二、实验题"))
        self.assertEqual(1, rebuilt.count("2. 局部 MinerU"))

    def test_locates_blocksplit_synthesized_section_lead_by_provenance(self):
        import blocksplit

        raw = (
            "# 通用练习标题\n\n## 一、单选题\n\n"
            "1. 第一题正文有足够多文字用于确认边界。\n\n"
            "2. 第二题正文有足够多文字用于确认前锚点。\n\n"
            "## 二、多选题\n\n"
            "这一整段是题号被 OCR 吃掉的第三题正文，内容保持连续且唯一。\n\n"
            "4. 第四题正文有足够多文字用于确认后锚点。\n"
        )
        blocks = converter._collection_blocks(SimpleNamespace(markdown=raw))

        self.assertEqual([1, 2, 3, 4], [block.number for block in blocks])
        self.assertTrue(blocks[2].text.startswith("3. "))
        spans = converter._exact_block_spans(raw, blocks)
        self.assertIsNotNone(spans)
        rebuilt = converter._rebuild_collection_unit(
            raw, blocks, spans, {
                2: "2. 局部 MinerU 恢复后的第二题前锚点。",
                3: "3. 局部 MinerU 恢复后的第三题完整正文。",
            })

        self.assertEqual(1, rebuilt.count("## 二、多选题"))
        self.assertIn("2. 局部 MinerU 恢复后的第二题前锚点。", rebuilt)
        self.assertIn("3. 局部 MinerU 恢复后的第三题完整正文。", rebuilt)
        self.assertNotIn("题号被 OCR 吃掉", rebuilt)
        self.assertIn("4. 第四题正文有足够多文字", rebuilt)

    def test_detects_complete_options_with_formula_shell_as_weak_question(self):
        raw = self._book(missing=False).replace(
            "3. 题干第三题的完整正文，它应当只出现一次。",
            "3. $m=2\\,kg$ $v=3\\,m/s$\n\n"
            "A. $1\\,m$\n\nB. $2\\,m$\n\nC. $3\\,m$\n\nD. $4\\,m$",
        )
        unit = collection_structure.split_markdown_units(raw)[0]

        self.assertEqual([3], converter._weak_collection_question_numbers(unit))

    def test_recovery_allows_unique_multicolumn_order_then_finally_sorts(self):
        unit = SimpleNamespace(
            title="多栏练习",
            markdown="\n\n".join(
                f"{number}. 第 {number} 题完整正文"
                for number in (1, 2, 4, 5, 8, 10, 9)))

        with self.assertRaises(converter.ConvertError):
            converter._collection_number_gaps(unit)
        self.assertEqual(
            [3, 6, 7],
            converter._collection_number_gaps(
                unit, allow_out_of_order=True))

        complete = SimpleNamespace(
            title="多栏练习",
            markdown="\n\n".join(
                f"{number}. 第 {number} 题完整正文"
                for number in (1, 2, 3, 4, 5, 6, 7, 8, 10, 9)))
        groups = blockpipe.group_blocks(
            blockpipe.split_and_prep(complete.markdown))
        self.assertEqual(
            list(range(1, 11)),
            [stem.number for stem, _solutions in groups])

    def test_local_mineru_crop_cache_prevents_second_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crop = root / "crop.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with crop.open("wb") as stream:
                writer.write(stream)
            recovery = root / "stable"
            calls = []

            class FakeClient:
                def __init__(self, _token, _model):
                    pass

                def parse_pdf(self, _path, *, extract_dir, **_kwargs):
                    calls.append(1)
                    Path(extract_dir).mkdir(parents=True, exist_ok=True)
                    return ("2. 这是可稳定复用的局部识别正文，字数足够。",
                            "crop.md")

            cfg = SimpleNamespace(
                mineru_model_version="test-model", mineru_token="fallback")
            converter._ensure_src_on_path()
            with mock.patch("src.mineru_client.MineruClient", FakeClient), \
                    mock.patch.object(
                        converter.ocr_pool, "run",
                        side_effect=lambda _backend, callback, fallback="":
                        callback("token")):
                first = converter._recognize_collection_recovery_crop(
                    crop, recovery, cfg)
                second = converter._recognize_collection_recovery_crop(
                    crop, recovery, cfg)

        self.assertEqual(1, len(calls))
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])


if __name__ == "__main__":
    unittest.main()
