import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import document_converter
import filestore


def _completed(command, returncode=0, stdout="", stderr=""):
    return document_converter.subprocess.CompletedProcess(
        command, returncode, stdout, stderr)


def _valid_pdf() -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class DocumentConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "课堂 讲义.docx"
        self.source.write_bytes(b"docx fixture")

    def tearDown(self):
        self.temp.cleanup()

    def _staging_leftovers(self):
        return list(self.root.glob(".quizforge-convert-*"))

    def test_docx_to_markdown_uses_gfm_math_and_extracts_media(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                "---\ncustom_field: 保留\n---\n\n# 正文\n\n$x^2$\n",
                encoding="utf-8",
            )
            media = Path(kwargs["cwd"]) / "课堂 讲义_assets" / "media"
            media.mkdir(parents=True)
            (media / "image1.png").write_bytes(b"image")
            return _completed(command)

        with (mock.patch.object(config, "PANDOC", "pandoc-test"),
              mock.patch.object(document_converter.subprocess, "run", fake_run)):
            result = document_converter.convert_docx_to_markdown(self.source)

        self.assertEqual(result, self.root / "课堂 讲义.md")
        meta, body = filestore._read_raw(result)
        self.assertEqual(meta["quizforge_kind"], "document")
        self.assertEqual(meta["custom_field"], "保留")
        self.assertIn("$x^2$", body)
        self.assertEqual(
            (self.root / "课堂 讲义_assets" / "media" / "image1.png").read_bytes(),
            b"image",
        )
        command, kwargs = calls[0]
        self.assertEqual(command[0], "pandoc-test")
        self.assertIn(str(self.source), command)
        self.assertEqual(command[command.index("--from") + 1], "docx")
        self.assertEqual(
            command[command.index("--to") + 1], "gfm+tex_math_dollars")
        self.assertIn("--wrap=none", command)
        self.assertEqual(
            command[command.index("--extract-media") + 1],
            "课堂 讲义_assets",
        )
        self.assertFalse(kwargs["shell"])
        self.assertEqual(self._staging_leftovers(), [])

    def test_markdown_target_or_media_collision_is_rejected_before_pandoc(self):
        for collision in ("target", "media"):
            with self.subTest(collision=collision):
                target = self.root / f"{collision}.md"
                media = self.root / f"{collision}_assets"
                occupied = target if collision == "target" else media
                if collision == "target":
                    occupied.write_text("existing", encoding="utf-8")
                else:
                    occupied.mkdir()
                with (mock.patch.object(document_converter.subprocess, "run") as run,
                      self.assertRaisesRegex(
                          document_converter.DocumentConversionError, "目标已存在")):
                    document_converter.convert_docx_to_markdown(
                        self.source, target)
                run.assert_not_called()

    def test_failed_markdown_conversion_leaves_no_partial_output(self):
        target = self.root / "失败.md"

        def fake_run(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_text(
                "partial", encoding="utf-8")
            (Path(kwargs["cwd"]) / "失败_assets").mkdir()
            return _completed(command, returncode=1, stderr="fixture failure")

        with (mock.patch.object(document_converter.subprocess, "run", fake_run),
              self.assertRaisesRegex(
                  document_converter.DocumentConversionError, "fixture failure")):
            document_converter.convert_docx_to_markdown(self.source, target)

        self.assertFalse(target.exists())
        self.assertFalse((self.root / "失败_assets").exists())
        self.assertEqual(self._staging_leftovers(), [])

    def test_legacy_doc_is_explicitly_rejected(self):
        source = self.root / "旧讲义.doc"
        source.write_bytes(b"legacy")
        with (mock.patch.object(document_converter.subprocess, "run") as run,
              self.assertRaisesRegex(
                  document_converter.DocumentConversionError, "另存为 .docx")):
            document_converter.convert_docx_to_markdown(source)
        run.assert_not_called()

    def test_windows_word_com_is_hidden_and_publishes_validated_pdf(self):
        observed = {}

        def fake_run(command, **kwargs):
            observed["command"] = command
            observed["kwargs"] = kwargs
            Path(kwargs["env"]["QUIZFORGE_PDF_TARGET"]).write_bytes(_valid_pdf())
            return _completed(command)

        with (mock.patch.object(document_converter, "_running_on_windows", return_value=True),
              mock.patch.object(document_converter.shutil, "which", return_value="powershell.exe"),
              mock.patch.object(document_converter.subprocess, "run", fake_run)):
            result = document_converter.convert_docx_to_pdf(self.source)

        self.assertEqual(result, self.root / "课堂 讲义.pdf")
        self.assertEqual(result.read_bytes(), _valid_pdf())
        script = observed["command"][observed["command"].index("-Command") + 1]
        self.assertIn("$word.Visible = $false", script)
        self.assertIn("$word.DisplayAlerts = 0", script)
        self.assertIn("finally", script)
        self.assertIn("$word.Quit(0)", script)
        self.assertFalse(observed["kwargs"]["shell"])
        self.assertEqual(self._staging_leftovers(), [])

    def test_word_unavailable_falls_back_only_when_both_tools_exist(self):
        commands = []

        def fake_pandoc(command, **_kwargs):
            commands.append(command)
            Path(command[command.index("--output") + 1]).write_bytes(_valid_pdf())
            return _completed(command)

        with (mock.patch.object(document_converter, "_running_on_windows", return_value=True),
              mock.patch.object(
                  document_converter, "_run_word_com",
                  side_effect=document_converter._WordUnavailable("no Word")),
              mock.patch.object(document_converter, "_tool_available", return_value=True),
              mock.patch.object(config, "PANDOC", "pandoc-test"),
              mock.patch.object(config, "XELATEX", "xelatex-test"),
              mock.patch.object(document_converter.subprocess, "run", fake_pandoc)):
            result = document_converter.convert_docx_to_pdf(
                self.source, self.root / "fallback.pdf")

        self.assertEqual(result.read_bytes(), _valid_pdf())
        self.assertEqual(commands[0][0], "pandoc-test")
        self.assertEqual(
            commands[0][commands[0].index("--pdf-engine") + 1],
            "xelatex-test",
        )

    def test_word_conversion_error_does_not_fall_back(self):
        with (mock.patch.object(document_converter, "_running_on_windows", return_value=True),
              mock.patch.object(
                  document_converter, "_run_word_com",
                  side_effect=document_converter.DocumentConversionError(
                      "Word 转换 PDF 失败：fixture")),
              mock.patch.object(document_converter, "_run_pdf_fallback") as fallback,
              self.assertRaisesRegex(
                  document_converter.DocumentConversionError, "fixture")):
            document_converter.convert_docx_to_pdf(self.source)
        fallback.assert_not_called()
        self.assertEqual(self._staging_leftovers(), [])

    def test_pdf_fallback_requires_pandoc_and_xelatex(self):
        def available(command):
            return command == "pandoc-test"

        with (mock.patch.object(document_converter, "_running_on_windows", return_value=False),
              mock.patch.object(config, "PANDOC", "pandoc-test"),
              mock.patch.object(config, "XELATEX", "missing-xelatex"),
              mock.patch.object(document_converter, "_tool_available", side_effect=available),
              mock.patch.object(document_converter.subprocess, "run") as run,
              self.assertRaisesRegex(
                  document_converter.DocumentConversionError, "XeLaTeX")):
            document_converter.convert_docx_to_pdf(self.source)
        run.assert_not_called()
        self.assertFalse((self.root / "课堂 讲义.pdf").exists())
        self.assertEqual(self._staging_leftovers(), [])

    def test_existing_pdf_is_rejected_before_any_converter_starts(self):
        target = self.root / "课堂 讲义.pdf"
        target.write_bytes(b"existing")
        with (mock.patch.object(document_converter, "_run_word_com") as word,
              mock.patch.object(document_converter, "_run_pdf_fallback") as fallback,
              self.assertRaisesRegex(
                  document_converter.DocumentConversionError, "目标已存在")):
            document_converter.convert_docx_to_pdf(self.source)
        word.assert_not_called()
        fallback.assert_not_called()
        self.assertEqual(target.read_bytes(), b"existing")

    def test_invalid_pdf_and_existing_target_leave_source_untouched(self):
        original = self.source.read_bytes()

        def fake_pandoc(command, **_kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"not pdf")
            return _completed(command)

        with (mock.patch.object(document_converter, "_running_on_windows", return_value=False),
              mock.patch.object(document_converter, "_tool_available", return_value=True),
              mock.patch.object(document_converter.subprocess, "run", fake_pandoc),
              self.assertRaisesRegex(
                  document_converter.DocumentConversionError, "PDF 文件无效")):
            document_converter.convert_docx_to_pdf(self.source)

        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse((self.root / "课堂 讲义.pdf").exists())
        self.assertEqual(self._staging_leftovers(), [])


if __name__ == "__main__":
    unittest.main()
