"""Agent 上传件的安全暂存。

Agent 可以接收一个包含试卷和图片的 ZIP，但 ZIP 不能直接交给转换线程：归档
成员可能带有目录穿越、符号链接或解压炸弹。这里先在 ``UPLOAD_DIR/agent`` 下
建立隔离暂存区，只发布通过格式和大小检查的试卷文件；后续工作流只应使用
``resolve_stage_file`` 返回的路径，不能接受客户端自行拼接的绝对路径。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import threading
import time
import secrets
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image

import config


class AgentUploadError(ValueError):
    """上传件不满足 Agent 暂存边界。"""

    def __init__(self, message: str, *, code: str = "invalid_upload",
                 status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# 这些文件是常见压缩工具自动写入的元数据，不参与转换；除此之外的未知类型
# 一律拒绝，避免把没有经过内容校验的任意压缩数据落到暂存区。
_IGNORED_PREFIXES = ("__MACOSX/",)
_IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db"})
_STAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,80}$")
_STAGE_TTL_SECONDS = 24 * 60 * 60
_MAX_MEMBERS = 2000
_MAX_UNPACKED_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1000
_CHUNK_SIZE = 1024 * 1024

_lock = threading.RLock()
_stages: dict[str, dict[str, Any]] = {}


def _upload_root() -> Path:
    root = Path(config.UPLOAD_DIR).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _stage_root() -> Path:
    root = (_upload_root() / "agent").resolve()
    if root == _upload_root():
        raise AgentUploadError("Agent 暂存目录无效", code="stage_root_invalid",
                               status=500)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _purge_expired_unlocked(now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = [sid for sid, row in _stages.items()
               if now - float(row.get("created_at", now)) > _STAGE_TTL_SECONDS]
    for sid in expired:
        row = _stages.pop(sid, None)
        if row:
            _remove_stage_dir(row.get("directory"))


def _remove_stage_dir(raw: object) -> None:
    """只删除已经登记在暂存根下的目录；失败时保留现场供清理任务处理。"""
    if not raw:
        return
    try:
        target = Path(str(raw)).resolve()
        root = _stage_root()
        if target == root or root not in target.parents:
            return
        shutil.rmtree(target)
    except (OSError, RuntimeError, AgentUploadError):
        return


def _stream_size(stream) -> int:
    try:
        position = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = int(stream.tell())
        stream.seek(position)
        return size
    except (AttributeError, OSError, ValueError) as exc:
        raise AgentUploadError("无法确认 ZIP 文件大小，请重新选择文件",
                               code="size_unavailable") from exc


def _safe_member_name(raw: str) -> str | None:
    """返回规范化的 POSIX 成员名；自动元数据返回 None。"""
    name = str(raw or "").replace("\\", "/")
    if "\x00" in name:
        raise AgentUploadError("ZIP 成员名称包含非法字符", code="invalid_member")
    if (name in _IGNORED_NAMES
            or any(name.startswith(prefix) for prefix in _IGNORED_PREFIXES)):
        return None
    # 目录项通常以斜杠结尾，去掉后再统一检查；空目录不会进入 manifest。
    name = name.rstrip("/")
    if not name:
        return None
    if re.match(r"^[A-Za-z]:", name) or name.startswith("/"):
        raise AgentUploadError("ZIP 成员不能使用绝对路径", code="path_traversal")
    rel = PurePosixPath(name)
    parts = tuple(part for part in rel.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise AgentUploadError("ZIP 成员路径越界", code="path_traversal")
    if any(part.startswith(".") for part in parts):
        raise AgentUploadError("ZIP 不能包含隐藏目录或文件", code="invalid_member")
    return PurePosixPath(*parts).as_posix()


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    return stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) \
        or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)


def _kind_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix in config.EXAM_IMAGE_EXTS:
        return "image"
    if suffix == ".zip":
        raise AgentUploadError("ZIP 内不能再嵌套 ZIP", code="nested_archive")
    raise AgentUploadError(
        f"ZIP 包含不支持的文件类型：{Path(path).name}",
        code="unsupported_member")


def _role_for_name(path: str) -> str:
    stem = Path(path).stem.casefold()
    if re.search(r"(?:答案|解析|解答|answer|answers|solution|solutions)", stem):
        return "solution"
    return "exam"


def _validate_content(path: Path, kind: str, display_name: str) -> None:
    """对已解压文件做与普通上传一致的最低限度真实内容检查。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AgentUploadError(f"无法读取 ZIP 成员：{display_name}",
                               code="member_unreadable") from exc
    if kind == "image":
        if size > config.MAX_EXAM_IMAGE_BYTES:
            raise AgentUploadError(
                f"图片过大（上限 {config.MAX_EXAM_IMAGE_BYTES // (1024 * 1024)}MB）",
                code="member_too_large")
        try:
            with Image.open(path) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > 40_000_000:
                    raise AgentUploadError("图片像素过大（上限 4000 万像素）",
                                           code="image_pixels_too_large")
                image.verify()
        except AgentUploadError:
            raise
        except Exception as exc:
            raise AgentUploadError(
                f"「{display_name}」不是可解析的图片", code="invalid_image") from exc
        return
    if size > config.MAX_EXAM_DOCUMENT_BYTES:
        raise AgentUploadError(
            f"文档过大（上限 {config.MAX_EXAM_DOCUMENT_BYTES // (1024 * 1024)}MB）",
            code="member_too_large")
    try:
        with path.open("rb") as handle:
            if kind == "pdf":
                if b"%PDF-" not in handle.read(1024):
                    raise AgentUploadError(
                        f"「{display_name}」内容不是 PDF", code="invalid_pdf")
                return
            with zipfile.ZipFile(handle) as document:
                names = {item.filename for item in document.infolist()}
                if ("[Content_Types].xml" not in names
                        or "word/document.xml" not in names):
                    raise AgentUploadError(
                        f"「{display_name}」内容不是 Word 文档", code="invalid_docx")
                infos = document.infolist()
                if (len(infos) > _MAX_MEMBERS
                        or sum(max(0, int(item.file_size)) for item in infos)
                        > _MAX_UNPACKED_BYTES):
                    raise AgentUploadError(
                        "DOCX 解压后过大或内部文件过多", code="docx_too_large")
    except AgentUploadError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise AgentUploadError(
            f"「{display_name}」内容不是 Word 文档", code="invalid_docx") from exc


