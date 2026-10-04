"""来源文件哈希与输出版本登记。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path

import config

_lock = threading.RLock()


def _read() -> list[dict]:
    try:
        rows = json.loads(Path(config.SOURCE_VERSIONS_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


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


def reserve_version(output_dir: Path, base_name: str, *, is_directory: bool,
                    source_key: str) -> dict:
    output = Path(output_dir).expanduser().resolve()
    if not output.is_dir():
        raise ValueError("输出目录不存在")
    base = Path(str(base_name)).name.strip()
    if not base or base in {".", ".."}:
        raise ValueError("版本名称无效")
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
            "profile": output.name,
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
                row.update({"manifest": manifest, "status": "committed"})
                _write(rows)
                return dict(row)
    raise ValueError("版本预留记录不存在")


def mark_recycle_pending(version_id: str, error: str) -> dict:
    with _lock:
        rows = _read()
        for row in rows:
            if row.get("version_id") == version_id:
                row["status"] = "recycle_pending"
                row["recycle_error"] = str(error)
                _write(rows)
                return dict(row)
    raise ValueError("版本记录不存在")


def list_versions(profile: str | None = None) -> list[dict]:
    with _lock:
        rows = _read()
        return [
            dict(row) for row in rows
            if row.get("status") == "committed"
            and (profile is None or row.get("profile") == profile)
        ]
