"""自定义 TeX 模板契约与沙箱回归测试。"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import config
import exporter
import template_pipeline
import tex_sandbox


def _list_template() -> bytes:
    return r"""\documentclass{article}
\usepackage{graphicx}
\newcommand{\qopen}[1]{\par\textbf{#1}}
\newcommand{\qclose}{\par}
\newcommand{\qsubopen}[1]{\par #1}
\newcommand{\qsubitem}[1]{\par #1}
\newcommand{\qsubclose}{\par}
\newcommand{\qfig}[2]{\includegraphics{#1}}
\newcommand{\qfigwrap}[2]{\includegraphics{#1}}
\newcommand{\qwrapclear}{}
\newcommand{\qfigflexbox}[3]{\includegraphics{#2}}
\newcommand{\qpairitem}[4]{#2}
\begin{document}
$body$
\end{document}
""".encode("utf-8")


def _manifest(entrypoint: str = "main.tex", modes=None) -> bytes:
    return json.dumps({
        "schema": 1,
        "contract": "quizforge-pandoc-v1",
        "entrypoint": entrypoint,
        "supported_modes": modes or ["list"],
    }).encode("utf-8")


class TemplateContractTests(unittest.TestCase):
    def test_fixed_preview_png_has_valid_chunks_and_pixels(self):
        raw = template_pipeline._SAMPLE_PNG
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
        cursor = 8
        idat = bytearray()
        chunk_names = []
        while cursor < len(raw):
            length = struct.unpack(">I", raw[cursor:cursor + 4])[0]
            name = raw[cursor + 4:cursor + 8]
            data = raw[cursor + 8:cursor + 8 + length]
            checksum = struct.unpack(
                ">I", raw[cursor + 8 + length:cursor + 12 + length]
            )[0]
            self.assertEqual(zlib.crc32(name + data) & 0xFFFFFFFF, checksum)
            chunk_names.append(name)
            if name == b"IDAT":
                idat.extend(data)
            cursor += 12 + length
        self.assertEqual(cursor, len(raw))
        self.assertEqual(chunk_names, [b"IHDR", b"IDAT", b"IEND"])
        self.assertEqual(zlib.decompress(bytes(idat)), b"\x00\x00\x00\x00\xff")

    def test_fixed_preview_uses_staged_quizforge_image(self):
        questions = template_pipeline._sample_questions("sample.png")
        for question in questions:
            question["body"] = exporter._stash_tables(question["body"])
        markdown = exporter.build_markdown(
            questions, "固定样例", mode="note", solution_mode="inline"
        )
        self.assertIn(r"\includegraphics", markdown)
        self.assertIn("{sample.png}", markdown)
        self.assertIn(r"\begin{tabular}", markdown)
        self.assertNotIn("QFIGSLOT0", markdown)
        self.assertNotIn("|---|", markdown)
        self.assertNotIn("![固定样例图片]", markdown)

    def test_single_tex_gets_manifest_and_stable_hash(self):
        files = template_pipeline.single_tex_package("main.tex", _list_template())
        first = template_pipeline.inspect_files(files)
        second = template_pipeline.inspect_files(reversed(files))
        self.assertEqual(first["manifest"]["contract"], "quizforge-pandoc-v1")
        self.assertEqual(first["supported_modes"], ["list"])
        self.assertEqual(first["source_hash"], second["source_hash"])

    def test_manifest_rejects_unknown_mode_and_entrypoint_traversal(self):
        with self.assertRaises(template_pipeline.TemplatePipelineError) as caught:
            template_pipeline.inspect_files([
                ("main.tex", _list_template()),
                ("quizforge-template.json", _manifest(modes=["unknown"])),
            ])
        self.assertEqual(caught.exception.code, "invalid_supported_modes")
        with self.assertRaises(template_pipeline.TemplatePipelineError) as caught:
            template_pipeline.inspect_files([
                ("main.tex", _list_template()),
                ("quizforge-template.json", _manifest(entrypoint="../main.tex")),
            ])
        self.assertEqual(caught.exception.code, "invalid_entrypoint")

    def test_runtime_macro_contract_is_mode_specific(self):
        files = [
            ("main.tex", _list_template()),
            ("quizforge-template.json", _manifest(modes=["slides"])),
        ]
        with self.assertRaises(template_pipeline.TemplatePipelineError) as caught:
            template_pipeline.inspect_files(files)
        self.assertEqual(caught.exception.code, "missing_runtime_macros")
        self.assertIn(r"\qslidecover", str(caught.exception))

    def test_real_preview_attempts_every_declared_mode(self):
        raw = Path(config.TEX_TEMPLATE).read_text(encoding="utf-8")
        # 内置模板兼顾 fragment/整卷而有两个 body；外部 v1 契约要求唯一 body。
        raw = raw.replace("$body$", "% fragment body由验证样例覆盖", 1)
        modes = list(template_pipeline.SUPPORTED_MODES)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "main.tex").write_text(raw, encoding="utf-8")
            (root / "quizforge-template.json").write_bytes(
                _manifest(modes=modes))
            calls = []
            rendered_markdown = []

            def fake_pandoc(markdown, output_tex, template, **kwargs):
                calls.append((markdown.name, tuple(kwargs.get("variables") or ())))
                rendered_markdown.append(markdown.read_text(encoding="utf-8"))
                output_tex.write_text(
                    r"\documentclass{article}\begin{document}ok\end{document}",
                    encoding="utf-8")

            def fake_xelatex(tex_file, **_kwargs):
                pdf = tex_file.with_suffix(".pdf")
                pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
                return pdf

            with mock.patch.object(tex_sandbox, "pandoc_path", return_value="pandoc"), \
                    mock.patch.object(tex_sandbox, "xelatex_path", return_value="xelatex"), \
                    mock.patch.object(tex_sandbox, "run_pandoc", side_effect=fake_pandoc), \
                    mock.patch.object(tex_sandbox, "compile_xelatex",
                                      side_effect=fake_xelatex):
                result = template_pipeline.compile_preview(root)

        self.assertEqual(set(result["modes"]), set(modes))
        self.assertEqual(len(calls), len(modes))
        self.assertTrue(all("代入" in text for text in rendered_markdown))
        variables = dict(calls)
        self.assertIn("slides=1", variables[next(name for name in variables if "slides" in name)])
        self.assertIn("practice=1",
                      variables[next(name for name in variables if "practice" in name)])

    def test_exporter_rejects_undeclared_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            package = root / "template-id"
            package.mkdir()
            (package / "main.tex").write_bytes(_list_template())
            (package / "quizforge-template.json").write_bytes(_manifest())
            with mock.patch.object(config, "AGENT_TEMPLATES_DIR", root):
                with self.assertRaises(exporter.ExportError) as caught:
                    exporter._resolve_template_path(package / "main.tex", mode="slides")
        self.assertIn("不支持 slides", str(caught.exception))

    def test_exporter_rejects_unregistered_template_inside_source_tree(self):
        with tempfile.TemporaryDirectory(dir=config.BASE_DIR) as td:
            package = Path(td)
            (package / "main.tex").write_bytes(_list_template())
            (package / "quizforge-template.json").write_bytes(_manifest())
            with self.assertRaises(exporter.ExportError) as caught:
                exporter._resolve_template_path(package / "main.tex", mode="list")
        self.assertIn("模板目录", str(caught.exception))


class TexSandboxTests(unittest.TestCase):
    def test_static_validation_rejects_process_and_parent_input(self):
        with self.assertRaises(tex_sandbox.TexSandboxError) as caught:
            tex_sandbox.validate_tex_text(r"\immediate\write18{calc.exe}")
        self.assertEqual(caught.exception.code, "dangerous_tex")
        with self.assertRaises(tex_sandbox.TexSandboxError) as caught:
            tex_sandbox.validate_tex_text(
                r"\input{../secret}", package_files=["main.tex"])
        self.assertEqual(caught.exception.code, "unsafe_resource_path")

    def test_sandbox_environment_removes_external_tex_paths(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"TEXINPUTS": "C:/secret//"}, clear=False):
            env = tex_sandbox._sandbox_env(Path(td))
        self.assertNotIn("TEXINPUTS", env)
        self.assertEqual(env["openin_any"], "p")
        self.assertEqual(env["openout_any"], "p")
        self.assertEqual(env["MIKTEX_ENABLE_INSTALLER"], "0")

    def test_xelatex_command_has_hard_sandbox_flags(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            executable = root / "MiKTeX" / "xelatex.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"")
            tex = root / "document.tex"
            tex.write_text(
                r"\documentclass{article}\begin{document}ok\end{document}",
                encoding="utf-8")
            calls = []

            def fake_run(command, *, cwd, timeout, step):
                calls.append(command)
                (cwd / "document.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
                return ""

            with mock.patch.object(config, "XELATEX", str(executable)), \
                    mock.patch.object(tex_sandbox, "run", side_effect=fake_run):
                result = tex_sandbox.compile_xelatex(tex, passes=2)

        self.assertTrue(result.name.endswith(".pdf"))
        self.assertEqual(len(calls), 2)
        self.assertIn("--disable-installer", calls[0])
        self.assertIn("-no-shell-escape", calls[0])
        self.assertIn("-halt-on-error", calls[0])


if __name__ == "__main__":
    unittest.main()