def _public_stage(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "original_name": row["original_name"],
        "files": [dict(item) for item in row.get("files", [])],
        "ignored": list(row.get("ignored", [])),
        "job_id": row.get("job_id"),
        "created_at": row["created_at"],
        "expires_at": row["created_at"] + _STAGE_TTL_SECONDS,
        "can_start": row.get("status") == "staged" and bool(row.get("files")),
    }


def bind_stage(stage_id: str, session_id: str) -> dict[str, Any]:
    """把暂存包绑定到一个 Agent 会话，避免不同会话互相读取附件。"""
    sid = str(stage_id or "")
    owner = str(session_id or "").strip()
    if not _STAGE_ID_RE.fullmatch(sid):
        raise AgentUploadError("暂存编号无效", code="invalid_stage", status=404)
    if not owner or len(owner) > 160:
        raise AgentUploadError("Agent 会话编号无效", code="invalid_session")
    with _lock:
        _purge_expired_unlocked()
        row = _stages.get(sid)
        if row is None:
            raise AgentUploadError("上传暂存不存在或已过期", code="stage_not_found",
                                   status=404)
        current = str(row.get("session_id") or "")
        if current and current != owner:
            raise AgentUploadError("上传暂存不属于当前 Agent 会话",
                                   code="stage_forbidden", status=403)
        row["session_id"] = owner
        row["updated_at"] = time.time()
        return _public_stage(row)


