import tempfile
import unittest
from pathlib import Path
from unittest import mock

import exporter


class SlidesExportTests(unittest.TestCase):
    def setUp(self):
        self.questions = [
            {"id": "q1", "body": "第一题", "type": "单选题", "solution": "解析一"},
            {"id": "q2", "body": "第二题", "type": "解答题", "solution": "解析二"},
        ]

    def test_slides_keep_order_and_use_one_question_per_page(self):
        pages = exporter.paginate(self.questions, mode="slides")

        self.assertEqual(len(pages), 2)
        self.assertEqual([p[0]["body"] for p in pages], ["第一题", "第二题"])
        self.assertTrue(all(p[0]["layout"] == "slide" for p in pages))

    def test_separate_solutions_use_one_slide_each(self):
        pages = exporter.paginate(
            self.questions, mode="slides", solution_mode="separate")

        self.assertEqual(len(pages), 4)
        self.assertEqual(
            [p[0]["kind"] for p in pages],
            ["question", "question", "solution_slide", "solution_slide"],
        )

    def test_separate_solution_slide_keeps_page_heading_but_not_field_prefix(self):
        question = {
            "id": "q1", "body": "题干", "type": "解答题",
            "solution": "【解析】由题意可得",
        }

        md = exporter.build_markdown(
            [question], "解析课件", mode="slides",
            solution_mode="separate")

        # 页级题号标题保留，但解析字段内部的结构标签不应重复显示。
        self.assertIn(r"\qslidehead{第 1 题解析}", md)
        self.assertIn("由题意可得", md)
        self.assertNotIn("【解析】", md)

    def test_markdown_has_cover_and_does_not_add_pandoc_title(self):
        md = exporter.build_markdown(self.questions, "函数_专题", mode="slides")

        self.assertTrue(md.startswith("% \n\n"))
        self.assertIn(r"\qslidecover{函数\_专题}", md)
        self.assertIn(r"\qslidehead{第 1 题}", md)
        self.assertIn(r"\qslidehead{第 2 题}", md)

    def test_question_content_is_left_aligned_at_seventy_percent_width(self):
        md = exporter.build_markdown(
            [self.questions[0]], "左侧留白", mode="slides")

        opening = (r"\noindent\begin{minipage}[t]{0.7\linewidth}"
                   r"\setlength{\parskip}{0.7em}\vspace{0pt}")
        self.assertIn(opening, md)
        self.assertIn(r"\end{minipage}\par", md)
        self.assertLess(md.index(opening), md.index(r"\qslidehead{第 1 题}"))
        self.assertLess(md.index(r"\qslidehead{第 1 题}"),
                        md.index(r"\end{minipage}\par"))

    def test_template_raises_slide_image_height_cap(self):
        # 2026-10-04 P1-15：限高值收敛到模板顶部「图片高度上限全景」的命名宏，
        # slides 文档段只引用 \qhcapslides；数值断言的归属随之转移到宏定义处。
        template = exporter.config.TEX_TEMPLATE.read_text(encoding="utf-8")
        slides_document = template.split(r"\begin{document}", 1)[1]
        slides_document = slides_document.split("$endif$", 1)[0]

        self.assertIn(r"\setlength{\qfigmaxh}{\qhcapslides\textheight}",
                      slides_document)
        self.assertIn(r"\def\qhcapslides{0.62}", template)

    def test_template_supports_math_in_markdown_heading(self):
        template = exporter.config.TEX_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn(r"\providecommand{\texorpdfstring}[2]{#1}", template)
        self.assertIn(r'"2160 -> "217F', template)
        self.assertIn(r'"2460 -> "2473', template)

    def test_template_uses_optional_plain_paper_background_for_all_modes(self):
        template = exporter.config.TEX_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn(r"\definecolor{qpapercream}{HTML}{FAF8F1}", template)
        self.assertIn("$if(paper_cream)$", template)
        self.assertIn(r"\pagecolor{qpapercream}", template)
        self.assertEqual(exporter._paper_tone_variable_args("cream"),
                         ["-V", "paper_cream=1"])
        self.assertEqual(exporter._paper_tone_variable_args("white"), [])
        self.assertEqual(exporter._paper_tone_variable_args("invalid"), [])
        self.assertNotIn("qslideblue", template)
        self.assertNotIn("QuizForge 课堂课件", template)
        self.assertNotIn(r"\colorbox", template)

    def test_wimath_logo_helper_stages_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "source-logo.pdf"
            source.write_bytes(b"%PDF-1.4\nWIMath")
            with mock.patch.object(
                    exporter.config, "WIMATH_LOGO_PDF", source):
                self.assertIsNone(
                    exporter._stage_wimath_logo("quiz_test", work, False))
                name = exporter._stage_wimath_logo(
                    "quiz_test", work, True)

            self.assertEqual(name, "quiz_test_img_wimath_logo.pdf")
            self.assertEqual((work / name).read_bytes(), source.read_bytes())

    def test_block_math_keeps_latex_commands_and_escapes_plain_backslash(self):
        source = (
            "求导：\n$$\\begin{array}{rl} y' & = \\sin x \\\\ & = 1 "
            "\\end{array}$$\n甲\\乙"
        )

        escaped = exporter._escape_stray_backslash(source)

        self.assertIn(
            r"$$\begin{array}{rl} y' & = \sin x \\ & = 1 \end{array}$$",
            escaped,
        )
        self.assertIn(r"甲\\乙", escaped)

    def test_duplicate_math_scripts_get_separate_empty_atom(self):
        source = r"条件为 $n \geqslant x ^ { 2 } ^ { , , }$，文本 ^ {a} ^ {b} 不动"

        repaired = exporter._repair_duplicate_math_scripts(source)

        self.assertIn(r"x ^ { 2 } {}^ { , , }", repaired)
        self.assertIn(r"文本 ^ {a} ^ {b} 不动", repaired)

    def test_fill_blank_markers_become_tex_lines_without_changing_subscripts(self):
        source = r"普通______，转义\_\_\_，公式 $S_6=______$，下标 $a_1$"

        repaired = exporter._normalize_fill_blank_markers(source)

        self.assertEqual(repaired.count(r"\underline{\hspace{2cm}}"), 3)
        self.assertNotIn("______", repaired)
        self.assertNotIn(r"\_\_\_", repaired)
        self.assertIn(r"$S_6=\underline{\hspace{2cm}}$", repaired)
        self.assertIn(r"$a_1$", repaired)

    def test_nested_inline_math_unwraps_only_invalid_display_shell(self):
        source = "$$\n$x$ $O$ $y$\n$$\n\n$$\n(0, \\frac{1}{2})\n$$"

        repaired = exporter._repair_nested_dollar_math(source)

        self.assertNotIn("$$\n$x$ $O$ $y$\n$$", repaired)
        self.assertIn("$x$ $O$ $y$", repaired)
        self.assertIn("$$\n(0, \\frac{1}{2})\n$$", repaired)

    def test_missing_fraction_denominator_gets_empty_argument_at_math_end(self):
        source = r"比较 $V _ \{ \dfrac { \mathrm { H } }$ 与 $\frac{1}2$"

        repaired = exporter._repair_incomplete_math_commands(source)

        self.assertIn(r"\dfrac { \mathrm { H } }{}$", repaired)
        self.assertIn(r"$\frac{1}2$", repaired)

    def test_missing_right_delimiter_at_math_end_gets_invisible_delimiter(self):
        source = "$$\n\\left| P F_1 \\right\n$$"

        repaired = exporter._repair_incomplete_math_commands(source)

        self.assertIn(r"\left| P F_1 \right.", repaired)

    def test_bare_frac_first_argument_before_font_command_gets_braces(self):
        # 源数据里的 `\dfrac1\mathrm{e}` 会让 TeX 把 \mathrm 当成第二个参数，
        # 之后连环报 Argument of \math@egroup has an extra }。
        source = "若 $\\displaystyle 0<a<\\dfrac1\\mathrm{e}$，且 $\\displaystyle " \
                 "a\\ge\\dfrac1\\mathrm{e}$"

        repaired = exporter._repair_incomplete_math_commands(source)

        self.assertEqual(repaired.count(r"\dfrac{1}{\mathrm{e}}"), 2)
        self.assertNotIn(r"\dfrac1\mathrm", repaired)
        # 合法的 TeX 简写不能被改写。
        keep = "$\\displaystyle \\dfrac12$ 与 $\\displaystyle \\frac{1}{2}$"
        self.assertEqual(exporter._repair_incomplete_math_commands(keep), keep)

    def test_array_colon_preamble_becomes_solid_bars(self):
        # 2026-10-05「历年预赛题」导出在第 29 页硬停：array 列格式里的 `:` 是
        # arydshln 语法，内置模板未加载该宏包，xelatex 报 Illegal pream-token。
        source = (
            "$\\displaystyle \\def\\arraystretch{2} \\begin{array}{c:c:c} "
            "\\hline 1&2&3\\\\ \\hline\\end{array}$"
        )

        repaired = exporter._repair_array_preambles(source)

        self.assertIn(r"\begin{array}{c|c|c}", repaired)
        self.assertNotIn("c:c", repaired)

    def test_array_with_fewer_columns_than_body_gets_extra_column(self):
        # 2021 四川第 8 题：preamble `{c:c}` 只有 2 列，表体却是 3×3 九宫格；
        # `:` 修成 `|` 后 xelatex 会以 Extra alignment tab 硬停，末尾补一列。
        source = ("$\\begin{array}{c:c}\\hline 1&2&3\\\\\\hline4&5&6\\\\"
                  "\\hline7&8&9\\\\\\hline\\end{array}$")

        repaired = exporter._repair_array_preambles(source)

        self.assertIn(r"\begin{array}{c|cc}", repaired)
        self.assertNotIn("c:c", repaired)

    def test_array_repair_counts_ampersands_inside_braces_not(self):
        # `\text{a&b}` 里的 `&` 是文本内容不是列分隔，不能把列数数多。
        source = r"$\begin{array}{c:c} \text{a&b}&c \end{array}$"

        repaired = exporter._repair_array_preambles(source)

        self.assertIn(r"\begin{array}{c|c}", repaired)

    def test_array_colon_repair_keeps_at_groups_and_plain_text(self):
        # `@{...}` 参数组里的冒号是列间插入的真实内容，不能改；
        # 数学区外的 `c:c` 普通文本也不能碰。
        source = r"$\begin{array}{c@{\quad:\quad}c} a&b \end{array}$ 与文本 c:c"

        repaired = exporter._repair_array_preambles(source)

        self.assertIn(r"\begin{array}{c@{\quad:\quad}c}", repaired)
        self.assertIn("文本 c:c", repaired)

    def test_array_colon_repair_handles_position_option_and_keeps_clean(self):
        # `[t]` 位置参数与本来就合法的 `|` 竖线：前者要能匹配，后者逐字不动。
        source = r"$\begin{array}[t]{l:l} a&b \end{array}$ 与 $\begin{array}{r|l} c&d \end{array}$"

        repaired = exporter._repair_array_preambles(source)

        self.assertIn(r"\begin{array}[t]{l|l}", repaired)
        self.assertIn(r"\begin{array}{r|l}", repaired)

    def test_unicode_math_symbols_become_latex_and_ocr_junk_is_removed(self):
        source = "\x01条件 $α∈A，θ⩾π，①．f′′$\uf8f3，且 $a$\u0338=0"

        cleaned = exporter._sanitize_export_text(source)
        repaired = exporter._normalize_unicode_math_symbols(cleaned)

        self.assertNotIn("\x01", repaired)
        self.assertNotIn("\uf8f3", repaired)
        self.assertIn(r"\alpha ", repaired)
        self.assertIn(r"\in ", repaired)
        self.assertIn(r"\theta ", repaired)
        self.assertIn(r"\geqslant ", repaired)
        self.assertIn(r"\pi ", repaired)
        self.assertIn(r"\text{\textcircled{1}}", repaired)
        self.assertIn(r"f^{\prime\prime}", repaired)
        self.assertIn(r"$a$ $\neq$ 0", repaired)

    def test_table_row_starting_with_interval_does_not_become_optional_argument(self):
        tex = exporter._table_tex(
            "<tr><td>年龄</td><td>人数</td></tr>"
            "<tr><td>[25,35)</td><td>45</td></tr>")
        self.assertIn(r"\relax [25,35) & 45", tex)

    def test_block_math_leading_colon_does_not_become_definition_list(self):
        source = "$$\n: V = \\frac{1}{3}Sh\n$$"
        self.assertEqual(
            exporter._sanitize_export_text(source),
            "$$\nV = \\frac{1}{3}Sh\n$$",
        )

    def test_unicode_slanted_inequalities_in_text_become_inline_math(self):
        source = r"当 $n$⩾2 且 x⩽$a$ 时，$b \geqslant c$ 不动"

        repaired = exporter._normalize_unicode_text_symbols(source)

        self.assertIn(r"$n$ $\geqslant$ 2", repaired)
        self.assertIn(r"x $\leqslant$ $a$", repaired)
        self.assertIn(r"$b \geqslant c$", repaired)

    def test_unicode_text_math_symbols_do_not_use_latin_text_glyphs(self):
        repaired = exporter._normalize_unicode_text_symbols("① A∩B，∵a⊥b∴λ=1")
        self.assertIn(r"\textcircled{1}", repaired)
        self.assertNotIn(r"$\textcircled{1}$", repaired)
        self.assertIn(r"$\text{\textcircled{1}}$", repaired)
        self.assertIn(r"$\cap$", repaired)
        self.assertIn(r"$\because$", repaired)
        self.assertIn(r"$\perp$", repaired)
        self.assertIn(r"$\therefore$", repaired)
        self.assertIn(r"$\lambda$", repaired)
        self.assertNotIn(
            r"\\textcircled",
            exporter._escape_stray_backslash(repaired),
        )

    def test_chinese_position_subscripts_become_lmr(self):
        repaired = exporter._normalize_unicode_math_symbols("$s_{左}^{2},s_{中}^{2},s_{右}^{2}$")
        self.assertEqual(
            repaired,
            r"$s_{\mathrm{L}}^{2},s_{\mathrm{M}}^{2},s_{\mathrm{R}}^{2}$",
        )

    def test_unicode_math_normalization_preserves_text_command_content(self):
        source = (
            r"$i_1<i_2<\cdots<i_k\text{，其中 }s_{中}^{2}$ 与 "
            r"$\text{左侧 α∈A，右端 }α∈A$"
        )

        repaired = exporter._normalize_unicode_math_symbols(source)

        self.assertIn(r"\text{，其中 }s_{\mathrm{M}}^{2}", repaired)
        self.assertIn(r"\text{左侧 α∈A，右端 }", repaired)
        self.assertIn(r"\alpha \in A", repaired)
        self.assertNotIn(r"\text{，其\mathrm{M}}", repaired)

    def test_unicode_math_normalization_preserves_all_text_mode_commands(self):
        commands = (
            "text", "textbf", "textit", "textup", "textsl", "textsc",
            "textmd", "textrm", "textnormal", "textsf", "texttt",
            "emph", "mbox", "hbox",
        )
        for command in commands:
            with self.subTest(command=command):
                protected = f"\\{command}" + "{其中 α∈A 左侧与右端}"
                source = f"${protected} + s_{{中}} + α∈A$"

                repaired = exporter._normalize_unicode_math_symbols(source)

                self.assertIn(protected, repaired)
                self.assertIn(r"s_{\mathrm{M}}", repaired)
                self.assertIn(r"\alpha \in A", repaired)

    def test_bold_greek_uses_math_bold_and_orphan_not_is_removed(self):
        source = (
            r"$\mathbf { \Delta } n + \mathbf{\Xi}$ 与 "
            r"$q_2 \not\equiv \mathrm { \Omega } ^ { \not } f(x)$，"
            r"$S_{\mathrm{\scriptsize \oplus}}$"
        )

        repaired = exporter._repair_invalid_math_font_wrappers(source)

        self.assertIn(r"\boldsymbol{\Delta}", repaired)
        self.assertIn(r"\boldsymbol{\Xi}", repaired)
        self.assertIn(r"\Omega", repaired)
        self.assertNotIn(r"\mathrm { \Omega }", repaired)
        self.assertNotIn(r"^{\not}", repaired.replace(" ", ""))
        self.assertNotIn(r"\scriptsize", repaired)


if __name__ == "__main__":
    unittest.main()
