"""好题、好卷、好资料自动导入 Profile。

Profile 层只编排识别结果与题卡元数据；OCR 本身由调用方注入，便于监听器和
离线测试复用现有 converter 而不在这里读取凭据。

三条链路的内容语义（2026-10-05 分叉）：
- 好题：单张截图 = 一道题。**不切题**——走 converter 的 single_block 通道，
  只在开头识别一次题号（进文件名、从正文剥离）。
- 好卷：试卷 = 多道题。沿用切题（importer.split_questions）逐题成卡。
- 好资料：讲义/书籍 = 正文/题目/解析交替的流。走纯文本出口 + 流式分块，
  每块保存来源页码，并生成一张"页码索引"卡按页链接到块卡。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import uuid
from pathlib import Path

import importer


_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_QUESTION_NUMBER = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__|\*)?\s*第?\s*(\d{1,3})\s*(?:\*\*|__|\*)?\s*[.．、题]"
)

# 资料分块的围栏/表格判据：与 importer 的切题链路同一模式。行级判定复用
# importer 的公开 helper（question_head_lines / is_solution_head），这两条
# 只负责"别把代码块里的编号当结构"。
_MATERIAL_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")

# 题干信号（题界第二道闸门）：讲义里"1. 定义 2. 定理"这类章节编号列表与题号
# 共享递增链形态，但几乎没有作答证据。判据刻意宽松——误判的代价只是多几张卡
# （文字都在），漏判才会把题目埋进正文块。
_MATERIAL_BLANK_RE = re.compile(r"_{2,}|\\underline|\\hspace")
_MATERIAL_EMPTY_PAREN_RE = re.compile(r"[(（]\s*[)）]")
_MATERIAL_COMMAND_RE = re.compile(
    r"求|证明|计算|化简|解方程|解不等式|判断|说明理由|讨论|写出")


def sanitize_output_name(raw: str, suffix: str = "") -> str:
    """收敛为 Windows 可用且不超过 120 字符的普通文件名片段。"""
    name = Path(str(raw or "")).stem.strip().rstrip(" .")
    name = _INVALID_NAME.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "未命名"
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(?:COM|LPT)[1-9]", name, re.I):
        name = f"_{name}"
    return (name + str(suffix))[:120].rstrip(" .") or "未命名"


def extract_question_number(text: str) -> tuple[int | None, str]:
    """返回首行主题号及移除该主题号后的题干，保留小题号和选项。"""
    body = str(text or "").lstrip()
    match = _QUESTION_NUMBER.match(body)
    if not match:
        return None, body
    number = int(match.group(1))
    stripped = body[match.end():].lstrip()
    return (number if number > 0 else None), stripped


def _source_id(source: Path) -> str:
    return hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _write_card(path: Path, body: str, *, source: Path, profile: str,
                meta: dict | None = None, kind: str = "question") -> None:
    """写一张卡片。

    kind="question" 会被题库扫描收录为题目；kind="document"（正文/解析/
    索引卡）由 filestore._is_question_meta 排除在题库之外——讲义正文若按
    题目收录会污染组卷候选与查重。身份字段走显式参数，不靠 meta 覆盖。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # created/updated 与 filestore._now_iso 同一格式：没有它们题库列表的
    # 时间列是空的，排序也没依据。
    now = _now_iso()
    data = {
        "quizforge_kind": kind,
        "id": uuid.uuid4().hex,
        "source": source.name,
        "qf_source_kind": profile,
        "created": now,
        "updated": now,
    }
    data.update(meta or {})
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}"
                        for k, v in data.items()] + ["---", "", body.strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _unique_card_name(used: set[str], base: str) -> str:
    """同一版本目录内不重名（Windows 大小写不敏感）；冲突缀 `_2`、`_3`……。

    讲义每章题号从 1 重数是常态：`第1题` 会反复命中同一路径，直接写盘就是
    静默覆盖。casefold 比较与 Windows 文件系统语义一致。
    """
    name = base
    suffix = 2
    while name.casefold() in used:
        name = sanitize_output_name(f"{base}_{suffix}")
        suffix += 1
    used.add(name.casefold())
    return name


def _wrap_math_symbols(text: str) -> str:
    """把成品题卡文本里的裸字母/数字包上 `$\\displaystyle $`。

    与手动导入链路的收尾同一口径（agent_services 导入预览的最后一步）——
    实时监控链路绕过那道收尾，题卡里的"直线 AC 上的点 P"曾以裸文本入库。
    这一步只能在**切好并清干净的成品文本**上做：mechfix 顶部标定注释明确
    禁止在整块规范化里盲包（那时正文混着破损公式碎片与图片哈希名），所以
    绝不能挪进 `normalize_block`。字母在前、数字在后（反序会挡掉一批本该
    包的字母，见 wrap_bare_numbers 的 docstring）。
    """
    import mechfix
    return mechfix.wrap_bare_numbers(mechfix.wrap_bare_letters(str(text or "")))


def _result_markdown(result) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("markdown", "md", "text", "body"):
            if isinstance(result.get(key), str):
                return result[key]
    raise ValueError("OCR 结果缺少 Markdown 文本")


def _is_image_source(source: Path) -> bool:
    return Path(source).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _ocr(source: Path, workspace: Path, *, ocr_backend: str, engine: str,
         provider=None, note_sink=None) -> str:
    """调用可选的大模型链路 OCR；真实监听器通过 monkeypatch/injector 提供它。"""
    import converter
    runner = getattr(converter, "source_ocr", None)
    if callable(runner):
        # runner 是测试/未来注入点，签名冻结：不给它传 note_sink，避免注入方
        # 不认识新关键字而 TypeError。
        return _result_markdown(runner(source, workspace,
                                       ocr_backend=ocr_backend, engine=engine))
    import mineru_store
    return converter.convert_file(
        source, mineru_store.resolve(), is_image=_is_image_source(source),
        include_solution=True, engine=engine, ocr_backend=ocr_backend,
        provider=provider, note_sink=note_sink)


def _ocr_mechanical(source: Path, workspace: Path, *, ocr_backend: str,
                    note_sink=None) -> str:
    """no_ai 两阶段机械链路：OCR + 切块 → 跳过 AI 直接机械渲染。

    好卷的默认路径（好题走 single_block 专线、好资料走纯文本出口，都不经
    这里）。不解析也不回落任何 LLM 配置（provider=None 会回落 .env 的
    DeepSeek，绝不能拿它冒充 no-AI）。
    """
    import converter
    import mineru_store

    pending = converter.convert_file_to_blocks(
        Path(source).resolve(), mineru_store.resolve(),
        is_image=_is_image_source(source), keep_images=True,
        ocr_backend=ocr_backend, note_sink=note_sink)
    return converter.finish_block_review(
        pending, action="skip", include_solution=True, note_sink=note_sink)


def _extract(source: Path, workspace: Path, *, ocr_backend: str, engine: str,
             provider, normalize: bool, note_sink=None) -> str:
    if normalize:
        return _ocr(source, workspace, ocr_backend=ocr_backend, engine=engine,
                    provider=provider, note_sink=note_sink)
    return _ocr_mechanical(source, workspace, ocr_backend=ocr_backend,
                           note_sink=note_sink)


# ---------------------------------------------------------------------------
# 好题：单张截图 = 一道题（不切题）
# ---------------------------------------------------------------------------


def convert_good_question(source: Path, workspace: Path, *, ocr_backend: str,
                          engine: str, provider=None, normalize: bool = True) -> dict:
    """好题专线：整段文本按**一道题**处理，不切题。

    截图没有可切的顶层题号，走切块链路必然报"没能从中切出任何题目"。
    题号只在开头识别一次：进题卡文件名（`源名第N题.md`）并从正文剥离；
    识别不到就保留源名。检测到多道题只写软警告（不失败）。
    """
    import converter
    import mineru_store

    source = Path(source).resolve()
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    pending = converter.convert_file_to_blocks(
        source, mineru_store.resolve(), is_image=_is_image_source(source),
        keep_images=True, ocr_backend=ocr_backend, single_block=True)
    notes: list[str] = []
    if normalize:
        if provider is None:
            # 与手动导入一致的解析口径；拿不到配置由 converter 给出清晰错误。
            import providers
            provider = providers.resolve_active()
        markdown = converter.finish_block_review(
            pending, action="ai", include_solution=True, provider=provider,
            note_sink=notes.append)
    else:
        markdown = converter.finish_block_review(
            pending, action="skip", include_solution=True,
            note_sink=notes.append)
    # 多题软检测跑在**渲染前**的整段清洗文本上：渲染会把题号归一化剥补，
    # 渲染后的切题信号反而失真；无题号截图本来也切不开，不会误报。
    pre_text = ""
    if pending.get("blocks"):
        pre_text = str(pending["blocks"][0].get("text") or "")
    warnings = list(notes)
    if len(importer.split_questions(pre_text)) > 1:
        warnings.append("好题输入识别到多道题，需人工校对")
    blocks = importer.split_questions(markdown)
    head = blocks[0] if blocks else markdown.strip()
    number = importer.block_number(head)
    body = importer.strip_leading_number(importer.strip_type_tag(head))
    # 裸字母/数字包 `$\displaystyle $`：手动导入链路的收尾有这一步，监控链路
    # 此前没有——题卡里的"点 P""A、B、C、D"一直是裸文本。
    body = _wrap_math_symbols(body)
    if not body.strip():
        raise ValueError("识别结果为空，无法生成题卡")
    base = sanitize_output_name(source.name)
    if number is not None:
        base = sanitize_output_name(source.name, f"第{number}题")
    out = workspace / f"{base}.md"
    _write_card(out, body, source=source, profile="good_question",
                meta={"number": number, "qf_review_required": bool(warnings)})
    return {"profile": "good_question", "source": str(source),
            "cards": [str(out.relative_to(workspace))], "warnings": warnings,
            "staged_root": str(workspace)}


# ---------------------------------------------------------------------------
# 好卷：试卷 = 多道题（切题，逐题成卡）
# ---------------------------------------------------------------------------


def convert_good_paper(source: Path, workspace: Path, *, ocr_backend: str,
                       engine: str, provider=None, normalize: bool = True) -> dict:
    """好卷：识别后走与手动 /import 同一条"原版"切卡链路。

    成品 Markdown 交给 agent_services._build_import_preview（手动导入的
    预览与免审入库共用的切卡函数）：题型判定、两套题号配对解析、mechfix
    收尾（裸字母/数字包裹、选项规范化、解析排版）、本次文本内查重、缺号
    账本全在那里，监控不再维护第二套（此前的 _split_blocks 不做题号校验、
    题卡也缺 type/number，漏题还会静默成卡）。缺号账本按 min(题号) 起算，
    题号从任意标号起连续都成立——这就是"不强制从 1 开始"的落点。

    产物形态：每题一卡（解析以 `## 解析` 段并入同一张卡，与手动导入一致），
    frontmatter 带 type/number/order 与图片布局字段。
    """
    notes: list[str] = []
    markdown = _extract(Path(source).resolve(), Path(workspace),
                        ocr_backend=ocr_backend, engine=engine,
                        provider=provider, normalize=normalize,
                        note_sink=notes.append)
    source = Path(source).resolve()
    # 惰性导入：agent_services 模块级 import source_ingest，而 source_ingest
    # 模块级 import 本模块，模块级反向导入会成环。刻意用属性调用（而不是
    # from ... import），让测试能 mock.patch.object(agent_services,
    # "_build_import_preview") 命中监控链路。
    import agent_services
    preview, _cols, missing = agent_services._build_import_preview(
        markdown, include_solution=True, only_numbers=None,
        existing_fps=set(), all_cols=[], boundary_mode="auto")
    warnings = list(notes)
    if missing:
        warnings.append("疑似缺题号：" + "、".join(str(n) for n in missing))
    document_id = _source_id(source)
    version_id = uuid.uuid4().hex
    root = Path(workspace) / sanitize_output_name(source.name)
    used: set[str] = set()
    question_seq = 0
    cards = []
    import filestore
    for index, row in enumerate(preview, 1):
        number = row.get("number")
        if number is not None:
            base = f"{sanitize_output_name(source.name)}第{number}题"
        else:
            # 题号缺失时保留顺序编号兜底；warnings 里已提示人工校对。
            question_seq += 1
            base = f"{sanitize_output_name(source.name)}第{question_seq}题"
        # 解析与备注按题库读取口径拼进正文：`## 解析` 段是 filestore 认的
        # 解析分区，备注段与手动导入入库（_auto_import_items）同形。
        extra = [("备注", row["note"])] if row.get("note") else []
        body = filestore._join_sections(row.get("body") or "",
                                        row.get("solution") or "", extra)
        meta = {
            "source_document_id": document_id,
            "source_document_name": source.name,
            "source_version_id": version_id,
            "source_block_id": uuid.uuid4().hex,
            "source_block_index": index,
            # 解析已并入题卡，这里恒为 question（旧的独立解析卡不再生成）。
            "source_block_type": "question",
            # 好卷暂未接入页锚注入（页码只对"好资料"资料库语义有意义）。
            "source_page_start": None,
            "source_page_end": None,
            "source_order": index,
            "type": row.get("type") or "",
            "number": number,
            # 卷内顺序：与手动导入逐题递增的 order 口径一致，整卷新卡不再
            # 全部落 0.0 在目标目录里并列排序。
            "order": float(index),
        }
        if row.get("img_split"):
            meta["img_split"] = row["img_split"]
            meta["img_layouts"] = row.get("img_layouts") or []
        if row.get("sol_img_split"):
            meta["sol_img_split"] = row["sol_img_split"]
            meta["sol_img_layouts"] = row.get("sol_img_layouts") or []
        stem = _unique_card_name(used, sanitize_output_name(base))
        relative = Path(f"{stem}.md")
        _write_card(root / relative, body, source=source, profile="good_paper",
                    meta=meta)
        cards.append(str(root.name + "/" + relative.as_posix()))
    return {"profile": "good_paper", "source": str(source), "cards": cards,
            "source_document_id": document_id, "source_version_id": version_id,
            "warnings": warnings, "staged_root": str(workspace)}


# ---------------------------------------------------------------------------
# 好资料：正文 / 题目 / 解析流式分块 + 页码
# ---------------------------------------------------------------------------


def _material_question_signal(lines: list[str], start: int, stop: int) -> bool:
    """候选题块（start 起到 stop 前）是否具备"题干特征"。

    讲义/教材的章节编号列表（"1. 定义…"）与题号共享递增链形态，区别在作答
    证据：填空位、空括号、选项组、指令动词。
    """
    text = "\n".join(lines[start:stop])
    if _MATERIAL_BLANK_RE.search(text) or _MATERIAL_EMPTY_PAREN_RE.search(text):
        return True
    if _MATERIAL_COMMAND_RE.search(text):
        return True
    import mechfix
    return bool(mechfix.looks_like_choice_options(text))


def _split_material_blocks(markdown: str) -> list[dict]:
    """资料全文 → 正文/题目/解析交替的块序列（每块带 1 基页码区间）。

    题界候选来自 importer.question_head_lines（围栏外、题号成递增链），再过
    "题干信号"第二道闸门；解析起点用 importer.is_solution_head（行首档）。
    页界来自带页码的标记行（converter.inject_content_page_breaks 注入），
    未注入时所有块页码为 None。首个题界之前的内容永远收进正文块——一个字
    都不丢。
    """
    import blocksplit

    lines = str(markdown or "").splitlines()
    if not lines:
        return []
    fenced: list[bool] = []
    in_fence = False
    for line in lines:
        opener = bool(_MATERIAL_FENCE_RE.match(line))
        if opener:
            in_fence = not in_fence
        fenced.append(in_fence or opener)

    candidates = importer.question_head_lines(lines)
    question_starts: set[int] = set()
    if len(candidates) >= 2:
        ordered = sorted(candidates)
        for pos, line_no in enumerate(ordered):
            stop = ordered[pos + 1] if pos + 1 < len(ordered) else len(lines)
            if _material_question_signal(lines, line_no, stop):
                question_starts.add(line_no)

    blocks: list[dict] = []
    current_kind = "body"
    current: list[str] = []
    current_start = 1
    page = 1

    def flush() -> None:
        nonlocal current, current_kind
        text = "\n".join(current).strip()
        if text:
            number = (importer.block_number(text)
                      if current_kind == "question" else None)
            blocks.append({"text": text, "kind": current_kind, "number": number,
                           "page_start": current_start, "page_end": page})
        current = []

    for line_no, line in enumerate(lines):
        marked = blocksplit.parse_source_page_break(line)
        if marked is not None:
            # 页界标记：先收束当前块（页号属上一页），再翻页；标记本身不输出。
            # 收束后类型重置为正文——页首到下一个题界之间的内容（章节标题、
            # 引言）是正文；不重置的话会被上一块的类型吞进去（题块把章节
            # 标题带走，还会因同块重复取号撞名）。
            flush()
            current_kind = "body"
            page = max(1, int(marked))
            current_start = page
            continue
        if not fenced[line_no] and line_no in question_starts:
            flush()
            current_kind = "question"
            current_start = page
            current = [line]
            continue
        if (not fenced[line_no] and current_kind != "solution"
                and importer.is_solution_head(line)
                and any(item.strip() for item in current)):
            # 解析块只在"当前有内容"时另起——题号前的孤立"解："行留在正文里，
            # 避免产出无头解析卡。
            flush()
            current_kind = "solution"
            current_start = page
            current = [line]
            continue
        current.append(line)
    flush()
    return blocks


def _write_material_index(root: Path, root_name: str,
                          entries: list[tuple[str, dict]], page_info: dict,
                          source: Path, document_id: str, version_id: str,
                          warnings: list[str], used: set[str]) -> Path:
    """写资料版本目录的页码索引卡（document，不进题库）。

    entries 按阅读序 [(卡名, 块)]；每行"第 N 页（区间）→ [[卡名]]"，
    在 Obsidian 与 QuizForge 阅读模式里都可点击跳转。无页码的块收进
    "页码不详"区，不丢条目。
    """
    total = int(page_info.get("page_total") or 0)
    located = len(page_info.get("located_pages") or [])
    status_map = {"reliable": "可靠", "partial": "部分页缺失，页码为参考值",
                  "unavailable": "未获得（页码不详）",
                  "unsupported": "当前 OCR 后端不支持页锚"}
    status_text = status_map.get(str(page_info.get("status") or ""),
                                 "未获得（页码不详）")
    lines = [f"# {root_name} · 页码索引", "",
             f"- 源文件：{source.name}",
             f"- 页码定位：{status_text}"
             + (f"（{located}/{total} 页）" if total else "")]
    if warnings:
        lines.append("- 提示：" + "；".join(warnings))
    lines.append("")
    listed = False
    for stem, block in entries:
        start = block.get("page_start")
        if start is None:
            continue
        end = block.get("page_end") or start
        label = f"第 {start} 页" if start == end else f"第 {start}–{end} 页"
        lines.append(f"- {label}：[[{stem}]]")
        listed = True
    unknown = [stem for stem, block in entries if block.get("page_start") is None]
    if unknown:
        lines.extend(["", "## 页码不详", ""])
        lines.extend(f"- [[{stem}]]" for stem in unknown)
    if not listed and not unknown:
        lines.append("（没有可索引的块）")
    name = _unique_card_name(used, sanitize_output_name(f"{root_name}_页码索引"))
    path = root / f"{name}.md"
    _write_card(path, "\n".join(lines), source=source,
                profile="good_material", kind="document", meta={
                    "source_document_id": document_id,
                    "source_document_name": source.name,
                    "source_version_id": version_id,
                    "source_block_type": "index"})
    return path


def convert_good_material(source: Path, workspace: Path, *, ocr_backend: str,
                          engine: str, provider=None, normalize: bool = True) -> dict:
    """好资料专线：一份资料 → 一个版本目录（正文块/题块/解析块 + 页码索引）。

    文本获取走 converter.convert_file_plain_text（不切题、不做题目向排版）；
    分块后勾选了"大模型规范化"时只对**题块/解析块**逐个走 LLM——正文是散文，
    题目向 prompt 只会劣化它（改标点、按"解："劈段）。
    """
    import converter
    import mineru_store

    source = Path(source).resolve()
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    text, page_info = converter.convert_file_plain_text(
        source, mineru_store.resolve(), is_image=_is_image_source(source),
        ocr_backend=ocr_backend, inject_pages=True)
    blocks = _split_material_blocks(text)
    warnings: list[str] = []
    if page_info.get("status") == "partial":
        warnings.append(
            f"部分页未能定位（{len(page_info.get('skipped') or [])} 页），"
            "块卡页码为参考值")
    elif page_info.get("status") in {"unavailable", "unsupported"}:
        warnings.append("未获得可靠页码：块卡页码为空，索引卡归入页码不详")
    if normalize:
        targets = [i for i, block in enumerate(blocks)
                   if block["kind"] in ("question", "solution")]
        if targets:
            if provider is None:
                import providers
                provider = providers.resolve_active()
            normalized = converter.normalize_plain_blocks(
                [blocks[i]["text"] for i in targets], provider,
                note_sink=warnings.append)
            for i, value in zip(targets, normalized):
                blocks[i]["text"] = value
    root_name = sanitize_output_name(source.name)
    root = workspace / root_name
    document_id = _source_id(source)
    version_id = uuid.uuid4().hex
    used: set[str] = set()
    counts = {"body": 0, "solution": 0, "question": 0}
    entries: list[tuple[str, dict]] = []
    cards: list[str] = []
    for index, block in enumerate(blocks, 1):
        kind = block["kind"]
        counts[kind] += 1
        if kind == "question":
            number = block.get("number")
            base = (f"{root_name}第{number}题" if number is not None
                    else f"{root_name}第{counts['question']}题")
        else:
            label = "正文" if kind == "body" else "解析"
            base = f"{root_name}{label}第{counts[kind]}块"
        stem = _unique_card_name(used, sanitize_output_name(base))
        relative = Path(f"{stem}.md")
        path = root / relative
        # 题块/解析块做裸字母数字包裹（同手动导入收尾，机械与 LLM 输出统一）；
        # 正文块是散文，不包——wrap 的标定场景是题干/解析，对连续散文（尤其
        # 英文句子里的冠词 a/I）误包代价更高。
        text = (_wrap_math_symbols(block["text"])
                if kind in ("question", "solution") else block["text"])
        _write_card(
            path, text, source=source, profile="good_material",
            kind="question" if kind == "question" else "document", meta={
                "source_document_id": document_id,
                "source_document_name": source.name,
                "source_version_id": version_id,
                "source_block_id": uuid.uuid4().hex,
                "source_block_index": index,
                "source_block_type": kind,
                "source_order": index,
                "source_page_start": block.get("page_start"),
                "source_page_end": block.get("page_end"),
                "number": block.get("number"),
            })
        entries.append((stem, block))
        cards.append(str(root.name + "/" + relative.as_posix()))
    index_path = _write_material_index(
        root, root_name, entries, page_info, source, document_id, version_id,
        warnings, used)
    cards.append(str(root.name + "/" + index_path.name))
    return {"profile": "good_material", "source": str(source), "cards": cards,
            "source_document_id": document_id, "source_version_id": version_id,
            "warnings": warnings,
            "page_info": {"status": page_info.get("status"),
                          "page_total": page_info.get("page_total"),
                          "located": len(page_info.get("located_pages") or []),
                          "skipped": len(page_info.get("skipped") or [])},
            "staged_root": str(workspace)}


def build_output_manifest(profile: str, source: Path, staged_root: Path) -> dict:
    import hashlib
    root = Path(staged_root)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files.append({"path": path.relative_to(root).as_posix(), "sha256": digest})
    if not files:
        raise ValueError("输出没有文件")
    return {"profile": profile, "source_path": str(Path(source).resolve()),
            "files": files}
