"""题库首页 Word 语义导出的回归测试。"""

import base64
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

import config
import word_exporter
import word_ooxml


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
        self.assertNotIn('1. [QF-Q-1]{custom-style="QuizForgeMarker"} :::',
                         plan.markdown)

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

    def test_handout_fullpage_question_keeps_manual_page_break_intent(self):
        plan = word_exporter.build_word_plan(
            sample_questions(),
            title="讲义",
            mode="handout",
            fullpage_ids=["solve-1"],
        )

        second_question = plan.markdown.index("QF-Q-2")
        self.assertNotEqual(
            plan.markdown.rfind("QF_PAGE_BREAK", 0, second_question), -1)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaisesRegex(word_exporter.ExportError, "不支持"):
            word_exporter.build_word_plan(
                sample_questions(), title="非法模式", mode="unknown")


class WordContentTests(unittest.TestCase):
    def test_math_and_html_table_remain_pandoc_semantics(self):
        text = (
            "已知 $x^2$。\n\n"
            "<table><tr><td>名称</td><td>值</td></tr>"
            "<tr><td>A</td><td>$1$</td></tr></table>"
        )

        rendered = word_exporter.normalize_word_markdown(text)

        self.assertIn("$x^2$", rendered)
        self.assertIn("| 名称 | 值 |", rendered)
        self.assertIn("| A | $1$ |", rendered)
        self.assertNotIn("<table", rendered)
        self.assertNotIn("\\begin{tabular}", rendered)

    def test_pipe_table_is_kept_as_an_editable_table(self):
        source = "| 项目 | 数值 |\n|---|---|\n| 甲 | $2$ |"

        rendered = word_exporter.normalize_word_markdown(source)

        self.assertEqual(rendered, source)

    def test_stage_image_uses_safe_relative_name_and_width(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            assets = root / "assets"
            work = root / "work"
            assets.mkdir()
            (assets / "diagram.png").write_bytes(b"valid-image-fixture")
            questions = sample_questions()[:1]
            questions[0]["body"] = "图示如下：\n\n![[diagram.png]]"
            questions[0]["img_layouts"] = [
                {"i": 0, "w": 42, "align": "center"},
            ]

            with mock.patch.object(config, "ASSETS_DIR", assets):
                staged, widths = word_exporter.stage_word_images(
                    questions, work, "word_test")

            self.assertIn("word_test_img_1.png", staged[0]["body"])
            self.assertNotIn(str(assets), staged[0]["body"])
            self.assertEqual(widths, (("word_test_img_1.png", 42),))
            self.assertEqual(
                (work / "word_test_img_1.png").read_bytes(),
                b"valid-image-fixture",
            )

    def test_missing_image_reports_question_and_resource(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            assets = root / "assets"
            assets.mkdir()
            questions = sample_questions()[:1]
            questions[0]["body"] = "![[missing.png]]"

            with mock.patch.object(config, "ASSETS_DIR", assets):
                with self.assertRaisesRegex(
                        word_exporter.ExportError, "第 1 题.*missing.png"):
                    word_exporter.stage_word_images(
                        questions, root / "work", "word_test")

    def test_image_path_cannot_escape_assets_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            assets = root / "assets"
            assets.mkdir()
            (root / "outside.png").write_bytes(b"outside")
            questions = sample_questions()[:1]
            questions[0]["body"] = "![[../outside.png]]"

            with mock.patch.object(config, "ASSETS_DIR", assets):
                with self.assertRaisesRegex(
                        word_exporter.ExportError, "图片路径无效"):
                    word_exporter.stage_word_images(
                        questions, root / "work", "word_test")


class WordPipelineTests(unittest.TestCase):
    def test_export_invokes_pandoc_with_reference_doc_and_argument_array(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            assets = root / "assets"
            assets.mkdir()
            commands = []

            def fake_pandoc(command, *, cwd):
                commands.append((command, cwd))
                target = Path(command[command.index("-o") + 1])
                shutil.copy2(config.WORD_REFERENCE_DOCX, target)

            with (mock.patch.object(config, "OUTPUT_DIR", output),
                  mock.patch.object(config, "ASSETS_DIR", assets),
                  mock.patch.object(word_exporter, "_run_pandoc", fake_pandoc)):
                result = word_exporter.export(
                    sample_questions(), title="含 空格", fmt="docx", mode="list")

            self.assertEqual(result.suffix, ".docx")
            self.assertTrue(result.is_file())
            self.assertEqual(len(commands), 1)
            command, cwd = commands[0]
            self.assertIsInstance(command, list)
            self.assertIn("--reference-doc", command)
            self.assertIn(str(config.WORD_REFERENCE_DOCX), command)
            self.assertEqual(cwd, result.parent)

    def test_pandoc_failure_removes_partial_work_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            assets = root / "assets"
            assets.mkdir()

            def failed_pandoc(command, *, cwd):
                Path(command[command.index("-o") + 1]).write_bytes(b"partial")
                raise word_exporter.ExportError("Pandoc 生成 Word 失败：测试错误")

            with (mock.patch.object(config, "OUTPUT_DIR", output),
                  mock.patch.object(config, "ASSETS_DIR", assets),
                  mock.patch.object(word_exporter, "_run_pandoc", failed_pandoc)):
                with self.assertRaisesRegex(
                        word_exporter.ExportError, "Pandoc 生成 Word 失败"):
                    word_exporter.export(
                        sample_questions(), title="失败测试", fmt="docx")

            self.assertFalse(any(output.glob("word_*")))

    def test_word_export_rejects_pdf_only_options(self):
        with self.assertRaisesRegex(word_exporter.ExportError, "底色"):
            word_exporter.export(
                sample_questions(), fmt="docx", paper_tone="cream")
        with self.assertRaisesRegex(word_exporter.ExportError, "WIMath"):
            word_exporter.export(
                sample_questions(), fmt="docx", wimath_logo=True)


class WordPandocIntegrationTests(unittest.TestCase):
    def test_real_docx_roundtrip_keeps_text_math_tables_and_images(self):
        if not word_exporter.pandoc_available():
            self.skipTest("本机未安装 Pandoc")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            assets = root / "assets"
            assets.mkdir()
            (assets / "diagram.png").write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                "AAAADUlEQVR42mNk+M/wHwAF/gL+Xf0YAAAAAElFTkSuQmCC"
            ))
            questions = sample_questions()
            questions[0]["body"] = (
                "二次函数满足 $x^2=1$。\n\n"
                "<table><tr><td>名称</td><td>值</td></tr>"
                "<tr><td>根</td><td>$1$</td></tr></table>\n\n"
                "![[diagram.png]]"
            )
            questions[0]["img_layouts"] = [
                {"i": 0, "w": 35, "align": "center"},
            ]

            with (mock.patch.object(config, "OUTPUT_DIR", output),
                  mock.patch.object(config, "ASSETS_DIR", assets)):
                result = word_exporter.export(
                    questions,
                    title="真实 Word 回归",
                    fmt="docx",
                    mode="exam_std",
                    solution_mode="separate",
                    header_footer={
                        "header_left": "{标题}",
                        "footer_center": "第 {页码} / {总页数} 页",
                    },
                    std_opts={"subject": "数学", "info_bar": True},
                )

            word_ooxml.validate_docx(result)
            with zipfile.ZipFile(result) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                media = [name for name in archive.namelist()
                         if name.startswith("word/media/")]
            self.assertIn("oMath", document_xml)
            self.assertIn("<w:tbl", document_xml)
            self.assertIn("drawing", document_xml)
            self.assertTrue(media)
            self.assertNotIn("custom-style", document_xml)

            readback = subprocess.run(
                [config.PANDOC, str(result), "-t", "markdown"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout
            self.assertIn("二次函数", readback)
            self.assertIn("答案与解析", readback)
            self.assertIn("x", readback)


if __name__ == "__main__":
    unittest.main()
