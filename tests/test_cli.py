"""Phase 2B：CLI 辅助逻辑、审批决策与会话互通。"""
from __future__ import annotations

import argparse
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_core
import cli


class HelperTests(unittest.TestCase):
    def test_pick_by_index_id_and_name(self):
        rows = [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}]
        self.assertEqual(cli._pick(rows, "2")["id"], "b")
        self.assertEqual(cli._pick(rows, "a")["id"], "a")
        self.assertEqual(cli._pick(rows, "乙")["id"], "b")
        self.assertIsNone(cli._pick(rows, "999"))
        self.assertIsNone(cli._pick(rows, ""))

    def test_summarize_counts_lists(self):
        text = cli._summarize({"count": 2, "imported_ids": ["x", "y"],
                               "folder": "数学", "message": ""})
        self.assertIn("count=2", text)
        self.assertIn("imported_ids=2", text)
        self.assertIn("folder=数学", text)


class RendererTests(unittest.TestCase):
    def test_renderer_collects_approvals_and_deltas(self):
        renderer = cli._TurnRenderer()
        renderer.handle({"type": "assistant_delta", "delta": "你好"})
        renderer.handle({"type": "tool_state", "name": "list_folders",
                         "status": "done"})
        renderer.handle({"type": "approval",
                         "approval": {"id": "a1", "summary": "改难度"}})
        renderer.finish()
        self.assertEqual(len(renderer.approvals), 1)
        self.assertEqual(renderer.approvals[0]["approval"]["id"], "a1")


class ApprovalDecisionTests(unittest.TestCase):
    def _bare_cli(self) -> cli.QuizForgeCli:
        obj = cli.QuizForgeCli.__new__(cli.QuizForgeCli)
        obj.sid = "s1"
        obj.permissions = {}
        obj.runtime = mock.Mock()
        obj.approvals = mock.Mock()
        obj._approve_and_execute = mock.Mock()
        obj._cancel_approval = mock.Mock()
        return obj

    def test_yes_executes_without_permission_change(self):
        obj = self._bare_cli()
        with mock.patch("builtins.input", return_value="y"):
            obj._handle_approvals([{"approval": {"id": "a1", "summary": "x"}}])
        obj._approve_and_execute.assert_called_once_with("a1")
        obj._cancel_approval.assert_not_called()
        self.assertEqual(obj._permission_level(), "standard")

    def test_always_opens_full_permission(self):
        obj = self._bare_cli()
        with mock.patch("builtins.input", return_value="a"):
            obj._handle_approvals([{"approval": {"id": "a2", "summary": "x"}}])
        self.assertEqual(obj._permission_level(), "full")
        obj._approve_and_execute.assert_called_once_with("a2")

    def test_no_cancels(self):
        obj = self._bare_cli()
        with mock.patch("builtins.input", return_value="n"):
            obj._handle_approvals([{"approval": {"id": "a3", "summary": "x"}}])
        obj._cancel_approval.assert_called_once_with("a3")
        obj._approve_and_execute.assert_not_called()
        self.assertEqual(obj._permission_level(), "standard")


class PermissionCommandTests(unittest.TestCase):
    def _cli(self) -> cli.QuizForgeCli:
        obj = cli.QuizForgeCli.__new__(cli.QuizForgeCli)
        obj.sid = "s1"
        obj.permissions = {}
        obj.runtime = mock.Mock()
        obj.approvals = mock.Mock()
        return obj

    def _capture(self, obj, command, arg=""):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            command(arg)
        return buf.getvalue()

    def test_permission_levels_and_mode_mapping(self):
        self.assertEqual(cli.mode_for_level("read-only"), "readonly")
        self.assertEqual(cli.mode_for_level("full"), "danger")
        self.assertEqual(cli.parse_permission_level("danger"), "full")
        self.assertEqual(cli.parse_permission_level("readonly"), "read-only")
        self.assertIsNone(cli.parse_permission_level("nope"))

    def test_permissions_switches_between_all_levels(self):
        obj = self._cli()
        for arg, expected in (("read-only", "read-only"), ("full", "full"),
                              ("standard", "standard"), ("danger", "full"),
                              ("readonly", "read-only")):
            self._capture(obj, obj.cmd_permissions, arg)
            self.assertEqual(obj._permission_level(), expected)
        self.assertEqual(obj._permission_mode(), cli.mode_for_level(expected))

    def test_permissions_rejects_unknown_level(self):
        obj = self._cli()
        out = self._capture(obj, obj.cmd_permissions, "whatever")
        self.assertIn("未知权限档位", out)
        self.assertEqual(obj._permission_level(), "standard")

    def test_permissions_without_arg_lists_levels(self):
        obj = self._cli()
        out = self._capture(obj, obj.cmd_permissions, "")
        for level in cli.PERMISSION_LEVELS:
            self.assertIn(level, out)

    def test_mode_is_alias_of_permissions(self):
        obj = self._cli()
        self._capture(obj, obj.cmd_mode, "full")
        self.assertEqual(obj._permission_level(), "full")

    def test_permission_is_per_session(self):
        obj = self._cli()
        obj.sid = "s1"
        self._capture(obj, obj.cmd_permissions, "full")
        obj.sid = "s2"
        self.assertEqual(obj._permission_level(), "standard")


