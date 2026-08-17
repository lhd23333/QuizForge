"""QuizForge Windows 独立桌面入口。

先确定用户数据与题库目录，再导入 Flask 应用；config.py 在导入时固定路径，顺序
不能反。桌面壳只负责窗口、目录选择和进程生命周期，不复制任何题库业务。
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import traceback
from urllib.parse import quote
import hashlib
import uuid
from contextlib import contextmanager


APP_NAME = "QuizForge"
DESKTOP_CONFIG_NAME = "desktop.json"
MAX_REMEMBERED_BANKS = 100
_INVALID_BANK_NAME_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})


def _default_app_data_dir() -> Path:
    configured = os.environ.get("QUIZFORGE_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return (Path(local) / APP_NAME).resolve()
    return (Path.home() / ".quizforge").resolve()


def _default_bank_dir() -> Path:
    return (Path.home() / "Documents" / APP_NAME).resolve()


def _load_desktop_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_desktop_config(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


_desktop_config_thread_lock = threading.RLock()


@contextmanager
def _desktop_config_guard(path: Path):
    """跨进程串行化 desktop.json 的读改写，防多窗口互相丢登记记录。"""
    with _desktop_config_thread_lock:
        if os.name != "nt":
            yield
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        name = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:20]
        handle = kernel32.CreateMutexW(None, False, f"Local\\QuizForge.Config.{name}")
        if not handle:
            raise OSError(ctypes.get_last_error(), "创建桌面配置锁失败")
        try:
            result = kernel32.WaitForSingleObject(handle, 30000)
            if result not in (0, 0x80):
                raise TimeoutError("等待桌面配置锁超时")
            try:
                yield
            finally:
                kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)


def _path_key(path: Path) -> str:
    """Windows 路径大小写不敏感；题库列表据此避免重复登记。"""
    return os.path.normcase(str(path.resolve()))


def _configured_bank_path(raw: object) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _configured_assets_path(raw: object) -> Path | None:
    """读取全局共享图片目录；只接受绝对路径，避免进程工作目录改变含义。"""
    return _configured_bank_path(raw)


def _saved_assets_dir(value: dict, bank: Path) -> Path:
    """取得共享图片目录；旧配置缺字段时保持原来的“当前题库/_assets”。"""
    configured = _configured_assets_path(value.get("assets_dir"))
    return configured if configured is not None else bank.resolve() / "_assets"


def _bank_display_name(path: Path, preferred: object = "") -> str:
    value = str(preferred or "").strip()
    if value and len(value) <= 80 and not any(ord(char) < 32 for char in value):
        return value
    return path.name or str(path)


def _normalize_subject(raw: object) -> str:
    return str(raw or "").strip().lower() if str(raw or "").strip().lower() in {
        "math", "physics"
    } else "math"


def _saved_subject(value: dict, bank: Path) -> str:
    target_key = _path_key(bank)
    for entry in value.get("banks") or []:
        if not isinstance(entry, dict):
            continue
        path = _configured_bank_path(entry.get("path"))
        if path is not None and _path_key(path) == target_key:
            return _normalize_subject(entry.get("subject"))
    return "math"


def _bank_entries(value: dict, current: Path | None = None,
                  current_subject: str | None = None) -> list[dict[str, str]]:
    """兼容旧 ``bank_dir``，统一得到去重后的题库列表。"""
    candidates: list[tuple[object, object, object]] = []
    if current is not None:
        subject = (current_subject if current_subject is not None
                   else _saved_subject(value, current))
        candidates.append((str(current), "", subject))
    saved = _configured_bank_path(value.get("bank_dir"))
    candidates.append((value.get("bank_dir"), "",
                       _saved_subject(value, saved) if saved else "math"))
    raw_entries = value.get("banks")
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if isinstance(entry, dict):
                candidates.append((entry.get("path"), entry.get("name"),
                                   entry.get("subject")))
            else:
                # 早期开发快照可能直接存路径字符串，读取时一并兼容。
                candidates.append((entry, "", "math"))

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_path, raw_name, raw_subject in candidates:
        path = _configured_bank_path(raw_path)
        if path is None:
            continue
        key = _path_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": _bank_display_name(path, raw_name), "path": str(path),
                       "subject": _normalize_subject(raw_subject)})
        if len(result) >= MAX_REMEMBERED_BANKS:
            break
    return result


def _normalized_desktop_config(value: dict, current: Path | None = None,
                               current_subject: str | None = None) -> dict:
    normalized = dict(value)
    entries = _bank_entries(value, current, current_subject)
    if current is not None:
        normalized["bank_dir"] = str(current.resolve())
    elif entries and _configured_bank_path(normalized.get("bank_dir")) is None:
        normalized["bank_dir"] = entries[0]["path"]
    normalized["banks"] = entries
    if _configured_assets_path(normalized.get("assets_dir")) is None:
        fallback = current or _configured_bank_path(normalized.get("bank_dir"))
        if fallback is not None:
            normalized["assets_dir"] = str(fallback.resolve() / "_assets")
    return normalized


def _remembered_desktop_config(value: dict, bank: Path,
                               subject: str = "math") -> dict:
    """登记 bank，但保留原来的默认题库；供并行新窗口和启动收尾使用。"""
    normalized = _normalized_desktop_config(value)
    target = bank.resolve()
    target_key = _path_key(target)
    entries = [
        entry for entry in normalized["banks"]
        if _path_key(Path(entry["path"])) != target_key
    ]
    normalized["banks"] = [
        *entries,
        {"name": _bank_display_name(target), "path": str(target),
         "subject": _normalize_subject(subject)},
    ][:MAX_REMEMBERED_BANKS]
    if _configured_bank_path(normalized.get("bank_dir")) is None:
        normalized["bank_dir"] = str(target)
    return normalized


def _parallel_window_config(value: dict, current: Path) -> dict:
    """读取多窗口共享配置；只有配置尚无默认题库时才用当前窗口补齐。"""
    normalized = _normalized_desktop_config(value)
    if _saved_bank(normalized) is None:
        normalized = _normalized_desktop_config(value, current)
    return normalized


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _merge_registered_assets(value: dict, current_assets: Path,
                             target: Path) -> dict[str, int]:
    """把所有已登记题库的旧 ``_assets`` 合并到新共享目录，不删除源文件。

    正文只存文件名；同名异内容无法靠改目录无损解决，必须整次拒绝，避免某个题库
    静默串图。全部冲突预检通过后才复制，复制使用同目录临时文件加原子替换。
    """
    normalized = _normalized_desktop_config(value)
    sources = [current_assets.resolve()]
    sources.extend(Path(entry["path"]).resolve() / "_assets"
                   for entry in normalized.get("banks") or [])
    unique_sources: list[Path] = []
    seen: set[str] = set()
    for source in sources:
        key = _path_key(source)
        if key not in seen:
            seen.add(key)
            unique_sources.append(source)

    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    if _is_link_or_junction(target) or not target.is_dir():
        raise ValueError("共享图片目录不能是符号链接、联接或普通文件")

    pending: list[tuple[Path, Path]] = []
    planned_by_name: dict[str, Path] = {}
    reused = 0
    for source in unique_sources:
        if not source.exists() or _path_key(source) == _path_key(target):
            continue
        if _is_link_or_junction(source) or not source.is_dir():
            raise ValueError(f"旧图片目录不是普通目录：{source}")
        for item in source.iterdir():
            if not item.is_file() or item.is_symlink():
                continue
            destination = target / item.name
            if destination.exists():
                if (not destination.is_file() or destination.is_symlink()
                        or item.stat().st_size != destination.stat().st_size
                        or _file_digest(item) != _file_digest(destination)):
                    raise ValueError(f"同名图片内容冲突，未切换目录：{item.name}")
                reused += 1
                continue
            # Windows 同名判断不区分大小写；提前发现 ``A.png`` / ``a.png`` 的
            # 内容冲突，不能等复制一半后才由文件系统碰撞。
            name_key = item.name.casefold()
            planned = planned_by_name.get(name_key)
            if planned is not None:
                if (item.stat().st_size != planned.stat().st_size
                        or _file_digest(item) != _file_digest(planned)):
                    raise ValueError(f"同名图片内容冲突，未切换目录：{item.name}")
                reused += 1
                continue
            planned_by_name[name_key] = item
            pending.append((item, destination))

    copied = 0
    for source, destination in pending:
        tmp = target / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copy2(source, tmp)
            if destination.exists():
                if (_file_digest(tmp) != _file_digest(destination)):
                    raise ValueError(f"复制期间出现同名图片冲突：{destination.name}")
                reused += 1
            else:
                tmp.replace(destination)
                copied += 1
        finally:
            tmp.unlink(missing_ok=True)
    return {"copied": copied, "reused": reused, "sources": len(unique_sources)}


def _bank_state_dir(app_data_dir: Path, bank_dir: Path) -> Path:
    digest = hashlib.sha256(_path_key(bank_dir).encode("utf-8")).hexdigest()[:16]
    return app_data_dir / "banks" / digest


_OCR_DIRECTORY_LIST_FIELDS = frozenset({
    "collection_cache_dirs", "cleanup_dirs",
})
_OCR_DIRECTORY_FIELDS = frozenset({"workspace_dir"})
_OCR_FILE_FIELDS = frozenset({"collection_raw_path"})


def _is_link_or_junction(path: Path) -> bool:
    """Windows junction 与符号链接都不能作为迁移源或目标。"""
    try:
        return path.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(path)
        )
    except OSError:
        return True


def _tree_manifest(root: Path) -> dict[str, tuple]:
    """返回不跟随链接的完整目录清单，用于验证复制不是半截结果。"""
    if _is_link_or_junction(root) or not root.is_dir():
        raise RuntimeError(f"OCR 工作区不是普通目录：{root}")
    manifest: dict[str, tuple] = {}

    def _scan(directory: Path, prefix: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeError(f"无法读取 OCR 工作区：{directory}") from exc
        for entry in entries:
            entry_path = Path(entry.path)
            relative = (prefix / entry.name).as_posix()
            if entry.is_symlink() or _is_link_or_junction(entry_path):
                raise RuntimeError(f"OCR 工作区含链接或联接点：{entry_path}")
            if entry.is_dir(follow_symlinks=False):
                manifest[relative] = ("dir",)
                _scan(entry_path, prefix / entry.name)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise RuntimeError(f"OCR 工作区含非普通文件：{entry_path}")
            digest = hashlib.sha256()
            try:
                with entry_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                size = entry_path.stat().st_size
            except OSError as exc:
                raise RuntimeError(f"无法校验 OCR 工作区文件：{entry_path}") from exc
            manifest[relative] = ("file", size, digest.hexdigest())

    _scan(root, Path())
    return manifest


def _iter_ocr_task_paths(value: object):
    """枚举任务快照中约定会指向 OCR 工作区的可改写路径槽位。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _OCR_DIRECTORY_LIST_FIELDS:
                if isinstance(child, list):
                    for index, item in enumerate(child):
                        if isinstance(item, str):
                            yield child, index, item, "directory"
                continue
            if key in _OCR_DIRECTORY_FIELDS:
                if isinstance(child, str):
                    yield value, key, child, "directory"
                continue
            if key in _OCR_FILE_FIELDS:
                if isinstance(child, str):
                    yield value, key, child, "file"
                continue
            if key == "extract_dirs":
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, dict) and isinstance(item.get("dir"), str):
                            yield item, "dir", item["dir"], "directory"
                        yield from _iter_ocr_task_paths(item)
                continue
            yield from _iter_ocr_task_paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_ocr_task_paths(child)


