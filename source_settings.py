"""本地来源目录配置。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path, PureWindowsPath

import config

_lock = threading.RLock()
_DEFAULT_NAMES = ("好题", "好卷", "好资料")
logger = logging.getLogger(__name__)


class SourceSettingsCorruptError(RuntimeError):
    """配置状态无法安全读取；写入必须停止以保全原件。"""


def _quarantine_corrupt(path: Path, reason: str) -> None:
    backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
    try:
        shutil.copy2(path, backup)
    except OSError:
        logger.error("source settings corrupt; preservation failed (%s)", reason)
        raise SourceSettingsCorruptError("来源配置损坏，已阻止写入") from None
    logger.error("source settings corrupt; evidence copy created (%s)", reason)
    raise SourceSettingsCorruptError("来源配置损坏，已阻止写入")


def _record_invalid_profile(name: str, reason: str) -> None:
    audit_path = Path(config.SOURCE_SETTINGS_PATH).with_suffix(".audit.jsonl")
    token = hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:12]
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"at": time.time(), "profile_id": token,
                                     "reason": reason}, separators=(",", ":")) + "\n")
    except OSError:
        logger.error("source profile audit write failed")


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
    workspace_lexical = Path(os.path.abspath(
        Path(config.SOURCE_WORKSPACE_DIR).expanduser()))
    workspace = workspace_lexical.resolve(strict=False)
    if (str(workspace_lexical) != str(workspace)
            or (getattr(workspace_lexical, "is_junction", lambda: False)())
            or PureWindowsPath(str(workspace)).drive.startswith("\\\\")):
        raise ValueError("工作区必须是普通本地目录")
    try:
        lexical.relative_to(workspace)
        in_workspace = True
    except ValueError:
        in_workspace = False
    resolved = lexical.resolve(strict=False)
    if PureWindowsPath(str(resolved)).drive.startswith("\\\\"):
        raise ValueError("不支持 UNC 网络路径")
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
        rows = _read_profile_rows()
        for name, row in rows.items():
            if (isinstance(name, str) and name.strip() and isinstance(row, dict)
                    and isinstance(row.get("input_dir"), str)
                    and isinstance(row.get("output_dir"), str)):
                try:
                    input_dir = validate_local_directory(row["input_dir"], create=False)
                    output_dir = validate_local_directory(row["output_dir"], create=False)
                except (OSError, ValueError):
                    _record_invalid_profile(name, "invalid_directory")
                    logger.warning("source profile rejected: invalid directory configuration")
                    continue
                profiles[name] = {"input_dir": str(input_dir),
                                  "output_dir": str(output_dir),
                                  "enabled": row.get("enabled") is True}
            else:
                _record_invalid_profile(str(name), "invalid_schema")
                logger.warning("source profile rejected: invalid profile schema")
        return profiles


def _read_profile_rows() -> dict:
    path = Path(config.SOURCE_SETTINGS_PATH)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _quarantine_corrupt(path, "invalid_json")
    except OSError:
        logger.error("source settings unreadable; writes blocked")
        raise SourceSettingsCorruptError("来源配置无法读取，已阻止写入") from None
    if not isinstance(raw, dict):
        _quarantine_corrupt(path, "invalid_schema")
    rows = raw.get("profiles", raw)
    if not isinstance(rows, dict):
        _quarantine_corrupt(path, "invalid_profiles")
    return rows


def _save(profiles: dict[str, dict]) -> None:
    _atomic_json(Path(config.SOURCE_SETTINGS_PATH), {"profiles": profiles})


def save_profile(name: str, *, input_dir: str | Path, output_dir: str | Path,
                 enabled: bool) -> dict:
    name = str(name).strip()
    if not name:
        raise ValueError("来源类型不能为空")
    with _lock:
        profiles = load_profiles()
        profile = {
            "input_dir": str(validate_local_directory(input_dir)),
            "output_dir": str(validate_local_directory(output_dir)),
            "enabled": bool(enabled),
        }
        raw_rows = _read_profile_rows()
        raw_rows[name] = profile
        _save(raw_rows)
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
        load_profiles()  # Validate and audit other rows before changing this profile.
        raw_rows = _read_profile_rows()
        raw_rows[name] = profile
        _save(raw_rows)
    return profile
