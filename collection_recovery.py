"""合集漏题的局部 MinerU 重识别辅助。

本模块只做可复现、可证明的机械步骤，不调用网络：

1. 读取 MinerU ``*_model.json`` 的页内文本与归一化坐标；
2. 用现有合集结构规则建立标题单元，同时保留不依赖标题的全局题号锚点；
3. 从原 Markdown 单元中的前后已识别题块提取正文签名，唯一定位布局锚点；
4. 把“前一题起点到后一题起点前”导出为局部 PDF，交给调用方再次送 MinerU；
5. 对局部 OCR 结果做严格验收，并安全归并其中引用的图片。

任何一步证据不唯一都会拒绝。这里宁可留下显式缺口，也不按页码、坐标或题目
内容猜测；因此同一份源 PDF 与 model.json 能稳定复现相同裁片。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import threading
import unicodedata

from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

import collection_structure
import mechfix


MAX_MISSING_NUMBERS = 4
MAX_PAGES_PER_GAP = 3
_MIN_ANCHOR_CHARS = 12
_ANCHOR_PREFIX_CHARS = 64
_MIN_VISIBLE_CHARS = 20

# 整本 PDF 的文本层只用于把“当前题干”唯一定位到某一页，绝不拿来生成题目正文。
# 同一批合集会并发转换几十个子组；缓存页签名可避免每个异常题都重新解析百页 PDF。
_PAGE_TEXT_CACHE: dict[tuple[str, int, int], tuple[str, ...]] = {}
_PAGE_TEXT_CACHE_LOCK = threading.Lock()
_PAGE_TEXT_CACHE_MAX = 8

_SKIP_MODEL_TYPES = {
    "ocr_text", "inline_formula", "interline_formula", "page_number",
    "footer", "header",
}
_MD_IMAGE_RE = re.compile(
    r"(?P<head>!\[[^\]]*\]\(\s*<?(?:\./)?images/)"
    r"(?P<path>[^)\s>]+)(?P<tail>>?\s*\))",
    re.IGNORECASE,
)
_HTML_IMAGE_RE = re.compile(
    r"(?P<head><img\b[^>]*\bsrc\s*=\s*['\"](?:\./)?images/)"
    r"(?P<path>[^'\"]+)(?P<tail>['\"][^>]*>)",
    re.IGNORECASE,
)
_QUESTION_HEAD_RE = re.compile(
    r"^\s*(?:>\s*)?(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:"
    r"(?P<arabic>\d{1,3})\s*[.．、]|"
    r"第\s*(?P<di>\d{1,3})\s*[题題]\s*[.．、:：]?"
    r")\s*",
)
_OPTION_RE = re.compile(r"(?<![A-Za-z])([A-DＡ-Ｄ])\s*[.．、)）]")
_CHOICE_WORD_RE = re.compile(
    r"(?:下列|以下).{0,30}(?:正确|错误|符合|不符合|可能|不可能)|"
    r"(?:正确|错误|可能|不可能)的是\s*[（(]?\s*[）)]?"
)

# 局部整段已经由前后题锚点限定；有些版面送 MinerU 时仍会漏掉位于页面中部的
# 题号行。二次裁片只从顶部缩窄、底部始终保留到原后一题锚点，因此一旦重新检出
# 缺题题号，正文尾部仍是完整的，不会把半道题当成恢复结果。
_VERTICAL_SUFFIX_TRIMS = (0.35, 0.20, 0.50)


class CollectionRecoveryError(ValueError):
    """局部恢复证据不足或输入不安全。消息可直接用于任务日志。"""


@dataclass(frozen=True)
class LayoutLine:
    """model.json 中一个可定位的文本行。"""

    page_index: int
    order: int
    kind: str
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class LayoutQuestion:
    """一个题号起点及其到下一题号前的布局正文。"""

    number: int
    page_index: int
    order: int
    bbox: tuple[float, float, float, float]
    text: str


@dataclass(frozen=True)
class LayoutUnit:
    """由中文结构标题确认的 model.json 布局单元。"""

    title: str
    topic: str
    ordinal: int | None
    start_order: int
    lines: tuple[LayoutLine, ...]
    questions: tuple[LayoutQuestion, ...]


@dataclass(frozen=True)
class LayoutDocument:
    """model.json 的机械布局索引。"""

    page_count: int
    lines: tuple[LayoutLine, ...]
    questions: tuple[LayoutQuestion, ...]
    units: tuple[LayoutUnit, ...]


@dataclass(frozen=True)
class PageSlice:
    """一页中按顶部坐标表达的裁切区间，左右始终取整页。"""

    page_index: int
    top: float
    bottom: float


@dataclass(frozen=True)
class GapCropPlan:
    """一个连续缺号段的可证明裁切计划。"""

    unit_title: str
    missing_numbers: tuple[int, ...]
    previous_number: int
    next_number: int
    slices: tuple[PageSlice, ...]


@dataclass(frozen=True)
class RecoveryCrop:
    """已经写出的局部 PDF 与其来源计划。"""

    plan: GapCropPlan
    path: Path


def _pdf_page_keys(source_pdf: str | Path) -> tuple[str, ...]:
    """读取并缓存 PDF 每页的无标点文本键，仅供页定位。"""
    source = Path(source_pdf).resolve()
    if not source.is_file() or source.is_symlink():
        raise CollectionRecoveryError("合集源 PDF 不存在或不是普通文件")
    stat = source.stat()
    cache_key = (str(source), int(stat.st_size), int(stat.st_mtime_ns))
    with _PAGE_TEXT_CACHE_LOCK:
        cached = _PAGE_TEXT_CACHE.get(cache_key)
        if cached is not None:
            return cached
        try:
            reader = PdfReader(str(source))
            page_keys = tuple(_key(page.extract_text() or "")
                              for page in reader.pages)
        except Exception as exc:
            raise CollectionRecoveryError(
                f"无法读取合集源 PDF 文本层：{exc}") from exc
        if not page_keys:
            raise CollectionRecoveryError("合集源 PDF 没有页面")
        # 同一路径内容变化后只保留新键，避免长期运行时缓存旧大文件。
        for old_key in [key for key in _PAGE_TEXT_CACHE
                        if key[0] == str(source) and key != cache_key]:
            _PAGE_TEXT_CACHE.pop(old_key, None)
        _PAGE_TEXT_CACHE[cache_key] = page_keys
        while len(_PAGE_TEXT_CACHE) > _PAGE_TEXT_CACHE_MAX:
            _PAGE_TEXT_CACHE.pop(next(iter(_PAGE_TEXT_CACHE)))
        return page_keys


def locate_unique_question_page(source_pdf: str | Path,
                                question_markdown: str) -> int:
    """用题干长签名唯一定位源 PDF 页，返回从 0 开始的页码。

    标题、题号和选项都不是硬前提。函数从题干前段抽取多组 32—64 字滑窗；只要
    至少一组在整本中唯一出现，且所有唯一证据都指向同一页，才接受定位。文本层
    仅给出页码，最终题面仍必须来自后续强制 MinerU OCR。
    """
    signature = _signature(question_markdown, limit=192)
    cjk_signature = "".join(
        char for char in unicodedata.normalize("NFKC", _strip_question_head(
            question_markdown or ""))
        if "\u3400" <= char <= "\u9fff"
    )[:192]
    if max(len(signature), len(cjk_signature)) < 32:
        raise CollectionRecoveryError("题干签名不足 32 字，不能唯一定位源 PDF 页")
    page_keys = _pdf_page_keys(source_pdf)
    page_cjk_keys = tuple(
        "".join(char for char in page_key if "\u3400" <= char <= "\u9fff")
        for page_key in page_keys)
    windows: list[tuple[str, tuple[str, ...]]] = []
    for value, haystacks in ((signature, page_keys),
                             (cjk_signature, page_cjk_keys)):
        for length in (64, 48, 40, 32):
            if len(value) < length:
                continue
            max_start = min(len(value) - length, 96)
            for start in range(0, max_start + 1, 16):
                window = value[start:start + length]
                pair = (window, haystacks)
                if pair not in windows:
                    windows.append(pair)
    unique_pages: set[int] = set()
    for window, haystacks in windows:
        hits = [index for index, page_key in enumerate(haystacks)
                if window in page_key]
        if len(hits) == 1:
            unique_pages.add(hits[0])
    if len(unique_pages) != 1:
        raise CollectionRecoveryError(
            f"题干长签名在源 PDF 中唯一定位到 {len(unique_pages)} 个不同页面，"
            "拒绝按猜测页码重识别")
    return next(iter(unique_pages))


def export_pdf_page(source_pdf: str | Path, page_index: int,
                    output_pdf: str | Path) -> Path:
    """原样导出一页供强制 OCR，采用临时文件原子替换并回读校验。"""
    source = Path(source_pdf).resolve()
    target = Path(output_pdf).resolve(strict=False)
    if not source.is_file() or source.is_symlink():
        raise CollectionRecoveryError("合集源 PDF 不存在或不是普通文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise CollectionRecoveryError("局部 PDF 输出目录不能是符号链接")
    try:
        reader = PdfReader(str(source))
        if page_index < 0 or page_index >= len(reader.pages):
            raise CollectionRecoveryError("局部重识别页码超出源 PDF 范围")
        writer = PdfWriter()
        writer.add_page(reader.pages[page_index])
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=".tmp", dir=target.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with temp_path.open("wb") as stream:
                writer.write(stream)
            if len(PdfReader(str(temp_path)).pages) != 1:
                raise CollectionRecoveryError("局部 PDF 写出后页数校验失败")
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
    except CollectionRecoveryError:
        raise
    except Exception as exc:
        raise CollectionRecoveryError(f"无法导出局部重识别页面：{exc}") from exc
    return target


def export_horizontal_prefix_crop(source_pdf: str | Path, right_ratio: float,
                                  output_pdf: str | Path) -> Path:
    """保留单页 PDF 的左侧前缀，供已证明的“左文右图”版面再次 OCR。

    这里只执行调用方已经由 MinerU 坐标推导出的横向裁切，不自行猜测栏宽。来源
    必须恰好一页且无旋转；裁切比例也限制在保守范围内，避免把任意 PDF 当作可
    安全分栏的页面。写出仍采用临时文件原子替换并回读校验。
    """
    source = Path(source_pdf).resolve()
    target = Path(output_pdf).resolve(strict=False)
    try:
        ratio = float(right_ratio)
    except (TypeError, ValueError) as exc:
        raise CollectionRecoveryError("左栏裁切比例无效") from exc
    if not 0.50 <= ratio <= 0.75:
        raise CollectionRecoveryError("左栏裁切比例超出安全范围")
    if not source.is_file() or source.is_symlink():
        raise CollectionRecoveryError("左栏裁切来源不存在或不是普通文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise CollectionRecoveryError("左栏裁切输出目录不能是符号链接")
    temp_path: Path | None = None
    try:
        reader = PdfReader(str(source))
        if len(reader.pages) != 1:
            raise CollectionRecoveryError("左栏裁切只接受单页 PDF")
        source_page = reader.pages[0]
        if int(source_page.rotation or 0) % 360:
            raise CollectionRecoveryError("带旋转的页面不能安全换算左栏坐标")
        writer = PdfWriter()
        page = writer.add_page(source_page)
        media = source_page.mediabox
        left = float(media.left)
        base = float(media.bottom)
        right = left + float(media.width) * ratio
        top = base + float(media.height)
        rectangle = RectangleObject((left, base, right, top))
        page.mediabox = rectangle
        page.cropbox = RectangleObject((left, base, right, top))
        with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".choice_column_", suffix=".pdf",
                delete=False) as stream:
            temp_path = Path(stream.name)
            writer.write(stream)
        check = PdfReader(str(temp_path))
        if len(check.pages) != 1:
            raise CollectionRecoveryError("左栏 PDF 写出后页数校验失败")
        expected_width = float(media.width) * ratio
        if abs(float(check.pages[0].mediabox.width) - expected_width) > 0.5:
            raise CollectionRecoveryError("左栏 PDF 写出后宽度校验失败")
        os.replace(temp_path, target)
        temp_path = None
    except CollectionRecoveryError:
        raise
    except Exception as exc:
        raise CollectionRecoveryError(f"无法导出左栏重识别页面：{exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return target


def _norm_bbox(value) -> tuple[float, float, float, float] | None:
    """只接受 MinerU model.json 的 0—1 归一化矩形。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        return None
    return box


