"""QuizForge 离线签名许可证的读取、验证与安装。

发行包只包含 Ed25519 公钥；签发私钥由 ``tools/license_signer.py`` 管理，绝不能
进入应用目录。许可证只负责降低随意转发和明确内测资格，不能替代编译保护，也不
声称能够阻止有能力修改本机程序的人绕过校验。
"""

from __future__ import annotations

from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import asdict, dataclass
from datetime import date
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import config
import device_identity


PRODUCT_ID = "quizforge"
LICENSE_SCHEMA = 2
LEGACY_LICENSE_SCHEMA = 1
MAX_LICENSE_BYTES = 64 * 1024
KNOWN_FEATURES = frozenset({"export"})


@dataclass(frozen=True)
class LicenseState:
    """供业务和页面消费的稳定状态；不暴露原始签名或未经验证的字段。"""

    valid: bool
    status: str
    summary: str
    detail: str
    license_id: str = ""
    licensee: str = ""
    edition: str = ""
    issued_at: str = ""
    expires_at: str = ""
    updates_until: str = ""
    device_id: str = ""
    features: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["features"] = list(self.features)
        value["export_allowed"] = self.valid and "export" in self.features
        value["enforced"] = is_enforced()
        return value


def is_enforced() -> bool:
    """仅独立桌面内测包强制授权；源码开发与 Obsidian 托管后端保持可调试。"""
    return os.environ.get("QUIZFORGE_LICENSE_ENFORCED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """签名只覆盖 payload，编码规则固定，避免不同平台缩进/换行导致验签失败。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _invalid(status: str, summary: str, detail: str) -> LicenseState:
    return LicenseState(False, status, summary, detail)


def _clean_text(payload: dict[str, Any], key: str, *, limit: int = 160) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"字段 {key} 必须是字符串")
    value = value.strip()
    if not value or len(value) > limit or any(ord(ch) < 32 for ch in value):
        raise ValueError(f"字段 {key} 的内容无效")
    return value


