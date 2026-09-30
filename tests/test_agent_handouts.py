"""Agent 讲义工具（指定题目做讲义）回归测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_approvals
import agent_core
import agent_handouts
import agent_services
import agent_snapshots
import agent_tools
import config
import filestore


class HandoutToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.bank = base / "bank"
        (self.bank / "数学").mkdir(parents=True)
        self._patchers = [
            mock.patch.object(config, "BANK_DIR", self.bank),
            mock.patch.object(config, "HANDOUTS_DIR", self.bank / "_handouts"),
            mock.patch.object(config, "TRASH_DIR", self.bank / ".trash"),
            mock.patch.object(config, "AGENT_SNAPSHOTS_DIR", base / "snaps"),
        ]
        for patcher in self._patchers:
            patcher.start()
        filestore.invalidate_scan_cache()
        self.qids = filestore.create_questions_batch([
            {"body": "1. 计算 $1+1$。", "solution": "$2$", "type": "填空题",
             "difficulty": "易", "source": "练习", "number": 1},
            {"body": "2. 计算 $2\\times 3$。", "solution": "$6$", "type": "填空题",
             "difficulty": "中", "source": "练习", "number": 2},
            {"body": "3. 计算 $3+4$。", "solution": "$7$", "type": "填空题",
             "difficulty": "中", "source": "考试", "number": 3},
        ], "数学")
        self.runtime = agent_core.AgentRuntime(self.bank, None)
        self.approvals = agent_approvals.ApprovalStore()
        agent_services.configure(runtime=self.runtime, approvals=self.approvals)
        self.session = self.runtime.new_session(workdir=None, scope="bank")
        self.session["mode"] = "danger"

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        filestore.invalidate_scan_cache()
        self._tmp.cleanup()

    # ------------------------------------------------------------------ 工具注册

    def test_tools_registered_and_classified(self):
        names = {spec["name"] for spec in agent_tools.TOOLS}
        for name in ("list_handouts", "read_handout", "create_handout",
                     "add_handout_questions", "update_handout_meta",
                     "export_handout", "delete_handout"):
            self.assertIn(name, names)
        for name in ("create_handout", "add_handout_questions",
                     "update_handout_meta", "export_handout", "delete_handout"):
            self.assertTrue(agent_tools.is_write_tool(name), name)
            self.assertTrue(agent_handouts.is_handout_action(name), name)
        for name in ("list_handouts", "read_handout"):
            self.assertFalse(agent_tools.is_write_tool(name), name)

    # ------------------------------------------------------------------ 主流程

    def test_create_handout_from_ids(self):
        result = agent_tools.dispatch("create_handout", {
            "title": "第一章", "ids": [self.qids[0], self.qids[1]],
            "columns": 2, "solution_mode": "hidden",
            "header_footer": {"header_center": "第一章"},
        }, session=self.session)
        self.assertTrue(result["executed"])
        path = result["result"]["path"]
        self.assertEqual(result["result"]["count"], 2)
        self.assertTrue((self.bank / path).is_file())

        text = (self.bank / path).read_text(encoding="utf-8")
        self.assertIn("quizforge:question", text)
        self.assertIn("计算 $1+1$", text)

        read = agent_tools.dispatch("read_handout", {"path": path}, session=self.session)
        self.assertEqual(read["question_count"], 2)
        self.assertEqual(read["metadata"]["columns"], 2)
        self.assertEqual(read["metadata"]["solution_default"], "hidden")
        self.assertNotIn("question_blocks", read["metadata"])  # 不下发逐题快照
        self.assertEqual([q["source_id"] for q in read["questions"]],
                         [self.qids[0], self.qids[1]])
        self.assertEqual(read["questions"][0]["type"], "填空题")

    def test_create_handout_by_filter(self):
        result = agent_tools.dispatch("create_handout", {
            "title": "只取中档题", "difficulty": "中",
        }, session=self.session)
        self.assertEqual(result["result"]["count"], 2)

    def test_add_update_and_delete(self):
        created = agent_tools.dispatch("create_handout", {
            "title": "讲义A", "ids": [self.qids[0]]}, session=self.session)
        path = created["result"]["path"]

        added = agent_tools.dispatch("add_handout_questions", {
            "path": path, "ids": [self.qids[2]], "position": "end"}, session=self.session)
        self.assertEqual(added["result"]["added"], 1)

        updated = agent_tools.dispatch("update_handout_meta", {
            "path": path, "page_format": "slides", "solution_mode": "inline"},
            session=self.session)
        self.assertTrue(updated["executed"])

        read = agent_tools.dispatch("read_handout", {"path": path}, session=self.session)
        self.assertEqual(read["question_count"], 2)
        self.assertEqual(read["metadata"]["page_format"], "slides")
        self.assertEqual(read["metadata"]["solution_default"], "inline")

        listed = agent_tools.dispatch("list_handouts", {}, session=self.session)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["handouts"][0]["title"], "讲义A")

        deleted = agent_tools.dispatch("delete_handout", {"path": path},
                                       session=self.session)
        self.assertTrue(deleted["result"]["deleted"])
        self.assertFalse((self.bank / path).exists())

    def test_snapshot_and_rollback(self):
        created = agent_tools.dispatch("create_handout", {
            "title": "可回滚", "ids": [self.qids[0]]}, session=self.session)
        path = created["result"]["path"]
        before = (self.bank / path).read_text(encoding="utf-8")

        agent_tools.dispatch("add_handout_questions", {
            "path": path, "ids": [self.qids[1]]}, session=self.session)
        self.assertNotEqual(before, (self.bank / path).read_text(encoding="utf-8"))

        snapshots = agent_snapshots.list_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["action"], "add_handout_questions")

        agent_snapshots.rollback_snapshot(
            snapshots[0]["id"], session_id=str(self.session["id"]))
        self.assertEqual(before, (self.bank / path).read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 边界

    def test_read_only_blocks_handout_writes(self):
        readonly = dict(self.session)
        readonly["mode"] = "readonly"
        with self.assertRaises(agent_tools.ToolError):
            agent_tools.dispatch("create_handout",
                                 {"title": "x", "ids": [self.qids[0]]},
                                 session=readonly)

    def test_standard_mode_creates_approval(self):
        standard = self.runtime.new_session(workdir=None, scope="bank")
        result = agent_tools.dispatch(
            "create_handout", {"title": "待确认", "ids": [self.qids[0]]},
            session=standard, approval_store=self.approvals)
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["approval"]["action"], "create_handout")
        self.assertFalse((self.bank / "_handouts" / "待确认.md").exists())

        self.approvals.approve(standard, result["approval"]["id"])
        _row, executed, payload = agent_services.execute_approval(
            standard, result["approval"]["id"])
        self.assertTrue(executed)
        self.assertTrue((self.bank / payload["path"]).is_file())

    def test_rejects_unknown_question_and_out_of_scope(self):
        with self.assertRaises(agent_tools.ToolError):
            agent_tools.dispatch("create_handout", {"title": "x", "ids": ["nope"]},
                                 session=self.session)

        scoped = self.runtime.new_session(workdir=None, scope="bank")
        scoped["workdir_id"] = "数学"
        scoped["mode"] = "danger"
        outside = filestore.create_questions_batch([
            {"body": "根目录的题", "type": "填空题"}], "")
        # 根目录不在 workdir「数学」内，必须被边界拦住
        with self.assertRaises(agent_tools.ToolError):
            agent_tools.dispatch("create_handout", {"title": "x", "ids": outside},
                                 session=scoped)

    def test_requires_targets(self):
        with self.assertRaises(agent_tools.ToolError):
            agent_tools.dispatch("create_handout", {"title": "x"}, session=self.session)


if __name__ == "__main__":
    unittest.main()
