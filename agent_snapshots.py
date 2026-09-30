"""Agent 写操作快照与回滚。

粒度：每次写操作（一次 ``execute_action`` 调用或一次批量工具调用）一个快照；
批量工具的快照天然包含其全部目标文件。快照只保存执行前已存在的文件的
原内容，不做增量 diff，也不负责清理操作后新建的文件——回滚时这些文件
会被保留并出现在结果报告里。

安全边界：

- 只接受题库目录（``BANK_DIR``）内的相对路径，拒绝越界、符号链接与保留目录；
- 单文件与单快照总量设上限，超限文件跳过快照并在 manifest 里标注；
- 回滚前自动为当前状态创建反向快照，避免"回滚执行错方向"无法挽救。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

#: 快照保留策略：超过任一限制即从最旧的开始清理。
MAX_AGE_DAYS = 30
MAX_COUNT = 100
#: 单文件与单快照总量上限，防止快照无节制吞噬磁盘。
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024

#: 题库内的保留目录：不参与题目扫描，也不做快照（图片、备份、回收站由各自的
#: 兜底机制负责）。``_handouts`` 是用户亲手制作的内容，故意不在此列——讲义被误改/
#: 误删后要能靠快照回滚。
_RESERVED = {"_assets", "_backups", ".trash"}
_SNAPSHOT_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")


class SnapshotError(ValueError):
    """快照参数或状态无效。"""


def _root() -> Path:
    return config.BANK_DIR.resolve()


def _snapshots_dir() -> Path:
    return Path(config.AGENT_SNAPSHOTS_DIR)


def safe_rel(value: object) -> str | None:
    """把外部输入收敛成题库内相对路径；非法或保留目录返回 None。"""
    text = str(value or "").replace("\\", "/").strip("/")
    if not text:
        return None
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or part.startswith(".") or part in _RESERVED:
            return None
        parts.append(part)
    return "/".join(parts) if parts else None


def create_snapshot(rel_paths, *, action: str, session_id: str = "",
                    note: str = "") -> dict | None:
    """把题库内这些相对路径的当前内容保存为一个快照。

    只保存存在的普通文件；目录与不存在的路径记入 ``skipped``。
    全部文件都不可快照时返回 ``None``（调用方继续执行原操作）。
    """
    root = _root()
    entries: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw in rel_paths or []:
        rel = safe_rel(raw)
        if not rel or rel in seen:
            continue
        seen.add(rel)
        target = root / rel
        try:
            if target.is_symlink() or not target.is_file():
                skipped.append(rel)
                continue
            size = target.stat().st_size
        except OSError:
            skipped.append(rel)
            continue
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            skipped.append(rel)
            continue
        total += size
        entries.append({"rel": rel, "size": size})
    if not entries:
        return None

    snapshot_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    directory = _snapshots_dir() / snapshot_id
    files_dir = directory / "files"
    try:
        files_dir.mkdir(parents=True, exist_ok=False)
        manifest_files = []
        for item in entries:
            rel = item["rel"]
            source = root / rel
            target = files_dir / Path(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            manifest_files.append(
                {"rel": rel, "size": item["size"], "sha256": digest})
    except OSError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise SnapshotError(f"快照写入失败：{exc}") from exc

    manifest = {
        "id": snapshot_id,
        "created": datetime.now().isoformat(timespec="seconds"),
        "session_id": str(session_id or ""),
        "action": str(action or ""),
        "note": str(note or "")[:500],
        "files": manifest_files,
        "skipped": skipped,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def list_snapshots(limit: int = 50) -> list[dict]:
    """按时间倒序列出快照摘要（不含文件内容）。"""
    directory = _snapshots_dir()
    if not directory.is_dir():
        return []
    try:
        limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    rows = []
    for item in sorted(directory.iterdir(), reverse=True):
        if not item.is_dir() or not _SNAPSHOT_ID_RE.fullmatch(item.name):
            continue
        try:
            data = json.loads(
                (item / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({
            "id": str(data.get("id") or item.name),
            "created": str(data.get("created") or ""),
            "action": str(data.get("action") or ""),
            "note": str(data.get("note") or ""),
            "file_count": len(data.get("files") or []),
            "skipped_count": len(data.get("skipped") or []),
        })
        if len(rows) >= limit:
            break
    return rows


def get_snapshot(snapshot_id: str) -> dict | None:
    """读取单个快照 manifest；id 非法或不存在时返回 None。"""
    sid = str(snapshot_id or "").strip()
    if not _SNAPSHOT_ID_RE.fullmatch(sid):
        return None
    try:
        return json.loads(
            (_snapshots_dir() / sid / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def rollback_snapshot(snapshot_id: str, *, session_id: str = "") -> dict:
    """把快照内文件写回原位；写回前先为当前状态创建反向快照。

    只做"恢复内容到 manifest 记录的路径"这一件事：不删除任何文件、
    不移动任何文件。快照之后新建的文件会原样保留。
    """
    manifest = get_snapshot(snapshot_id)
    if manifest is None:
        raise SnapshotError("快照不存在或已过期")
    root = _root()
    files = [item for item in (manifest.get("files") or [])
             if isinstance(item, dict)]
    reverse = None
    try:
        reverse = create_snapshot(
            [str(item.get("rel") or "") for item in files],
            action=f"rollback:{manifest.get('action') or ''}",
            session_id=session_id,
            note=f"回滚 {snapshot_id} 前的自动快照")
    except SnapshotError:
        logger.warning("回滚前反向快照失败，继续回滚 %s", snapshot_id)

    restored: list[str] = []
    failed: list[dict] = []
    for item in files:
        rel = safe_rel(item.get("rel"))
        if not rel:
            continue
        source = (_snapshots_dir() / str(manifest.get("id"))
                  / "files" / Path(*rel.split("/")))
        target = root / rel
        try:
            if not source.is_file():
                failed.append({"rel": rel, "error": "快照文件缺失"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".qfrollback.tmp")
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
            restored.append(rel)
        except OSError as exc:
            failed.append({"rel": rel, "error": str(exc)[:200]})
    return {
        "snapshot_id": str(manifest.get("id") or snapshot_id),
        "restored": restored,
        "failed": failed,
        "reverse_snapshot": reverse.get("id") if reverse else None,
    }


def cleanup_expired(now: float | None = None) -> int:
    """按保留策略删除过期/超量快照，返回删除数量。"""
    directory = _snapshots_dir()
    if not directory.is_dir():
        return 0
    now = time.time() if now is None else now
    cutoff = now - MAX_AGE_DAYS * 86400
    items: list[tuple[float, Path]] = []
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return 0
    for item in candidates:
        if not item.is_dir() or not _SNAPSHOT_ID_RE.fullmatch(item.name):
            continue
        try:
            items.append((item.stat().st_mtime, item))
        except OSError:
            continue
    items.sort(key=lambda pair: pair[0], reverse=True)
    removed = 0
    for index, (mtime, item) in enumerate(items):
        if mtime >= cutoff and index < MAX_COUNT:
            continue
        shutil.rmtree(item, ignore_errors=True)
        removed += 1
    return removed