def _scaled_bbox(value, width: float, height: float
                 ) -> tuple[float, float, float, float] | None:
    """把 content_list/layout 的页面坐标缩放到 0—1。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
        width = float(width)
        height = float(height)
    except (TypeError, ValueError):
        return None
    if (width <= 0 or height <= 0 or not
            (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height)):
        return None
    return x0 / width, y0 / height, x1 / width, y1 / height


def _model_pages(payload) -> list[list[dict]]:
    """兼容 MinerU 当前页数组，以及外层带 ``pages`` 的等价封装。"""
    pages = payload.get("pages") if isinstance(payload, dict) else payload
    if not isinstance(pages, list):
        raise CollectionRecoveryError("MinerU model.json 顶层不是页面数组")
    output: list[list[dict]] = []
    for index, page in enumerate(pages):
        if isinstance(page, dict):
            page = page.get("blocks") or page.get("layout_dets")
        if not isinstance(page, list):
            raise CollectionRecoveryError(
                f"MinerU model.json 第 {index + 1} 页不是布局块数组")
        output.append(page)
    return output


def _model_text(block: dict) -> str:
    value = block.get("content")
    if not isinstance(value, str):
        value = block.get("text")
    return value if isinstance(value, str) else ""


def _nested_content_text(value) -> str:
    """提取 content_list_v2 的正文叶子，跳过图片路径等元数据。"""
    parts: list[str] = []

    def _walk(node, key: str = "") -> None:
        if isinstance(node, str):
            if not node.strip():
                return
            if key == "html":
                text = html.unescape(re.sub(r"<[^>]+>", " ", node))
                if text.strip():
                    parts.append(text)
            elif (key in {"content", "text", "math_content"}
                  or key.endswith("_content")):
                parts.append(node)
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, key)
            return
        if not isinstance(node, dict):
            return
        for child_key, child in node.items():
            if child_key in {
                    "type", "path", "image_source", "math_type",
                    "table_type", "table_nest_level", "level"}:
                continue
            _walk(child, child_key)

    _walk(value)
    return "\n".join(part for part in parts if part.strip())


def _append_layout_text(lines: list[LayoutLine], *, page_index: int,
                        kind: str, text: str, bbox, order: int
                        ) -> int:
    """把一个视觉块展开成共享坐标的非空文本行。"""
    if bbox is None:
        return order
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for raw_line in normalized.splitlines():
        if raw_line.strip():
            lines.append(LayoutLine(
                page_index, order, kind, raw_line, bbox))
            order += 1
    return order


def _read_v1_content_list(payload: list[dict]
                          ) -> tuple[int, list[LayoutLine]]:
    page_indices = [item.get("page_idx") for item in payload
                    if isinstance(item, dict)]
    if (not page_indices
            or any(not isinstance(index, int) or index < 0
                   for index in page_indices)):
        raise CollectionRecoveryError("MinerU content_list 缺少合法页码")
    lines: list[LayoutLine] = []
    order = 0
    for block in payload:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind in _SKIP_MODEL_TYPES or kind in {"page_footer", "page_header"}:
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        order = _append_layout_text(
            lines, page_index=block["page_idx"], kind=kind, text=text,
            bbox=_scaled_bbox(block.get("bbox"), 1000, 1000), order=order)
    if not lines:
        raise CollectionRecoveryError("MinerU content_list 没有可定位的文本布局块")
    return max(page_indices) + 1, lines


def _layout_span_text(line: dict) -> str:
    spans = line.get("spans") if isinstance(line, dict) else None
    if not isinstance(spans, list):
        return ""
    return "".join(
        str(span.get("content") or "")
        for span in spans if isinstance(span, dict)
    )


def _read_layout_json(payload: dict) -> tuple[int, list[LayoutLine]]:
    pages = payload.get("pdf_info")
    if not isinstance(pages, list) or not pages:
        raise CollectionRecoveryError("MinerU layout.json 缺少 pdf_info 页面数组")
    lines: list[LayoutLine] = []
    order = 0
    for fallback_index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise CollectionRecoveryError("MinerU layout.json 页面格式无效")
        page_index = page.get("page_idx", fallback_index)
        size = page.get("page_size")
        if (not isinstance(page_index, int) or page_index < 0
                or not isinstance(size, (list, tuple)) or len(size) != 2):
            raise CollectionRecoveryError("MinerU layout.json 页码或尺寸无效")
        try:
            width, height = float(size[0]), float(size[1])
        except (TypeError, ValueError) as exc:
            raise CollectionRecoveryError(
                "MinerU layout.json 页面尺寸无效") from exc

        def _visit(blocks) -> None:
            nonlocal order
            if not isinstance(blocks, list):
                return
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = str(block.get("type") or "")
                if kind in _SKIP_MODEL_TYPES or kind in {
                        "page_footer", "page_header"}:
                    continue
                visual_lines = block.get("lines")
                if isinstance(visual_lines, list) and visual_lines:
                    for visual_line in visual_lines:
                        if not isinstance(visual_line, dict):
                            continue
                        text = _layout_span_text(visual_line)
                        box = _scaled_bbox(
                            visual_line.get("bbox"), width, height)
                        order = _append_layout_text(
                            lines, page_index=page_index, kind=kind,
                            text=text, bbox=box, order=order)
                else:
                    _visit(block.get("blocks"))

        # preproc_blocks 与 para_blocks 是同一正文的两个阶段，读取两者会重复题号。
        _visit(page.get("para_blocks"))
    if not lines:
        raise CollectionRecoveryError("MinerU layout.json 没有可定位的文本布局块")
    return len(pages), lines


def _read_layout_lines(model_json: str | Path) -> tuple[int, list[LayoutLine]]:
    path = Path(model_json)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionRecoveryError(f"无法读取 MinerU 布局 JSON：{exc}") from exc
    if isinstance(payload, dict) and isinstance(payload.get("pdf_info"), list):
        return _read_layout_json(payload)
    if (isinstance(payload, list) and payload
            and all(isinstance(item, dict) and "page_idx" in item
                    for item in payload)):
        return _read_v1_content_list(payload)
    pages = _model_pages(payload)
    lines: list[LayoutLine] = []
    order = 0
    for page_index, blocks in enumerate(pages):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "")
            if kind in _SKIP_MODEL_TYPES:
                continue
            box = (_norm_bbox(block.get("bbox"))
                   or _scaled_bbox(block.get("bbox"), 1000, 1000))
            text = _model_text(block)
            if not text and isinstance(block.get("content"), dict):
                text = _nested_content_text(block["content"])
            order = _append_layout_text(
                lines, page_index=page_index, kind=kind, text=text,
                bbox=box, order=order)
    if not lines:
        raise CollectionRecoveryError("MinerU model.json 没有可定位的文本布局块")
    return len(pages), lines


def _question_contexts(lines: list[LayoutLine]) -> list[LayoutQuestion]:
    hits: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        number = collection_structure._question_number(line.text)
        if isinstance(number, int) and 1 <= number <= 300:
            hits.append((index, number))
    questions: list[LayoutQuestion] = []
    for hit_index, (line_index, number) in enumerate(hits):
        end = hits[hit_index + 1][0] if hit_index + 1 < len(hits) else len(lines)
        start_line = lines[line_index]
        text = "\n".join(line.text for line in lines[line_index:end]).strip()
        questions.append(LayoutQuestion(
            number, start_line.page_index, start_line.order,
            start_line.bbox, text,
        ))
    return questions


def _layout_units(lines: list[LayoutLine],
                  questions: list[LayoutQuestion]) -> list[LayoutUnit]:
    """用现有标题+连续题号规则建单元；标题不全时保留全局索引继续工作。"""
    raw = "\n".join(line.text for line in lines)
    try:
        markdown_units = collection_structure.split_markdown_units(
            raw, label="MinerU 布局")
    except collection_structure.CollectionStructureError:
        return []

    output: list[LayoutUnit] = []
    for index, unit in enumerate(markdown_units):
        start_index = unit.start_line - 1
        end_index = (markdown_units[index + 1].start_line - 1
                     if index + 1 < len(markdown_units) else len(lines))
        unit_lines = tuple(lines[start_index:end_index])
        if not unit_lines:
            continue
        start_order = unit_lines[0].order
        end_order = (lines[end_index].order if end_index < len(lines)
                     else lines[-1].order + 1)
        unit_questions = tuple(
            question for question in questions
            if start_order <= question.order < end_order)
        output.append(LayoutUnit(
            unit.title, unit.topic, unit.ordinal, start_order,
            unit_lines, unit_questions,
        ))
    return output


def load_layout_document(model_json: str | Path) -> LayoutDocument:
    """读取 MinerU model/content_list/layout JSON 并建立统一布局索引。"""
    page_count, lines = _read_layout_lines(model_json)
    questions = _question_contexts(lines)
    return LayoutDocument(
        page_count, tuple(lines), tuple(questions),
        tuple(_layout_units(lines, questions)),
    )


def layout_reference_score(document: LayoutDocument, markdown: str
                           ) -> tuple[int, int]:
    """返回布局对原 Markdown 题块的（唯一命中数，歧义命中数）。"""
    unique = 0
    ambiguous = 0
    for number, blocks in _markdown_question_blocks(markdown).items():
        candidates = [question for question in document.questions
                      if question.number == number]
        for block in blocks:
            signature = _signature(block)
            if len(signature) < _MIN_ANCHOR_CHARS:
                continue
            matches = [candidate for candidate in candidates
                       if _signature(candidate.text).startswith(signature)]
            if len(matches) == 1:
                unique += 1
            elif len(matches) > 1:
                ambiguous += 1
    return unique, ambiguous


def load_layout_units(model_json: str | Path) -> tuple[LayoutUnit, ...]:
    """只需要标题布局单元时的便捷入口。"""
    return load_layout_document(model_json).units


def _key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def _markdown_question_blocks(markdown: str) -> dict[int, list[str]]:
    lines = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    hits: list[tuple[int, int]] = []
    fenced = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        number = collection_structure._question_number(line)
        if isinstance(number, int) and 1 <= number <= 300:
            hits.append((index, number))
    output: dict[int, list[str]] = {}
    for hit_index, (line_index, number) in enumerate(hits):
        end = hits[hit_index + 1][0] if hit_index + 1 < len(hits) else len(lines)
        output.setdefault(number, []).append(
            "\n".join(lines[line_index:end]).strip())
    return output


def _strip_question_head(text: str) -> str:
    lines = (text or "").splitlines()
    if not lines:
        return ""
    lines[0] = _QUESTION_HEAD_RE.sub("", lines[0], count=1)
    return "\n".join(lines)


def _signature(text: str, *, limit: int = _ANCHOR_PREFIX_CHARS) -> str:
    """生成不受换行、Markdown 标记与图片文件名影响的正文签名。"""
    value = _strip_question_head(text)
    value = _MD_IMAGE_RE.sub(" ", value)
    value = _HTML_IMAGE_RE.sub(" ", value)
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value)
    return value[:limit]


def _match_title_unit(document: LayoutDocument,
                      unit: collection_structure.MarkdownUnit
                      ) -> LayoutUnit | None:
    exact = [candidate for candidate in document.units
             if _key(candidate.title) == _key(unit.title)]
    if len(exact) == 1:
        return exact[0]
    fallback = [candidate for candidate in document.units
                if candidate.ordinal == unit.ordinal
                and _key(candidate.topic) == _key(unit.topic)]
    return fallback[0] if len(fallback) == 1 else None


def _unique_question(candidates: list[LayoutQuestion], number: int,
                     markdown_block: str, *, role: str) -> LayoutQuestion:
    """按正文前缀全局唯一定位；短文本由调用方改走标题单元证据。"""
    signature = _signature(markdown_block)
    if len(signature) < _MIN_ANCHOR_CHARS:
        raise CollectionRecoveryError(
            f"第 {number} 题{role}正文过短，不能在 model.json 中唯一定位")
    matches = [candidate for candidate in candidates
               if candidate.number == number
               and _signature(candidate.text).startswith(signature)]
    if len(matches) != 1:
        raise CollectionRecoveryError(
            f"第 {number} 题{role}正文签名在 model.json 中匹配到 {len(matches)} 处，"
            "无法证明裁切位置唯一")
    return matches[0]


def _resolve_anchor(document: LayoutDocument, layout_unit: LayoutUnit | None,
                    number: int, markdown_block: str, *, role: str
                    ) -> LayoutQuestion:
    # 先用正文签名做全局唯一匹配，解析册标题缺失时也能工作。只有题块本身短到
    # 不足以形成签名时，才允许退到“标题单元内题号唯一”这一条独立强证据。
    try:
        return _unique_question(
            list(document.questions), number, markdown_block, role=role)
    except CollectionRecoveryError as signature_error:
        if len(_signature(markdown_block)) >= _MIN_ANCHOR_CHARS or layout_unit is None:
            raise signature_error
        matches = [candidate for candidate in layout_unit.questions
                   if candidate.number == number]
        if len(matches) != 1:
            raise signature_error
        return matches[0]


def _missing_runs(missing_numbers, *, max_missing_numbers: int
                  ) -> list[tuple[int, ...]]:
    try:
        values = sorted({int(number) for number in missing_numbers})
    except (TypeError, ValueError) as exc:
        raise CollectionRecoveryError("缺失题号必须是整数") from exc
    if not values:
        raise CollectionRecoveryError("没有提供缺失题号")
    if any(number < 1 or number > 300 for number in values):
        raise CollectionRecoveryError("缺失题号必须在 1—300 之间")
    if len(values) > max_missing_numbers:
        raise CollectionRecoveryError(
            f"一次最多恢复 {max_missing_numbers} 个缺失题号，当前为 {len(values)} 个")
    runs: list[list[int]] = []
    for number in values:
        if not runs or number != runs[-1][-1] + 1:
            runs.append([number])
        else:
            runs[-1].append(number)
    return [tuple(run) for run in runs]


def _page_slices(previous: LayoutQuestion, following: LayoutQuestion, *,
                 allow_cross_page: bool,
                 max_pages_per_gap: int) -> tuple[PageSlice, ...]:
    if previous.order >= following.order:
        raise CollectionRecoveryError("前后题布局顺序倒置，无法证明裁切区间")
    if previous.page_index == following.page_index:
        top, bottom = previous.bbox[1], following.bbox[1]
        if top >= bottom:
            raise CollectionRecoveryError("同页前后题纵向坐标倒置，拒绝裁切")
        return (PageSlice(previous.page_index, top, bottom),)
    if not allow_cross_page:
        raise CollectionRecoveryError("前后题不在同一页，当前策略拒绝跨页裁切")
    if previous.page_index > following.page_index:
        raise CollectionRecoveryError("前后题页序倒置，拒绝跨页裁切")
    page_count = following.page_index - previous.page_index + 1
    if page_count > max_pages_per_gap:
        raise CollectionRecoveryError(
            f"局部区间跨 {page_count} 页，超过上限 {max_pages_per_gap} 页")
    slices = [PageSlice(previous.page_index, previous.bbox[1], 1.0)]
    for page in range(previous.page_index + 1, following.page_index):
        slices.append(PageSlice(page, 0.0, 1.0))
    if following.bbox[1] <= 0:
        raise CollectionRecoveryError("后一题位于跨页页首，末页没有可裁内容")
    slices.append(PageSlice(following.page_index, 0.0, following.bbox[1]))
    return tuple(slices)


def plan_gap_crops(document: LayoutDocument,
                   markdown_unit: collection_structure.MarkdownUnit,
                   missing_numbers, *,
                   previous_unit: collection_structure.MarkdownUnit | None = None,
                   next_unit: collection_structure.MarkdownUnit | None = None,
                   max_missing_numbers: int = MAX_MISSING_NUMBERS,
                   allow_cross_page: bool = False,
                   max_pages_per_gap: int = MAX_PAGES_PER_GAP,
                   replace_existing: bool = False,
                   ) -> tuple[GapCropPlan, ...]:
    """为一个 Markdown 单元的连续缺号段生成布局裁切计划。

    每段必须同时拥有前一题和后一题。优先用两侧题块正文在全书布局中唯一匹配；
    标题可靠时，短题块也可由“标题单元 + 题号唯一”定位。跨页默认关闭，调用方
    显式开启后仍限制页数，并按首/中/末页布局坐标裁切。
    """
    if max_missing_numbers < 1 or max_pages_per_gap < 1:
        raise CollectionRecoveryError("恢复数量与页数上限必须为正整数")
    blocks = _markdown_question_blocks(markdown_unit.markdown)
    layout_unit = _match_title_unit(document, markdown_unit)
    plans: list[GapCropPlan] = []
    for run in _missing_runs(
            missing_numbers, max_missing_numbers=max_missing_numbers):
        if not replace_existing and any(number in blocks for number in run):
            raise CollectionRecoveryError(
                f"题号 {run} 在 Markdown 单元中并未缺失，拒绝重复恢复")
        previous_number, next_number = run[0] - 1, run[-1] + 1
        previous_blocks = blocks.get(previous_number, [])
        next_blocks = blocks.get(next_number, [])
        previous_layout_unit = layout_unit
        next_layout_unit = layout_unit
        # 单元首尾缺题没有同单元的双侧锚点。调用方若能提供相邻 MarkdownUnit，
        # 允许用上一单元最后一题／下一单元第一题作为纯“裁切边界”；题号本身不
        # 参与猜测，正文仍须在整本 model.json 中唯一匹配。这样解析册最后一道
        # 答案漏号也能恢复，而不需要写死下一专题的页码。
        previous_anchor_number = previous_number
        next_anchor_number = next_number
        if not previous_blocks and run[0] == 1 and previous_unit is not None:
            adjacent = _markdown_question_blocks(previous_unit.markdown)
            ordered = [number for number in previous_unit.question_numbers
                       if number in adjacent]
            if ordered:
                previous_anchor_number = ordered[-1]
                previous_blocks = adjacent[previous_anchor_number]
                previous_layout_unit = _match_title_unit(document, previous_unit)
        if previous_anchor_number < 1 or not previous_blocks:
            raise CollectionRecoveryError(
                "第 1 题缺失且没有上一单元的末题锚点，不能自动裁切")
        if not next_blocks and next_unit is not None:
            adjacent = _markdown_question_blocks(next_unit.markdown)
            ordered = [number for number in next_unit.question_numbers
                       if number in adjacent]
            if ordered:
                next_anchor_number = ordered[0]
                next_blocks = adjacent[next_anchor_number]
                next_layout_unit = _match_title_unit(document, next_unit)
        if len(previous_blocks) != 1 or len(next_blocks) != 1:
            raise CollectionRecoveryError(
                f"缺号段 {run[0]}—{run[-1]} 的前后题块不唯一")
        previous = _resolve_anchor(
            document, previous_layout_unit, previous_anchor_number,
            previous_blocks[0], role="前锚点")
        following = _resolve_anchor(
            document, next_layout_unit, next_anchor_number,
            next_blocks[0], role="后锚点")
        plans.append(GapCropPlan(
            markdown_unit.title, run, previous_anchor_number, next_anchor_number,
            _page_slices(
                previous, following,
                allow_cross_page=allow_cross_page,
                max_pages_per_gap=max_pages_per_gap,
            ),
        ))
    return tuple(plans)


def _safe_filename(value: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z\u3400-\u9fff._-]+", "_", value).strip("._")
    return clean[:60] or "unit"


def export_gap_crops(source_pdf: str | Path, plans, output_dir: str | Path
                     ) -> tuple[RecoveryCrop, ...]:
    """按计划导出局部 PDF；每一页只用 model.json 推导出的纵向坐标。"""
    source = Path(source_pdf)
    target_root = Path(output_dir)
    target_root.mkdir(parents=True, exist_ok=True)
    if target_root.is_symlink():
        raise CollectionRecoveryError("局部 PDF 输出目录不能是符号链接")
    reader = PdfReader(str(source))
    output: list[RecoveryCrop] = []
    for plan in plans:
        writer = PdfWriter()
        for part in plan.slices:
            if part.page_index < 0 or part.page_index >= len(reader.pages):
                raise CollectionRecoveryError(
                    f"model.json 第 {part.page_index + 1} 页超出源 PDF 页数")
            source_page = reader.pages[part.page_index]
            if int(source_page.rotation or 0) % 360:
                raise CollectionRecoveryError(
                    f"源 PDF 第 {part.page_index + 1} 页带旋转，当前不能安全换算坐标")
            page = writer.add_page(source_page)
            media = source_page.mediabox
            left, right = float(media.left), float(media.right)
            base, height = float(media.bottom), float(media.height)
            lower = base + (1.0 - part.bottom) * height
            upper = base + (1.0 - part.top) * height
            if lower >= upper:
                raise CollectionRecoveryError("裁切区间高度不为正")
            rectangle = RectangleObject((left, lower, right, upper))
            page.mediabox = rectangle
            page.cropbox = RectangleObject((left, lower, right, upper))

        first, last = plan.missing_numbers[0], plan.missing_numbers[-1]
        number_label = str(first) if first == last else f"{first}-{last}"
        name = f"{_safe_filename(plan.unit_title)}_缺题{number_label}.pdf"
        target = target_root / name
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                    dir=target_root, prefix=".recovery_", suffix=".pdf",
                    delete=False) as stream:
                temp_path = Path(stream.name)
                writer.write(stream)
            check = PdfReader(str(temp_path))
            if len(check.pages) != len(plan.slices):
                raise CollectionRecoveryError("局部 PDF 写出后页数校验失败")
            os.replace(temp_path, target)
            temp_path = None
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        output.append(RecoveryCrop(plan, target))
    return tuple(output)


def export_vertical_suffix_crops(
        source_pdf: str | Path, output_dir: str | Path,
        trims: tuple[float, ...] = _VERTICAL_SUFFIX_TRIMS,
        ) -> tuple[Path, ...]:
    """把单页局部裁片机械缩窄成若干“保留下半段”的二次裁片。

    只处理已经由 :func:`export_gap_crops` 生成的单页安全区间。所有变体都保留
    原裁片底边（即后一题起点），只移动顶边；这样缩窄版即使改善了 MinerU 的
    版面识别，也不会截掉待恢复题目的末尾。多页、旋转页或异常比例一律拒绝。
    """
    source = Path(source_pdf)
    target_root = Path(output_dir)
    if source.is_symlink() or not source.is_file():
        raise CollectionRecoveryError("二次裁片来源不存在或不是普通文件")
    target_root.mkdir(parents=True, exist_ok=True)
    if target_root.is_symlink():
        raise CollectionRecoveryError("二次裁片输出目录不能是符号链接")
    if (not trims or any(
            not isinstance(value, (int, float))
            or not 0 < float(value) < 0.80 for value in trims)):
        raise CollectionRecoveryError("二次裁片顶部缩减比例必须位于 0—80% 之间")

    reader = PdfReader(str(source))
    if len(reader.pages) != 1:
        raise CollectionRecoveryError("二次裁片只支持单页局部区间")
    source_page = reader.pages[0]
    if int(source_page.rotation or 0) % 360:
        raise CollectionRecoveryError("二次裁片来源页带旋转，拒绝换算坐标")
    media = source_page.mediabox
    left, right = float(media.left), float(media.right)
    base, top = float(media.bottom), float(media.top)
    height = top - base
    if right <= left or height <= 0:
        raise CollectionRecoveryError("二次裁片来源页面尺寸无效")

    output: list[Path] = []
    for index, raw_trim in enumerate(trims, 1):
        trim = float(raw_trim)
        upper = top - trim * height
        if upper <= base:
            raise CollectionRecoveryError("二次裁片高度不为正")
        writer = PdfWriter()
        page = writer.add_page(source_page)
        rectangle = RectangleObject((left, base, right, upper))
        page.mediabox = rectangle
        page.cropbox = RectangleObject((left, base, right, upper))
        target = target_root / f"refined_{index:02d}.pdf"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                    dir=target_root, prefix=".refined_", suffix=".pdf",
                    delete=False) as stream:
                temp_path = Path(stream.name)
                writer.write(stream)
            check = PdfReader(str(temp_path))
            if len(check.pages) != 1:
                raise CollectionRecoveryError("二次裁片写出后页数校验失败")
            os.replace(temp_path, target)
            temp_path = None
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        output.append(target)
    return tuple(output)


def create_gap_recovery_crops(
        source_pdf: str | Path, model_json: str | Path,
        markdown_unit: collection_structure.MarkdownUnit, missing_numbers,
        output_dir: str | Path, *,
        previous_unit: collection_structure.MarkdownUnit | None = None,
        next_unit: collection_structure.MarkdownUnit | None = None,
        max_missing_numbers: int = MAX_MISSING_NUMBERS,
        allow_cross_page: bool = False,
        max_pages_per_gap: int = MAX_PAGES_PER_GAP,
        replace_existing: bool = False,
        ) -> tuple[RecoveryCrop, ...]:
    """读取布局、规划并写出局部 PDF 的一站式入口。"""
    document = load_layout_document(model_json)
    plans = plan_gap_crops(
        document, markdown_unit, missing_numbers,
        previous_unit=previous_unit,
        next_unit=next_unit,
        max_missing_numbers=max_missing_numbers,
        allow_cross_page=allow_cross_page,
        max_pages_per_gap=max_pages_per_gap,
        replace_existing=replace_existing,
    )
    return export_gap_crops(source_pdf, plans, output_dir)


def _visible_body(text: str) -> tuple[int, int]:
    value = _strip_question_head(text)
    image_count = len(_MD_IMAGE_RE.findall(value)) + len(_HTML_IMAGE_RE.findall(value))
    value = _MD_IMAGE_RE.sub(" ", value)
    value = _HTML_IMAGE_RE.sub(" ", value)
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    visible = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", value)
    return len(visible), image_count


def normalize_recovered_question_heads(local_markdown: str, expected_numbers,
                                        *, content_role: str) -> str:
    """补回 MinerU 粘在上一段末尾、但证据充分的解析题号换行。

    这里只处理调用方已经由版面缺口确定的题号，并要求标记在当前局部裁片中恰好
    出现一次。允许的两种题首都很强：``11. B【详解】``，或计算/实验解析常见的
    ``16. (1)``。普通正文里的小数、公式序号和“第 11 点”不会命中。

    调用方应一次传入前锚点和全部缺题；否则先单独取前锚点、再补缺题换行，会让
    已取出的前锚点仍吞着缺题正文，最终形成题号完整但正文重复的假成功。
    """
    if content_role not in {"stem", "solution"}:
        raise ValueError("content_role 必须是 stem 或 solution")
    if content_role != "solution":
        return local_markdown
    try:
        numbers = tuple(sorted({int(number) for number in expected_numbers}))
    except (TypeError, ValueError) as exc:
        raise CollectionRecoveryError("预期题号必须是整数") from exc
    if not numbers or any(number < 1 or number > 300 for number in numbers):
        raise CollectionRecoveryError("预期题号必须在 1—300 之间且不能为空")
    insertions: set[int] = set()
    for number in numbers:
        answer_head = (
            r"(?:(?:[A-DＡ-Ｄ](?:[ \t,，、/]*[A-DＡ-Ｄ]){0,3})"
            r"[ \t]*)?【(?:答案|详解|解析)】")
        multipart_head = (r"[（(][ \t]*1[ \t]*[)）]"
                          if number - 1 in numbers else r"(?!)")
        pattern = re.compile(
            rf"(?<![\d.．])(?P<head>{number}[ \t]*[.．、][ \t]*(?:"
            rf"{answer_head}|{multipart_head}))"
        )
        matches = list(pattern.finditer(local_markdown))
        if len(matches) != 1:
            continue
        match = matches[0]
        line_start = local_markdown.rfind("\n", 0, match.start()) + 1
        if local_markdown[line_start:match.start()].strip():
            insertions.add(match.start())
    for index in sorted(insertions, reverse=True):
        local_markdown = (
            local_markdown[:index].rstrip()
            + "\n\n" + local_markdown[index:].lstrip())
    return local_markdown


def trim_trailing_next_unit_title(local_markdown: str,
                                  next_unit_title: str | None) -> str:
    """移除局部裁片末尾唯一、精确对应下一单元的强标题。

    最后一题的安全裁片会延伸到下一单元标题以证明题尾完整；该标题不是当前题正文。
    只有标题解析为强候选、规范化后与调用方已确认的下一单元标题相同，且标题后仅
    有空白时才移除。普通解析小标题或正文中的书名号不会命中。
    """
    if not (next_unit_title or "").strip():
        return local_markdown
    lines = (local_markdown or "").replace(
        "\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    expected_key = _key(next_unit_title or "")
    matches: list[int] = []
    for index, line in enumerate(lines):
        candidate = collection_structure._candidate(line.rstrip("\n"), index)
        if (candidate is not None and candidate.strong
                and _key(candidate.title) == expected_key):
            matches.append(index)
    if len(matches) != 1:
        return local_markdown
    index = matches[0]
    if "".join(lines[index + 1:]).strip():
        return local_markdown
    return "".join(lines[:index]).rstrip()


def _comparison_key_with_positions(text: str) -> tuple[str, tuple[int, ...]]:
    """生成正文比对键，并保留每个字符在原 Markdown 中的位置。"""
    value = text or ""
    # 图片文件名与 HTML 属性不是题目正文，且可能含长摘要；用等长空格遮掉，
    # 保持后续位置仍能无损映射回原 Markdown。
    html_tag = re.compile(
        r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^>\n]*)?>")
    for pattern in (_MD_IMAGE_RE, _HTML_IMAGE_RE, html_tag):
        value = pattern.sub(lambda match: " " * len(match.group(0)), value)
    # \left / \right 只控制括号尺寸，不改变公式含义；MinerU 在行内式与独立
    # 公式间经常一边保留、一边省略。等长遮掉后，公式正文才能形成稳定边界。
    value = re.sub(
        r"\\(?:left|right|displaystyle|textstyle|scriptstyle|scriptscriptstyle)\b",
        lambda match: " " * len(match.group(0)), value)
    chars: list[str] = []
    positions: list[int] = []
    for raw_index, raw_char in enumerate(value):
        normalized = unicodedata.normalize("NFKC", raw_char).lower()
        for char in normalized:
            if re.fullmatch(r"[0-9a-z\u3400-\u9fff]", char):
                chars.append(char)
                positions.append(raw_index)
    return "".join(chars), tuple(positions)


def trim_swallowed_solution_suffix(anchor: str, recovered: str, *,
                                    anchor_number: int) -> str:
    """从前锚点末尾机械剥离已由缺题恢复结果覆盖的重复正文。

    该函数只解决一种已观测到的 MinerU 版面错误：题号行被漏掉，后续解析正文被
    并入上一题。必须同时满足长且唯一的逐字重合、重合位于锚点后段与恢复题前段、
    被删除后缀绝大部分可由恢复正文解释，且后缀没有图片。任一证据不足即拒绝，
    宁可让本组显式失败，也不静默删错上一题。
    """
    body = _strip_question_head(recovered)
    anchor_key, anchor_positions = _comparison_key_with_positions(anchor)
    recovered_key, _ = _comparison_key_with_positions(body)
    if len(anchor_key) < 80 or len(recovered_key) < 80:
        raise CollectionRecoveryError("缩窄裁片正文不足，不能证明前锚点重复后缀")

    matcher = SequenceMatcher(None, anchor_key, recovered_key, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    longest = max(blocks, key=lambda block: block.size, default=None)
    if longest is None or longest.size < 64 or longest.b > 64:
        raise CollectionRecoveryError("缩窄裁片与前锚点没有足够长的唯一正文重合")
    seed_length = min(64, longest.size)
    seed_offset = (longest.size - seed_length) // 2
    seed = recovered_key[
        longest.b + seed_offset:longest.b + seed_offset + seed_length]
    if anchor_key.count(seed) != 1 or recovered_key.count(seed) != 1:
        raise CollectionRecoveryError("缩窄裁片与前锚点的正文重合不唯一")

    # 最长块可能从“解得……”才开始；直接在那里切会把目标题的首个独立公式
    # 留给上一题。改取恢复题前 64 个可见字符内最早的唯一公共块作为边界，
    # 再用整段后缀覆盖率证明这个较早边界确实属于同一道题。
    boundary_candidates = [
        block for block in blocks
        if block.size >= 24 and block.b <= 64
        and block.a >= max(32, len(anchor_key) // 5)
    ]
    if not boundary_candidates:
        raise CollectionRecoveryError("无法唯一定位被吞解析的起始公式或正文")
    boundary = min(boundary_candidates, key=lambda block: (block.b, block.a))
    boundary_seed_length = min(48, boundary.size)
    boundary_seed = recovered_key[
        boundary.b:boundary.b + boundary_seed_length]
    if (anchor_key.count(boundary_seed) != 1
            or recovered_key.count(boundary_seed) != 1):
        raise CollectionRecoveryError("被吞解析的起始正文映射不唯一")
    if boundary.a < max(32, len(anchor_key) // 5):
        raise CollectionRecoveryError("疑似重复正文不在前锚点后段，拒绝裁掉")
    if boundary.b > 64:
        raise CollectionRecoveryError("前锚点重合未覆盖恢复题开头，拒绝裁掉")

    anchor_suffix = anchor_key[boundary.a:]
    recovered_suffix = recovered_key[boundary.b:]
    suffix_matcher = SequenceMatcher(
        None, anchor_suffix, recovered_suffix, autojunk=False)
    matched = sum(block.size for block in suffix_matcher.get_matching_blocks())
    coverage = matched / max(1, len(anchor_suffix))
    similarity = suffix_matcher.ratio()
    if coverage < 0.90 or similarity < 0.90:
        raise CollectionRecoveryError("前锚点后缀不能由恢复题正文充分解释")

    raw_cut = anchor_positions[boundary.a]
    removed = anchor[raw_cut:]
    if _MD_IMAGE_RE.search(removed) or _HTML_IMAGE_RE.search(removed):
        raise CollectionRecoveryError("疑似重复后缀含图片，无法证明图片归属")
    cleaned = anchor[:raw_cut].rstrip()
    blocks = _markdown_question_blocks(cleaned)
    if set(blocks) != {anchor_number} or len(blocks[anchor_number]) != 1:
        raise CollectionRecoveryError("剥离重复后缀后前锚点题号结构异常")
    _validate_recovered_block(
        cleaned, anchor_number, _MIN_VISIBLE_CHARS, content_role="solution")
    combined_key, _ = _comparison_key_with_positions(cleaned + "\n" + recovered)
    if combined_key.count(seed) != 1:
        raise CollectionRecoveryError("剥离后恢复题长签名仍然重复")
    return cleaned


def _validate_recovered_block(text: str, number: int,
                              min_visible_chars: int, *,
                              content_role: str) -> None:
    visible_chars, image_count = _visible_body(text)
    if visible_chars < min_visible_chars and not (
            image_count and visible_chars >= 8):
        raise CollectionRecoveryError(
            f"局部 OCR 的第 {number} 题可见正文不足，拒绝自动插入")
    if content_role == "solution":
        # 解析本来就只含答案与推导，不应重复 A-D 选项。其完整性仍由正文长度、
        # 唯一题号、前锚相似度以及最终逐题配对四道门共同保证。
        return
    normalized = mechfix.normalize_embedded_choice_labels(text)
    labels = [label.translate(str.maketrans("ＡＢＣＤ", "ABCD"))
              for label in _OPTION_RE.findall(normalized)]
    label_set = set(labels)
    prompt = re.split(r"【(?:答案|详解|解析)】", normalized, maxsplit=1)[0]
    choice_phrase = bool(_CHOICE_WORD_RE.search(prompt))
    choice_like = (
        mechfix.looks_like_choice_options(normalized)
        or len(label_set) >= 3
        or (choice_phrase and (
            bool(label_set) or mechfix.has_choice_answer_blank(prompt)))
    )
    if choice_like and not mechfix.has_complete_choice_options(normalized):
        found = "".join(sorted(set(labels))) or "无"
        raise CollectionRecoveryError(
            f"局部 OCR 的第 {number} 题疑似选择题，但只检出选项 {found}")


def select_recovered_questions(local_markdown: str, expected_numbers, *,
                               min_visible_chars: int = _MIN_VISIBLE_CHARS,
                               content_role: str = "stem",
                               ) -> dict[int, str]:
    """从局部 OCR Markdown 中选择全部预期题块，证据不足时整体拒绝。"""
    if content_role not in {"stem", "solution"}:
        raise ValueError("content_role 必须是 stem 或 solution")
    expected = _missing_runs(
        # 集成层会把前锚点与最多 MAX_MISSING_NUMBERS 道缺题
        # 一起验收；锚点不是额外的“缺题”，因而总数允许多 1。
        expected_numbers, max_missing_numbers=MAX_MISSING_NUMBERS + 1)
    numbers = tuple(number for run in expected for number in run)
    local_markdown = normalize_recovered_question_heads(
        local_markdown, numbers, content_role=content_role)
    blocks = _markdown_question_blocks(local_markdown)
    selected: dict[int, str] = {}
    for number in numbers:
        matches = blocks.get(number, [])
        if len(matches) != 1:
            raise CollectionRecoveryError(
                f"局部 OCR 中第 {number} 题检出 {len(matches)} 次，必须恰好一次")
        _validate_recovered_block(
            matches[0], number, min_visible_chars,
            content_role=content_role)
        selected[number] = matches[0]
    return selected


def select_recovered_question(local_markdown: str, expected_number: int, *,
                              min_visible_chars: int = _MIN_VISIBLE_CHARS,
                              content_role: str = "stem") -> str:
    """单个缺失题号的便捷入口。"""
    return select_recovered_questions(
        local_markdown, [expected_number],
        min_visible_chars=min_visible_chars,
        content_role=content_role,
    )[expected_number]


def validate_recovered_anchor(original: str, recovered: str, number: int) -> None:
    """确认局部 OCR 的前锚点仍是原题，而不是同号的其他题。

    裁片位置已经由 model.json 中的唯一正文签名确定；这里再以
    局部 OCR 的文字做第二道独立验收。允许 OCR 少量不同，但不允许核心
    签名整段换成另一道题。
    """
    left = _signature(original, limit=96)
    right = _signature(recovered, limit=96)
    if len(left) < _MIN_ANCHOR_CHARS or len(right) < _MIN_ANCHOR_CHARS:
        raise CollectionRecoveryError(
            f"局部 OCR 的第 {number} 题前锚点正文过短，拒绝替换")
    common_prefix = os.path.commonprefix((left, right))
    similarity = SequenceMatcher(None, left, right).ratio()
    if len(common_prefix) < _MIN_ANCHOR_CHARS and similarity < 0.72:
        raise CollectionRecoveryError(
            f"局部 OCR 的第 {number} 题与原前锚点不一致，拒绝替换")


def _safe_image_name(raw_name: str) -> str:
    if "%" in raw_name or "\\" in raw_name:
        raise CollectionRecoveryError(f"局部 OCR 图片路径不安全：{raw_name}")
    path = PurePosixPath(raw_name)
    if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
        raise CollectionRecoveryError(f"局部 OCR 图片路径不安全：{raw_name}")
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", Path(path.name).suffix):
        raise CollectionRecoveryError(f"局部 OCR 图片扩展名不安全：{raw_name}")
    return path.name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_recovery_images(markdown: str, local_extract_dir: str | Path,
                         main_extract_dir: str | Path) -> str:
    """复制局部 OCR 引用图到主 ``images/``，并返回已改写的 Markdown。

    只接受 ``images/<单个文件名>``。目标名由内容哈希决定，既避免不同 OCR 任务
    同名覆盖，也让同一裁片重复恢复保持幂等。
    """
    value = markdown or ""
    if not _MD_IMAGE_RE.search(value) and not _HTML_IMAGE_RE.search(value):
        return value
    local_root = Path(local_extract_dir)
    main_root = Path(main_extract_dir)
    if local_root.is_symlink() or main_root.is_symlink():
        raise CollectionRecoveryError("OCR 解压目录不能是符号链接")
    source_images = local_root / "images"
    if source_images.is_symlink() or not source_images.is_dir():
        raise CollectionRecoveryError("局部 OCR 结果缺少安全的 images 目录")
    destination_images = main_root / "images"
    destination_images.mkdir(parents=True, exist_ok=True)
    if destination_images.is_symlink():
        raise CollectionRecoveryError("主 OCR images 目录不能是符号链接")
    source_root = source_images.resolve()
    destination_root = destination_images.resolve()
    replacements: dict[str, str] = {}

    def _copy(raw_name: str) -> str:
        safe_name = _safe_image_name(raw_name)
        if safe_name in replacements:
            return replacements[safe_name]
        source = source_images / safe_name
        if source.is_symlink() or not source.is_file():
            raise CollectionRecoveryError(f"局部 OCR 图片不存在或不是普通文件：{safe_name}")
        resolved_source = source.resolve()
        if resolved_source.parent != source_root:
            raise CollectionRecoveryError(f"局部 OCR 图片越出 images 目录：{safe_name}")
        digest = _sha256(resolved_source)
        suffix = source.suffix.lower()
        target_name = f"recovery_{digest[:20]}{suffix}"
        target = destination_images / target_name
        if target.resolve(strict=False).parent != destination_root:
            raise CollectionRecoveryError("恢复图片目标越出主 images 目录")
        if target.exists():
            if target.is_symlink() or not target.is_file() or _sha256(target) != digest:
                raise CollectionRecoveryError(f"恢复图片目标冲突：{target_name}")
        else:
            created = False
            try:
                with target.open("xb") as output, resolved_source.open("rb") as source_stream:
                    created = True
                    shutil.copyfileobj(source_stream, output, length=1024 * 1024)
                if _sha256(target) != digest:
                    raise CollectionRecoveryError(f"恢复图片复制校验失败：{safe_name}")
            except Exception:
                if created and target.exists():
                    target.unlink()
                raise
        replacements[safe_name] = target_name
        return target_name

    def _replace(match: re.Match) -> str:
        target_name = _copy(match.group("path"))
        return f'{match.group("head")}{target_name}{match.group("tail")}'

    value = _MD_IMAGE_RE.sub(_replace, value)
    return _HTML_IMAGE_RE.sub(_replace, value)
