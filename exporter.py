"""导出：题目列表 → Markdown → pandoc → TeX →（xelatex）→ PDF。

复用 project-alpha 的导出链路与 exam_template.tex。
关键避坑（来自 project-alpha 调试档案）：
  1. 选项间必须加空行，否则 pandoc 会把连续行合并成一段
  2. 模板已含 tightlist / none-counter / array / longtable / calc 兼容补丁
  3. 文件统一 UTF-8（不加 BOM）
  4. 日志/异常文本用 ASCII 标记，避免 Windows GBK 终端 emoji 崩溃
"""

import base64
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

import config


class ExportError(Exception):
    """导出过程出错。"""


# 完整选项标签：优先匹配 $\displaystyle A.$（含美元整体），否则裸 A. / A．
# 两个分支分别用 finditer 定位“标签起点”，只在起点切分，绝不切进 $...$ 内部
# （否则 $ 落单，会被 pandoc 转义成 \$，导致公式失效）。
_LABEL_DOLLAR = re.compile(r"\$\\displaystyle\s*[A-D][.．]\s*\$")
_LABEL_BARE = re.compile(r"(?<![A-Za-z\\])[A-D][.．]")


def _split_at(line: str, pattern: re.Pattern) -> list[str]:
    """在 pattern 每个匹配的起点处切分（保留标签），返回非空片段。"""
    starts = [m.start() for m in pattern.finditer(line)]
    if len(starts) < 2:
        return [line]
    starts.append(len(line))
    parts = []
    head = line[: starts[0]].strip()
    if head:
        parts.append(head)
    for i in range(len(starts) - 1):
        seg = line[starts[i]: starts[i + 1]].strip()
        if seg:
            parts.append(seg)
    return parts


def _format_options(body: str) -> str:
    """把挤在一行的 A./B./C./D. 选项拆成段落（段间空行）。"""
    lines = body.splitlines()
    out: list[str] = []
    for line in lines:
        dollar_labels = _LABEL_DOLLAR.findall(line)
        if len(dollar_labels) >= 2:
            parts = _split_at(line, _LABEL_DOLLAR)
            out.append("\n\n".join(parts))
        else:
            bare = _LABEL_BARE.findall(line)
            if len({b[0] for b in bare}) >= 2:
                parts = _split_at(line, _LABEL_BARE)
                out.append("\n\n".join(parts))
            else:
                out.append(line)
    return "\n".join(out)


_ANSWER_BRACKET = r"$\qquad(\qquad)$"
_CAP_SENT = "QFIGCAPTIONNUM"

_CMD_WIDTH = {
    "vec": 0, "overrightarrow": 0, "overline": 0, "hat": 0, "bar": 0,
    "dot": 0, "tilde": 0, "widehat": 0, "widetilde": 0, "boldsymbol": 0,
    "mathrm": 0, "mathbf": 0, "left": 0, "right": 0, "displaystyle": 0,
    "sqrt": 1, "cdot": 1, "times": 1, "div": 1, "pm": 1, "mp": 1,
}
_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")


def _frac_width(m) -> str:
    num, den = m.group(1), m.group(2)
    return "x" * max(_visible_len(num), _visible_len(den), 1)


def _visible_len(text: str) -> int:
    s = _TABLE_TOKEN_RE.sub("", text)
    s = _LABEL_DOLLAR.sub("", s)
    s = re.sub(r"(?<![A-Za-z\\])[A-D][.．]", "", s, count=1)
    s = s.replace("$", "")
    prev = None
    while prev != s:
        prev = s
        s = _FRAC_RE.sub(_frac_width, s)
    width = 0
    for name in re.findall(r"\\([a-zA-Z]+)", s):
        width += _CMD_WIDTH.get(name, 1)
    s = re.sub(r"\\[a-zA-Z]+", "", s)
    s = re.sub(r"[{}\s]", "", s)
    for ch in s:
        width += 2 if ord(ch) > 0x2E7F else 1
    return width


def _choice_cols(parts: list[str]) -> int:
    longest = max((_visible_len(p) for p in parts), default=0)
    if longest <= 10:
        return 4
    if longest <= 28:
        return 2
    return 1


def _choice_tasks(body: str, want_parts: bool = False):
    """选择题渲染：题干末尾加作答括号并另起一行，选项用 tasks 环境自适应分列。"""
    pattern = _LABEL_DOLLAR if len(_LABEL_DOLLAR.findall(body)) >= 2 else _LABEL_BARE
    marks = list(pattern.finditer(body))
    seq = []
    for m in marks:
        mm = re.search(r"[A-D]", m.group(0))
        if mm:
            seq.append((mm.group(0), m.start(), m.end()))
    if len({s[0] for s in seq}) < 2:
        formatted = _format_options(body)
        return (None, formatted) if want_parts else formatted

    first = seq[0][1]
    stem = body[:first].rstrip()

    opts = []
    for i, (letter, start, _end) in enumerate(seq):
        end = seq[i + 1][1] if i + 1 < len(seq) else len(body)
        seg = body[start:end].strip()
        if seg:
            opts.append(seg)
    if len(opts) < 2:
        formatted = _format_options(body)
        return (None, formatted) if want_parts else formatted

    tail = ""
    last = opts[-1]
    fig_m = re.search(r"\n*```\{=latex\}.*?```\n*|\n*!\[\][^\n]*", last, re.S)
    if fig_m:
        tail = fig_m.group(0).strip()
        opts[-1] = (last[:fig_m.start()] + last[fig_m.end():]).strip()

    opts = [re.sub(r"\s+", " ", o) for o in opts]

    cols = _choice_cols(opts)
    tasks_body = "\n".join(f"  \\task {p}" for p in opts)
    tasks_env = f"\\begin{{tasks}}({cols})\n{tasks_body}\n\\end{{tasks}}"

    if want_parts:
        return stem + _ANSWER_BRACKET, tasks_env

    md = stem + _ANSWER_BRACKET + _raw(tasks_env)
    if tail:
        md += "\n\n" + tail
    return md


