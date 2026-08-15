"""讲义工作台的 Markdown → TeX/PDF/ZIP 导出链路。

讲义编辑器只负责保存结构化 Markdown；最终分页仍交给现有 Pandoc + XeLaTeX
模板。这里刻意复用 exporter 的题目图片与题型排版，避免“快速导出”和讲义工作台
出现两套公式、选项、图片实现。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import uuid

import config
import exporter
import filestore
import handouts


_WIKI_IMAGE_RE = re.compile(r"!\[\[([^\]\r\n]+)\]\]")


def _file_fingerprint(raw_path) -> dict:
    """缓存键只记录工具身份，不执行外部命令，避免每次确认题卡额外启动进程。"""
    path = Path(raw_path)
    try:
        stat = path.resolve(strict=True).stat()
        return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"path": str(path), "missing": True}


def _stage_wimath_logo(meta: dict, stem: str, work_dir: Path) -> str | None:
    """按需把品牌标志放进本次独占导出目录，TeX/ZIP 因而使用同一相对路径。"""
    return exporter._stage_wimath_logo(
        stem, work_dir, bool(meta.get("wimath_logo")))


def _fragment_width(meta: dict) -> str:
    """与 exam_template.tex 的 geometry 严格同源的题卡有效行宽。"""
    if meta.get("page_format") == "slides":
        return "311.658mm"  # 13.333in 页面减去左右各 1.35cm
    if int(meta.get("columns") or 1) == 2:
        return "87.75mm"    # (210mm - 2*13.5mm - 7.5mm 栏距) / 2
    return "160mm"          # A4 左右各 25mm


def _solution_mode(block: dict, metadata: dict) -> str:
    placement = str(block.get("solution_placement") or "inherit")
    if placement == "inherit":
        placement = str(metadata.get("solution_default") or "hidden")
    return placement if placement in {"hidden", "inline", "appendix"} else "hidden"


def _label(block: dict, position: int) -> str:
    override = block.get("number_override")
    # 覆盖题号必须逐字显示，自动题号才补标准句点。
    return str(override) if override not in (None, "") else f"{position}."


def _question_markdown(block: dict, label: str, mode: str,
                       *, slides: bool = False) -> tuple[str, str, str | None]:
    """返回题干、独立内联解析，以及需要放入文末答案区的解析正文。"""
    body = str(block.get("body") or "")
    solution = str(block.get("solution") or "")
    inline_solution = ""
    if mode == "inline" and solution:
        inline_solution = exporter._solution_md(
            solution,
            block.get(exporter._SOL_IMG_FILES_KEY),
            block.get("sol_img_layouts"),
            block.get("sol_img_split"),
        )
    core = exporter._q_md(
        None,
        body,
        block.get("question_type") or block.get("type"),
        block.get("img_align"),
        block.get("img_width"),
        block.get("img_split"),
        block.get("img_layouts"),
        block.get(exporter._IMG_FILES_KEY),
        practice_image_wrap=bool(block.get("_practice")),
    )
    escaped = exporter._latex_escape(label)
    question_md = (
        # 编辑器把已确认题卡当作不可拆顶层块；samepage 让正常高度的题目在最终
        # PDF 中也整体换页/换栏，内容高于一页时 TeX 仍会按必须断开的情况降级。
        exporter._raw(f"\\begin{{samepage}}\\qopen{{{escaped}}}")
        + core
        + exporter._raw("\\qclose\\end{samepage}")
    )
    if inline_solution:
        # 先在题号与 samepage 均已收口后清理题干浮图，再开始解析。解析自身可能以
        # wrapfigure 开头，不能进入 list/samepage/qpracticesolve 等冲突环境。
        inline_solution = (
            exporter._raw("\\qwrapclear").strip("\n")
            + "\n\n" + inline_solution
        )
    if slides:
        # 横版仍保持整张题卡（题干 + 解析）位于左侧 70%；该 minipage 已经由真实
        # XeLaTeX 验证可承载 wrapfigure，冲突的只是上面的题号/list 与 samepage。
        question_md = exporter._slide_left_content(
            question_md + ("\n\n" + inline_solution if inline_solution else ""))
        inline_solution = ""
    return (question_md, inline_solution,
            solution if mode == "appendix" and solution else None)


def _stage_markdown_images(text: str, stem: str, work_dir: Path) -> str:
    """暂存普通讲义段落里的 Obsidian 图片引用。

    题目块由 exporter._stage_images 处理；这里仅处理教师在题目之间自行插入的图片。
    使用 ``stem_img_body_*`` 命名，仍会被现有 tex.zip 打包规则收进压缩包。
    """
    counter = 0
    cached: dict[str, str] = {}

    def replace(match: re.Match) -> str:
        nonlocal counter
        name = match.group(1).strip()
        if name in cached:
            return f"![]({cached[name]})"
        source = exporter._resolve_image_source(name)
        if name.lower().endswith(".svg"):
            pdf_name = str(Path(name.replace("\\", "/")).with_suffix(".pdf"))
            source = exporter._resolve_image_source(pdf_name)
        if source is None:
            return ""
        local = f"{stem}_img_body_{counter}{source.suffix or '.png'}"
        counter += 1
        try:
            shutil.copy2(source, work_dir / local)
        except OSError:
            return ""
        cached[name] = local
        return f"![]({local})"

    return _WIKI_IMAGE_RE.sub(replace, str(text or ""))


def build_markdown(metadata: dict, body: str, *, stem: str = "handout",
                   work_dir: Path | None = None) -> tuple[str, list[str]]:
    """把讲义正文渲染成供 Pandoc 消费的 Markdown。

    返回 ``(markdown, warnings)``。该函数不负责自动分页；只有显式分页标记会变成
    ``\\clearpage``。自动题号完全按题目块在正文中的逻辑位置计算。
    """
    meta = handouts.normalize_metadata(metadata or {})
    blocks, warnings = handouts.parse_content(body, meta.get("question_blocks"))
    questions = [block for block in blocks if block["kind"] == "question"]
    if work_dir is not None and questions:
        staged = exporter._stage_images(questions, stem, work_dir)
        staged_by_id = {block["block_id"]: block for block in staged}
        blocks = [
            staged_by_id.get(block.get("block_id"), block)
            if block["kind"] == "question" else block
            for block in blocks
        ]

    is_double = meta.get("page_format") == "a4" and meta.get("columns") == 2
    rendered: list[str] = []
    appendix: list[tuple[str, dict, str]] = []
    position = 0
    double_solve_seen = False
    for block in blocks:
        if block["kind"] == "markdown":
            text = str(block.get("text") or "")
            if work_dir is not None:
                text = _stage_markdown_images(text, stem, work_dir)
            # 只有显式标记进入磁盘；导出时才换成分页命令。
            rendered.append(text.replace(handouts.PAGE_BREAK_MARKER, exporter.CLEARPAGE))
            if is_double and handouts.PAGE_BREAK_MARKER in text:
                # 显式分页已开启新页左栏，下一道大题无需再额外跳到右栏。
                double_solve_seen = False
            continue
        position += 1
        block["_practice"] = is_double
        label = _label(block, position)
        mode = _solution_mode(block, meta)
        question_md, inline_solution, appendix_solution = _question_markdown(
            block, label, mode, slides=meta.get("page_format") == "slides")
        qtype = block.get("question_type") or block.get("type")
        is_solve = qtype not in exporter._SINGLE | exporter._MULTI | exporter._BLANK
        if is_double and is_solve:
            question_md = (
                exporter._raw("\\begin{qpracticesolve}").strip("\n")
                + "\n\n" + question_md + "\n\n"
                + exporter._raw("\\end{qpracticesolve}").strip("\n")
            )
            if double_solve_seen:
                question_md = (
                    exporter._raw("\\columnbreak").strip("\n")
                    + "\n\n" + question_md
                )
            double_solve_seen = True
        if inline_solution:
            question_md += "\n\n" + inline_solution
        rendered.append(question_md)
        if appendix_solution:
            appendix.append((label, block, appendix_solution))

    document = "\n\n".join(part.strip("\n") for part in rendered if part.strip())
    if appendix:
        items = [
            exporter._raw(
                "\\begin{center}{\\LARGE\\bfseries 参考解析}\\end{center}"
                "\\vspace{0.6em}"
            )
        ]
        for label, block, solution in appendix:
            solution_body = exporter._solution_body(
                solution,
                block.get(exporter._SOL_IMG_FILES_KEY),
                block.get("sol_img_layouts"),
                block.get("sol_img_split"),
            )
            # solution_body 可能以 raw-LaTeX 围栏开头，必须另起段落保持围栏行首。
            items.append(f"**{label}**\n\n{solution_body}")
        document += exporter.CLEARPAGE + "\n\n".join(items)

    if is_double:
        # 显式分页必须结束并重开 multicols，否则 clearpage 在双栏环境内只换栏。
        pages = document.split(exporter.CLEARPAGE)
        document = exporter.CLEARPAGE.join(
            exporter._raw("\\qpracticebegin")
            + page.strip()
            + exporter._raw("\\qpracticeend")
            for page in pages
        )
    return document.rstrip() + "\n", warnings


def export(metadata: dict, body: str, fmt: str = "pdf") -> Path:
    """导出讲义当前内存草稿；无需先覆盖磁盘文件。"""
    if fmt not in {"pdf", "tex", "zip"}:
        raise exporter.ExportError("不支持的讲义导出格式")
    meta = handouts.normalize_metadata(metadata or {})
    with exporter._EXPORT_SLOTS:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        work_dir = config.OUTPUT_DIR / f"handout_{stamp}_{uuid.uuid4().hex}"
        work_dir.mkdir()
        stem = f"handout_{stamp}"
        md_path = work_dir / f"{stem}.md"
        tex_path = work_dir / f"{stem}.tex"
        pdf_path = work_dir / f"{stem}.pdf"

        markdown, _warnings = build_markdown(
            meta, filestore.normalize_newlines(body), stem=stem, work_dir=work_dir)
        md_path.write_text(markdown, encoding="utf-8", newline="\n")

        command = [
            config.PANDOC, str(md_path), "-o", str(tex_path),
            "--template", str(config.TEX_TEMPLATE),
        ]
        if meta.get("page_format") == "slides":
            command += ["-V", "slides=1"]
        elif meta.get("columns") == 2:
            command += ["-V", "practice=1"]
        logo_name = _stage_wimath_logo(meta, stem, work_dir)
        if logo_name:
            command += ["-V", f"wimath_logo={logo_name}"]
        command += exporter._paper_tone_variable_args(meta.get("paper_tone", "white"))
        command += exporter._hf_variable_args(
            meta.get("header_footer"), meta.get("title") or "讲义")
        exporter._run(command, cwd=work_dir, step="pandoc")
        if fmt == "tex":
            return tex_path
        if fmt == "zip":
            return exporter._zip_tex(tex_path, stem, work_dir)

        for index in range(2):
            exporter._run(
                [config.XELATEX, "-interaction=nonstopmode",
                 *(["-halt-on-error"] if index == 0 else []), f"{stem}.tex"],
                cwd=work_dir,
                step="xelatex",
            )
        if not pdf_path.is_file():
            raise exporter.ExportError("xelatex 未生成 PDF，请检查 .log 文件")
        return pdf_path


def render_question(metadata: dict, question: dict, position: int) -> Path:
    """使用正式导出模板把单题编译成无字体依赖的 SVG。

    SVG 只作为可丢弃缓存，不进入 Markdown 真源。题号、解析位置、题型图片和栏宽均
    走正式导出函数；移动题目或切换版面后，输入摘要变化会自然命中新的缓存文件。
    """
    meta = handouts.normalize_metadata(metadata or {})
    if not isinstance(question, dict):
        raise exporter.ExportError("题卡编译数据无效")
    try:
        logical_position = max(1, int(position))
    except (TypeError, ValueError):
        logical_position = 1
    body = filestore.normalize_newlines(str(question.get("body") or ""))
    solution = filestore.normalize_newlines(str(question.get("solution") or ""))
    if len((body + solution).encode("utf-8")) > 2 * 1024 * 1024:
        raise exporter.ExportError("单个题卡超过 2 MiB，无法实时编译")

    # 延迟导入，避免 desktop_product -> service_ports -> handout_exporter 的启动环。
    import desktop_product

    payload = {
        "implementation": {
            "product_version": desktop_product.PRODUCT_VERSION,
            "handout_exporter": _file_fingerprint(__file__),
            "exporter": _file_fingerprint(exporter.__file__),
            "template": _file_fingerprint(config.TEX_TEMPLATE),
            "pandoc": _file_fingerprint(config.PANDOC),
            "xelatex": _file_fingerprint(config.XELATEX),
            "dvisvgm": _file_fingerprint(config.DVISVGM),
        },
        "page_format": meta.get("page_format"),
        "columns": meta.get("columns"),
        "solution_default": meta.get("solution_default"),
        "position": logical_position,
        "question": question,
        "body": body,
        "solution": solution,
    }
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    cache_dir = config.OUTPUT_DIR / "handout_card_cache"
    svg_path = cache_dir / f"{digest}.svg"
    if cache_dir.is_symlink():
        raise exporter.ExportError("题卡缓存目录不能是符号链接")
    if svg_path.is_file() and not svg_path.is_symlink():
        try:
            os.utime(svg_path, None)
        except OSError:
            pass
        return svg_path

    with exporter._EXPORT_SLOTS:
        if svg_path.is_file() and not svg_path.is_symlink():
            return svg_path
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        work_dir = config.OUTPUT_DIR / f"handout_card_{digest[:12]}_{uuid.uuid4().hex}"
        work_dir.mkdir()
        stem = f"card_{digest[:16]}"
        md_path = work_dir / f"{stem}.md"
        tex_path = work_dir / f"{stem}.tex"
        pdf_path = work_dir / f"{stem}.pdf"
        local_svg = work_dir / f"{stem}.svg"

        block = {
            **question,
            "kind": "question",
            "block_id": str(question.get("block_id") or f"q_{digest[:16]}"),
            "body": body,
            "solution": solution,
            "_practice": meta.get("page_format") == "a4" and meta.get("columns") == 2,
        }
        staged = exporter._stage_images([block], stem, work_dir)[0]
        mode = _solution_mode(staged, meta)
        markdown, inline_solution, _appendix = _question_markdown(
            staged, _label(staged, logical_position), mode,
            slides=meta.get("page_format") == "slides")
        if block["_practice"]:
            markdown = (
                exporter._raw("\\qfragmentpracticebegin")
                + markdown + ("\n\n" + inline_solution if inline_solution else "")
                + exporter._raw("\\qfragmentpracticeend")
            )
        elif inline_solution:
            markdown += "\n\n" + inline_solution
        md_path.write_text(markdown.rstrip() + "\n", encoding="utf-8", newline="\n")

        command = [
            config.PANDOC, str(md_path), "-o", str(tex_path),
            "--template", str(config.TEX_TEMPLATE),
            "-V", "fragment=1", "-V", f"fragment_width={_fragment_width(meta)}",
        ]
        if meta.get("page_format") == "slides":
            command += ["-V", "slides=1"]
        elif meta.get("columns") == 2:
            # 题卡与最终双栏 PDF 共用 practice 字体选择；题卡本身是否开启 multicol
            # 仍由正文中的 qfragmentpractice 宏隔离，不能另造模板未识别的变量。
            command += ["-V", "practice=1"]
        exporter._run(command, cwd=work_dir, step="pandoc")
        exporter._run([
            config.XELATEX, "-no-shell-escape", "-interaction=nonstopmode",
            "-halt-on-error", f"{stem}.tex",
        ], cwd=work_dir, step="xelatex")
        if not pdf_path.is_file():
            raise exporter.ExportError("题卡 XeLaTeX 编译后未生成 PDF")
        exporter._run([
            config.DVISVGM, "--pdf", "--page=1", "--no-fonts",
            f"--output={local_svg.name}", pdf_path.name,
        ], cwd=work_dir, step="dvisvgm")
        if not local_svg.is_file():
            raise exporter.ExportError("题卡 PDF 转 SVG 失败")
        temp_svg = cache_dir / f".{digest}.{uuid.uuid4().hex}.tmp"
        shutil.copy2(local_svg, temp_svg)
        os.replace(temp_svg, svg_path)
        return svg_path
