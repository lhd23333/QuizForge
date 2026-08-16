"""可编辑 Word 导出的语义渲染与生成入口。

本模块刻意不复用 ``exporter.py`` 中含 raw LaTeX 的 Markdown。PDF 版式与 Word
语义版式分别演进，只共享题目输入和统一的 ``ExportError`` 错误边界。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
from typing import Iterable

import config
import export_tables
from exporter import ExportError


SUPPORTED_MODES = frozenset({
    "exam",
    "exam_std",
    "note",
    "lecture",
    "slides",
    "practice",
    "list",
    "handout",
})

_GROUPED_MODES = frozenset({"exam", "exam_std", "practice"})
_TYPE_ORDER = ("单选题", "多选题", "填空题", "解答题")
_TYPE_KEYS = {
    "单选题": "single",
    "多选题": "multi",
    "填空题": "blank",
    "解答题": "solve",
}
_EXPORT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_QIMG_RE = re.compile(r"!\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")
_WORD_IMAGE_EXTENSIONS = frozenset({
    ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp",
})


@dataclass(frozen=True)
class SectionSpec:
    """Word 分节意图；真正的 OOXML 分节由 ``word_ooxml`` 写入。"""

    marker: str
    orientation: str = "portrait"
    columns: int = 1
    start: str = "newPage"


@dataclass(frozen=True)
class WordPlan:
    """不含临时路径的 Word 语义计划，便于纯逻辑测试。"""

    markdown: str
    sections: tuple[SectionSpec, ...]
    image_widths: tuple[tuple[str, int], ...] = ()


def _pipe_cell(text: str) -> str:
    return str(text or "").replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _table_markdown(inner: str) -> str | None:
    parsed = export_tables.html_table_rows(inner)
    if not parsed:
        return None
    column_count = max(sum(span for _text, span in row) for row in parsed)
    if column_count < 1:
        return None
    rows: list[list[str]] = []
    for row in parsed:
        expanded: list[str] = []
        for text, span in row:
            expanded.append(_pipe_cell(text))
            expanded.extend([""] * (max(1, span) - 1))
        expanded.extend([""] * (column_count - len(expanded)))
        rows.append(expanded[:column_count])

    def _line(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    separator = ["---"] * column_count
    return "\n".join([_line(rows[0]), _line(separator), *(_line(row) for row in rows[1:])])


def normalize_word_markdown(text: str) -> str:
    """清理不可见 OCR 噪声，并把 HTML 表格改为 Pandoc 原生表格。"""
    cleaned = _EXPORT_CONTROL_RE.sub("", str(text or "")).replace("\uf8f3", "")

    def _replace_table(match: re.Match) -> str:
        table = _table_markdown(match.group(1))
        return match.group(0) if table is None else f"\n\n{table}\n\n"

    return export_tables.TABLE_RE.sub(_replace_table, cleaned).rstrip()


def _parse_image_layouts(value) -> dict[int, dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, list):
        return {}
    layouts: dict[int, dict] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        try:
            width = int(item.get("w")) if item.get("w") not in (None, "") else None
        except (TypeError, ValueError):
            width = None
        align = item.get("align")
        layouts[index] = {
            "width": max(10, min(100, width)) if width is not None else None,
            "align": align if align in {"left", "center", "right"} else None,
            "stack": bool(item.get("stack")),
        }
    return layouts


def _safe_image_source(raw_name: str, question_number: int) -> Path:
    name = str(raw_name or "").strip().replace("\\", "/")
    if not name or "/" in name or Path(name).name != name:
        raise ExportError(f"第 {question_number} 题图片路径无效：{raw_name}")
    if Path(name).suffix.lower() not in _WORD_IMAGE_EXTENSIONS:
        raise ExportError(f"第 {question_number} 题图片格式不支持 Word：{name}")
    source = config.ASSETS_DIR / name
    try:
        assets_root = config.ASSETS_DIR.resolve(strict=True)
        resolved = source.resolve(strict=True)
    except OSError:
        raise ExportError(f"第 {question_number} 题图片缺失：{name}") from None
    if source.is_symlink() or resolved.parent != assets_root or not resolved.is_file():
        raise ExportError(f"第 {question_number} 题图片路径无效：{name}")
    return resolved


def stage_word_images(questions: list[dict], work_dir: Path,
                      stem: str) -> tuple[list[dict], tuple[tuple[str, int], ...]]:
    """把 Obsidian 图片复制到独占目录并改写为安全的相对 Markdown 引用。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[Path, str] = {}
    widths: list[tuple[str, int]] = []
    counter = 0

    def _stage_text(text: str, question: dict, question_number: int,
                    *, solution: bool) -> str:
        nonlocal counter
        layouts = _parse_image_layouts(
            question.get("sol_img_layouts" if solution else "img_layouts"))
        occurrence = 0

        def _replace(match: re.Match) -> str:
            nonlocal counter, occurrence
            source = _safe_image_source(match.group(1), question_number)
            local_name = cache.get(source)
            if local_name is None:
                counter += 1
                local_name = f"{stem}_img_{counter}{source.suffix.lower()}"
                shutil.copy2(source, work_dir / local_name)
                cache[source] = local_name

            layout = layouts.get(occurrence, {})
            fallback = question.get("img_width") if not solution and occurrence == 0 else None
            try:
                fallback_width = int(fallback) if fallback not in (None, "") else None
            except (TypeError, ValueError):
                fallback_width = None
            width = layout.get("width") or fallback_width or 70
            width = max(10, min(100, width))
            occurrence += 1
            widths.append((local_name, width))
            image = f"![图片]({local_name}){{width={width}%}}"
            align = layout.get("align")
            if align:
                return f'\n\n::: {{custom-style="Image{align.title()}"}}\n{image}\n:::\n\n'
            return image

        return _QIMG_RE.sub(_replace, str(text or ""))

    staged: list[dict] = []
    for question_number, question in enumerate(questions, start=1):
        copy = dict(question)
        copy["body"] = _stage_text(
            copy.get("body", ""), copy, question_number, solution=False)
        copy["solution"] = _stage_text(
            copy.get("solution", ""), copy, question_number, solution=True)
        staged.append(copy)
    return staged, tuple(widths)


