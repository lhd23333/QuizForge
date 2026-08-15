"""独立桌面许可证使用的本机设备身份。

设备身份来自随机秘密，不读取主板、硬盘、网卡等硬件标识。随机秘密只以 Windows
DPAPI 当前用户密文落盘；许可证签名绑定的是它的不可逆摘要。复制许可证文件到另一
台电脑时没有可解密的本机秘密，因此无法通过设备校验。
"""

from __future__ import annotations

from base64 import b32encode
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from hashlib import sha256
import hmac
import os
from pathlib import Path
import re
import secrets
import threading

import config


_FILE_MAGIC = b"QFDI1\x00"
_SECRET_BYTES = 32
_CODE_RE = re.compile(r"QFD1(?:-[A-Z2-7]{8}){6}-[A-Z2-7]{4}\Z")
_LOCK = threading.RLock()
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DeviceIdentityError(RuntimeError):
    """设备身份无法创建或读取。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


@dataclass(frozen=True)
class DeviceIdentity:
    valid: bool
    status: str
    summary: str
    detail: str
    device_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "status": self.status,
            "summary": self.summary,
            "detail": self.detail,
            "device_id": self.device_id,
        }


def normalize_device_id(value: str) -> str:
    """校验并统一签发端/客户端共用的设备请求码格式。"""
    normalized = str(value or "").strip().upper()
    if not _CODE_RE.fullmatch(normalized):
        raise ValueError("设备请求码格式无效")
    return normalized


def _device_id(secret: bytes) -> str:
    encoded = b32encode(sha256(secret).digest()).decode("ascii").rstrip("=")
    groups = [encoded[index:index + 8] for index in range(0, len(encoded), 8)]
    return "QFD1-" + "-".join(groups)


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    return value, buffer


def _windows_libraries():
    if os.name != "nt":
        raise DeviceIdentityError("设备绑定仅支持 Windows 独立桌面版")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _protect(secret: bytes) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    source, source_buffer = _blob(secret)
    output = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(source), "QuizForge device identity", None,
        None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
    ):
        raise DeviceIdentityError(f"Windows DPAPI 加密失败：{ctypes.WinError(ctypes.get_last_error())}")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


def _unprotect(encrypted: bytes) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    source, source_buffer = _blob(encrypted)
    output = _DataBlob()
    description = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), ctypes.byref(description), None,
        None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
    ):
        raise DeviceIdentityError(f"Windows DPAPI 解密失败：{ctypes.WinError(ctypes.get_last_error())}")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
        if description:
            kernel32.LocalFree(description)
        del source_buffer


def _write_identity(path: Path, secret: bytes) -> None:
    encrypted = _protect(secret)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(_FILE_MAGIC + encrypted)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_identity(path: Path) -> bytes:
    raw = path.read_bytes()
    if not raw.startswith(_FILE_MAGIC) or len(raw) <= len(_FILE_MAGIC):
        raise DeviceIdentityError("设备身份文件格式损坏")
    secret = _unprotect(raw[len(_FILE_MAGIC):])
    if len(secret) != _SECRET_BYTES:
        raise DeviceIdentityError("设备身份内容长度无效")
    return secret


def get_or_create(path: Path | None = None) -> DeviceIdentity:
    """读取设备身份；不存在时创建，损坏时拒绝静默覆盖。"""
    target = path or config.DEVICE_IDENTITY_PATH
    with _LOCK:
        try:
            if target.is_file():
                secret = _read_identity(target)
            else:
                secret = secrets.token_bytes(_SECRET_BYTES)
                _write_identity(target, secret)
            code = _device_id(secret)
            return DeviceIdentity(
                True, "ready", "设备身份可用",
                "请求码只由本机随机身份生成，不包含硬件或题库信息。", code,
            )
        except (OSError, DeviceIdentityError) as exc:
            return DeviceIdentity(
                False, "unavailable", "设备身份不可用",
                f"{exc}。请保留原文件并联系签发者处理。",
            )


def matches(expected: str, current: str) -> bool:
    """常量时间比较规范设备请求码。"""
    try:
        left = normalize_device_id(expected)
        right = normalize_device_id(current)
    except ValueError:
        return False
    return hmac.compare_digest(left, right)
