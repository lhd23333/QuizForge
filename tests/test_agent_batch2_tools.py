"""Phase 2C：批次 2 工具（导入域 + 模板域）回归测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_approvals
import agent_catalog
import agent_core
import agent_services
import agent_tools
import config
import filestore


class _BankCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.base = base
        self.bank = base / "bank"
        (self.bank / "数学").mkdir(parents=True)
        self._patchers = [
            mock.patch.object(config, "BANK_DIR", self.bank),
            mock.patch.object(config, "AGENT_SNAPSHOTS_DIR", base / "snaps"),
            mock.patch.object(config, "AGENT_TEMPLATES_PATH", base / "templates.json"),
            mock.patch.object(config, "AGENT_TEMPLATES_DIR", base / "templates"),
        ]
        for patcher in self._patchers:
            patcher.start()
        filestore.invalidate_scan_cache()

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        filestore.invalidate_scan_cache()
        self._tmp.cleanup()

    @property
    def session(self):
        return {"id": "sess-batch2", "scope": "bank", "workdir_id": "",
                "mode": "standard"}


class ReadToolTests(_BankCase):
    def test_diagnose_markdown_reports_gap_and_missing_image(self):
        text = ("1. 第一题\n\n"
                "2. 第二题\n\n"
                "4. 第四题 ![[missing.png]]")
        result = agent_tools.dispatch(
            "diagnose_markdown", {"text": text}, session=self.session)
        self.assertGreaterEqual(result["number_count"], 3)
        self.assertIn(3, result["number_gaps"])
        self.assertEqual(result["image_refs"]["total"], 1)
        self.assertEqual(result["image_refs"]["missing"], ["missing.png"])
        self.assertEqual(result["block_count"], result["number_count"])

    def test_diagnose_markdown_rejects_empty(self):
        with self.assertRaises(agent_tools.ToolError):
            agent_tools.dispatch("diagnose_markdown", {"text": "   "},
                                 session=self.session)

    def test_split_preview_modes(self):
        text = "1. 甲\n\n2. 乙\n\n第3题 丙"
        auto = agent_tools.dispatch(
            "split_preview", {"text": text, "boundary_mode": "auto"},
            session=self.session)
        whitelist = agent_tools.dispatch(
            "split_preview", {"text": text, "boundary_mode": "whitelist"},
            session=self.session)
        self.assertEqual(auto["boundary_mode"], "auto")
        self.assertEqual(whitelist["boundary_mode"], "whitelist")
        # 白名单模式自带 `第x题` 边界，至少能切出 3 块
        self.assertGreaterEqual(whitelist["total"], 3)
        self.assertEqual(auto["shown"], len(auto["questions"]))

    def test_list_templates_empty_catalog(self):
        result = agent_tools.dispatch("list_templates", {}, session=self.session)
        self.assertEqual(result, {"total": 0, "templates": []})

    def test_validate_template_rejects_invalid_source(self):
        result = agent_tools.dispatch(
            "validate_template", {"source": "\\documentclass{article}"},
            session=self.session)
        self.assertFalse(result["valid"])
        self.assertIn("error", result)

    def test_validate_template_requires_input(self):
        with self.assertRaises(agent_tools.ToolError):
            agent_tools.dispatch("validate_template", {}, session=self.session)


class ServiceWriteTests(_BankCase):
    def setUp(self):
        super().setUp()
        self.runtime = agent_core.AgentRuntime(self.bank, None)
        self.approvals = agent_approvals.ApprovalStore()
        agent_services.configure(runtime=self.runtime, approvals=self.approvals)
        self.session_row = self.runtime.new_session(workdir=None, scope="bank")

    def _approve_and_execute(self, result):
        self.assertTrue(result["pending_confirmation"])
        approval_id = result["approval"]["id"]
        self.approvals.approve(self.session_row, approval_id)
        _row, executed, _payload = agent_services.execute_approval(
            self.session_row, approval_id)
        self.assertTrue(executed)

    def test_apply_review_fixes_requires_approval(self):
        job_id = "jobfix0001"
        agent_services._jobs[job_id] = {
            "status": "done",
            "md": ("1. 题目一\n\n## 解析\n答案一\n\n"
                   "2. 题目二\n\n## 解析\n答案二"),
            "filename": "t.md", "include_solution": True,
            "agent_session_id": self.session_row["id"], "agent_workdir_id": "",
            "only_numbers": None, "boundary_mode": "", "agent_imported_ids": [],
        }
        try:
            result = agent_tools.dispatch(
                "apply_review_fixes",
                {"job_id": job_id,
                 "fixes": [{"index": 0, "body": "改后的题目一"}]},
                session=self.session_row, approval_store=self.approvals)
            self._approve_and_execute(result)
            stored = agent_services._jobs[job_id]["md"]
            self.assertIn("改后的题目一", stored)
            self.assertNotIn("题目一\n\n## 解析\n答案一", stored)
        finally:
            agent_services._jobs.pop(job_id, None)

    def test_apply_review_fixes_rejects_imported_job(self):
        job_id = "jobfix0002"
        agent_services._jobs[job_id] = {
            "status": "done", "md": "1. 题目", "filename": "t.md",
            "include_solution": True,
            "agent_session_id": self.session_row["id"], "agent_workdir_id": "",
            "only_numbers": None, "boundary_mode": "", "agent_imported_ids": ["q1"],
        }
        try:
            with self.assertRaises(agent_tools.ToolError):
                agent_tools.dispatch(
                    "apply_review_fixes",
                    {"job_id": job_id, "fixes": [{"index": 0, "body": "x"}]},
                    session=self.session_row, approval_store=self.approvals)
        finally:
            agent_services._jobs.pop(job_id, None)

    def test_enable_template_requires_approval(self):
        with mock.patch.object(
                agent_catalog, "get_template",
                return_value={"id": "t1", "name": "样例模板"}), \
             mock.patch.object(
                agent_catalog, "confirm_template",
                return_value={"id": "t1", "enabled": True}) as confirm:
            result = agent_tools.dispatch(
                "enable_template", {"template_id": "t1"},
                session=self.session_row, approval_store=self.approvals)
            self.assertTrue(result["pending_confirmation"])
            confirm.assert_not_called()
            self._approve_and_execute(result)
            confirm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
