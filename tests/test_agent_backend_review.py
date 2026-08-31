"""Agent 后端契约回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_core
import agent_orchestrator
import agent_provider
import agent_tools
import config


class AgentConcurrencyTests(unittest.TestCase):
    def test_busy_turn_has_distinct_error_type(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = agent_core.AgentRuntime(Path(raw))
            session = runtime.new_session()
            with runtime.turn(session["id"]):
                with self.assertRaises(agent_core.AgentBusyError):
                    with runtime.turn(session["id"]):
                        pass


class AgentProviderTests(unittest.TestCase):
    def test_public_presets_are_openai_compatible_and_redacted(self):
        presets = agent_provider.list_presets()
        self.assertGreaterEqual(len(presets), 4)
        ids = {item["id"] for item in presets}
        self.assertTrue({"deepseek", "openai", "qwen", "openrouter"}.issubset(ids))
        for preset in presets:
            self.assertNotIn("api_key", preset)
            self.assertNotIn("api_key_enc", preset)
            self.assertTrue(str(preset["base_url"]).startswith(("http://", "https://")))
            self.assertTrue(preset["models"])

    def test_disabled_provider_is_not_selected_as_active(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "agent-providers.json"
            with mock.patch.object(config, "AGENT_PROVIDERS_PATH", path), \
                    mock.patch.object(agent_provider.crypto_utils,
                                      "encrypt_token", return_value="enc"):
                provider_id = agent_provider.create(
                    name="本地禁用", base_url="http://127.0.0.1:11434/v1",
                    api_key="", model="demo", enabled=False)
                self.assertIsNotNone(provider_id)
                self.assertIsNone(agent_provider.active())
                public = agent_provider.list_public()
                self.assertEqual(len(public), 1)
                self.assertFalse(public[0]["active"])

    def test_patch_can_change_one_field_and_rejects_public_without_key(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "agent-providers.json"
            with mock.patch.object(config, "AGENT_PROVIDERS_PATH", path), \
                    mock.patch.object(agent_provider.crypto_utils,
                                      "encrypt_token", return_value="enc"):
                provider_id = agent_provider.create(
                    name="本地", base_url="http://127.0.0.1:11434/v1",
                    api_key="", model="demo")
                self.assertTrue(agent_provider.update(provider_id, model="demo-2"))
                public = agent_provider.list_public()[0]
                self.assertEqual(public["model"], "demo-2")
                with self.assertRaises(agent_provider.AgentProviderError):
                    agent_provider.update(provider_id,
                                          base_url="https://api.example.com/v1")
                # 校验失败不能半更新原记录。
                self.assertEqual(agent_provider.list_public()[0]["base_url"],
                                 "http://127.0.0.1:11434/v1")

    def test_import_cc_switch_responses_config(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "agent-providers.json"
            config_text = '''
model_provider = "custom"
model = "gpt-5.6-sol"
model_reasoning_effort = "low"
service_tier = "default"
[model_providers.custom]
name = "YAPI"
wire_api = "responses"
base_url = "http://127.0.0.1:1234/v1"
experimental_bearer_token = "sk-test"
disable_response_storage = true
'''
            with mock.patch.object(config, "AGENT_PROVIDERS_PATH", path), \
                    mock.patch.object(agent_provider.crypto_utils,
                                      "encrypt_token", return_value="enc"):
                provider_id = agent_provider.import_cc_switch(config_text=config_text)
                public = agent_provider.list_public()[0]
                self.assertEqual(public["id"], provider_id)
                self.assertEqual(public["wire_api"], "responses")
                self.assertEqual(public["reasoning_effort"], "low")
                self.assertEqual(public["service_tier"], "default")
                self.assertFalse(public["store_responses"])


class AgentOrchestratorTests(unittest.TestCase):
    def _runtime(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        runtime = agent_core.AgentRuntime(Path(root.name))
        session = runtime.new_session()
        return runtime, session

    def test_invalid_provider_is_reported_instead_of_local_fallback(self):
        runtime, session = self._runtime()
        with self.assertRaises(agent_orchestrator.AgentTurnError):
            agent_orchestrator.run_turn(
                runtime, session["id"], "你好", provider_id="missing")

    def test_provider_decryption_error_is_wrapped(self):
        runtime, session = self._runtime()
        error = agent_provider.AgentProviderError("密钥无法解密")
        with mock.patch.object(agent_provider, "get", side_effect=error):
            with self.assertRaises(agent_orchestrator.AgentTurnError) as caught:
                agent_orchestrator.run_turn(
                    runtime, session["id"], "你好", provider_id="p1")
        self.assertIn("密钥无法解密", str(caught.exception))

    def test_unexpected_tool_error_is_returned_to_model(self):
        runtime, session = self._runtime()
        provider = agent_provider.AgentProviderConfig(
            id="p1", name="测试", base_url="http://127.0.0.1:11434/v1",
            api_key="local", model="demo")
        tool_call = {
            "id": "call-1", "type": "function",
            "function": {"name": "list_folders", "arguments": "{}"},
        }
        responses = iter([
            ("", [tool_call], {"role": "assistant", "content": "",
                               "tool_calls": [tool_call]}),
            ("工具出错但会话仍可继续", [], {
                "role": "assistant", "content": "工具出错但会话仍可继续"}),
        ])
        with mock.patch.object(agent_provider, "get", return_value=provider), \
                mock.patch.object(agent_orchestrator, "_model_turn",
                                  side_effect=lambda *args, **kwargs: next(responses)), \
                mock.patch.object(agent_tools, "dispatch",
                                  side_effect=RuntimeError("底层扫描失败")):
            reply, events = agent_orchestrator.run_turn(
                runtime, session["id"], "列出目录", provider_id="p1")
        self.assertEqual(reply, "工具出错但会话仍可继续")
        self.assertEqual(events[-1]["status"], "error")

    def test_chat_scope_does_not_expose_bank_tools_to_model(self):
        runtime, session = self._runtime()
        runtime.update_session(session["id"], scope="chat")
        provider = agent_provider.AgentProviderConfig(
            id="p1", name="测试", base_url="http://127.0.0.1:11434/v1",
            api_key="local", model="demo")
        captured = {}

        def model_turn(_provider, messages, *, tools):
            captured["messages"] = messages
            captured["tools"] = tools
            return "普通聊天回复", [], {
                "role": "assistant", "content": "普通聊天回复"}

        with mock.patch.object(agent_provider, "get", return_value=provider), \
                mock.patch.object(agent_orchestrator, "_model_turn",
                                  side_effect=model_turn), \
                mock.patch.object(agent_tools, "dispatch") as dispatch:
            reply, _events = agent_orchestrator.run_turn(
                runtime, session["id"], "你好", provider_id="p1")

        self.assertEqual(reply, "普通聊天回复")
        self.assertEqual(captured["tools"], [])
        self.assertIn("仅聊天", captured["messages"][0]["content"])
        dispatch.assert_not_called()

    def test_responses_input_and_output_are_normalized(self):
        messages = [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "列出目录"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1", "function": {"name": "list_folders", "arguments": "{}"}
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
        ]
        instructions, items = agent_orchestrator._responses_input(messages)
        self.assertEqual(instructions, "规则")
        self.assertEqual(items[1]["type"], "function_call")
        self.assertEqual(items[2]["type"], "function_call_output")
        response = SimpleNamespace(output_text="", output=[SimpleNamespace(
            type="function_call", call_id="call-2", name="list_folders", arguments="{}")])
        content, calls = agent_orchestrator._response_output_to_parts(response)
        self.assertEqual(content, "")
        self.assertEqual(calls[0]["function"]["name"], "list_folders")

    def test_chat_stream_aggregates_text_and_fragmented_tool_arguments(self):
        provider = agent_provider.AgentProviderConfig(
            id="p1", name="测试", base_url="http://127.0.0.1:11434/v1",
            api_key="local", model="demo")
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(content="先", tool_calls=[]))]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(content="看", tool_calls=[SimpleNamespace(
                    index=0, id="call-1", function=SimpleNamespace(
                        name="search_questions", arguments='{"query":'))]))]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls",
                delta=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
                    index=0, id=None, function=SimpleNamespace(
                        name=None, arguments='"极限"}'))]))]),
        ]

        class FakeStream(list):
            def close(self):
                self.closed = True

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=mock.Mock(return_value=FakeStream(chunks)))))
        deltas = []
        with mock.patch.object(agent_orchestrator, "_model_client", return_value=client):
            content, calls, assistant = agent_orchestrator._chat_stream_turn(
                provider, [{"role": "user", "content": "查找"}], tools=[],
                on_delta=deltas.append)

        self.assertEqual(content, "先看")
        self.assertEqual(deltas, ["先", "看"])
        self.assertEqual(calls[0]["id"], "call-1")
        self.assertEqual(calls[0]["function"]["name"], "search_questions")
        self.assertEqual(calls[0]["function"]["arguments"], '{"query":"极限"}')
        self.assertEqual(assistant["tool_calls"], calls)

    def test_chat_stream_drops_tool_call_when_response_is_incomplete(self):
        provider = agent_provider.AgentProviderConfig(
            id="p1", name="测试", base_url="http://127.0.0.1:11434/v1",
            api_key="local", model="demo")
        chunk = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length", delta=SimpleNamespace(
                content="未完成", tool_calls=[SimpleNamespace(
                    index=0, id="call-1", function=SimpleNamespace(
                        name="create_folder", arguments='{"folder":"x"}'))]))])
        stream = SimpleNamespace(__iter__=lambda self: iter([chunk]), close=lambda: None)

        class FakeStream:
            def __iter__(self):
                return iter([chunk])

            def close(self):
                pass

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=mock.Mock(return_value=FakeStream()))))
        with mock.patch.object(agent_orchestrator, "_model_client", return_value=client):
            content, calls, _ = agent_orchestrator._chat_stream_turn(
                provider, [], tools=[], on_delta=lambda _delta: None)
        self.assertEqual(content, "未完成")
        self.assertEqual(calls, [])

    def test_responses_stream_aggregates_fragmented_function_arguments(self):
        provider = agent_provider.AgentProviderConfig(
            id="p1", name="测试", base_url="http://127.0.0.1:11434/v1",
            api_key="local", model="demo", wire_api="responses")
        events = [
            SimpleNamespace(type="response.output_text.delta", delta="正在处理"),
            SimpleNamespace(type="response.output_item.added", output_index=1,
                            item=SimpleNamespace(type="function_call", id="item-1",
                                                 call_id="call-2", name="list_folders",
                                                 arguments="")),
            SimpleNamespace(type="response.function_call_arguments.delta",
                            output_index=1, item_id="item-1", delta="{"),
            SimpleNamespace(type="response.function_call_arguments.delta",
                            output_index=1, item_id="item-1", delta="}"),
            SimpleNamespace(type="response.function_call_arguments.done",
                            output_index=1, item_id="item-1", name="list_folders",
                            arguments="{}"),
            SimpleNamespace(type="response.completed", response=None),
        ]

        class FakeStream:
            def __iter__(self):
                return iter(events)

            def close(self):
                pass

        client = SimpleNamespace(responses=SimpleNamespace(
            create=mock.Mock(return_value=FakeStream())))
        deltas = []
        with mock.patch.object(agent_orchestrator, "_model_client", return_value=client):
            content, calls, _ = agent_orchestrator._responses_stream_turn(
                provider, [{"role": "user", "content": "目录"}], tools=[],
                on_delta=deltas.append)
        self.assertEqual(content, "正在处理")
        self.assertEqual(deltas, ["正在处理"])
        self.assertEqual(calls[0]["id"], "call-2")
        self.assertEqual(calls[0]["function"]["name"], "list_folders")
        self.assertEqual(calls[0]["function"]["arguments"], "{}")

    def test_cancelled_turn_persists_partial_reply_once(self):
        runtime, session = self._runtime()
        provider = agent_provider.AgentProviderConfig(
            id="p1", name="测试", base_url="http://127.0.0.1:11434/v1",
            api_key="local", model="demo")
        control = runtime.start_turn(session["id"])
        try:
            with mock.patch.object(agent_provider, "get", return_value=provider), \
                    mock.patch.object(
                        agent_orchestrator, "_model_turn_stream",
                        side_effect=agent_orchestrator.AgentTurnCancelled("部分回复")):
                with self.assertRaises(agent_orchestrator.AgentTurnCancelled):
                    agent_orchestrator.run_turn(
                        runtime, session["id"], "开始", provider_id="p1",
                        turn_control=control, stream=True)
        finally:
            runtime.finish_turn(control, "stopped")
        messages = runtime.get_session(session["id"])["messages"]
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[-1]["content"], "部分回复")
        self.assertEqual(messages[-1]["status"], "stopped")
        self.assertEqual(messages[-1]["turn_id"], control.id)


if __name__ == "__main__":
    unittest.main()