# ---------------------------------------------------------------------------
# 分页/留白 raw-LaTeX 原语
# ---------------------------------------------------------------------------

CLEARPAGE = "\n\n```{=latex}\n\\clearpage\n```\n"
_HALF_OPEN = ("\n\n```{=latex}\n\\nointerlineskip\\vbox to 0.5\\textheight\\bgroup"
              "\\vskip0pt\\noindent\\begin{minipage}[t]{\\linewidth}\n```\n")
_HALF_CLOSE = "\n\n```{=latex}\n\\end{minipage}\\vss\\egroup\n```\n"

_SINGLE = {"单选题"}
_MULTI = {"多选题"}
_CHOICE = _SINGLE | _MULTI
_BLANK = {"填空题"}
_SOLVE = {"解答题"}
_CN_NUM = ["一", "二", "三", "四", "五", "六"]


def _norm_split(v) -> str | None:
    if v is None or v == 0 or v == "" or v == "off":
        return None
    if v == "full":
        return "full"
    if v == "sub":
        return "sub"
    return "opts"


_LEADING_RAW_RE = re.compile(r"^\s*(```\{=latex\}\n.*?\n```)\s*", re.S)

_SUBQ_LINE_RE = re.compile(
    r"^[ \t　]*（\s*(?:[0-9０-９]+|[ivxIVX]+|[Ⅰ-ⅿ]+)\s*）"
)


