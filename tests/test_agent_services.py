"""Phase 2A：Agent 服务解耦与共享审批执行核心。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_approvals
import agent_core
import agent_services
import agent_snapshots
import agent_tools
import config
import filestore

BASE_DIR = Path(__file__).resolve().parent.parent


class SharedApprovalExecutionTests(unittest.TestCase):
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
             "source": "练习", "tags": []},
        ], "数学")
        self.runtime = agent_core.AgentRuntime(self.bank, None)
        self.approvals = agent_approvals.ApprovalStore()
        agent_services.configure(runtime=self.runtime, approvals=self.approvals)

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        filestore.invalidate_scan_cache()
        self._tmp.cleanup()

    def test_execute_approval_runs_write_once(self):
        session = self.runtime.new_session(workdir=None, scope="bank")
        result = agent_tools.dispatch(
            "update_question",
            {"id": self.qids[0], "body": "1+1=?（已改）", "difficulty": "难"},
            session=session, approval_store=self.approvals)
        self.assertTrue(result["pending_confirmation"])
        approval_id = result["approval"]["id"]

        # 未批准前不能领取执行权
        with self.assertRaises(agent_approvals.ApprovalError):
            agent_services.execute_approval(session, approval_id)

        self.approvals.approve(session, approval_id)
        row, executed, _payload = agent_services.execute_approval(session, approval_id)
        self.assertTrue(executed)
        self.assertEqual(row["status"], "executed")
        self.assertEqual(filestore.get_question(self.qids[0])["difficulty"], "难")
        self.assertEqual(len(agent_snapshots.list_snapshots()), 1)

        # 已执行后确认项进入终态，不会二次写入（不重复产生快照）
        self.assertEqual(
            self.approvals.get(session["id"], approval_id)["status"], "executed")
        self.assertEqual(len(agent_snapshots.list_snapshots()), 1)

    def test_service_tools_cover_new_services(self):
        self.assertIn("apply_review_fixes", agent_tools.SERVICE_TOOLS)
        self.assertIn("enable_template", agent_tools.SERVICE_TOOLS)
        self.assertLessEqual(agent_tools.SERVICE_TOOLS,
                             agent_services._AGENT_SERVICE_ACTIONS)


class ModuleBoundaryTests(unittest.TestCase):
    def test_import_agent_services_does_not_load_flask(self):
        code = ("import sys, agent_services; "
                "print('flask' in sys.modules, 'agent_catalog' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(BASE_DIR),
                             capture_output=True, text=True, timeout=180)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "False False")


if __name__ == "__main__":
    unittest.main()
