"""识别历史的文件式归档。

每个任务组对应一个目录，目录内保存原文件、最终 Markdown 与 manifest.json。
原文件和上传暂存位于同一磁盘时优先创建硬链接，任务清理掉暂存文件后历史副本仍
有效，同时无书签合集拆出几十组时不会把同一本大 PDF 真正复制几十遍。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

import config


_lock = threading.RLock()
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST = "manifest.json"
_MARKDOWN = "result.md"


class HistoryError(ValueError):
    """历史记录不存在、损坏或无法安全操作。"""


def _items_root() -> Path:
    return config.HISTORY_DIR / "items"


def _trash_root() -> Path:
    return config.HISTORY_DIR / "trash"


def _record_dir(record_id: str, *, trashed: bool = False) -> Path:
    value = str(record_id or "")
    if not _ID_RE.fullmatch(value):
        raise HistoryError("历史记录编号无效")
    return (_trash_root() if trashed else _items_root()) / value


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(str(text).replace("\r\n", "\n").replace("\r", "\n"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _copy_or_link(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def create_record(title: str, source_paths, *, source_names=None,
                  metadata: dict | None = None) -> dict:
    """建立只含原文件的归档；识别成功后再由 attach_markdown 补结果。"""
    paths = [Path(raw).resolve() for raw in source_paths if raw]
    if not paths or any(not path.is_file() for path in paths):
        raise HistoryError("原文件不存在，无法建立历史记录")
    names = list(source_names or [])
    record_id = uuid.uuid4().hex
    now = time.time()
    with _lock:
        root = _items_root()
        root.mkdir(parents=True, exist_ok=True)
        temp_dir = root / f".{record_id}.tmp"
        target_dir = root / record_id
        temp_dir.mkdir()
        try:
            files = []
            seen = set()
            for index, source in enumerate(paths):
                identity = os.path.normcase(str(source))
                if identity in seen:
                    continue
                seen.add(identity)
                suffix = source.suffix.lower()
                stored_name = f"source-{index + 1}{suffix}"
                target = temp_dir / stored_name
                _copy_or_link(source, target)
                display_name = (
                    str(names[index]).strip() if index < len(names) else ""
                ) or source.name
                files.append({
                    "name": stored_name,
                    "display_name": Path(display_name).name,
                    "size": target.stat().st_size,
                    "sha256": _digest(target),
                })
            manifest = {
                "schema": 1,
                "id": record_id,
                "title": str(title or "识别记录").strip() or "识别记录",
                "created_at": now,
                "updated_at": now,
                "has_markdown": False,
                "files": files,
                "metadata": dict(metadata or {}),
            }
            _write_json_atomic(temp_dir / _MANIFEST, manifest)
            os.replace(temp_dir, target_dir)
            return manifest
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise


def _load_from(directory: Path) -> dict:
    try:
        payload = json.loads((directory / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoryError("历史记录损坏，无法读取") from exc
    if (not isinstance(payload, dict)
            or payload.get("schema") != 1
            or payload.get("id") != directory.name
            or not isinstance(payload.get("files"), list)):
        raise HistoryError("历史记录格式无效")
    return payload


def get(record_id: str, *, trashed: bool = False) -> dict:
    with _lock:
        directory = _record_dir(record_id, trashed=trashed)
        if not directory.is_dir():
            raise HistoryError("历史记录不存在")
        payload = _load_from(directory)
        payload["trashed"] = bool(trashed)
        return payload


def list_records(*, trashed: bool = False) -> list[dict]:
    root = _trash_root() if trashed else _items_root()
    if not root.is_dir():
        return []
    rows = []
    with _lock:
        for directory in root.iterdir():
            if not directory.is_dir() or not _ID_RE.fullmatch(directory.name):
                continue
            try:
                payload = _load_from(directory)
            except HistoryError:
                continue
            payload["trashed"] = bool(trashed)
            rows.append(payload)
    rows.sort(key=lambda item: (float(item.get("created_at") or 0), item["id"]),
              reverse=True)
    return rows


def attach_markdown(record_id: str, markdown: str, *, title: str = "",
                    metadata=None) -> dict:
    with _lock:
        directory = _record_dir(record_id)
        if not directory.is_dir():
            raise HistoryError("历史记录不存在")
        payload = _load_from(directory)
        _write_text_atomic(directory / _MARKDOWN, markdown)
        payload["has_markdown"] = True
        payload["updated_at"] = time.time()
        if str(title or "").strip():
            payload["title"] = str(title).strip()
        if metadata:
            payload.setdefault("metadata", {}).update(dict(metadata))
        _write_json_atomic(directory / _MANIFEST, payload)
        return payload


def read_markdown(record_id: str, *, trashed: bool = False) -> str:
    directory = _record_dir(record_id, trashed=trashed)
    get(record_id, trashed=trashed)
    path = directory / _MARKDOWN
    if not path.is_file():
        raise HistoryError("该记录还没有识别 Markdown")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HistoryError("识别 Markdown 无法读取") from exc


def write_markdown(record_id: str, markdown: str,
                   expected_mtime_ns: int) -> tuple[bool, int]:
    """带版本检查地保存归档 Markdown，避免资料库覆盖外部修改。"""
    with _lock:
        directory = _record_dir(record_id)
        if not directory.is_dir():
            raise HistoryError("历史记录不存在")
        payload = _load_from(directory)
        path = directory / _MARKDOWN
        if not payload.get("has_markdown") or not path.is_file():
            raise HistoryError("该记录还没有识别 Markdown")
        current_mtime = path.stat().st_mtime_ns
        if current_mtime != expected_mtime_ns:
            return False, current_mtime
        _write_text_atomic(path, markdown)
        payload["updated_at"] = time.time()
        _write_json_atomic(directory / _MANIFEST, payload)
        return True, path.stat().st_mtime_ns


def file_path(record_id: str, name: str, *, trashed: bool = False) -> Path:
    payload = get(record_id, trashed=trashed)
    allowed = {item.get("name") for item in payload["files"]}
    if payload.get("has_markdown"):
        allowed.add(_MARKDOWN)
    if name not in allowed or Path(name).name != name:
        raise HistoryError("历史文件不存在")
    path = _record_dir(record_id, trashed=trashed) / name
    if not path.is_file():
        raise HistoryError("历史文件不存在")
    return path


def move_to_trash(record_id: str) -> None:
    with _lock:
        source = _record_dir(record_id)
        if not source.is_dir():
            raise HistoryError("历史记录不存在")
        target = _record_dir(record_id, trashed=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise HistoryError("历史回收站中已存在同名记录")
        os.replace(source, target)


def restore(record_id: str) -> None:
    with _lock:
        source = _record_dir(record_id, trashed=True)
        if not source.is_dir():
            raise HistoryError("历史记录不存在")
        target = _record_dir(record_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise HistoryError("历史记录已存在，无法重复恢复")
        os.replace(source, target)


def purge(record_id: str) -> None:
    with _lock:
        target = _record_dir(record_id, trashed=True)
        if not target.is_dir():
            raise HistoryError("历史记录不存在")
        shutil.rmtree(target)