def _break_subquestions(body: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    for line in lines:
        if _SUBQ_LINE_RE.match(line):
            stripped = line.lstrip(" \t　")
            if out and out[-1].strip() != "":
                out.append("")
            out.append(stripped)
        else:
            out.append(line)
    return "\n".join(out)


def _num_wrap(num: int, core: str) -> str:
    return _raw(f"\\qopen{{{num}.}}") + core + _raw("\\qclose")


def _q_md(num: int, body: str, qtype: str = None, img_align: str = None,
          img_width=None, img_split=None, img_layouts=None) -> str:
    """单题 Markdown：题号 + 正文。"""
    prefix = ""
    m = _LEADING_RAW_RE.match(body)
    if m:
        prefix = m.group(1) + "\n\n"
        body = body[m.end():]
    body, marks = _extract_mark(body)
    layouts = _parse_layouts(img_layouts)
    split_mode = _norm_split(img_split)
    if marks:
        name, cap = marks[0]
        w0, a0 = _layout_at(layouts, 0, img_width, img_align)
        tail = _extra_figs(marks, layouts)
    else:
        name = cap = None
        w0, a0 = img_width, img_align
        tail = ""
    if qtype in _CHOICE:
        if split_mode and name is not None:
            stem, tasks_env = _choice_tasks(body, want_parts=True)
            if stem is not None:
                full = split_mode == "full"
                return prefix + _place_choice_split(
                    num, stem, tasks_env, name, cap or "", tail, full=full,
                    width=w0)
            core = tasks_env
        else:
            core = _choice_tasks(body)
    elif qtype in _SOLVE and split_mode == "sub" and name is not None:
        parts = _split_stem_subs(body)
        if parts is not None:
            return prefix + _place_solve_split(
                num, parts[0], parts[1], name, cap or "", tail, width=w0)
        core = _format_options(_break_subquestions(body))
    else:
        core = _format_options(_break_subquestions(body))
    return prefix + _place_image(_num_wrap(num, core), marks, qtype,
                                 img_align, img_width, bool(split_mode), layouts)


_MARK_RE = re.compile(r"\\qfigmark\{([^}]*)\}\{([^}]*)\}")
_TRAILING_MARK_RE = re.compile(
    r"\n*```\{=latex\}\n((?:\\qfigmark\{[^}]*\}\{[^}]*\})+)\n```\n*\Z", re.S
)


def _extract_mark(text: str) -> tuple[str, list[tuple[str, str]]]:
    m = _TRAILING_MARK_RE.search(text)
    if not m:
        return text, []
    marks = _MARK_RE.findall(m.group(1))
    if not marks:
        return text, []
    return text[:m.start()].rstrip(), marks


# ---------------------------------------------------------------------------
# 多图逐图排版：img_layouts
# ---------------------------------------------------------------------------

_LAYOUT_ALIGNS = ("left", "center", "right")


def _parse_layouts(img_layouts) -> dict[int, dict]:
    if not img_layouts:
        return {}
    data = img_layouts
    if isinstance(data, str):
        import json
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return {}
    if not isinstance(data, list):
        return {}
    out: dict[int, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        w = item.get("w")
        try:
            w = int(w) if w not in (None, "") else None
        except (TypeError, ValueError):
            w = None
        align = item.get("align")
        out[idx] = {"w": w, "align": align if align in _LAYOUT_ALIGNS else None}
    return out


def _layout_at(layouts: dict[int, dict], idx: int,
               width=None, align=None) -> tuple[object, object]:
    lay = layouts.get(idx)
    if not lay:
        return width, align
    return (lay["w"] if lay["w"] is not None else width,
            lay["align"] or align)


_MULTI_ROW_MAX = 0.95


def _fig_box(name: str, cap: str, frac: float) -> str:
    return f"\\qfigflexbox{{{frac}}}{{{name}}}{{{cap}}}"


def _figs_latex(items: list[tuple[str, str, float, str]]) -> str:
    if not items:
        return ""
    rows: list[list[tuple[str, str, float, str]]] = []
    i = 0
    while i < len(items):
        if (i + 1 < len(items)
                and items[i][2] + items[i + 1][2] <= _MULTI_ROW_MAX):
            rows.append([items[i], items[i + 1]])
            i += 2
        else:
            rows.append([items[i]])
            i += 1

    out = []
    for row in rows:
        if len(row) == 2:
            a, b = row
            out.append("\\par\\nobreak\\vspace{0.3em}\\noindent"
                       + _fig_box(a[0], a[1], a[2]) + "\\hfill"
                       + _fig_box(b[0], b[1], b[2]) + "\\par\\vspace{0.3em}")
        else:
            name, cap, frac, align = row[0]
            before = "" if align == "left" else "\\hfill"
            after = "" if align == "right" else "\\hfill"
            out.append("\\par\\nobreak\\vspace{0.3em}\\noindent"
                       + before + _fig_box(name, cap, frac) + after
                       + "\\par\\vspace{0.3em}")
    return _raw("".join(out))


_DEFAULT_IMG_FRAC = 35


def _extra_figs(marks: list[tuple[str, str]], layouts: dict[int, dict],
                start: int = 1) -> str:
    items = []
    for idx in range(start, len(marks)):
        name, cap = marks[idx]
        w, align = _layout_at(layouts, idx)
        frac = (int(w) if w else _DEFAULT_IMG_FRAC) / 100
        items.append((name, cap, frac, align or "right"))
    return _figs_latex(items)


def _split_fracs(width) -> tuple[float, float]:
    if width:
        img = min(0.7, max(0.1, int(width) / 100))
        return round(0.96 - img, 4), img
    return 0.48, 0.48


def _split_img_hcap(frac: float) -> float:
    return min(0.6, round(frac * 0.72, 3))


def _place_image(numbered_md: str, marks: list[tuple[str, str]],
                  qtype: str = None, align: str = None, width=None,
                  split=False, layouts: dict[int, dict] = None) -> str:
    if not marks:
        return numbered_md
    layouts = layouts or {}
    name, cap = marks[0]
    cap = cap or ""
    width, align = _layout_at(layouts, 0, width, align)
    tail = _extra_figs(marks, layouts)
    multi = len(marks) > 1

    if split and qtype in _BLANK:
        txt_frac, img_frac = _split_fracs(width)
        hcap = _split_img_hcap(img_frac)
        img_side = (f"\\end{{minipage}}\\hfill\\begin{{minipage}}[t]{{{img_frac}\\linewidth}}"
                    "\\setlength{\\parskip}{0pt}\\vspace{0pt}"
                    f"\\centering\\includegraphics[width=\\linewidth,height={hcap}\\textheight,"
                    f"keepaspectratio]{{{name}}}"
                    + (f"\\par\\vspace{{0.2em}}{{\\footnotesize {cap}}}" if cap else "")
                    + "\\end{minipage}")
        open_block = (f"\n\n```{{=latex}}\n\\noindent\\begin{{minipage}}[t]{{{txt_frac}\\linewidth}}"
                       "\\setlength{\\parskip}{0pt}\\vspace{0pt}\n```\n")
        close_block = f"\n\n```{{=latex}}\n{img_side}\n```\n"
        return open_block + numbered_md + close_block + tail

    if align or width or multi:
        frac = (int(width) if width else _DEFAULT_IMG_FRAC) / 100
        items = [(name, cap, frac, align or "right")]
        for idx in range(1, len(marks)):
            n2, c2 = marks[idx]
            w2, a2 = _layout_at(layouts, idx)
            items.append((n2, c2 or "", (int(w2) if w2 else _DEFAULT_IMG_FRAC) / 100,
                          a2 or "right"))
        return numbered_md + _figs_latex(items)

    if qtype in _BLANK:
        fig_latex = f"\\qfigwrap{{{name}}}{{{cap}}}"
        return _raw(fig_latex).lstrip("\n") + "\n\n" + numbered_md + tail

    fig_latex = f"\\qfig{{{name}}}{{{cap}}}"
    return numbered_md + _raw(fig_latex) + tail


def _choice_img_side(name: str, cap: str, frac: float = 0.48) -> str:
    h = _split_img_hcap(frac)
    return (f"\\begin{{minipage}}[t]{{{frac}\\linewidth}}"
            "\\setlength{\\parskip}{0pt}\\vspace{0pt}"
            f"\\centering\\includegraphics[width=\\linewidth,height={h}\\textheight,"
            f"keepaspectratio]{{{name}}}"
            + (f"\\par\\vspace{{0.2em}}{{\\footnotesize {cap}}}" if cap else "")
            + "\\end{minipage}")


def _place_choice_split(num: int, stem: str, tasks_env: str, name: str, cap: str,
                         tail: str, full: bool = False, width=None) -> str:
    txt_frac, img_frac = _split_fracs(width)
    right = _choice_img_side(name, cap, img_frac)

    if full:
        open_block = (f"\n\n```{{=latex}}\n\\noindent\\begin{{minipage}}[t]{{{txt_frac}\\linewidth}}"
                      "\\setlength{\\parskip}{0pt}\\vspace{0pt}\n```\n")
        close_block = (f"\n\n```{{=latex}}\n\\par\\vspace{{0.3em}}\n{tasks_env}"
                       f"\\end{{minipage}}\\hfill{right}\n```\n")
        core = open_block + stem + close_block + tail
        return _num_wrap(num, core)

    left = (f"\\begin{{minipage}}[t]{{{txt_frac}\\linewidth}}"
            f"\\setlength{{\\parskip}}{{0pt}}\\vspace{{0pt}}\n{tasks_env}"
            "\\end{minipage}")
    row = f"\\par\\vspace{{0.3em}}\\noindent{left}\\hfill{right}"
    core = stem + _raw(row) + tail
    return _num_wrap(num, core)


def _split_stem_subs(body: str) -> tuple[str, str] | None:
    normalized = _break_subquestions(body)
    lines = normalized.splitlines()
    for i, line in enumerate(lines):
        if _SUBQ_LINE_RE.match(line):
            stem = "\n".join(lines[:i]).strip()
            subs = "\n".join(lines[i:]).strip()
            if stem and subs:
                return stem, subs
            return None
    return None


def _place_solve_split(num: int, stem: str, subs: str, name: str, cap: str,
                        tail: str, width=None) -> str:
    txt_frac, img_frac = _split_fracs(width)
    right = _choice_img_side(name, cap, img_frac)
    open_block = (f"\n\n```{{=latex}}\n\\par\\vspace{{0.3em}}\\noindent"
                  f"\\begin{{minipage}}[t]{{{txt_frac}\\linewidth}}"
                  "\\setlength{\\parskip}{0pt}\\vspace{0pt}\n```\n")
    close_block = f"\n\n```{{=latex}}\n\\end{{minipage}}\\hfill{right}\n```\n"
    subs_md = _format_options(subs)
    core = stem + open_block + subs_md + close_block + tail
    return _num_wrap(num, core)


def _img_fields(q: dict) -> dict:
    return {"img_align": q.get("img_align"), "img_width": q.get("img_width"),
            "img_split": q.get("img_split"), "img_layouts": q.get("img_layouts")}


def _raw(latex: str) -> str:
    """把一段 raw LaTeX 包成 pandoc 能透传的 fenced 块。"""
    return f"\n\n```{{=latex}}\n{latex}\n```\n"


# ---------------------------------------------------------------------------
# 分页计算：paginate() —— 导出与预览共用的唯一分页逻辑
# ---------------------------------------------------------------------------


def _new_page(pages):
    pages.append([])
    return pages[-1]


def paginate(questions: list[dict], mode: str = "list", keypoints: str = "",
             fullpage_ids=None, solution_mode: str = "none",
             std_opts: dict = None) -> list[list[dict]]:
    """把题目按模式分页，返回 list[page]（page=block 列表）。纯函数，无副作用。"""
    fullpage_ids = set(fullpage_ids or [])

    if mode == "exam_std":
        pages = _paginate_exam_std(questions, std_opts or {})
    elif mode == "exam":
        pages = _paginate_exam(questions)
    elif mode == "handout":
        pages = _paginate_handout(questions, keypoints, fullpage_ids)
    elif mode == "note":
        pages = _paginate_two(questions, fullpage_ids, start_num=1)
    elif mode == "lecture":
        pages = [[{"kind": "question", "num": i, "body": q["body"],
                   "layout": "full", "solution": q.get("solution"),
                   "type": q.get("type"), **_img_fields(q)}]
                 for i, q in enumerate(questions, 1)]
    else:
        pages = [[{"kind": "question", "num": i, "body": q["body"],
                   "layout": "flow", "solution": q.get("solution"),
                   "type": q.get("type"), **_img_fields(q)}
                  for i, q in enumerate(questions, 1)]]

    if solution_mode == "separate":
        pages = pages + _solution_pages(pages)
    return pages


def _solution_pages(pages: list[list[dict]]) -> list[list[dict]]:
    items = []
    for page in pages:
        for b in page:
            if b.get("kind") == "question" and b.get("solution"):
                items.append((b["num"], b["solution"]))
    if not items:
        return []
    blocks = [{"kind": "solution_head"}]
    blocks += [{"kind": "solution_item", "num": n, "text": s} for n, s in items]
    return [blocks]


def _paginate_two(questions, fullpage_ids, start_num=1):
    pages = []
    page = _new_page(pages)
    on_page = 0
    for i, q in enumerate(questions):
        num = start_num + i
        if q.get("id") in fullpage_ids:
            if on_page > 0:
                page = _new_page(pages)
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "full", "solution": q.get("solution"),
                         "type": q.get("type"), **_img_fields(q)})
            page = _new_page(pages)
            on_page = 0
            continue
        if on_page == 2:
            page = _new_page(pages)
            on_page = 0
        page.append({"kind": "question", "num": num, "body": q["body"],
                     "layout": "half", "solution": q.get("solution"),
                     "type": q.get("type"), **_img_fields(q)})
        on_page += 1
    return [p for p in pages if p]


