"""带图片的 QuizForge Markdown 导入包校验与暂存。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from PIL import Image, UnidentifiedImageError


SCHEMA_VERSION = 1
CONTRACT = "quizforge-markdown-v1"
MANIFEST_NAME = "quizforge-import.json"
ENTRYPOINT_NAME = "questions.md"
ALLOWED_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
MAX_MANIFEST_BYTES = 64 * 1024
MAX_IMAGE_PIXELS = 80_000_000
MAX_PATH_LENGTH = 512

_STAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MD_IMAGE_RE = re.compile(
    r"!\[([^\]\r\n]*)\]\(\s*(<[^>\r\n]+>|[^)\s\r\n]+)"
    r"(?:\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'))?\s*\)")
_UNSUPPORTED_MD_IMAGE_RE = re.compile(r"!\[[^\]\r\n]*\]\(")
_HTML_IMAGE_RE = re.compile(r"<\s*img\b", re.IGNORECASE)
_lock = threading.RLock()


class ImportBundleError(ValueError):
    """资源包不符合 QuizForge 导入契约。"""


def is_bundle_filename(filename: str) -> bool:
    lowered = str(filename or "").strip().lower()
    return lowered.endswith(".zip") or lowered.endswith(".qfimport.zip")


def _safe_member_name(raw: str, *, directory: bool) -> str:
    if (not raw or "\x00" in raw or "\\" in raw or len(raw) > MAX_PATH_LENGTH
            or raw.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", raw)):
        raise ImportBundleError(f"ZIP 包含不安全路径：{raw!r}")
    stripped = raw[:-1] if directory and raw.endswith("/") else raw
    path = PurePosixPath(stripped)
    if not stripped or any(part in ("", ".", "..") for part in path.parts):
        raise ImportBundleError(f"ZIP 包含不安全路径：{raw!r}")
    normalized = path.as_posix()
    if normalized != stripped:
        raise ImportBundleError(f"ZIP 路径不是规范相对路径：{raw!r}")
    return normalized


def _validate_member_type(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise ImportBundleError(f"ZIP 文件不得加密：{info.filename}")
    if info.compress_type not in ALLOWED_COMPRESSION:
        raise ImportBundleError(f"ZIP 使用了不支持的压缩算法：{info.filename}")
    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type and not (stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode)):
        raise ImportBundleError(f"ZIP 不允许符号链接或特殊文件：{info.filename}")


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo,
                 limit: int, label: str) -> bytes:
    if info.file_size > limit:
        raise ImportBundleError(f"{label}过大（上限 {limit // (1024 * 1024) or 1}MB）")
    try:
        data = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ImportBundleError(f"ZIP 条目读取失败：{info.filename}") from exc
    if len(data) != info.file_size:
        raise ImportBundleError(f"ZIP 条目大小异常：{info.filename}")
    return data


def _validate_image(data: bytes, suffix: str, label: str) -> str:
    expected = {
        ".png": {"PNG"},
        ".jpg": {"JPEG"},
        ".jpeg": {"JPEG"},
        ".webp": {"WEBP"},
        ".bmp": {"BMP"},
    }[suffix]
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            image_format = str(image.format or "").upper()
            if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                raise ImportBundleError(f"图片尺寸异常：{label}")
            image.verify()
    except ImportBundleError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ImportBundleError(f"图片内容损坏或格式伪装：{label}") from exc
    if image_format not in expected:
        raise ImportBundleError(f"图片扩展名与真实格式不符：{label}")
    return {
        "PNG": ".png",
        "JPEG": ".jpg",
        "WEBP": ".webp",
        "BMP": ".bmp",
    }[image_format]


def _reference_path(raw_target: str) -> str:
    target = raw_target[1:-1] if raw_target.startswith("<") else raw_target
    try:
        target = unquote(target, errors="strict")
    except (UnicodeError, ValueError) as exc:
        raise ImportBundleError(f"图片引用编码无效：{raw_target}") from exc
    if (not target or "\\" in target or "?" in target or "#" in target
            or target.startswith(("/", "//"))
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)):
        raise ImportBundleError(f"图片必须使用 assets/ 下的相对路径：{raw_target}")
    path = PurePosixPath(target)
    if any(part in ("", ".", "..") for part in path.parts):
        raise ImportBundleError(f"图片引用路径不安全：{raw_target}")
    normalized = path.as_posix()
    if len(path.parts) < 2 or path.parts[0] != "assets":
        raise ImportBundleError(f"图片必须位于 assets/：{raw_target}")
    if Path(normalized).suffix.lower() not in ALLOWED_IMAGE_EXTS:
        raise ImportBundleError(f"图片类型不受支持：{raw_target}")
    return normalized


def _stage_path(stage_root: Path, stage_id: str) -> Path:
    if not _STAGE_ID_RE.fullmatch(str(stage_id or "")):
        raise ImportBundleError("资源包暂存编号无效")
    root = stage_root.resolve()
    target = (root / stage_id).resolve()
    if target.parent != root:
        raise ImportBundleError("资源包暂存路径越界")
    return target


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_asset_atomic(target: Path, data: bytes) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists():
            raise ImportBundleError(f"资产文件名冲突：{target.name}")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def stage_bundle(payload: bytes, filename: str, *, stage_root: Path,
                 assets_dir: Path, max_bundle_bytes: int,
                 max_files: int, max_uncompressed_bytes: int,
                 max_image_bytes: int, max_markdown_bytes: int,
                 max_compression_ratio: int) -> dict:
    """校验资源包、暂存图片并返回可直接进入现有校对链的 Markdown。"""
    if not is_bundle_filename(filename):
        raise ImportBundleError("请选择 .qfimport.zip 或 .zip 文件")
    if not payload:
        raise ImportBundleError("资源包为空")
    if len(payload) > max_bundle_bytes:
        raise ImportBundleError(
            f"资源包过大（上限 {max_bundle_bytes // (1024 * 1024)}MB）")

    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ImportBundleError("文件不是有效的 ZIP 资源包") from exc

    with archive:
        infos: dict[str, zipfile.ZipInfo] = {}
        folded: set[str] = set()
        file_count = 0
        total_size = 0
        total_compressed = 0
        for info in archive.infolist():
            _validate_member_type(info)
            name = _safe_member_name(info.filename, directory=info.is_dir())
            key = name.casefold()
            if key in folded:
                raise ImportBundleError(f"ZIP 包含重复路径：{name}")
            folded.add(key)
            if info.is_dir():
                continue
            file_count += 1
            if file_count > max_files:
                raise ImportBundleError(f"ZIP 文件数量超过上限 {max_files}")
            total_size += info.file_size
            total_compressed += info.compress_size
            if total_size > max_uncompressed_bytes:
                raise ImportBundleError("ZIP 解压后总量超过安全上限")
            if (info.file_size > 1024 * 1024
                    and info.file_size > max(1, info.compress_size) * max_compression_ratio):
                raise ImportBundleError(f"ZIP 条目压缩比异常：{name}")
            infos[name] = info
        if (total_size > 1024 * 1024
                and total_size > max(1, total_compressed) * max_compression_ratio):
            raise ImportBundleError("ZIP 总压缩比异常，疑似压缩炸弹")

        manifest_info = infos.get(MANIFEST_NAME)
        entry_info = infos.get(ENTRYPOINT_NAME)
        if manifest_info is None or entry_info is None:
            raise ImportBundleError(
                f"ZIP 根目录必须包含 {MANIFEST_NAME} 和 {ENTRYPOINT_NAME}")
        try:
            manifest = json.loads(_read_member(
                archive, manifest_info, MAX_MANIFEST_BYTES, "导入清单").decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ImportBundleError("quizforge-import.json 不是有效的 UTF-8 JSON") from exc
        if (not isinstance(manifest, dict)
                or manifest.get("schema") != SCHEMA_VERSION
                or manifest.get("contract") != CONTRACT
                or manifest.get("entrypoint") != ENTRYPOINT_NAME):
            raise ImportBundleError(
                "导入清单必须声明 schema=1、contract=quizforge-markdown-v1、"
                "entrypoint=questions.md")

        for name in infos:
            if name in {MANIFEST_NAME, ENTRYPOINT_NAME}:
                continue
            if not name.startswith("assets/"):
                raise ImportBundleError(f"ZIP 包含契约外文件：{name}")
            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_IMAGE_EXTS:
                raise ImportBundleError(f"assets/ 中包含不支持的文件：{name}")
            if infos[name].file_size > max_image_bytes:
                raise ImportBundleError(f"图片 {name} 过大")

        try:
            markdown = _read_member(
                archive, entry_info, max_markdown_bytes, "questions.md").decode("utf-8-sig")
        except UnicodeError as exc:
            raise ImportBundleError("questions.md 必须是 UTF-8 文本") from exc
        markdown = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not markdown:
            raise ImportBundleError("questions.md 为空")
        if _HTML_IMAGE_RE.search(markdown):
            raise ImportBundleError("questions.md 不允许使用 HTML 图片标签")

        matches = list(_MD_IMAGE_RE.finditer(markdown))
        without_supported = _MD_IMAGE_RE.sub("", markdown)
        if _UNSUPPORTED_MD_IMAGE_RE.search(without_supported):
            raise ImportBundleError("questions.md 包含不受支持的图片引用语法")

        referenced: dict[str, tuple[bytes, str]] = {}
        for match in matches:
            rel = _reference_path(match.group(2))
            info = infos.get(rel)
            if info is None:
                raise ImportBundleError(f"questions.md 引用了不存在的图片：{rel}")
            if rel not in referenced:
                suffix = Path(rel).suffix.lower()
                data = _read_member(archive, info, max_image_bytes, f"图片 {rel}")
                canonical_suffix = _validate_image(data, suffix, rel)
                referenced[rel] = (data, canonical_suffix)

    stage_id = uuid.uuid4().hex
    stage_root.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = _stage_path(stage_root, stage_id)
    mapping = {}
    planned: dict[str, bytes] = {}
    for rel, (data, suffix) in referenced.items():
        digest = hashlib.sha256(data).hexdigest()
        asset_name = f"qfimport_{stage_id}_{digest[:20]}{suffix}"
        mapping[rel] = asset_name
        planned.setdefault(asset_name, data)

    state = {
        "schema": SCHEMA_VERSION,
        "id": stage_id,
        "status": "writing",
        "source_name": Path(filename).name,
        "created_at": time.time(),
        "created_names": sorted(planned),
        "mapping": mapping,
    }
    with _lock:
        try:
            stage_dir.mkdir()
            _write_json_atomic(stage_dir / "stage.json", state)
            for asset_name, data in planned.items():
                _write_asset_atomic(assets_dir / asset_name, data)
            state["status"] = "ready"
            _write_json_atomic(stage_dir / "stage.json", state)
        except Exception:
            for asset_name in planned:
                target = assets_dir / asset_name
                try:
                    if target.resolve().parent == assets_dir.resolve():
                        target.unlink(missing_ok=True)
                except OSError:
                    pass
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise

    def rewrite(match: re.Match) -> str:
        rel = _reference_path(match.group(2))
        return f"![[{mapping[rel]}]]"

    return {
        "id": stage_id,
        "source_name": Path(filename).name,
        "markdown": _MD_IMAGE_RE.sub(rewrite, markdown),
        "asset_names": sorted(planned),
    }


def get_stage(stage_root: Path, stage_id: str) -> dict:
    stage_dir = _stage_path(stage_root, stage_id)
    path = stage_dir / "stage.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImportBundleError("资源包暂存已过期或损坏") from exc
    if (not isinstance(state, dict) or state.get("schema") != SCHEMA_VERSION
            or state.get("id") != stage_id or state.get("status") != "ready"):
        raise ImportBundleError("资源包暂存状态无效")
    names = state.get("created_names")
    if (not isinstance(names, list)
            or any(Path(str(name)).name != name for name in names)):
        raise ImportBundleError("资源包暂存资产清单无效")
    return state


def rewrite_final_references(text: str, mapping: dict[str, str]) -> str:
    """把 Obsidian 暂存图片名替换为最终摘要名，保留宽度等后缀。"""
    result = str(text or "")
    for stage_name, final_name in mapping.items():
        if (Path(str(stage_name)).name != stage_name
                or Path(str(final_name)).name != final_name):
            raise ImportBundleError("资源包图片改写映射无效")
        result = result.replace(f"![[{stage_name}", f"![[{final_name}")
    return result


@contextmanager
def finalize_stage(stage_root: Path, assets_dir: Path, stage_id: str):
    """将暂存图片固化为内容摘要名，并在调用方失败时回滚新资产。

    上下文期间持有资源包锁，避免两个确认请求同时复用一个尚未完成入库的文件。
    返回的 ``mapping`` 用于把校对表单中的暂存引用改写为稳定引用。
    """
    with _lock:
        state = get_stage(stage_root, stage_id)
        raw_mapping = state.get("mapping")
        if not isinstance(raw_mapping, dict):
            raise ImportBundleError("资源包暂存图片映射无效")
        prefix = f"qfimport_{stage_id}_"
        stage_names = set()
        for raw_name in raw_mapping.values():
            name = str(raw_name)
            if (Path(name).name != name or not name.startswith(prefix)
                    or Path(name).suffix.lower() not in ALLOWED_IMAGE_EXTS):
                raise ImportBundleError("资源包暂存图片映射无效")
            stage_names.add(name)

        assets_root = assets_dir.resolve()
        replacements: dict[str, str] = {}
        created: set[str] = set()
        try:
            for stage_name in sorted(stage_names):
                source = assets_dir / stage_name
                try:
                    resolved_source = source.resolve(strict=True)
                except OSError as exc:
                    raise ImportBundleError("资源包暂存图片已丢失") from exc
                if (resolved_source.parent != assets_root or source.is_symlink()
                        or not resolved_source.is_file()):
                    raise ImportBundleError("资源包暂存图片路径无效")
                try:
                    data = resolved_source.read_bytes()
                except OSError as exc:
                    raise ImportBundleError("资源包暂存图片无法读取") from exc
                suffix = _validate_image(
                    data, Path(stage_name).suffix.lower(), stage_name)
                digest = hashlib.sha256(data).hexdigest()
                final_name = f"qfimport_{digest}{suffix}"
                target = assets_dir / final_name
                if target.exists() or target.is_symlink():
                    try:
                        resolved_target = target.resolve(strict=True)
                    except OSError as exc:
                        raise ImportBundleError("摘要图片路径无效") from exc
                    if (target.is_symlink() or resolved_target.parent != assets_root
                            or not resolved_target.is_file()):
                        raise ImportBundleError("摘要图片路径无效")
                    try:
                        current_digest = hashlib.sha256(
                            resolved_target.read_bytes()).hexdigest()
                    except OSError as exc:
                        raise ImportBundleError("摘要图片无法读取") from exc
                    if current_digest != digest:
                        raise ImportBundleError("摘要图片发生内容冲突")
                else:
                    _write_asset_atomic(target, data)
                    created.add(final_name)
                replacements[stage_name] = final_name
            yield {
                "mapping": replacements,
                "created_names": sorted(created),
            }
        except BaseException:
            for name in created:
                target = assets_dir / name
                try:
                    if (not target.is_symlink()
                            and target.resolve().parent == assets_root):
                        target.unlink(missing_ok=True)
                except OSError:
                    pass
            raise


def discard_stage(stage_root: Path, assets_dir: Path, stage_id: str) -> set[str]:
    """移除暂存元数据，返回应交给全库引用检查的资产候选。"""
    stage_dir = _stage_path(stage_root, stage_id)
    prefix = f"qfimport_{stage_id}_"
    names: set[str] = set()
    try:
        state = json.loads((stage_dir / "stage.json").read_text(encoding="utf-8"))
        for raw in state.get("created_names") or []:
            name = str(raw)
            if Path(name).name == name and name.startswith(prefix):
                names.add(name)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    if assets_dir.is_dir():
        for path in assets_dir.glob(prefix + "*"):
            if path.is_file() and path.parent.resolve() == assets_dir.resolve():
                names.add(path.name)
    with _lock:
        if stage_dir.exists() and not stage_dir.is_symlink():
            shutil.rmtree(stage_dir, ignore_errors=True)
    return names


def cleanup_stale_stages(stage_root: Path, assets_dir: Path, *,
                         max_age_seconds: int, now: float | None = None) -> set[str]:
    """清理过期暂存，并返回可按题库引用关系回收的图片名。"""
    if not stage_root.is_dir():
        return set()
    cutoff = (time.time() if now is None else float(now)) - max_age_seconds
    candidates: set[str] = set()
    for child in list(stage_root.iterdir()):
        if child.is_symlink() or not child.is_dir() or not _STAGE_ID_RE.fullmatch(child.name):
            continue
        try:
            expired = child.stat().st_mtime < cutoff
        except OSError:
            continue
        if expired:
            candidates.update(discard_stage(stage_root, assets_dir, child.name))
    try:
        stage_root.rmdir()
    except OSError:
        pass
    return candidates
