"""无书签合集的 OCR 后结构分组回归：全部离线。"""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

import collection_structure
import converter


def _unit(title: str, numbers=(1, 2, 3), *, image="") -> str:
    questions = "\n\n".join(f"{number}．第 {number} 题正文" for number in numbers)
    image_line = f"\n\n![](images/{image})" if image else ""
    return (f"# {title}\n\n## 一、单选题\n\n{questions}{image_line}\n\n"
            "## 二、解答题\n")


def _ordinary_exam(title: str | None, numbers) -> str:
    heading = f"# {title}\n\n" if title else ""
    questions = "\n\n".join(
        f"{number}．第 {number} 题正文" for number in numbers)
    return f"{heading}{questions}\n\n"


class CollectionStructureTests(unittest.TestCase):
    def test_trailing_arabic_topic_ordinals_split_short_consecutive_units(self):
        raw = (
            "# 重难专题 16 位置关系\n\n"
            + "\n\n".join(f"{number}. 专题十六第 {number} 题" for number in range(1, 11))
            + "\n\n## 重难专题 17 范围问题\n\n"
            + "\n\n".join(f"{number}. 专题十七第 {number} 题" for number in range(1, 4))
            + "\n\n## 重难专题 18 定点问题\n\n"
            + "\n\n".join(f"{number}. 专题十八第 {number} 题" for number in range(1, 5))
            + "\n\n## 重难专题 19 存在性问题\n\n"
            + "\n\n".join(f"{number}. 专题十九第 {number} 题" for number in range(1, 3))
        )

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual([16, 17, 18, 19], [unit.ordinal for unit in units])
        self.assertEqual(
            [(1, 2, 3, 4, 5, 6, 7, 8, 9, 10), (1, 2, 3),
             (1, 2, 3, 4), (1, 2)],
            [unit.question_numbers for unit in units])

    def test_number_resets_split_exam_without_chinese_ordinal_titles(self):
        raw = (_ordinary_exam("模拟卷甲", range(1, 20))
               + _ordinary_exam("模拟卷乙", range(1, 20)))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertEqual(["模拟卷甲", "模拟卷乙"],
                         [unit.title for unit in units])
        self.assertTrue(all(unit.number_reset for unit in units))
        self.assertTrue(all(set(unit.question_numbers) == set(range(1, 20))
                            for unit in units))

    def test_number_resets_split_exam_without_any_titles(self):
        raw = (_ordinary_exam(None, range(1, 20))
               + _ordinary_exam(None, range(1, 20)))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(["第1组", "第2组"], [unit.title for unit in units])
        self.assertTrue(all(unit.generated_title for unit in units))
        self.assertTrue(all(set(unit.question_numbers) == set(range(1, 20))
                            for unit in units))

    def test_number_reset_does_not_split_consecutive_duplicate_one(self):
        raw = (_ordinary_exam("模拟卷甲", [1, 1, *range(2, 20)])
               + _ordinary_exam("模拟卷乙", range(1, 20)))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertEqual((1, 1, 2), units[0].question_numbers[:3])

    def test_number_reset_rejects_low_question_coverage(self):
        sparse = (1, 2, 4, 6, 8, 10)
        raw = (_ordinary_exam("模拟卷甲", sparse)
               + _ordinary_exam("模拟卷乙", sparse))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "覆盖不足"):
            collection_structure.split_markdown_units(raw)

    def test_explicit_title_conflict_does_not_fall_back_to_number_resets(self):
        raw = (_unit("训练一：运动学", numbers=range(1, 20))
               + "# 训练二：没有题号\n\n"
               + _unit("训练三：力学", numbers=range(1, 20)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "训练二"):
            collection_structure.split_markdown_units(raw)

    def test_number_reset_accepts_multicolumn_out_of_order_numbers(self):
        multicolumn = [*range(1, 12), 14, 15, 12, 13, *range(16, 20)]
        raw = (_ordinary_exam("力学测试甲", multicolumn)
               + _ordinary_exam("力学测试乙", multicolumn))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertEqual(set(range(1, 20)), set(units[0].question_numbers))
        self.assertEqual(multicolumn, list(units[1].question_numbers))

    def test_number_reset_expands_plain_titled_exams_after_strong_units(self):
        raw = (_unit("精练一：运动学", numbers=range(1, 8))
               + _unit("精练二：力学", numbers=range(1, 8))
               + _ordinary_exam("期末测试卷（A 卷）", range(1, 16))
               + _ordinary_exam("期末测试卷（B 卷）", range(1, 16)))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(4, len(units))
        self.assertEqual(
            ["精练一:运动学", "精练二:力学", "期末测试卷(A 卷)",
             "期末测试卷(B 卷)"],
            [unit.title for unit in units])
        self.assertTrue(all(unit.number_reset for unit in units[1:]))

    def test_internal_question_section_restart_is_not_a_new_exam(self):
        raw = (_unit("精练一：运动学", numbers=range(1, 8))
               + _unit("精练二：力学", numbers=range(1, 8))
               + "## 填空题\n\n"
               + "\n\n".join(
                   f"{number}．填空第 {number} 题" for number in range(1, 8)))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertIn("## 填空题", units[-1].markdown)

    def test_internal_titled_reset_with_too_few_questions_fails_closed(self):
        raw = (_unit("精练一：运动学", numbers=range(1, 8))
               + _unit("精练二：力学", numbers=range(1, 8))
               + _ordinary_exam("残缺期末卷", (1, 2, 4)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "覆盖不足"):
            collection_structure.split_markdown_units(raw)

    def test_number_reset_rejects_question_section_as_new_exam(self):
        raw = ("## 选择题\n\n"
               + "\n\n".join(
                   f"{number}．选择题 {number}" for number in range(1, 11))
               + "\n\n## 填空题\n\n"
               + "\n\n".join(
                   f"{number}．填空题 {number}" for number in range(1, 11)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "题型标题"):
            collection_structure.split_markdown_units(raw)

    def test_number_reset_keeps_exam_title_before_question_section(self):
        questions = "\n\n".join(
            f"{number}．第 {number} 题" for number in range(1, 20))
        raw = (f"# 模拟卷甲\n\n## 选择题\n\n{questions}\n\n"
               f"# 模拟卷乙\n\n## 选择题\n\n{questions}\n")

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(["模拟卷甲", "模拟卷乙"],
                         [unit.title for unit in units])
        self.assertTrue(units[1].markdown.startswith("# 模拟卷乙"))

    def test_number_reset_title_search_does_not_cross_nonempty_body(self):
        raw = (_ordinary_exam("模拟卷甲", range(1, 20))
               + "# 不应偷取的标题\n\n上一题的补充正文\n\n"
               + _ordinary_exam(None, range(1, 20)))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual("第2组", units[1].title)
        self.assertTrue(units[1].generated_title)
        self.assertNotIn("不应偷取的标题", units[1].markdown)

    def test_number_reset_pairing_still_requires_question_overlap(self):
        exams = (_ordinary_exam("模拟卷甲", range(1, 20))
                 + _ordinary_exam("模拟卷乙", range(1, 20)))
        solutions = (_unit("训练一：模拟卷甲", numbers=range(1, 20))
                     + _unit("训练二：模拟卷乙", numbers=range(1, 11)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "题号重合度不足"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_number_reset_pairing_rejects_conflicting_real_titles(self):
        exams = (_ordinary_exam("模拟卷甲", range(1, 20))
                 + _ordinary_exam("模拟卷乙", range(1, 20)))
        solutions = (_unit("训练一：光学", numbers=range(1, 20))
                     + _unit("训练二：热学", numbers=range(1, 20)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError,
                "都有明确标题"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_solution_number_reset_pairing_also_requires_eighty_percent(self):
        exams = (_unit("训练一：运动学", numbers=range(1, 6))
                 + _unit("训练二：力学", numbers=range(1, 6)))
        solutions = (_ordinary_exam(None, range(1, 6))
                     + _ordinary_exam(None, range(1, 4)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "题号重合度不足"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_solution_plain_title_moves_with_exam_driven_reset_group(self):
        exams = (_unit("精练一：运动学", numbers=range(1, 8))
                 + _unit("精练二：力学", numbers=range(1, 8))
                 + _ordinary_exam("期末测试卷", range(1, 16)))
        solutions = (
            _ordinary_exam("精练一：运动学参考答案", range(1, 8))
            + _ordinary_exam("精练二：力学参考答案", range(1, 8))
            + _ordinary_exam("期末测试卷参考答案", range(1, 16)))

        pairs = collection_structure.pair_markdown_collections(
            exams, solutions)

        self.assertEqual(3, len(pairs))
        self.assertTrue(
            pairs[-1].solution.markdown.startswith("# 期末测试卷参考答案"))
        self.assertNotIn("期末测试卷参考答案", pairs[-2].solution.markdown)

    def test_sparse_solution_accepts_seventy_five_percent_with_strong_title(self):
        exams = (_unit("训练一：运动学", numbers=range(1, 13))
                 + _unit("训练二：力学", numbers=range(1, 13)))
        solutions = (
            _unit("《训练一：运动学》参考答案", numbers=range(1, 13))
            + _unit("《训练二：力学》参考答案",
                    numbers=(1, 2, 3, 4, 6, 7, 8, 10, 12)))

        pairs = collection_structure.pair_markdown_collections(
            exams, solutions)

        self.assertEqual(2, len(pairs))
        self.assertEqual(
            {1, 2, 3, 4, 6, 7, 8, 10, 12},
            set(pairs[1].solution.question_numbers))

    def test_sparse_solution_below_seventy_five_percent_still_rejected(self):
        exams = (_unit("训练一：运动学", numbers=range(1, 13))
                 + _unit("训练二：力学", numbers=range(1, 13)))
        solutions = (
            _unit("《训练一：运动学》参考答案", numbers=range(1, 13))
            + _unit("《训练二：力学》参考答案",
                    numbers=(1, 2, 3, 4, 6, 8, 10, 12)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError,
                "题号重合度不足"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_sparse_solution_with_extra_numbers_still_rejected(self):
        exams = (_unit("训练一：运动学", numbers=range(1, 13))
                 + _unit("训练二：力学", numbers=range(1, 13)))
        solutions = (
            _unit("《训练一：运动学》参考答案", numbers=range(1, 13))
            + _unit("《训练二：力学》参考答案",
                    numbers=(1, 2, 3, 4, 5, 6, 7, 8, 9, 28, 29, 30)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError,
                "题号重合度不足"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_sparse_solution_without_real_title_still_rejected(self):
        exams = (_unit("训练一：运动学", numbers=range(1, 13))
                 + _unit("训练二：力学", numbers=range(1, 13)))
        solutions = (
            _unit("《训练一：运动学》参考答案", numbers=range(1, 13))
            + _ordinary_exam(None, (1, 2, 3, 4, 6, 7, 8, 10, 12)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError,
                "题号重合度不足"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_sparse_solution_with_wrong_ordinal_still_rejected(self):
        exams = (_unit("训练一：运动学", numbers=range(1, 13))
                 + _unit("训练二：力学", numbers=range(1, 13)))
        solutions = (
            _unit("《训练一：运动学》参考答案", numbers=range(1, 13))
            + _unit("《训练三：力学》参考答案",
                    numbers=(1, 2, 3, 4, 6, 7, 8, 10, 12)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError,
                "题号重合度不足"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_two_adjacent_sparse_solution_groups_still_rejected(self):
        exams = (_unit("训练一：运动学", numbers=range(1, 13))
                 + _unit("训练二：力学", numbers=range(1, 13))
                 + _unit("训练三：光学", numbers=range(1, 13)))
        sparse = (1, 2, 3, 4, 6, 7, 8, 10, 12)
        solutions = (
            _unit("《训练一：运动学》参考答案", numbers=range(1, 13))
            + _unit("《训练二：力学》参考答案", numbers=sparse)
            + _unit("《训练三：光学》参考答案", numbers=sparse))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError,
                "题号重合度不足"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_splits_strong_titles_and_keeps_images_in_their_unit(self):
        raw = ("# 封面\n\n目录不进入分组\n\n"
               + _unit("训练一：运动学基础", image="first.png")
               + _unit("专题二：力学进阶", image="second.png"))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertEqual([1, 2], [unit.ordinal for unit in units])
        self.assertNotIn("目录不进入分组", units[0].markdown)
        self.assertIn("images/first.png", units[0].markdown)
        self.assertNotIn("images/second.png", units[0].markdown)
        self.assertIn("images/second.png", units[1].markdown)

    def test_uses_weak_chinese_ordinal_titles_but_not_question_sections(self):
        raw = (_unit("一、运动学基础")
               + _unit("二、力学进阶"))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(["一、运动学基础", "二、力学进阶"],
                         [unit.title for unit in units])
        self.assertTrue(all("一、单选题" in unit.markdown for unit in units))

    def test_numbered_parts_do_not_override_weak_collection_titles(self):
        raw = ("# 一、运动学\n\n## 第一部分 选择题\n\n"
               "1．第一题。\n\n2．第二题。\n\n"
               "## 第二部分 非选择题\n\n3．第三题。\n\n"
               "# 二、力学\n\n## 第一部分 选择题\n\n"
               "1．第一题。\n\n2．第二题。\n\n"
               "## 第二部分 非选择题\n\n3．第三题。")

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(["一、运动学", "二、力学"],
                         [unit.title for unit in units])

    def test_rejects_title_without_continuous_question_numbers(self):
        raw = (_unit("练习一：运动学", numbers=(1, 3))
               + _unit("练习二：力学"))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "基本连续"):
            collection_structure.split_markdown_units(raw)

    def test_recovery_only_accepts_sparse_consecutive_strong_titles(self):
        sparse = (1, 2, 4, 6, 8)
        raw = (_unit("精练一：运动学", numbers=sparse)
               + _unit("精练二：力学", numbers=sparse))

        with self.assertRaises(collection_structure.CollectionStructureError):
            collection_structure.split_markdown_units(raw)
        units = collection_structure.split_markdown_units_for_recovery(raw)

        self.assertEqual([1, 2], [unit.ordinal for unit in units])
        self.assertEqual([set(sparse), set(sparse)],
                         [set(unit.question_numbers) for unit in units])

    def test_recovery_only_expands_sparse_plain_titled_exam(self):
        sparse = (1, 2, 4, 6, 8)
        raw = (_unit("精练一：运动学", numbers=sparse)
               + _unit("精练二：力学", numbers=sparse)
               + _ordinary_exam("期末综合测试卷", sparse))

        units = collection_structure.split_markdown_units_for_recovery(raw)

        self.assertEqual(3, len(units))
        self.assertEqual("期末综合测试卷", units[-1].title)
        self.assertEqual(set(sparse), set(units[-1].question_numbers))

    def test_recovery_only_rejects_sparse_nonconsecutive_strong_titles(self):
        sparse = (1, 2, 4, 6, 8)
        raw = (_unit("精练一：运动学", numbers=sparse)
               + _unit("精练三：力学", numbers=sparse))

        with self.assertRaises(collection_structure.CollectionStructureError):
            collection_structure.split_markdown_units_for_recovery(raw)

    def test_recovery_only_rejects_short_false_title_evidence(self):
        raw = (_unit("精练一：运动学", numbers=(1, 4))
               + _unit("精练二：力学", numbers=(1, 4)))

        with self.assertRaises(collection_structure.CollectionStructureError):
            collection_structure.split_markdown_units_for_recovery(raw)

    def test_fullwidth_question_marker_can_be_followed_by_a_year(self):
        raw = ("# 训练一：天体运动\n\n"
               "1．2021 年发射某卫星。\n\n2．2025 年观测某行星。\n\n"
               "# 训练二：功和能\n\n"
               "1．2024 年完成某实验。\n\n2．2026 年重复实验。")

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertEqual((1, 2), units[0].question_numbers)

    def test_priority_stars_before_question_numbers_are_not_structure(self):
        raw = ("# 精练一：运动学\n\n★1．第一题。\n\n★★2．第二题。\n\n"
               "# 精练二：力学\n\n☆1．第一题。\n\n☆☆2．第二题。")

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertEqual([(1, 2), (1, 2)],
                         [unit.question_numbers for unit in units])

    def test_di_ti_marker_can_be_followed_immediately_by_chinese_text(self):
        raw = ("# 训练一：运动学\n\n第1题如图所示。\n\n第二题下列说法正确。\n\n"
               "# 训练二：力学\n\n第1题计算拉力。\n\n第二题求加速度。")

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertEqual((1, 2), units[0].question_numbers)

    def test_ordinary_sentence_starting_with_yici_is_not_a_title(self):
        raw = (_unit("练习一：运动学")
               + "\n一次速度减为 0 时,满足某式。\n\n"
               + _unit("练习二：力学"))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertIn("一次速度减为", units[0].markdown)

    def test_first_time_sentence_is_not_a_title(self):
        raw = ("# 第一讲：圆周运动\n\n"
               "1．小球第一次摆到左侧最高点的过程中,下列说法正确的是( )\n\n"
               "2．第二题\n\n"
               "# 第二讲：万有引力\n\n"
               "1．第一题\n\n2．第二题\n")

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertIn("第一次摆到左侧最高点", units[0].markdown)

    def test_internal_numbered_heading_does_not_replace_unit_title(self):
        for heading in ("步骤一：建立坐标系", "解法一：受力分析",
                        "方法一：整体法", "情形一：加速运动"):
            with self.subTest(heading=heading):
                raw = ("# 精练一：运动学\n\n1．第一题必须保留\n\n"
                       f"{heading}\n\n2．第二题\n\n3．第三题\n\n"
                       "# 精练二：力学\n\n1．第一题\n\n"
                       "2．第二题\n\n3．第三题\n")

                units = collection_structure.split_markdown_units(raw)

                self.assertEqual(2, len(units))
                self.assertEqual("精练一:运动学", units[0].title)
                self.assertIn("1．第一题必须保留", units[0].markdown)
                self.assertIn(heading, units[0].markdown)

    def test_missing_first_question_number_stops_instead_of_guessing_boundary(self):
        raw = (_unit("精练一：运动学", numbers=(1, 2, 3))
               + _unit("精练二：力学", numbers=(2, 3, 4)))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "基本连续"):
            collection_structure.split_markdown_units(raw)

    def test_one_question_unit_is_not_merged_with_next_missing_first_number(self):
        raw = (_unit("精练一：运动学", numbers=(1,))
               + _unit("精练二：力学", numbers=(2, 3, 4))
               + _unit("精练三：光学", numbers=(1, 2, 3)))

        with self.assertRaises(collection_structure.CollectionStructureError):
            collection_structure.split_markdown_units(raw)

    def test_financial_chinese_ordinal_title(self):
        raw = (_unit("专题壹：运动") + _unit("专题贰：受力"))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual([1, 2], [unit.ordinal for unit in units])

    def test_ignores_catalog_titles_and_repeated_running_headers(self):
        raw = ("# 目录\n\n练习一：运动学\n\n练习二：力学\n\n"
               + _unit("练习一：运动学", numbers=(1, 2))
               + "\n# 练习一：运动学\n\n3．第三题正文\n\n"
               + _unit("练习二：力学", numbers=(1, 2, 3)))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertTrue(units[0].markdown.startswith("# 练习一：运动学"))
        self.assertIn("3．第三题正文", units[0].markdown)
        self.assertNotIn("# 目录", units[0].markdown)

    def test_running_header_before_question_two_does_not_drop_question_one(self):
        raw = ("# 练习一：运动学\n\n1．第一题必须保留。\n\n"
               "# 练习一：运动学\n\n2．第二题。\n\n3．第三题。\n\n"
               + _unit("练习二：力学", numbers=(1, 2, 3)))

        units = collection_structure.split_markdown_units(raw)

        self.assertEqual(2, len(units))
        self.assertIn("1．第一题必须保留", units[0].markdown)

    def test_answer_number_table_can_confirm_solution_unit(self):
        answers = ("# 训练一：运动学\n\n"
                   "| 题号 | 1 | 2 | 3 |\n|---|---|---|---|\n"
                   "| 答案 | A | B | C |\n\n"
                   "# 训练二：力学\n\n"
                   "|题号|1|2|3|\n|---|---|---|---|\n"
                   "|答案|B|C|D|\n")

        units = collection_structure.split_markdown_units(answers)

        self.assertEqual(2, len(units))
        self.assertEqual((1, 2, 3), units[0].question_numbers)

    def test_solution_missing_titles_falls_back_to_confirmed_number_resets(self):
        exams = (_unit("精练一：运动学", numbers=(1, 2, 3, 4))
                 + _unit("精练二：受力", numbers=(1, 2, 3, 4))
                 + _unit("精练三：圆周运动", numbers=(1, 2, 3, 4)))
        solutions = (
            _unit("精练一：运动学", numbers=(1, 2, 3, 4))
            + "\n1．第一题答案\n\n2．第二题答案\n\n3．第三题答案\n\n"
            "4．第四题答案\n\n"
            + _unit("精练三：圆周运动", numbers=(1, 2, 3, 4)))

        pairs = collection_structure.pair_markdown_collections(
            exams, solutions)

        self.assertEqual(3, len(pairs))
        self.assertEqual("精练二:受力", pairs[1].solution.title)
        self.assertTrue(pairs[1].solution.markdown.startswith("1．第一题答案"))

    def test_number_reset_fallback_ignores_duplicate_first_answer(self):
        exams = (_unit("精练一：运动学") + _unit("精练二：受力"))
        solutions = (
            "# 精练一：运动学\n\n1．第一题旧版答案\n\n"
            "1．第一题新版答案\n\n2．第二题答案\n\n3．第三题答案\n\n"
            "1．下一组第一题答案\n\n2．下一组第二题答案\n\n"
            "3．下一组第三题答案\n")

        pairs = collection_structure.pair_markdown_collections(
            exams, solutions)

        self.assertEqual(2, len(pairs))
        self.assertIn("第一题旧版答案", pairs[0].solution.markdown)
        self.assertIn("第一题新版答案", pairs[0].solution.markdown)
        self.assertEqual("精练二:受力", pairs[1].solution.title)

    def test_generic_solution_titles_pair_by_ordinal_and_allow_missing_answers(self):
        exams = (_unit("提升精练一：运动学", numbers=range(1, 13))
                 + _unit("提升精练二：受力", numbers=range(1, 13)))
        solutions = (
            "## 提升精练一参考答案:\n\n"
            + "\n\n".join(
                f"{number}．第 {number} 题解析" for number in range(1, 13))
            + "\n\n## 提升精练二参考答案:\n\n"
            + "\n\n".join(
                f"{number}．第 {number} 题解析"
                for number in (*range(1, 11), 12)))

        pairs = collection_structure.pair_markdown_collections(
            exams, solutions)

        self.assertEqual(2, len(pairs))
        self.assertEqual(set(range(1, 13)),
                         set(pairs[0].solution.question_numbers))
        self.assertEqual(set((*range(1, 11), 12)),
                         set(pairs[1].solution.question_numbers))

    def test_generic_solution_title_still_requires_matching_ordinal(self):
        exams = (_unit("提升精练一：运动学")
                 + _unit("提升精练二：受力"))
        solutions = (
            "## 提升精练一参考答案:\n\n1．答\n\n2．答\n\n3．答\n\n"
            "## 提升精练三参考答案:\n\n1．答\n\n2．答\n\n3．答\n")

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError,
                "不能可靠对应"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_number_reset_fallback_rejects_unrelated_detected_title(self):
        exams = (_unit("精练一：运动学") + _unit("精练二：受力"))
        solutions = (
            _unit("精练一：运动学")
            + _unit("精练二：光学"))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError,
                "不能可靠对应"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_pairs_by_topic_when_source_ordinals_contain_typos(self):
        exams = (_unit("精练十七：平抛与斜抛")
                 + _unit("精练十七：立体空间中的抛体"))
        solutions = (_unit("《精练十六：平抛与斜抛》参考答案")
                     + _unit("《精练十七：立体空间中的抛体》参考答案"))

        pairs = collection_structure.pair_markdown_collections(exams, solutions)

        self.assertEqual(2, len(pairs))
        self.assertEqual(16, pairs[0].solution.ordinal)
        self.assertEqual(17, pairs[0].exam.ordinal)
        self.assertIn("平抛与斜抛", pairs[0].solution.topic)

    def test_rejects_same_position_titles_with_unrelated_topics(self):
        exams = (_unit("练习一：运动学") + _unit("练习二：力学"))
        solutions = (_unit("练习一：光学") + _unit("练习二：热学"))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "不能可靠对应"):
            collection_structure.pair_markdown_collections(exams, solutions)

    def test_same_ordinal_does_not_hide_similar_but_different_topics(self):
        for exam_topic, solution_topic in (
                ("运动学基础", "力学基础"),
                ("动量守恒定律", "机械能守恒定律"),
                ("直线运动", "曲线运动"),
                ("电场基础", "磁场基础"),
                ("动量定理", "动能定理"),
                ("牛顿第一定律", "牛顿第二定律"),
                ("匀加速直线运动", "匀减速直线运动"),
                ("完全弹性碰撞", "完全非弹性碰撞"),
                ("第一宇宙速度", "第二宇宙速度"),
                ("正电荷的电场", "负电荷的电场")):
            exams = (_unit(f"练习一：{exam_topic}")
                     + _unit("练习二：电场"))
            solutions = (_unit(f"练习一：{solution_topic}")
                         + _unit("练习二：电场"))
            with self.subTest(exam=exam_topic, solution=solution_topic), \
                    self.assertRaisesRegex(
                        collection_structure.CollectionStructureError,
                        "不能可靠对应"):
                collection_structure.pair_markdown_collections(
                    exams, solutions)

    def test_repeated_topics_require_unique_ordinal_alignment(self):
        exams = (_unit("练习一：力学") + _unit("练习二：力学"))
        solutions = (_unit("练习二：力学") + _unit("练习三：力学"))

        with self.assertRaisesRegex(
                collection_structure.CollectionStructureError, "不能唯一定位"):
            collection_structure.pair_markdown_collections(exams, solutions)


class CollectionConverterTests(unittest.TestCase):
    def test_manual_cache_edit_materializes_single_unit_without_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "raw"
            with mock.patch.object(converter, "_RAW_MD_ROOT", raw_root):
                cache_dirs = converter.allocate_collection_cache_dirs(False)
                workspace = Path(cache_dirs[0])
                images = workspace / "images"
                images.mkdir(parents=True)
                (images / "graph.png").write_bytes(b"image")
                converter._write_collection_cache(
                    workspace,
                    "# 初稿\n\n1．原题\n\n![](images/graph.png)",
                    {"provider": "doc2x"})

                before = converter.collection_cache_snapshot(
                    cache_dirs, has_solution=False)
                edited = "# 调整后\n\n1．第一题\n\n2．第二题\n\n![](images/graph.png)"
                after = converter.update_collection_cache_markdown(
                    cache_dirs, has_solution=False,
                    exam_markdown=edited,
                    expected_revision=before["revision"])
                unit = converter.materialize_collection_cache_as_unit(
                    cache_dirs, has_solution=False, title="整份试卷",
                    ocr_backend="doc2x")

                self.assertNotEqual(before["revision"], after["revision"])
                self.assertEqual(
                    edited,
                    Path(unit["raw_path"]).read_text(encoding="utf-8"))
                self.assertTrue(
                    (Path(unit["workspace_dir"]) / "images" / "graph.png").is_file())
                self.assertEqual("doc2x", unit["ocr_backend"])
                with self.assertRaisesRegex(converter.ConvertError, "其他窗口"):
                    converter.update_collection_cache_markdown(
                        cache_dirs, has_solution=False,
                        exam_markdown=edited + "\n\n3．第三题",
                        expected_revision=before["revision"])

                converter.cleanup_collection_workspace(unit["workspace_dir"])
                converter.cleanup_collection_workspace(cache_dirs[0])

    def test_collection_unit_blocksplit_reads_raw_without_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "collection_unit_demo_raw.md"
            raw_path.write_text(
                "# 练习一：运动学\n\n"
                "1．这是第一题的完整测试正文，用于确认无需再次调用 OCR。\n\n"
                "2．这是第二题的完整测试正文，用于确认会保留原始内容。\n\n"
                "3．这是第三题的完整测试正文，用于确认可以独立重转。",
                encoding="utf-8")

            pending = converter.convert_collection_unit_to_blocks(
                raw_path, source_name="练习一")

        self.assertEqual([1, 2, 3],
                         [block["number"] for block in pending["blocks"]])
        self.assertTrue(pending["defer_cleanup"])
        self.assertEqual("collection_unit_demo",
                         pending["extract_dirs"][0]["stem"])

    def test_recognizes_two_whole_books_once_and_materializes_unit_images(self):
        exam_raw = (_unit("练习一：运动学", image="exam-one.png")
                    + _unit("练习二：力学"))
        solution_raw = (_unit("《练习一：运动学》参考答案",
                              image="solution-one.png")
                        + _unit("《练习二：力学》参考答案"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam = root / "exam.pdf"
            solution = root / "solution.pdf"
            exam.write_bytes(b"exam")
            solution.write_bytes(b"solution")
            calls = []

            def fake_parse(path, extract_dir, _cfg, **kwargs):
                calls.append((Path(path).name, kwargs.get("collection")))
                images = Path(extract_dir) / "images"
                images.mkdir(parents=True, exist_ok=True)
                if Path(path) == exam:
                    (images / "exam-one.png").write_bytes(b"exam image")
                    return exam_raw, "exam.md", {"side": "exam"}
                (images / "solution-one.png").write_bytes(b"solution image")
                return solution_raw, "solution.md", {"side": "solution"}

            with mock.patch.object(converter, "_RAW_MD_ROOT", root / "raw"), \
                    mock.patch.object(converter, "_alpha_cwd", return_value=nullcontext()), \
                    mock.patch.object(
                        converter, "_load_config_for_user",
                        return_value=SimpleNamespace(mineru_model_version="test")), \
                    mock.patch.object(converter, "_prep_for_ocr",
                                      side_effect=lambda path, *_args, **_kwargs: Path(path)), \
                    mock.patch.object(converter, "_parse_with_ocr_backend",
                                      side_effect=fake_parse):
                units = converter.recognize_collection_units(exam, solution)

                self.assertEqual(2, len(units))
                self.assertEqual([("exam.pdf", True), ("solution.pdf", True)],
                                 sorted(calls))
                first_raw = Path(units[0]["raw_path"]).read_text(encoding="utf-8")
                self.assertIn("# 参考答案与解析", first_raw)
                self.assertIn("images/exam_exam-one.png", first_raw)
                self.assertIn("images/solution_solution-one.png", first_raw)
                first_images = Path(units[0]["workspace_dir"]) / "images"
                self.assertTrue((first_images / "exam_exam-one.png").is_file())
                self.assertTrue((first_images / "solution_solution-one.png").is_file())
                cache_dirs = units[0]["collection_cache_dirs"]
                self.assertEqual(2, len(cache_dirs))
                for unit in units:
                    converter.cleanup_collection_workspace(unit["workspace_dir"])
                for directory in cache_dirs:
                    converter.cleanup_collection_workspace(directory)
                self.assertTrue(all(not Path(unit["workspace_dir"]).exists()
                                    for unit in units))
                self.assertTrue(all(not Path(path).exists()
                                    for path in cache_dirs))

    def test_structure_failure_reuses_both_ocr_caches_with_solution_image(self):
        # 只有一个结构标题，OCR 成功但分组必然失败；第二次传回异常携带的
        # 缓存目录时，两侧都不得再次进入 OCR，解析图片合并也必须幂等。
        bad_exam = _unit("练习一：运动学", image="exam-one.png")
        bad_solution = _unit(
            "《练习一：运动学》参考答案", image="solution-one.png")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam = root / "exam.pdf"
            solution = root / "solution.pdf"
            exam.write_bytes(b"exam")
            solution.write_bytes(b"solution")
            calls = []

            def fake_parse(path, extract_dir, _cfg, **_kwargs):
                calls.append(Path(path).name)
                images = Path(extract_dir) / "images"
                images.mkdir(parents=True, exist_ok=True)
                if Path(path) == exam:
                    (images / "exam-one.png").write_bytes(b"exam image")
                    return bad_exam, "exam.md", {"side": "exam"}
                (images / "solution-one.png").write_bytes(b"solution image")
                return bad_solution, "solution.md", {"side": "solution"}

            with mock.patch.object(converter, "_RAW_MD_ROOT", root / "raw"), \
                    mock.patch.object(converter, "_alpha_cwd", return_value=nullcontext()), \
                    mock.patch.object(
                        converter, "_load_config_for_user",
                        return_value=SimpleNamespace(mineru_model_version="test")), \
                    mock.patch.object(converter, "_prep_for_ocr",
                                      side_effect=lambda path, *_args, **_kwargs: Path(path)), \
                    mock.patch.object(converter, "_parse_with_ocr_backend",
                                      side_effect=fake_parse):
                with self.assertRaises(converter.CollectionRecognitionError) as first:
                    converter.recognize_collection_units(exam, solution)
                cache_dirs = first.exception.workspace_dirs
                self.assertEqual(2, len(cache_dirs))
                self.assertEqual(2, len(calls))

                with self.assertRaises(converter.CollectionRecognitionError):
                    converter.recognize_collection_units(
                        exam, solution, cache_dirs=cache_dirs)
                self.assertEqual(2, len(calls))
                for directory in cache_dirs:
                    converter.cleanup_collection_workspace(directory)
                self.assertTrue(all(not Path(path).exists()
                                    for path in cache_dirs))


if __name__ == "__main__":
    unittest.main()