def _paginate_exam(questions):
    choice = [q for q in questions if q.get("type") in _CHOICE]
    blank = [q for q in questions if q.get("type") in _BLANK]
    solve = [q for q in questions if q.get("type") not in _CHOICE | _BLANK]

    pages = []
    page = _new_page(pages)
    num = 1
    sec = 0

    def cn_heading():
        nonlocal sec
        text = _CN_NUM[sec]
        sec += 1
        return text

    for bucket, name in [(choice, "选择题"), (blank, "填空题")]:
        if not bucket:
            continue
        page.append({"kind": "heading", "text": f"{cn_heading()}、{name}"})
        for q in bucket:
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "flow", "solution": q.get("solution"),
                         "type": q.get("type"), **_img_fields(q)})
            num += 1

    if solve:
        solve_heading = f"{cn_heading()}、解答题"
        if any(page):
            page = _new_page(pages)
        on_page = 0
        for idx, q in enumerate(solve):
            if on_page == 2:
                page = _new_page(pages)
                on_page = 0
            block = {"kind": "question", "num": num, "body": q["body"],
                     "layout": "half", "solution": q.get("solution"),
                     "type": q.get("type"), **_img_fields(q)}
            if idx == 0:
                block["heading"] = solve_heading
            page.append(block)
            num += 1
            on_page += 1

    return [p for p in pages if p]


