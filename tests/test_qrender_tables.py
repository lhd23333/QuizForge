"""页面题目表格预览回归。"""

import unittest

import exporter
import qrender


class QrenderTableTests(unittest.TestCase):
    def test_mineru_html_table_renders_and_escapes_cell_content(self):
        source = (
            "统计结果如下\n"
            "<table><tr><td>地区</td><td>平均分</td></tr>"
            "<tr><td>甲&lt;script&gt;alert(1)&lt;/script&gt;</td>"
            "<td>$\\displaystyle 3.59$</td></tr></table>"
        )
        html = str(qrender.render_body(source, "填空题"))
        self.assertIn('<table class="q-table">', html)
        self.assertIn("<th>地区</th>", html)
        self.assertIn("$\\displaystyle 3.59$", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_markdown_pipe_table_renders(self):
        source = "观察表格\n\n| $x$ | 1 | 2 |\n| --- | --- | --- |\n| $y$ | 3 | 4 |"
        html = str(qrender.render_body(source, "解答题"))
        self.assertIn('<div class="q-table-wrap"', html)
        self.assertEqual(html.count("<th>"), 3)
        self.assertEqual(html.count("<td>"), 3)
        self.assertNotIn("| --- |", html)

    def test_option_letters_inside_table_do_not_trigger_choice_split(self):
        source = (
            "根据表格判断\n"
            "<table><tr><td>A.</td><td>B.</td><td>C.</td><td>D.</td></tr>"
            "<tr><td>1</td><td>2</td><td>3</td><td>4</td></tr></table>"
        )
        html = str(qrender.render_body(source, "单选题"))
        self.assertIn('<table class="q-table">', html)
        self.assertNotIn('class="q-opts"', html)

    def test_solution_uses_same_table_renderer(self):
        source = "解析如下\n| 方法 | 结果 |\n| --- | --- |\n| 方法一 | 1 |"
        html = str(qrender.render_solution(source))
        self.assertIn('<table class="q-table">', html)

    def test_solution_wrap_default_width_matches_ui_35_percent(self):
        html = str(qrender.render_solution(
            "解析正文\n\n![[answer.png]]", sol_img_split="full"))
        self.assertIn('class="q-solution-flow-img"', html)
        self.assertIn('style="width:35.0%"', html)

    def test_solution_wrap_group_direction_comes_from_first_image(self):
        source = "解析正文\n\n![[a.png]]\n![[b.png]]"
        right = str(qrender.render_solution(
            source,
            sol_img_layouts=[
                {"i": 0, "align": "right"},
                {"i": 1, "align": "left"},
            ],
            sol_img_split="full",
        ))
        left = str(qrender.render_solution(
            source,
            sol_img_layouts=[
                {"i": 0, "align": "left"},
                {"i": 1, "align": "right"},
            ],
            sol_img_split="full",
        ))
        self.assertIn("q-solution-flow-right", right)
        self.assertNotIn("q-solution-flow-left", right)
        self.assertIn("q-solution-flow-left", left)
        self.assertNotIn("q-solution-flow-right", left)

    def test_fill_question_split_renders_text_and_tail_image_in_columns(self):
        source = "曲线在点处的切线斜率为______。\n\n![[fill-tail.png]]"

        split_html = str(qrender.render_body(
            source, "填空题", img_split="opts"))
        plain_html = str(qrender.render_body(
            source, "填空题", img_split="off"))

        self.assertIn('class="q-split"', split_html)
        self.assertIn('class="q-split-text"', split_html)
        self.assertIn('class="q-split-img"', split_html)
        self.assertEqual(split_html.count("fill-tail.png"), 1)
        self.assertNotIn('class="q-split"', plain_html)

    def test_choice_columns_use_actual_text_fraction_in_image_split(self):
        options = ["abcdefghijklmnopqrstuv"] * 4
        self.assertEqual(exporter.choice_cols(options), 2)
        self.assertEqual(exporter.choice_cols(options, 0.6), 1)

        source = (
            "选择正确结论。\n"
            "A. abcdefghijklmnopqrstuv\n"
            "B. abcdefghijklmnopqrstuv\n"
            "C. abcdefghijklmnopqrstuv\n"
            "D. abcdefghijklmnopqrstuv\n\n"
            "![[choice-tail.png]]"
        )
        html = str(qrender.render_body(
            source, "单选题", img_split="opts", img_width=40))
        self.assertIn('class="q-split"', html)
        self.assertIn('class="q-opts" data-cols="1"', html)

    def test_fill_between_places_images_before_subquestions(self):
        source = (
            "根据装置完成实验。\n"
            "（1）记录示数。\n"
            "（2）分析误差。\n\n"
            "![[experiment.png]]"
        )
        html = str(qrender.render_body(
            source, "填空题", img_split="between"))
        self.assertLess(html.index("根据装置完成实验"),
                        html.index("experiment.png"))
        self.assertLess(html.index("experiment.png"), html.index("记录示数"))


if __name__ == "__main__":
    unittest.main()
