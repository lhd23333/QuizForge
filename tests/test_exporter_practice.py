import subprocess
import tempfile
import unittest
from pathlib import Path

import exporter


class PracticeExportTests(unittest.TestCase):
    def setUp(self):
        self.questions = [
            {"id": "q1", "body": "第一题", "type": "填空题",
             "difficulty": "1", "solution": ""},
            {"id": "q2", "body": "第二题\n（1）第一问\n（2）第二问",
             "type": "解答题", "difficulty": "4", "solution": "解析"},
        ]

    def test_practice_groups_types_like_simple_exam_and_passes_difficulty(self):
        mixed = [
            {"id": "s1", "body": "解答一", "type": "解答题",
             "difficulty": "5", "solution": ""},
            {"id": "b1", "body": "填空一", "type": "填空题",
             "difficulty": "1", "solution": ""},
            {"id": "c1", "body": "单选一", "type": "单选题",
             "difficulty": "2", "solution": ""},
            {"id": "c2", "body": "单选二", "type": "单选题",
             "difficulty": "3", "solution": ""},
        ]
        pages = exporter.paginate(mixed, mode="practice")
        headings = [b["text"] for b in pages[0] if b["kind"] == "heading"]
        questions = [b for b in pages[0] if b["kind"] == "question"]

        self.assertEqual(len(pages), 1)
        self.assertEqual(headings, ["一、单选题", "二、填空题", "三、解答题"])
        self.assertEqual([b["body"] for b in questions],
                         ["单选一", "单选二", "填空一", "解答一"])
        self.assertEqual([b["num"] for b in questions], [1, 2, 3, 4])
        self.assertTrue(all(b["layout"] == "practice" for b in questions))
        self.assertEqual([b["difficulty"] for b in questions],
                         ["2", "3", "1", "5"])
        self.assertEqual(questions[-1]["heading"], "三、解答题")
        self.assertEqual(questions[-1]["practice_solve_index"], 0)
        solve_heading = next(
            b for b in pages[0]
            if b.get("kind") == "heading" and b.get("text") == "三、解答题"
        )
        self.assertTrue(solve_heading["suppress_render"])

    def test_answer_space_uses_difficulty_and_top_level_subquestions(self):
        easy = exporter._practice_answer_space("求证。", "1")
        hard = exporter._practice_answer_space(
            "（1）第一问\n（i）分步\n（2）第二问\n（3）第三问", "5")
        capped = exporter._practice_answer_space(
            "\n".join(f"（{i}）第{i}问" for i in range(1, 10)), "5")

        self.assertEqual(easy, r"3.00\baselineskip")
        self.assertEqual(hard, r"10.00\baselineskip")
        self.assertEqual(capped, r"12.00\baselineskip")

    def test_markdown_wraps_questions_in_unbalanced_two_columns(self):
        md = exporter.build_markdown(
            self.questions, "椭圆_刷题", mode="practice")

        self.assertTrue(md.startswith("% \n\n"))
        self.assertIn(r"\begin{center}{\LARGE 椭圆\_刷题}", md)
        self.assertIn(r"\qpracticebegin", md)
        self.assertIn(r"\qpracticeend", md)
        self.assertIn(r"\thispagestyle{fancy}", md)
        self.assertIn(r"\begin{samepage}", md)
        self.assertIn(r"\end{samepage}", md)
        self.assertIn(r"\vspace*{7.50\baselineskip}", md)
        self.assertIn("一、填空题", md)
        self.assertIn("二、解答题", md)
        self.assertNotIn("``````", md)

    def test_physics_export_renames_blank_section_without_changing_type(self):
        pages = exporter.paginate(
            self.questions, mode="exam_std", bank_subject="physics",
            std_opts={"section_points": {"blank": "5"}},
        )
        headings = [block["text"] for block in pages[0]
                    if block["kind"] == "heading"]

        self.assertEqual(headings, ["一、实验题", "二、解答题"])
        blank = next(block for block in pages[0]
                     if block.get("kind") == "question" and block["num"] == 1)
        self.assertEqual(blank["type"], "填空题")

    def test_standard_exam_title_is_regular_and_subject_is_larger_bold(self):
        md = exporter.build_markdown(
            self.questions, "湖北联考", mode="exam_std",
            std_opts={"subject": "物理", "info_bar": False},
            bank_subject="physics",
        )

        self.assertIn(r"\begin{center}{\LARGE 湖北联考}\end{center}", md)
        self.assertIn(r"\begin{center}{\Large\bfseries 物理}\end{center}", md)
        self.assertNotIn(r"\LARGE\bfseries 湖北联考", md)

    def test_practice_header_footer_uses_all_six_global_positions(self):
        args = exporter._hf_variable_args({
            "header_left": "{标题}",
            "header_center": "数学专题",
            "header_right": "练习",
            "footer_left": "姓名",
            "footer_center": "第 {页码} / {总页数} 页",
            "footer_right": "QuizForge",
        }, "双栏_刷题")
        joined = "\n".join(args)

        self.assertIn(r"hf_hl=双栏\_刷题", joined)
        self.assertIn("hf_hc=数学专题", joined)
        self.assertIn("hf_hr=练习", joined)
        self.assertIn("hf_fl=姓名", joined)
        self.assertIn(r"hf_fc=第 \thepage / \pageref{LastPage} 页", joined)
        self.assertIn("hf_fr=QuizForge", joined)
        self.assertIn("hf_rule=1", joined)

    def test_inline_solution_does_not_add_answer_space_after_solution(self):
        md = exporter.build_markdown(
            [self.questions[1]], "带解析", mode="practice",
            solution_mode="inline")

        self.assertIn("解析", md)
        self.assertNotIn("【解析】", md)
        self.assertNotIn(r"\vspace*{", md)

    def test_solution_body_strips_only_structural_leading_labels(self):
        cases = {
            "【解析】由题意可得": "由题意可得",
            "## 【解析】\n由题意可得": "由题意可得",
            "解析：由题意可得": "由题意可得",
            "解析:由题意可得": "由题意可得",
            "解析如下，所以成立": "解析如下，所以成立",
            "参考解析内容": "参考解析内容",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                rendered = exporter._solution_md(source)
                self.assertEqual(rendered, expected)

    def test_separate_solution_keeps_global_heading_without_field_label(self):
        question = {
            "id": "q", "body": "题干", "type": "解答题",
            "solution": "【解析】由题意可得",
        }

        md = exporter.build_markdown(
            [question], "分离解析", mode="list",
            solution_mode="separate")

        self.assertIn("参考解析", md)
        self.assertIn("由题意可得", md)
        self.assertNotIn("【解析】", md)

    def test_solution_image_split_wraps_text_below_image(self):
        source = "图片前保持整行\nQFIGSLOT0\n从这里开始环绕\n图片下恢复整行"
        files = ["answer.png"]
        layouts = [{"i": 0, "w": 35}]

        normal = exporter._solution_md(source, files, layouts, "off")
        split = exporter._solution_md(source, files, layouts, "full")

        self.assertIn(r"\qfigflexbox{0.35}{answer.png}{}", normal)
        self.assertNotIn(r"\begin{wrapfigure}", normal)
        self.assertIn(r"\begin{wrapfigure}{r}{0.3500\linewidth}", split)
        self.assertIn(r"\end{wrapfigure}", split)
        self.assertIn(r"\qwrapclear", split)
        self.assertNotIn(r"\begin{minipage}[t]{0.61\linewidth}", split)
        self.assertIn("answer.png", split)
        self.assertLess(split.index("图片前保持整行"),
                        split.index(r"\begin{wrapfigure}"))
        self.assertLess(split.index(r"\begin{wrapfigure}"),
                        split.index("从这里开始环绕"))
        self.assertLess(split.index("图片下恢复整行"),
                        split.index(r"\qwrapclear"))

    def test_solution_image_wrap_defaults_to_editor_width(self):
        split = exporter._solution_md(
            "解析文字\nQFIGSLOT0", ["answer.png"], None, "full")

        self.assertIn(r"\begin{wrapfigure}{r}{0.3500\linewidth}", split)

    def test_solution_image_wrap_honors_left_alignment(self):
        split = exporter._solution_md(
            "解析文字\nQFIGSLOT0", ["answer.png"],
            [{"i": 0, "w": 42, "align": "left"}], "full")

        self.assertIn(r"\begin{wrapfigure}{l}{0.4200\linewidth}", split)
        self.assertIn(r"\includegraphics[width=\linewidth", split)

    def test_inline_solution_wrap_stays_outside_question_number_list(self):
        markdown = exporter.build_markdown([{
            "id": "q", "body": "题干", "type": "填空题",
            "solution": "解析正文\nQFIGSLOT0",
            "sol_img_split": "full",
            "sol_img_layouts": [{"i": 0, "w": 35}],
            "_sol_img_files": ["answer.png"],
        }], "解析混排", mode="list", solution_mode="inline")

        # wrapfig 不能位于 qopen/qclose 的 list 内，否则 TeX 会把图强制漂到解析末尾。
        self.assertLess(markdown.index(r"\qclose"),
                        markdown.index(r"\begin{wrapfigure}"))
        self.assertLess(markdown.index("解析正文"),
                        markdown.index(r"\begin{wrapfigure}"))

    def test_practice_inline_solution_wrap_stays_outside_layout_wrappers(self):
        markdown = exporter._render_block({
            "kind": "question", "layout": "practice", "num": 1,
            "body": "题干", "type": "解答题", "difficulty": "2",
            "practice_solve": True, "solution": "解析正文\nQFIGSLOT0",
            "sol_img_split": "full",
            "sol_img_layouts": [{"i": 0, "w": 35}],
            exporter._SOL_IMG_FILES_KEY: ["answer.png"],
        }, solution_mode="inline")

        self.assertLess(markdown.index(r"\end{qpracticesolve}"),
                        markdown.index(r"\begin{wrapfigure}"))
        self.assertLess(markdown.index(r"\qclose"),
                        markdown.index(r"\begin{wrapfigure}"))

    def test_solution_item_wrap_raw_block_starts_on_own_line(self):
        markdown = exporter._render_block({
            "kind": "solution_item", "num": 7,
            "text": "解析正文\nQFIGSLOT0",
            "sol_img_split": "full",
            "sol_img_layouts": [{"i": 0, "w": 35}],
            exporter._SOL_IMG_FILES_KEY: ["answer.png"],
        })

        self.assertIn(
            "**7.**\n\n解析正文\n\n```{=latex}\n\\begin{wrapfigure}", markdown)
        self.assertNotIn("**7.** ```{=latex}", markdown)

        pandoc = Path(exporter.config.PANDOC)
        if not pandoc.is_file():
            self.skipTest("本地未携带 Pandoc")
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "solution.md"
            target = Path(temp_dir) / "solution.tex"
            source.write_text(markdown, encoding="utf-8", newline="\n")
            result = subprocess.run(
                [str(pandoc), str(source), "-o", str(target)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            latex = target.read_text(encoding="utf-8")
        # 不只检查 Markdown 字符串：实际 Pandoc 必须把 fenced block 透传为 LaTeX，
        # 而不是把围栏/反斜杠渲染成普通等宽文本。
        self.assertIn(r"\begin{wrapfigure}", latex)
        self.assertNotIn(r"\texttt{\textbackslash begin", latex)

    def test_unicode_roman_subquestion_markers_use_available_latin_glyphs(self):
        md = exporter.build_markdown(
            [{"id": "q", "body": "题干\n（ⅰ）第一步\n（ⅱ）第二步",
              "type": "解答题", "difficulty": "3", "solution": ""}],
            "罗马序号", mode="practice")

        self.assertIn(r"\qsubopen{（i）}", md)
        self.assertIn(r"\qsubitem{（ii）}", md)
        self.assertNotIn("ⅰ", md)
        self.assertNotIn("ⅱ", md)

    def test_template_has_compact_practice_geometry_and_column_rule(self):
        template = exporter.config.TEX_TEMPLATE.read_text(encoding="utf-8")
        practice_block = template.split("$if(practice)$", 1)[1].split(
            "$endif$", 1)[0]

        self.assertIn("$if(practice)$", template)
        self.assertIn(r"\IfFontExistsTF{Noto Sans SC}", practice_block)
        self.assertIn(r"\setCJKmainfont{Microsoft YaHei}", practice_block)
        self.assertIn(r"\usepackage{multicol}", template)
        self.assertIn(r"\begin{multicols*}{2}\raggedcolumns", template)
        self.assertIn(r"\setlength{\columnseprule}{0.35pt}", template)

    def test_each_solve_has_one_wrapper_and_only_followups_force_new_column(self):
        questions = [
            {"id": "b", "body": "填空题干", "type": "填空题",
             "difficulty": "1", "solution": ""},
            *[
                {"id": f"s{i}", "body": f"解答{i}", "type": "解答题",
                 "difficulty": str(i), "solution": ""}
                for i in range(1, 4)
            ],
        ]

        md = exporter.build_markdown(questions, "大题换栏", mode="practice")

        self.assertEqual(md.count(r"\begin{qpracticesolve}"), 3)
        self.assertEqual(md.count(r"\end{qpracticesolve}"), 3)
        self.assertEqual(md.count(r"\columnbreak"), 2)
        first_open = md.index(r"\begin{qpracticesolve}")
        first_close = md.index(r"\end{qpracticesolve}", first_open)
        heading = md.index("二、解答题")
        self.assertLess(first_open, heading)
        self.assertLess(heading, first_close)

    def test_practice_solve_template_measures_real_box_height(self):
        template = exporter.config.TEX_TEMPLATE.read_text(encoding="utf-8")
        solve_env = template.split(r"\newenvironment{qpracticesolve}", 1)[1]
        solve_env = solve_env.split(r"\makeatother", 1)[0]

        self.assertIn(r"\ht\qpracticesolvebox+\dp\qpracticesolvebox", solve_env)
        self.assertIn(r"\qpracticecolumnheight", solve_env)
        self.assertIn(r"\unvbox\qpracticesolvebox", solve_env)
        self.assertIn(
            r"\global\qpracticecolumnheight=\csname @colroom\endcsname",
            template,
        )

    def test_first_solve_has_no_forced_break_so_tex_can_measure_remaining_column(self):
        questions = [
            {"id": "fill", "body": "填空占位", "type": "填空题",
             "difficulty": "1", "solution": ""},
            {"id": "solve", "body": "第一道大题", "type": "解答题",
             "difficulty": "2", "solution": ""},
        ]

        md = exporter.build_markdown(questions, "余量分支", mode="practice")

        first_solve = md.index(r"\begin{qpracticesolve}")
        self.assertNotIn(r"\columnbreak", md[:first_solve])
        self.assertIn(r"\box\qpracticesolvebox",
                      exporter.config.TEX_TEMPLATE.read_text(encoding="utf-8"))

    def test_practice_choice_options_wrap_only_in_one_column(self):
        four_cols = exporter._choice_tasks(
            "短选项\nA. 1 B. 2 C. 3 D. 4", nowrap_multicol=True)
        two_cols = exporter._choice_tasks(
            "中等选项\nA. abcdefghijkl B. bcdefghijklm "
            "C. cdefghijklmn D. defghijklmno", nowrap_multicol=True)
        one_col = exporter._choice_tasks(
            "长选项\nA. abcdefghijklmnopqrstuvwxyz12345 "
            "B. bcdefghijklmnopqrstuvwxyz12345", nowrap_multicol=True)

        self.assertIn(r"\begin{tasks}(4)", four_cols)
        self.assertEqual(four_cols.count(r"\task \mbox{"), 4)
        self.assertIn(r"\begin{tasks}(2)", two_cols)
        self.assertEqual(two_cols.count(r"\task \mbox{"), 4)
        self.assertIn(r"\begin{tasks}(1)", one_col)
        self.assertNotIn(r"\task \mbox{", one_col)

        normal_exam = exporter._choice_tasks(
            "短选项\nA. 1 B. 2 C. 3 D. 4")
        self.assertNotIn(r"\task \mbox{", normal_exam)

    def test_practice_uses_narrower_column_thresholds(self):
        medium = ["A. abcdefghijkl", "B. bcdefghijklm"]
        self.assertEqual(exporter.choice_cols(medium), 2)
        self.assertEqual(exporter._practice_choice_cols(medium), 2)

        full_width_short = [r"A. $x\in(-2,0)$", r"B. $x\in(0,2)$"]
        self.assertEqual(exporter.choice_cols(full_width_short), 4)
        self.assertEqual(exporter._practice_choice_cols(full_width_short), 2)

    def test_practice_image_split_wraps_text_below_figure(self):
        cases = [
            ("单选题", "题干\nQFIGSLOT0\nA. 1 B. 2 C. 3 D. 4", "full"),
            ("填空题", "较长的填空题干\nQFIGSLOT0", "opts"),
            ("解答题", "题干\nQFIGSLOT0\n（1）第一问\n（2）第二问", "sub"),
        ]

        for qtype, body, split_mode in cases:
            with self.subTest(qtype=qtype):
                md = exporter._render_block({
                    "kind": "question", "layout": "practice", "num": 1,
                    "body": body, "type": qtype, "difficulty": "2",
                    "solution": "", "img_split": split_mode,
                    "img_layouts": [{"i": 0, "w": 70}],
                    exporter._IMG_FILES_KEY: ["figure.png"],
                })

                self.assertIn(r"\begin{wrapfigure}{r}{0.4600\linewidth}", md)
                self.assertIn(r"\qwrapneed{0.24\textheight}", md)
                self.assertIn(r"\includegraphics", md)
                self.assertIn("figure.png", md)
                self.assertIn(r"\end{wrapfigure}", md)
                self.assertIn("**1.** ", md)
                self.assertNotIn(r"\noindent\textbf{1.}", md)
                self.assertIn(r"\qwrapclear", md)
                self.assertNotIn(r"\qopen{1.}", md)
                # 常规图文分栏的左右 minipage 不应再嵌进双栏刷题；浮图内部仅保留
                # 一个占满自身宽度的图片盒，正文随后自然绕排并在图下恢复整栏宽。
                self.assertNotIn(r"\begin{minipage}[t]{0.26\linewidth}", md)
                self.assertNotIn(r"\begin{minipage}[t]{0.48\linewidth}", md)

    def test_non_practice_image_split_keeps_rigid_columns(self):
        md = exporter._q_md(
            1, "题干\nQFIGSLOT0\nA. 1 B. 2 C. 3 D. 4", "单选题",
            img_split="full", img_files=["figure.png"])

        self.assertNotIn(r"\begin{wrapfigure}", md)
        self.assertIn(r"\begin{minipage}[t]{0.48\linewidth}", md)

    def test_template_clears_short_wrap_before_next_question(self):
        template = exporter.config.TEX_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn(r"\newcommand{\qwrapclear}", template)
        self.assertIn(r"\newcommand{\qwrapneed}", template)
        self.assertIn(r"\c@WF@wrappedlines", template)
        self.assertIn(r"\multiply\@tempdima", template)
        self.assertIn(r"\WFclear", template)
        self.assertNotIn(r"\loop\ifnum\c@WF@wrappedlines", template)

    def test_non_split_tail_image_honors_left_center_right(self):
        expected = {
            "left": r"\noindent\qfigflexbox{0.35}{figure.png}{}\hfill",
            "center": r"\noindent\hfill\qfigflexbox{0.35}{figure.png}{}\hfill",
            "right": r"\noindent\hfill\qfigflexbox{0.35}{figure.png}{}\par",
        }

        for align, latex in expected.items():
            with self.subTest(align=align):
                md = exporter._q_md(
                    1, "题干\nQFIGSLOT0", "解答题", img_split="off",
                    img_layouts=[{"i": 0, "w": 35, "align": align}],
                    img_files=["figure.png"])
                self.assertIn(latex, md)

    def test_explicit_layout_collects_any_number_of_images(self):
        body = ("题干\nQFIGSLOT0\nQFIGSLOT1\nQFIGSLOT2\n"
                "A. 1 B. 2 C. 3 D. 4")
        row = exporter.plan_figs(body, "单选题", [], "opts")
        stack = exporter.plan_figs(
            body, "单选题", [{"i": 0, "stack": True}], "opts")

        self.assertEqual(row["groups"], [{"ids": [0, 1, 2], "row": True}])
        self.assertEqual(stack["groups"], [{"ids": [0, 1, 2], "row": False}])
        self.assertTrue(all(slot["pos"] == "tail" for slot in row["slots"]))

        md = exporter._q_md(
            1, body, "单选题", img_split="between",
            img_files=["a.png", "b.png", "c.png"])
        self.assertEqual(md.count(r"\qfigflexbox"), 3)
        self.assertLess(md.index("题干"), md.index("a.png"))
        self.assertLess(md.index("c.png"), md.index(r"\begin{tasks}"))

    def test_solve_between_and_full_split_match_choice_semantics(self):
        body = "公共题干\nQFIGSLOT0\n（1）第一问\n（2）第二问"
        between = exporter._q_md(
            1, body, "解答题", img_split="between",
            img_files=["solve.png"])
        full = exporter._q_md(
            1, body, "解答题", img_split="full",
            img_files=["solve.png"])

        self.assertLess(between.index("公共题干"), between.index("solve.png"))
        self.assertLess(between.index("solve.png"), between.index("（1）"))
        self.assertIn(r"\begin{minipage}[t]{0.48\linewidth}", full)
        self.assertIn("solve.png", full)


if __name__ == "__main__":
    unittest.main()