def mark_stage_started(stage_id: str, session_id: str, job_id: str) -> dict[str, Any]:
    """原子标记暂存包已进入转换任务，防止重复创建 OCR 任务。"""
    sid = str(stage_id or "")
    owner = str(session_id or "").strip()
    jid = str(job_id or "").strip()
    if not _STAGE_ID_RE.fullmatch(sid):
        raise AgentUploadError("暂存编号无效", code="invalid_stage", status=404)
    if not owner or not jid:
        raise AgentUploadError("Agent 暂存任务参数无效", code="invalid_session")
    with _lock:
        _purge_expired_unlocked()
        row = _stages.get(sid)
        if row is None:
            raise AgentUploadError("上传暂存不存在或已过期", code="stage_not_found",
                                   status=404)
        if str(row.get("session_id") or "") != owner:
            raise AgentUploadError("上传暂存不属于当前 Agent 会话",
                                   code="stage_forbidden", status=403)
        if row.get("status") != "staged":
            raise AgentUploadError("上传暂存已经启动过转换", code="stage_started", status=409)
        row["status"] = "started"
        row["job_id"] = jid
        row["updated_at"] = time.time()
        return _public_stage(row)


def stage_zip(storage) -> dict[str, Any]:
    """校验并暂存一个 ZIP，返回不含绝对路径的 manifest。"""
    filename = str(getattr(storage, "filename", "") or "")
    if Path(filename).suffix.lower() != ".zip":
        raise AgentUploadError("这里只接受 ZIP 文件", code="invalid_extension")
    stream = getattr(storage, "stream", None)
    if stream is None:
        raise AgentUploadError("上传流无效", code="invalid_stream")
    size = _stream_size(stream)
    max_archive = min(int(config.MAX_EXAM_DOCUMENT_BYTES),
                      512 * 1024 * 1024)
    if size <= 0 or size > max_archive:
        raise AgentUploadError(
            f"ZIP 文件过大（上限 {max_archive // (1024 * 1024)}MB）",
            code="archive_too_large")
    position = stream.tell()
    stage_id = secrets.token_urlsafe(18)
    stage_dir = (_stage_root() / stage_id).resolve()
    stage_root = _stage_root()
    if stage_root not in stage_dir.parents:
        raise AgentUploadError("Agent 暂存路径无效", code="stage_path_invalid",
                               status=500)
    files: list[dict[str, Any]] = []
    ignored: list[str] = []
    seen: set[str] = set()
    unpacked_total = 0
    try:
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            if not infos:
                raise AgentUploadError("ZIP 文件为空", code="empty_archive")
            if len(infos) > _MAX_MEMBERS:
                raise AgentUploadError(
                    f"ZIP 内文件过多（上限 {_MAX_MEMBERS} 个）",
                    code="too_many_members")
            entries: list[tuple[zipfile.ZipInfo, str, str]] = []
            for info in infos:
                rel = _safe_member_name(info.filename)
                if rel is None:
                    ignored.append(str(info.filename)[:255])
                    continue
                if _is_symlink(info):
                    raise AgentUploadError(
                        f"ZIP 成员不能是符号链接或特殊文件：{rel}",
                        code="symlink_member")
                if info.is_dir():
                    continue
                kind = _kind_for_path(rel)
                if rel in seen:
                    raise AgentUploadError(
                        f"ZIP 内存在重复文件名：{rel}", code="duplicate_member")
                seen.add(rel)
                declared = int(info.file_size)
                compressed = int(info.compress_size)
                if declared < 0 or declared > _MAX_UNPACKED_BYTES:
                    raise AgentUploadError(
                        f"ZIP 成员过大：{rel}", code="member_too_large")
                if declared and (compressed <= 0
                                 or declared > max(1, compressed) * _MAX_COMPRESSION_RATIO):
                    raise AgentUploadError(
                        f"ZIP 成员压缩比异常：{rel}", code="compression_bomb")
                unpacked_total += declared
                if unpacked_total > _MAX_UNPACKED_BYTES:
                    raise AgentUploadError(
                        "ZIP 解压后总量超过安全上限", code="archive_unpacked_too_large")
                entries.append((info, rel, kind))
            if not entries:
                raise AgentUploadError(
                    "ZIP 中没有可识别的 PDF、DOCX 或图片", code="no_supported_member")

            stage_dir.mkdir(parents=True, exist_ok=False)
            for info, rel, kind in entries:
                target = (stage_dir / PurePosixPath(rel)).resolve()
                if target != stage_dir and stage_dir not in target.parents:
                    raise AgentUploadError("ZIP 成员路径越界", code="path_traversal")
                target.parent.mkdir(parents=True, exist_ok=True)
                # 父目录是在本轮刚创建的，仍检查一次，防止异常环境预先放入联接。
                current = stage_dir
                for part in PurePosixPath(rel).parts[:-1]:
                    current = current / part
                    if current.is_symlink():
                        raise AgentUploadError(
                            "暂存路径不能包含符号链接", code="symlink_member")
                copied = 0
                with archive.open(info, "r") as source, target.open("xb") as out:
                    while True:
                        chunk = source.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > int(info.file_size):
                            raise AgentUploadError(
                                f"ZIP 成员实际大小异常：{rel}", code="member_size_mismatch")
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if copied != int(info.file_size):
                    raise AgentUploadError(
                        f"ZIP 成员校验失败：{rel}", code="member_size_mismatch")
                _validate_content(target, kind, rel)
                files.append({
                    "path": rel,
                    "name": Path(rel).name,
                    "kind": kind,
                    "role": _role_for_name(rel),
                    "size": copied,
                })
    except AgentUploadError:
        _remove_stage_dir(stage_dir)
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile,
            zipfile.LargeZipFile) as exc:
        _remove_stage_dir(stage_dir)
        raise AgentUploadError("ZIP 文件损坏或无法读取", code="invalid_archive") from exc
    finally:
        try:
            stream.seek(position)
        except (OSError, ValueError):
            pass

    now = time.time()
    row = {
        "id": stage_id,
        "status": "staged",
        "original_name": Path(filename).name or "上传.zip",
        "directory": str(stage_dir),
        "files": files,
        "ignored": ignored,
        "created_at": now,
    }
    with _lock:
        _purge_expired_unlocked(now)
        _stages[stage_id] = row
        return _public_stage(row)


