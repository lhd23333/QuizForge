"""Agent 任务服务（转换 / 导入 / 导出）与审批执行核心。

本模块是 GUI（app.py）与 CLI（cli.py）共用的服务实现层：**不导入 Flask**。
运行时依赖通过 :func:`configure` 注入：

- ``runtime``：``agent_core.AgentRuntime`` 实例（会话与持久化）
- ``approvals``：``agent_approvals.ApprovalStore`` 实例（审批与审计）
- ``url_builder``：GUI 注入 Flask ``url_for``；CLI 不注入时回落到等价路径

``agent_catalog`` 只在导出模板分支内延迟导入（该模块是 Blueprint 路由层，
顶层导入会把 Flask 带进来）；因此 ``import agent_services`` 本身零 Flask 依赖。
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import agent_actions
import agent_approvals as agent_approval_module
import agent_handouts
import agent_tools
import agent_upload as agent_upload_store
import blocksplit
import config
import converter
import dedup
import exporter
import filestore
import history_store
import import_defaults
import importer
import mechfix
import providers
import qrender
import service_ports
import task_store
from search_query import SearchQueryError

logger = logging.getLogger(__name__)

agent_runtime = None
agent_approval_store = None
_url_builder = None


def configure(*, runtime=None, approvals=None, url_builder=None) -> None:
    """注入运行环境。GUI 与 CLI 各自在初始化后调用一次。"""
    global agent_runtime, agent_approval_store, _url_builder
    if runtime is not None:
        agent_runtime = runtime
    if approvals is not None:
        agent_approval_store = approvals
    _url_builder = url_builder if callable(url_builder) else None


def _runtime():
    if agent_runtime is None:
        raise agent_tools.ToolError(
            "Agent 服务运行环境未初始化：请先调用 agent_services.configure()")
    return agent_runtime


def _approvals():
    if agent_approval_store is None:
        raise agent_tools.ToolError(
            "Agent 审批存储未初始化：请先调用 agent_services.configure()")
    return agent_approval_store


def _catalog():
    """延迟导入 agent_catalog（Blueprint 路由层），避免模块级引入 Flask。"""
    import agent_catalog as mod
    return mod


def _agent_url(endpoint: str, **values) -> str:
    """构造对外 URL：GUI 走 Flask url_for，CLI 回落到等价绝对路径。"""
    if _url_builder is not None:
        try:
            return _url_builder(endpoint, **values)
        except RuntimeError:
            pass
    if endpoint == "agent_task":
        return f"/api/agent/tasks/{values['job_id']}"
    if endpoint == "out_file":
        suffix = "?dl=1" if values.get("dl") else ""
        return f"/outfile/{values['token']}{suffix}"
    raise KeyError(endpoint)


# --------------------------------------------------------------------------
# 题目图片引用正则
# --------------------------------------------------------------------------

_QIMG_RE = import_defaults.QIMG_RE


# --------------------------------------------------------------------------
# 转换任务运行时（内存任务表 + 持久化）
# --------------------------------------------------------------------------

# 实时执行仍在内存；每次状态变化另存 task_store JSON 快照，插件/后端重启后恢复。
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

def _persist_job(job_id: str, job: dict) -> None:
    task_store.save("job", job_id, job)


# --------------------------------------------------------------------------
# 识别参数解析
# --------------------------------------------------------------------------

def _parse_number_spec(spec: str):
    """把题号规格串解析成整数列表；空串/无效返回 None（=全部，不过滤）。

    支持逗号分隔与区间：'8,11,14,18,19' 或 '7-14,18'（区间含两端）。
    去重、升序。上限保护：单个题号 <= 999，避免异常输入。
    """
    if not spec or not spec.strip():
        return None
    nums = set()
    for part in spec.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            for n in range(lo, hi + 1):
                if 1 <= n <= 999:
                    nums.add(n)
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= 999:
                nums.add(n)
    return sorted(nums) or None

# 缺省识别引擎。放成模块常量而不是在三处各写一遍 `or converter.ENGINE_WHOLE`：
# 翻默认值时漏改一处，表现是「有的入口翻了有的没翻」，而这两条路径的产物长得一样，
# 用户只会看到「同一份卷子两次结果不同」。与线上版 routes/import_convert.py
# 的 _DEFAULT_ENGINE 逐字对齐——两版的识别口径必须完全一致（见下面 _parse_engine）。
_DEFAULT_ENGINE = converter.ENGINE_BLOCK
_DEFAULT_OCR_BACKEND = converter.OCR_MINERU


def _parse_ocr_backend(raw: str) -> str:
    """OCR 服务选择。不认识的值回落 MinerU，保持旧任务和旧页面行为不变。"""
    return converter.normalize_ocr_backend(raw or _DEFAULT_OCR_BACKEND)


def _parse_engine(raw: str) -> str:
    """表单里的识别引擎选择。只认显式的 "whole"，其余（含缺省）一律走逐块路径。

    默认值 2026-08-08 从 whole 翻成 block，与线上版（`69d03d8`）对齐。原先的保守
    口径（「新路径是加出来的第二条路，不能因为参数名写错就换掉默认行为」）在逐块
    路径还没有回归手段时是对的；线上版翻默认的理由同样适用于本地版：整篇路径的
    块数由模型决定，漏题时账面完全正常（v0.3.1 的 `. .....4分` 吞掉一整道题就是
    这个形状）。whole 保留为可选兜底，不删。

    **两版默认值必须一致**：只有逐块路径会跑 `mechfix.normalize_block`，而它最后
    一步 `re.sub(r"\\n{3,}", "\\n\\n", ...)` 是唯一收敛连续空行的地方。默认值分叉的
    直接症状就是本地版入库的解答题小问之间空三行、线上版不空——同一份卷子两版
    产物不同，正是这个函数的默认值造成的。
    """
    return (converter.ENGINE_WHOLE if (raw or "").strip() == "whole"
            else _DEFAULT_ENGINE)


# 逐题识别切完块之后怎么处理：直接送 AI / 先人工审拆题 / 完全不送 AI
_BLOCK_MODE_ALL_AI = "all_ai"
_BLOCK_MODE_MANUAL = "manual"
_BLOCK_MODE_NO_AI = "no_ai"
_BLOCK_MODES = (_BLOCK_MODE_ALL_AI, _BLOCK_MODE_MANUAL, _BLOCK_MODE_NO_AI)

# 切题边界与“切完后是否送 AI”是两件独立的事。旧任务没有这个字段，必须稳定
# 回落到智能识别，不能因为升级后读取不到键而改变既有任务的切题语义。
_BOUNDARY_MODE_AUTO = "auto"
_BOUNDARY_MODE_WHITELIST = "whitelist"
_BOUNDARY_MODES = (_BOUNDARY_MODE_AUTO, _BOUNDARY_MODE_WHITELIST)


def _parse_block_mode(raw: str) -> str:
    """表单里的拆题处理方式。同 _parse_engine：不认识的值一律落回默认。"""
    val = (raw or "").strip()
    return val if val in _BLOCK_MODES else _BLOCK_MODE_NO_AI


def _parse_boundary_mode(raw: str) -> str:
    """表单里的题号边界策略；未知值按旧版智能识别处理。"""
    val = (raw or "").strip()
    return val if val in _BOUNDARY_MODES else _BOUNDARY_MODE_AUTO


def _parse_num_template(raw: str) -> str:
    """题号模板。空串=自动判定；非空则先编译一遍验证写法，编译结果丢掉——
    真正切块时 blocksplit 会自己再编一次，这里只为把错误挡在提交那一刻。
    """
    tpl = (raw or "").strip()
    if not tpl:
        return ""
    blocksplit.compile_dialect(tpl)   # 写法不合法会抛 TemplateError
    return tpl


def _convert_with_ocr_credentials(ocr_backend: str, make_call):
    """转换层会按每个 OCR 文档任务取凭证；这里只保留统一调用签名。"""
    _parse_ocr_backend(ocr_backend)
    return make_call("", "")


# --------------------------------------------------------------------------
# 识别历史归档
# --------------------------------------------------------------------------

def _history_record_for_sources(title: str, file_path, solution_path=None,
                                *, ocr_backend: str = "") -> str:
    """给一次识别尝试建立原文件归档，返回历史编号。"""
    paths = [path for path in (file_path, solution_path) if path]
    names = [str(title or Path(paths[0]).name)]
    if solution_path:
        names.append(f"解析文件{Path(solution_path).suffix.lower()}")
    record = history_store.create_record(
        Path(str(title or "识别记录")).stem or "识别记录",
        paths,
        source_names=names,
        metadata={"ocr_backend": _parse_ocr_backend(ocr_backend)},
    )
    return record["id"]

def _archive_group_markdown(group: dict, markdown: str,
                            *, record_id: str | None = None) -> None:
    """把最终 Markdown 补进该次尝试的归档；历史再导入不重复制造副本。"""
    if group.get("history_reimport") or group.get("history_skip_archive"):
        return
    record_id = record_id or group.get("history_id")
    if not record_id:
        record_id = _history_record_for_sources(
            group.get("filename") or "识别记录",
            group.get("file_path"), group.get("solution_path"),
            ocr_backend=group.get("ocr_backend", ""))
        group["history_id"] = record_id
    history_store.attach_markdown(
        record_id, markdown,
        title=Path(group.get("filename") or "识别记录").stem,
        metadata={
            "include_solution": bool(group.get("include_solution")),
            "boundary_mode": _parse_boundary_mode(
                group.get("boundary_mode", "")),
        },
    )


# --------------------------------------------------------------------------
# 后台转换 worker
# --------------------------------------------------------------------------

def _convert_worker(job_id: str, saved_path, orig_filename: str,
                    include_solution: bool = False, solution_path=None,
                    only_numbers=None, provider=None,
                    engine: str = _DEFAULT_ENGINE,
                    num_template: str = "",
                    ocr_backend: str = _DEFAULT_OCR_BACKEND,
                    boundary_mode: str = _BOUNDARY_MODE_AUTO,
                    image_page_count: int = 0,
                    solution_image_page_count: int = 0,
                    block_mode: str = _BLOCK_MODE_ALL_AI):
    """后台线程：跑转换，结果写回 _jobs。

    solution_path 非空 → 走「题干+解析双文件」路径，按题号关联解析。
    only_numbers 非空 → 仅导入指定题号的题（压轴题过滤）。
    provider 在起线程前就解析好传进来（这里没有请求上下文）。
    上传文件不在此删除——保留供预览对照，由下次转换前 _clean_uploads 清理。
    """
    notes: list[str] = []
    boundary_mode = _parse_boundary_mode(boundary_mode)
    image_page_count = max(0, int(image_page_count or 0))
    solution_image_page_count = max(
        0, int(solution_image_page_count or 0))
    try:
        block_mode = _parse_block_mode(block_mode)
        if engine == converter.ENGINE_BLOCK and block_mode in {
                _BLOCK_MODE_NO_AI, _BLOCK_MODE_MANUAL}:
            if solution_path is not None:
                pending = _convert_with_ocr_credentials(
                    ocr_backend,
                    lambda tok, doc2x_key: converter.convert_exam_and_solution_to_blocks(
                        saved_path, solution_path, mineru_token=tok,
                        num_template=num_template, boundary_mode=boundary_mode,
                        only_numbers=only_numbers,
                        exam_image_page_count=image_page_count,
                        solution_image_page_count=solution_image_page_count,
                        ocr_backend=ocr_backend, doc2x_api_key=doc2x_key))
            else:
                pending = _convert_with_ocr_credentials(
                    ocr_backend,
                    lambda tok, doc2x_key: converter.convert_file_to_blocks(
                        saved_path, mineru_token=tok, num_template=num_template,
                        boundary_mode=boundary_mode, image_page_count=image_page_count,
                        only_numbers=only_numbers, ocr_backend=ocr_backend,
                        doc2x_api_key=doc2x_key))
            if block_mode == _BLOCK_MODE_MANUAL:
                with _jobs_lock:
                    _jobs[job_id].update(status="awaiting_block_review", pending=pending)
                    _persist_job(job_id, _jobs[job_id])
                return
            md = converter.finish_block_review(
                pending, action="skip", include_solution=include_solution,
                provider=None, note_sink=notes.append)
        elif solution_path is not None:
            md = _convert_with_ocr_credentials(
                ocr_backend,
                lambda tok, doc2x_key: converter.convert_exam_and_solution(
                    saved_path, solution_path, mineru_token=tok,
                    only_numbers=only_numbers,
                    provider=provider, engine=engine, num_template=num_template,
                    boundary_mode=boundary_mode,
                    exam_image_page_count=image_page_count,
                    solution_image_page_count=solution_image_page_count,
                    note_sink=notes.append, ocr_backend=ocr_backend,
                    doc2x_api_key=doc2x_key))
        else:
            md = _convert_with_ocr_credentials(
                ocr_backend,
                lambda tok, doc2x_key: converter.convert_file(
                    saved_path, mineru_token=tok,
                    include_solution=include_solution,
                    only_numbers=only_numbers, provider=provider, engine=engine,
                    num_template=num_template, boundary_mode=boundary_mode,
                    image_page_count=image_page_count, note_sink=notes.append,
                    ocr_backend=ocr_backend, doc2x_api_key=doc2x_key))
        with _jobs_lock:
            history_job = dict(_jobs.get(job_id) or {})
        _archive_group_markdown(history_job, md)
        with _jobs_lock:
            # 池子重试会把整条转换重跑一遍，notes 里可能攒了同一句话两份，去重
            _jobs[job_id].update(status="done", md=md,
                                 note=" ".join(dict.fromkeys(notes)))
            _persist_job(job_id, _jobs[job_id])
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id].update(status="error", error=str(e))
            _persist_job(job_id, _jobs[job_id])


# --------------------------------------------------------------------------
# 导入预览
# --------------------------------------------------------------------------

_IMPORT_NOTE_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s*备注\s*$")


def _split_import_note(stem: str, solution: str | None) -> tuple[str, str | None, str]:
    """从题干或解析末尾拆出 ``## 备注``，沿用题库独立 section 语义。"""
    for field in ("solution", "stem"):
        value = solution if field == "solution" else stem
        if not value:
            continue
        match = _IMPORT_NOTE_HEADING_RE.search(value)
        if not match:
            continue
        before = value[:match.start()].rstrip()
        note = value[match.end():].strip()
        if field == "solution":
            solution = before or None
        else:
            stem = before
        return stem, solution, note
    return stem, solution, ""