class CommandCoverageTests(unittest.TestCase):
    """每条斜杠命令都能被解析且不抛异常（输出用 mock 掉）。"""

    def _cli_with_args(self):
        obj = cli.QuizForgeCli.__new__(cli.QuizForgeCli)
        obj.sid = "s1"
        obj.permissions = {}
        obj.runtime = mock.Mock()
        obj.approvals = mock.Mock()
        obj.args = argparse.Namespace(workdir=None, permissions=None, danger=False)
        return obj

    def test_help_and_config_and_clear_and_new(self):
        obj = self._cli_with_args()
        obj.runtime.get_session.return_value = {
            "id": "s1", "scope": "bank", "workdir_id": "", "provider_id": None}
        obj.runtime.root = "X:/bank"
        obj.runtime.new_session.return_value = {
            "id": "s2", "workdir_id": ""}
        obj._effective_provider = mock.Mock(return_value=None)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            obj.cmd_help("")
            obj.cmd_config("")
            obj.cmd_clear("")
            obj.cmd_new("")
        out = buf.getvalue()
        self.assertIn("可用命令", out)
        self.assertIn("权限档位", out)
        self.assertEqual(obj.sid, "s2")

    def test_sessions_and_model_and_provider(self):
        obj = self._cli_with_args()
        obj.runtime.list_sessions.return_value = [
            {"id": "s1", "scope": "bank", "workdir_id": "", "messages": []}]
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), \
             mock.patch("cli.agent_provider.list_public", return_value=[]), \
             mock.patch("cli.api_config.probe_magpie",
                        return_value={"available": False}):
            obj.cmd_sessions("")
            obj.cmd_model("")
            obj.cmd_provider("magpie")
        out = buf.getvalue()
        self.assertIn("s1", out)
        self.assertIn("未配置 Provider", out)
        self.assertIn("不可达", out)

    def test_all_help_commands_have_handlers(self):
        obj = self._cli_with_args()
        handlers = {
            "help": obj.cmd_help, "config": obj.cmd_config,
            "clear": obj.cmd_clear, "new": obj.cmd_new,
            "mode": obj.cmd_mode, "permissions": obj.cmd_permissions,
            "sessions": obj.cmd_sessions, "model": obj.cmd_model,
            "provider": obj.cmd_provider,
        }
        known = set(handlers) | {"quit"}
        # 帮助里列出的每个 /命令 都必须有处理器（只扫命令行，不扫正文里的 y/n）
        for token in re.findall(r"(?m)^\s+/([a-z][a-z-]*)", cli.HELP_TEXT):
            self.assertIn(token, known, f"帮助里列了 /{token} 但没有处理器")
        # 每个处理器都必须在帮助里出现
        for name in handlers:
            self.assertRegex(cli.HELP_TEXT, rf"/{name}\b", f"{name} 未写进帮助")


class TurnOverrideTests(unittest.TestCase):
    """权限档位只经 session_override 传给编排层，不写回持久化会话。"""

    def _cli(self, level: str):
        obj = cli.QuizForgeCli.__new__(cli.QuizForgeCli)
        obj.sid = "s1"
        obj.permissions = {"s1": level}
        obj.approvals = mock.Mock()
        obj.runtime = mock.Mock()
        obj.runtime.start_turn.return_value = mock.Mock()
        obj.runtime.get_session.return_value = {
            "id": "s1", "scope": "bank", "workdir_id": "",
            "provider_id": None, "mode": "standard"}
        obj._handle_approvals = mock.Mock()
        return obj

    def _override(self, level):
        obj = self._cli(level)
        with mock.patch.object(cli.agent_orchestrator, "run_turn",
                               return_value=("ok", [])) as run:
            obj.run_turn("你好")
        return run.call_args.kwargs

    def test_full_permission_maps_to_danger_mode(self):
        kwargs = self._override("full")
        self.assertEqual(kwargs["session_override"], {"mode": "danger"})
        self.assertTrue(kwargs["stream"])

    def test_read_only_permission_maps_to_readonly_mode(self):
        kwargs = self._override("read-only")
        self.assertEqual(kwargs["session_override"], {"mode": "readonly"})

    def test_standard_is_default(self):
        kwargs = self._override("standard")
        self.assertEqual(kwargs["session_override"], {"mode": "standard"})


class SessionInteropTests(unittest.TestCase):
    """GUI 与 CLI 共享同一 agent_sessions.json：一处建会话，另一处可见可续。"""

    def test_gui_session_visible_from_cli_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bank = base / "bank"
            bank.mkdir()
            sessions = base / "agent_sessions.json"

            gui = agent_core.AgentRuntime(bank, sessions)
            row = gui.new_session(workdir=None, scope="bank")
            gui.append(row["id"], "user", "来自 GUI 的消息")

            cli_runtime = agent_core.AgentRuntime(bank, sessions)
            ids = [item["id"] for item in cli_runtime.list_sessions()]
            self.assertIn(row["id"], ids)
            resumed = cli_runtime.get_session(row["id"])
            self.assertEqual(resumed["messages"][0]["content"], "来自 GUI 的消息")


if __name__ == "__main__":
    unittest.main()
