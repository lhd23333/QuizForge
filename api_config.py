"""统一 API 配置层。

所有外部 API（agent 对话、题目处理 LLM、配图重绘、OCR）的统一管理面。
底层存储沿用现有四套文件（providers.json / agent_providers.json /
mineru.json / doc2x.json），本模块只做聚合视图与统一读写分发——
不迁移数据、不复制凭据、不改变任何既有解析路径。

统一的是**管理方式，不是配置值**：每个条目独立配置协议、端点、凭据与
模型，互不绑定；仅界面、存储、安全与 agent 工具接口共用。

明文凭据只在本模块内存里短暂出现（来自"安全通道"调用方——GUI 表单或
CLI 隐藏输入），永远不写入清单、日志或模型上下文；agent 工具层不暴露
明文参数。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import agent_provider
import crypto_utils
import doc2x_store
import mineru_store
import providers

logger = logging.getLogger(__name__)

#: magpie 本机网关默认地址；transport 按该地址特征推断。
DEFAULT_MAGPIE_BASE_URL = "http://127.0.0.1:3425/v1"
MAGPIE_PORT = 3425

PURPOSES = ("agent", "md", "redraw", "ocr-mineru", "ocr-doc2x")
LLM_PURPOSES = ("md", "redraw")
OCR_PURPOSES = ("ocr-mineru", "ocr-doc2x")

_LOOPBACK_PREFIXES = ("http://127.0.0.1", "http://localhost", "http://[::1]")


class ApiConfigError(ValueError):
    """API 配置参数或状态无效。"""


def _transport_of(base_url: str) -> str:
    text = str(base_url or "").lower()
    if f"127.0.0.1:{MAGPIE_PORT}" in text or f"localhost:{MAGPIE_PORT}" in text:
        return "magpie"
    return "direct"


def _is_loopback(base_url: str) -> bool:
    return str(base_url or "").lower().startswith(_LOOPBACK_PREFIXES)


def _split(cid: str) -> tuple[str, str]:
    text = str(cid or "").strip()
    if ":" not in text:
        raise ApiConfigError(
            "配置 id 形如 llm:<id> / agent:<id> / mineru:<id> / doc2x:<id>")
    prefix, raw = text.split(":", 1)
    if prefix not in {"llm", "agent", "mineru", "doc2x"} or not raw:
        raise ApiConfigError("配置 id 前缀无效")
    return prefix, raw


def list_api_configs() -> list[dict]:
    """聚合五类用途的全部条目（统一视图，全部脱敏）。"""
    rows: list[dict] = []
    for item in providers.list_llm_providers():
        purposes = []
        if item.get("active_md"):
            purposes.append("md")
        if item.get("active_redraw"):
            purposes.append("redraw")
        rows.append({
            "id": f"llm:{item['id']}",
            "source": "llm",
            "kind": "llm",
            "name": str(item.get("name") or item["id"]),
            "purposes": purposes,
            "available_purposes": list(LLM_PURPOSES),
            "active": bool(purposes),
            "protocol": "openai-chat",
            "transport": _transport_of(item.get("base_url", "")),
            "base_url": str(item.get("base_url") or ""),
            "model": str(item.get("model") or ""),
            "max_tokens": int(item.get("max_tokens") or 8192),
            "supports_vision": bool(item.get("supports_vision")),
            "secret_state": "stored" if item.get("api_key_enc") else "missing",
        })
    for item in agent_provider.list_public():
        rows.append({
            "id": f"agent:{item['id']}",
            "source": "agent",
            "kind": "llm",
            "name": str(item.get("name") or item["id"]),
            "purposes": ["agent"] if item.get("active") else [],
            "available_purposes": ["agent"],
            "active": bool(item.get("active")),
            "protocol": ("openai-responses"
                         if item.get("wire_api") == "responses"
                         else "openai-chat"),
            "transport": _transport_of(item.get("base_url", "")),
            "base_url": str(item.get("base_url") or ""),
            "model": str(item.get("model") or ""),
            "max_tokens": int(item.get("max_tokens") or 8192),
            "supports_vision": bool(item.get("supports_vision")),
            "secret_state": ("stored" if item.get("key_configured")
                             else "not-required"),
            "enabled": bool(item.get("enabled", True)),
        })
    for token in mineru_store.list_tokens():
        raw = str(token.get("id") or "")
        rows.append({
            "id": f"mineru:{raw}",
            "source": "mineru",
            "kind": "ocr",
            "name": str(token.get("label") or f"MinerU Token {raw[:6]}"),
            "purposes": ["ocr-mineru"],
            "available_purposes": ["ocr-mineru"],
            "active": True,
            "protocol": "mineru",
            "transport": "direct",
            "base_url": "https://mineru.net",
            "model": "auto",
            "secret_state": "stored",
            "added": str(token.get("added") or ""),
        })
    for key in doc2x_store.list_keys():
        raw = str(key.get("id") or "")
        rows.append({
            "id": f"doc2x:{raw}",
            "source": "doc2x",
            "kind": "ocr",
            "name": str(key.get("label") or f"Doc2X Key {raw[:6]}"),
            "purposes": ["ocr-doc2x"],
            "available_purposes": ["ocr-doc2x"],
            "active": True,
            "protocol": "doc2x",
            "transport": "direct",
            "base_url": "https://doc2x.noedgeai.com",
            "model": "auto",
            "secret_state": "stored",
            "added": str(key.get("added") or ""),
        })
    return rows


def get_api_config(cid: str) -> dict | None:
    """按统一 id 取单条（脱敏）。"""
    target = str(cid or "").strip()
    return next((row for row in list_api_configs() if row["id"] == target), None)


def upsert_api_config(*, cid: str | None = None, name: str = "",
                      purpose: str = "", base_url: str = "", model: str = "",
                      max_tokens: int | None = None,
                      supports_vision: bool | None = None,
                      wire_api: str | None = None,
                      secret: str | None = None,
                      label: str = "") -> dict:
    """新增或修改一条配置，按来源分发到底层存储。

    ``secret`` 由安全通道调用方在本地进程内直接传入；agent 工具层调用本
    函数时不会携带明文（见 agent_tools 的参数白名单）。
    """
    if cid:
        source, raw = _split(cid)
        if source == "llm":
            existing = providers.get_llm_provider(raw)
            if existing is None:
                raise ApiConfigError("配置不存在")
            enc = crypto_utils.encrypt_token(secret) if secret else None
            ok = providers.update_llm_provider(
                raw,
                name=str(name or existing.get("name") or ""),
                base_url=str(base_url or existing.get("base_url") or ""),
                model=str(model or existing.get("model") or ""),
                max_tokens=int(max_tokens if max_tokens is not None
                               else existing.get("max_tokens") or 8192),
                api_key_enc=enc,
                supports_vision=supports_vision)
            if not ok:
                raise ApiConfigError("配置更新失败")
            return {"id": cid, "created": False}
        if source == "agent":
            try:
                ok = agent_provider.update(
                    raw, name=name or None, base_url=base_url or None,
                    model=model or None, max_tokens=max_tokens,
                    api_key=secret, supports_vision=supports_vision,
                    wire_api=wire_api)
            except agent_provider.AgentProviderError as exc:
                raise ApiConfigError(str(exc)) from exc
            if not ok:
                raise ApiConfigError("配置不存在或更新失败")
            return {"id": cid, "created": False}
        raise ApiConfigError(
            "OCR 凭据不支持就地编辑；请删除后重新添加，或在设置页替换")

    p = str(purpose or "").strip()
    if p == "agent":
        try:
            pid = agent_provider.create(
                name=str(name or base_url or "未命名"), base_url=str(base_url or ""),
                api_key=str(secret or ""), model=str(model or ""),
                max_tokens=int(max_tokens or 8192),
                supports_vision=bool(supports_vision),
                wire_api=str(wire_api or "chat"))
        except agent_provider.AgentProviderError as exc:
            raise ApiConfigError(str(exc)) from exc
        return {"id": f"agent:{pid}", "created": True}
    if p in LLM_PURPOSES:
        base = str(base_url or "")
        key = str(secret or "")
        if not key and _is_loopback(base):
            # 本机端点（magpie / Ollama 等）不校验 Key，但 LLM 解析链要求
            # 密文非空；存一个占位值，功能上等价于无凭据。
            key = "local" if _transport_of(base) != "magpie" else "magpie"
        if not key:
            raise ApiConfigError(
                "该端点需要 API Key；请通过安全输入通道提供后再保存")
        pid = providers.add_llm_provider(
            str(name or base), base, crypto_utils.encrypt_token(key),
            str(model or ""), int(max_tokens or 8192), purposes=(p,),
            supports_vision=bool(supports_vision))
        return {"id": f"llm:{pid}", "created": True}
    if p == "ocr-mineru":
        if not secret:
            raise ApiConfigError("MinerU Token 不能为空")
        mineru_store.add_token(str(secret), label=str(label or name or ""))
        tokens = mineru_store.list_tokens()
        raw = str(tokens[-1].get("id") or "") if tokens else ""
        return {"id": f"mineru:{raw}", "created": True}
    if p == "ocr-doc2x":
        if not secret:
            raise ApiConfigError("Doc2X Key 不能为空")
        doc2x_store.add_key(str(secret), label=str(label or name or ""))
        keys = doc2x_store.list_keys()
        raw = str(keys[-1].get("id") or "") if keys else ""
        return {"id": f"doc2x:{raw}", "created": True}
    raise ApiConfigError(
        "purpose 必须是 agent / md / redraw / ocr-mineru / ocr-doc2x")


def set_active_api_config(cid: str, *, purpose: str = "") -> dict:
    """把某条配置设为指定用途的当前生效项。"""
    source, raw = _split(cid)
    if source == "agent":
        if not agent_provider.set_active(raw):
            raise ApiConfigError("配置不存在或已停用")
        return {"id": cid, "active": True}
    if source == "llm":
        target = str(purpose or "").strip()
        if target not in LLM_PURPOSES:
            raise ApiConfigError("切换 LLM 配置需要指定 purpose：md 或 redraw")
        providers.set_active_llm_provider(raw, target)
        return {"id": cid, "purpose": target, "active": True}
    raise ApiConfigError("OCR 凭据按轮转调度全部生效，无需（也不支持）切换启用项")


def delete_api_config(cid: str) -> dict:
    """删除一条配置（底层语义即删除对应凭据/条目）。"""
    source, raw = _split(cid)
    if source == "llm":
        providers.remove_llm_provider(raw)
        return {"id": cid, "deleted": True}
    if source == "agent":
        if not agent_provider.remove(raw):
            raise ApiConfigError("配置不存在")
        return {"id": cid, "deleted": True}
    if source == "mineru":
        if not mineru_store.remove_token(raw):
            raise ApiConfigError("Token 不存在")
        return {"id": cid, "deleted": True}
    if not doc2x_store.remove_key(raw):
        raise ApiConfigError("Key 不存在")
    return {"id": cid, "deleted": True}


def _http_get_json(url: str, api_key: str = "",
                   timeout: float = 8.0) -> tuple[int, object]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(1024 * 1024)
        text = body.decode("utf-8", "replace")
        try:
            return response.status, json.loads(text)
        except json.JSONDecodeError:
            return response.status, None


def _models_from_payload(payload: object) -> list[str]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    models = []
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            models.append(str(row["id"]))
    return models


def _test_llm_endpoint(base_url: str, api_key: str) -> dict:
    url = str(base_url or "").rstrip("/") + "/models"
    try:
        status, payload = _http_get_json(url, api_key)
    except urllib.error.HTTPError as exc:
        hint = "端点可达但拒绝了请求；检查凭据或权限"
        if exc.code == 404:
            hint = "端点不支持 /models；检查 base_url 是否包含 /v1"
        return {"ok": False, "status": exc.code, "error": f"HTTP {exc.code}",
                "hint": hint}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"ok": False, "status": None, "error": str(exc)[:200],
                "hint": "无法连接端点；确认服务已启动且地址/端口正确"}
    models = _models_from_payload(payload)
    return {"ok": True, "status": status, "model_count": len(models),
            "models": models[:20],
            "hint": "" if models else "端点可达，但未返回模型列表（仍可能可用）"}


def test_api_config(cid: str) -> dict:
    """连通性测试：LLM 类做 /models 探测；OCR 类只验证凭据已安全保存。"""
    source, raw = _split(cid)
    if source == "llm":
        row = providers.get_llm_provider(raw)
        if row is None:
            raise ApiConfigError("配置不存在")
        try:
            key = (crypto_utils.decrypt_token(str(row.get("api_key_enc") or ""))
                   if row.get("api_key_enc") else "")
        except crypto_utils.CryptoError:
            return {"ok": False, "error": "凭据无法解密，请重新保存",
                    "hint": "密钥文件可能被更换"}
        result = _test_llm_endpoint(str(row.get("base_url") or ""), key)
        result["id"] = cid
        result["transport"] = _transport_of(str(row.get("base_url") or ""))
        return result
    if source == "agent":
        provider = agent_provider.get(raw)
        if provider is None:
            raise ApiConfigError("配置不存在或已停用")
        result = _test_llm_endpoint(provider.base_url, provider.api_key)
        result["id"] = cid
        result["transport"] = _transport_of(provider.base_url)
        return result
    if source in {"mineru", "doc2x"}:
        return {"id": cid, "ok": True, "mode": "credential-only",
                "hint": "凭据已加密保存；OCR 属付费/限流服务，默认不做联网探测"}
    raise ApiConfigError("配置 id 前缀无效")


def list_remote_models(base_url: str, api_key: str = "") -> dict:
    """从 OpenAI 兼容端点拉取模型目录（magpie / Ollama / 各家云服务通用）。"""
    if not str(base_url or "").strip():
        raise ApiConfigError("base_url 不能为空")
    result = _test_llm_endpoint(str(base_url), str(api_key or ""))
    result["base_url"] = str(base_url)
    return result


def probe_magpie() -> dict:
    """探测本机 magpie 网关是否可用，并返回其模型目录。"""
    result = _test_llm_endpoint(DEFAULT_MAGPIE_BASE_URL, "")
    result["base_url"] = DEFAULT_MAGPIE_BASE_URL
    result["available"] = bool(result.get("ok"))
    return result