def _build_import_preview(raw: str, *, include_solution: bool = True,
                          only_numbers=None, existing_fps=None,
                          all_cols=None,
                          boundary_mode: str = _BOUNDARY_MODE_AUTO):
    """把规范化 md 文本切成预览题卡列表，附查重标记。

    返回 `(preview, all_cols, missing_numbers)`。单文件预览与批量 md 队列共用，
    保证两条路的切分/查重规则一致。

    **解析归位分两种形态**（症状是解析全混在题干里）：
      · 智能模式下，文档里有**两套题号**（题目 1..N 之后答案又从 1 数一遍，docx/PDF 卷子的
        常见排版）→ `importer.pair_duplicate_numbering` 按题号把后一套配成前一套
        的解析。这一档与 include_solution 无关地先做：要丢解析也得先知道哪半是
        解析，否则那一半会当成题留在库里。
      · 只有一套题号 → 在每块内部按 `答案/解析` 字样切
        （`split_solution(scan_markers=True)`）。
    `include_solution=False`（用户选了「丢弃解析」）时两档都照切，只是切出来的
    解析扔掉、不入库——比「假装没看见」稳，也免得答案文字残留在题干里。
    强制白名单允许跳号、重复和回卷，因此不把第二套题号猜成答案区；只保留转换器
    已经输出的顶层题块，再在每块内部识别显式的答案/解析标记。
    """
    boundary_mode = _parse_boundary_mode(boundary_mode)
    # 两套题号先试：`split_questions` 取最长递增序列，第二套题号编号更小会被当成
    # 小问丢掉（那是它对的行为），所以两套题号必须由 split_questions_with_restart
    # 单独切一次。配好对之后 blocks 只剩前一套，解析由 paired 提供。
    paired = None
    if boundary_mode == _BOUNDARY_MODE_AUTO:
        restart = importer.split_questions_with_restart(raw)
        if restart is not None:
            rblocks, cut = restart
            paired = importer.pair_duplicate_numbering(
                [mechfix.fix_subq_parens(b) for b in rblocks], cut)
    if paired is not None:
        blocks = [b for b, _ in paired]
        pre_solutions = [s for _, s in paired]
    else:
        blocks = [mechfix.fix_subq_parens(b)
                  for b in importer.split_questions(raw)]
        pre_solutions = [None] * len(blocks)
    # 导入时不再自动扫描整座题库查重。文件式题库达到上万道后，这一步需要递归
    # 读取全部 Markdown，单次就可能耗时近一分钟；批量免审还会让用户误以为 OCR
    # 一直没有结束。历史库查重由独立「查重」页按需执行，这里只保留本次文本内部
    # 的 O(n) 指纹去重。existing_fps 仍作为显式注入口保留，供离线工具按需复用。
    if existing_fps is None:
        existing_fps = set()
    if all_cols is None:
        # 校对页下拉框只需要真实目录名，不需要每个文件夹的题数。带计数的默认树会
        # 为 641 个目录解析全库 Markdown，13k 题下仅打开校对页就要几十秒。
        all_cols = filestore.all_collections(filestore.list_collections_tree([]))
    preview = []
    seen_fps = set()
    found_numbers = set()
    for i, b in enumerate(blocks):
        # 先读块首题型标签定类型（逐块识别路径会打 `[单选]` 这类标签），再把标签
        # 剥掉——之后的切解析/查重/入库正文都用干净文本，标签不能进库
        qtype = importer.guess_type(b)
        b = importer.strip_type_tag(b)
        # 块内切解析：两套题号那档已经把解析摘出去了，块内不再扫标记——那时块里
        # 剩的 `答案：` 只可能是题干自带的字样（`根据答案：`），扫了是误伤。
        stem, solution = importer.split_solution(b, scan_markers=paired is None)
        if pre_solutions[i]:
            solution = pre_solutions[i]
        stem, solution, note = _split_import_note(stem, solution)
        # 「参考答案」大标题落在上一题块尾（它前面没有新题号），剥掉免得拖进题干
        stem = importer.strip_answer_head(stem)
        n = importer.block_number(b)
        if n is not None:
            found_numbers.add(n)
        # 漏题检测要在剥题号之前做（block_number 靠题号定位），展示/入库用的
        # stem 则剥掉题号和紧随的分值标注——题卡渲染不需要这些。
        stem = importer.strip_leading_number(stem)
        # 「18. 证明：命题……」里的“证明”是题目指令，不是答案分区。但块内解析
        # 切分只看标记时会把整句话搬进 solution，留下空题干，自动入库因此失败。
        # 仅在解答题题干已经完全为空时回收整段；只要原题干还有一个字就不猜，避免
        # 把真正的解析覆盖回题干。
        if qtype == "解答题" and not stem.strip() and solution:
            stem = importer.strip_leading_number(solution)
            solution = None
        # whole/LLM 路径偶尔也会漏掉 MinerU 的 <sub>；机械路径虽已在 normalize_block
        # 处理过，这里再做一次幂等收口，确保任何识别入口都不会把 OCR HTML 入库。
        stem = mechfix.normalize_html_subscripts(stem)
        stem = mechfix.normalize_html_superscripts(stem)
        stem = mechfix.normalize_intrusive_column_text(stem)
        stem = mechfix.normalize_misplaced_constraints(stem)
        if solution:
            solution = mechfix.normalize_html_subscripts(solution)
            solution = mechfix.normalize_html_superscripts(solution)
            solution = mechfix.normalize_intrusive_column_text(solution)
            solution = mechfix.normalize_misplaced_constraints(solution)
        if note:
            note = mechfix.normalize_html_subscripts(note)
            note = mechfix.normalize_html_superscripts(note)
        if qtype == "解答题":
            stem = mechfix.normalize_subquestion_layout(stem)
        stem = mechfix.ensure_fill_blank(stem, qtype)
        fp = dedup.fingerprint(stem)   # 指纹用题干，不含解析
        if fp in existing_fps:
            dup = "库中已存在"
        elif fp in seen_fps:
            dup = "本批重复"
        else:
            dup = None
        seen_fps.add(fp)
        # 裸字母/数字包 `$\displaystyle $` 是最后一步：查重指纹已经算完（dedup.normalize
        # 会把 `$` 和 `\displaystyle` 都删掉，包与不包指纹相同，顺序其实无关，但
        # 摆在指纹之后更明确——展示层的事不该有机会影响查重）。
        # 字母在前、数字在后：数字那步产出的 `$` 会被字母那步的零距离判据当成碎片
        # 信号，反序会挡掉一批本该包的字母（见 wrap_bare_numbers 的 docstring）。
        stem = mechfix.wrap_bare_numbers(mechfix.wrap_bare_letters(stem))
        if qtype in ("单选题", "多选题"):
            # 先利用 ``($A$)`` 这类带括号的强标签定位四个选项，再统一为 A.。
            # 旧顺序先剥括号，会把标签降成与题干中的点 A、事件 B 完全同形的弱
            # 标签，选项正文再出现 A/B 时整组就无法可靠识别。
            stem = mechfix.normalize_choice_options(stem, known_choice=True)
        if solution:
            solution = mechfix.wrap_bare_numbers(mechfix.wrap_bare_letters(solution))
            solution = mechfix.normalize_solution_layout(solution)
        # number 一路带到入库：文件名按它取（filestore._question_filename），
        # 没有它就只能落回 uuid 名。此前这个数字只用来做漏题检测就丢了。
        image_count = len(_QIMG_RE.findall(stem))
        img_mode, img_layouts, img_flow = _import_image_defaults(qtype, stem)
        final_solution = (solution or "") if include_solution else ""
        sol_img_split, sol_img_layouts = _import_solution_image_defaults(
            final_solution)
        preview.append({"idx": i, "body": stem,
                        "solution": final_solution,
                        "note": note,
                        "type": qtype, "dup": dup, "number": n,
                        "img_split": img_mode, "img_layouts": img_layouts,
                        "img_flow": img_flow, "image_count": image_count,
                        "sol_img_split": sol_img_split,
                        "sol_img_layouts": sol_img_layouts})
    missing_numbers = None
    if only_numbers:
        miss = sorted(set(only_numbers) - found_numbers)
        if miss:
            missing_numbers = miss
    elif (boundary_mode == _BOUNDARY_MODE_AUTO
          and len(found_numbers) >= 2):
        # 智能模式的全卷导入必须报自然题号断档。白名单模式刻意允许跳号；但上面
        # only_numbers 是用户明确点名要取的题，两种模式都必须报告显式缺失。
        # 此前只在用户手动勾“指定题号”时检查，
        # 2025 三份真卷分别漏 3/12/18 题却全部显示可入库，质量门禁形同虚设。
        miss = sorted(set(range(min(found_numbers), max(found_numbers) + 1))
                      - found_numbers)
        if miss:
            missing_numbers = miss
    return preview, all_cols, missing_numbers

