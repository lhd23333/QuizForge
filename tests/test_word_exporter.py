"""题库首页 Word 语义导出的回归测试。"""

import unittest

import word_exporter


def sample_questions() -> list[dict]:
    """固定两道不同题型，覆盖分区、题号与解析关联。"""
    return [
        {
            "id": "single-1",
            "body": "已知 $x=1$，选择正确结论。\n\nA. 甲\n\nB. 乙",
            "type": "单选题",
            "difficulty": "2",
            "solution": "答案：A。",
            "img_align": "",
            "img_width": None,
            "img_split": None,
            "img_layouts": [],
            "sol_img_split": None,
            "sol_img_layouts": [],
        },
        {
            "id": "solve-1",
            "body": "证明：$a^2+b^2\\ge 2ab$。",
            "type": "解答题",
            "difficulty": "4",
            "solution": "由 $(a-b)^2\\ge 0$ 即得。",
            "img_align": "",
            "img_width": None,
            "img_split": None,
            "img_layouts": [],
            "sol_img_split": None,
            "sol_img_layouts": [],
        },
    ]


class WordPlanTests(unittest.TestCase):
    def test_every_homepage_mode_builds_a_plan(self):
        for mode in sorted(word_exporter.SUPPORTED_MODES):
            with self.subTest(mode=mode):
                plan = word_exporter.build_word_plan(
                    sample_questions(), title="模式回归", mode=mode)
                self.assertIn("模式回归", plan.markdown)

    def test_standard_exam_has_title_info_sections_and_stable_numbers(self):
        plan = word_exporter.build_word_plan(
            sample_questions(),
            title="期中测试",
            mode="exam_std",
            solution_mode="separate",
            std_opts={
                "subject": "数学",
                "info_bar": True,
                "secret_notice": "绝密★启用前",
                "exam_notes": "先写姓名",
                "section_points": {
                    "single": "5", "multi": "6", "blank": "5", "solve": "",
                },
            },
        )

        self.assertIn("期中测试", plan.markdown)
        self.assertIn("姓名", plan.markdown)
        self.assertIn("单选题", plan.markdown)
        self.assertIn("答案与解析", plan.markdown)
        self.assertEqual(plan.markdown.count("QF-Q-1"), 2)

    def test_practice_uses_native_two_column_section(self):
        plan = word_exporter.build_word_plan(
            sample_questions(), title="刷题", mode="practice")

        self.assertTrue(any(
            section.columns == 2 and section.start == "continuous"
            for section in plan.sections
        ))

    def test_slides_are_landscape_and_one_question_per_page(self):
        plan = word_exporter.build_word_plan(
            sample_questions(), title="课件", mode="slides")

        self.assertEqual(plan.sections[-1].orientation, "slides")
        self.assertEqual(plan.markdown.count("QF_PAGE_BREAK"), 1)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaisesRegex(word_exporter.ExportError, "不支持"):
            word_exporter.build_word_plan(
                sample_questions(), title="非法模式", mode="unknown")


if __name__ == "__main__":
    unittest.main()
