"""LLM 识别模型配置：JSON 文件存储，可存多套、一键切换当前生效的那套。

替代 quizbank 的 llm_providers 表 + llm_provider.py。存储位置见
`config.PROVIDERS_PATH`（data/providers.json），cc-switch 风格：一个 active id
+ 一份配置列表。api_key 只存 Fernet 密文（crypto_utils），设置页不回显明文。

没有启用项时 resolve() 返回 None，调用方（converter.py）回落到 project-alpha
的默认 DeepSeek 客户端——与加这个模块之前的行为一致。
"""

import dataclasses
import json
import logging
import threading

import config
import crypto_utils

logger = logging.getLogger(__name__)

_lock = threading.Lock()


@dataclasses.dataclass(frozen=True)
class ProviderConfig:
    """解析结果。api_key 是解密后的明文，只在内存里存在，别写日志。"""
    id: str
    name: str
    base_url: str
    api_key: str
    model: str
    max_tokens: int

    @property
    def label(self) -> str:
        return self.name


def _load() -> dict:
    if not config.PROVIDERS_PATH.exists():
        return {"active": None, "providers": []}
    try:
        return json.loads(config.PROVIDERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("providers.json 解析失败，视为空配置")
        return {"active": None, "providers": []}


def _save(data: dict):
    config.PROVIDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PROVIDERS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_llm_providers() -> list[dict]:
    """返回列表（不含解密后的明文 key，仅供设置页展示）。"""
    return list(_load()["providers"])


def get_llm_provider(pid: str) -> dict | None:
    for p in _load()["providers"]:
        if p["id"] == pid:
            return p
    return None


def _to_config(row: dict) -> ProviderConfig | None:
    try:
        api_key = crypto_utils.decrypt_token(row["api_key_enc"])
    except crypto_utils.CryptoError:
        logger.warning("LLM 配置 id=%s 的 api_key 解密失败，已跳过", row["id"])
        return None
    if not api_key:
        return None
    return ProviderConfig(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        api_key=api_key,
        model=row["model"],
        max_tokens=int(row["max_tokens"]),
    )


def get_active_llm_provider() -> dict | None:
    data = _load()
    active = data.get("active")
    if not active:
        return None
    return get_llm_provider(active)


def resolve() -> ProviderConfig | None:
    """解析这次识别该用的 LLM 配置；没有启用项则返回 None。"""
    row = get_active_llm_provider()
    return _to_config(row) if row is not None else None


# 与 quizbank 的 llm_provider.resolve_active() 对齐，供 converter.py 沿用同一调用方式。
resolve_active = resolve


def add_llm_provider(name: str, base_url: str, api_key_enc: str,
                      model: str, max_tokens: int) -> str:
    """新增一套配置，返回 id。第一套自动置为启用。"""
    import uuid

    with _lock:
        data = _load()
        first = data.get("active") is None
        pid = uuid.uuid4().hex[:8]
        data["providers"].append({
            "id": pid,
            "name": name.strip(),
            "base_url": base_url.strip(),
            "api_key_enc": api_key_enc,
            "model": model.strip(),
            "max_tokens": int(max_tokens),
        })
        if first:
            data["active"] = pid
        _save(data)
        return pid


def deactivate_llm_providers():
    """清空当前启用项（不删除配置）。回落到 project-alpha 的老行为。"""
    with _lock:
        data = _load()
        data["active"] = None
        _save(data)


def set_active_llm_provider(pid: str):
    """把 pid 设为当前生效配置。pid 不存在则无操作。"""
    with _lock:
        data = _load()
        if not any(p["id"] == pid for p in data["providers"]):
            return
        data["active"] = pid
        _save(data)


def remove_llm_provider(pid: str):
    """删除一套配置。删掉的正好是启用项时就没有启用配置了，解析会自动回落。"""
    with _lock:
        data = _load()
        data["providers"] = [p for p in data["providers"] if p["id"] != pid]
        if data.get("active") == pid:
            data["active"] = None
        _save(data)