def _import_image_defaults(qtype: str, body: str, requested_mode: str = "",
                           requested_flow: str = "") -> tuple[str | None, list[dict], str]:
    """返回新导入题的 ``(图片位置, 逐图布局, 多图方向)``。

    默认值只写进新导入题，不迁移既有题卡。选择题的四图 A-D 配对优先于普通多图
    规则；否则按科目、题型和图片数量选位置。``requested_*`` 来自人工校对页，合法值
    永远优先，用户手改不能被默认值覆盖。
    """
    return import_defaults.import_image_defaults(
        qtype,
        body,
        subject=config.BANK_SUBJECT,
        pair_applies=qrender.pair_applies,
        requested_mode=requested_mode,
        requested_flow=requested_flow,
    )

def _import_solution_image_defaults(solution: str) -> tuple[str | None, list[dict]]:
    """解析图片默认图文混排；多图作为一个纵向视觉组，避免横排挤压推导文字。"""
    return import_defaults.import_solution_image_defaults(solution)


# --------------------------------------------------------------------------
# 导出辅助
# --------------------------------------------------------------------------

def _question_export_payload(record: dict) -> dict:
    """题库记录收敛为导出器字段；备注是内部整理信息，不进入交付产物。"""
    return {
        "id": record["id"], "body": record["body"], "type": record["type"],
        "source": record.get("source", ""),
        # 双栏刷题的解答题作答区会结合难度与一级小问数计算；漏传时
        # exporter 只能把所有未知难度都按 3 处理，题卡上调的难度就失效。
        "difficulty": record["difficulty"],
        "solution": record["solution"], "img_align": record["img_align"],
        "img_width": record["img_width"], "img_split": record["img_split"],
        # 多图逐图排版设置（见 exporter._parse_layouts）；老题为空列表时导出
        # 退回 img_width/img_align 的单图行为。
        "img_layouts": record["img_layouts"],
        # 解析里的图片排版设置，序号与题干各自独立编号。
        "sol_img_split": record["sol_img_split"],
        "sol_img_layouts": record["sol_img_layouts"],
    }

