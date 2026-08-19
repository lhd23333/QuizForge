"""资料库 PDF 页面工具的纯文件操作。

本模块不认识 Flask、题库路径或后台任务，只负责读取 PDF 并把新产物安全写到
调用方指定的位置。所有页码接口均使用用户可见的 1-based 页码。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import BinaryIO, Iterator

from pypdf import PdfReader, PdfWriter


_TEMP_PREFIX = ".quizforge-pdf-"
_ALLOWED_ROTATIONS = frozenset({0, 90, 180, 270})


class PdfToolError(ValueError):
    """可由应用层转换为明确错误响应的 PDF 工具异常。"""

    def __init__(self, message: str, *, code: str = "invalid_operation",
                 status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _source_path(raw_path: str | os.PathLike[str]) -> Path:
    path = Path(raw_path).expanduser()
    if path.suffix.casefold() != ".pdf":
        raise PdfToolError("只支持 PDF 文件", code="unsupported_file")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PdfToolError(
            "PDF 文件不存在", code="not_found", status=404) from exc
    if not resolved.is_file():
        raise PdfToolError(
            "PDF 文件不存在", code="not_found", status=404)
    return resolved


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right))


def _path_exists(path: Path) -> bool:
    """``lexists`` 同时拦住指向不存在目标的同名符号链接。"""
    return os.path.lexists(path)


def _output_path(
        raw_path: str | os.PathLike[str] | None,
        default_path: Path,
        sources: Iterable[Path],
) -> Path:
    candidate = Path(raw_path).expanduser() if raw_path is not None else default_path
    if candidate.suffix.casefold() != ".pdf":
        raise PdfToolError("输出文件必须使用 .pdf 扩展名", code="invalid_output")
    try:
        target = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PdfToolError("输出路径无效", code="invalid_output") from exc
    if not target.parent.is_dir():
        raise PdfToolError("输出目录不存在", code="output_dir_missing", status=404)
    if any(_same_path(target, source) for source in sources):
        raise PdfToolError(
            "输出必须是新文件，不能覆盖输入 PDF",
            code="output_is_source",
            status=409,
        )
    if _path_exists(target):
        raise PdfToolError(
            "输出位置已存在同名文件", code="conflict", status=409)
    return target


def _reader_from_stream(stream: BinaryIO, label: Path) -> tuple[PdfReader, int]:
    try:
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted:
            raise PdfToolError(
                f"不支持加密 PDF：{label.name}", code="encrypted_pdf")
        page_count = len(reader.pages)
        if page_count < 1:
            raise PdfToolError(
                f"PDF 没有可处理的页面：{label.name}", code="empty_pdf")
        # 逐页读取页面框，确保损坏的页树不会等到发布后才暴露。
        for page in reader.pages:
            float(page.mediabox.width)
            float(page.mediabox.height)
        return reader, page_count
    except PdfToolError:
        raise
    # pypdf 对不同版本、不同损坏位置抛出的异常类型并不完全一致；统一包装，
    # 避免把内部堆栈或半解析对象暴露给应用层。
    except Exception as exc:
        raise PdfToolError(
            f"无法读取有效 PDF：{label.name}", code="invalid_pdf") from exc


@contextmanager
def _open_reader(path: Path) -> Iterator[tuple[PdfReader, int]]:
    try:
        stream = path.open("rb")
    except OSError as exc:
        raise PdfToolError(
            f"无法读取 PDF：{path.name}", code="read_failed") from exc
    try:
        yield _reader_from_stream(stream, path)
    finally:
        stream.close()


def _page_numbers(
        pages: Sequence[int] | Iterable[int],
        page_count: int,
        *,
        allow_duplicates: bool = False,
) -> list[int]:
    values = list(pages)
    if not values:
        raise PdfToolError("至少选择一页", code="empty_pages")
    if any(isinstance(value, bool) or not isinstance(value, int)
           for value in values):
        raise PdfToolError("页码必须是整数", code="invalid_page")
    if any(value < 1 or value > page_count for value in values):
        raise PdfToolError(
            f"页码必须在 1 到 {page_count} 之间", code="page_out_of_range")
    if not allow_duplicates and len(set(values)) != len(values):
        raise PdfToolError("页码不能重复", code="duplicate_page")
    return values


def _copy_metadata(writer: PdfWriter, reader: PdfReader) -> None:
    """元数据损坏不应阻断页面操作，只复制可稳定转成字符串的字段。"""
    try:
        metadata = reader.metadata
        if not metadata:
            return
        cleaned = {
            str(key): str(value)
            for key, value in metadata.items()
            if str(key).startswith("/") and value is not None
        }
        if cleaned:
            writer.add_metadata(cleaned)
    except Exception:
        return


def _temporary_path(parent: Path) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=_TEMP_PREFIX, suffix=".pdf", dir=parent)
        os.close(descriptor)
        return Path(name)
    except OSError as exc:
        raise PdfToolError("无法在输出目录创建暂存文件", code="stage_failed") from exc


def _validate_staged(path: Path, expected_pages: int) -> None:
    with _open_reader(path) as (_reader, page_count):
        if page_count != expected_pages:
            raise PdfToolError(
                "PDF 产物页数校验失败", code="output_validation_failed")


def _stage_writer(writer: PdfWriter, target: Path, expected_pages: int) -> Path:
    staged = _temporary_path(target.parent)
    try:
        with staged.open("wb") as stream:
            writer.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        _validate_staged(staged, expected_pages)
        return staged
    except PdfToolError:
        with suppress(OSError):
            staged.unlink(missing_ok=True)
        raise
    except Exception as exc:
        with suppress(OSError):
            staged.unlink(missing_ok=True)
        raise PdfToolError("PDF 产物写入失败", code="write_failed") from exc
    finally:
        with suppress(Exception):
            writer.close()


def _publish_file(staged: Path, target: Path) -> None:
    """原子发布到不存在的目标，任何平台都不覆盖既有文件。"""
    try:
        if os.name == "nt":
            os.rename(staged, target)
        else:
            os.link(staged, target)
            # 硬链接已经完成发布；暂存文件清理失败不应把已发布的完整文件
            # 报告成失败，finally 还会再次尝试清理。
            with suppress(OSError):
                staged.unlink(missing_ok=True)
    except FileExistsError as exc:
        raise PdfToolError(
            "输出位置已存在同名文件", code="conflict", status=409) from exc
    except OSError as exc:
        raise PdfToolError("PDF 产物发布失败", code="publish_failed") from exc


def _write_output(writer: PdfWriter, target: Path, expected_pages: int) -> Path:
    staged = _stage_writer(writer, target, expected_pages)
    try:
        _publish_file(staged, target)
        return target
    finally:
        staged.unlink(missing_ok=True)


def inspect_pdf(source_path: str | os.PathLike[str]) -> dict:
    """返回 PDF 的基础信息；加密或损坏文件会被明确拒绝。"""
    source = _source_path(source_path)
    with _open_reader(source) as (reader, page_count):
        pages = []
        for number, page in enumerate(reader.pages, start=1):
            pages.append({
                "number": number,
                "width": float(page.mediabox.width),
                "height": float(page.mediabox.height),
                "rotation": int(page.rotation or 0) % 360,
            })
        metadata: dict[str, str] = {}
        try:
            raw_metadata = reader.metadata
            if raw_metadata:
                for key in ("title", "author", "subject", "creator", "producer"):
                    value = getattr(raw_metadata, key, None)
                    if value is not None:
                        metadata[key] = str(value)
        except Exception:
            metadata = {}
    stat = source.stat()
    return {
        "path": str(source),
        "name": source.name,
        "page_count": page_count,
        "size_bytes": stat.st_size,
        "encrypted": False,
        "metadata": metadata,
        "pages": pages,
    }


def merge_pdfs(
        source_paths: Sequence[str | os.PathLike[str]],
        *,
        output_path: str | os.PathLike[str] | None = None,
) -> Path:
    """按输入顺序合并至少两份 PDF。"""
    sources = [_source_path(path) for path in source_paths]
    if len(sources) < 2:
        raise PdfToolError("合并至少需要两份 PDF", code="too_few_inputs")
    target = _output_path(
        output_path,
        sources[0].with_name(f"{sources[0].stem}-合并.pdf"),
        sources,
    )
    writer = PdfWriter()
    expected_pages = 0
    with ExitStack() as stack:
        opened = [stack.enter_context(_open_reader(source)) for source in sources]
        for reader, page_count in opened:
            for page in reader.pages:
                writer.add_page(page)
            expected_pages += page_count
        _copy_metadata(writer, opened[0][0])
        return _write_output(writer, target, expected_pages)


def extract_pages(
        source_path: str | os.PathLike[str],
        pages: Sequence[int] | Iterable[int],
        *,
        output_path: str | os.PathLike[str] | None = None,
) -> Path:
    """按给定顺序提取不重复的页面。"""
    source = _source_path(source_path)
    target = _output_path(
        output_path, source.with_name(f"{source.stem}-提取.pdf"), [source])
    with _open_reader(source) as (reader, page_count):
        numbers = _page_numbers(pages, page_count)
        writer = PdfWriter()
        for number in numbers:
            writer.add_page(reader.pages[number - 1])
        _copy_metadata(writer, reader)
        return _write_output(writer, target, len(numbers))


def reorder_pages(
        source_path: str | os.PathLike[str],
        order: Sequence[int] | Iterable[int],
        *,
        output_path: str | os.PathLike[str] | None = None,
) -> Path:
    """按完整排列重排全部页面，不能遗漏或重复。"""
    source = _source_path(source_path)
    target = _output_path(
        output_path, source.with_name(f"{source.stem}-排序.pdf"), [source])
    with _open_reader(source) as (reader, page_count):
        numbers = _page_numbers(order, page_count)
        if len(numbers) != page_count or set(numbers) != set(range(1, page_count + 1)):
            raise PdfToolError(
                "重排顺序必须完整包含每一页且各出现一次",
                code="incomplete_permutation",
            )
        writer = PdfWriter()
        for number in numbers:
            writer.add_page(reader.pages[number - 1])
        _copy_metadata(writer, reader)
        return _write_output(writer, target, page_count)


def rotate_pages(
        source_path: str | os.PathLike[str],
        pages: Sequence[int] | Iterable[int],
        rotation: int,
        *,
        output_path: str | os.PathLike[str] | None = None,
) -> Path:
    """把指定页面顺时针旋转 0、90、180 或 270 度。"""
    if (isinstance(rotation, bool) or not isinstance(rotation, int)
            or rotation not in _ALLOWED_ROTATIONS):
        raise PdfToolError(
            "旋转角度只能是 0、90、180 或 270 度", code="invalid_rotation")
    source = _source_path(source_path)
    target = _output_path(
        output_path, source.with_name(f"{source.stem}-旋转.pdf"), [source])
    with _open_reader(source) as (reader, page_count):
        numbers = _page_numbers(pages, page_count)
        selected = set(numbers)
        writer = PdfWriter()
        for number, page in enumerate(reader.pages, start=1):
            output_page = writer.add_page(page)
            if number in selected:
                output_page.rotate(rotation)
        _copy_metadata(writer, reader)
        return _write_output(writer, target, page_count)


def _split_ranges(
        ranges: Sequence[tuple[int, int]] | Iterable[tuple[int, int]],
        page_count: int,
) -> list[tuple[int, int]]:
    values = list(ranges)
    if len(values) < 2:
        raise PdfToolError("拆分至少需要两个页段", code="too_few_ranges")
    normalized: list[tuple[int, int]] = []
    for value in values:
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise PdfToolError(
                "拆分页段必须是起止页码", code="invalid_range")
        start, end = value
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int)
                or start < 1 or end < start or end > page_count):
            raise PdfToolError(
                f"拆分页段必须位于 1 到 {page_count} 页且起页不大于止页",
                code="invalid_range",
            )
        normalized.append((start, end))
    expected_start = 1
    for start, end in normalized:
        if start != expected_start:
            raise PdfToolError(
                "拆分页段必须按顺序完整覆盖整份 PDF，不能遗漏或重叠",
                code="incomplete_ranges",
            )
        expected_start = end + 1
    if expected_start != page_count + 1:
        raise PdfToolError(
            "拆分页段必须按顺序完整覆盖整份 PDF，不能遗漏或重叠",
            code="incomplete_ranges",
        )
    return normalized


def _split_name(source: Path, start: int, end: int) -> str:
    page_label = f"第{start}页" if start == end else f"第{start}-{end}页"
    return f"{source.stem}-{page_label}.pdf"


def split_pdf(
        source_path: str | os.PathLike[str],
        ranges: Sequence[tuple[int, int]] | Iterable[tuple[int, int]],
        *,
        output_dir: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """按完整、有序、互不重叠的 1-based 闭区间拆分 PDF。"""
    source = _source_path(source_path)
    directory = (Path(output_dir).expanduser() if output_dir is not None
                 else source.parent)
    try:
        directory = directory.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PdfToolError(
            "输出目录不存在", code="output_dir_missing", status=404) from exc
    if not directory.is_dir():
        raise PdfToolError(
            "输出目录不存在", code="output_dir_missing", status=404)

    with _open_reader(source) as (reader, page_count):
        normalized = _split_ranges(ranges, page_count)
        targets = [
            _output_path(directory / _split_name(source, start, end),
                         directory / _split_name(source, start, end), [source])
            for start, end in normalized
        ]
        if len({_normalized_path_key(path) for path in targets}) != len(targets):
            raise PdfToolError("拆分输出名称重复", code="duplicate_output")

        staged_outputs: list[tuple[Path, Path]] = []
        published: list[Path] = []
        try:
            for (start, end), target in zip(normalized, targets, strict=True):
                writer = PdfWriter()
                for number in range(start, end + 1):
                    writer.add_page(reader.pages[number - 1])
                _copy_metadata(writer, reader)
                staged = _stage_writer(writer, target, end - start + 1)
                staged_outputs.append((staged, target))

            # 先把全部页段写完并验证，再开始发布；发布竞态失败时回滚本轮已发布项。
            for staged, target in staged_outputs:
                _publish_file(staged, target)
                published.append(target)
            return targets
        except Exception:
            for target in reversed(published):
                with suppress(OSError):
                    target.unlink(missing_ok=True)
            raise
        finally:
            for staged, _target in staged_outputs:
                staged.unlink(missing_ok=True)


def _normalized_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))
