"""识别历史文件存储回归，不触碰真实用户目录。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import history_store


class HistoryStoreTests(unittest.TestCase):
    def test_source_markdown_and_trash_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4\narchive")
            history_root = root / "history"
            with mock.patch.object(config, "HISTORY_DIR", history_root):
                record = history_store.create_record(
                    "归档测试", [source], source_names=["原始试卷.pdf"],
                    metadata={"ocr_backend": "mineru"})
                source.unlink()
                archived = history_store.file_path(
                    record["id"], record["files"][0]["name"])
                self.assertEqual(archived.read_bytes(), b"%PDF-1.4\narchive")

                history_store.attach_markdown(
                    record["id"], "第一行\r\n第二行", title="识别完成")
                loaded = history_store.get(record["id"])
                self.assertTrue(loaded["has_markdown"])
                self.assertEqual(loaded["title"], "识别完成")
                self.assertEqual(
                    history_store.read_markdown(record["id"]),
                    "第一行\n第二行")
                result_path = history_store.file_path(record["id"], "result.md")
                first_mtime = result_path.stat().st_mtime_ns
                saved, second_mtime = history_store.write_markdown(
                    record["id"], "资料库修改", first_mtime)
                self.assertTrue(saved)
                self.assertEqual(
                    history_store.read_markdown(record["id"]), "资料库修改")
                stale, current_mtime = history_store.write_markdown(
                    record["id"], "不得覆盖", first_mtime)
                self.assertFalse(stale)
                self.assertEqual(current_mtime, second_mtime)

                history_store.move_to_trash(record["id"])
                self.assertEqual(history_store.list_records(), [])
                self.assertEqual(len(history_store.list_records(trashed=True)), 1)
                history_store.restore(record["id"])
                self.assertEqual(len(history_store.list_records()), 1)
                history_store.move_to_trash(record["id"])
                history_store.purge(record["id"])
                self.assertEqual(history_store.list_records(trashed=True), [])


if __name__ == "__main__":
    unittest.main()
