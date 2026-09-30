"""Phase 2C：全屏终端界面（textual TUI）冒烟与审批链路测试。"""
from __future__ import annotations

import argparse
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import api_config
import cli
import cli_tui
import config
import filestore


def _args(**overrides):
    values = {"session": None, "resume": False, "workdir": None,
              "provider": None, "danger": False, "plain": True}
    values.update(overrides)
    return argparse.Namespace(**values)


class TuiLaunchTests(unittest.TestCase):
    def test_main_dispatches_to_tui_by_default(self):
        with mock.patch("cli._interactive_terminal", return_value=True), \
             mock.patch("cli_tui.run", return_value=0) as run:
            self.assertEqual(cli.main([]), 0)
            run.assert_called_once()

    def test_main_without_tty_falls_back_to_plain(self):
        with mock.patch("cli._interactive_terminal", return_value=False), \
             mock.patch("cli_tui.run") as run, \
             mock.patch.object(cli.QuizForgeCli, "setup",
                               side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit):
                cli.main([])
            run.assert_not_called()

    def test_main_plain_skips_tui(self):
        with mock.patch("cli_tui.run") as run:
            with mock.patch.object(cli.QuizForgeCli, "setup",
                                   side_effect=SystemExit(0)):
                with self.assertRaises(SystemExit):
                    cli.main(["--plain"])
            run.assert_not_called()


class TuiAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.bank = base / "bank"
        (self.bank / "数学").mkdir(parents=True)
        self._patchers = [
            mock.patch.object(config, "BANK_DIR", self.bank),
            mock.patch.object(config, "AGENT_SESSIONS_PATH",
                              base / "agent_sessions.json"),
            mock.patch.object(api_config, "probe_magpie",
                              return_value={"available": False}),
        ]
        for patcher in self._patchers:
            patcher.start()
        filestore.invalidate_scan_cache()

    async def asyncTearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        filestore.invalidate_scan_cache()
        self._tmp.cleanup()

    @staticmethod
    async def _wait_for(predicate, *, timeout: float = 8.0) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return False

    async def _wait_for_button(self, app, selector: str) -> bool:
        """等到弹窗按钮真正完成布局（region 非零）再交互。"""
        def ready() -> bool:
            found = app.screen.query(selector)
            if not found:
                return False
            try:
                return found.first().region.width > 0
            except Exception:  # noqa: BLE001 - 未挂载完成时取 region 会异常
                return False

        return await self._wait_for(ready)

    async def test_mounts_with_session_and_cycles_permissions(self):
        app = cli_tui.QuizForgeTui(_args())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertTrue(app.sid)
            self.assertIn("standard", app.status_text)
            app.action_toggle_mode()
            await pilot.pause()
            self.assertEqual(app._permission_level(), "full")
            self.assertIn("FULL ACCESS", app.status_text)
            app.action_toggle_mode()
            await pilot.pause()
            self.assertEqual(app._permission_level(), "read-only")
            self.assertIn("READ-ONLY", app.status_text)
            app.action_toggle_mode()
            await pilot.pause()
            self.assertEqual(app._permission_level(), "standard")

    async def test_permissions_command_with_argument(self):
        app = cli_tui.QuizForgeTui(_args())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._command("/permissions full")
            await pilot.pause()
            self.assertEqual(app._permission_level(), "full")
            app._command("/permissions read-only")
            await pilot.pause()
            self.assertEqual(app._permission_level(), "read-only")
            app._command("/permissions nonsense")
            await pilot.pause()
            self.assertEqual(app._permission_level(), "read-only")

    async def test_permissions_panel_selects_full(self):
        app = cli_tui.QuizForgeTui(_args())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._command("/permissions")   # 无参 → 弹选择面板
            self.assertTrue(await self._wait_for_button(app, "#choice-full"))
            await pilot.click("#choice-full")
            self.assertTrue(await self._wait_for(
                lambda: app._permission_level() == "full"))

    async def test_all_slash_commands_are_handled(self):
        app = cli_tui.QuizForgeTui(_args())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for line in ("/help", "/config", "/clear", "/new", "/sessions",
                         "/model", "/provider", "/mode standard"):
                app._command(line)
                await pilot.pause()
            self.assertTrue(app.sid)

    async def test_new_session_action_changes_id(self):
        app = cli_tui.QuizForgeTui(_args())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            first = app.sid
            app.action_new_session()
            await pilot.pause()
            self.assertNotEqual(first, app.sid)

    async def test_choice_screen_returns_selection(self):
        app = cli_tui.QuizForgeTui(_args())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            result: dict = {}

            async def ask() -> None:
                try:
                    result["value"] = await app.push_screen_wait(cli_tui.ChoiceScreen(
                        "确认", "正文",
                        [("yes", "执行", "success"), ("no", "取消", "error")]))
                except Exception as exc:  # noqa: BLE001
                    result["error"] = repr(exc)

            app.run_worker(ask(), exclusive=False)
            self.assertTrue(await self._wait_for_button(app, "#choice-yes"))
            await pilot.click("#choice-yes")
            self.assertTrue(await self._wait_for(lambda: "value" in result),
                            f"未取得弹窗结果：{result}")
            self.assertEqual(result["value"], "yes")

    async def test_approval_modal_executes_after_click(self):
        app = cli_tui.QuizForgeTui(_args())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            approval = {"id": "ap1", "summary": "批量改难度",
                        "arguments": {"difficulty": "难"}}

            def fake_run_turn(runtime, sid, text, **kwargs):
                kwargs["on_event"]({"type": "assistant_delta", "delta": "好的"})
                kwargs["on_event"]({"type": "approval",
                                    "name": "bulk_update_questions",
                                    "approval": approval})
                return "好的", []

            app.approvals = mock.Mock()
            app.approvals.approve = mock.Mock()
            execution = mock.Mock(return_value=(None, True, {"count": 2}))
            with mock.patch.object(cli_tui.agent_orchestrator, "run_turn",
                                   side_effect=fake_run_turn), \
                 mock.patch.object(cli_tui.agent_services, "execute_approval",
                                   execution):
                app._start_turn("把这两道题改成难")
                self.assertTrue(await self._wait_for_button(app, "#choice-yes"),
                                "审批弹窗未出现")
                await pilot.click("#choice-yes")
                self.assertTrue(await self._wait_for(lambda: execution.called),
                                "审批未执行")
            execution.assert_called_once()


if __name__ == "__main__":
    unittest.main()
