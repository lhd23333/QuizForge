"""Agent 会话与题库工具边界回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_core
import agent_tools
import config


class AgentRuntimeTests(unittest.TestCase):
    def test_context_update_is_atomic_when_path_is_invalid(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bank"
            root.mkdir()
            runtime = agent_core.AgentRuntime(root)
            created = runtime.new_session(scope="chat")

            with self.assertRaises(agent_core.AgentError):
                runtime.set_workdir(created["id"], "../outside", scope="bank")

            current = runtime.get_session(created["id"])
            self.assertEqual(current["scope"], "chat")
            self.assertIsNone(current["workdir"])
            self.assertEqual(current["workdir_id"], "")

    def test_public_session_does_not_expose_internal_message_list(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = agent_core.AgentRuntime(Path(raw))
            session = runtime.new_session()
            snapshot = runtime.public_session(runtime.get_session(session["id"]))
            snapshot["messages"].append({"role": "user", "content": "外部修改"})
            self.assertEqual(runtime.get_session(session["id"])["messages"], [])

    def test_persisted_session_restores_context_without_absolute_workdir(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bank"
            folder = root / "math"
            folder.mkdir(parents=True)
            store = Path(raw) / "agent_sessions.json"

            runtime = agent_core.AgentRuntime(root, store)
            created = runtime.new_session("math")
            runtime.append(created["id"], "user", "请整理这组题")
            runtime.append(created["id"], "assistant", "好的")
            runtime.set_provider(created["id"], "provider-1")

            payload = json.loads(store.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            saved = next(row for row in payload["sessions"]
                         if row["id"] == created["id"])
            self.assertEqual(saved["workdir_id"], "math")
            self.assertNotIn("workdir", saved)
            self.assertEqual(saved["bank_root"], str(root.resolve()))
            self.assertEqual(saved["mode"], "standard")

            # 兼容旧版遗留文件，但重启时必须撤销危险授权。
            saved["mode"] = "danger"
            store.write_text(json.dumps(payload), encoding="utf-8")

            restored = agent_core.AgentRuntime(root, store)
            current = restored.get_session(created["id"])
            self.assertEqual(current["scope"], "bank")
            self.assertEqual(current["workdir_id"], "math")
            self.assertEqual(current["workdir"], str(folder.resolve()))
            self.assertEqual(current["mode"], "standard")
            self.assertEqual(current["provider_id"], "provider-1")
            self.assertEqual(
                [item["content"] for item in current["messages"]],
                ["请整理这组题", "好的"],
            )

    def test_danger_mode_cannot_be_persisted_on_session(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = agent_core.AgentRuntime(Path(raw))
            session = runtime.new_session()
            with self.assertRaisesRegex(agent_core.AgentError, "当前页面"):
                runtime.set_mode(session["id"], "danger")
            self.assertEqual(runtime.get_session(session["id"])["mode"], "standard")

    def test_turn_cancel_is_idempotent_and_releases_lock_once(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = agent_core.AgentRuntime(Path(raw))
            session = runtime.new_session()
            control = runtime.start_turn(session["id"])
            closer = mock.Mock()
            control.bind_closer(closer)

            first = runtime.cancel_turn(control.id, session["id"])
            second = runtime.cancel_turn(control.id, session["id"])
            self.assertTrue(first["cancelled"])
            self.assertTrue(second["cancelled"])
            closer.assert_called_once_with()

            runtime.finish_turn(control, "stopped")
            runtime.finish_turn(control, "complete")
            with runtime.turn(session["id"]):
                pass

    def test_persistence_isolated_between_bank_roots(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root_a = base / "bank-a"
            root_b = base / "bank-b"
            root_a.mkdir()
            root_b.mkdir()
            store = base / "agent_sessions.json"

            first = agent_core.AgentRuntime(root_a, store).new_session()
            second = agent_core.AgentRuntime(root_b, store).new_session()

            restored_a = agent_core.AgentRuntime(root_a, store)
            restored_b = agent_core.AgentRuntime(root_b, store)
            self.assertEqual(
                [row["id"] for row in restored_a.list_sessions()], [first["id"]]
            )
            self.assertEqual(
                [row["id"] for row in restored_b.list_sessions()], [second["id"]]
            )

    def test_corrupt_or_unsafe_persisted_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bank"
            root.mkdir()
            store = Path(raw) / "agent_sessions.json"
            payload = {
                "version": 1,
                "bank_root": str(root.resolve()),
                "sessions": [
                    {"id": "valid", "scope": "chat", "mode": "standard",
                     "messages": [{"role": "user", "content": "保留"}]},
                    {"id": "outside", "bank_root": str(Path(raw).resolve()),
                     "scope": "chat", "mode": "standard"},
                    {"id": "escape", "scope": "bank", "mode": "standard",
                     "workdir_id": "../outside"},
                    {"id": "bad-mode", "scope": "bank", "mode": "unrestricted"},
                    "not-a-row",
                ],
            }
            store.write_text(json.dumps(payload), encoding="utf-8")

            runtime = agent_core.AgentRuntime(root, store)
            self.assertEqual([row["id"] for row in runtime.list_sessions()], ["valid"])

    def test_delete_is_persisted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = root / "agent_sessions.json"
            runtime = agent_core.AgentRuntime(root, store)
            session = runtime.new_session()
            runtime.delete_session(session["id"])
            restored = agent_core.AgentRuntime(root, store)
            self.assertEqual(restored.list_sessions(), [])

    def test_workdir_must_be_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "question.md").write_text("题目", encoding="utf-8")
            runtime = agent_core.AgentRuntime(root)

            with self.assertRaisesRegex(agent_core.AgentError, "不是文件夹"):
                runtime.new_session("question.md")
            session = runtime.new_session()
            with self.assertRaisesRegex(agent_core.AgentError, "不存在"):
                runtime.update_session(session["id"], workdir="missing")
            self.assertEqual(runtime.get_session(session["id"])["workdir_id"], "")

    def test_output_directory_is_scoped_to_workdir_and_persisted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bank"
            (root / "math" / "exports").mkdir(parents=True)
            store = Path(raw) / "agent_sessions.json"
            runtime = agent_core.AgentRuntime(root, store)
            session = runtime.new_session("math", output_dir="math/exports")
            self.assertEqual(session["workdir_id"], "math")
            self.assertEqual(session["output_dir_id"], "math/exports")
            updated = runtime.update_session(session["id"], output_dir="math")
            self.assertEqual(updated["output_dir_id"], "math")
            restored = agent_core.AgentRuntime(root, store).get_session(session["id"])
            self.assertEqual(restored["output_dir_id"], "math")
            with self.assertRaises(agent_core.AgentError):
                runtime.update_session(session["id"], output_dir="other")

    def test_input_directory_is_independent_and_persisted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bank"
            (root / "materials").mkdir(parents=True)
            (root / "questions").mkdir(parents=True)
            store = Path(raw) / "agent_sessions.json"
            runtime = agent_core.AgentRuntime(root, store)
            session = runtime.new_session("questions", input_dir="materials")
            self.assertEqual(session["workdir_id"], "questions")
            self.assertEqual(session["input_dir_id"], "materials")
            updated = runtime.update_session(session["id"], input_dir="questions")
            self.assertEqual(updated["input_dir_id"], "questions")
            restored = agent_core.AgentRuntime(root, store).get_session(session["id"])
            self.assertEqual(restored["input_dir_id"], "questions")


class AgentToolScopeTests(unittest.TestCase):
    def test_every_command_requires_approval_in_standard_mode(self):
        plan = {"destructive": False, "cwd_folder": "", "language": "powershell",
                "command": "Get-ChildItem", "timeout": 30}
        store = mock.Mock()
        store.create.return_value = {"id": "approval-1", "status": "pending"}
        session = {"id": "s1", "scope": "bank", "workdir_id": "",
                   "mode": "standard"}
        with mock.patch.object(agent_tools, "_command_plan", return_value=plan), \
                mock.patch.object(agent_tools, "_execute_command") as execute:
            result = agent_tools.dispatch(
                "execute_command", {"command": "Get-ChildItem"},
                session=session, approval_store=store)
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["approval"]["id"], "approval-1")
        store.create.assert_called_once()
        execute.assert_not_called()

    def test_danger_mode_executes_command_without_approval(self):
        plan = {"destructive": True, "cwd_folder": "", "language": "powershell",
                "command": "Set-Content x y", "timeout": 30}
        store = mock.Mock()
        session = {"id": "s1", "scope": "bank", "workdir_id": "",
                   "mode": "danger"}
        with mock.patch.object(agent_tools, "_command_plan", return_value=plan), \
                mock.patch.object(agent_tools, "_execute_command",
                                  return_value={"ok": True}) as execute:
            result = agent_tools.dispatch(
                "execute_command", {"command": "Set-Content x y"},
                session=session, approval_store=store)
        self.assertTrue(result["ok"])
        execute.assert_called_once()
        store.create.assert_not_called()

    def test_browse_quizforge_reads_all_registered_areas(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bank"
            for name in ("_assets", "_handouts"):
                (root / name).mkdir(parents=True)
            (root / "library.md").write_text("资料", encoding="utf-8")
            state = Path(raw) / "state"
            (state / "history").mkdir(parents=True)
            (state / "history" / "job.md").write_text("识别", encoding="utf-8")
            (root / "_assets" / "figure.png").write_bytes(b"png")
            with mock.patch.object(config, "BANK_DIR", root), \
                    mock.patch.object(config, "ASSETS_DIR", root / "_assets"), \
                    mock.patch.object(config, "HANDOUTS_DIR", root / "_handouts"), \
                    mock.patch.object(config, "TRASH_DIR", root / ".trash"), \
                    mock.patch.object(config, "HISTORY_DIR", state / "history"):
                for area in ("bank", "library", "assets", "handouts", "history"):
                    result = agent_tools.dispatch("browse_quizforge", {
                        "area": area, "read_text": True,
                    }, session={"scope": "bank", "workdir_id": ""})
                    self.assertEqual(result["area"], area)
                asset = agent_tools.dispatch("browse_quizforge", {
                    "area": "assets", "path": "figure.png",
                }, session={"scope": "bank", "workdir_id": ""})
                self.assertEqual(asset["entries"][0]["size"], 3)

    def test_search_cannot_escape_bound_subfolder(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(config, "BANK_DIR", root):
                with mock.patch.object(
                        agent_tools.filestore, "collection_records_snapshot") as snapshot:
                    with self.assertRaises(agent_tools.ToolError):
                        agent_tools.dispatch(
                            "search_questions",
                            {"query": "函数", "folder": "physics"},
                            session={"scope": "bank", "workdir_id": "math"},
                        )
                    snapshot.assert_not_called()

    def test_search_allows_bound_subfolder(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(config, "BANK_DIR", root):
                with mock.patch.object(
                        agent_tools.filestore,
                        "collection_records_snapshot",
                        return_value=[],
                    ) as snapshot, mock.patch.object(
                        agent_tools.filestore, "list_questions", return_value=[]):
                    result = agent_tools.dispatch(
                        "search_questions",
                        {"query": "函数", "folder": "math/calculus"},
                        session={"scope": "bank", "workdir_id": "math"},
                    )
                    self.assertEqual(result["total"], 0)
                    snapshot.assert_called_once_with("math/calculus")

    def test_list_folders_only_returns_bound_path(self):
        tree = [
            {"id": "math", "name": "数学", "children": [
                {"id": "math/calculus", "name": "微积分", "children": [
                    {"id": "math/calculus/limits", "name": "极限", "children": []},
                ]},
                {"id": "math/algebra", "name": "代数", "children": []},
            ]},
            {"id": "physics", "name": "物理", "children": []},
        ]
        with mock.patch.object(
                agent_tools.filestore,
                "list_navigation_tree", return_value=tree) as list_tree:
            result = agent_tools.dispatch(
                "list_folders",
                session={"scope": "bank", "workdir_id": "math/calculus"},
            )

        list_tree.assert_called_once_with(active_id="math/calculus")
        self.assertEqual(
            result["folders"],
            [{"id": "math", "name": "数学", "children": [
                {"id": "math/calculus", "name": "微积分", "children": [
                    {"id": "math/calculus/limits", "name": "极限", "children": []},
                ]},
            ]}],
        )

    def test_read_question_rejects_question_outside_bound_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.object(config, "BANK_DIR", Path(raw)), mock.patch.object(
                    agent_tools.filestore,
                    "get_question",
                    return_value={"id": "q1", "folder": "physics", "body": "..."},
                ):
                with self.assertRaises(agent_tools.ToolError):
                    agent_tools.dispatch(
                        "read_question", {"id": "q1"},
                        session={"scope": "bank", "workdir_id": "math"},
                    )

    def test_check_duplicates_is_scoped_and_returns_redacted_groups(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "math").mkdir()
            records = [
                {"id": "q1", "body": "求 $x+1$", "folder": "math",
                 "title": "第一题", "path": str(root / "math" / "q1.md")},
                {"id": "q2", "body": "求 x + 1", "folder": "math",
                 "title": "第二题", "path": str(root / "math" / "q2.md")},
            ]
            with mock.patch.object(config, "BANK_DIR", root), \
                    mock.patch.object(
                        agent_tools.filestore,
                        "collection_records_snapshot",
                        return_value=records,
                    ) as snapshot:
                result = agent_tools.dispatch(
                    "check_duplicates", {"threshold": 0.85},
                    session={"scope": "bank", "workdir_id": "math"},
                )

            snapshot.assert_called_once_with("math")
            self.assertEqual(result["scanned"], 2)
            self.assertEqual(result["total_groups"], 1)
            self.assertEqual(result["groups"][0]["kind"], "exact")
            self.assertEqual(
                {member["id"] for member in result["groups"][0]["members"]},
                {"q1", "q2"},
            )
            for member in result["groups"][0]["members"]:
                self.assertNotIn("path", member)

    def test_check_duplicates_rejects_folder_outside_bound(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "math").mkdir()
            with mock.patch.object(config, "BANK_DIR", root), mock.patch.object(
                    agent_tools.filestore,
                    "collection_records_snapshot",
                ) as snapshot:
                with self.assertRaises(agent_tools.ToolError):
                    agent_tools.dispatch(
                        "check_duplicates", {"folder": "physics"},
                        session={"scope": "bank", "workdir_id": "math"},
                    )
                snapshot.assert_not_called()

    def test_check_duplicates_rejects_chat_scope(self):
        with self.assertRaises(agent_tools.ToolError):
            agent_tools.dispatch(
                "check_duplicates", {},
                session={"scope": "chat", "workdir_id": ""},
            )


if __name__ == "__main__":
    unittest.main()