def _styled(style: str, text: str) -> str:
    return f'::: {{custom-style="{style}"}}\n{text.strip()}\n:::'


def _marker(value: str) -> str:
    return f'[{value}]{{custom-style="QuizForgeMarker"}}'


def _indent_list_body(text: str) -> str:
    lines = str(text or "").strip().splitlines() or [""]
    first = lines[0]
    rest = "\n".join(f"   {line}" if line else "" for line in lines[1:])
    return first if not rest else f"{first}\n{rest}"


def _question_item(number: int, question: dict, solution_mode: str) -> str:
    body = _indent_list_body(question.get("body", ""))
    item = f"{number}. {_marker(f'QF-Q-{number}')} {body}".rstrip()
    solution = str(question.get("solution") or "").strip()
    if solution_mode == "inline" and solution:
        item += "\n\n" + _styled("Solution", f"答案与解析：{solution}")
    return item


def _ordered_types(questions: Iterable[dict]) -> list[tuple[str, list[dict]]]:
    grouped: dict[str, list[dict]] = {}
    for question in questions:
        grouped.setdefault(str(question.get("type") or "未分类"), []).append(question)
    names = [name for name in _TYPE_ORDER if name in grouped]
    names.extend(name for name in grouped if name not in _TYPE_ORDER)
    return [(name, grouped[name]) for name in names]


def _section_description(name: str, count: int, std_opts: dict) -> str:
    points = (std_opts.get("section_points") or {}).get(_TYPE_KEYS.get(name, ""), "")
    try:
        per_question = int(str(points).strip())
    except (TypeError, ValueError):
        per_question = 0
    if per_question > 0:
        return f"本题共 {count} 小题，每小题 {per_question} 分，共 {count * per_question} 分。"
    return f"本题共 {count} 小题。"


def _standard_exam_front(title: str, std_opts: dict) -> list[str]:
    blocks = []
    secret_notice = str(std_opts.get("secret_notice") or "").strip()
    if secret_notice:
        blocks.append(_styled("ExamNotice", secret_notice))
    blocks.append(_styled("ExamTitle", title))
    subject = str(std_opts.get("subject") or "").strip()
    if subject:
        blocks.append(_styled("ExamSubtitle", subject))
    if std_opts.get("info_bar"):
        blocks.append("| 姓名 | 班级 | 学号 |\n|---|---|---|\n|  |  |  |")
    notes = str(std_opts.get("exam_notes") or "").strip()
    if notes:
        blocks.append(_styled("ExamNotes", f"注意事项\n\n{notes}"))
    return blocks


