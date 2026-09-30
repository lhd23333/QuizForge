"""Agent 批量工具（筛选 / 复制 / 批量改属性）回归测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_actions
import agent_snapshots
import agent_tools
import config
import filestore


class BulkToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.bank = base / "bank"
        (self.bank / "数学").mkdir(parents=True)
        self.snaps = base / "snaps"
        self._patchers = [
            mock.patch.object(config, "BANK_DIR", self.bank),
            mock.patch.object(config, "AGENT_SNAPSHOTS_DIR", self.snaps),
        ]
        for patcher in self._patchers:
            patcher.start()
        filestore.invalidate_scan_cache()
        self.qids = filestore.create_questions_batch([
            {"body": "1+1=?", "type": "填空", "difficulty": "易",
             "source": "练习", "tags": ["算术"]},
            {"body": "2+2=?", "type": "选择", "difficulty": "中",
             "source": "考试", "tags": ["算术", "基础"]},
        ], "数学")

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        filestore.invalidate_scan_cache()
        self._tmp.cleanup()

    def test_bulk_update_fields_and_snapshot(self):
        session = {"scope": "bank", "workdir_id": "", "id": "s1"}
        result = agent_actions.execute_action(
            "bulk_update_questions",
            {"ids": list(self.qids), "difficulty": "难", "source": "改后"},
            session=session)
        self.assertEqual(result["count"], 2)
        row = filestore.get_question(self.qids[0])
        self.assertEqual(row["difficulty"], "难")
        self.assertEqual(row["source"], "改后")
        snapshots = agent_snapshots.list_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["action"], "bulk_update_questions")

    def test_copy_questions_creates_new_ids(self):
        result = agent_actions.execute_action(
            "copy_questions",
            {"ids": [self.qids[0]], "folder": "数学"},
            session={"scope": "bank", "workdir_id": "", "id": "s1"})
        self.assertEqual(result["count"], 1)
        new_id = result["created_ids"][0]
        self.assertNotIn(new_id, self.qids)
        original = filestore.get_question(self.qids[0])
        copied = filestore.get_question(new_id)
        self.assertEqual(copied["body"], original["body"])

    def test_filter_questions_combines_conditions(self):
        result = agent_tools.dispatch(
            "filter_questions",
            {"difficulty": "中", "tags": ["基础"], "tag_match": "and"},
            session={"scope": "bank", "workdir_id": ""})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["questions"][0]["id"], self.qids[1])

    def test_filter_questions_source_keyword(self):
        result = agent_tools.dispatch(
            "filter_questions", {"source": "练习"},
            session={"scope": "bank", "workdir_id": ""})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["questions"][0]["id"], self.qids[0])


if __name__ == "__main__":
    unittest.main()