_EXPORT_SOURCE_SPECIAL_RE = re.compile(r"([\\`*_{}\[\]()#+.!$|<>])")


def _source_prefixed_body(body: str, source: str) -> str:
    """把题源作为安全的 Markdown 行内前缀放到题号后、题干前。"""
    clean = " ".join(str(source or "").split())
    if not clean:
        return body
    escaped = _EXPORT_SOURCE_SPECIAL_RE.sub(r"\\\1", clean)
    return f"【{escaped}】{body}"

# 是最普通的同源请求，iframe 里能直接当 src，插件侧也能用 requestUrl 抓下来自己落盘。
#
# 只存路径不存内容：PDF 动辄几 MB，产物本来就已经在 OUTPUT_DIR 里了（由
# cleanup_output.py 定期清理），再在内存里留一份没有意义。
_out_files: dict[str, dict] = {}
_out_files_lock = threading.Lock()
# 取件号上限：单人使用，攒到这个数就把最早的丢掉（丢的只是取件号，文件还在
# OUTPUT_DIR 里）。不设上限的话开一天题库点上千次预览，这个 dict 只会长不会消。
_MAX_OUT_FILES = 64


def _register_out_file(path, download_name: str = "") -> str:
    """把导出产物登记成一个取件号，返回它。"""
    token = uuid.uuid4().hex
    with _out_files_lock:
        _out_files[token] = {"path": str(path),
                             "name": download_name or Path(path).name}
        while len(_out_files) > _MAX_OUT_FILES:
            _out_files.pop(next(iter(_out_files)))
    return token


# --------------------------------------------------------------------------
# Agent 任务服务
# --------------------------------------------------------------------------

# Agent 工具不能直接调用 Flask 路由或拿到任意文件路径；下面四个回调把现有
# 转换、校对和导出链路收敛成受控服务。所有回调都再次校验会话、工作目录和
# 参数，即使调用方绕过模型层直接提交工具请求也不会扩大权限。

_AGENT_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,100}$")
_AGENT_MAX_EXPORT_QUESTIONS = 500
_AGENT_EXPORT_MODES = frozenset({
    "list", "note", "lecture", "slides", "practice", "exam",
    "exam_std", "handout",
})
_AGENT_SOLUTION_MODES = frozenset({"none", "inline", "separate"})
_AGENT_HF_KEYS = frozenset({
    "header_left", "header_center", "header_right",
    "footer_left", "footer_center", "footer_right",
})
_AGENT_IMPORT_LOCKS: dict[str, threading.Lock] = {}
_AGENT_IMPORT_LOCKS_GUARD = threading.Lock()


def _agent_import_lock(job_id: str) -> threading.Lock:
    with _AGENT_IMPORT_LOCKS_GUARD:
        return _AGENT_IMPORT_LOCKS.setdefault(job_id, threading.Lock())


def _agent_require_bank(session: dict) -> str:
    sid = str((session or {}).get("id") or "").strip()
    if not sid:
        raise agent_tools.ToolError("缺少 Agent 会话")
    if str((session or {}).get("scope") or "bank") != "bank":
        raise agent_tools.ToolError("当前会话为仅聊天模式，不能操作题库")
    try:
        agent_actions.normalize_folder(
            str((session or {}).get("workdir_id") or ""),
            session=session, must_exist=True)
    except agent_actions.AgentActionError as exc:
        raise agent_tools.ToolError(str(exc)) from exc
    return sid


def _agent_job_for_session(job_id: object, session: dict) -> dict:
    """读取并校验 Agent 自己创建的任务，返回稳定快照。"""
    sid = _agent_require_bank(session)
    jid = str(job_id or "").strip()
    if not _AGENT_JOB_ID_RE.fullmatch(jid):
        raise agent_tools.ToolError("识别任务编号无效")
    with _jobs_lock:
        row = _jobs.get(jid)
        if row is None:
            raise agent_tools.ToolError("识别任务不存在或已过期")
        if str(row.get("agent_session_id") or "") != sid:
            raise agent_tools.ToolError("识别任务不属于当前 Agent 会话")
        if str(row.get("agent_workdir_id") or "") != str(
                (session or {}).get("workdir_id") or ""):
            raise agent_tools.ToolError("识别任务的工作目录已变化，请重新上传")
        return dict(row)


def _agent_public_preview_item(item: dict) -> dict:
    """截断单题预览，避免把整份识别稿塞进事件和会话历史。"""
    def _clip(value, limit=1200):
        text = str(value or "")
        return text[:limit] + ("\n…（内容已截断）" if len(text) > limit else "")

    return {
        "idx": item.get("idx"), "number": item.get("number"),
        "type": item.get("type") or "", "body": _clip(item.get("body")),
        "solution": _clip(item.get("solution")), "dup": item.get("dup"),
        "image_count": int(item.get("image_count") or 0),
    }


def _agent_raw_conversion_preview(job: dict, *, include_solution: bool,
                                  existing_fps=None) -> tuple[list[dict], list[int] | None]:
    raw = job.get("md")
    if not isinstance(raw, str) or not raw.strip():
        raise agent_tools.ToolError("识别任务没有可导入的文本结果")
    try:
        preview, _all_cols, missing = _build_import_preview(
            raw,
            include_solution=include_solution,
            existing_fps=existing_fps,
            only_numbers=job.get("only_numbers"),
            boundary_mode=_parse_boundary_mode(job.get("boundary_mode", "")),
        )
    except Exception as exc:
        raise agent_tools.ToolError(f"识别结果预览失败：{exc}") from exc
    return preview, missing


def _agent_existing_fingerprints(folder: str) -> set[str]:
    """只在 Agent 当前工作目录内计算已有题目指纹，避免扫描整个大题库。"""
    try:
        records = filestore.collection_records_snapshot(folder)
    except Exception as exc:
        raise agent_tools.ToolError(f"读取目标目录失败：{exc}") from exc
    result = set()
    for row in records:
        try:
            result.add(dedup.fingerprint(str(row.get("body") or "")))
        except Exception:
            continue
    return result


def _agent_conversion_preview(job: dict, *, include_solution: bool,
                              existing_fps=None) -> dict:
    preview, missing = _agent_raw_conversion_preview(
        job, include_solution=include_solution, existing_fps=existing_fps)
    public = [_agent_public_preview_item(item) for item in preview[:100]]
    duplicates = [item for item in public if item.get("dup")]
    return {
        "total": len(preview),
        "shown": len(public),
        "questions": public,
        "duplicates": duplicates,
        "duplicate_count": len([item for item in preview if item.get("dup")]),
        "missing_numbers": missing or [],
    }


def _agent_inspect_conversion(args: dict, session: dict) -> dict:
    job = _agent_job_for_session((args or {}).get("job_id"), session)
    status = str(job.get("status") or "error")
    result = {
        "job_id": str((args or {}).get("job_id") or ""),
        "status": status,
        "filename": Path(str(job.get("filename") or "识别任务")).name,
        "error": str(job.get("error") or ""),
    }
    if status == "done":
        include_solution = bool(job.get("include_solution", True))
        try:
            inspect_folder = str(session.get("workdir_id") or "")
            existing_fps = _agent_existing_fingerprints(inspect_folder)
        except agent_tools.ToolError:
            existing_fps = set()
        result["preview"] = _agent_conversion_preview(
            job, include_solution=include_solution, existing_fps=existing_fps)
        result["imported_ids"] = list(job.get("agent_imported_ids") or [])
        result["imported_count"] = len(result["imported_ids"])
    return result