def _paginate_exam_std(questions, std_opts):
    sp = std_opts.get("section_points", {}) if std_opts else {}
    choice = [q for q in questions if q.get("type") in _CHOICE]
    blank = [q for q in questions if q.get("type") in _BLANK]
    solve = [q for q in questions if q.get("type") not in _CHOICE | _BLANK]

    pages = []
    page = _new_page(pages)
    num = 1
    sec = 0

    def cn_heading():
        nonlocal sec
        text = _CN_NUM[sec]
        sec += 1
        return text

    for bucket, name, pkey in [(choice, "选择题", "choice"), (blank, "填空题", "blank")]:
        if not bucket:
            continue
        page.append({"kind": "heading", "text": f"{cn_heading()}、{name}",
                     "points": (sp.get(pkey) or "").strip()})
        for q in bucket:
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "flow", "solution": q.get("solution"),
                         "type": q.get("type"), **_img_fields(q)})
            num += 1

    if solve:
        page.append({"kind": "heading", "text": f"{cn_heading()}、解答题",
                     "points": (sp.get("solve") or "").strip()})
        for q in solve:
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "solve_compact", "solution": q.get("solution"),
                         "type": q.get("type"), **_img_fields(q)})
            num += 1

    return [p for p in pages if p]


def _paginate_handout(questions, keypoints, fullpage_ids):
    pages = []
    if keypoints.strip():
        pages.append([{"kind": "keypoints", "text": keypoints.strip()}])
    pages.extend(_paginate_two(questions, fullpage_ids, start_num=1))
    return pages


# ---------------------------------------------------------------------------
# 渲染：把 paginate() 的页结构转成含 raw-LaTeX 的 Markdown
# ---------------------------------------------------------------------------


def _heading_latex(text: str, points: str = "") -> str:
    label = text
    if points and points.strip():
        p = _latex_escape(points.strip())
        if p.startswith("（") or p.startswith("("):
            label = f"{text}{p}"
        else:
            label = f"{text}（{p}）"
    return (f"\\par\\noindent{{\\large\\bfseries {label}}}"
            f"\\par\\vspace{{0.4em}}")


def _half_block(num: int, body: str, heading: str = "", qtype: str = None,
                 img_align: str = None, img_width=None, img_split=None,
                 img_layouts=None) -> str:
    inner = _q_md(num, body, qtype, img_align, img_width, img_split, img_layouts)
    if heading:
        inner = _raw(_heading_latex(heading)).strip("\n") + "\n\n" + inner
    return (_HALF_OPEN + inner + _HALF_CLOSE)


def _solution_md(text: str) -> str:
    return (_raw("\\par\\noindent{\\bfseries 【解析】}\\par\\nobreak").strip("\n")
            + "\n\n" + _format_options(text))


def _fill_caption(md: str, num: int) -> str:
    caption = f"第{num}题图" if num else ""
    return md.replace(_CAP_SENT, caption)


