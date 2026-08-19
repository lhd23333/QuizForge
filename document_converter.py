"""资料库 DOCX 的按需转换。

转换产物先写入源文件旁的隐藏暂存目录，验证后再发布；目标已存在时一律拒绝，
避免资料库里的原文或用户整理过的媒体被静默覆盖。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import MutableMapping
from pathlib import Path

import config
import filestore


DOCUMENT_KIND = "document"
_STAGING_PREFIX = ".quizforge-convert-"
_CONVERSION_LOCK = threading.RLock()
_PANDOC_TIMEOUT_SECONDS = 300
_WORD_TIMEOUT_SECONDS = 300


class DocumentConversionError(RuntimeError):
    """可直接展示给用户的文档转换错误。"""


class _WordUnavailable(DocumentConversionError):
    """当前机器不能启动 Word COM；调用方可以决定是否走后备方案。"""


def _exists(path: Path) -> bool:
    """包含损坏符号链接在内的占位都算已存在，不能被转换产物覆盖。"""
    return os.path.lexists(str(path))


def _source_docx(raw: str | Path) -> Path:
    candidate = Path(raw).expanduser().absolute()
    suffix = candidate.suffix.lower()
    if suffix == ".doc":
        raise DocumentConversionError(
            "暂不支持旧版 .doc 文件，请先在 Word 中另存为 .docx")
    if suffix != ".docx":
        raise DocumentConversionError("只能转换 .docx 文件")
    if candidate.is_symlink():
        raise DocumentConversionError("不支持转换符号链接指向的 DOCX")
    if not candidate.is_file():
        raise DocumentConversionError("DOCX 文件不存在")
    return candidate.resolve()


def _output_path(source: Path, raw: str | Path | None, suffix: str) -> Path:
    target = (source.with_suffix(suffix) if raw is None
              else Path(raw).expanduser().absolute())
    if target.suffix.lower() != suffix:
        raise DocumentConversionError(f"转换目标必须使用 {suffix} 扩展名")
    if not target.parent.is_dir():
        raise DocumentConversionError("转换目标文件夹不存在")
    if target.parent.is_symlink():
        raise DocumentConversionError("转换目标文件夹不能是符号链接")
    return target.resolve(strict=False)


def _ensure_available(*paths: Path) -> None:
    for path in paths:
        if _exists(path):
            raise DocumentConversionError(f"目标已存在，未执行转换：{path.name}")


def _staging_directory(source: Path) -> Path:
    for _attempt in range(20):
        staging = source.parent / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
        try:
            staging.mkdir(mode=0o700)
            return staging
        except FileExistsError:
            continue
    raise DocumentConversionError("无法建立文档转换暂存目录")


def _process_detail(process: subprocess.CompletedProcess) -> str:
    detail = (process.stderr or process.stdout or "未知错误").strip()
    return detail[-2000:] if len(detail) > 2000 else detail


def _run_pandoc(command: list[str], *, cwd: Path, purpose: str) -> None:
    try:
        process = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=_PANDOC_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise DocumentConversionError("未找到 Pandoc，请检查软件转换环境") from None
    except subprocess.TimeoutExpired:
        raise DocumentConversionError(f"Pandoc {purpose}超时") from None
    except OSError as exc:
        raise DocumentConversionError(f"无法启动 Pandoc：{exc}") from exc
    if process.returncode != 0:
        raise DocumentConversionError(
            f"Pandoc {purpose}失败：{_process_detail(process)}")


def _document_markdown(text: str) -> str:
    """写入普通文档身份；Pandoc 带出的有效自定义 frontmatter 继续保留。"""
    normalized = filestore.normalize_newlines(text)
    try:
        meta, body = filestore._parse_raw_text(normalized)
    except Exception:
        meta, body = {}, normalized
    if not isinstance(meta, MutableMapping):
        meta, body = {}, normalized
    meta["quizforge_kind"] = DOCUMENT_KIND
    return filestore._render_raw(meta, body)


def _validate_media_tree(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        raise DocumentConversionError("Pandoc 生成的媒体目录无效")
    for child in path.rglob("*"):
        if child.is_symlink():
            raise DocumentConversionError("Pandoc 媒体目录中包含不安全的符号链接")


def _publish_file(staged: Path, target: Path) -> None:
    """发布单个文件且不覆盖；暂存与目标同卷，可保持原子性。"""
    try:
        if os.name == "nt":
            os.rename(staged, target)
        else:
            os.link(staged, target)
            staged.unlink()
    except FileExistsError as exc:
        raise DocumentConversionError(
            f"目标已存在，未覆盖：{target.name}") from exc
    except OSError as exc:
        raise DocumentConversionError(
            f"无法发布转换结果 {target.name}：{exc}") from exc


def _publish_markdown(staged_markdown: Path, target: Path,
                      staged_media: Path, media_target: Path) -> None:
    media_published = False
    try:
        if staged_media.exists():
            _validate_media_tree(staged_media)
            if _exists(media_target):
                raise DocumentConversionError(
                    f"媒体目录已存在，未覆盖：{media_target.name}")
            try:
                os.rename(staged_media, media_target)
            except FileExistsError as exc:
                raise DocumentConversionError(
                    f"媒体目录已存在，未覆盖：{media_target.name}") from exc
            except OSError as exc:
                raise DocumentConversionError(
                    f"无法发布媒体目录 {media_target.name}：{exc}") from exc
            media_published = True
        _publish_file(staged_markdown, target)
    except Exception:
        # 媒体目录是本次转换刚发布的；若 Markdown 未能发布，必须一并撤回，避免
        # 用户看到没有正文引用的半份转换结果。
        if media_published and media_target.exists():
            shutil.rmtree(media_target, ignore_errors=True)
        raise


def convert_docx_to_markdown(
        source_path: str | Path,
        output_path: str | Path | None = None) -> Path:
    """把 DOCX 转为同名普通 Markdown，图片提取到 ``<stem>_assets``。"""
    source = _source_docx(source_path)
    target = _output_path(source, output_path, ".md")
    media_target = target.with_name(f"{target.stem}_assets")

    with _CONVERSION_LOCK:
        _ensure_available(target, media_target)
        staging = _staging_directory(source)
        staged_markdown = staging / target.name
        staged_media = staging / media_target.name
        try:
            command = [
                str(config.PANDOC),
                str(source),
                "--from", "docx",
                "--to", "gfm+tex_math_dollars",
                "--wrap=none",
                "--extract-media", media_target.name,
                "--output", str(staged_markdown),
            ]
            _run_pandoc(command, cwd=staging, purpose="转换 Markdown")
            if not staged_markdown.is_file() or staged_markdown.is_symlink():
                raise DocumentConversionError("Pandoc 未生成 Markdown 文件")
            try:
                converted = staged_markdown.read_text(
                    encoding="utf-8", newline="")
            except (OSError, UnicodeError) as exc:
                raise DocumentConversionError(
                    f"Pandoc 生成的 Markdown 无法读取：{exc}") from exc
            staged_markdown.write_text(
                _document_markdown(converted), encoding="utf-8", newline="\n")
            _ensure_available(target, media_target)
            _publish_markdown(
                staged_markdown, target, staged_media, media_target)
            return target
        finally:
            shutil.rmtree(staging, ignore_errors=True)


_WORD_COM_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$source = [Environment]::GetEnvironmentVariable('QUIZFORGE_DOCX_SOURCE')
$target = [Environment]::GetEnvironmentVariable('QUIZFORGE_PDF_TARGET')
$word = $null
$document = $null
$status = 0
$detail = ''
try {
    try {
        $word = New-Object -ComObject Word.Application
    } catch {
        $status = 41
        $detail = "WORD_UNAVAILABLE: $($_.Exception.Message)"
    }
    if ($status -eq 0) {
        $word.Visible = $false
        $word.DisplayAlerts = 0
        $word.ScreenUpdating = $false
        $document = $word.Documents.Open($source, $false, $true, $false)
        $document.ExportAsFixedFormat($target, 17)
    }
} catch {
    $status = 42
    $detail = "WORD_CONVERSION_FAILED: $($_.Exception.Message)"
} finally {
    if ($null -ne $document) {
        try { $document.Close(0) } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) } catch {}
    }
    if ($null -ne $word) {
        try { $word.Quit(0) } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($word) } catch {}
    }
}
if ($detail) { [Console]::Error.WriteLine($detail) }
exit $status
"""