def _agent_stage_selection(args: dict, session: dict):
    """解析上传暂存 manifest 中的题干/解析成员，返回路径和页数。"""
    sid = _agent_require_bank(session)
    stage_id = str((args or {}).get("stage_id") or "").strip()
    if not stage_id:
        raise agent_tools.ToolError("缺少上传暂存编号")
    try:
        manifest = agent_upload_store.get_stage(stage_id, sid)
    except agent_upload_store.AgentUploadError as exc:
        raise agent_tools.ToolError(str(exc)) from exc
    if not manifest.get("can_start"):
        raise agent_tools.ToolError("该上传暂存已经启动过转换或没有可用文件")
    entries = {str(item.get("path")): dict(item)
               for item in manifest.get("files") or []
               if item.get("path")}
    if not entries:
        raise agent_tools.ToolError("ZIP 中没有可识别文件")

    def _as_paths(value, label):
        if value is None or value == "":
            return []
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, (list, tuple)):
            raise agent_tools.ToolError(f"{label}必须是文件名列表")
        if len(values) > 100:
            raise agent_tools.ToolError(f"{label}数量过多")
        result = []
        for raw in values:
            rel = str(raw or "").replace("\\", "/").strip()
            if rel not in entries:
                raise agent_tools.ToolError(f"ZIP 中不存在成员：{rel}")
            try:
                path = agent_upload_store.resolve_stage_file(stage_id, rel, sid)
            except agent_upload_store.AgentUploadError as exc:
                raise agent_tools.ToolError(str(exc)) from exc
            if path.is_symlink() or not path.is_file():
                raise agent_tools.ToolError(f"ZIP 成员不可读取：{rel}")
            result.append((rel, path, entries[rel]))
        return result

    selected = _as_paths((args or {}).get("files"), "题干文件")
    if not selected:
        selected = _as_paths(
            [item["path"] for item in entries.values()
             if item.get("role") == "exam"], "题干文件")
    if not selected:
        selected = _as_paths(
            [item["path"] for item in entries.values()
             if item.get("role") != "solution"], "题干文件")
    if not selected:
        raise agent_tools.ToolError("没有选到题干文件")
    if any(item[2].get("role") == "solution" for item in selected):
        raise agent_tools.ToolError("题干文件不能选择答案/解析成员")

    solution_value = (args or {}).get("solution")
    solutions = _as_paths(solution_value, "解析文件")
    if solution_value in (None, "") and not solutions:
        solutions = _as_paths(
            [item["path"] for item in entries.values()
             if item.get("role") == "solution"], "解析文件")

    def _materialize(items, label):
        if len(items) == 1:
            rel, source, meta = items[0]
            return source, Path(rel).name, (1 if meta.get("kind") == "image" else 0), []
        if not all(item[2].get("kind") == "image" for item in items):
            raise agent_tools.ToolError(
                f"{label}有多个文档；多个文件时请全部选择图片，或只保留一份文档")
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        copied = []
        try:
            for rel, source, _meta in items:
                target = config.UPLOAD_DIR / f"agent-{uuid.uuid4().hex}{Path(rel).suffix.lower()}"
                shutil.copyfile(source, target)
                copied.append(target)
            merged = config.UPLOAD_DIR / f"agent-{uuid.uuid4().hex}.pdf"
            converter.images_to_pdf(copied, merged)
            return merged, f"{Path(items[0][0]).stem} 等 {len(items)} 张.pdf", len(items), copied
        except Exception:
            for path in copied:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise

    # 单文档也复制出暂存目录，确保任务不依赖 24 小时后清理的 stage。
    def _copy_single(path, rel):
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = config.UPLOAD_DIR / f"agent-{uuid.uuid4().hex}{Path(rel).suffix.lower()}"
        shutil.copyfile(path, target)
        return target

    try:
        exam_path, exam_name, exam_pages, exam_extra = _materialize(selected, "题干文件")
        if len(selected) == 1:
            exam_path = _copy_single(exam_path, selected[0][0])
            exam_extra = []
        solution_path = None
        solution_name = ""
        solution_pages = 0
        solution_extra = []
        if solutions:
            solution_path, solution_name, solution_pages, solution_extra = _materialize(
                solutions, "解析文件")
            if len(solutions) == 1:
                solution_path = _copy_single(solution_path, solutions[0][0])
                solution_extra = []
    except agent_tools.ToolError:
        raise
    except Exception as exc:
        raise agent_tools.ToolError(f"准备 ZIP 文件失败：{exc}") from exc
    return (manifest, stage_id, exam_path, exam_name, exam_pages,
            solution_path, solution_name, solution_pages,
            list(exam_extra) + list(solution_extra))


def _agent_start_conversion(args: dict, session: dict) -> dict:
    args = dict(args or {})
    sid = _agent_require_bank(session)
    normalization_mode = str(args.get("normalization_mode") or "").strip().lower()
    engine_value = str(args.get("engine") or "").strip().lower()
    ocr_value = str(args.get("ocr_backend") or "").strip().lower()
    # 上传后的第一步只是规划。缺少这些会改变结果或费用的参数时，返回结构化
    # 选项，供 Agent/前端继续询问用户，绝不隐式调用 LLM 或 OCR 服务。
    if not normalization_mode or not engine_value or not ocr_value:
        return {
            "choice_required": True,
            "choices": [
                {"id": "ocr_backend", "title": "请选择识别后端", "required": True,
                 "options": [{"id": "mineru", "label": "MinerU"},
                              {"id": "doc2x", "label": "Doc2X"}]},
                {"id": "engine", "title": "请选择导入方式", "required": True,
                 "options": [{"id": "block", "label": "逐题切分（推荐）"},
                              {"id": "whole", "label": "整篇规范化"}]},
                {"id": "normalization_mode", "title": "请选择规范化方式", "required": True,
                 "options": [{"id": "mechanical", "label": "机械规范化，不调用 LLM"},
                              {"id": "llm", "label": "LLM 规范化"},
                              {"id": "review", "label": "识别后人工审核"}]},
            ],
            "message": "文件已暂存。请先确定识别后端、导入方式和规范化方式，确认后才会启动任务。",
        }
    if normalization_mode not in {"mechanical", "llm", "review"}:
        raise agent_tools.ToolError("规范化方式必须是 mechanical、llm 或 review")
    if engine_value not in {"block", "whole"}:
        raise agent_tools.ToolError("导入方式必须是 block 或 whole")
    if ocr_value not in {"mineru", "doc2x"}:
        raise agent_tools.ToolError("识别后端必须是 mineru 或 doc2x")
    stage_id = str(args.get("stage_id") or "").strip()
    (manifest, stage_id, exam_path, exam_name, exam_pages,
     solution_path, solution_name, solution_pages, extra_paths) = _agent_stage_selection(
        args, session)
    cleanup_paths = [Path(exam_path), *extra_paths]
    if solution_path:
        cleanup_paths.append(Path(solution_path))
    try:
        ocr_backend = _parse_ocr_backend(str(args.get("ocr_backend") or ""))
        engine = _parse_engine(engine_value)
        if normalization_mode in {"mechanical", "review"}:
            # 机械/人工审核只有逐块链路能在代码层保证不调用 LLM；整篇模式必须
            # 改为逐块，否则会在后台隐式构造 LLM 客户端。
            engine = converter.ENGINE_BLOCK
        boundary_mode = _parse_boundary_mode(str(args.get("boundary_mode") or ""))
        if boundary_mode == _BOUNDARY_MODE_WHITELIST:
            engine = converter.ENGINE_BLOCK
        raw_numbers = args.get("only_numbers", "")
        if isinstance(raw_numbers, (list, tuple, set)):
            raw_numbers = ",".join(str(item) for item in raw_numbers)
        only_numbers = _parse_number_spec(str(raw_numbers or ""))
        try:
            num_template = _parse_num_template(str(args.get("num_template") or ""))
        except blocksplit.TemplateError as exc:
            raise agent_tools.ToolError(f"题号模板写法不对：{exc}") from exc
        include_solution = bool(solution_path) or bool(args.get("include_solution", True))
        provider = providers.resolve_active() if normalization_mode == "llm" else None
        if normalization_mode == "llm" and provider is None:
            raise agent_tools.ToolError(
                "当前没有启用规范化 LLM，请改选机械规范化/人工审核，或先在设置中配置 LLM")
    except agent_tools.ToolError:
        raise
    except Exception as exc:
        raise agent_tools.ToolError(f"识别参数或模型配置无效：{exc}") from exc

    display_name = exam_name
    if solution_path:
        display_name = f"{Path(exam_name).stem} + {solution_name}（解析）"
    try:
        history_id = _history_record_for_sources(
            display_name, exam_path, solution_path, ocr_backend=ocr_backend)
    except Exception as exc:
        for path in cleanup_paths:
            try:
                path.unlink()
            except OSError:
                pass
        raise agent_tools.ToolError(f"历史记录创建失败：{exc}") from exc

    job_id = uuid.uuid4().hex
    workdir_id = str(session.get("workdir_id") or "")
    job = {
        "status": "pending", "md": None, "error": None,
        "filename": display_name, "path": str(exam_path),
        "solution_path": str(solution_path) if solution_path else None,
        "include_solution": include_solution, "only_numbers": only_numbers,
        "note": "", "ocr_backend": ocr_backend,
        "block_mode": {"mechanical": _BLOCK_MODE_NO_AI,
                        "review": _BLOCK_MODE_MANUAL,
                        "llm": _BLOCK_MODE_ALL_AI}[normalization_mode],
        "normalization_mode": normalization_mode,
        "boundary_mode": boundary_mode,
        "image_page_count": exam_pages,
        "exam_image_page_count": exam_pages,
        "solution_image_page_count": solution_pages,
        "history_id": history_id,
        "agent_session_id": sid, "agent_workdir_id": workdir_id,
        "agent_input_dir_id": str(session.get("input_dir_id") or workdir_id),
        "agent_stage_id": stage_id, "agent_imported_ids": [],
        "agent_import_folder": "", "cleanup_paths": [str(p) for p in cleanup_paths],
    }
    try:
        # 先原子占用 stage，再登记任务；重复点击会在这里得到 409，而不会
        # 创建第二条可能重复扣费的 OCR 线程。
        agent_upload_store.mark_stage_started(stage_id, sid, job_id)
        with _jobs_lock:
            _jobs[job_id] = job
            _persist_job(job_id, job)
        threading.Thread(
            target=_convert_worker,
            args=(job_id, exam_path, display_name, include_solution,
                  solution_path, only_numbers, provider, engine, num_template,
                  ocr_backend, boundary_mode, exam_pages, solution_pages,
                  {"mechanical": _BLOCK_MODE_NO_AI,
                   "review": _BLOCK_MODE_MANUAL,
                   "llm": _BLOCK_MODE_ALL_AI}[normalization_mode]),
            daemon=True,
        ).start()
    except Exception as exc:
        with _jobs_lock:
            _jobs.pop(job_id, None)
        task_store.delete("job", job_id)
        try:
            history_store.move_to_trash(history_id)
            history_store.purge(history_id)
        except Exception:
            pass
        for path in cleanup_paths:
            try:
                path.unlink()
            except OSError:
                pass
        raise agent_tools.ToolError(f"启动识别任务失败：{exc}") from exc
    _runtime().append(sid, "system", f"已启动暂存文件识别任务：{job_id}")
    try:
        task_url = _agent_url("agent_task", job_id=job_id)
    except RuntimeError:
        task_url = f"/api/agent/tasks/{job_id}"
    return {
        "ok": True, "executed": True, "job_id": job_id,
        "status": "pending", "filename": display_name,
        "stage_id": stage_id, "task_url": task_url,
        "selected_files": [item.get("path") for item in manifest.get("files", [])
                            if item.get("role") in {"exam", "solution"}],
    }