def _render_block(b: dict, solution_mode: str = "none") -> str:
    kind = b["kind"]
    if kind == "heading":
        return _raw(_heading_latex(b["text"], b.get("points", "")))
    if kind == "keypoints":
        return (_raw("\\par\\noindent{\\large\\bfseries 知识要点}"
                     "\\par\\vspace{0.4em}") + "\n" + b["text"])
    if kind == "solution_head":
        return _raw("\\begin{center}{\\LARGE\\bfseries 参考解析}\\end{center}"
                    "\\vspace{0.6em}")
    if kind == "solution_item":
        return _expand_tables(
            _fill_caption(f"**{b['num']}.** {_format_options(b['text'])}",
                          b["num"]))
    layout = b.get("layout", "flow")
    body = b["body"]
    sol = b.get("solution")
    if solution_mode == "inline" and sol and layout != "half":
        body = body + "\n\n" + _solution_md(sol)

    img_layouts = b.get("img_layouts")
    if layout == "half":
        md = _half_block(b["num"], body, heading=b.get("heading", ""),
                         qtype=b.get("type"), img_align=b.get("img_align"),
                         img_width=b.get("img_width"), img_split=b.get("img_split"),
                         img_layouts=img_layouts)
    else:
        md = _q_md(b["num"], body, b.get("type"), b.get("img_align"),
                   b.get("img_width"), b.get("img_split"),
                   img_layouts)
        if layout == "solve_compact":
            md = md + _raw(f"\\vspace{{{_STD_SOLVE_ANSWER_SPACE}}}")
    return _expand_tables(_fill_caption(md, b["num"]))


def _render_pages(pages: list[list[dict]], solution_mode: str = "none") -> str:
    page_md = []
    for page in pages:
        blocks = [_render_block(b, solution_mode) for b in page]
        page_md.append("\n\n".join(blocks))
    return CLEARPAGE.join(page_md)


_MODES = {
    "list": "清单模式",
    "note": "笔记模式",
    "lecture": "讲解模式",
    "exam": "试卷模式",
    "exam_std": "标准试卷模式",
    "handout": "讲义模式",
}

_STD_SOLVE_ANSWER_SPACE = "5.5em"


def _std_head_latex(title: str, secret_notice: str, exam_notes: str,
                    subject: str = "", info_bar: bool = True) -> str:
    out = ["\\setlength{\\parskip}{0.2em}",
           "\\clubpenalty=10000\\widowpenalty=10000\\interfootnotelinepenalty=10000",
           "\\xeCJKDeclareCharClass{CJK}{\"2605}"]
    sn = (secret_notice or "").strip()
    if sn:
        out.append(f"\\noindent{{\\bfseries {_latex_escape(sn)}}}\\par")
    out.append(f"\\begin{{center}}{{\\LARGE\\bfseries {title}}}\\end{{center}}")
    subj = (subject or "").strip()
    if subj:
        out.append("\\vspace{-0.2em}")
        out.append(f"\\begin{{center}}{{\\large {_latex_escape(subj)}}}\\end{{center}}")
    if info_bar:
        out.append("\\vspace{0.4em}")
        out.append("\\noindent 姓名\\hrulefill\\hspace{2em}班级\\hrulefill"
                   "\\hspace{2em}学号\\hrulefill\\par")
    en = (exam_notes or "").strip()
    if en:
        lines = [ln.strip() for ln in en.splitlines() if ln.strip()]
        out.append("\\vspace{0.4em}")
        out.append("\\noindent{\\bfseries 注意事项：}\\par")
        body = "\\\\\n".join(_latex_escape(ln) for ln in lines)
        out.append(f"\\noindent{{\\small {body}}}\\par")
    out.append("\\vspace{0.6em}\\hrule\\vspace{0.6em}")
    return _raw("\n".join(out))


def build_markdown(questions: list[dict], title: str, mode: str = "list",
                   keypoints: str = "", fullpage_ids=None,
                   solution_mode: str = "none", std_opts: dict = None) -> str:
    pages = paginate(questions, mode=mode, keypoints=keypoints,
                     fullpage_ids=fullpage_ids, solution_mode=solution_mode,
                     std_opts=std_opts)
    body = _render_pages(pages, solution_mode)

    if mode == "exam_std":
        so = std_opts or {}
        head = _std_head_latex(title, so.get("secret_notice", ""),
                               so.get("exam_notes", ""),
                               subject=so.get("subject", ""),
                               info_bar=so.get("info_bar", True))
        parts = ["% ", "", head, body]
    elif mode == "exam":
        head = _raw(
            f"\\begin{{center}}{{\\LARGE\\bfseries {title}}}\\end{{center}}"
            f"\\vspace{{0.6em}}"
        )
        parts = ["% ", "", head, body]
    else:
        parts = [f"% {title}", "", body]
    return "\n".join(parts)


_HF_KEYS = {
    "header_left": "hf_hl", "header_center": "hf_hc", "header_right": "hf_hr",
    "footer_left": "hf_fl", "footer_center": "hf_fc", "footer_right": "hf_fr",
}


def _latex_escape(s: str) -> str:
    repl = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
            "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}"}
    return "".join(repl.get(c, c) for c in s)


def _resolve_hf(text: str, title: str) -> str:
    if not text or not text.strip():
        return ""
    SENT_PAGE = "\x00PAGE\x00"
    SENT_TOTAL = "\x00TOTAL\x00"
    SENT_TITLE = "\x00TITLE\x00"
    s = text.replace("{页码}", SENT_PAGE)
    s = s.replace("{总页数}", SENT_TOTAL)
    s = s.replace("{标题}", SENT_TITLE)
    s = _latex_escape(s)
    s = s.replace(SENT_PAGE, r"\thepage")
    s = s.replace(SENT_TOTAL, r"\pageref{LastPage}")
    s = s.replace(SENT_TITLE, _latex_escape(title))
    return s