def _lexical_relative_to(path: str | Path, root: Path) -> Path | None:
    """按路径文本判断归属，不先 resolve，避免链接把越界路径伪装成安全路径。"""
    candidate = Path(path)
    if not candidate.is_absolute():
        return None
    candidate_key = os.path.normcase(os.path.abspath(str(candidate)))
    root_key = os.path.normcase(os.path.abspath(str(root)))
    try:
        if os.path.commonpath((candidate_key, root_key)) != root_key:
            return None
    except ValueError:
        return None
    relative = os.path.relpath(candidate_key, root_key)
    return Path(relative)


def _assert_update_tasks_quiescent(snapshot: dict, tasks_path: Path) -> None:
    """更新期间不接管尚未稳定的任务，避免启动恢复合法改写快照。"""
    active = {"pending", "converting"}
    jobs = snapshot.get("job") if isinstance(snapshot.get("job"), dict) else {}
    for task_id, item in jobs.items():
        payload = item.get("payload") if isinstance(item, dict) else None
        if isinstance(payload, dict) and payload.get("status") in active:
            raise RuntimeError(
                f"更新前仍有未完成转换任务（job {task_id}）；请先重试、跳过或中止任务"
            )

    batches = snapshot.get("batch") if isinstance(snapshot.get("batch"), dict) else {}
    for batch_id, item in batches.items():
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict):
            continue
        if payload.get("status") in active or payload.get("running"):
            raise RuntimeError(
                f"更新前仍有未完成转换批次（batch {batch_id}）；请先处理完该批次"
            )
        for group in payload.get("groups") or []:
            if not isinstance(group, dict):
                continue
            if group.get("status") in active or group.get("in_flight"):
                raise RuntimeError(
                    f"更新前仍有在途转换组（batch {batch_id}）；请先处理完该组"
                )


