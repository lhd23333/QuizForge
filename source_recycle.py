"""Windows 系统回收站适配。"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


def can_recycle(path: Path) -> bool:
    """判断源路径是否适合提交给 Shell 回收站。"""
    candidate = Path(path)
    try:
        return candidate.is_file() or candidate.is_dir()
    except OSError:
        return False


def _shell_delete(path: Path) -> None:
    if os.name != "nt":
        raise OSError("Windows 系统回收站仅在 Windows 上可用")
    raw = str(Path(path).resolve()) + "\0\0"
    operation = _SHFILEOPSTRUCTW(
        None, FO_DELETE, raw, None,
        FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI,
        0, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"Windows 回收站操作失败: {result}")


def send_to_recycle_bin(path: Path) -> None:
    candidate = Path(path)
    if not can_recycle(candidate):
        raise FileNotFoundError(str(candidate))
    _shell_delete(candidate)
