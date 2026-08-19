"""资料库文件管理的安全文件系统操作。

本模块只处理题库根目录内的真实文件，不认识 Flask 请求、历史记录虚拟目录或界面
状态。调用方负责把成功结果同步给已打开的标签页；本模块只在复制会进入题库索引的
题卡 ``.md`` 时生成新的题目 id，普通文档和讲义始终保留原字节。
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import threading
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import filestore


MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown"})
QUESTION_MARKDOWN_EXTENSION = ".md"
DOCUMENT_MARKDOWN_KIND = "document"
PDF_EXTENSIONS = frozenset({".pdf"})
WORD_EXTENSIONS = frozenset({".doc", ".docx"})
IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
})
SUPPORTED_FILE_EXTENSIONS = (
    MARKDOWN_EXTENSIONS | PDF_EXTENSIONS | WORD_EXTENSIONS | IMAGE_EXTENSIONS
)

_INVALID_WINDOWS_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_RE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE,
)
_OPERATION_LOCK = threading.RLock()
_TEMP_PREFIX = ".quizforge-tmp-"
_RESERVED_ROOT_NAMES = frozenset({"_assets", "_handouts", "_backups"})


class LibraryOperationError(ValueError):
    """可直接转换成资料库 JSON 错误响应的业务异常。"""

    def __init__(self, message: str, *, code: str = "invalid_operation",
                 status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class OperationResult:
    path: str
    kind: str
    old_path: str | None = None
    copied: bool = False

    def as_dict(self) -> dict:
        result = {"path": self.path, "kind": self.kind, "copied": self.copied}
        if self.old_path is not None:
            result["old_path"] = self.old_path
        return result


def _validate_name(name: str) -> str:
    value = str(name or "")
    if (not value or value != value.strip() or value in {".", ".."}
            or value.startswith(".") or value.endswith(".")):
        raise LibraryOperationError("名称无效", code="invalid_name")
    if len(value) > 255:
        raise LibraryOperationError("名称不能超过 255 个字符", code="invalid_name")
    if (any(char in _INVALID_WINDOWS_CHARS or ord(char) < 32 for char in value)
            or _WINDOWS_RESERVED_RE.fullmatch(value)):
        raise LibraryOperationError("名称包含 Windows 不支持的字符或保留名称",
                                    code="invalid_name")
    return value


def _relative_parts(raw: str, *, allow_root: bool) -> tuple[str, ...]:
    value = str(raw or "").replace("\\", "/")
    rel = PurePosixPath(value)
    parts = tuple(part for part in rel.parts if part not in ("", "."))
    if rel.is_absolute() or any(part == ".." for part in parts):
        raise LibraryOperationError("资料库路径无效", code="invalid_path")
    if not parts and not allow_root:
        raise LibraryOperationError("不能操作资料库根目录", code="invalid_path")
    for part in parts:
        _validate_name(part)
    return parts


def _ensure_not_reserved(relative_path: str) -> None:
    parts = PurePosixPath(relative_path).parts
    if parts and parts[0].casefold() in _RESERVED_ROOT_NAMES:
        raise LibraryOperationError(
            "保留目录及其内容不能通过资料库通用操作修改",
            code="reserved_path",
        )


def _root_path(root: str | Path) -> Path:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise LibraryOperationError("资料库根目录不存在", code="root_missing", status=404)
    return path


def is_link_or_junction(path: Path) -> bool:
    """符号链接和 Windows junction 都不能参与通用资料库写操作。"""
    try:
        if path.is_symlink():
            return True
        path_check = getattr(path, "is_junction", None)
        if path_check and path_check():
            return True
        os_check = getattr(os.path, "isjunction", None)
        return bool(os_check and os_check(path))
    except OSError:
        return True


def _resolve(root: Path, raw: str, *, allow_root: bool) -> tuple[Path, str]:
    parts = _relative_parts(raw, allow_root=allow_root)
    candidate = root.joinpath(*parts)

    # 资料库列表本来就隐藏符号链接；写接口也不能允许构造请求绕过这一层。
    current = root
    for part in parts:
        current = current / part
        if is_link_or_junction(current):
            raise LibraryOperationError(
                "不支持操作符号链接或目录联接",
                code="symlink_rejected",
            )

    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise LibraryOperationError("资料库路径越界", code="invalid_path")
    rel = PurePosixPath(*parts).as_posix() if parts else ""
    return resolved, rel


def _entry_kind(path: Path) -> str:
    if path.is_dir():
        return "folder"
    if not path.is_file():
        raise LibraryOperationError("文件或文件夹不存在", code="not_found", status=404)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_FILE_EXTENSIONS:
        raise LibraryOperationError("资料库不支持操作该文件类型",
                                    code="unsupported_file")
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown"
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    if suffix in WORD_EXTENSIONS:
        return "word"
    return "image"


def _target_for_name(parent: Path, name: str) -> Path:
    target = (parent / _validate_name(name)).resolve(strict=False)
    if target.parent != parent:
        raise LibraryOperationError("目标名称无效", code="invalid_name")
    return target


def _temporary_path(parent: Path, suffix: str = "") -> Path:
    while True:
        candidate = parent / f"{_TEMP_PREFIX}{uuid.uuid4().hex}{suffix}"
        if not candidate.exists():
            return candidate


def _publish_file(staged: Path, target: Path) -> None:
    """把同目录暂存文件发布到不存在的目标，绝不替换既有文件。"""
    try:
        if os.name == "nt":
            # Windows 的 rename 在目标存在时失败，且同卷内改名是原子的。
            os.rename(staged, target)
        else:
            # POSIX rename 会覆盖既有普通文件；硬链接发布提供原子的 O_EXCL 语义。
            os.link(staged, target)
            staged.unlink()
    except FileExistsError as exc:
        raise LibraryOperationError("目标位置已存在同名项目",
                                    code="conflict", status=409) from exc


def _copy_file_atomic(source: Path, target: Path) -> None:
    staged = _temporary_path(target.parent, target.suffix)
    try:
        shutil.copy2(source, staged)
        _publish_file(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _read_question_markdown(source: Path) -> tuple[MutableMapping, str] | None:
    """读取会进入题库索引的 Markdown；显式文档和讲义不套用题卡复制语义。"""
    try:
        meta, body = filestore._read_raw(source)
    except OSError:
        raise
    except Exception:
        # 无法按题卡解析的 .md 仍是资料库可见的普通文件；原样转移不会制造活跃
        # 题卡 id，且与资料树的“可见即可拖动”语义保持一致。
        return None
    if not isinstance(meta, MutableMapping):
        return None
    if not filestore._is_question_meta(meta):
        return None
    return meta, body


def _render_markdown_copy(source: Path, *, order: float | None = None) -> str | None:
    """生成题卡副本文本，保留正文、图片引用及未知 frontmatter。"""
    try:
        parsed = _read_question_markdown(source)
        if parsed is None:
            return None
        meta, body = parsed
        new_id = filestore._new_id()
        now = filestore._now_iso()
        meta["id"] = new_id
        meta["created"] = now
        meta["updated"] = now
        if order is not None:
            meta["order"] = float(order)
        for key in (
                "_quizforge_import_scope", "_quizforge_import_index",
                "_trash_original_path", "_trash_deleted_at"):
            meta.pop(key, None)
        return filestore._render_raw(meta, body)
    except OSError:
        raise
    except Exception as exc:
        raise LibraryOperationError(
            f"无法安全复制 Markdown：{source.name}",
            code="invalid_markdown",
        ) from exc


def _write_staged_text(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(filestore.normalize_newlines(text))
        handle.flush()
        os.fsync(handle.fileno())


def _next_markdown_order(directory: Path) -> float:
    """直接读取目标目录题卡顺序，避免依赖全局 BANK_DIR 或扫描整座题库。"""
    maximum: float | None = None
    for path in directory.iterdir():
        if (not path.is_file() or is_link_or_junction(path)
                or path.suffix.lower() != QUESTION_MARKDOWN_EXTENSION):
            continue
        try:
            meta, _body = filestore._read_raw(path)
            if not isinstance(meta, MutableMapping):
                continue
            if not filestore._is_question_meta(meta):
                continue
            value = float(meta.get("order", 0.0) or 0.0)
        except Exception:
            # 与题库扫描一致：损坏到无法解析的 Markdown 不参与题卡排序。
            continue
        maximum = value if maximum is None else max(maximum, value)
    return (maximum + 1.0) if maximum is not None else 1.0


def _copy_markdown_atomic(source: Path, target: Path, *, order: float) -> None:
    text = _render_markdown_copy(source, order=order)
    if text is None:
        _copy_file_atomic(source, target)
        return
    staged = _temporary_path(target.parent, target.suffix)
    try:
        _write_staged_text(staged, text)
        _publish_file(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _write_new_markdown(target: Path, text: str) -> None:
    staged = _temporary_path(target.parent, target.suffix)
    try:
        with staged.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(filestore.normalize_newlines(text))
            handle.flush()
            os.fsync(handle.fileno())
        _publish_file(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _render_document_markdown(text: str) -> str:
    """给资料库新建 Markdown 写入文档身份，同时保留有效的自定义 frontmatter。"""
    normalized = filestore.normalize_newlines(text)
    try:
        meta, body = filestore._parse_raw_text(normalized)
    except Exception:
        # YAML 已损坏时不能丢掉用户交来的原文；把它整体放进新文档正文，仍能在
        # 编辑器里看见并修复。
        meta, body = {}, normalized
    if not isinstance(meta, MutableMapping):
        # 非映射 frontmatter 不能承载身份字段，同样按正文原样保留。
        meta, body = {}, normalized
    meta["quizforge_kind"] = DOCUMENT_MARKDOWN_KIND
    return filestore._render_raw(meta, body)


def _copyable_tree_files(source: Path) -> list[Path]:
    """返回普通文件清单；递归前先拒绝链接，避免 copytree 跟进 junction。"""
    files: list[Path] = []

    def scan(directory: Path) -> None:
        if is_link_or_junction(directory):
            raise LibraryOperationError(
                "文件夹中包含符号链接或目录联接，不能递归复制",
                code="symlink_rejected",
            )
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or is_link_or_junction(path):
                    raise LibraryOperationError(
                        "文件夹中包含符号链接或目录联接，不能递归复制",
                        code="symlink_rejected",
                    )
                if entry.is_dir(follow_symlinks=False):
                    scan(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)
                else:
                    raise LibraryOperationError(
                        "文件夹中包含不支持的特殊文件",
                        code="unsupported_file",
                    )

    scan(source)
    return files


def _rewrite_folder_markdown_copies(staged: Path) -> None:
    for path in _copyable_tree_files(staged):
        if path.suffix.lower() != QUESTION_MARKDOWN_EXTENSION:
            continue
        relative = path.relative_to(staged)
        if any(part.startswith(".") or part in _RESERVED_ROOT_NAMES
               for part in relative.parts[:-1]):
            continue
        text = _render_markdown_copy(path)
        if text is None:
            continue
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())


def _copy_folder_atomic(source: Path, target: Path) -> None:
    _copyable_tree_files(source)
    staged = _temporary_path(target.parent)
    try:
        # 链接按链接复制，随后再检查暂存树；即使源目录在检查后发生竞态，也不会
        # 被 copytree 当作普通目录跟随到资料库之外。
        shutil.copytree(
            source, staged, copy_function=shutil.copy2, symlinks=True)
        _rewrite_folder_markdown_copies(staged)
        if target.exists():
            raise LibraryOperationError("目标位置已存在同名项目",
                                        code="conflict", status=409)
        try:
            os.rename(staged, target)
        except FileExistsError as exc:
            raise LibraryOperationError("目标位置已存在同名项目",
                                        code="conflict", status=409) from exc
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def _move_no_overwrite(source: Path, target: Path) -> None:
    if target.exists():
        raise LibraryOperationError("目标位置已存在同名项目",
                                    code="conflict", status=409)
    try:
        if source.is_file() and os.name != "nt":
            # 与复制相同，POSIX 下用 link 保证不会在竞态中替换目标。
            os.link(source, target)
            try:
                source.unlink()
            except Exception:
                target.unlink(missing_ok=True)
                raise
        else:
            os.rename(source, target)
    except FileExistsError as exc:
        raise LibraryOperationError("目标位置已存在同名项目",
                                    code="conflict", status=409) from exc
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # 正常资料库操作不会跨卷；保留明确错误，避免 shutil.move 的隐式复制删除
        # 在失败时留下半份目录。
        raise LibraryOperationError("源文件与目标目录不在同一磁盘，无法安全移动",
                                    code="cross_device") from exc


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _invalidate(*, folder_structure: bool) -> None:
    filestore.invalidate_scan_cache(folder_structure=folder_structure)


def create_folder(root: str | Path, parent_path: str, name: str) -> OperationResult:
    root_path = _root_path(root)
    parent, _ = _resolve(root_path, parent_path, allow_root=True)
    if not parent.is_dir():
        raise LibraryOperationError("目标文件夹不存在", code="not_found", status=404)
    target = _target_for_name(parent, name)
    _ensure_not_reserved(_relative(root_path, target))
    with _OPERATION_LOCK, filestore._write_lock:
        try:
            target.mkdir()
        except FileExistsError as exc:
            raise LibraryOperationError("目标位置已存在同名项目",
                                        code="conflict", status=409) from exc
        _invalidate(folder_structure=True)
    return OperationResult(path=_relative(root_path, target), kind="folder")


def create_markdown(root: str | Path, parent_path: str, name: str,
                    text: str = "") -> OperationResult:
    if not isinstance(text, str):
        raise LibraryOperationError("Markdown 内容无效", code="invalid_content")
    root_path = _root_path(root)
    parent, _ = _resolve(root_path, parent_path, allow_root=True)
    if not parent.is_dir():
        raise LibraryOperationError("目标文件夹不存在", code="not_found", status=404)
    filename = _validate_name(name)
    if not Path(filename).suffix:
        filename += ".md"
    if Path(filename).suffix.lower() not in MARKDOWN_EXTENSIONS:
        raise LibraryOperationError("新建文件必须使用 .md 或 .markdown 扩展名",
                                    code="unsupported_file")
    target = _target_for_name(parent, filename)
    _ensure_not_reserved(_relative(root_path, target))
    with _OPERATION_LOCK, filestore._write_lock:
        if target.exists():
            raise LibraryOperationError("目标位置已存在同名项目",
                                        code="conflict", status=409)
        _write_new_markdown(target, _render_document_markdown(text))
        _invalidate(folder_structure=False)
    return OperationResult(path=_relative(root_path, target), kind="markdown")


def rename_entry(root: str | Path, source_path: str,
                 new_name: str) -> OperationResult:
    root_path = _root_path(root)
    source, old_rel = _resolve(root_path, source_path, allow_root=False)
    _ensure_not_reserved(old_rel)
    kind = _entry_kind(source)
    name = _validate_name(new_name)
    if kind != "folder":
        source_suffix = source.suffix
        requested_suffix = Path(name).suffix
        if not requested_suffix:
            name += source_suffix
        elif requested_suffix.lower() != source_suffix.lower():
            raise LibraryOperationError(
                "重命名不能更改文件扩展名，请使用格式转换功能",
                code="extension_change_rejected",
            )
        else:
            name = name[:-len(requested_suffix)] + source_suffix
    target = _target_for_name(source.parent, name)
    _ensure_not_reserved(_relative(root_path, target))
    if source.name == name:
        return OperationResult(path=old_rel, old_path=old_rel, kind=kind)
    if source == target:
        # resolve() 在 Windows 上可能把纯大小写变化还原成磁盘现有名称；后续两步
        # 改名仍要保留用户请求的拼写。
        target = source.parent / name

    with _OPERATION_LOCK, filestore._write_lock:
        if target.exists() and os.path.normcase(str(target)) != os.path.normcase(str(source)):
            raise LibraryOperationError("目标位置已存在同名项目",
                                        code="conflict", status=409)
        if os.path.normcase(str(target)) == os.path.normcase(str(source)):
            # Windows 大小写不敏感，必须借一个同目录临时名完成纯大小写改名。
            staged = _temporary_path(source.parent, source.suffix)
            os.rename(source, staged)
            try:
                os.rename(staged, target)
            except Exception:
                os.rename(staged, source)
                raise
        else:
            _move_no_overwrite(source, target)
        _invalidate(folder_structure=kind == "folder")
    return OperationResult(path=_relative(root_path, target), old_path=old_rel,
                           kind=kind)


def transfer_entry(root: str | Path, source_path: str, target_folder_path: str,
                   *, copy: bool = False) -> OperationResult:
    root_path = _root_path(root)
    source, old_rel = _resolve(root_path, source_path, allow_root=False)
    _ensure_not_reserved(old_rel)
    target_folder, target_folder_rel = _resolve(
        root_path, target_folder_path, allow_root=True)
    if target_folder_rel:
        _ensure_not_reserved(target_folder_rel)
    kind = _entry_kind(source)
    if not target_folder.is_dir():
        raise LibraryOperationError("目标文件夹不存在", code="not_found", status=404)
    if kind == "folder" and (target_folder == source or source in target_folder.parents):
        raise LibraryOperationError("不能移动或复制到自身的子文件夹",
                                    code="descendant_target")
    target = target_folder / source.name
    _ensure_not_reserved(_relative(root_path, target))

    with _OPERATION_LOCK, filestore._write_lock:
        if target.exists():
            raise LibraryOperationError("目标位置已存在同名项目",
                                        code="conflict", status=409)
        if copy:
            if kind == "folder":
                _copy_folder_atomic(source, target)
            elif (kind == "markdown"
                  and source.suffix.lower() == QUESTION_MARKDOWN_EXTENSION):
                _copy_markdown_atomic(
                    source, target, order=_next_markdown_order(target_folder))
            else:
                _copy_file_atomic(source, target)
        else:
            _move_no_overwrite(source, target)
        _invalidate(folder_structure=kind == "folder")
    return OperationResult(path=_relative(root_path, target), old_path=old_rel,
                           kind=kind, copied=copy)


def move_entry(root: str | Path, source_path: str,
               target_folder_path: str) -> OperationResult:
    return transfer_entry(root, source_path, target_folder_path, copy=False)


def copy_entry(root: str | Path, source_path: str,
               target_folder_path: str) -> OperationResult:
    return transfer_entry(root, source_path, target_folder_path, copy=True)