def stage_file(storage) -> dict[str, Any]:
    """安全暂存单个 PDF/DOCX/图片，等待用户选择识别方案后再启动。

    单文件也使用和 ZIP 相同的 manifest/生命周期，避免上传接口因为文件类型
    不同而出现一条自动启动转换的旁路。
    """
    filename = str(getattr(storage, "filename", "") or "")
    kind = _kind_for_path(Path(filename).name)
    if kind not in {"pdf", "docx", "image"}:
        raise AgentUploadError("仅支持 PDF、DOCX 或图片文件", code="unsupported_file")
    stream = getattr(storage, "stream", None)
    if stream is None:
        raise AgentUploadError("上传流无效", code="invalid_stream")
    size = _stream_size(stream)
    if size <= 0 or size > int(config.MAX_EXAM_DOCUMENT_BYTES):
        raise AgentUploadError(
            f"文件过大（上限 {int(config.MAX_EXAM_DOCUMENT_BYTES) // (1024 * 1024)}MB）",
            code="file_too_large")
    stage_id = secrets.token_urlsafe(18)
    stage_root = _stage_root()
    stage_dir = (stage_root / stage_id).resolve()
    if stage_root not in stage_dir.parents:
        raise AgentUploadError("Agent 暂存路径无效", code="stage_path_invalid", status=500)
    safe_name = Path(filename).name or "上传文件"
    target = (stage_dir / safe_name).resolve()
    if target.parent != stage_dir:
        raise AgentUploadError("上传文件名无效", code="invalid_filename")
    position = stream.tell()
    try:
        stage_dir.mkdir(parents=True, exist_ok=False)
        stream.seek(0)
        with target.open("xb") as out:
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        _validate_content(target, kind, safe_name)
    except AgentUploadError:
        _remove_stage_dir(stage_dir)
        raise
    except (OSError, RuntimeError) as exc:
        _remove_stage_dir(stage_dir)
        raise AgentUploadError("上传文件无法暂存", code="stage_failed") from exc
    finally:
        try:
            stream.seek(position)
        except (OSError, ValueError):
            pass
    now = time.time()
    row = {
        "id": stage_id,
        "status": "staged",
        "original_name": safe_name,
        "directory": str(stage_dir),
        "files": [{"path": safe_name, "name": safe_name, "kind": kind,
                   "role": _role_for_name(safe_name), "size": size}],
        "ignored": [],
        "created_at": now,
    }
    with _lock:
        _purge_expired_unlocked(now)
        _stages[stage_id] = row
        return _public_stage(row)