def _agent_import_conversion(args: dict, session: dict) -> dict:
    args = dict(args or {})
    approved = bool(args.pop("_approved_execute", False))
    job_id = str(args.get("job_id") or "").strip()
    job = _agent_job_for_session(job_id, session)
    status = str(job.get("status") or "")
    if status != "done":
        if status == "error":
            raise agent_tools.ToolError(f"识别任务失败：{job.get('error') or '未知错误'}")
        raise agent_tools.ToolError(f"识别任务尚未完成（当前状态：{status or '未知'}）")
    try:
        folder, _folder_path = agent_actions.normalize_folder(
            args.get("folder"), session=session, must_exist=True)
    except agent_actions.AgentActionError as exc:
        raise agent_tools.ToolError(str(exc)) from exc
    include_solution = bool(args.get("include_solution", job.get("include_solution", True)))
    allow_duplicates = bool(args.get("include_duplicates", False))
    existing_fps = _agent_existing_fingerprints(folder)
    raw_preview, _missing_numbers = _agent_raw_conversion_preview(
        job, include_solution=include_solution, existing_fps=existing_fps)
    public_questions = [_agent_public_preview_item(item)
                        for item in raw_preview[:100]]
    duplicates_public = [item for item in public_questions if item.get("dup")]
    preview = {
        "total": len(raw_preview), "shown": len(public_questions),
        "questions": public_questions, "duplicates": duplicates_public,
        "duplicate_count": len([item for item in raw_preview if item.get("dup")]),
        "missing_numbers": _missing_numbers or [],
    }
    skipped = [_agent_public_preview_item(item) for item in raw_preview
               if item.get("dup") and not allow_duplicates]
    selected = [item for item in raw_preview
                if str(item.get("body") or "").strip()
                and (allow_duplicates or not item.get("dup"))]
    if not selected:
        return {"ok": True, "executed": True, "job_id": job_id,
                "imported_ids": [], "count": 0, "skipped": skipped,
                "preview": preview, "message": "没有可新增的题目"}

    try:
        tags_value = args.get("tags", [])
        if isinstance(tags_value, str):
            tags_value = tags_value.replace("，", ",").split(",")
        if not isinstance(tags_value, (list, tuple, set)):
            tags_value = []
        tags = []
        for value in tags_value:
            tag = str(value or "").strip()
            if tag and len(tag) <= 80 and "\n" not in tag and "\r" not in tag:
                tags.append(tag)
        tags = list(dict.fromkeys(tags))[:50]
    except Exception:
        tags = []
    source = Path(str(job.get("filename") or "识别导入")).name
    pending_questions = []
    for item in selected:
        pending_questions.append({
            "body": str(item.get("body") or ""),
            "solution": str(item.get("solution") or "") if include_solution else "",
            "type": str(item.get("type") or ""), "source": source,
            "difficulty": "", "tags": tags, "number": item.get("number"),
            "img_split": item.get("img_split"),
            "img_layouts": item.get("img_layouts") or [],
            "sol_img_split": item.get("sol_img_split"),
            "sol_img_layouts": item.get("sol_img_layouts") or [],
        })

    safe_args = {
        "job_id": job_id, "folder": folder,
        "include_solution": include_solution,
        "include_duplicates": allow_duplicates,
        "tags": tags,
    }
    if (not approved
            and str(session.get("mode") or "standard") != "danger"):
        approval = _approvals().create(
            session, "import_conversion",
            f"将识别结果导入“{folder or '题库根目录'}”（{len(selected)} 道题）",
            safe_args)
        return {
            "ok": True, "pending_confirmation": True,
            "approval": approval, "job_id": job_id,
            "preview": preview, "skipped": skipped,
            "count": len(selected),
            "message": "导入计划已生成，请确认后写入题库",
        }

    with _agent_import_lock(job_id):
        # 重新读取而不是使用回调开始时的快照，防止两个审批请求同时通过
        # 前置检查后各自写入一遍。
        with _jobs_lock:
            current_snapshot = dict(_jobs.get(job_id) or {})
        imported_ids = list(current_snapshot.get("agent_imported_ids") or [])
        previous_folder = str(current_snapshot.get("agent_import_folder") or "")
        if imported_ids:
            if previous_folder != folder:
                raise agent_tools.ToolError("这份识别结果已经导入到其他目录，不能更换落点")
            return {"ok": True, "executed": True, "job_id": job_id,
                    "imported_ids": imported_ids, "count": len(imported_ids),
                    "skipped": skipped, "preview": preview,
                    "message": "这份识别结果已经导入过，未重复创建题目"}
        try:
            new_ids = filestore.create_questions_batch(
                pending_questions, folder,
                idempotency_scope=f"agent:{session.get('id')}:{job_id}:{folder}",
            )
        except Exception as exc:
            raise agent_tools.ToolError(f"导入题库失败：{exc}") from exc
        with _jobs_lock:
            current = _jobs.get(job_id)
            if current is not None:
                current["agent_imported_ids"] = list(new_ids)
                current["agent_import_folder"] = folder
                current["agent_imported_at"] = time.time()
                _persist_job(job_id, current)
    return {
        "ok": True, "executed": True, "job_id": job_id,
        "imported_ids": list(new_ids), "count": len(new_ids),
        "skipped": skipped, "preview": preview,
        "folder": folder, "message": f"已导入 {len(new_ids)} 道题",
    }