def _hf_variable_args(header_footer: dict, title: str) -> list[str]:
    args = []
    hf = header_footer or {}
    any_header = False
    for form_key, var in _HF_KEYS.items():
        val = _resolve_hf(hf.get(form_key, ""), title)
        if val:
            args += ["-V", f"{var}={val}"]
            if form_key.startswith("header"):
                any_header = True
    if any_header:
        args += ["-V", "hf_rule=1"]
    return args


def _clean_output():
    for f in config.OUTPUT_DIR.glob("quiz_*"):
        try:
            f.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 表格：内联 HTML <table> / markdown 管道表格 → raw LaTeX tabular
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table\s*>", re.S | re.I)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr\s*>", re.S | re.I)
_CELL_RE = re.compile(r"<t([dh])\b([^>]*)>(.*?)</t\1\s*>", re.S | re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_COLSPAN_RE = re.compile(r"\bcolspan\s*=\s*[\"']?(\d+)", re.I)

_MATH_SPLIT_RE = re.compile(r"(\$[^$]*\$)")

_TEX_SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def _tex_text(seg: str) -> str:
    return "".join(_TEX_SPECIALS.get(ch, ch) for ch in seg)


def _cell_tex(raw: str) -> str:
    import html

    s = _BR_RE.sub(" ", raw)
    s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    parts = _MATH_SPLIT_RE.split(s)
    for i, part in enumerate(parts):
        if i % 2 == 0:
            parts[i] = _tex_text(part)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


_TABLE_NARROW_COLS = 4


def _table_tex(inner: str) -> str | None:
    rows: list[list[tuple[str, int]]] = []
    for rm in _TR_RE.finditer(inner):
        cells: list[tuple[str, int]] = []
        for cm in _CELL_RE.finditer(rm.group(1)):
            span = _COLSPAN_RE.search(cm.group(2) or "")
            cells.append((_cell_tex(cm.group(3)), max(1, int(span.group(1))) if span else 1))
        if cells:
            rows.append(cells)
    return _rows_to_tex(rows)


def _rows_to_tex(rows: list[list[tuple[str, int]]]) -> str | None:
    if not rows:
        return None
    ncol = max(sum(span for _t, span in r) for r in rows)
    if ncol < 1:
        return None

    if ncol <= _TABLE_NARROW_COLS:
        colspec = "l" * ncol
    else:
        colspec = (r"p{\dimexpr(\linewidth-%d\tabcolsep)/%d\relax}" % (2 * ncol, ncol)) * ncol

    def _row_tex(cells: list[tuple[str, int]], bold: bool = False) -> str:
        out = []
        for text, span in cells:
            body = f"\\textbf{{{text}}}" if bold and text else text
            out.append(f"\\multicolumn{{{span}}}{{l}}{{{body}}}" if span > 1 else body)
        pad = ncol - sum(span for _t, span in cells)
        out.extend([""] * max(0, pad))
        return " & ".join(out) + r" \\"

    lines = [f"\\begin{{tabular}}{{@{{}}{colspec}@{{}}}}", r"\toprule",
             _row_tex(rows[0], bold=True)]
    if len(rows) > 1:
        lines.append(r"\midrule")
        lines.extend(_row_tex(r) for r in rows[1:])
    lines += [r"\bottomrule", r"\end{tabular}"]
    body = "\n".join(lines)
    return ("\\par\\nobreak\\vspace{0.3em}\\noindent\\begin{center}\n"
            + body + "\n\\end{center}\\vspace{0.3em}\\par\\noindent ")


_TABLE_TOKEN_RE = re.compile(r"QFIGTABLE([A-Za-z0-9\-_=]+)QFIGTABLEEND")


def _token(tex: str) -> str:
    b64 = base64.urlsafe_b64encode(tex.encode("utf-8")).decode("ascii")
    return f"\n\nQFIGTABLE{b64}QFIGTABLEEND\n\n"


_PIPE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")


def _pipe_cells(line: str) -> list[tuple[str, int]]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [(_cell_tex(c), 1) for c in s.split("|")]


def _stash_pipe_tables(text: str) -> str:
    if not text or "|" not in text:
        return text
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        head = lines[i].strip()
        if (head.startswith("|") and i + 1 < len(lines)
                and _PIPE_SEP_RE.match(lines[i + 1])):
            rows = [_pipe_cells(lines[i])]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(_pipe_cells(lines[j]))
                j += 1
            tex = _rows_to_tex(rows)
            if tex is not None:
                out.append(_token(tex))
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _stash_tables(text: str) -> str:
    if not text:
        return text

    if "<table" in text.lower():
        def _sub(m):
            tex = _table_tex(m.group(1))
            return m.group(0) if tex is None else _token(tex)
        text = _TABLE_RE.sub(_sub, text)
    return _stash_pipe_tables(text)


def _expand_tables(md: str) -> str:
    def _sub(m):
        tex = base64.urlsafe_b64decode(m.group(1).encode("ascii")).decode("utf-8")
        return _raw(tex)
    return _TABLE_TOKEN_RE.sub(_sub, md)


# ---------------------------------------------------------------------------
# 图片：Obsidian embed 语法 ![[filename]] → xelatex 本地文件
# ---------------------------------------------------------------------------
#
# 软件版存储层没有 web 路径这一层：题目正文/解析里的图片引用就是 Obsidian 的
# embed 语法 ![[<filename>]]，文件平铺存在 config.ASSETS_DIR 下（无 scope 子目录，
# 因为题库不再区分多用户/多题库作用域）。xelatex 在 OUTPUT_DIR 内跑，
# \includegraphics 以 OUTPUT_DIR 为基准找图，故导出前要把引用到的图从 ASSETS_DIR
# 拷贝一份到 OUTPUT_DIR（命名加 quiz_<stamp> 前缀，下次导出 _clean_output 一并清理）。
_QIMG_EXPORT_RE = re.compile(r"!\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")


def _stage_images(questions: list[dict], stem: str) -> list[dict]:
    """把题目/解析里 ![[filename]] 引用的图拷进 OUTPUT_DIR 供 xelatex 用。

    与旧版（/qimages/<scope>/<file> web 路径）的唯一差异：图片来源目录从
    config.IMAGES_DIR/<scope>/ 变成扁平的 config.ASSETS_DIR，且引用语法从
    markdown 图片语法变成 Obsidian embed 语法 ![[filename]]（无 alt 文本、无 scope）。
    其余逻辑（去重拷贝、中性占位 \\qfigmark、图缺失时跳过不中断整份导出）不变。
    """
    import shutil

    cache: dict[str, str] = {}
    counter = [0]

    def _stage_one(fname: str) -> str | None:
        if fname in cache:
            return cache[fname]
        src = config.ASSETS_DIR / fname
        if not src.is_file():
            return None
        ext = Path(fname).suffix or ".png"
        local = f"{stem}_img_{counter[0]}{ext}"
        counter[0] += 1
        try:
            shutil.copy2(src, config.OUTPUT_DIR / local)
        except OSError:
            return None
        cache[fname] = local
        return local

    def _rewrite(text: str) -> str:
        if not text:
            return text

        figs: list[str] = []

        def _sub(m):
            fname = m.group(1)
            local = _stage_one(fname)
            if local is None:
                return ""
            figs.append(local)
            return ""

        stripped = _QIMG_EXPORT_RE.sub(_sub, _stash_tables(text)).rstrip()
        if not figs:
            return stripped
        fig_latex = "".join(f"\\qfigmark{{{name}}}{{{_CAP_SENT}}}" for name in figs)
        return stripped + _raw(fig_latex)

    staged = []
    for q in questions:
        nq = dict(q)
        nq["body"] = _rewrite(q.get("body", ""))
        if q.get("solution"):
            nq["solution"] = _rewrite(q["solution"])
        staged.append(nq)
    return staged


def export(questions: list[dict], title: str = "试卷", fmt: str = "pdf",
           mode: str = "list", keypoints: str = "", fullpage_ids=None,
           header_footer: dict = None, solution_mode: str = "none",
           std_opts: dict = None) -> Path:
    """导出为 tex / pdf / zip（tex + 插图打包），返回产物路径。"""
    if not questions:
        raise ExportError("没有题目可导出")

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _clean_output()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"quiz_{stamp}"
    md_path = config.OUTPUT_DIR / f"{stem}.md"
    tex_path = config.OUTPUT_DIR / f"{stem}.tex"
    pdf_path = config.OUTPUT_DIR / f"{stem}.pdf"

    questions = _stage_images(questions, stem)

    md_path.write_text(
        build_markdown(questions, title, mode=mode, keypoints=keypoints,
                       fullpage_ids=fullpage_ids, solution_mode=solution_mode,
                       std_opts=std_opts),
        encoding="utf-8",
    )

    cmd = [config.PANDOC, str(md_path), "-o", str(tex_path),
           "--template", str(config.TEX_TEMPLATE)]
    cmd += _hf_variable_args(header_footer, title)
    _run(cmd, cwd=config.OUTPUT_DIR, step="pandoc")
    if fmt == "tex":
        return tex_path
    if fmt == "zip":
        return _zip_tex(tex_path, stem)

    for i in range(2):
        _run(
            [config.XELATEX, "-interaction=nonstopmode",
             *(["-halt-on-error"] if i == 0 else []),
             f"{stem}.tex"],
            cwd=config.OUTPUT_DIR,
            step="xelatex",
        )
    if not pdf_path.exists():
        raise ExportError("xelatex 未生成 PDF，请检查 .log 文件")
    return pdf_path


def _zip_tex(tex_path: Path, stem: str) -> Path:
    zip_path = config.OUTPUT_DIR / f"{stem}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(tex_path, arcname=tex_path.name)
        for img in sorted(config.OUTPUT_DIR.glob(f"{stem}_img_*")):
            zf.write(img, arcname=img.name)
    return zip_path


def _run(cmd: list[str], cwd: Path, step: str):
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except FileNotFoundError:
        raise ExportError(f"[{step}] 找不到可执行文件：{cmd[0]}")
    except subprocess.TimeoutExpired:
        raise ExportError(f"[{step}] 超时（>120s）")

    if proc.returncode != 0:
        tail = (proc.stdout or "")[-800:] + (proc.stderr or "")[-800:]
        raise ExportError(f"[{step}] 退出码 {proc.returncode}: {tail}")