def _running_on_windows() -> bool:
    return os.name == "nt"


def _run_word_com(source: Path, staged_pdf: Path) -> None:
    executable = (shutil.which("powershell.exe")
                  or shutil.which("powershell")
                  or "powershell.exe")
    environment = os.environ.copy()
    environment["QUIZFORGE_DOCX_SOURCE"] = str(source)
    environment["QUIZFORGE_PDF_TARGET"] = str(staged_pdf)
    try:
        process = subprocess.run(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-Command", _WORD_COM_SCRIPT,
            ],
            cwd=str(source.parent),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=_WORD_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise _WordUnavailable("未找到 PowerShell，无法启动 Word COM") from None
    except subprocess.TimeoutExpired:
        raise DocumentConversionError("Word 转换 PDF 超时") from None
    except OSError as exc:
        raise _WordUnavailable(f"无法启动 Word COM：{exc}") from exc
    if process.returncode == 41:
        raise _WordUnavailable(_process_detail(process))
    if process.returncode != 0:
        raise DocumentConversionError(
            f"Word 转换 PDF 失败：{_process_detail(process)}")


def _tool_available(command: str | Path) -> bool:
    value = str(command or "").strip()
    if not value:
        return False
    candidate = Path(value)
    try:
        if candidate.is_file():
            return True
    except OSError:
        pass
    return shutil.which(value) is not None


