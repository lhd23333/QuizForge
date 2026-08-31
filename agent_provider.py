"""Agent 对话模型 Provider。

Agent 的对话模型与 OCR/配图模型分开配置，但复用同一套 Fernet 加密和
OpenAI 兼容的 Base URL 校验。模块只返回脱敏后的公开信息，API Key 不进入
会话、日志或前端响应。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import tomllib
import uuid

import config
import crypto_utils
import llm_client

logger = logging.getLogger(__name__)
_lock = threading.RLock()


class AgentProviderError(ValueError):
    """Provider 配置或调用参数无效。"""


@dataclasses.dataclass(frozen=True)
class AgentProviderConfig:
    id: str
    name: str
    base_url: str
    api_key: str
    model: str
    max_tokens: int = 8192
    enabled: bool = True
    supports_tools: bool = True
    supports_vision: bool = False
    wire_api: str = "chat"
    reasoning_effort: str | None = None
    service_tier: str | None = None
    store_responses: bool = False


# Agent 当前统一走 OpenAI Chat Completions 兼容协议。预设只用于帮用户
# 填好常见服务的地址和模型名，不会写入 API Key，也不会自动启用或联网。
# 原生 Anthropic/Google 协议暂未在 Agent 编排器中实现；需要使用它们时，
# 请填写服务商提供的 OpenAI 兼容端点或中转站地址。
AGENT_PROVIDER_PRESETS = (
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": (
            {"id": "deepseek-chat", "label": "DeepSeek Chat", "max_tokens": 32768},
            {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner", "max_tokens": 32768},
        ),
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": (
            {"id": "gpt-4o-mini", "label": "GPT-4o mini", "max_tokens": 16384},
            {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini", "max_tokens": 32768},
        ),
    },
    {
        "id": "qwen",
        "label": "阿里云百炼（Qwen）",
        "name": "Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": (
            {"id": "qwen-plus", "label": "Qwen Plus", "max_tokens": 32768},
            {"id": "qwen-turbo", "label": "Qwen Turbo", "max_tokens": 32768},
            {"id": "qwen3-vl-plus", "label": "Qwen3 VL Plus（支持图片）", "max_tokens": 16384,
             "supports_vision": True},
        ),
    },
    {
        "id": "openrouter",
        "label": "OpenRouter（中转）",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": (
            {"id": "openai/gpt-4o-mini", "label": "OpenAI GPT-4o mini", "max_tokens": 16384},
            {"id": "deepseek/deepseek-chat", "label": "DeepSeek Chat", "max_tokens": 32768},
        ),
    },
    {
        "id": "ollama",
        "label": "Ollama（本机）",
        "name": "Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": (
            {"id": "llama3.2", "label": "llama3.2", "max_tokens": 8192},
            {"id": "qwen2.5:7b", "label": "qwen2.5:7b", "max_tokens": 8192},
        ),
    },
    {
        "id": "lmstudio",
        "label": "LM Studio（本机）",
        "name": "LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "models": (
            {"id": "local-model", "label": "当前加载的模型", "max_tokens": 8192},
        ),
    },
)


def list_presets() -> list[dict]:
    """返回可安全交给前端的 Provider 预设，不包含任何凭据。"""
    # 深拷贝成普通 JSON 结构，避免调用方修改模块级 tuple 中的元数据。
    return [
        {
            "id": str(preset["id"]),
            "label": str(preset["label"]),
            "name": str(preset["name"]),
            "base_url": str(preset["base_url"]),
            "models": [dict(model) for model in preset.get("models", ())],
        }
        for preset in AGENT_PROVIDER_PRESETS
    ]


def _empty() -> dict:
    return {"active": None, "providers": []}


def _load() -> dict:
    path = config.AGENT_PROVIDERS_PATH
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("agent_providers.json 解析失败，视为空配置")
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    providers = data.get("providers")
    if not isinstance(providers, list):
        providers = []
    return {"active": data.get("active"), "providers": providers}


def _save(data: dict) -> None:
    path = config.AGENT_PROVIDERS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _normalise_input(name: str, base_url: str, model: str,
                     max_tokens, wire_api: str = "chat",
                     reasoning_effort: str | None = None,
                     service_tier: str | None = None) -> tuple[str, str, str, int, str, str | None, str | None]:
    name = str(name or "").strip()
    base_url = str(base_url or "").strip()
    model = str(model or "").strip()
    if not name or not base_url or not model:
        raise AgentProviderError("Provider 名称、Base URL 和模型名都不能为空")
    try:
        max_tokens = llm_client.clamp_max_tokens(max_tokens)
    except Exception as exc:  # 防止第三方实现抛出非标准异常
        raise AgentProviderError("最大输出 tokens 无效") from exc
    try:
        base_url = llm_client.normalize_base_url(base_url)
        base_url = llm_client.validate_base_url(base_url)
    except Exception as exc:
        raise AgentProviderError(str(exc)) from exc
    wire_api = str(wire_api or "chat").strip().lower()
    if wire_api == "auto":
        wire_api = "chat"
    if wire_api not in {"chat", "responses"}:
        raise AgentProviderError("接口协议只能选择 Chat Completions 或 Responses")
    reasoning_effort = str(reasoning_effort or "").strip().lower() or None
    if reasoning_effort not in {None, "minimal", "low", "medium", "high", "xhigh"}:
        raise AgentProviderError("推理强度无效")
    service_tier = str(service_tier or "").strip().lower() or None
    if service_tier not in {None, "auto", "default", "flex", "scale", "priority", "fast"}:
        raise AgentProviderError("服务等级无效")
    return name, base_url, model, max_tokens, wire_api, reasoning_effort, service_tier


def list_public() -> list[dict]:
    """列出脱敏配置，绝不返回 ``api_key_enc``。"""
    data = _load()
    active = data.get("active")
    result = []
    for raw in data["providers"]:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        result.append({
            "id": str(raw["id"]),
            "name": str(raw.get("name") or raw["id"]),
            "base_url": str(raw.get("base_url") or ""),
            "model": str(raw.get("model") or ""),
            "max_tokens": int(raw.get("max_tokens") or 8192),
            "enabled": bool(raw.get("enabled", True)),
            "supports_tools": bool(raw.get("supports_tools", True)),
            "supports_vision": bool(raw.get("supports_vision", False)),
            "wire_api": str(raw.get("wire_api") or "chat"),
            "reasoning_effort": str(raw.get("reasoning_effort") or "") or None,
            "service_tier": str(raw.get("service_tier") or "") or None,
            "store_responses": bool(raw.get("store_responses", False)),
            "key_configured": bool(raw.get("api_key_enc")),
            "active": str(raw["id"]) == str(active) if active else False,
        })
    return result


def _find(data: dict, provider_id: str) -> dict | None:
    return next((row for row in data["providers"]
                 if isinstance(row, dict) and str(row.get("id")) == str(provider_id)), None)


def get(provider_id: str) -> AgentProviderConfig | None:
    """解密并返回指定 Provider；调用方不得把结果写日志或响应。"""
    data = _load()
    row = _find(data, provider_id)
    if row is None or not row.get("enabled", True):
        return None
    enc = str(row.get("api_key_enc") or "")
    try:
        key = crypto_utils.decrypt_token(enc) if enc else "local"
    except crypto_utils.CryptoError as exc:
        raise AgentProviderError("Agent Provider 的 API Key 无法解密，请重新保存") from exc
    return AgentProviderConfig(
        id=str(row["id"]), name=str(row.get("name") or row["id"]),
        base_url=str(row.get("base_url") or ""), api_key=key,
        model=str(row.get("model") or ""),
        max_tokens=llm_client.clamp_max_tokens(row.get("max_tokens")),
        enabled=bool(row.get("enabled", True)),
        supports_tools=bool(row.get("supports_tools", True)),
        supports_vision=bool(row.get("supports_vision", False)),
        wire_api=str(row.get("wire_api") or "chat"),
        reasoning_effort=str(row.get("reasoning_effort") or "") or None,
        service_tier=str(row.get("service_tier") or "") or None,
        store_responses=bool(row.get("store_responses", False)),
    )


def active() -> AgentProviderConfig | None:
    data = _load()
    provider_id = data.get("active")
    return get(provider_id) if provider_id else None


def create(*, name: str, base_url: str, api_key: str, model: str,
           max_tokens=8192, enabled: bool = True,
           supports_tools: bool = True, supports_vision: bool = False,
           wire_api: str = "chat", reasoning_effort: str | None = None,
           service_tier: str | None = None,
           store_responses: bool = False) -> str:
    name, base_url, model, max_tokens, wire_api, reasoning_effort, service_tier = _normalise_input(
        name, base_url, model, max_tokens, wire_api, reasoning_effort, service_tier)
    api_key = str(api_key or "").strip()
    # 只有回环服务允许无 key；公网 endpoint 没有 key 时大概率是误配，
    # 直接提示用户，避免首次对话才收到难懂的 401。
    if not api_key and not base_url.lower().startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        raise AgentProviderError("公网 Provider 必须填写 API Key；本地模型可留空")
    try:
        encrypted = crypto_utils.encrypt_token(api_key) if api_key else ""
    except crypto_utils.CryptoError as exc:
        raise AgentProviderError(str(exc)) from exc
    provider_id = uuid.uuid4().hex[:12]
    with _lock:
        data = _load()
        data["providers"].append({
            "id": provider_id, "name": name, "base_url": base_url,
            "api_key_enc": encrypted, "model": model,
            "max_tokens": max_tokens, "enabled": bool(enabled),
            "supports_tools": bool(supports_tools),
            "supports_vision": bool(supports_vision),
            "wire_api": wire_api,
            "reasoning_effort": reasoning_effort,
            "service_tier": service_tier,
            "store_responses": bool(store_responses),
        })
        # 禁用的 Provider 不能成为全局活动项，否则 ``active()`` 会立即
        # 返回 None，而设置页却把它标成“全局默认”，用户下一次对话会在
        # 没有提示的情况下退回本地快捷模式。
        if bool(enabled) and not data.get("active"):
            data["active"] = provider_id
        _save(data)
    return provider_id


def update(provider_id: str, *, name: str | None = None,
           base_url: str | None = None, model: str | None = None,
           max_tokens=None, api_key: str | None = None,
           enabled: bool | None = None, supports_tools: bool | None = None,
           supports_vision: bool | None = None, wire_api: str | None = None,
           reasoning_effort: str | None = None, service_tier: str | None = None,
           store_responses: bool | None = None) -> bool:
    with _lock:
        data = _load()
        row = _find(data, provider_id)
        if row is None:
            return False

        # PATCH 允许只改一项；先从现有记录补齐其余字段，再统一校验，避免
        # 前端编辑开关时因为缺少名称/模型而得到一个误导性的“不能为空”。
        next_name = row.get("name") if name is None else name
        next_base_url = row.get("base_url") if base_url is None else base_url
        next_model = row.get("model") if model is None else model
        next_max_tokens = (row.get("max_tokens", 8192)
                           if max_tokens is None else max_tokens)
        next_wire_api = row.get("wire_api", "chat") if wire_api is None else wire_api
        next_reasoning = row.get("reasoning_effort") if reasoning_effort is None else reasoning_effort
        next_tier = row.get("service_tier") if service_tier is None else service_tier
        (next_name, next_base_url, next_model, next_max_tokens, next_wire_api,
         next_reasoning, next_tier) = _normalise_input(
            next_name, next_base_url, next_model, next_max_tokens,
            next_wire_api, next_reasoning, next_tier)

        # 空 API Key 的 PATCH 保持原有密文（便于表单不回显密钥）；如果把
        # endpoint 改成公网，必须确认记录确实有密钥，否则拒绝整个更新，
        # 避免保存一个必然 401 的 Provider。
        next_key_enc = str(row.get("api_key_enc") or "")
        if api_key is not None and str(api_key).strip():
            try:
                next_key_enc = crypto_utils.encrypt_token(str(api_key).strip())
            except crypto_utils.CryptoError as exc:
                raise AgentProviderError(str(exc)) from exc
        if (not next_key_enc and
                not next_base_url.lower().startswith((
                    "http://127.0.0.1", "http://localhost", "http://[::1]"))):
            raise AgentProviderError("公网 Provider 必须填写 API Key；本地模型可留空")

        row.update(name=next_name, base_url=next_base_url, model=next_model,
                   max_tokens=next_max_tokens, api_key_enc=next_key_enc,
                   wire_api=next_wire_api, reasoning_effort=next_reasoning,
                   service_tier=next_tier)
        if enabled is not None:
            row["enabled"] = bool(enabled)
        if supports_tools is not None:
            row["supports_tools"] = bool(supports_tools)
        if supports_vision is not None:
            row["supports_vision"] = bool(supports_vision)
        if store_responses is not None:
            row["store_responses"] = bool(store_responses)
        if data.get("active") == provider_id and not row.get("enabled", True):
            data["active"] = None
        elif data.get("active") is None and row.get("enabled", True):
            # 之前唯一的 Provider 可能被禁用；重新启用后可恢复为默认项，
            # 但不会覆盖用户已经选择的其他 active Provider。
            data["active"] = provider_id
        _save(data)
    return True


def set_active(provider_id: str | None) -> bool:
    with _lock:
        data = _load()
        if provider_id is not None:
            row = _find(data, provider_id)
            if row is None or not row.get("enabled", True):
                return False
        data["active"] = provider_id
        _save(data)
    return True


def remove(provider_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["providers"])
        data["providers"] = [row for row in data["providers"]
                              if str(row.get("id")) != str(provider_id)]
        if len(data["providers"]) == before:
            return False
        if str(data.get("active")) == str(provider_id):
            data["active"] = None
        _save(data)
    return True


def describe_active() -> dict | None:
    """供 UI 显示当前模型；只返回公开元数据。"""
    return next((row for row in list_public() if row.get("active")), None)


def import_cc_switch(*, config_text: str, auth_text: str = "",
                     name: str | None = None) -> str:
    """从用户主动选择的 CC Switch config.toml/auth.json 导入一个 Provider。"""
    try:
        config_data = tomllib.loads(str(config_text or ""))
    except (tomllib.TOMLDecodeError, TypeError) as exc:
        raise AgentProviderError(f"CC Switch config.toml 无法解析：{exc}") from exc
    try:
        auth_data = json.loads(str(auth_text or "{}"))
    except (ValueError, TypeError) as exc:
        raise AgentProviderError(f"CC Switch auth.json 无法解析：{exc}") from exc
    if not isinstance(auth_data, dict):
        raise AgentProviderError("CC Switch auth.json 必须是 JSON 对象")
    provider_id = str(config_data.get("model_provider") or "custom")
    providers = config_data.get("model_providers") or {}
    section = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(section, dict):
        section = providers.get("custom") if isinstance(providers, dict) else None
    section = section if isinstance(section, dict) else {}
    base_url = section.get("base_url") or config_data.get("base_url")
    model = config_data.get("model") or section.get("model")
    if not base_url or not model:
        raise AgentProviderError("CC Switch 配置缺少 base_url 或 model")
    token = section.get("experimental_bearer_token")
    if not token:
        token = auth_data.get("OPENAI_API_KEY")
    if not token:
        raise AgentProviderError("CC Switch 配置中没有 OPENAI_API_KEY 或 Bearer Token")
    wire_api = section.get("wire_api") or "chat"
    if str(wire_api).lower() == "responses":
        wire_api = "responses"
    else:
        wire_api = "chat"
    return create(
        name=name or section.get("name") or provider_id,
        base_url=base_url, api_key=token, model=model,
        max_tokens=config_data.get("max_output_tokens") or 16384,
        supports_tools=True, wire_api=wire_api,
        reasoning_effort=config_data.get("model_reasoning_effort"),
        service_tier=config_data.get("service_tier"),
        store_responses=not bool(section.get("disable_response_storage", False)),
    )
