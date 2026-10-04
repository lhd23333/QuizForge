"""本地来源目录配置。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path, PureWindowsPath

import config

_lock = threading.RLock()
_DEFAULT_NAMES = ("好题", "好卷", "好资料")


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def validate_local_directory(raw: str | Path, *, create: bool = True) -> Path:
    """展开并验证本地目录；工作区内部的链接不能将写入带出工作区。"""
    text = str(raw).strip()
    if not text:
        raise ValueError("目录不能为空")
    win_path = PureWindowsPath(text)
    if win_path.drive.startswith("\\\\") or text.startswith(("\\\\", "//")):
        raise ValueError("不支持 UNC 网络路径")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    lexical = Path(os.path.abspath(path))
    workspace = Path(config.SOURCE_WORKSPACE_DIR).expanduser().resolve()
    try:
        lexical.relative_to(workspace)
        in_workspace = True
    except ValueError:
        in_workspace = False
    resolved = lexical.resolve(strict=False)
    if in_workspace:
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("工作区路径不能通过符号链接越界") from exc
    if lexical.exists() and not lexical.is_dir():
        raise ValueError("路径不是目录")
    if create:
        lexical.mkdir(parents=True, exist_ok=True)
    return lexical.resolve(strict=False)


def _defaults() -> dict[str, dict]:
    return {
        name: {
            "input_dir": str(Path(config.BANK_DIR) / name),
            "output_dir": str(Path(config.BANK_DIR) / name),
            "enabled": False,
        }
        for name in _DEFAULT_NAMES
    }


def load_profiles() -> dict[str, dict]:
    with _lock:
        profiles = _defaults()
        path = Path(config.SOURCE_SETTINGS_PATH)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return profiles
        if not isinstance(raw, dict):
            return profiles
        rows = raw.get("profiles", raw)
        if isinstance(rows, dict):
            for name, row in rows.items():
                if (isinstance(name, str) and name.strip() and isinstance(row, dict)
                        and isinstance(row.get("input_dir"), str)
                        and isinstance(row.get("output_dir"), str)):
                    profiles[name] = {
                        "input_dir": row["input_dir"],
                        "output_dir": row["output_dir"],
                        "enabled": row.get("enabled") is True,
                    }
        return profiles


def _save(profiles: dict[str, dict]) -> None:
    _atomic_json(Path(config.SOURCE_SETTINGS_PATH), {"profiles": profiles})


def save_profile(name: str, *, input_dir: str | Path, output_dir: str | Path,
                 enabled: bool) -> dict:
    name = str(name).strip()
    if not name:
        raise ValueError("来源类型不能为空")
    profile = {
        "input_dir": str(validate_local_directory(input_dir)),
        "output_dir": str(validate_local_directory(output_dir)),
        "enabled": bool(enabled),
    }
    with _lock:
        profiles = load_profiles()
        profiles[name] = profile
        _save(profiles)
    return profile


def reset_profile(name: str) -> dict:
    name = str(name).strip()
    if not name:
        raise ValueError("来源类型不能为空")
    defaults = _defaults()
    profile = defaults.get(name, {
        "input_dir": str(Path(config.BANK_DIR) / name),
        "output_dir": str(Path(config.BANK_DIR) / name),
        "enabled": False,
    })
    with _lock:
        profiles = load_profiles()
        profiles[name] = profile
        _save(profiles)
    return profile