def _run_pdf_fallback(source: Path, staged_pdf: Path) -> None:
    missing = [
        name for name, command in (
            ("Pandoc", config.PANDOC),
            ("XeLaTeX", config.XELATEX),
        ) if not _tool_available(command)
    ]
    if missing:
        raise DocumentConversionError(
            "Word 不可用，且未找到 " + "、".join(missing)
            + "，无法转换 DOCX 为 PDF")
    command = [
        str(config.PANDOC),
        str(source),
        "--from", "docx",
        "--pdf-engine", str(config.XELATEX),
        "-V", "documentclass=ctexart",
        "--output", str(staged_pdf),
    ]
    _run_pandoc(command, cwd=staged_pdf.parent, purpose="转换 PDF")


def _validate_pdf(path: Path) -> None:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(5)
            handle.seek(max(0, size - 2048))
            tail = handle.read()
    except OSError as exc:
        raise DocumentConversionError(f"转换后的 PDF 无法读取：{exc}") from exc
    if size < 12 or header != b"%PDF-" or b"%%EOF" not in tail:
        raise DocumentConversionError("转换程序生成的 PDF 文件无效")


def convert_docx_to_pdf(
        source_path: str | Path,
        output_path: str | Path | None = None) -> Path:
    """把 DOCX 转为新 PDF；Windows 优先用后台 Word，缺失时回退 Pandoc。"""
    source = _source_docx(source_path)
    target = _output_path(source, output_path, ".pdf")

    with _CONVERSION_LOCK:
        _ensure_available(target)
        staging = _staging_directory(source)
        staged_pdf = staging / target.name
        try:
            word_unavailable = not _running_on_windows()
            if not word_unavailable:
                try:
                    _run_word_com(source, staged_pdf)
                except _WordUnavailable:
                    word_unavailable = True
            if word_unavailable:
                staged_pdf.unlink(missing_ok=True)
                _run_pdf_fallback(source, staged_pdf)
            _validate_pdf(staged_pdf)
            _ensure_available(target)
            _publish_file(staged_pdf, target)
            return target
        finally:
            shutil.rmtree(staging, ignore_errors=True)
