"""Agent 对话编排：可选模型工具调用 + 无模型本地降级。

模型永远只能看到 ``agent_tools`` 暴露的函数。这个模块不提供 shell、Python
或任意文件读写入口；写入工具返回待确认计划，最终执行仍由后端权限层负责。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from openai import OpenAI

import agent_provider
import agent_tools
import llm_client


class AgentTurnError(RuntimeError):
    pass


class AgentTurnCancelled(AgentTurnError):
    """用户停止了当前回合；partial 保存已经收到的助手文本。"""

    def __init__(self, partial: str = ""):
        super().__init__("已停止生成")
        self.partial = str(partial or "")


SYSTEM_PROMPT = """你是 QuizForge 题库助手。你只能通过提供的 QuizForge 工具工作；只有提供 execute_command 工具时才能请求执行 PowerShell、CMD 或 Python，不能绕过工具直接访问系统，也不能访问当前题库之外的路径。
你可以只读浏览题库题卡、资料库文件、回收站、图片附件、讲义和识别历史；读取二进制附件时只报告文件元数据，不要假装看到了图片内容。
先完整理解用户的复合目标，再选择最少的只读工具并形成计划。上传试卷后只能先暂存，缺少识别后端、导入方式、规范化方式、目标题库目录、筛选条件或导出格式/模板时，必须明确询问并展示选项，绝不自动启动 OCR、调用 LLM 或导出。机械规范化不调用 LLM；只有用户明确选择 LLM 规范化时才使用模型。写入、删除、移动和入库需要说明变更和影响并等待确认，导出可按权限直接执行。当前会话若标记为仅聊天，不要调用题库工具。
回答使用简体中文，引用题目时给出题目 id 和所在目录；不要编造题库中不存在的内容。"""


def _system_prompt(session: dict) -> str:
    """把后端已确认的会话边界明确交给模型，权限仍由工具层强制执行。"""
    if session.get("scope") == "chat":
        context = "当前会话范围：仅聊天。没有提供任何题库工具，禁止声称已经读取或修改题库。"
    else:
        workdir = str(session.get("workdir_id") or "").strip() or "题库根目录"
        mode = "危险模式（普通写入可直接执行）" if session.get("mode") == "danger" else "标准模式（写入需用户确认）"
        output_dir = str(session.get("output_dir_id") or workdir).strip() or "题库根目录"
        input_dir = str(session.get("input_dir_id") or workdir).strip() or "题库根目录"
        context = f"当前会话范围：题库；材料目录：{input_dir}；题库联动目录：{workdir}；默认导出目录：{output_dir}；权限模式：{mode}。材料目录和题库目录可以不同，但都必须在当前题库根目录内。"
    command_policy = ("命令权限更新：你可以使用 execute_command 执行 PowerShell、CMD 或 Python；命令的工作目录和路径必须留在上述允许目录内。"
                      "标准模式下每一条本机命令都必须等待用户审批；当前页面已显式武装危险模式时可以直接执行。")
    return f"{SYSTEM_PROMPT}\n{context}\n{command_policy}"


def _tool_definitions() -> list[dict]:
    result = []
    for spec in agent_tools.TOOLS:
        result.append({
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "parameters": spec.get("parameters") or {
                    "type": "object", "properties": {}, "additionalProperties": False,
                },
            },
        })
    return result


def _response_tool_definitions() -> list[dict]:
    """Responses API 使用扁平 function schema，与 Chat Completions 不同。"""
    result = []
    for spec in agent_tools.TOOLS:
        result.append({
            "type": "function",
            "name": spec["name"],
            "description": spec.get("description", ""),
            "parameters": spec.get("parameters") or {
                "type": "object", "properties": {}, "additionalProperties": False,
            },
            "strict": False,
        })
    return result


def _message_value(message: Any, key: str, default=None):
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


def _tool_call_to_dict(call: Any) -> dict:
    function = _message_value(call, "function", {}) or {}
    return {
        "id": str(_message_value(call, "id", "call")),
        "type": "function",
        "function": {
            "name": str(_message_value(function, "name", "")),
            "arguments": str(_message_value(function, "arguments", "{}")),
        },
    }


def _model_client(provider: agent_provider.AgentProviderConfig) -> OpenAI:
    # 复用 llm_client 的 DNS/回环安全网络后端；Agent Provider 不绕过既有 SSRF 防护。
    return OpenAI(
        api_key=provider.api_key or "local",
        base_url=provider.base_url,
        http_client=llm_client._safe_http_client(),
    )


def _responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    """把内部统一消息转换为 Responses API 的 input/instructions。"""
    instructions = ""
    items: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            instructions = str(message.get("content") or "")
            continue
        if role in {"user", "assistant"}:
            content = str(message.get("content") or "")
            if content:
                items.append({"role": role, "content": content})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or "call"),
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or "{}"),
                })
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or "call"),
                "output": str(message.get("content") or ""),
            })
    return instructions, items


def _response_output_to_parts(response: Any) -> tuple[str, list[dict]]:
    content = str(_message_value(response, "output_text", "") or "")
    calls: list[dict] = []
    for item in _message_value(response, "output", None) or []:
        item_type = _message_value(item, "type", "")
        if item_type == "function_call":
            calls.append({
                "id": str(_message_value(item, "call_id", None)
                          or _message_value(item, "id", "call")),
                "type": "function",
                "function": {
                    "name": str(_message_value(item, "name", "")),
                    "arguments": str(_message_value(item, "arguments", "{}")),
                },
            })
        elif item_type == "message" and not content:
            for part in _message_value(item, "content", None) or []:
                if _message_value(part, "type", "") in {"output_text", "text"}:
                    content += str(_message_value(part, "text", "") or "")
    return content.strip(), calls


def _model_turn(provider: agent_provider.AgentProviderConfig,
                messages: list[dict], *, tools: list[dict]) -> tuple[str, list[dict], dict]:
    try:
        client = _model_client(provider)
        if provider.wire_api == "responses":
            instructions, response_input = _responses_input(messages)
            kwargs = {
                "model": provider.model,
                "instructions": instructions,
                "input": response_input,
                "tools": _response_tool_definitions() if provider.supports_tools and tools else None,
                "max_output_tokens": provider.max_tokens,
                "store": bool(provider.store_responses),
            }
            if provider.reasoning_effort:
                kwargs["reasoning"] = {"effort": provider.reasoning_effort}
            if provider.service_tier:
                kwargs["service_tier"] = provider.service_tier
            response = client.responses.create(**kwargs)
            content, calls = _response_output_to_parts(response)
            response_status = _message_value(response, "status", None)
            if response_status is not None and str(response_status) != "completed":
                calls = []
            assistant = {"role": "assistant", "content": content}
            if calls:
                assistant["tool_calls"] = calls
            return content, calls, assistant
        response = client.chat.completions.create(
                model=provider.model,
                messages=messages,
                tools=tools if provider.supports_tools and tools else None,
                temperature=0.2,
                max_tokens=provider.max_tokens,
            )
    except Exception as exc:
        detail = " ".join(str(exc).split())
        raise AgentTurnError(
            f"Agent 模型调用失败（{provider.name} / {provider.model}）：{detail[:320]}"
        ) from exc
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise AgentTurnError("Agent 模型没有返回可用回复")
    choice = choices[0]
    message = choice.message
    content = _message_value(message, "content", "") or ""
    raw_calls = _message_value(message, "tool_calls", None) or []
    # 少数 OpenAI 兼容中转仍返回旧版 ``function_call`` 字段。统一成内部
    # tool_calls 结构后继续走同一白名单执行器，避免这类 Provider 看起来像
    # “没有回复”而把用户卡在对话框里。
    if not raw_calls:
        legacy = _message_value(message, "function_call", None)
        if legacy:
            raw_calls = [{"id": "legacy-call", "function": legacy}]
    calls = [_tool_call_to_dict(item) for item in raw_calls]
    finish_reason = _message_value(choice, "finish_reason", None)
    if calls and finish_reason is not None \
            and str(finish_reason) not in {"stop", "tool_calls", "function_call"}:
        calls = []
    assistant = {"role": "assistant", "content": content}
    if calls:
        assistant["tool_calls"] = calls
    return str(content).strip(), calls, assistant


def _check_cancel(control, partial: str = "") -> None:
    if control is not None and control.cancelled:
        raise AgentTurnCancelled(partial)


def _chat_stream_turn(provider: agent_provider.AgentProviderConfig,
                      messages: list[dict], *, tools: list[dict],
                      on_delta: Callable[[str], None], control=None
                      ) -> tuple[str, list[dict], dict]:
    """聚合 Chat Completions 流；工具参数只在完整终态后交给执行层。"""
    content_parts: list[str] = []
    calls_by_index: dict[int, dict] = {}
    finish_reason = None
    stream = None
    try:
        client = _model_client(provider)
        stream = client.chat.completions.create(
            model=provider.model,
            messages=messages,
            tools=tools if provider.supports_tools and tools else None,
            temperature=0.2,
            max_tokens=provider.max_tokens,
            stream=True,
        )
        if control is not None:
            control.bind_closer(getattr(stream, "close", None))
        for chunk in stream:
            _check_cancel(control, "".join(content_parts))
            choices = _message_value(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            reason = _message_value(choice, "finish_reason", None)
            if reason is not None:
                finish_reason = str(reason)
            delta = _message_value(choice, "delta", {}) or {}
            text = _message_value(delta, "content", "") or ""
            if text:
                text = str(text)
                content_parts.append(text)
                on_delta(text)
            for raw_call in _message_value(delta, "tool_calls", None) or []:
                raw_index = _message_value(raw_call, "index", None)
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    call_id = str(_message_value(raw_call, "id", "") or "")
                    existing = next((key for key, value in calls_by_index.items()
                                     if call_id and value.get("id") == call_id), None)
                    if existing is not None:
                        index = existing
                    elif call_id and calls_by_index:
                        index = max(calls_by_index) + 1
                    else:
                        index = max(calls_by_index) if calls_by_index else 0
                call = calls_by_index.setdefault(index, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                call_id = _message_value(raw_call, "id", "") or ""
                if call_id and not call["id"]:
                    call["id"] = str(call_id)
                function = _message_value(raw_call, "function", {}) or {}
                name = _message_value(function, "name", "") or ""
                arguments = _message_value(function, "arguments", "") or ""
                if name:
                    call["function"]["name"] += str(name)
                if arguments:
                    call["function"]["arguments"] += str(arguments)
        _check_cancel(control, "".join(content_parts))
    except AgentTurnCancelled:
        raise
    except Exception as exc:
        _check_cancel(control, "".join(content_parts))
        detail = " ".join(str(exc).split())
        raise AgentTurnError(
            f"Agent 模型调用失败（{provider.name} / {provider.model}）：{detail[:320]}"
        ) from exc
    finally:
        if control is not None:
            control.bind_closer(None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    if finish_reason is None:
        raise AgentTurnError("Agent 流式响应意外中断")
    content = "".join(content_parts).strip()
    calls = [calls_by_index[index] for index in sorted(calls_by_index)]
    for index, call in enumerate(calls):
        if not call["id"]:
            call["id"] = f"stream-call-{index + 1}"
        if not call["function"]["arguments"]:
            call["function"]["arguments"] = "{}"
    # length/content_filter 不是可执行工具的成功终态。保留文本，但丢弃任何
    # 半成品调用，避免截断 JSON 恰好可解析时被误执行。
    if finish_reason not in {"stop", "tool_calls", "function_call"}:
        calls = []
    assistant = {"role": "assistant", "content": content}
    if calls:
        assistant["tool_calls"] = calls
    return content, calls, assistant


def _responses_stream_turn(provider: agent_provider.AgentProviderConfig,
                           messages: list[dict], *, tools: list[dict],
                           on_delta: Callable[[str], None], control=None
                           ) -> tuple[str, list[dict], dict]:
    """聚合 Responses API 流，包括分片 function arguments。"""
    instructions, response_input = _responses_input(messages)
    kwargs = {
        "model": provider.model,
        "instructions": instructions,
        "input": response_input,
        "tools": _response_tool_definitions() if provider.supports_tools and tools else None,
        "max_output_tokens": provider.max_tokens,
        "store": bool(provider.store_responses),
        "stream": True,
    }
    if provider.reasoning_effort:
        kwargs["reasoning"] = {"effort": provider.reasoning_effort}
    if provider.service_tier:
        kwargs["service_tier"] = provider.service_tier

    content_parts: list[str] = []
    calls: dict[str, dict] = {}
    completed_response = None
    completed = False
    stream = None

    def call_key(event, item=None) -> str:
        output_index = _message_value(event, "output_index", None)
        if output_index is not None:
            return f"index:{output_index}"
        return str(_message_value(event, "item_id", "") or
                   _message_value(item, "id", "") or
                   _message_value(item, "call_id", "") or len(calls))

    def merge_item(event, item) -> None:
        if _message_value(item, "type", "") != "function_call":
            return
        key = call_key(event, item)
        row = calls.setdefault(key, {
            "id": "", "type": "function",
            "function": {"name": "", "arguments": ""},
        })
        call_id = _message_value(item, "call_id", None) or _message_value(item, "id", "")
        name = _message_value(item, "name", "") or ""
        arguments = _message_value(item, "arguments", None)
        if call_id:
            row["id"] = str(call_id)
        if name:
            row["function"]["name"] = str(name)
        if arguments is not None:
            row["function"]["arguments"] = str(arguments)

    try:
        client = _model_client(provider)
        stream = client.responses.create(**kwargs)
        if control is not None:
            control.bind_closer(getattr(stream, "close", None))
        for event in stream:
            _check_cancel(control, "".join(content_parts))
            event_type = str(_message_value(event, "type", "") or "")
            if event_type == "response.output_text.delta":
                delta = str(_message_value(event, "delta", "") or "")
                if delta:
                    content_parts.append(delta)
                    on_delta(delta)
            elif event_type in {"response.output_item.added", "response.output_item.done"}:
                merge_item(event, _message_value(event, "item", {}) or {})
            elif event_type == "response.function_call_arguments.delta":
                key = call_key(event)
                row = calls.setdefault(key, {
                    "id": str(_message_value(event, "item_id", "") or ""),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                row["function"]["arguments"] += str(
                    _message_value(event, "delta", "") or "")
            elif event_type == "response.function_call_arguments.done":
                key = call_key(event)
                row = calls.setdefault(key, {
                    "id": str(_message_value(event, "item_id", "") or ""),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                name = _message_value(event, "name", "") or ""
                if name:
                    row["function"]["name"] = str(name)
                row["function"]["arguments"] = str(
                    _message_value(event, "arguments", "") or "{}")
            elif event_type == "response.completed":
                completed = True
                completed_response = _message_value(event, "response", None)
            elif event_type in {"response.failed", "error"}:
                error = _message_value(event, "error", None)
                raise AgentTurnError(str(_message_value(error, "message", "") or
                                         "Agent Responses 流返回失败"))
            elif event_type in {"response.incomplete", "response.cancelled"}:
                completed = False
        _check_cancel(control, "".join(content_parts))
    except (AgentTurnCancelled, AgentTurnError):
        raise
    except Exception as exc:
        _check_cancel(control, "".join(content_parts))
        detail = " ".join(str(exc).split())
        raise AgentTurnError(
            f"Agent 模型调用失败（{provider.name} / {provider.model}）：{detail[:320]}"
        ) from exc
    finally:
        if control is not None:
            control.bind_closer(None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    if not completed:
        raise AgentTurnError("Agent Responses 流未完整结束")
    content = "".join(content_parts).strip()
    final_calls: list[dict]
    if completed_response is not None:
        final_content, final_calls = _response_output_to_parts(completed_response)
        if not content and final_content:
            content = final_content
            on_delta(final_content)
    else:
        final_calls = list(calls.values())
    for index, call in enumerate(final_calls):
        if not call.get("id"):
            call["id"] = f"response-call-{index + 1}"
        function = call.setdefault("function", {})
        if not function.get("arguments"):
            function["arguments"] = "{}"
    assistant = {"role": "assistant", "content": content}
    if final_calls:
        assistant["tool_calls"] = final_calls
    return content, final_calls, assistant


def _model_turn_stream(provider: agent_provider.AgentProviderConfig,
                       messages: list[dict], *, tools: list[dict],
                       on_delta: Callable[[str], None], control=None
                       ) -> tuple[str, list[dict], dict]:
    if provider.wire_api == "responses":
        return _responses_stream_turn(
            provider, messages, tools=tools, on_delta=on_delta, control=control)
    return _chat_stream_turn(
        provider, messages, tools=tools, on_delta=on_delta, control=control)


def _local_reply(content: str, *, session: dict) -> tuple[str, list[dict]]:
    """没有配置 Agent 模型时提供可用的本地题库快捷交互。"""
    events: list[dict] = []
    text = str(content or "").strip()
    if session.get("scope") == "chat":
        return (
            "当前是“仅聊天”模式，我不会读取题库或本地文件。配置 Agent 模型后，"
            "这里可以继续普通对话；切换到“当前题库”即可搜索和读取题目。"
        ), events
    if re.search(r"(目录|文件夹|题库结构|题集|有哪些分类)", text):
        result = agent_tools.dispatch("list_folders", session=session)
        lines = _folder_lines(result.get("folders") or [])
        events.append({"type": "tool", "name": "list_folders", "status": "done"})
        return "当前工作目录下的题库结构：\n" + "\n".join(lines), events

    search = re.match(r"^(?:搜索|查找|搜题|找一下|帮我找)(?:题目|题卡)?[:：\s]*(.*)$", text)
    if search:
        query = search.group(1).strip()
        if not query:
            return "请补充要搜索的关键词，例如“搜索 微积分”。", events
        result = agent_tools.dispatch("search_questions", {"query": query}, session=session)
        events.append({"type": "tool", "name": "search_questions", "status": "done",
                       "count": result.get("total", 0)})
        rows = result.get("questions") or []
        reply = f"找到 {result.get('total', 0)} 道题：\n" + "\n".join(
            f"- {row.get('name') or row.get('title') or row.get('id')}（{row.get('id')}）"
            for row in rows)
        if not rows:
            reply += "\n暂时没有匹配结果。"
        elif result.get("total", 0) > len(rows):
            reply += f"\n仅显示前 {len(rows)} 道，可继续缩小关键词。"
        return reply, events

    match = re.search(r"(?:读取|查看|打开|展示)\s*(?:题目|题卡)?\s*([A-Za-z0-9][A-Za-z0-9_-]{1,80})", text)
    if match:
        result = agent_tools.dispatch("read_question", {"id": match.group(1)}, session=session)
        question = result["question"]
        events.append({"type": "tool", "name": "read_question", "status": "done"})
        body = question.get("body", question.get("content", ""))
        return (f"题目 {question.get('id', match.group(1))}\n"
                f"目录：{question.get('folder', '') or '题库根目录'}\n"
                f"题型：{question.get('type', '')}\n题干：{body}"), events
    return (
        "我可以协助搜索和读取当前题库。试试“列出目录”“搜索 极限”或“读取题目 <id>”。"
        "上传试卷后，我会把识别任务放到后台并显示进度。"
    ), events


def _folder_lines(nodes: list[dict], prefix: str = "") -> list[str]:
    lines: list[str] = []
    for node in nodes:
        name = node.get("name") or node.get("id") or "未命名目录"
        lines.append(f"{prefix}{name}")
        lines.extend(_folder_lines(node.get("children") or [], prefix + "  "))
    return lines or ["（当前目录暂无子目录）"]


def _is_lightweight_request(text: str) -> bool:
    """识别不需要访问题库的单轮请求，避免模型无意义地调用工具。"""
    value = str(text or "").strip().lower()
    if not value:
        return False
    operation_words = (
        "题库", "题目", "题卡", "目录", "文件", "pdf", "docx", "zip", "导入", "导出",
        "标签", "查重", "回收站", "资料库", "附件", "ocr", "mineru", "doc2x", "模板",
        "读取", "写入", "删除", "移动", "创建目录", "批量",
    )
    if any(word in value for word in operation_words):
        return False
    lightweight_words = (
        "改写", "润色", "重写", "解释", "翻译", "总结", "概括", "优化表达", "代码建议",
        "怎么写", "如何写", "帮我写", "生成一段", "提示词", "什么是", "为什么",
    )
    return any(word in value for word in lightweight_words)


def run_turn(runtime, sid: str, content: str, *, provider_id: str | None = None,
             approval_store=None,
             on_event: Callable[[dict], None] | None = None,
             turn_control=None, stream: bool = False,
             session_override: dict | None = None) -> tuple[str, list[dict]]:
    """处理一轮消息，返回最终回复和过程事件。"""
    base_session = runtime.get_session(sid)
    permission_mode = str((session_override or {}).get("mode") or "standard")
    session = dict(base_session)
    session["mode"] = "danger" if permission_mode == "danger" else "standard"
    text = str(content or "").strip()
    if not text:
        raise AgentTurnError("消息内容不能为空")
    turn_id = getattr(turn_control, "id", None)
    runtime.append(sid, "user", text, turn_id=turn_id)
    # append 会更新时间和消息列表，重新取快照后再覆盖短期权限模式。
    session = dict(runtime.get_session(sid))
    session["mode"] = "danger" if permission_mode == "danger" else "standard"
    events: list[dict] = []
    streamed_parts: list[str] = []

    def emit(event: dict) -> None:
        events.append(event)
        if on_event:
            on_event(event)

    def emit_delta(delta: str) -> None:
        if not delta:
            return
        streamed_parts.append(delta)
        emit({"type": "assistant_delta", "delta": delta})

    reply = ""
    try:
        _check_cancel(turn_control)
        try:
            if provider_id:
                provider = agent_provider.get(provider_id)
                if provider is None:
                    raise AgentTurnError("指定的 Agent Provider 不存在或已停用")
            else:
                provider = agent_provider.active()
        except agent_provider.AgentProviderError as exc:
            # Provider 配置损坏或密钥无法解密属于可预期的配置错误；统一转成
            # AgentTurnError，由 HTTP 层返回 400，而不是让 Flask 产生 500 页面。
            raise AgentTurnError(str(exc)) from exc
        _check_cancel(turn_control)
        if provider is None:
            reply, local_events = _local_reply(text, session=session)
            _check_cancel(turn_control, reply)
            for event in local_events:
                mapped = dict(event)
                if mapped.get("type") == "tool":
                    mapped["type"] = "tool_state"
                emit(mapped)
            if stream and reply:
                emit_delta(reply)
        else:
            history = []
            for message in (session.get("messages") or [])[-30:]:
                role = message.get("role")
                if role not in {"user", "assistant"}:
                    continue
                value = str(message.get("content") or "")
                if value:
                    history.append({"role": role, "content": value[-12000:]})
            messages = [{"role": "system", "content": _system_prompt(session)}] + history
            lightweight = _is_lightweight_request(text)
            tools = [] if session.get("scope") == "chat" or lightweight else _tool_definitions()
            max_rounds = 1 if lightweight else 6
            for _round in range(max_rounds):
                _check_cancel(turn_control, "".join(streamed_parts))
                pending_confirmation = False
                if stream:
                    content_part, calls, assistant = _model_turn_stream(
                        provider, messages, tools=tools,
                        on_delta=emit_delta, control=turn_control)
                else:
                    content_part, calls, assistant = _model_turn(
                        provider, messages, tools=tools)
                _check_cancel(turn_control, content_part or "".join(streamed_parts))
                if calls:
                    messages.append(assistant)
                    for call in calls:
                        _check_cancel(turn_control, content_part or "".join(streamed_parts))
                        name = call["function"]["name"]
                        raw_args = call["function"].get("arguments") or "{}"
                        try:
                            arguments = json.loads(raw_args)
                            if not isinstance(arguments, dict):
                                raise ValueError("arguments 不是对象")
                        except (TypeError, ValueError, json.JSONDecodeError) as exc:
                            result = {"ok": False, "error": f"工具参数无法解析：{exc}"}
                        else:
                            emit({"type": "tool_state", "name": name, "status": "running"})
                            _check_cancel(turn_control, content_part or "".join(streamed_parts))
                            try:
                                result = agent_tools.dispatch(
                                    name, arguments, session=session,
                                    approval_store=approval_store)
                                if not isinstance(result, dict):
                                    result = {"ok": False, "error": "工具返回了无效结果"}
                            except Exception as exc:
                                # 工具是业务边界；即使底层题库扫描或第三方适配器
                                # 抛出未预期异常，也只把错误交回模型，不应击穿 HTTP。
                                detail = " ".join(str(exc).split())[:320]
                                result = {"ok": False, "error": f"工具执行失败：{detail}"}
                            if result.get("pending_confirmation"):
                                approval = result.get("approval") or result.get("plan") or {}
                                emit({"type": "approval", "name": name,
                                      "status": "pending", "approval": approval})
                                emit({"type": "tool_state", "name": name,
                                      "status": "awaiting_confirmation"})
                                reply = str(result.get("message") or
                                            "写入操作已生成预览，请在对话框中确认后执行。")
                                if approval.get("summary"):
                                    reply += f"\n\n{approval['summary']}"
                                if stream and reply:
                                    emit_delta(reply)
                                pending_confirmation = True
                            else:
                                emit({"type": "tool_state", "name": name,
                                      "status": "done" if result.get("ok", True) else "error"})
                                pending_confirmation = False
                        messages.append({
                            "role": "tool", "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False)[:16000],
                        })
                        if pending_confirmation:
                            break
                    if pending_confirmation:
                        break
                    _check_cancel(turn_control, content_part or "".join(streamed_parts))
                    continue
                reply = content_part or "模型没有生成文字回复。"
                if not stream and reply:
                    # JSON 端点仍返回完整文本，不需要产生 delta 事件。
                    pass
                break
            else:
                reply = "工具调用轮次超过上限，请把任务拆成更小的步骤后重试。"
                if stream:
                    emit_delta(reply)
        _check_cancel(turn_control, reply or "".join(streamed_parts))
    except AgentTurnCancelled as exc:
        partial = exc.partial or "".join(streamed_parts) or reply
        runtime.append(sid, "assistant", partial, status="stopped", turn_id=turn_id)
        raise AgentTurnCancelled(partial) from exc

    runtime.append(sid, "assistant", reply, turn_id=turn_id)
    return reply, events


def test_provider(provider: agent_provider.AgentProviderConfig) -> str:
    """发送极小探测请求，返回模型的简短文字；不会把凭据带回调用方。"""
    try:
        client = _model_client(provider)
        if provider.wire_api == "responses":
            response = client.responses.create(
                model=provider.model, input="只回复：连接成功",
                max_output_tokens=64, store=False,
            )
            return str(_message_value(response, "output_text", "") or "连接成功").strip()
        response = client.chat.completions.create(
                model=provider.model,
                messages=[{"role": "user", "content": "只回复：连接成功"}],
                temperature=0,
                max_tokens=64,
            )
    except Exception as exc:
        detail = " ".join(str(exc).split())
        raise AgentTurnError(f"连接测试失败：{detail[:320]}") from exc
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise AgentTurnError("连接测试没有返回内容")
    return str(_message_value(choices[0].message, "content", "") or "").strip() or "连接成功"
