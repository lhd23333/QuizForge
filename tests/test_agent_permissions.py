"""权限档位（read-only / standard / full）在工具层的强制边界。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_approvals
import agent_services
import agent_tools
import config
import filestore


class PermissionGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.bank = base / "bank"
        (self.bank / "数学").mkdir(parents=True)
        self._patchers = [
            mock.patch.object(config, "BANK_DIR", self.bank),
            mock.patch.object(config, "AGENT_SNAPSHOTS_DIR", base / "snaps"),
        ]
        for patcher in self._patchers:
            patcher.start()
        filestore.invalidate_scan_cache()
        self.qids = filestore.create_questions_batch([
            {"body": "1+1=?", "type": "填空", "difficulty": "易", "tags": []},
        ], "数学")
        self.approvals = agent_approvals.ApprovalStore()
        agent_services.configure(
            runtime=mock.Mock(root=self.bank), approvals=self.approvals)

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        filestore.invalidate_scan_cache()
        self._tmp.cleanup()

    def _session(self, mode: str) -> dict:
        return {"id": "s1", "scope": "bank", "workdir_id": "", "mode": mode}

    def test_permission_mode_normalises_unknown_to_standard(self):
        self.assertEqual(agent_tools.permission_mode(None), "standard")
        self.assertEqual(agent_tools.permission_mode({"mode": "??"}), "standard")
        self.assertEqual(agent_tools.permission_mode({"mode": "danger"}), "danger")
        self.assertEqual(agent_tools.permission_mode({"mode": "readonly"}), "readonly")

    def test_is_write_tool_mapping(self):
        for name in ("create_question", "update_question", "delete_questions",
                     "rename_question", "move_questions", "tag_questions",
                     "bulk_update_questions", "copy_questions",
                     "rollback_snapshot", "upsert_api_config",
                     "export_questions", "import_conversion",
                     "apply_review_fixes", "enable_template",
                     "preview_template", "execute_command"):
            self.assertTrue(agent_tools.is_write_tool(name), name)
        for name in ("read_question", "filter_questions", "list_folders",
                     "search_questions", "diagnose_markdown", "split_preview",
                     "list_templates", "validate_template",
                     "inspect_conversion", "list_snapshots"):
            self.assertFalse(agent_tools.is_write_tool(name), name)

    def test_read_only_rejects_every_write_path(self):
        session = self._session("readonly")
        cases = [
            ("update_question", {"id": self.qids[0], "body": "改"}),
            ("create_question", {"body": "新题"}),
            ("delete_questions", {"ids": [self.qids[0]]}),
            ("export_questions", {"ids": [self.qids[0]]}),
            ("upsert_api_config", {"name": "magpie"}),
            ("preview_template", {"template_id": "t1"}),
        ]
        for name, args in cases:
            with self.assertRaises(agent_tools.ToolError, msg=name):
                agent_tools.dispatch(name, args, session=session)

    def test_read_only_allows_read_tools(self):
        session = self._session("readonly")
        filtered = agent_tools.dispatch("filter_questions", {}, session=session)
        self.assertIn("total", filtered)
        folders = agent_tools.dispatch("list_folders", {}, session=session)
        self.assertIn("folders", folders)
        preview = agent_tools.dispatch(
            "split_preview", {"text": "1. 甲\n\n2. 乙"}, session=session)
        self.assertEqual(preview["total"], 2)

    def test_standard_mode_still_creates_approval(self):
        session = self._session("standard")
        result = agent_tools.dispatch(
            "update_question", {"id": self.qids[0], "body": "改"},
            session=session, approval_store=self.approvals)
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["approval"]["action"], "update_question")

    def test_full_permission_executes_without_approval(self):
        session = self._session("danger")
        result = agent_tools.dispatch(
            "update_question", {"id": self.qids[0], "body": "直接改"},
            session=session, approval_store=self.approvals)
        self.assertTrue(result["executed"])
        self.assertEqual(filestore.get_question(self.qids[0])["body"], "直接改")

    def test_execute_approval_refuses_read_only_session(self):
        with self.assertRaises(agent_tools.ToolError):
            agent_services.execute_approval(self._session("readonly"), "whatever")


if __name__ == "__main__":
    unittest.main()
