"""Doc2X API Key 的本机加密存储（支持多份，兼容旧单 Key 文件）。"""

import json
import logging
from pathlib import Path
import threading
import uuid
from datetime import datetime

import config
import crypto_utils

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _path():
    """新添加的 Key 只写入独立文件，避免覆盖升级前的配置。"""
    return getattr(config, "DOC2X_LOCAL_KEY_PATH", config.DOC2X_KEY_PATH)


def _legacy_path():
    """返回升级前的配置文件；与新路径相同时不重复读取。"""
    path = getattr(config, "DOC2X_KEY_PATH", None)
    if path is None:
        return None
    path = Path(path)
    return None if path == Path(_path()) else path


def _load(path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.warning("Doc2X 配置解析失败，已跳过：%s", path)
        return {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def has_key() -> bool:
    """只判断密文是否存在；不在页面请求里解密。"""
    return bool(_all_entries())


def _entries_from(path, source: str) -> list[dict]:
    if path is None:
        return []
    data = _load(path)
    items = data.get("keys")
    if isinstance(items, list):
        return [{**item, "source": source} for item in items
                if isinstance(item, dict) and item.get("key_enc")]
    encrypted = (data.get("key_enc") or "").strip()
    if encrypted:
        return [{"id": "single", "label": "", "key_enc": encrypted,
                 "added": "", "source": source}]
    return []


def _local_entries() -> list[dict]:
    return _entries_from(_path(), "local")


def _all_entries() -> list[dict]:
    """合并新旧配置；相同密文只保留一份，旧文件始终只读。"""
    result = []
    seen = set()
    for item in [*_local_entries(), *_entries_from(_legacy_path(), "legacy")]:
        encrypted = str(item.get("key_enc") or "")
        if not encrypted or encrypted in seen:
            continue
        seen.add(encrypted)
        result.append(item)
    return result


def list_keys() -> list[dict]:
    """设置页只取非敏感元数据。"""
    return [{"id": item.get("id") or "", "label": item.get("label") or "",
             "added": item.get("added") or "",
             "legacy": item.get("source") == "legacy"}
            for item in _all_entries()]


def add_key(plain: str, label: str = "") -> bool:
    plain = (plain or "").strip()
    if not plain:
        return False
    encrypted = crypto_utils.encrypt_token(plain)
    with _lock:
        items = _local_entries()
        items.append({"id": uuid.uuid4().hex, "label": label.strip(),
                      "key_enc": encrypted,
                      "added": datetime.now().isoformat(timespec="seconds")})
        _save({"keys": items})
    return True


def set_key(plain: str) -> bool:
    """加密并替换当前 Key；空串不修改，返回是否写入。"""
    plain = (plain or "").strip()
    if not plain:
        return False
    encrypted = crypto_utils.encrypt_token(plain)
    with _lock:
        _save({"key_enc": encrypted})
    return True


def clear_key() -> None:
    with _lock:
        _save({})


def remove_key(key_id: str) -> bool:
    with _lock:
        items = _local_entries()
        kept = [item for item in items if (item.get("id") or "") != key_id]
        if len(kept) == len(items):
            return False
        _save({"keys": kept})
    return True


def resolve_all() -> list[str]:
    out = []
    seen = set()
    for item in _all_entries():
        try:
            value = crypto_utils.decrypt_token(item["key_enc"])
        except crypto_utils.CryptoError:
            logger.warning("Doc2X Key 解密失败（.enc_key 可能被换过），已跳过该条")
            continue
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def resolve() -> str:
    """返回明文 Key；未配置返回空串，密文损坏则保留明确的加密错误。"""
    values = resolve_all()
    return values[0] if values else ""
