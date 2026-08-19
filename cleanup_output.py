"""启动时清理过期导出产物、上传暂存件和转换任务快照。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import config
import task_store

logger = logging.getLogger(__name__)

OUTPUT_MAX_AGE_HOURS = 24
UPLOAD_MAX_AGE_HOURS = 24
TASK_MAX_AGE_DAYS = 7
WORKSPACE_ORPHAN_MAX_AGE_HOURS = 24


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _payload_paths(value) -> set[Path]:
    """只提取任务结构里约定的临时路径字段，不碰 target_folder_id 等题库路径。"""
    found: set[Path] = set()
    if not isinstance(value, dict):
        return found
    for key in ("path", "file_path", "solution_path"):
        raw = value.get(key)
        if raw:
            found.add(Path(str(raw)))
    for raw in value.get("cleanup_paths") or []:
        if raw:
            found.add(Path(str(raw)))
    for child in value.get("groups") or []:
        found.update(_payload_paths(child))
    return found


def _payload_cleanup_dirs(value) -> set[Path]:
    """递归提取结构合集子组的 OCR 后工作区。

    这些目录不在上传目录内，不能交给普通文件清理；真正删除时仍统一走
    ``converter.cleanup_collection_workspace`` 的根目录、名称和符号链接校验。
    """
    found: set[Path] = set()
    if not isinstance(value, dict):
        return found
    for raw in value.get("cleanup_dirs") or []:
        if raw:
            found.add(Path(str(raw)))
    for child in value.get("groups") or []:
        found.update(_payload_cleanup_dirs(child))
    return found


def _active_uploads() -> set[Path]:
    active: set[Path] = set()
    for kind in task_store.KINDS:
        for _, payload in task_store.load(kind):
            active.update(p.resolve() for p in _payload_paths(payload)
                          if _inside(p, config.UPLOAD_DIR))
    return active


def _active_cleanup_dirs() -> set[Path]:
    """返回仍被未过期任务引用的合集工作区绝对路径。"""
    active: set[Path] = set()
    for kind in task_store.KINDS:
        for _, payload in task_store.load(kind):
            for path in _payload_cleanup_dirs(payload):
                try:
                    active.add(path.resolve())
                except OSError:
                    continue
    return active


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("临时文件清理失败 %s：%s", path, exc)
        return False


def _remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    dirs = sorted((p for p in root.rglob("*") if p.is_dir()),
                  key=lambda p: len(p.parts), reverse=True)
    for path in dirs:
        try:
            path.rmdir()
        except OSError:
            pass


def run_cleanup(now: float | None = None) -> dict[str, int]:
    """执行一次保守清理；任何路径必须先证明位于应用临时目录之下。"""
    current = time.time() if now is None else float(now)
    removed_tasks = task_store.purge_expired(TASK_MAX_AGE_DAYS)
    active = _active_uploads()
    active_cleanup_dirs = _active_cleanup_dirs()
    counts = {"tasks": len(removed_tasks), "uploads": 0,
              "outputs": 0, "workspaces": 0}

    # 过期任务携带的上传件可立即删；同一路径仍被未过期任务引用时必须保留。
    for payload in removed_tasks:
        for path in _payload_paths(payload):
            resolved = path.resolve()
            if (_inside(resolved, config.UPLOAD_DIR)
                    and resolved not in active and resolved.is_file()):
                counts["uploads"] += int(_unlink(resolved))

    # 结构合集会为每个分组保留一份 raw Markdown 与所需图片，供人工审核及
    # “重新转换”复用。任务快照过期后这些目录已无入口，必须同步回收；若同一目录
    # 仍被另一个活跃快照引用则保留。删除继续委托 converter 的严格边界检查，
    # cleanup_dirs 即使被损坏或手工篡改也不能越界递归删除。
    expired_cleanup_dirs: set[Path] = set()
    for payload in removed_tasks:
        for path in _payload_cleanup_dirs(payload):
            try:
                expired_cleanup_dirs.add(path.resolve())
            except OSError:
                continue
    if expired_cleanup_dirs:
        import converter

        for directory in expired_cleanup_dirs - active_cleanup_dirs:
            existed = directory.is_dir()
            converter.cleanup_collection_workspace(directory)
            counts["workspaces"] += int(existed and not directory.exists())

    # 进程可能在“物化单元目录”与“把目录写进批次快照”之间退出。此类目录
    # 没有 payload 可追溯；仅扫描专用前缀、仅删超过一天且无活跃引用的直属
    # 工作区，避免崩溃窗口长期积累整本图片副本。
    import converter

    orphan_cutoff = current - WORKSPACE_ORPHAN_MAX_AGE_HOURS * 3600
    raw_root = converter.collection_workspace_root()
    if raw_root.is_dir():
        for directory in raw_root.iterdir():
            if (not directory.is_dir()
                    or not directory.name.startswith("collection_unit_")
                    or directory.resolve() in active_cleanup_dirs):
                continue
            try:
                expired = directory.stat().st_mtime < orphan_cutoff
            except OSError:
                continue
            if expired:
                existed = directory.exists()
                converter.cleanup_collection_workspace(directory)
                counts["workspaces"] += int(existed and not directory.exists())

    upload_cutoff = current - UPLOAD_MAX_AGE_HOURS * 3600
    if config.UPLOAD_DIR.exists():
        for path in config.UPLOAD_DIR.rglob("*"):
            if not path.is_file() or path.resolve() in active:
                continue
            try:
                expired = path.stat().st_mtime < upload_cutoff
            except OSError:
                continue
            if expired:
                counts["uploads"] += int(_unlink(path))
        _remove_empty_dirs(config.UPLOAD_DIR)

    output_cutoff = current - OUTPUT_MAX_AGE_HOURS * 3600
    if config.OUTPUT_DIR.exists():
        for path in config.OUTPUT_DIR.rglob("*"):
            if not path.is_file():
                continue
            try:
                expired = path.stat().st_mtime < output_cutoff
            except OSError:
                continue
            if expired:
                counts["outputs"] += int(_unlink(path))
        _remove_empty_dirs(config.OUTPUT_DIR)

    if any(counts.values()):
        logger.info("启动清理完成：%s", counts)
    return counts
