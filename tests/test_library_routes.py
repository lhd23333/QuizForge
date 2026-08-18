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
