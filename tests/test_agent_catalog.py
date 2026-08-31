"""Agent Skill、模板与偏好目录的安全边界测试。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from flask import Flask

import agent_catalog
import config
import template_pipeline
import tex_sandbox


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            mock.patch.object(config, "AGENT_SKILLS_PATH", self.root / "skills.json"),
            mock.patch.object(config, "AGENT_TEMPLATES_PATH", self.root / "templates.json"),
            mock.patch.object(config, "AGENT_PREFERENCES_PATH", self.root / "preferences.json"),
            mock.patch.object(config, "AGENT_SKILLS_DIR", self.root / "skills"),
            mock.patch.object(config, "AGENT_TEMPLATES_DIR", self.root / "templates"),
            mock.patch.object(config, "BANK_DIR", self.root / "bank"),
        ]
        for patcher in self.patches:
            patcher.start()
        (self.root / "bank").mkdir()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    @staticmethod
    def _valid_tex(*, extra: str = "") -> bytes:
        return (r"""\documentclass{article}
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
$if(title)$ $title$ $endif$
$question$
""" + extra + r"""
$body$
\end{document}
""").encode("utf-8")

    def test_natural_skill_is_draft_until_explicit_confirmation(self):
        row = agent_catalog.generate_skill_draft("搜索题目并读取答案", name="检索题目")
        self.assertEqual(row["status"], "draft")
        self.assertIn("search_questions", row["tools"])
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.enable_skill(row["id"])
        self.assertEqual(caught.exception.code, "confirmation_required")
        self.assertEqual(agent_catalog.enable_skill(row["id"], confirm=True)["status"], "enabled")

    def test_skill_zip_rejects_executable_and_traversal(self):
        def archive(name, content):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as zf:
                zf.writestr("SKILL.md", "# Demo\n- read")
                zf.writestr(name, content)
            return stream.getvalue()

        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.import_skill_folder(archive("run.py", b"print(1)"))
        self.assertEqual(caught.exception.code, "executable_file")
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.import_skill_folder(archive("../outside.md", b"bad"))
        self.assertEqual(caught.exception.code, "path_traversal")

    def test_json_skill_manifest_is_supported_and_nested_command_is_rejected(self):
        row = agent_catalog.import_skill_folder([
            ("SKILL.json", json.dumps({
                "name": "JSON Skill", "description": "demo",
                "tools": ["search_questions"], "steps": ["搜索"],
            }).encode("utf-8")),
        ])
        self.assertEqual(row["name"], "JSON Skill")
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.import_skill_folder([
                ("SKILL.json", json.dumps({"steps": [{"command": "echo"}]}).encode("utf-8")),
            ])
        self.assertEqual(caught.exception.code, "executable_field")

    def test_tex_template_extracts_fields_and_requires_confirmation(self):
        tex = self._valid_tex()
        row = agent_catalog.register_template_upload(("exam.tex", tex))
        self.assertEqual(row["format"], "tex")
        self.assertIn("title", row["fields"])
        self.assertIn("question", row["fields"])
        self.assertNotIn("endif", row["fields"])
        with self.assertRaises(agent_catalog.CatalogError):
            agent_catalog.confirm_template(row["id"])
        compiled = {
            "status": "valid", "source_hash": row["source_hash"],
            "preview_mode": "list", "preview_pdf": b"%PDF-1.4\n%%EOF\n",
            "modes": {"list": {"status": "passed", "pdf_bytes": 15}},
        }
        with mock.patch.object(template_pipeline, "compile_preview",
                               return_value=compiled):
            previewed = agent_catalog.preview_template(row["id"])
        self.assertEqual(previewed["validation"]["status"], "valid")
        enabled = agent_catalog.confirm_template(row["id"], confirm=True)
        self.assertTrue(enabled["enabled"])
        self.assertTrue(agent_catalog.select_template(row["id"])["selected"])
        source = agent_catalog.template_source_path(row["id"])
        self.assertTrue(source.name.endswith(".tex"))
        with self.assertRaises(agent_catalog.CatalogError):
            agent_catalog.template_source_path(row["id"], relative="../outside.tex")

    def test_tex_zip_requires_root_manifest_and_declared_entrypoint(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("main.tex", self._valid_tex())
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.register_template_upload(("bad.tex.zip", stream.getvalue()))
        self.assertEqual(caught.exception.code, "missing_manifest")

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("main.tex", self._valid_tex())
            archive.writestr("quizforge-template.json", json.dumps({
                "schema": 1,
                "contract": "quizforge-pandoc-v1",
                "entrypoint": "main.tex",
                "supported_modes": ["list"],
            }))
        row = agent_catalog.register_template_upload(("ok.tex.zip", stream.getvalue()))
        self.assertEqual(row["entrypoint"], "main.tex")
        self.assertEqual(row["supported_modes"], ["list"])
        self.assertEqual(row["validation"]["status"], "pending")

    def test_plain_zip_is_rejected_even_with_a_valid_template_manifest(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("main.tex", self._valid_tex())
            archive.writestr("quizforge-template.json", json.dumps({
                "schema": 1,
                "contract": "quizforge-pandoc-v1",
                "entrypoint": "main.tex",
                "supported_modes": ["list"],
            }))
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.register_template_upload(("ordinary.zip", stream.getvalue()))
        self.assertEqual(caught.exception.code, "unsupported_file")

    def test_template_rejects_duplicate_body_and_dangerous_tex(self):
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.register_template_upload((
                "duplicate.tex", self._valid_tex(extra="$body$")))
        self.assertEqual(caught.exception.code, "invalid_body_placeholder")

        dangerous = self._valid_tex(extra=r"\immediate\write18{calc.exe}")
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.register_template_upload(("dangerous.tex", dangerous))
        self.assertEqual(caught.exception.code, "dangerous_tex")

    def test_pdf_is_reference_only_and_cannot_be_enabled(self):
        with mock.patch.object(agent_catalog, "_pdf_metadata",
                               return_value={"pages": 1, "page_sizes": []}):
            row = agent_catalog.register_template_upload(
                ("reference.pdf", b"%PDF-1.4\n%%EOF\n"))
        self.assertTrue(row["reference_only"])
        self.assertFalse(row["executable"])
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.confirm_template(row["id"], confirm=True)
        self.assertEqual(caught.exception.code, "reference_only")

    def test_missing_xelatex_keeps_template_pending(self):
        row = agent_catalog.register_template_upload(("exam.tex", self._valid_tex()))
        with mock.patch.object(template_pipeline, "compile_preview",
                               side_effect=tex_sandbox.TexToolUnavailable("xelatex")):
            result = agent_catalog.preview_template(row["id"])
        self.assertEqual(result["validation"]["status"], "pending")
        self.assertIn("不能启用", result["validation"]["message"])

    def test_source_hash_change_invalidates_enabled_template(self):
        row = agent_catalog.register_template_upload(("exam.tex", self._valid_tex()))
        compiled = {
            "status": "valid", "source_hash": row["source_hash"],
            "preview_mode": "list", "preview_pdf": b"%PDF-1.4\n%%EOF\n",
            "modes": {"list": {"status": "passed", "pdf_bytes": 15}},
        }
        with mock.patch.object(template_pipeline, "compile_preview",
                               return_value=compiled):
            agent_catalog.preview_template(row["id"])
        agent_catalog.confirm_template(row["id"], confirm=True)
        source = self.root / "templates" / row["id"] / "exam.tex"
        source.write_bytes(source.read_bytes() + b"\n% changed")
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.template_source_path(row["id"])
        self.assertEqual(caught.exception.code, "template_stale")
        self.assertEqual(agent_catalog.get_template(row["id"])["status"], "stale")

    def test_template_list_revokes_stale_enabled_and_default_template(self):
        row = agent_catalog.register_template_upload(("exam.tex", self._valid_tex()))
        compiled = {
            "status": "valid", "source_hash": row["source_hash"],
            "preview_mode": "list", "preview_pdf": b"%PDF-1.4\n%%EOF\n",
            "modes": {"list": {"status": "passed", "pdf_bytes": 15}},
        }
        with mock.patch.object(template_pipeline, "compile_preview",
                               return_value=compiled):
            agent_catalog.preview_template(row["id"])
        agent_catalog.confirm_template(row["id"], confirm=True)
        agent_catalog.select_template(row["id"])

        source = self.root / "templates" / row["id"] / "exam.tex"
        source.write_bytes(source.read_bytes() + b"\n% changed outside QuizForge")

        self.assertEqual(agent_catalog.list_templates(include_disabled=False), [])
        stale = agent_catalog.list_templates()[0]
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["validation"]["status"], "stale")
        self.assertFalse(stale["enabled"])
        self.assertFalse(stale["selected"])
        self.assertIsNone(agent_catalog.selected_template())
        with self.assertRaises(agent_catalog.CatalogError):
            agent_catalog.update_preferences({"template_id": row["id"]})
        stored = json.loads((self.root / "templates.json").read_text(encoding="utf-8"))
        self.assertIsNone(stored["active_id"])

    def test_corrupt_template_catalog_is_never_treated_as_empty_or_overwritten(self):
        catalog = self.root / "templates.json"
        catalog.write_text("{not-json", encoding="utf-8")

        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.list_templates()
        self.assertEqual(caught.exception.code, "catalog_corrupt")
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.register_template_metadata(name="new template")
        self.assertEqual(caught.exception.code, "catalog_corrupt")
        self.assertEqual(catalog.read_text(encoding="utf-8"), "{not-json")
        template_root = self.root / "templates"
        self.assertFalse(template_root.exists() and any(template_root.iterdir()))

    def test_legacy_catalog_migrates_without_deleting_source(self):
        template_id = "legacy_template_01"
        directory = self.root / "templates" / template_id
        directory.mkdir(parents=True)
        source = directory / "legacy.tex"
        source.write_bytes(self._valid_tex())
        (self.root / "templates.json").write_text(json.dumps({
            "version": 1,
            "active_id": template_id,
            "templates": [{
                "id": template_id, "name": "旧模板", "format": "tex",
                "source_file": "legacy.tex", "status": "enabled",
                "enabled": True, "selected": True,
            }],
        }), encoding="utf-8")
        row = agent_catalog.list_templates()[0]
        self.assertEqual(row["schema_version"], 2)
        self.assertEqual(row["validation"]["status"], "pending")
        self.assertFalse(row["enabled"])
        self.assertTrue(source.is_file())
        stored = json.loads((self.root / "templates.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 2)
        self.assertIsNone(stored["active_id"])

    def test_preferences_reject_secret_and_out_of_scope_workdir(self):
        self.assertEqual(agent_catalog.update_preferences({"default_workdir": "unit"})["default_workdir"], "unit")
        with self.assertRaises(agent_catalog.CatalogError):
            agent_catalog.update_preferences({"api_key": "should-not-persist"})
        with self.assertRaises(agent_catalog.CatalogError) as caught:
            agent_catalog.update_preferences({"default_workdir": "../outside"})
        self.assertEqual(caught.exception.code, "path_traversal")
        raw = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
        self.assertNotIn("api_key", raw["preferences"])

    def test_blueprint_skill_and_preference_routes(self):
        app = Flask(__name__)
        app.register_blueprint(agent_catalog.bp)
        client = app.test_client()
        response = client.post("/api/agent/skills", json={"description": "搜索题目"})
        self.assertEqual(response.status_code, 201)
        skill_id = response.get_json()["skill"]["id"]
        response = client.get("/api/agent/skills")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 1)
        response = client.patch(f"/api/agent/skills/{skill_id}", json={"name": "新名称"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["skill"]["name"], "新名称")
        response = client.patch("/api/agent/preferences", json={"ocr_backend": "doc2x"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["preferences"]["ocr_backend"], "doc2x")

        response = client.post(
            "/api/templates",
            data={"file": (io.BytesIO(self._valid_tex()), "api.tex")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        template_id = response.get_json()["template"]["id"]
        response = client.get(f"/api/templates/{template_id}/preview")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
