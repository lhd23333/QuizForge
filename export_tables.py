"""OCR 表格的共享结构解析边界。

页面预览和 PDF 导出都只消费这里返回的纯文本行列，不直接复用外来 HTML。
LaTeX 与安全 HTML 的具体渲染仍分别留在 exporter.py 和 qrender.py。
"""

import html
import re


TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table\s*>", re.S | re.I)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr\s*>", re.S | re.I)
_CELL_RE = re.compile(r"<t([dh])\b([^>]*)>(.*?)</t\1\s*>", re.S | re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_COLSPAN_RE = re.compile(r"\bcolspan\s*=\s*[\"']?(\d+)", re.I)
PIPE_SEP_RE = re.compile(
    r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$"
)


def cell_text(raw: str) -> str:
    """单元格 HTML 转为无标签纯文本，不把外来标签带回页面。"""
    text = _BR_RE.sub(" ", raw)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def html_table_rows(inner: str) -> list[list[tuple[str, int]]]:
    """HTML 表格内部转为纯文本行，每格表示为 ``(内容, colspan)``。"""
    rows: list[list[tuple[str, int]]] = []
    for row_match in _TR_RE.finditer(inner):
        cells: list[tuple[str, int]] = []
        for cell_match in _CELL_RE.finditer(row_match.group(1)):
            span_match = _COLSPAN_RE.search(cell_match.group(2) or "")
            span = max(1, int(span_match.group(1))) if span_match else 1
            cells.append((cell_text(cell_match.group(3)), span))
        if cells:
            rows.append(cells)
    return rows


def pipe_text_cells(line: str) -> list[tuple[str, int]]:
    """一行 Markdown 管道表格转为纯文本单元格。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [(cell_text(cell), 1) for cell in text.split("|")]
