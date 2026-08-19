"""资料库文件索引与写接口的定向回归。"""

from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config


_tmp = None
_old_config = {}
app_module = None


def setUpModule():
    global _tmp, _old_config, app_module
    _tmp = tempfile.TemporaryDirectory()
    root = Path(_tmp.name)
    paths = {
        "BANK_DIR": root / "bank",
        "TRASH_DIR": root / "bank" / ".trash",
        "ASSETS_DIR": root / "bank" / "_assets",
        "HANDOUTS_DIR": root / "bank" / "_handouts",
        "IMAGES_DIR": root / "bank" / "_assets",
        "PROVIDERS_PATH": root / "providers.json",
        "SELECTIONS_PATH": root / "selections.json",
        "TASKS_PATH": root / "tasks.json",
        "UPLOAD_DIR": root / "uploads",
        "BATCH_UPLOAD_DIR": root / "uploads" / "batch",
        "HISTORY_DIR": root / "history",
        "OUTPUT_DIR": root / "output",
    }
    _old_config = {name: getattr(config, name) for name in paths}
    for name, value in paths.items():
        setattr(config, name, value)
    config.BANK_DIR.mkdir(parents=True)
    app_module = importlib.import_module("app")
    app_module.filestore.invalidate_scan_cache(folder_structure=True)


def tearDownModule():
    for name, value in _old_config.items():
        setattr(config, name, value)
    app_module.filestore.invalidate_scan_cache(folder_structure=True)
    _tmp.cleanup()


class LibraryRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.headers = {
            "X-CSRF-Token": app_module._WRITE_TOKEN,
            "Content-Type": "application/json",
        }

    def test_lists_doc_and_docx_as_word(self):
        folder = config.BANK_DIR / "Word 索引"
        folder.mkdir()
        (folder / "旧讲义.doc").write_bytes(b"doc")
        (folder / "新讲义.DOCX").write_bytes(b"docx")
        (folder / "忽略.odt").write_bytes(b"odt")

        response = self.client.get(
            "/api/library/children", query_string={"path": "Word 索引"})

        self.assertEqual(response.status_code, 200)
        entries = {item["name"]: item["kind"]
                   for item in response.get_json()["entries"]}
        self.assertEqual(entries, {"旧讲义.doc": "word", "新讲义.DOCX": "word"})

    def test_junction_is_hidden_and_direct_path_is_rejected(self):
        junction = config.BANK_DIR / "模拟联接"
        junction.mkdir()
        (junction / "外部.pdf").write_bytes(b"pdf")
        original_check = app_module.library_ops.is_link_or_junction

        def fake_check(path: Path) -> bool:
            return path == junction or original_check(path)

        with mock.patch.object(
                app_module.library_ops, "is_link_or_junction",
                side_effect=fake_check):
            root_response = self.client.get("/api/library/children")
            direct_response = self.client.get(
                "/api/library/children", query_string={"path": "模拟联接"})

        self.assertEqual(root_response.status_code, 200)
        self.assertNotIn(
            "模拟联接",
            {item["name"] for item in root_response.get_json()["entries"]},
        )
        self.assertEqual(direct_response.status_code, 404)

    def test_create_folder_and_rename_file(self):
        created = self.client.post(
            "/api/library/folder",
            json={"parent": "", "name": "课程资料"},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json()["entry"]["path"], "课程资料")
        (config.BANK_DIR / "课程资料" / "原讲义.docx").write_bytes(b"docx")

        renamed = self.client.post(
            "/api/library/rename",
            json={"path": "课程资料/原讲义.docx", "name": "高数讲义"},
            headers=self.headers,
        )

        self.assertEqual(renamed.status_code, 200)
        data = renamed.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["entry"]["old_path"], "课程资料/原讲义.docx")
        self.assertEqual(data["entry"]["path"], "课程资料/高数讲义.docx")
        self.assertEqual(data["entry"]["kind"], "word")
        self.assertEqual(
            (config.BANK_DIR / "课程资料" / "高数讲义.docx").read_bytes(),
            b"docx",
        )

    def test_create_markdown_accepts_optional_text(self):
        parent = config.BANK_DIR / "Markdown 新建"
        parent.mkdir()
        created = self.client.post(
            "/api/library/markdown",
            json={"parent": "Markdown 新建", "name": "课堂笔记",
                  "text": "# 第一节\r\n\r\n正文"},
            headers=self.headers,
        )
        empty = self.client.post(
            "/api/library/markdown",
            json={"parent": "Markdown 新建", "name": "空白笔记.md"},
            headers=self.headers,
        )

        self.assertEqual(created.status_code, 201)
        created_entry = created.get_json()["entry"]
        self.assertEqual(created_entry["path"], "Markdown 新建/课堂笔记.md")
        self.assertEqual(created_entry["kind"], "markdown")
        self.assertEqual(empty.status_code, 201)
        self.assertEqual(empty.get_json()["entry"]["path"],
                         "Markdown 新建/空白笔记.md")
        created_text = (parent / "课堂笔记.md").read_text(encoding="utf-8")
        empty_text = (parent / "空白笔记.md").read_text(encoding="utf-8")
        self.assertIn("quizforge_kind: document", created_text)
        self.assertIn("# 第一节\n\n正文", created_text)
        self.assertIn("quizforge_kind: document", empty_text)

    def test_create_markdown_rejects_history_and_reserved_roots(self):
        for reserved in ("_assets", "_handouts", "_backups"):
            (config.BANK_DIR / reserved).mkdir(exist_ok=True)
        for parent in (".quizforge-history", "_assets", "_handouts", "_backups"):
            with self.subTest(parent=parent):
                response = self.client.post(
                    "/api/library/markdown",
                    json={"parent": parent, "name": "不得创建"},
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(response.get_json()["code"],
                              {"read_only", "reserved_path"})
                self.assertFalse((config.BANK_DIR / parent / "不得创建.md").exists())

    def test_markdown_creation_ui_contract(self):
        page = self.client.get("/library").get_data(as_text=True)
        script = (config.BASE_DIR / "static" / "js" / "library-tabs.js").read_text(
            encoding="utf-8")

        self.assertIn('id="library-new-markdown"', page)
        self.assertIn('aria-label="新建顶层 Markdown 文档"', page)
        self.assertIn("function createLibraryMarkdown(parent = '')", script)
        self.assertIn("fetchJson('/api/library/markdown'", script)
        self.assertIn("['new-markdown', '新建 Markdown']", script)
        self.assertIn("if (host) await loadChildren(host, parent, 0)", script)
        self.assertIn("openDocument({...data.entry", script)
        self.assertIn(
            "newMarkdownButton?.addEventListener('click', () => createLibraryMarkdown(''))",
            script,
        )

    def test_transfer_moves_and_copies_entries(self):
        source = config.BANK_DIR / "转移来源"
        target = config.BANK_DIR / "转移目标"
        source.mkdir()
        target.mkdir()
        (source / "讲义.pdf").write_bytes(b"pdf")
        (source / "配图.png").write_bytes(b"png")

        copied = self.client.post(
            "/api/library/transfer",
            json={"path": "转移来源/讲义.pdf", "target": "转移目标",
                  "mode": "copy"},
            headers=self.headers,
        )
        moved = self.client.post(
            "/api/library/transfer",
            json={"path": "转移来源/配图.png", "target": "转移目标",
                  "mode": "move"},
            headers=self.headers,
        )

        self.assertEqual(copied.status_code, 200)
        self.assertEqual(copied.get_json()["entry"], {
            "path": "转移目标/讲义.pdf",
            "old_path": "转移来源/讲义.pdf",
            "kind": "pdf",
            "copied": True,
        })
        self.assertEqual(moved.status_code, 200)
        self.assertFalse((source / "配图.png").exists())
        self.assertEqual((target / "配图.png").read_bytes(), b"png")

    def test_errors_are_structured_and_history_never_reaches_disk_ops(self):
        self.client.post(
            "/api/library/folder",
            json={"parent": "", "name": "已有"},
            headers=self.headers,
        )
        conflict = self.client.post(
            "/api/library/folder",
            json={"parent": "", "name": "已有"},
            headers=self.headers,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["code"], "conflict")

        with mock.patch.object(app_module.library_ops, "create_folder") as operation:
            rejected = self.client.post(
                "/api/library/folder",
                json={"parent": ".quizforge-history", "name": "不得创建"},
                headers=self.headers,
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["code"], "read_only")
        operation.assert_not_called()

        invalid_mode = self.client.post(
            "/api/library/transfer",
            json={"path": "已有", "target": "", "mode": "link"},
            headers=self.headers,
        )
        self.assertEqual(invalid_mode.status_code, 400)
        self.assertEqual(invalid_mode.get_json()["code"], "invalid_mode")

        with mock.patch.object(app_module.library_ops, "transfer_entry") as operation:
            rejected = self.client.post(
                "/api/library/transfer",
                json={"path": ".quizforge-history/record/result.md",
                      "target": "", "mode": "copy"},
                headers=self.headers,
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["code"], "read_only")
        operation.assert_not_called()

        with mock.patch.object(app_module.library_ops, "transfer_entry") as operation:
            rejected = self.client.post(
                "/api/library/transfer",
                json={"path": "已有", "target": ".quizforge-history",
                      "mode": "move"},
                headers=self.headers,
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["code"], "read_only")
        operation.assert_not_called()

        with mock.patch.object(app_module.library_ops, "rename_entry") as operation:
            rejected = self.client.post(
                "/api/library/rename",
                json={"path": ".quizforge-history/record/result.md", "name": "新名称"},
                headers=self.headers,
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["code"], "read_only")
        operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
