"""好题/好卷/好资料三条 Profile 链路的编排行为。

全部离线：converter 的 OCR / 渲染入口都被 mock，不发任何网络请求、不读
真实凭据（好题链路现在会真实调用 convert_file_to_blocks，测试必须挡住它）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import source_profiles


class NameAndNumberTests(unittest.TestCase):
    def test_extract_number_removes_only_main_number(self):
        number, body = source_profiles.extract_question_number(
            "11. 已知函数\n（1）求值\nA. 1")
        self.assertEqual(number, 11)
        self.assertEqual(body, "已知函数\n（1）求值\nA. 1")

    def test_sanitize_rejects_windows_reserved_names(self):
        self.assertTrue(
            source_profiles.sanitize_output_name("CON?.pdf").startswith("CON"))

    def test_unique_card_name_suffixes_case_insensitively(self):
        used: set[str] = set()
        self.assertEqual(source_profiles._unique_card_name(used, "第1题"), "第1题")
        self.assertEqual(source_profiles._unique_card_name(used, "第1题"), "第1题_2")
        self.assertEqual(source_profiles._unique_card_name(used, "第1题"), "第1题_3")
        self.assertEqual(source_profiles._unique_card_name(used, "ABC"), "ABC")
        self.assertEqual(source_profiles._unique_card_name(used, "abc"), "abc_2")


class _ProfileTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "QQ20261004-224554.png"
        self.source.write_bytes(b"img")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_pending(self, text: str) -> dict:
        return {
            "blocks": [{"index": 0, "number": None, "text": text,
                        "section": None, "group": None, "zone": "stem",
                        "line_no": 1, "kind": "unknown"}],
            "extract_dirs": [], "keep_images": True,
            "source_name": self.source.name, "boundary_mode": "auto",
            "ocr_backend": "mineru", "ocr_meta": {}, "single_block": True,
        }

    def _card_text(self, result: dict, index: int = 0) -> str:
        return (Path(result["staged_root"]) / result["cards"][index]).read_text(
            encoding="utf-8")

    def _card_path(self, result: dict, index: int = 0) -> Path:
        return Path(result["staged_root"]) / result["cards"][index]


class GoodQuestionTests(_ProfileTestBase):
    def test_question_without_number_keeps_source_name(self):
        """事故回归（真实卡文本）：无题号截图必须成功出卡，且裸字母会被
        包上 `$\\displaystyle $`（手动导入收尾口径，监控链路此前缺失）。"""
        raw = ("在平面直角坐标系中, A、B、C、D 是圆 "
               "$\\displaystyle x^{2} + y^{2} = 1$ 上四个不同的点, "
               "直线 AC 与直线 BD 的交点为 P, 则 "
               "$\\displaystyle |OP|$ 的取值范围为 ___")
        with mock.patch("converter.convert_file_to_blocks",
                        return_value=self._fake_pending(raw)) as cfb, \
                mock.patch("converter.finish_block_review",
                           return_value="- [填空] " + raw):
            result = source_profiles.convert_good_question(
                self.source, self.root / "stage", ocr_backend="mineru",
                engine="block", normalize=False)
        self.assertTrue(cfb.call_args.kwargs.get("single_block"))
        card = self._card_path(result)
        self.assertEqual(card.name, "QQ20261004-224554.md")
        text = card.read_text(encoding="utf-8")
        self.assertIn("在平面直角坐标系中", text)
        self.assertNotIn("[填空]", text)   # 契约题型标签不进卡片正文
        # 裸单字母必须包 $；已在公式内的不重复包；填空线不动。
        self.assertIn("$\\displaystyle A$", text)
        self.assertIn("$\\displaystyle P$", text)
        self.assertNotIn("$\\displaystyle x$", text)  # x 本就在公式内
        self.assertIn("为 ___", text)
        self.assertFalse(result["warnings"])

    def test_question_with_number_goes_into_card_name_and_leaves_body(self):
        raw = "3. 已知函数 $f(x)$，求最小值。"
        with mock.patch("converter.convert_file_to_blocks",
                        return_value=self._fake_pending(raw)), \
                mock.patch("converter.finish_block_review",
                           return_value="- [解答] 3. 已知函数 $f(x)$，求最小值。"):
            result = source_profiles.convert_good_question(
                self.source, self.root / "stage", ocr_backend="mineru",
                engine="block", normalize=False)
        card = self._card_path(result)
        self.assertEqual(card.name, "QQ20261004-224554第3题.md")
        text = card.read_text(encoding="utf-8")
        self.assertIn("已知函数", text)
        self.assertNotIn("3. 已知", text)
        self.assertIn("number: 3", text)

    def test_multi_question_source_adds_soft_warning(self):
        raw = "1. 第一题\n2. 第二题"
        with mock.patch("converter.convert_file_to_blocks",
                        return_value=self._fake_pending(raw)), \
                mock.patch("converter.finish_block_review",
                           return_value="- [解答] 1. 第一题\n2. 第二题"):
            result = source_profiles.convert_good_question(
                self.source, self.root / "stage", ocr_backend="mineru",
                engine="block", normalize=False)
        self.assertTrue(result["warnings"])

    def test_llm_mode_uses_ai_action_with_provider(self):
        raw = "4. 题目"
        provider = object()
        with mock.patch("converter.convert_file_to_blocks",
                        return_value=self._fake_pending(raw)), \
                mock.patch("converter.finish_block_review",
                           return_value="- [解答] 4. 题目") as fbr:
            source_profiles.convert_good_question(
                self.source, self.root / "stage", ocr_backend="mineru",
                engine="block", provider=provider, normalize=True)
        self.assertEqual(fbr.call_args.kwargs.get("action"), "ai")
        self.assertEqual(fbr.call_args.kwargs.get("provider"), provider)


class GoodPaperTests(_ProfileTestBase):
    def test_good_paper_splits_questions_and_strips_contract_prefix(self):
        raw = ("- [解答] 1. 题目一\n【解析】因为一\n\n"
               "- [解答] 2. 题目二")
        with mock.patch.object(source_profiles, "_extract", return_value=raw):
            result = source_profiles.convert_good_paper(
                self.source, self.root / "paper", ocr_backend="mineru",
                engine="block", normalize=False)
        names = sorted(Path(p).name for p in result["cards"])
        # 原版链路（_build_import_preview）把解析并进同题卡，不再单独出解析卡。
        self.assertEqual(names, [
            "QQ20261004-224554第1题.md", "QQ20261004-224554第2题.md"])
        first = self._card_text(result, 0)
        self.assertIn("题目一", first)
        self.assertNotIn("[解答]", first)
        self.assertIn("## 解析", first)
        self.assertIn("因为一", first)
        # frontmatter 与手动导入产出对齐：题型/题号/卷内顺序都落盘，
        # 题库列表与排序才读得到。
        self.assertIn('type: "解答题"', first)
        self.assertIn("\nnumber: 1\n", first)
        self.assertIn("\norder: 1.0\n", first)
        self.assertFalse([w for w in result["warnings"] if "缺题号" in w])

    def test_numbering_may_start_away_from_one(self):
        """题号从任意标号起连续即可：12..14 必须全部成卡，且不得报缺 1..11。"""
        raw = ("- [解答] 12. 第十二题\n\n- [解答] 13. 第十三题\n\n"
               "- [解答] 14. 第十四题")
        with mock.patch.object(source_profiles, "_extract", return_value=raw):
            result = source_profiles.convert_good_paper(
                self.source, self.root / "paper_start", ocr_backend="mineru",
                engine="block", normalize=False)
        names = sorted(Path(p).name for p in result["cards"])
        self.assertEqual(names, [
            "QQ20261004-224554第12题.md", "QQ20261004-224554第13题.md",
            "QQ20261004-224554第14题.md"])
        self.assertFalse([w for w in result["warnings"] if "缺题号" in w])

    def test_natural_number_gap_is_reported_as_warning(self):
        """题号真断档（1、3）要进 warnings（面板"需人工校对"），不能静默。"""
        raw = "- [填空] 1. 第一题\n\n- [填空] 3. 第三题"
        with mock.patch.object(source_profiles, "_extract", return_value=raw):
            result = source_profiles.convert_good_paper(
                self.source, self.root / "paper_gap", ocr_backend="mineru",
                engine="block", normalize=False)
        self.assertTrue(any("缺题号" in w for w in result["warnings"]))

    def test_duplicate_numbers_get_unique_names(self):
        raw = ("- [解答] 1. 甲组第一题\n\n- [解答] 1. 乙组第一题\n"
               "\n- [解答] 2. 甲组第二题")
        with mock.patch.object(source_profiles, "_extract", return_value=raw):
            result = source_profiles.convert_good_paper(
                self.source, self.root / "paper2", ocr_backend="mineru",
                engine="block", normalize=False)
        names = sorted(Path(p).name for p in result["cards"])
        self.assertIn("QQ20261004-224554第1题.md", names)
        self.assertIn("QQ20261004-224554第1题_2.md", names)


class MaterialSplitTests(unittest.TestCase):
    def test_split_body_question_solution_with_pages(self):
        text = ("第一章 函数\n"
                "<!-- quizforge:source-page-break:2 -->\n"
                "1. 已知 $f(x)=x^2$，求最小值。\n"
                "解：由配方得最小值为 0。\n"
                "2. 判断函数 $g(x)$ 的奇偶性。\n"
                "<!-- quizforge:source-page-break:3 -->\n"
                "第二章 导数\n"
                "3. 计算 $h(x)$ 的导数。")
        blocks = source_profiles._split_material_blocks(text)
        kinds = [(b["kind"], b["page_start"], b["page_end"]) for b in blocks]
        self.assertEqual(kinds, [
            ("body", 1, 1),
            ("question", 2, 2),
            ("solution", 2, 2),
            ("question", 2, 2),
            ("body", 3, 3),
            ("question", 3, 3),
        ])
        numbers = [b["number"] for b in blocks if b["kind"] == "question"]
        self.assertEqual(numbers, [1, 2, 3])

    def test_single_candidate_stays_in_body(self):
        text = "1. 定义：设函数 $f$ 在区间上连续。"
        blocks = source_profiles._split_material_blocks(text)
        self.assertEqual([b["kind"] for b in blocks], ["body"])

    def test_section_numbering_without_question_signal_stays_body(self):
        text = ("1. 定义：设函数连续。\n"
                "2. 定理：连续函数有最值。\n"
                "3. 性质：最值唯一。")
        blocks = source_profiles._split_material_blocks(text)
        self.assertEqual([b["kind"] for b in blocks], ["body"])

    def test_leading_solution_marker_stays_in_body(self):
        text = ("解：本章说明如下。\n"
                "1. 已知 $x$，求最大值。\n"
                "2. 已知 $y$，求最小值。")
        blocks = source_profiles._split_material_blocks(text)
        # "解："在没有任何内容之前出现，不开解析块——并入正文。
        self.assertEqual(blocks[0]["kind"], "body")
        self.assertIn("解：本章说明如下。", blocks[0]["text"])

    def test_fenced_numbering_is_ignored(self):
        text = ("```\n1. 代码里的编号\n2. 代码里的编号\n```\n"
                "1. 已知 $x$，求最大值。\n"
                "2. 已知 $y$，求最小值。")
        blocks = source_profiles._split_material_blocks(text)
        questions = [b for b in blocks if b["kind"] == "question"]
        self.assertEqual(len(questions), 2)


class GoodMaterialTests(_ProfileTestBase):
    def test_material_writes_cards_pages_and_index(self):
        text = ("第一章 函数\n"
                "<!-- quizforge:source-page-break:2 -->\n"
                "1. 已知 $f(x)=x^2$，求最小值。\n"
                "解：由配方得最小值为 0。\n"
                "2. 判断函数 $g(x)$ 的奇偶性。\n"
                "<!-- quizforge:source-page-break:3 -->\n"
                "第二章 导数\n"
                "3. 计算 $h(x)$ 的导数。")
        page_info = {"status": "reliable", "located_pages": [1, 2, 3],
                     "skipped": [], "empty_pages": [], "page_total": 3}
        with mock.patch("converter.convert_file_plain_text",
                        return_value=(text, page_info)):
            result = source_profiles.convert_good_material(
                self.source, self.root / "mat", ocr_backend="mineru",
                engine="block", normalize=False)
        names = sorted(Path(p).name for p in result["cards"])
        self.assertIn("QQ20261004-224554正文第1块.md", names)
        self.assertIn("QQ20261004-224554第1题.md", names)
        self.assertIn("QQ20261004-224554解析第1块.md", names)
        self.assertIn("QQ20261004-224554第2题.md", names)
        self.assertIn("QQ20261004-224554正文第2块.md", names)
        self.assertIn("QQ20261004-224554第3题.md", names)
        self.assertIn("QQ20261004-224554_页码索引.md", names)
        # 题卡是 question（进题库），正文/解析是 document（不进题库）
        cards_dir = self._card_path(result).parent
        q_text = (cards_dir / "QQ20261004-224554第1题.md").read_text(
            encoding="utf-8")
        self.assertIn('quizforge_kind: "question"', q_text)
        self.assertIn("source_page_start: 2", q_text)
        body_text = (cards_dir / "QQ20261004-224554正文第1块.md").read_text(
            encoding="utf-8")
        self.assertIn('quizforge_kind: "document"', body_text)
        index_text = (cards_dir / "QQ20261004-224554_页码索引.md").read_text(
            encoding="utf-8")
        self.assertIn("[[QQ20261004-224554第1题]]", index_text)
        self.assertIn("第 2 页", index_text)


if __name__ == "__main__":
    unittest.main()
