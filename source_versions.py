"""来源文件哈希与输出版本登记。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

import config

_lock = threading.RLock()
logger = logging.getLogger(__name__)


class SourceVersionsCorruptError(RuntimeError):
    """版本账本无法安全读取；写入必须停止以保全原件。"""


def _quarantine_corrupt(path: Path, reason: str) -> None:
    backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
    try:
        shutil.copy2(path, backup)
    except OSError:
        logger.error("source versions corrupt; preservation failed (%s)", reason)
        raise SourceVersionsCorruptError("来源版本账本损坏，已阻止写入") from None
    logger.error("source versions corrupt; evidence copy created (%s)", reason)
    raise SourceVersionsCorruptError("来源版本账本损坏，已阻止写入")


def _read() -> list[dict]:
    path = Path(config.SOURCE_VERSIONS_PATH)
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _quarantine_corrupt(path, "invalid_json")
    except OSError:
        logger.error("source versions unreadable; writes blocked")
        raise SourceVersionsCorruptError("来源版本账本无法读取，已阻止写入") from None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        _quarantine_corrupt(path, "invalid_schema")
    return rows


def _write(rows: list[dict]) -> None:
    path = Path(config.SOURCE_VERSIONS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(rows, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def version_key(profile: str, source_path: Path, source_hash: str) -> str:
    canonical = "\0".join((
        str(profile).strip(),
        os.path.normcase(str(Path(source_path).expanduser().resolve())),
        str(source_hash).lower(),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _infer_profile(output: Path) -> str:
    # Legacy callers did not pass profile; use the matching configured output path.
    import source_settings

    matches = [name for name, row in source_settings.load_profiles().items()
               if Path(row["output_dir"]).resolve() == output]
    if len(matches) > 1:
        raise ValueError("输出目录对应多个来源类型，请显式指定 profile")
    return matches[0] if matches else output.name


def reserve_version(output_dir: Path, base_name: str, *, is_directory: bool,
                    source_key: str, profile: str | None = None) -> dict:
    output = Path(output_dir).expanduser().resolve()
    if not output.is_dir():
        raise ValueError("输出目录不存在")
    base = Path(str(base_name)).name.strip()
    if not base or base in {".", ".."}:
        raise ValueError("版本名称无效")
    if profile is None:
        profile = _infer_profile(output)
    else:
        profile = str(profile).strip()
        if not profile:
            raise ValueError("来源类型不能为空")
    with _lock:
        rows = _read()
        for row in rows:
            if row.get("source_key") == source_key:
                return dict(row)
        n = 1
        while True:
            name = base if n == 1 else f"{base}_{n}"
            candidate = output / name
            if not candidate.exists() and not any(
                    Path(row.get("path", "")).resolve() == candidate
                    for row in rows if row.get("status") != "recycled"):
                break
            n += 1
        row = {
            "version_id": uuid.uuid4().hex,
            "profile": profile,
            "source_key": str(source_key),
            "path": str(candidate),
            "is_directory": bool(is_directory),
            "status": "reserved",
        }
        rows.append(row)
        _write(rows)
        return dict(row)


def commit_version(record: dict, manifest: dict) -> dict:
    if not isinstance(record, dict) or not record.get("version_id"):
        raise ValueError("版本预留记录无效")
    if not isinstance(manifest, dict):
        raise ValueError("版本清单必须是对象")
    with _lock:
        rows = _read()
        for row in rows:
            if row.get("version_id") == record["version_id"]:
                if row.get("status") == "committed":
                    return dict(row)
                if row.get("status") != "reserved":
                    raise ValueError("仅可提交有效的版本预留")
                row.update({"manifest": manifest, "status": "committed"})
                _write(rows)
                return dict(row)
    raise ValueError("版本预留记录不存在")


def mark_recycle_pending(version_id: str, error: str) -> dict:
    with _lock:
        rows = _read()
        for row in rows:
            if row.get("version_id") == version_id:
                if row.get("status") != "committed":
                    raise ValueError("仅可标记已提交版本的回收失败")
                row["status"] = "recycle_pending"
                # 外部错误文本可能带本地路径或凭据，只留受控错误码。
                row["recycle_error"] = "SOURCE_RECYCLE_FAILED"
                if error:
                    logger.warning("source recycle failed; details omitted")
                _write(rows)
                return dict(row)
    raise ValueError("版本记录不存在")


def cancel_reservation(version_id: str) -> bool:
    """只释放仍未提交且输出目标尚不存在的预留。"""
    with _lock:
        rows = _read()
        for index, row in enumerate(rows):
            if row.get("version_id") != version_id:
                continue
            if row.get("status") != "reserved":
                raise ValueError("仅可释放未提交的版本预留")
            target = Path(row.get("path", ""))
            if target.exists():
                raise ValueError("版本目标已存在，不能释放预留")
            del rows[index]
            _write(rows)
            return True
    return False


def list_versions(profile: str | None = None) -> list[dict]:
    with _lock:
        rows = _read()
        return [
            dict(row) for row in rows
            if row.get("status") == "committed"
            and (profile is None or row.get("profile") == profile)
        ]
