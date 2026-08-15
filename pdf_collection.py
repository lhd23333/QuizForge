"""带书签的试卷合集 PDF 拆分与题干/答案配对。

合集模式只负责把一份大 PDF 还原成若干普通单卷 PDF；拆完以后仍走现有
``converter`` 链路。这样题号在每卷内重新从 1 开始，既不会把不同试卷误并成一卷，
也不需要给机械拆题再维护一套“第几套卷”的状态机。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata
import uuid

try:
    # 放在模块级 ``try`` 中，让普通单卷路径在依赖缺失时仍可启动；同时保留显式
    # import，桌面打包器才能自动收进 pypdf，而不是到用户点击合集模式才发现漏包。
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover - 只在安装依赖不完整时走，入口会给可读错误
    PdfReader = None
    PdfWriter = None


class CollectionSplitError(ValueError):
    """合集结构无法安全拆分时给用户的可读错误。"""


class NoBookmarksError(CollectionSplitError):
    """没有 PDF 书签，上层可改走整本 OCR 后结构分组。"""


@dataclass(frozen=True)
class PdfPart:
    """一个书签界定的半开页区间，页号均为从 0 开始。"""

    title: str
    start: int
    end: int


@dataclass(frozen=True)
class CollectionPair:
    """已经落盘的一份题干卷及其可选答案卷。"""

    title: str
    exam_path: Path
    solution_path: Path | None


_GENERIC_TITLES = {
    "物理", "数学", "化学", "生物", "语文", "英语",
    "物理试题", "数学试题", "化学试题", "生物试题", "语文试题", "英语试题",
    "试题", "试卷", "答案", "解析", "参考答案", "答案解析", "详细解析",
}
_PAPER_HINT_RE = re.compile(
    r"(?:中学|学校|附中|一中|届|学年|高一|高二|高三|初一|初二|初三|"
    r"高考|中考|月考|联考|模考|一模|二模|三模|期中|期末|测试|考试|竞赛|试卷|试题)"
)
_SOLUTION_SUFFIX_RE = re.compile(
    r"(?:参考答案|答案解析|详细解析|解析|答案|详解|解答)+\s*$"
)
_SUBJECT_SUFFIX_RE = re.compile(
    r"(?:物理|数学|化学|生物|语文|英语)(?:试题|试卷)?\s*$"
)
_SAFE_TITLE_RE = re.compile(r"[\x00-\x1f<>:\"/\\|?*]+")


def _require_pypdf() -> None:
    if PdfReader is None or PdfWriter is None:
        raise CollectionSplitError(
            "合集拆分组件未安装，请先安装 requirements.txt 中的 pypdf 后重启软件"
        )


def _outline_rows(items, reader, depth=0):
    """把 pypdf 的“书签项与子列表交错”结构摊平成 (层级, 页, 标题)。"""
    for item in items:
        if isinstance(item, list):
            yield from _outline_rows(item, reader, depth + 1)
            continue
        title = getattr(item, "title", None)
        if title is None and hasattr(item, "get"):
            title = item.get("/Title", "")
        try:
            page = reader.get_destination_page_number(item)
        except Exception:
            continue
        if isinstance(page, int) and page >= 0:
            yield depth, page, " ".join(str(title or "").split())


def _looks_like_paper_title(title: str) -> bool:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", title or ""))
    if not compact or compact in _GENERIC_TITLES:
        return False
    if re.match(r"^[一二三四五六七八九十]+[、.．]", compact):
        return False
    return bool(_PAPER_HINT_RE.search(compact))


def _title_score(title: str) -> tuple[int, int]:
    """同一页有“学校月考”和“物理试题”两个书签时，稳定选前者。"""
    compact = re.sub(r"\s+", "", title)
    signals = sum(token in compact for token in (
        "中学", "学校", "附中", "一中", "届", "学年", "月考", "联考", "模考",
        "一模", "二模", "期中", "期末", "高考", "中考", "参考答案",
    ))
    return signals, len(compact)


def discover_parts(path: str | Path) -> list[PdfPart]:
    """从最浅的“多试卷书签层”发现各卷页区间。

    只使用书签，不从页面文字猜边界。页面标题 OCR 一旦漏字就可能把两套卷粘在一起；
    书签则是 PDF 制作者给出的确定分页信息，像本次 198/222 页合集可直接得到 32 对。
    """
    _require_pypdf()
    source = Path(path)
    try:
        reader = PdfReader(str(source))
        rows = list(_outline_rows(reader.outline, reader))
    except Exception as exc:
        raise CollectionSplitError(f"无法读取 PDF 书签：{source.name}") from exc
    if not rows:
        raise NoBookmarksError(f"「{source.name}」没有 PDF 书签")

    # 有的文件用一个“合集”根书签，试卷在第 1 层；有的直接把每套卷放在第 0 层。
    # 从浅到深找第一个能形成至少两份卷的层级，避免把更深的“大题一/二/三”当卷。
    starts: list[tuple[int, str]] = []
    selected_depth: int | None = None
    for depth in sorted({row[0] for row in rows}):
        by_page: dict[int, list[str]] = {}
        for row_depth, page, title in rows:
            if row_depth == depth and _looks_like_paper_title(title):
                by_page.setdefault(page, []).append(title)
        if len(by_page) < 2:
            continue
        starts = [
            (page, max(titles, key=_title_score))
            for page, titles in sorted(by_page.items())
        ]
        selected_depth = depth
        break
    if len(starts) < 2:
        raise CollectionSplitError(
            f"「{source.name}」没有找到至少两份带标题的试卷书签；"
            "请确认目录书签直接指向每套试卷首页"
        )

    # 不能只取“第一个看起来可用的层级”后把其余书签静默吞掉。异常 PDF 可能把
    # 前两卷放在同层、第三卷误挂成其中一卷的子书签；若继续切，第三卷会被并进
    # 第二卷。合集根书签通常在封面页，允许它位于首卷之前；首卷以后出现的其他
    # 层级试卷标题则说明目录结构有歧义，宁可停止让用户修书签，也不能漏卷。
    selected_pages = {page for page, _title in starts}
    first_start = starts[0][0]
    competing = [
        (page, title)
        for depth, page, title in rows
        if depth != selected_depth
        and page >= first_start
        and page not in selected_pages
        and _looks_like_paper_title(title)
    ]
    if competing:
        _page, title = min(competing)
        raise CollectionSplitError(
            f"「{source.name}」的试卷书签层级混杂（例如：{title}），"
            "无法保证不漏卷；请把各卷首页书签调整到同一层级"
        )

    parts = []
    for index, (start, title) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(reader.pages)
        if end <= start:
            raise CollectionSplitError(f"「{source.name}」的书签页序异常：{title}")
        parts.append(PdfPart(title=title, start=start, end=end))
    return parts


def pairing_key(title: str) -> str:
    """把题干标题与“同名+参考答案”归到同一键。"""
    value = unicodedata.normalize("NFKC", title or "").strip()
    value = _SOLUTION_SUFFIX_RE.sub("", value).strip()
    value = _SUBJECT_SUFFIX_RE.sub("", value).strip()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def _safe_display_title(title: str) -> str:
    value = _SOLUTION_SUFFIX_RE.sub("", title or "").strip()
    value = _SAFE_TITLE_RE.sub("_", value).strip(" ._")
    return value[:120] or "未命名试卷"


def pair_parts(exam_parts: list[PdfPart], solution_parts: list[PdfPart] | None = None):
    """按规范化标题一一配对；不按页序硬凑，避免答案整体错位。"""
    exam_keys = [pairing_key(part.title) for part in exam_parts]
    if not all(exam_keys) or len(set(exam_keys)) != len(exam_keys):
        raise CollectionSplitError("题干合集存在空标题或重名试卷书签，无法安全拆分")
    if not solution_parts:
        return [(part, None) for part in exam_parts]

    solution_map: dict[str, PdfPart] = {}
    duplicates = set()
    for part in solution_parts:
        key = pairing_key(part.title)
        if key in solution_map:
            duplicates.add(key)
        solution_map[key] = part
    if duplicates:
        raise CollectionSplitError("答案合集存在重名试卷书签，无法确定对应关系")

    pairs = []
    missing = []
    used = set()
    for exam in exam_parts:
        key = pairing_key(exam.title)
        solution = solution_map.get(key)
        if solution is None:
            missing.append(exam.title)
            continue
        used.add(key)
        pairs.append((exam, solution))
    extra = [part.title for part in solution_parts if pairing_key(part.title) not in used]
    if missing or extra:
        details = []
        if missing:
            details.append("缺答案：" + "、".join(missing[:3]))
        if extra:
            details.append("无题干：" + "、".join(extra[:3]))
        raise CollectionSplitError(
            "题干合集与答案合集的书签标题不能一一配对（" + "；".join(details) + "）"
        )
    return pairs


def _write_parts(source: Path, parts: list[PdfPart], output_dir: Path,
                 prefix: str) -> dict[PdfPart, Path]:
    reader = PdfReader(str(source))
    written: dict[PdfPart, Path] = {}
    try:
        for part in parts:
            writer = PdfWriter()
            for page_index in range(part.start, part.end):
                writer.add_page(reader.pages[page_index])
            writer.add_metadata({"/Title": part.title})
            target = output_dir / f"{prefix}_{uuid.uuid4().hex}.pdf"
            with target.open("wb") as handle:
                writer.write(handle)
            written[part] = target
    except Exception:
        for target in written.values():
            target.unlink(missing_ok=True)
        raise
    return written


def split_collection_pair(exam_path: str | Path, solution_path: str | Path | None,
                          output_dir: str | Path, *, max_parts: int = 1000
                          ) -> list[CollectionPair]:
    """拆分合集并返回按标题配好的单卷路径。失败时不保留半成品。"""
    _require_pypdf()
    exam_source = Path(exam_path)
    solution_source = Path(solution_path) if solution_path else None
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    exam_parts = discover_parts(exam_source)
    solution_parts = discover_parts(solution_source) if solution_source else None
    planned = pair_parts(exam_parts, solution_parts)
    if len(planned) > max_parts:
        raise CollectionSplitError(f"合集包含 {len(planned)} 份试卷，超过上限 {max_parts} 份")

    exam_written: dict[PdfPart, Path] = {}
    solution_written: dict[PdfPart, Path] = {}
    succeeded = False
    try:
        exam_written = _write_parts(exam_source, exam_parts, out, "collection_exam")
        if solution_source and solution_parts:
            solution_written = _write_parts(
                solution_source, solution_parts, out, "collection_solution")
        result = [
            CollectionPair(
                title=_safe_display_title(exam.title),
                exam_path=exam_written[exam],
                solution_path=solution_written.get(solution) if solution else None,
            )
            for exam, solution in planned
        ]
        succeeded = True
        return result
    except CollectionSplitError:
        raise
    except Exception as exc:
        raise CollectionSplitError("拆分合集 PDF 失败，请确认文件未损坏或加密") from exc
    finally:
        # 只有完整返回时这些路径才需要交给批次生命周期管理；异常时立即回收。
        if not succeeded:
            for target in list(exam_written.values()) + list(solution_written.values()):
                target.unlink(missing_ok=True)