def _migrate_legacy_ocr_workspaces(legacy_root: Path,
                                   bank_state_dir: Path, *,
                                   strict: bool = False,
                                   require_quiescent: bool = False) -> int:
    """把当前题库任务明确引用的旧 OCR 工作区复制到题库状态目录。

    旧安装目录由所有题库共享，不能按目录名猜归属，更不能整目录搬走。这里只认
    ``conversion_tasks.json`` 的缓存、清理、切块审核与合集原文路径字段。每个引用
    都先归并到旧根目录的直接子工作区，复制后再逐槽位改写；源或目标中的符号链接、
    junction、半截同名目录都会拒绝，避免把付费结果悄悄指向错误内容。
    """
    legacy_root = Path(legacy_root)
    bank_state_dir = Path(bank_state_dir)
    tasks_path = bank_state_dir / "conversion_tasks.json"
    if not tasks_path.exists():
        return 0
    if (_is_link_or_junction(tasks_path) or not tasks_path.is_file()
            or _is_link_or_junction(bank_state_dir)):
        if strict:
            raise RuntimeError(f"任务状态目录或文件不是普通路径：{tasks_path}")
        return 0

    destination_root = bank_state_dir / "raw_md"
    migrated = 0
    with _desktop_config_guard(tasks_path):
        try:
            snapshot = json.loads(tasks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if strict:
                raise RuntimeError(f"无法读取转换任务快照：{tasks_path}") from exc
            return 0
        if not isinstance(snapshot, dict):
            if strict:
                raise RuntimeError(f"转换任务快照格式无效：{tasks_path}")
            return 0
        if require_quiescent:
            _assert_update_tasks_quiescent(snapshot, tasks_path)

        referenced = list(_iter_ocr_task_paths(snapshot))
        replacements: dict[str, str] = {}
        source_manifests: dict[str, dict[str, tuple]] = {}
        for _, _, raw_path, path_kind in referenced:
            if raw_path in replacements:
                continue
            relative = _lexical_relative_to(raw_path, legacy_root)
            if relative is None:
                continue
            parts = relative.parts
            if not parts or relative == Path("."):
                if strict:
                    raise RuntimeError(f"任务路径不能直接指向 OCR 根目录：{raw_path}")
                continue
            workspace_name = parts[0]
            suffix = Path(*parts[1:]) if len(parts) > 1 else Path()
            if path_kind == "directory" and suffix != Path():
                if strict:
                    raise RuntimeError(f"OCR 目录字段不是直接工作区：{raw_path}")
                continue

            source = legacy_root / workspace_name
            try:
                source_manifest = source_manifests.setdefault(
                    workspace_name, _tree_manifest(source)
                )
            except RuntimeError:
                if strict:
                    raise
                continue

            if (_is_link_or_junction(destination_root)
                    or (destination_root.exists() and not destination_root.is_dir())):
                if strict:
                    raise RuntimeError(f"OCR 迁移目标不是普通目录：{destination_root}")
                continue
            destination_root.mkdir(parents=True, exist_ok=True)
            target = destination_root / workspace_name
            if _is_link_or_junction(target):
                if strict:
                    raise RuntimeError(f"OCR 迁移目标是链接或联接点：{target}")
                continue
            if target.exists():
                try:
                    target_manifest = _tree_manifest(target)
                except RuntimeError:
                    if strict:
                        raise
                    continue
                if target_manifest != source_manifest:
                    if strict:
                        raise RuntimeError(
                            f"OCR 迁移目标已存在但内容不一致：{target}"
                        )
                    continue
            else:
                temporary = destination_root / (
                    f".{workspace_name}.{uuid.uuid4().hex}.tmp")
                try:
                    shutil.copytree(source, temporary, symlinks=True)
                    if _tree_manifest(temporary) != source_manifest:
                        raise RuntimeError(f"OCR 工作区复制校验失败：{source}")
                    os.replace(temporary, target)
                    if _tree_manifest(target) != source_manifest:
                        raise RuntimeError(f"OCR 工作区落盘校验失败：{target}")
                    migrated += 1
                except (OSError, RuntimeError):
                    shutil.rmtree(temporary, ignore_errors=True)
                    if strict:
                        raise
                    continue
            replacement = target / suffix if suffix != Path() else target
            if path_kind == "file" and not replacement.is_file():
                if strict:
                    raise RuntimeError(f"OCR 原文文件未随工作区迁移：{raw_path}")
                continue
            replacements[raw_path] = str(replacement)

        if not replacements:
            return migrated

        changed = False

        for container, key, raw_path, _ in referenced:
            replacement = replacements.get(raw_path)
            if replacement is not None and replacement != raw_path:
                container[key] = replacement
                changed = True
        if not changed:
            if strict:
                leftovers = [raw for _, _, raw, _ in _iter_ocr_task_paths(snapshot)
                             if _lexical_relative_to(raw, legacy_root) is not None]
                if leftovers:
                    raise RuntimeError(
                        f"仍有任务路径指向旧 OCR 根目录：{leftovers[0]}"
                    )
            return migrated

        bank_state_dir.mkdir(parents=True, exist_ok=True)
        temporary_tasks = tasks_path.with_name(
            f".{tasks_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary_tasks.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_tasks, tasks_path)
        finally:
            temporary_tasks.unlink(missing_ok=True)
        if strict:
            leftovers = [raw for _, _, raw, _ in _iter_ocr_task_paths(snapshot)
                         if _lexical_relative_to(raw, legacy_root) is not None]
            if leftovers:
                raise RuntimeError(f"OCR 路径迁移审计失败：{leftovers[0]}")
    return migrated


def migrate_all_legacy_ocr_workspaces(legacy_root: Path,
                                      app_data_dir: Path) -> int:
    """更新安装前，迁移所有题库任务明确引用的旧 OCR 工作区。

    桌面正常启动只需要迁移当前窗口的题库；原位更新器却必须在启动后的哈希核验
    之前完成迁移，否则一次合法的路径改写会被误报成“任务状态遭到修改”。这里只
    枚举 ``%LOCALAPPDATA%/QuizForge/banks`` 的直接子目录，具体能复制哪些工作区
    仍完全由 :func:`_migrate_legacy_ocr_workspaces` 的任务引用与边界校验决定。
    """
    banks_root = Path(app_data_dir) / "banks"
    if not banks_root.exists():
        return 0
    if (_is_link_or_junction(banks_root) or not banks_root.is_dir()):
        raise RuntimeError(f"题库状态根目录不是普通目录：{banks_root}")
    if _is_link_or_junction(legacy_root):
        raise RuntimeError(f"旧 OCR 根目录是链接或联接点：{legacy_root}")
    migrated = 0
    try:
        entries = list(banks_root.iterdir())
    except OSError as exc:
        raise RuntimeError(f"无法枚举题库状态目录：{banks_root}") from exc
    for state_dir in entries:
        if _is_link_or_junction(state_dir):
            raise RuntimeError(f"题库状态项是链接或联接点：{state_dir}")
        if not state_dir.is_dir():
            continue
        migrated += _migrate_legacy_ocr_workspaces(
            Path(legacy_root), state_dir, strict=True, require_quiescent=True)
    return migrated


def _migrate_legacy_bank_state(config_path: Path, app_data_dir: Path,
                               desktop_config: dict, bank_dir: Path) -> dict:
    """把旧版全局任务状态只迁给当时的默认题库一次。"""
    # 两个窗口可能被连续打开。迁移和标记必须共用跨进程锁，否则两边会同时
    # copytree 到同一目录；desktop.json 的原子写只能防半截 JSON，防不了重复迁移。
    with _desktop_config_guard(config_path):
        current = _load_desktop_config(config_path)
        if current.get("bank_state_migrated"):
            return current
        saved = _saved_bank(current or desktop_config)
        if saved is None or _path_key(saved) != _path_key(bank_dir):
            return current or desktop_config
        state_dir = _bank_state_dir(app_data_dir, bank_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        for name in ("conversion_tasks.json", "selections.json"):
            source = app_data_dir / name
            target = state_dir / name
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)
        for name in ("uploads", "corpus", "output"):
            source = app_data_dir / name
            target = state_dir / name
            if source.is_dir() and not target.exists():
                shutil.copytree(source, target)
        current["bank_state_migrated"] = str(bank_dir.resolve())
        _write_desktop_config(config_path, current)
        return current


def _saved_bank(value: dict) -> Path | None:
    selected = _configured_bank_path(value.get("bank_dir"))
    if selected is not None:
        return selected
    entries = _bank_entries(value)
    return Path(entries[0]["path"]) if entries else None


def _validate_new_bank_name(raw_name: str) -> str:
    name = str(raw_name or "").strip()
    if not name:
        raise ValueError("请输入新题库名称")
    if len(name) > 80:
        raise ValueError("题库名称不能超过 80 个字符")
    if name in {".", ".."} or name.endswith((" ", ".")):
        raise ValueError("题库名称不能以空格或句点结尾")
    if any(ord(char) < 32 or char in _INVALID_BANK_NAME_CHARS for char in name):
        raise ValueError("题库名称包含 Windows 文件夹不允许的字符")
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("该名称是 Windows 保留名称，请更换")
    return name


def _ask_initial_bank(default: Path) -> Path:
    """首次启动用系统目录选择器；取消则创建默认文档目录。"""
    try:
        from tkinter import Tk, filedialog

        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            parent=root,
            title="选择 QuizForge 题库文件夹（可选择已有 Obsidian vault）",
            initialdir=str(default.parent if default.parent.exists() else Path.home()),
            mustexist=False,
        )
        root.destroy()
        if chosen:
            return Path(chosen).expanduser().resolve()
    except Exception:
        # 打包环境缺少 Tk 时仍能启动；进入软件后可用 pywebview 原生对话框再切换。
        pass
    default.mkdir(parents=True, exist_ok=True)
    return default


def _configure_logging(app_data_dir: Path) -> None:
    log_dir = app_data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "quizforge.log", maxBytes=2 * 1024 * 1024,
        backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


class DesktopApi:
    """只暴露桌面外壳能力；页面业务继续走现有本地 HTTP 接口。"""

    def __init__(self, bank_dir: Path, app_data_dir: Path, config_path: Path,
                 subject: str = "math", assets_dir: Path | None = None):
        self.bank_dir = bank_dir.resolve()
        self.app_data_dir = app_data_dir
        self.config_path = config_path
        self.subject = _normalize_subject(subject)
        self.assets_dir = (assets_dir.resolve() if assets_dir is not None
                           else _saved_assets_dir(
                               _load_desktop_config(config_path), self.bank_dir))
        self._config_lock = threading.RLock()
        # pywebview 会递归扫描 js_api 的公开属性；若把原生窗口挂在公开属性上，
        # Windows 端会误遍历整棵 WinForms/WebView2 对象树并刷出递归与线程错误。
        self._window = None
        self._maximized = False
        self.restart_requested = False
        self.restart_bank_dir: Path | None = None
        self.restart_subject: str | None = None
        self._update_lock = threading.RLock()
        self._update_dir = self.app_data_dir / "updates"
        previous_update = {}
        try:
            import update_client

            previous_update = update_client.previous_update_status(self._update_dir)
        except (ImportError, OSError):
            pass
        self._update_state: dict[str, object] = {
            "status": "idle",
            "message": "",
            "downloaded": 0,
            "total": 0,
            "previous": previous_update,
        }

    def runtime_info(self) -> dict:
        import desktop_product
        import service_ports

        return {
            "desktop": True,
            "version": desktop_product.PRODUCT_VERSION,
            "bank_dir": str(self.bank_dir),
            "assets_dir": str(self.assets_dir),
            "subject": self.subject,
            "banks": self._bank_list(),
            "services": service_ports.status(),
        }

    def _bank_list(self) -> list[dict[str, object]]:
        value = _normalized_desktop_config(
            _load_desktop_config(self.config_path), self.bank_dir, self.subject
        )
        current_key = _path_key(self.bank_dir)
        result: list[dict[str, object]] = []
        for entry in value["banks"]:
            path = Path(entry["path"])
            try:
                available = path.is_dir()
            except OSError:
                available = False
            result.append({
                "name": entry["name"],
                "path": entry["path"],
                "current": _path_key(path) == current_key,
                "available": available,
                "subject": entry["subject"],
                "subject_label": "物理" if entry["subject"] == "physics" else "数学",
            })
        return result

    def list_bank_directories(self) -> dict:
        """返回桌面题库列表；不扫描题目，也不触碰任何题库内容。"""
        return {
            "ok": True,
            "bank_dir": str(self.bank_dir),
            "assets_dir": str(self.assets_dir),
            "subject": self.subject,
            "banks": self._bank_list(),
        }

    @staticmethod
    def _open_folder(path: Path) -> dict:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except (AttributeError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def open_bank_folder(self) -> dict:
        return self._open_folder(self.bank_dir)

    def open_local_file(self, raw_path: str) -> dict:
        """用系统默认程序打开题库内文件；路径不允许越出当前题库。"""
        value = str(raw_path or "").strip().replace("\\", "/")
        relative = Path(value)
        if (not value or relative.is_absolute() or relative.drive
                or any(part in ("", ".", "..") for part in relative.parts)):
            return {"ok": False, "error": "文件路径无效"}
        root = self.bank_dir.resolve()
        candidate = root.joinpath(*relative.parts)
        try:
            if (_is_link_or_junction(root) or
                    any(_is_link_or_junction(root.joinpath(*relative.parts[:index]))
                        for index in range(1, len(relative.parts) + 1))):
                return {"ok": False, "error": "文件路径不能包含链接或联接点"}
            target = candidate.resolve(strict=True)
            if os.path.commonpath((str(root), str(target))) != str(root):
                return {"ok": False, "error": "文件路径超出题库目录"}
            if not target.is_file():
                return {"ok": False, "error": "文件不存在"}
            os.startfile(str(target))
        except (AttributeError, OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def open_log_folder(self) -> dict:
        return self._open_folder(self.app_data_dir / "logs")

    def open_data_folder(self) -> dict:
        return self._open_folder(self.app_data_dir)

    def window_minimize(self) -> dict:
        if self._window is None:
            return {"ok": False, "error": "桌面窗口尚未就绪"}
        self._window.minimize()
        return {"ok": True}

    def window_toggle_maximize(self) -> dict:
        if self._window is None:
            return {"ok": False, "error": "桌面窗口尚未就绪"}
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized
        return {"ok": True, "maximized": self._maximized}

    def window_close(self) -> dict:
        if self._window is None:
            return {"ok": False, "error": "桌面窗口尚未就绪"}
        # 与重启一样延迟销毁，让 JS API 有机会先收到成功响应。
        threading.Timer(0.05, self._window.destroy).start()
        return {"ok": True}

    def window_resize(self, width: int, height: int, fixed_corner: str) -> dict:
        """缩放无边框窗口；fixed_corner 表示拖动时保持不动的对侧角。"""
        if self._window is None:
            return {"ok": False, "error": "桌面窗口尚未就绪"}
        if self._maximized:
            return {"ok": False, "error": "最大化状态下不能拖动缩放"}
        try:
            from webview.window import FixPoint

            fixed_points = {
                "nw": FixPoint.NORTH | FixPoint.WEST,
                "ne": FixPoint.NORTH | FixPoint.EAST,
                "sw": FixPoint.SOUTH | FixPoint.WEST,
                "se": FixPoint.SOUTH | FixPoint.EAST,
            }
            fix_point = fixed_points[str(fixed_corner or "").lower()]
            target_width = max(1024, int(width))
            target_height = max(680, int(height))
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "窗口缩放参数无效"}
        self._window.resize(target_width, target_height, fix_point)
        return {"ok": True, "width": target_width, "height": target_height}

    def choose_bank_directory(self) -> dict:
        """兼容旧页面：选择目录后直接保存；新页面使用浏览、保存两步接口。"""
        selected = self.browse_bank_directory()
        if not selected.get("ok"):
            return selected
        return self.set_bank_directory(selected.get("bank_dir", ""), self.subject)

    def browse_bank_directory(self) -> dict:
        """只打开系统目录选择器，不修改配置。"""
        import webview

        if self._window is None:
            return {"ok": False, "error": "桌面窗口尚未就绪"}
        try:
            selected = self._window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=str(self.bank_dir),
                allow_multiple=False,
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("打开题库目录选择器失败")
            return {"ok": False, "error": f"无法打开目录选择器：{exc}"}
        if not selected:
            return {"ok": False, "cancelled": True}
        # pywebview 的契约是 tuple；部分后端/版本可能直接返回字符串，不能把
        # selected[0] 错当成盘符首字符。
        raw_path = selected if isinstance(selected, str) else selected[0]
        try:
            target = Path(raw_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "error": f"所选目录无效：{exc}"}
        return {"ok": True, "bank_dir": str(target)}

    def browse_bank_parent_directory(self) -> dict:
        """选择新题库的父目录；创建动作由显式的名称确认完成。"""
        import webview

        if self._window is None:
            return {"ok": False, "error": "桌面窗口尚未就绪"}
        try:
            selected = self._window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=str(self.bank_dir.parent),
                allow_multiple=False,
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("打开新题库父目录选择器失败")
            return {"ok": False, "error": f"无法打开目录选择器：{exc}"}
        if not selected:
            return {"ok": False, "cancelled": True}
        raw_path = selected if isinstance(selected, str) else selected[0]
        try:
            target = Path(raw_path).expanduser()
            if not target.is_absolute():
                raise ValueError("父目录必须是绝对路径")
            target = target.resolve()
            if not target.is_dir():
                raise ValueError("父目录不存在")
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "error": f"所选父目录无效：{exc}"}
        return {"ok": True, "parent_dir": str(target)}

    def browse_assets_directory(self) -> dict:
        """选择所有题库共用的图片目录，只返回路径，不立即改配置。"""
        import webview

        if self._window is None:
            return {"ok": False, "error": "桌面窗口尚未就绪"}
        start = self.assets_dir if self.assets_dir.is_dir() else self.bank_dir
        try:
            selected = self._window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=str(start), allow_multiple=False)
        except Exception as exc:
            logging.getLogger(__name__).exception("打开共享图片目录选择器失败")
            return {"ok": False, "error": f"无法打开目录选择器：{exc}"}
        if not selected:
            return {"ok": False, "cancelled": True}
        raw_path = selected if isinstance(selected, str) else selected[0]
        try:
            target = Path(raw_path).expanduser()
            if not target.is_absolute():
                raise ValueError("图片目录必须是绝对路径")
            target = target.resolve()
            if not target.is_dir():
                raise ValueError("图片目录不存在")
            if _is_link_or_junction(target):
                raise ValueError("图片目录不能是符号链接或联接")
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "error": f"所选图片目录无效：{exc}"}
        return {"ok": True, "assets_dir": str(target)}

    def set_assets_directory(self, raw_path: str) -> dict:
        """保存唯一共享图片目录，并无损合并各题库原有图片。"""
        try:
            value_path = str(raw_path or "").strip()
            if not value_path:
                raise ValueError("请输入共享图片目录")
            target = Path(value_path).expanduser()
            if not target.is_absolute():
                raise ValueError("图片目录必须是绝对路径")
            target = target.resolve()
            if not target.is_dir() or _is_link_or_junction(target):
                raise ValueError("图片目录必须是已存在的普通目录")
            probe = target / f".quizforge_asset_test_{os.getpid()}_{threading.get_ident()}"
            try:
                probe.write_text("ok", encoding="ascii")
            finally:
                probe.unlink(missing_ok=True)

            with self._config_lock:
                with _desktop_config_guard(self.config_path):
                    value = _normalized_desktop_config(
                        _load_desktop_config(self.config_path),
                        self.bank_dir, self.subject)
                    merged = _merge_registered_assets(value, self.assets_dir, target)
                    value["assets_dir"] = str(target)
                    _write_desktop_config(self.config_path, value)
                    saved = _configured_assets_path(
                        _load_desktop_config(self.config_path).get("assets_dir"))
                    if saved is None or _path_key(saved) != _path_key(target):
                        raise OSError("共享图片目录写入后校验不一致")
        except ValueError as exc:
            logging.getLogger(__name__).warning(
                "共享图片目录校验未通过：%s", exc)
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError) as exc:
            logging.getLogger(__name__).exception("保存共享图片目录失败：%s", raw_path)
            return {"ok": False, "error": str(exc)}
        restart_required = _path_key(target) != _path_key(self.assets_dir)
        logging.getLogger(__name__).info(
            "共享图片目录已保存：%s（复制 %d，复用 %d）",
            target, merged["copied"], merged["reused"])
        return {
            "ok": True, "assets_dir": str(target),
            "restart_required": restart_required,
            "copied": merged["copied"], "reused": merged["reused"],
        }

    @staticmethod
    def _validate_existing_bank(raw_path: str) -> Path:
        value_path = str(raw_path or "").strip()
        if not value_path:
            raise ValueError("请输入题库文件夹路径")
        candidate = Path(value_path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("题库路径必须是绝对路径")
        target = candidate.resolve()
        if not target.is_dir():
            raise ValueError("题库文件夹不存在；如需创建，请使用“新建题库”")
        # 随机探针只验证根目录可写，完成后立即清理，不改动题库内容。
        probe = target / f".quizforge_write_test_{os.getpid()}_{threading.get_ident()}"
        try:
            probe.write_text("ok", encoding="ascii")
        finally:
            probe.unlink(missing_ok=True)
        return target

    def _save_active_bank(self, target: Path, subject: str) -> None:
        with self._config_lock:
            with _desktop_config_guard(self.config_path):
                value = _normalized_desktop_config(
                    _load_desktop_config(self.config_path), self.bank_dir, self.subject
                )
                target_key = _path_key(target)
                entries = [
                    entry for entry in value["banks"]
                    if _path_key(Path(entry["path"])) != target_key
                ]
                value["bank_dir"] = str(target)
                value["banks"] = [
                    {"name": _bank_display_name(target), "path": str(target),
                     "subject": _normalize_subject(subject)},
                    *entries,
                ][:MAX_REMEMBERED_BANKS]
                _write_desktop_config(self.config_path, value)
                saved = _load_desktop_config(self.config_path)
                if _path_key(Path(saved.get("bank_dir", ""))) != target_key:
                    raise OSError("配置写入后校验不一致")

    def _remember_bank(self, target: Path, subject: str) -> None:
        """登记题库但不改变当前窗口和默认题库。"""
        with self._config_lock:
            with _desktop_config_guard(self.config_path):
                base = _parallel_window_config(
                    _load_desktop_config(self.config_path), self.bank_dir)
                value = _remembered_desktop_config(base, target, subject)
                _write_desktop_config(self.config_path, value)

    def set_bank_directory(self, raw_path: str, subject: str = "math") -> dict:
        """登记并选择已有题库；实际切换在重启后生效。"""
        try:
            target = self._validate_existing_bank(raw_path)
            subject = _normalize_subject(subject)
            self._save_active_bank(target, subject)
        except ValueError as exc:
            logging.getLogger(__name__).warning("题库目录校验未通过：%s", exc)
            return {"ok": False, "error": f"题库目录不可写：{exc}"}
        except (OSError, RuntimeError) as exc:
            logging.getLogger(__name__).exception("保存题库目录失败：%s", raw_path)
            return {"ok": False, "error": f"题库目录不可写：{exc}"}
        logging.getLogger(__name__).info("题库位置已保存，重启后生效：%s", target)
        self.restart_bank_dir = target
        self.restart_subject = subject
        return {
            "ok": True,
            "bank_dir": str(target),
            "restart_required": (_path_key(target) != _path_key(self.bank_dir)
                                 or subject != self.subject),
        }

    def open_bank_in_new_window(self, raw_path: str, subject: str = "math") -> dict:
        """登记已有题库并启动独立进程；当前窗口与题库保持不变。"""
        try:
            target = self._validate_existing_bank(raw_path)
            if _path_key(target) == _path_key(self.bank_dir):
                raise ValueError("这个题库已经在当前窗口打开")
            subject = _normalize_subject(subject)
            self._remember_bank(target, subject)
            process = _launch_bank_process(target, self.app_data_dir, subject)
        except ValueError as exc:
            return {"ok": False, "error": f"题库目录不可写：{exc}"}
        except (OSError, RuntimeError) as exc:
            logging.getLogger(__name__).exception("在新窗口打开题库失败：%s", raw_path)
            return {"ok": False, "error": f"无法打开新窗口：{exc}"}
        logging.getLogger(__name__).info(
            "已在新进程打开题库：pid=%s bank=%s", process.pid, target)
        return {"ok": True, "bank_dir": str(target), "pid": process.pid}

    def create_bank_directory(self, raw_parent: str, raw_name: str,
                              subject: str = "math") -> dict:
        """在指定父目录中新建空题库并选中；不会写演示题或 Obsidian 配置。"""
        created: Path | None = None
        try:
            name = _validate_new_bank_name(raw_name)
            parent = Path(str(raw_parent or "").strip()).expanduser()
            if not parent.is_absolute():
                raise ValueError("父目录必须是绝对路径")
            parent = parent.resolve()
            if not parent.is_dir():
                raise ValueError("父目录不存在")
            target = (parent / name).resolve()
            if target.parent != parent:
                raise ValueError("新题库路径越过所选父目录")
            target.mkdir()
            created = target
            self._validate_existing_bank(str(target))
            subject = _normalize_subject(subject)
            self._save_active_bank(target, subject)
        except FileExistsError:
            return {"ok": False, "error": "同名文件夹已经存在；请改名或用“打开已有题库”"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError) as exc:
            logging.getLogger(__name__).exception(
                "创建题库失败：parent=%s name=%s", raw_parent, raw_name
            )
            detail = str(exc)
            if created is not None and created.exists():
                detail += "；空文件夹已经创建，但没有加入题库列表"
            return {"ok": False, "error": detail}
        logging.getLogger(__name__).info("新题库已创建，重启后生效：%s", target)
        self.restart_bank_dir = target
        self.restart_subject = subject
        return {"ok": True, "bank_dir": str(target), "restart_required": True}

    def create_bank_in_new_window(self, raw_parent: str, raw_name: str,
                                  subject: str = "math") -> dict:
        """创建空题库、登记后在新窗口打开，不切走当前窗口。"""
        created: Path | None = None
        try:
            name = _validate_new_bank_name(raw_name)
            parent = Path(str(raw_parent or "").strip()).expanduser()
            if not parent.is_absolute():
                raise ValueError("父目录必须是绝对路径")
            parent = parent.resolve()
            if not parent.is_dir():
                raise ValueError("父目录不存在")
            target = (parent / name).resolve()
            if target.parent != parent:
                raise ValueError("新题库路径越过所选父目录")
            target.mkdir()
            created = target
            self._validate_existing_bank(str(target))
            subject = _normalize_subject(subject)
            self._remember_bank(target, subject)
            process = _launch_bank_process(target, self.app_data_dir, subject)
        except FileExistsError:
            return {"ok": False, "error": "同名文件夹已经存在；请改名或用“打开已有题库”"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError) as exc:
            logging.getLogger(__name__).exception(
                "创建并打开新题库失败：parent=%s name=%s", raw_parent, raw_name
            )
            detail = str(exc)
            if created is not None and created.exists():
                detail += "；空文件夹已经创建，但新窗口没有打开"
            return {"ok": False, "error": detail}
        return {"ok": True, "bank_dir": str(target), "pid": process.pid}

    def remove_bank_directory(self, raw_path: str) -> dict:
        """只移除列表记录；当前题库和磁盘文件都不能由此接口删除。"""
        try:
            target = _configured_bank_path(raw_path)
            if target is None:
                raise ValueError("题库路径必须是绝对路径")
            target_key = _path_key(target)
            if target_key == _path_key(self.bank_dir):
                raise ValueError("当前题库不能移除；请先切换到另一题库")
            with self._config_lock:
                with _desktop_config_guard(self.config_path):
                    value = _parallel_window_config(
                        _load_desktop_config(self.config_path), self.bank_dir)
                    before = len(value["banks"])
                    value["banks"] = [
                        entry for entry in value["banks"]
                        if _path_key(Path(entry["path"])) != target_key
                    ]
                    if len(value["banks"]) == before:
                        raise ValueError("题库列表中没有这条记录")
                    _write_desktop_config(self.config_path, value)
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        logging.getLogger(__name__).info("已从题库列表移除记录（未删除文件）：%s", target)
        return {"ok": True, "removed": str(target), "files_deleted": False}

    def restart_app(self) -> dict:
        if self._window is None:
            return {"ok": False, "error": "桌面窗口尚未就绪"}
        self.restart_requested = True
        # JS API 在线程里执行，延迟销毁避免窗口先关、调用结果来不及返回。
        threading.Timer(0.2, self._window.destroy).start()
        return {"ok": True}

    def update_status(self) -> dict:
        """返回一键更新进度；只含版本、字节数与状态文本。"""
        with self._update_lock:
            return {"ok": True, "update": dict(self._update_state)}

    def _set_update_state(self, **values) -> None:
        with self._update_lock:
            self._update_state.update(values)

    def _run_update(self) -> None:
        import desktop_product
        import service_ports
        import update_client

        try:
            self._set_update_state(
                status="checking", message="正在重新检查更新",
                downloaded=0, total=0, error="",
            )
            entry = Path(sys.argv[0]).resolve()
            if not getattr(sys, "frozen", False) or entry.suffix.lower() != ".exe":
                raise update_client.UpdateCheckError("一键覆盖仅在正式安装版中可用")

            def progress(downloaded: int, total: int) -> None:
                self._set_update_state(
                    status="downloading", message="正在下载安装包",
                    downloaded=downloaded, total=total,
                )

            prepared = update_client.prepare_update(
                desktop_product.PRODUCT_VERSION,
                service_ports.load().update_manifest_url,
                self._update_dir,
                progress=progress,
            )
            self._set_update_state(
                status="verified", message="安装包已验证，正在退出并覆盖安装",
                version=prepared["latest_version"],
            )
            update_client.launch_installer(
                Path(str(prepared["installer_path"])), entry.parent, entry,
                self._update_dir, parent_pid=os.getpid(),
            )
            self._set_update_state(status="exiting", message="即将关闭并完成更新")
            if self._window is None:
                raise update_client.UpdateCheckError("桌面窗口尚未就绪")
            threading.Timer(0.5, self._window.destroy).start()
        except (update_client.UpdateCheckError, OSError, ValueError) as exc:
            logging.getLogger(__name__).exception("一键更新失败")
            self._set_update_state(status="failed", message=str(exc), error=str(exc))

    def start_update(self) -> dict:
        """启动一次受签名保护的更新；重复点击不会并发下载。"""
        with self._update_lock:
            if self._update_state.get("status") in {
                "checking", "downloading", "verified", "exiting",
            }:
                return {"ok": False, "error": "更新已在进行中",
                        "update": dict(self._update_state)}
            self._update_state = {
                "status": "queued", "message": "正在准备更新",
                "downloaded": 0, "total": 0,
                "previous": self._update_state.get("previous", {}),
            }
        threading.Thread(
            target=self._run_update, name="quizforge-update", daemon=True
        ).start()
        return self.update_status()


def _restart_command() -> list[str]:
    entry = Path(sys.argv[0]).resolve()
    if entry.suffix.lower() == ".exe":
        return [str(entry)]
    return [sys.executable, str(Path(__file__).resolve())]


def _launch_bank_process(bank_dir: Path, app_data_dir: Path, subject: str = "math"):
    """以显式题库环境启动独立进程，避免继承旧窗口的 QUIZFORGE_BANK。"""
    desktop_config = _normalized_desktop_config(
        _load_desktop_config(app_data_dir / DESKTOP_CONFIG_NAME),
        bank_dir, subject)
    assets_dir = _saved_assets_dir(desktop_config, bank_dir)
    env = os.environ.copy()
    env["QUIZFORGE_BANK"] = str(bank_dir.resolve())
    env["QUIZFORGE_ASSETS_DIR"] = str(assets_dir.resolve())
    env["QUIZFORGE_DATA_DIR"] = str(app_data_dir.resolve())
    env["QUIZFORGE_BANK_STATE_DIR"] = str(_bank_state_dir(app_data_dir, bank_dir))
    env["QUIZFORGE_SUBJECT"] = _normalize_subject(subject)
    return subprocess.Popen(_restart_command(), env=env, close_fds=True)


def _webview_storage_dir(app_data_dir: Path, bank_dir: Path) -> Path:
    """每个题库独立 WebView2 profile，避免多进程争用同一个浏览器数据目录。"""
    digest = hashlib.sha256(_path_key(bank_dir).encode("utf-8")).hexdigest()[:16]
    return app_data_dir / "webview-banks" / digest


def main() -> int:
    app_data_dir = _default_app_data_dir()
    app_data_dir.mkdir(parents=True, exist_ok=True)
    desktop_config_path = app_data_dir / DESKTOP_CONFIG_NAME
    desktop_config = _load_desktop_config(desktop_config_path)

    env_bank = os.environ.get("QUIZFORGE_BANK", "").strip()
    env_subject = os.environ.get("QUIZFORGE_SUBJECT", "").strip()
    saved_bank = _saved_bank(desktop_config)
    first_launch = not env_bank and saved_bank is None
    if env_bank:
        bank_dir = Path(env_bank).expanduser().resolve()
    elif saved_bank is not None:
        bank_dir = saved_bank
    else:
        bank_dir = _ask_initial_bank(_default_bank_dir())
        subject = "math"
        _write_desktop_config(
            desktop_config_path,
            _normalized_desktop_config({}, bank_dir, subject),
        )
    if env_bank:
        subject = _normalize_subject(env_subject or _saved_subject(desktop_config, bank_dir))
    elif saved_bank is not None:
        subject = _saved_subject(desktop_config, bank_dir)
    bank_dir.mkdir(parents=True, exist_ok=True)
    desktop_config = _migrate_legacy_bank_state(
        desktop_config_path, app_data_dir, desktop_config, bank_dir)
    desktop_config = _normalized_desktop_config(
        desktop_config, bank_dir, subject)
    assets_dir = _saved_assets_dir(desktop_config, bank_dir)
    bank_state_dir = _bank_state_dir(app_data_dir, bank_dir)
    bank_state_dir.mkdir(parents=True, exist_ok=True)
    try:
        _migrate_legacy_ocr_workspaces(
            Path(__file__).resolve().parent
            / "vendor" / "project_alpha" / "output" / "raw_md",
            bank_state_dir,
        )
    except Exception:
        # 迁移失败时保留旧任务引用，不能因为一次缓存搬运阻断题库启动。
        logging.getLogger(__name__).exception("迁移旧 OCR 工作区失败，已保留旧引用")

    # 必须发生在 import app/config 之前，见模块顶部说明。
    os.environ["QUIZFORGE_DATA_DIR"] = str(app_data_dir)
    os.environ["QUIZFORGE_BANK"] = str(bank_dir)
    os.environ["QUIZFORGE_ASSETS_DIR"] = str(assets_dir)
    os.environ["QUIZFORGE_BANK_STATE_DIR"] = str(bank_state_dir)
    os.environ["QUIZFORGE_SUBJECT"] = subject
    os.environ["QUIZFORGE_DESKTOP"] = "1"
    # 软件版默认免费本地运行。仅当用户在启动前显式设置旧兼容变量时，
    # 才启用历史离线授权门控，避免升级时误把账号系统重新带回正常链路。
    _configure_logging(app_data_dir)

    import webview
    from werkzeug.serving import make_server
    from app import app as flask_app
    import desktop_product

    demo_created = False
    if first_launch:
        try:
            demo_created = desktop_product.seed_demo_bank(bank_dir)
        except Exception:
            logging.getLogger(__name__).exception("写入首次启动示例题库失败，继续启动")
    with _desktop_config_guard(desktop_config_path):
        current_config = _remembered_desktop_config(
            _load_desktop_config(desktop_config_path), bank_dir, subject
        )
        current_config.update({
            "last_version": desktop_product.PRODUCT_VERSION,
        })
        _write_desktop_config(desktop_config_path, current_config)

    server = make_server("127.0.0.1", 0, flask_app, threaded=True)
    server_thread = threading.Thread(
        target=server.serve_forever, name="quizforge-local-http", daemon=True
    )
    server_thread.start()
    logging.getLogger(__name__).info(
        "桌面本地服务已启动：127.0.0.1:%s，题库=%s", server.server_port, bank_dir
    )

    api = DesktopApi(bank_dir, app_data_dir, desktop_config_path, subject, assets_dir)
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    initial_content = "/welcome?demo=1" if first_launch and demo_created else (
        "/welcome" if first_launch else "/"
    )
    # 桌面窗口只加载一次常驻外壳。资料库和普通业务页分别占一个同源 iframe；
    # 顶栏切换时只做显隐，PDF 的 WebView2 阅读实例不会再被整页导航销毁。
    initial_path = "/workspace?path=" + quote(initial_content, safe="")
    window = webview.create_window(
        APP_NAME,
        f"http://127.0.0.1:{server.server_port}{initial_path}",
        js_api=api,
        width=1440,
        height=920,
        min_size=(1024, 680),
        background_color="#f3f6fa",
        frameless=True,
        easy_drag=False,
        shadow=True,
    )
    api._window = window

    try:
        webview.start(
            gui="edgechromium",
            debug=os.environ.get("QUIZFORGE_DEBUG", "") == "1",
            private_mode=False,
            storage_path=str(_webview_storage_dir(app_data_dir, bank_dir)),
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)

    if api.restart_requested:
        _launch_bank_process(api.restart_bank_dir or bank_dir, app_data_dir,
                             api.restart_subject or subject)
    return 0


def _report_fatal_error() -> None:
    """windowed 构建没有控制台，致命错误必须落日志并弹系统消息框。"""
    details = traceback.format_exc()
    try:
        data_dir = _default_app_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "fatal-error.log").write_text(details, encoding="utf-8")
    except OSError:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "QuizForge 启动失败。详细信息已写入用户数据目录的 fatal-error.log。",
            "QuizForge",
            0x10,
        )
    except Exception:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        _report_fatal_error()
        raise SystemExit(1)