def _agent_export_questions(args: dict, session: dict) -> dict:
    args = dict(args or {})
    _agent_require_bank(session)
    try:
        bound_folder = str(session.get("workdir_id") or "")
        requested_folder = args.get("folder")
        folder, _folder_path = agent_actions.normalize_folder(
            requested_folder if requested_folder not in (None, "") else bound_folder,
            session=session, must_exist=True)
        output_value = args.get("output_dir")
        if output_value in (None, ""):
            output_value = session.get("output_dir_id") or bound_folder
        output_folder, output_path = agent_actions.normalize_folder(
            output_value if output_value not in (None, "") else bound_folder,
            session=session, must_exist=True)
    except agent_actions.AgentActionError as exc:
        raise agent_tools.ToolError(str(exc)) from exc

    ids_value = args.get("ids")
    query = str(args.get("query") or "").strip()
    try:
        if ids_value:
            values = [ids_value] if isinstance(ids_value, str) else ids_value
            if not isinstance(values, (list, tuple)) or len(values) > _AGENT_MAX_EXPORT_QUESTIONS:
                raise agent_tools.ToolError(
                    f"导出题目数量必须不超过 {_AGENT_MAX_EXPORT_QUESTIONS} 道")
            requested_ids = [str(value).strip() for value in values if str(value).strip()]
            rows_by_id = {str(row.get("id")): row for row in
                          filestore.records_from_ids(requested_ids)}
            rows = []
            for qid in requested_ids:
                row = rows_by_id.get(qid)
                if row is None:
                    raise agent_tools.ToolError(f"未找到题目：{qid}")
                row_folder = str(row.get("folder") or "").strip("/")
                if folder and not (row_folder == folder or row_folder.startswith(folder + "/")):
                    raise agent_tools.ToolError("所选题目不在当前 Agent 工作目录内")
                rows.append(row)
        else:
            records = filestore.collection_records_snapshot(folder)
            rows = filestore.list_questions(
                records=records, search=query, qtype=str(args.get("type") or ""),
                difficulty=str(args.get("difficulty") or ""), sort="custom")
            if len(rows) > _AGENT_MAX_EXPORT_QUESTIONS:
                rows = rows[:_AGENT_MAX_EXPORT_QUESTIONS]
    except agent_tools.ToolError:
        raise
    except (SearchQueryError, ValueError) as exc:
        raise agent_tools.ToolError(f"筛选题目失败：{exc}") from exc
    if not rows:
        raise agent_tools.ToolError("没有符合条件的题目可导出")

    fmt = str(args.get("format", args.get("fmt", "pdf")) or "pdf").lower()
    if fmt not in {"pdf", "tex", "zip"}:
        raise agent_tools.ToolError("导出格式只支持 pdf、tex 或 zip")
    mode = str(args.get("mode") or "list")
    if mode not in _AGENT_EXPORT_MODES:
        raise agent_tools.ToolError("导出模式无效")
    solution_mode = str(args.get("solution_mode") or "none")
    if solution_mode not in _AGENT_SOLUTION_MODES:
        raise agent_tools.ToolError("解析输出模式无效")
    title = str(args.get("title") or "试卷").strip()[:180] or "试卷"
    keypoints = str(args.get("keypoints") or "")[:10000]
    show_source = bool(args.get("show_source", False))
    questions = [_question_export_payload(row) for row in rows]
    if show_source:
        for question, row in zip(questions, rows):
            question["body"] = _source_prefixed_body(
                question["body"], row.get("source", ""))

    header_footer = {}
    raw_hf = args.get("header_footer")
    if isinstance(raw_hf, dict):
        for key in _AGENT_HF_KEYS:
            value = raw_hf.get(key)
            if value is not None:
                header_footer[key] = str(value)[:300]
    std_opts = {}
    raw_std = args.get("std_opts")
    if isinstance(raw_std, dict):
        for key in ("subject", "secret_notice", "exam_notes"):
            if raw_std.get(key) is not None:
                std_opts[key] = str(raw_std[key])[:1000]
        if "info_bar" in raw_std:
            std_opts["info_bar"] = bool(raw_std["info_bar"])
        if isinstance(raw_std.get("section_points"), dict):
            std_opts["section_points"] = {
                str(k): str(v)[:30] for k, v in raw_std["section_points"].items()
                if str(k) in {"single", "multi", "blank", "solve"}
            }

    template_id = str(args.get("template_id") or "").strip()
    template_path = None
    template_info = None
    warning = ""
    if template_id:
        try:
            template_info = _catalog().get_template(template_id)
            template_path = _catalog().template_source_path(
                template_id, require_enabled=True)
        except _catalog().CatalogError as exc:
            raise agent_tools.ToolError(str(exc)) from exc
    else:
        try:
            selected_template = _catalog().selected_template()
            if selected_template:
                template_id = str(selected_template.get("id") or "")
                template_info = selected_template
                template_path = _catalog().template_source_path(
                    template_id, require_enabled=True)
        except _catalog().CatalogError:
            template_id, template_path = "", None
    try:
        out_path = service_ports.export_document(
            questions, title=title, fmt=fmt, mode=mode,
            keypoints=keypoints,
            fullpage_ids=[str(item) for item in (args.get("fullpage_ids") or [])][:500],
            header_footer=header_footer, solution_mode=solution_mode,
            std_opts=std_opts, paper_tone=("cream" if args.get("paper_tone") == "cream" else "white"),
            wimath_logo=bool(args.get("wimath_logo", False)),
            bank_subject=config.BANK_SUBJECT, tex_backend="local",
            **({"template_path": str(template_path)} if template_path else {}),
        )
    except (exporter.ExportError, OSError) as exc:
        raise agent_tools.ToolError(f"导出失败：{exc}") from exc

    # exporter 的中间目录位于 OUTPUT_DIR；Agent 最终产物复制到当前工作目录，
    # 文件名冲突时递增后缀，绝不静默覆盖用户已有文件。
    suffix = Path(out_path).suffix.lower() or ("." + fmt)
    stem = filestore.safe_folder_name(title) or "试卷"
    destination = output_path / f"{stem}{suffix}"
    counter = 2
    while destination.exists() or destination.is_symlink():
        destination = output_path / f"{stem}_{counter}{suffix}"
        counter += 1
    try:
        shutil.copy2(out_path, destination)
    except OSError as exc:
        raise agent_tools.ToolError(f"导出文件写入工作目录失败：{exc}") from exc
    token = _register_out_file(destination, destination.name)
    try:
        url = _agent_url("out_file", token=token, dl=1)
    except RuntimeError:
        url = f"/outfile/{token}?dl=1"
    relative_output = (f"{output_folder}/{destination.name}"
                       if output_folder else destination.name)
    if template_info and template_info.get("format") == "pdf":
        warning = "PDF 样例模板已使用生成的 TeX 草稿，复杂视觉效果仍需人工调整"
    return {
        "ok": True, "executed": True, "format": fmt,
        "count": len(rows), "filename": destination.name,
        "output_path": relative_output, "url": url,
        "template_id": template_id or None,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# 批次 2 服务：识别结果逐题修正 / 模板启用
# ---------------------------------------------------------------------------

def _render_review_item(item: dict, idx: int) -> str:
    """把一个修正后的题卡渲染回可再切分的 markdown（保留题号）。"""
    number = item.get("number") or (idx + 1)
    body = str(item.get("body") or "").strip()
    text = f"{number}. {body}" if body else f"{number}."
    solution = str(item.get("solution") or "").strip()
    if solution:
        text += f"\n\n## 解析\n{solution}"
    return text


def _plan_review_fixes(items: list[dict], fixes: list) -> dict:
    """把修正计划套用到预览题卡上。索引对应 inspect_conversion 的预览序号。"""
    working = [dict(item) for item in items]
    applied: list[int] = []
    delete_set: set[int] = set()
    merge_set: set[int] = set()
    preview = [{"index": i, "number": working[i].get("number"),
                "head": (str(working[i].get("body") or "").strip().splitlines() or [""])[0][:80]}
               for i in range(len(working))]
    for fix in fixes:
        if not isinstance(fix, dict):
            raise agent_tools.ToolError("fixes 必须是对象列表")
        try:
            index = int(fix.get("index", -1))
        except (TypeError, ValueError):
            raise agent_tools.ToolError("fix 的 index 必须是整数")
        if not 0 <= index < len(working):
            raise agent_tools.ToolError(f"修正序号越界：{index}")
        changed = False
        if "body" in fix:
            body = str(fix.get("body") or "")
            if not body.strip():
                raise agent_tools.ToolError(f"第 {index} 题的新题干不能为空")
            working[index]["body"] = body
            changed = True
        if "solution" in fix:
            working[index]["solution"] = str(fix.get("solution") or "")
            changed = True
        if changed:
            applied.append(index)
        if fix.get("merge_next"):
            merge_set.add(index)
        if fix.get("delete"):
            delete_set.add(index)
    merged = 0
    deleted = 0
    consumed: set[int] = set()
    result: list[dict] = []
    for i, item in enumerate(working):
        if i in delete_set:
            deleted += 1
            continue
        if i in consumed:
            continue
        if i in merge_set:
            if i + 1 >= len(working):
                raise agent_tools.ToolError(f"第 {i} 题没有下一题可以合并")
            if (i + 1) in delete_set:
                raise agent_tools.ToolError(f"第 {i} 题要合并的下一题被同时删除")
            nxt = working[i + 1]
            item = dict(item)
            item["body"] = "\n\n".join(
                part for part in (str(item.get("body") or "").strip(),
                                  str(nxt.get("body") or "").strip()) if part)
            rs = str(nxt.get("solution") or "").strip()
            if rs:
                item["solution"] = "\n\n".join(
                    part for part in (str(item.get("solution") or "").strip(), rs) if part)
            consumed.add(i + 1)
            merged += 1
        result.append(item)
    return {"items": result, "applied": applied, "merged": merged,
            "deleted": deleted, "preview": preview}


def _agent_apply_review_fixes(args: dict, session: dict) -> dict:
    """对识别任务结果应用逐题修正；只改任务结果，不动题库文件。"""
    args = dict(args or {})
    approved = bool(args.pop("_approved_execute", False))
    job_id = str(args.get("job_id") or "").strip()
    job = _agent_job_for_session(job_id, session)
    if list(job.get("agent_imported_ids") or []):
        raise agent_tools.ToolError("该识别结果已经导入题库，不能再修改；请重新识别")
    raw_fixes = args.get("fixes")
    if not isinstance(raw_fixes, list) or not raw_fixes:
        raise agent_tools.ToolError("fixes 必须是非空列表")
    if len(raw_fixes) > 200:
        raise agent_tools.ToolError("单次修正不能超过 200 条")
    existing_fps = _agent_existing_fingerprints(str(session.get("workdir_id") or ""))
    items, _missing = _agent_raw_conversion_preview(
        job, include_solution=True, existing_fps=existing_fps)
    plan = _plan_review_fixes(items, raw_fixes)
    summary = (f"对识别任务 {job_id} 应用 {len(plan['applied'])} 条修正"
               f"（合并 {plan['merged']} 处，删除 {plan['deleted']} 题）")
    if (not approved
            and str(session.get("mode") or "standard") != "danger"):
        approval = _approvals().create(
            session, "apply_review_fixes", summary,
            {"job_id": job_id, "fixes": raw_fixes})
        return {"ok": True, "pending_confirmation": True,
                "approval": approval, "preview": plan["preview"],
                "message": "修正计划已生成，请确认后写入识别结果"}
    new_items = plan["items"]
    new_md = "\n\n".join(_render_review_item(item, idx)
                          for idx, item in enumerate(new_items))
    with _jobs_lock:
        current = dict(_jobs.get(job_id) or {})
        if not current:
            raise agent_tools.ToolError("识别任务不存在或已过期")
        current["md"] = new_md
        current["agent_imported_ids"] = []
        current["agent_import_folder"] = ""
        current["agent_review_fixed_at"] = time.time()
        _jobs[job_id] = current
        _persist_job(job_id, current)
    return {"ok": True, "executed": True, "job_id": job_id,
            "count": len(new_items), "applied": plan["applied"],
            "merged": plan["merged"], "deleted": plan["deleted"],
            "message": summary}


def _agent_enable_template(args: dict, session: dict) -> dict:
    """启用一个已通过校验的导出模板；目录写入属系统配置，标准模式走审批。"""
    args = dict(args or {})
    approved = bool(args.pop("_approved_execute", False))
    template_id = str(args.get("template_id") or "").strip()
    if not template_id:
        raise agent_tools.ToolError("必须提供 template_id")
    catalog = _catalog()
    try:
        row = catalog.get_template(template_id)
    except catalog.CatalogError as exc:
        raise agent_tools.ToolError(str(exc)) from exc
    summary = f"启用导出模板「{row.get('name') or template_id}」"
    if (not approved
            and str(session.get("mode") or "standard") != "danger"):
        approval = _approvals().create(
            session, "enable_template", summary, {"template_id": template_id})
        return {"ok": True, "pending_confirmation": True,
                "approval": approval,
                "message": summary + "，请确认后启用"}
    try:
        result = catalog.confirm_template(template_id, confirm=True)
    except catalog.CatalogError as exc:
        raise agent_tools.ToolError(str(exc)) from exc
    return {"ok": True, "executed": True, "template": result}


# ---------------------------------------------------------------------------
# 审批执行核心（GUI 与 CLI 共用）
# ---------------------------------------------------------------------------

_AGENT_SERVICE_ACTIONS = frozenset(agent_tools.SERVICE_TOOLS) | {"execute_command"}


def execute_approval(session: dict, approval_id: str):
    """执行一个已确认的 Agent 计划，并以原子领取保证不会重复写入。"""
    if agent_tools.permission_mode(session) == agent_tools.PERMISSION_READ_ONLY:
        raise agent_tools.ToolError("当前权限档位为只读，不能执行写入操作")
    claimed = _approvals().claim_execution(session, approval_id)
    if claimed is None:
        # 另一条请求已经领取/完成；返回当前公开状态即可，调用方不要重试写入。
        row = _approvals().get(session["id"], approval_id)
        return row, row.get("status") == "executed", row.get("result")
    action, arguments = claimed
    try:
        if agent_actions.is_write_action(action):
            result = agent_actions.execute_action(
                action, arguments, session=session)
        elif action in agent_tools.API_WRITE_TOOLS:
            result = agent_tools.execute_api_action(
                action, arguments, session=session)
        elif agent_handouts.is_handout_action(action):
            result = agent_handouts.execute_handout_action(
                action, arguments, session=session)
        elif action in _AGENT_SERVICE_ACTIONS:
            result = agent_tools.execute_service(
                action, arguments, session=session, approved_execute=True)
        else:
            raise agent_tools.ToolError(f"未注册的 Agent 确认操作：{action}")
        row = _approvals().mark_executed(
            session, approval_id, result=result)
        _runtime().append(session["id"], "system",
                          f"已完成 Agent 操作：{action}")
        return row, True, result
    except Exception as exc:
        detail = " ".join(str(exc).split())[:500]
        try:
            row = _approvals().mark_failed(
                session, approval_id, detail or "执行失败")
        except agent_approval_module.ApprovalError:
            row = _approvals().get(session["id"], approval_id)
        _runtime().append(session["id"], "system",
                          f"Agent 操作失败：{detail or '执行失败'}")
        return row, False, detail or "执行失败"


def register_all() -> None:
    """注册任务服务。GUI 与 CLI 都在初始化后调用。"""
    agent_tools.register_service("inspect_conversion", _agent_inspect_conversion)
    agent_tools.register_service("start_conversion", _agent_start_conversion)
    agent_tools.register_service("import_conversion", _agent_import_conversion)
    agent_tools.register_service("export_questions", _agent_export_questions)
    agent_tools.register_service("apply_review_fixes", _agent_apply_review_fixes)
    agent_tools.register_service("enable_template", _agent_enable_template)