def _render_questions(questions: list[dict], mode: str, solution_mode: str,
                      std_opts: dict) -> list[str]:
    blocks: list[str] = []
    numbered = list(enumerate(questions, start=1))
    if mode not in _GROUPED_MODES:
        for index, (number, question) in enumerate(numbered):
            blocks.append(_question_item(number, question, solution_mode))
            if mode in {"lecture", "slides"} and index < len(numbered) - 1:
                blocks.append(_marker("QF_PAGE_BREAK"))
            elif mode == "note" and (index + 1) % 2 == 0 and index < len(numbered) - 1:
                blocks.append(_marker("QF_PAGE_BREAK"))
        return blocks

    number_by_id = {id(question): number for number, question in numbered}
    for type_name, type_questions in _ordered_types(questions):
        heading = type_name
        if mode == "exam_std":
            heading += "：" + _section_description(type_name, len(type_questions), std_opts)
        blocks.append(_styled("QuestionType", heading))
        blocks.extend(
            _question_item(number_by_id[id(question)], question, solution_mode)
            for question in type_questions
        )
    return blocks


def _render_separate_solutions(questions: list[dict]) -> list[str]:
    blocks = [_styled("QuestionType", "答案与解析")]
    for number, question in enumerate(questions, start=1):
        solution = str(question.get("solution") or "").strip() or "（无解析）"
        blocks.append(
            f"{number}. {_marker(f'QF-Q-{number}')} "
            + _styled("Solution", solution)
        )
    return blocks


def build_word_plan(questions, *, title, mode, keypoints="", fullpage_ids=None,
                    solution_mode="none", std_opts=None,
                    bank_subject="math") -> WordPlan:
    """把题目与导出参数转换为不含 raw LaTeX 的 Word 语义计划。"""
    if mode not in SUPPORTED_MODES:
        raise ExportError(f"Word 不支持导出模式：{mode}")
    if solution_mode not in {"none", "inline", "separate"}:
        raise ExportError("Word 解析位置无效")
    questions = list(questions or [])
    if not questions:
        raise ExportError("没有题目可导出")

    title = str(title or "").strip() or "试卷"
    std_opts = dict(std_opts or {})
    sections: list[SectionSpec] = []
    blocks: list[str] = []

    if mode == "slides":
        marker = "QF_SECTION_SLIDES"
        sections.append(SectionSpec(marker, orientation="slides"))
        blocks.append(_marker(marker))
    elif mode == "practice":
        blocks.append(_styled("ExamTitle", title))
        marker = "QF_SECTION_PRACTICE"
        sections.append(SectionSpec(marker, columns=2, start="continuous"))
        blocks.append(_marker(marker))
    else:
        marker = "QF_SECTION_MAIN"
        sections.append(SectionSpec(marker))
        blocks.append(_marker(marker))

    if mode == "exam_std":
        blocks.extend(_standard_exam_front(title, std_opts))
    elif mode != "practice":
        blocks.append(_styled("ExamTitle", title))

    if mode == "handout" and str(keypoints or "").strip():
        blocks.append(_styled("ExamNotes", f"知识要点\n\n{str(keypoints).strip()}"))

    # 物理题库只改变面向用户的分区名称，不改变题目存储类型。
    display_questions = questions
    if bank_subject == "physics":
        display_questions = [
            {**question, "type": "实验题"}
            if question.get("type") == "填空题" else question
            for question in questions
        ]
    blocks.extend(_render_questions(display_questions, mode, solution_mode, std_opts))

    if solution_mode == "separate":
        marker = "QF_SECTION_SOLUTIONS"
        sections.append(SectionSpec(marker, start="newPage"))
        blocks.append(_marker(marker))
        blocks.extend(_render_separate_solutions(questions))

    return WordPlan(
        markdown="\n\n".join(block for block in blocks if block).rstrip() + "\n",
        sections=tuple(sections),
    )