def get_stage(stage_id: str, session_id: str | None = None) -> dict[str, Any]:
    sid = str(stage_id or "")
    if not _STAGE_ID_RE.fullmatch(sid):
        raise AgentUploadError("暂存编号无效", code="invalid_stage", status=404)
    with _lock:
        _purge_expired_unlocked()
        row = _stages.get(sid)
        if row is None:
            raise AgentUploadError("上传暂存不存在或已过期", code="stage_not_found",
                                   status=404)
        owner = str(session_id or "").strip()
        current = str(row.get("session_id") or "")
        if owner and current != owner:
            raise AgentUploadError("上传暂存不属于当前 Agent 会话",
                                   code="stage_forbidden", status=403)
        return _public_stage(row)


def resolve_stage_file(stage_id: str, relative_path: str,
                       session_id: str | None = None) -> Path:
    """解析 manifest 中已登记的成员，拒绝任意路径读写。"""
    sid = str(stage_id or "")
    rel = str(relative_path or "").replace("\\", "/")
    with _lock:
        _purge_expired_unlocked()
        row = _stages.get(sid)
        if row is None or not _STAGE_ID_RE.fullmatch(sid):
            raise AgentUploadError("上传暂存不存在或已过期", code="stage_not_found",
                                   status=404)
        owner = str(session_id or "").strip()
        current_owner = str(row.get("session_id") or "")
        if owner and current_owner != owner:
            raise AgentUploadError("上传暂存不属于当前 Agent 会话",
                                   code="stage_forbidden", status=403)
        known = {str(item.get("path")): item for item in row.get("files", [])}
        if rel not in known:
            raise AgentUploadError("暂存成员不存在", code="member_not_found", status=404)
        root = Path(row["directory"]).resolve()
        target = (root / PurePosixPath(rel)).resolve()
        if target != root and root not in target.parents or not target.is_file():
            raise AgentUploadError("暂存成员路径无效", code="member_not_found", status=404)
        return target


def discard_stage(stage_id: str, session_id: str | None = None) -> bool:
    sid = str(stage_id or "")
    if not _STAGE_ID_RE.fullmatch(sid):
        raise AgentUploadError("暂存编号无效", code="invalid_stage", status=404)
    with _lock:
        row = _stages.get(sid)
        if row is None:
            return False
        owner = str(session_id or "").strip()
        current_owner = str(row.get("session_id") or "")
        if owner and current_owner != owner:
            raise AgentUploadError("上传暂存不属于当前 Agent 会话",
                                   code="stage_forbidden", status=403)
        _stages.pop(sid, None)
        _remove_stage_dir(row.get("directory"))
        return True


def list_stages() -> list[dict[str, Any]]:
    with _lock:
        _purge_expired_unlocked()
        return [_public_stage(row) for row in _stages.values()]
