"""Agent 流式 HTTP 协议与短期危险授权回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_core
import app as app_module


class AgentStreamingApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.original_runtime = app_module.agent_runtime
        app_module.agent_runtime = agent_core.AgentRuntime(Path(self.temporary.name))
        self.addCleanup(setattr, app_module, "agent_runtime", self.original_runtime)
        with app_module._agent_danger_lock:
            app_module._agent_danger_grants.clear()
        self.client = app_module.app.test_client()
        self.headers = {"X-CSRF-Token": app_module._WRITE_TOKEN}
        self.session = app_module.agent_runtime.new_session(scope="chat")

    def tearDown(self):
        with app_module._agent_danger_lock:
            app_module._agent_danger_grants.clear()

    def test_danger_grant_is_explicit_ephemeral_and_bound_to_session(self):
        sid = self.session["id"]
        missing_ack = self.client.post(
            f"/api/agent/sessions/{sid}/danger", json={}, headers=self.headers)
        self.assertEqual(missing_ack.status_code, 400)

        armed = self.client.post(
            f"/api/agent/sessions/{sid}/danger",
            json={"acknowledged": True}, headers=self.headers)
        self.assertEqual(armed.status_code, 200)
        token = armed.get_json()["danger_token"]
        self.assertTrue(token)
        self.assertEqual(app_module.agent_runtime.get_session(sid)["mode"], "standard")

        with app_module.app.test_request_context(
                headers={"X-Agent-Danger-Token": token}):
            effective = app_module._agent_effective_session(sid)
        self.assertEqual(effective["mode"], "danger")

        second = app_module.agent_runtime.new_session(scope="chat")
        with app_module.app.test_request_context(
                headers={"X-Agent-Danger-Token": token}):
            with self.assertRaisesRegex(agent_core.AgentError, "授权已失效"):
                app_module._agent_effective_session(second["id"])

        rejected = self.client.patch(
            f"/api/agent/sessions/{sid}", json={"mode": "danger"},
            headers=self.headers)
        self.assertEqual(rejected.status_code, 400)
        revoked = self.client.delete(
            f"/api/agent/sessions/{sid}/danger",
            headers={**self.headers, "X-Agent-Danger-Token": token})
        self.assertEqual(revoked.status_code, 200)
        with app_module.app.test_request_context(
                headers={"X-Agent-Danger-Token": token}):
            with self.assertRaisesRegex(agent_core.AgentError, "授权已失效"):
                app_module._agent_effective_session(sid)

    def test_stream_protocol_has_ordered_fixed_events_and_terminal_snapshot(self):
        sid = self.session["id"]

        def fake_turn(runtime, session_id, content, **kwargs):
            control = kwargs["turn_control"]
            runtime.append(session_id, "user", content, turn_id=control.id)
            kwargs["on_event"]({"type": "assistant_delta", "delta": "你"})
            kwargs["on_event"]({"type": "assistant_delta", "delta": "好"})
            runtime.append(session_id, "assistant", "你好", turn_id=control.id)
            return "你好", []

        with mock.patch.object(app_module.agent_orchestrator, "run_turn",
                               side_effect=fake_turn):
            response = self.client.post(
                "/api/agent/message/stream",
                json={"session_id": sid, "content": "你好"},
                headers=self.headers, buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        payloads = [
            json.loads(line[6:])
            for line in response.get_data(as_text=True).splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [item["type"] for item in payloads],
            ["turn_started", "assistant_delta", "assistant_delta", "turn_finished"],
        )
        self.assertEqual([item["seq"] for item in payloads], [1, 2, 3, 4])
        self.assertEqual(len({item["turn_id"] for item in payloads}), 1)
        terminal = payloads[-1]
        self.assertEqual(terminal["status"], "complete")
        self.assertEqual(terminal["session"]["messages"][-1]["content"], "你好")

    def test_cancel_endpoint_is_idempotent_and_approval_creation_is_closed(self):
        control = app_module.agent_runtime.start_turn(self.session["id"])
        closer = mock.Mock()
        control.bind_closer(closer)
        try:
            url = f"/api/agent/turns/{control.id}/cancel"
            for _ in range(2):
                response = self.client.post(
                    url, json={"session_id": self.session["id"]},
                    headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["turn"]["cancelled"])
            closer.assert_called_once_with()
        finally:
            app_module.agent_runtime.finish_turn(control, "stopped")

        response = self.client.post(
            "/api/agent/approvals",
            json={"session_id": self.session["id"], "action": "create_folder"},
            headers=self.headers)
        self.assertEqual(response.status_code, 405)

    def test_general_tool_endpoint_uses_session_lock_and_releases_after_failure(self):
        sid = self.session["id"]
        control = app_module.agent_runtime.start_turn(sid)
        try:
            with mock.patch.object(app_module.agent_tools, "dispatch") as dispatch:
                busy = self.client.post(
                    "/api/agent/tool",
                    json={"session_id": sid, "name": "list_folders", "arguments": {}},
                    headers=self.headers)
            self.assertEqual(busy.status_code, 409)
            dispatch.assert_not_called()
        finally:
            app_module.agent_runtime.finish_turn(control, "complete")

        with mock.patch.object(
                app_module.agent_tools, "dispatch",
                side_effect=app_module.agent_tools.ToolError("工具失败")):
            failed = self.client.post(
                "/api/agent/tool",
                json={"session_id": sid, "name": "list_folders", "arguments": {}},
                headers=self.headers)
        self.assertEqual(failed.status_code, 400)

        with mock.patch.object(
                app_module.agent_tools, "dispatch", return_value={"folders": []}):
            retried = self.client.post(
                "/api/agent/tool",
                json={"session_id": sid, "name": "list_folders", "arguments": {}},
                headers=self.headers)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.get_json()["result"], {"folders": []})


if __name__ == "__main__":
    unittest.main()