def _optional_date(payload: dict[str, Any], key: str) -> tuple[str, date | None]:
    value = payload.get(key)
    if value in (None, ""):
        return "", None
    if not isinstance(value, str):
        raise ValueError(f"字段 {key} 必须是 YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"字段 {key} 必须是 YYYY-MM-DD")
    return value, parsed


def _public_key() -> Ed25519PublicKey:
    try:
        raw = config.LICENSE_PUBLIC_KEY_PATH.read_bytes()
        key = serialization.load_pem_public_key(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("软件内置的许可证公钥缺失或损坏") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("软件内置的许可证公钥类型不正确")
    return key


def verify_document(
    document: Any,
    *,
    today: date | None = None,
    expected_device_id: str | None = None,
    require_device: bool | None = None,
) -> LicenseState:
    """验证已解析的许可证文档，不读写磁盘。"""
    if not isinstance(document, dict):
        return _invalid("invalid", "许可证无效", "许可证顶层必须是 JSON 对象。")
    schema = document.get("schema")
    if schema not in {LEGACY_LICENSE_SCHEMA, LICENSE_SCHEMA}:
        return _invalid("unsupported", "许可证版本不受支持", "请向签发者索取新版许可证。")
    payload = document.get("payload")
    signature_text = document.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature_text, str):
        return _invalid("invalid", "许可证无效", "许可证缺少 payload 或 signature。")

    try:
        signature = b64decode(signature_text.encode("ascii"), validate=True)
        _public_key().verify(signature, canonical_payload(payload))
    except (UnicodeEncodeError, Base64Error, InvalidSignature, ValueError) as exc:
        return _invalid("invalid", "许可证签名无效", str(exc) or "文件可能被修改。")

    try:
        product = _clean_text(payload, "product", limit=40)
        license_id = _clean_text(payload, "license_id", limit=128)
        licensee = _clean_text(payload, "licensee")
        edition = _clean_text(payload, "edition", limit=40)
        device_id = ""
        if schema == LICENSE_SCHEMA:
            device_id = device_identity.normalize_device_id(
                _clean_text(payload, "device_id", limit=128)
            )
        issued_text, issued = _optional_date(payload, "issued_at")
        if issued is None:
            raise ValueError("字段 issued_at 不能为空")
        not_before_text, not_before = _optional_date(payload, "not_before")
        expires_text, expires = _optional_date(payload, "expires_at")
        updates_text, updates = _optional_date(payload, "updates_until")
        raw_features = payload.get("features")
        if not isinstance(raw_features, list) or not raw_features:
            raise ValueError("字段 features 必须是非空数组")
        features = tuple(sorted({str(item).strip() for item in raw_features}))
        if any(feature not in KNOWN_FEATURES for feature in features):
            raise ValueError("许可证包含当前版本不认识的功能项")
        if expires is not None and not_before is not None and expires < not_before:
            raise ValueError("许可证有效期前后矛盾")
        if updates is not None and updates < issued:
            raise ValueError("更新有效期早于签发日期")
    except (TypeError, ValueError) as exc:
        return _invalid("invalid", "许可证内容无效", str(exc))

    if product != PRODUCT_ID:
        return _invalid("wrong_product", "许可证不适用于 QuizForge", "产品标识不匹配。")
    enforce_device = is_enforced() if require_device is None else require_device
    if enforce_device:
        if schema == LEGACY_LICENSE_SCHEMA:
            return _invalid(
                "device_required", "许可证需要重新签发",
                "这份旧许可证没有绑定设备。请复制本机设备请求码，向签发者索取新版许可证。",
            )
        if expected_device_id is None:
            identity = device_identity.get_or_create()
            if not identity.valid:
                return _invalid(
                    "device_unavailable", identity.summary, identity.detail
                )
            expected_device_id = identity.device_id
        if not device_identity.matches(device_id, expected_device_id):
            return _invalid(
                "wrong_device", "许可证不适用于本机",
                "许可证绑定的设备与当前设备请求码不一致。",
            )
    current = today or date.today()
    effective = not_before or issued
    if current < effective:
        return _invalid(
            "not_yet_valid", "许可证尚未生效", f"生效日期：{effective.isoformat()}。"
        )
    if expires is not None and current > expires:
        return _invalid(
            "expired", "许可证已过期", f"到期日期：{expires.isoformat()}。请联系签发者续期。"
        )
    return LicenseState(
        True,
        "valid",
        "许可证有效",
        "本机已完成离线签名验证。",
        license_id=license_id,
        licensee=licensee,
        edition=edition,
        issued_at=issued_text,
        expires_at=expires_text,
        updates_until=updates_text,
        device_id=device_id,
        features=features,
    )


def verify_bytes(
    raw: bytes,
    *,
    today: date | None = None,
    expected_device_id: str | None = None,
    require_device: bool | None = None,
) -> LicenseState:
    if not raw:
        return _invalid("invalid", "许可证为空", "请选择有效的 .qflicense 文件。")
    if len(raw) > MAX_LICENSE_BYTES:
        return _invalid("invalid", "许可证过大", "许可证文件不得超过 64 KiB。")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _invalid("invalid", "许可证格式错误", str(exc))
    return verify_document(
        document,
        today=today,
        expected_device_id=expected_device_id,
        require_device=require_device,
    )


def load(
    path: Path | None = None,
    *,
    today: date | None = None,
    expected_device_id: str | None = None,
    require_device: bool | None = None,
) -> LicenseState:
    license_path = path or config.LICENSE_PATH
    if not license_path.is_file():
        return _invalid(
            "missing", "尚未导入许可证",
            "可以阅读和整理题库；独立桌面内测版的预览与正式导出需要许可证。",
        )
    try:
        raw = license_path.read_bytes()
    except OSError as exc:
        return _invalid("unreadable", "许可证无法读取", str(exc))
    return verify_bytes(
        raw,
        today=today,
        expected_device_id=expected_device_id,
        require_device=require_device,
    )


def install(
    raw: bytes,
    path: Path | None = None,
    *,
    expected_device_id: str | None = None,
    require_device: bool | None = None,
) -> LicenseState:
    """验证通过后原子安装；无效文件绝不覆盖当前可用许可证。"""
    state = verify_bytes(
        raw,
        expected_device_id=expected_device_id,
        require_device=require_device,
    )
    if not state.valid:
        return state
    document = json.loads(raw.decode("utf-8"))
    normalized = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    target = path or config.LICENSE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(normalized)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return load(
        target,
        expected_device_id=expected_device_id,
        require_device=require_device,
    )


def export_allowed(state: LicenseState | None = None) -> bool:
    current = state or load()
    return current.valid and "export" in current.features
