"""逐块识别路径的总编排（新增的第二条上传识别路径）。

把五层串起来：
  ① blocksplit.split_blocks   机械切块 + 分区/分组/区 判定（块数在此定死）
  ② mechfix.normalize_block   安全子集的机械排版
  ③ blocksplit 的 kind 预分类  明显纯解析的块省一次 LLM 调用
  ④ blocknorm.normalize_blocks 逐块并行判类型 + 规范化剩余排版
  ⑤ blocksplit.pair_blocks    按 (组, 题号) 程序化配对，配不上的明确记账

输出**沿用老路径的 md 格式**（每题一个顶层 `- `、块首 `[单选]` 类标签、解析以
`【解析】` 独占一行开头），这样 dedup.py / importer.py / 校对页一行都不用改，
两条路径的下游完全共用。

与老路径的关系：老路径（project-alpha 的 normalize 整篇规范化）保持原样不动，
这里是并行的另一条路。选哪条由 converter.convert_file(..., engine=...) 决定。
"""

import json
import logging
import re
from dataclasses import replace
from pathlib import Path

import blocknorm
import blocksplit
import mechfix
import qualcheck

logger = logging.getLogger(__name__)

_TAG = {"单选": "[单选]", "多选": "[多选]", "填空": "[填空]", "解答": "[解答]"}

# 题号被 OCR 吃掉时，下一道选择题常整段粘在上一题的 D 项之后。只有同时满足
# “相邻题号恰好差 2”“左右两段各自都有完整 A—D”“右段以常见题干起句开头”
# 才能证明中间确实少了一道题；少一个条件都不拆，避免把选项里的嵌套 A—D 误判。
_UNNUMBERED_CHOICE_HEAD_RE = re.compile(
    r"^(?:若|已知|设|在|记|执行|如图|函数|下列|某|为了|复数|向量|数列|"
    r"抛物线|椭圆|双曲线|正方体|给定|定义|有)"
)


def _indent(text: str) -> str:
    """把块正文的第二行起缩进两空格。

    老格式靠「行首 `- ` 且非两空格缩进」判题界（importer.split_questions），
    续行不缩进的话，正文里任何以 `- ` 开头的行都会被当成新题起点而把一题劈开。
    """
    lines = text.split("\n")
    return "\n".join([lines[0]] + ["  " + l if l.strip() else "" for l in lines[1:]])


_LEAD_NUM_RE = re.compile(r"^\s*(\d{1,3})\s*[.．、)）]")


def _with_number(body: str, number: int | None) -> str:
    """在块正文最前面补回原卷题号，形如 `3. 若…`。

    **这是「题目 md 按题号命名」唯一的数字来源**（2026-08-08 补）。链路是
    `blockpipe` 渲染 → `importer.block_number` 取号 → 导入预览的 `number` 字段 →
    `filestore.create_question(number=…)` → 文件名 `第3题.md`。下游那几段一直是
    对的，断点在这里：走 AI 时 LLM 顺手把题号剥了（`nb.body` 已不含题号），跳过
    AI 时 `mechfix.strip_lead_number` 明确剥掉，于是渲染出的块里没有任何数字，
    `block_number` 一律返回 None，文件名只能落回 uuid。

    收尾符固定用 `.`：`importer.block_number` 的 `_NUM_RE` 认它，
    `importer.strip_leading_number` 的 `_STRIP_NUM_RE` 也认它（那条刻意不认
    `)` `）`——小问写法），所以入库前这个号会被干净剥掉、不会残留进题干正文。
    **不要为此放宽 `_NUM_RE` 的 `\\D{0,4}`**：`block_number` 自己会先剥 `- ` 再剥
    `[单选]` 标签才去匹配，`\\D{0,4}?` 是懒惰的、可以匹配零个字符，所以
    `- [单选] 3. 若…` 本来就取得到 3，一个正则字符都不用动。
    """
    if number is None:
        return body
    if _LEAD_NUM_RE.match(body):        # 已经带号（LLM 没剥干净）就不叠第二个
        return body
    return f"{number}. {body}" if body else f"{number}."


def _normalize_body_layout(body: str, qtype: str) -> str:
    """在任务看板落盘前完成不依赖 LLM 的题型专属排版。"""
    if qtype in ("单选", "多选"):
        return mechfix.normalize_choice_options(body, known_choice=True)
    if qtype == "填空":
        return mechfix.ensure_fill_blank(body, "填空题")
    if qtype == "解答":
        return mechfix.normalize_subquestion_layout(body)
    return body


def _render(nb, sol_text: str, number: int | None = None) -> str:
    """把一个题（含解析）渲染成一个老格式 `- ` 块。

    number 是切块阶段定下的原卷题号（`Block.number`），带在题干最前面一路送到入库，
    文件名按它取。理由与格式见 `_with_number`。
    """
    body = _normalize_body_layout(nb.body.strip(), nb.qtype)
    body = _with_number(body, number)
    tag = _TAG.get(nb.qtype, "[解答]")
    parts = [f"{tag} {body}" if body else tag]
    if sol_text.strip():
        sol = sol_text.strip()
        if not sol.lstrip().startswith("【解析】"):
            sol = "【解析】\n" + sol
        parts.append(sol)
    return "- " + _indent("\n".join(parts))


def _reconcile(blocks, normed):
    """用 LLM 的块类型判断校正机械判出的「区」。

    机械层判区靠位置（参考答案标题 / 题号回退 / 解析块占比），漏判时解析块会留在
    题干区，配对阶段就会把它当成一道没有题干的「题」输出——这正是老路径最难看的
    那个症状（校对页出现一堆只有 `【解析】` 的空题）。LLM 在单块上判「这块是纯
    解析」很可靠，所以以它为准把 zone 掰回来。反向（解析区里判成题目）不动：那多
    半是解答题的解析复述了题干，掰过去反而凭空多一道题。
    """
    by_index = {nb.index: nb for nb in normed}
    flipped = 0
    for b in blocks:
        nb = by_index.get(b.index)
        if nb is not None and b.zone == "stem" and nb.kind == "solution":
            b.zone = "solution"
            nb.zone = "solution"
            flipped += 1
    if flipped:
        logger.info("按 LLM 判定把 %d 个块从题干区改判为解析区", flipped)
    return by_index


def _dump(artifact_dir, name: str, blocks, normed, res) -> None:
    """落一份中间产物，供事后复盘。

    老路径只留 `_raw.md` 和 `_normalized.md`，出问题时无从判断是切块错了、判类型
    错了还是配对错了。这里把三层的中间结果都写下来，定位一次问题少猜几轮。
    写失败不影响转换本身（诊断产物不该反过来搞掉主流程）。
    """
    if artifact_dir is None:
        return
    try:
        d = Path(artifact_dir)
        d.mkdir(parents=True, exist_ok=True)
        by_index = {nb.index: nb for nb in normed}
        data = {
            "blocks": [
                {"index": b.index, "number": b.number, "group": b.group,
                 "zone": b.zone, "section": b.section, "line_no": b.line_no,
                 "kind_pre": b.kind,
                 "kind_llm": getattr(by_index.get(b.index), "kind", None),
                 "qtype": getattr(by_index.get(b.index), "qtype", None),
                 "degraded": getattr(by_index.get(b.index), "degraded", None),
                 "head": b.head()}
                for b in blocks
            ],
            "paired": [
                {"stem": s.index, "solution": (x.index if x else None),
                 "number": s.number, "group": s.group}
                for s, x in res.paired
            ],
            "orphan_solutions": [b.index for b in res.orphan_solutions],
            "conflicts": res.conflicts,
        }
        (d / f"{name}_blocks.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("中间产物写入失败（不影响转换）: %s", e)


def _filter_by_numbers(blocks: list, only_numbers) -> list:
    """按题号过滤，规则与 run() 的 only_numbers 兜底一致：取不到题号的块保守保留，
    宁多勿漏。

    **按 (组, 题号) 坐标判，不按位置判**。原先是位置式的：遇题干块决定留/弃、
    随后的解析块跟着走，直到下一个题干块。那条规则依赖「解析紧跟在自己题目后面」
    这个不变量，而 2026-08-07 在 60 份真实产物上标定的结论正相反——真实试卷压倒性
    地是「所有题干在前、所有解析统一附在卷末」（`SSSSSSssssss`，见 group_blocks
    的注释）。这种形态下所有解析块都排在最后一个题干块之后，于是全体继承它的
    留/弃标记：`only_numbers=[1]` 时最后一题被弃 → 每一份解析都被静默丢掉，
    只剩一道没有解析的题；`only_numbers=[最后一题]` 时反过来，把全卷解析都留下。

    坐标判没有这个问题，而且与 pair_blocks 用的是同一套坐标，两边不会打架。
    取不到题号的解析块保留（宁多勿漏，与题干块同一条规则）。
    """
    want = set(only_numbers)
    return [b for b in blocks if b.number is None or b.number in want]


def _recover_unnumbered_choice_blocks(blocks: list) -> list:
    """用单个题号空洞与双选项组，恢复被 OCR 吃掉题号的选择题。"""
    repaired = []
    for pos, block in enumerate(blocks):
        next_block = blocks[pos + 1] if pos + 1 < len(blocks) else None
        if not (next_block and block.zone == "stem"
                and next_block.zone == "stem"
                and isinstance(block.number, int)
                and isinstance(next_block.number, int)
                and next_block.number == block.number + 2
                and block.group == next_block.group
                and block.section == next_block.section):
            repaired.append(block)
            continue

        lines = block.text.splitlines()
        split_at = None
        for line_no in range(1, len(lines)):
            tail = "\n".join(lines[line_no:]).strip()
            if not _UNNUMBERED_CHOICE_HEAD_RE.match(tail):
                continue
            head = "\n".join(lines[:line_no]).strip()
            if (mechfix.has_complete_choice_options(head, known_choice=True)
                    and mechfix.has_complete_choice_options(
                        tail, known_choice=True)):
                split_at = line_no
                break
        if split_at is None:
            repaired.append(block)
            continue

        head = "\n".join(lines[:split_at]).rstrip()
        tail = "\n".join(lines[split_at:]).strip()
        missing = block.number + 1
        block.text = head
        recovered = replace(
            block, index=0, number=missing, text=f"{missing}. {tail}",
            line_no=block.line_no + split_at,
        )
        repaired.extend((block, recovered))

    for index, block in enumerate(repaired):
        block.index = index
    return repaired


def _repair_solution_section_drift(blocks: list) -> list:
    """修复多栏阅读顺序导致的“解答题仍沿用填空题分区”。

    分区标题可能被抽到上一栏，但题号顺序通常仍正确。只在同一组里出现至少三道
    连号题、它们都被标为填空题、都有连续的（1）（2）小问且没有填空线时改判。
    单道带小问的填空题和普通正文括号均不会触发。
    """
    candidates = []
    for block in blocks:
        is_candidate = (
            block.zone == "stem"
            and isinstance(block.number, int)
            and _FILL_SECTION_RE.search(block.section or "") is not None
            and "___" not in block.text
            and mechfix.has_sequential_subquestions(block.text)
        )
        if is_candidate:
            candidates.append(block)
            continue
        _repair_solution_run(candidates)
        candidates = []
    _repair_solution_run(candidates)
    return blocks


def _repair_fill_prefix_in_solution_section(blocks: list) -> list:
    """恢复误并入解答题分区开头的连续填空题。

    多栏试卷偶尔会漏掉“填空题”标题，使若干填空题与后面的解答题共用同一分区。
    这里只认同一分区开头至少三道连续、带明确答题空且没有选项的题，并要求其后
    确实出现带连续小问的解答题；这两个边界同时成立才改判，避免误伤普通大题。
    """
    for run in _stem_runs(blocks):
        section = run[0].section or ""
        if len(run) < 4 or "解答" not in section:
            continue
        prefix = []
        for block in run:
            is_fill = (
                not mechfix.has_complete_choice_options(
                    block.text, known_choice=True)
                and ("___" in block.text
                     or re.search(
                         r"\\(?:underline|underbar|hspace|rule)\s*\{",
                         block.text)
                     or _FILL_PROMPT_TAIL_RE.search(block.text.strip()))
            )
            if not is_fill:
                break
            prefix.append(block)
        if (len(prefix) < 3 or len(prefix) == len(run)
                or not mechfix.has_sequential_subquestions(
                    run[len(prefix)].text)):
            continue
        for block in prefix:
            block.section = "填空题（由解答题段开头连续答题空恢复）"
    return blocks


_GENERIC_CHOICE_SECTION_RE = re.compile(r"选择题")
_SPECIFIC_CHOICE_SECTION_RE = re.compile(r"单选|多选|不定项|单项|多项")
_FILL_SECTION_RE = re.compile(r"(?:填空|实验)题")
_FILL_PROMPT_TAIL_RE = re.compile(
    r"(?:=|为|是)\s*(?:\$|[.。．]|\\qquad|\\underline|___|\s)*$")


def _stem_runs(blocks: list) -> list[list]:
    """按连续且同分区的题干块分组，供分区漂移修复共用。"""
    runs: list[list] = []
    current: list = []
    current_section = None
    for block in blocks:
        if block.zone != "stem":
            if current:
                runs.append(current)
                current = []
                current_section = None
            continue
        section = block.section or ""
        if current and section != current_section:
            runs.append(current)
            current = []
        current.append(block)
        current_section = section
    if current:
        runs.append(current)
    return runs


def _repair_choice_section_drift(blocks: list) -> list:
    """把浙江卷式“标题写填空、整段实际为 A-D 选择题”恢复为单选分区。"""
    image_re = re.compile(r"!\[[^\]]*\]\([^)]*\)")
    for run in _stem_runs(blocks):
        section = run[0].section or ""
        if len(run) < 4 or _FILL_SECTION_RE.search(section) is None:
            continue
        choice_signals = sum(
            1 for block in run
            if (mechfix.has_complete_choice_options(block.text, known_choice=True)
                or (mechfix.has_choice_answer_blank(block.text)
                    and len(image_re.findall(block.text)) >= 3)))
        if choice_signals * 2 < len(run):
            continue
        for block in run:
            block.section = "单选题（由整段 A-D 选项恢复）"
    return blocks


def _repair_fill_section_drift(blocks: list) -> list:
    """把上海卷式“标题称选择题、整段实际为填空”恢复为填空分区。

    只处理至少四道连续题组成的同一分区：分区只能是泛称“选择题”，整段不能出现
    完整 A-D 或选择题答题括号，并且至少半数题目带答题空／以“=、为、是”收束。
    三个条件共同成立时才改分区，避免把偶发丢选项的普通选择题整段误判为填空。
    """
    for run in _stem_runs(blocks):
        section = run[0].section or ""
        if (len(run) < 4
                or not _GENERIC_CHOICE_SECTION_RE.search(section)
                or _SPECIFIC_CHOICE_SECTION_RE.search(section)):
            continue
        if any(mechfix.has_complete_choice_options(block.text, known_choice=True)
               or mechfix.has_choice_answer_blank(block.text)
               for block in run):
            continue
        fill_signals = sum(
            1 for block in run
            if ("___" in block.text
                or re.search(r"\\(?:underline|underbar|hspace|rule)\s*\{",
                             block.text)
                or _FILL_PROMPT_TAIL_RE.search(block.text.strip())))
        if fill_signals * 2 < len(run):
            continue
        for block in run:
            block.section = "填空题（由整段无选项答题空恢复）"
    return blocks


def _repair_generic_choice_section(blocks: list) -> list:
    """用整段多数证据把泛称“选择题”落实为单选，保住个别 OCR 残缺题的题型。

    ``blocknorm._guess_type`` 刻意不凭“选择题”三个字直接判单选，因为少量原卷标题
    会漂移成填空题；上一步已经用整段填空证据处理了这种反例。剩余连续分区至少四
    题、且过半题有完整 A-D 或答题括号时，可以把整段落实为单选。题面明确写多选的
    题仍由 ``_guess_type`` 的更高优先级规则判多选。
    """
    for run in _stem_runs(blocks):
        section = run[0].section or ""
        if (len(run) < 4
                or not _GENERIC_CHOICE_SECTION_RE.search(section)
                or _SPECIFIC_CHOICE_SECTION_RE.search(section)):
            continue
        signal_positions = [
            index for index, block in enumerate(run)
            if (mechfix.has_complete_choice_options(block.text, known_choice=True)
                or mechfix.has_choice_answer_blank(block.text))
        ]
        if len(signal_positions) * 2 < len(run):
            continue
        # 只落实首个到末个选择题信号之间的区间。OCR 若漏掉“填空题”标题，题号
        # 13—16 会和前 12 道选择题落入同一泛称分区；把整段都改成单选会误伤
        # 尾部填空。区间内部允许个别题选项 OCR 残缺，尾部无信号题则保持原判定。
        for block in run[signal_positions[0]:signal_positions[-1] + 1]:
            block.section = "单选题（由整段选择题多数证据恢复）"
        trailing = run[signal_positions[-1] + 1:]
        if len(trailing) >= 3:
            trailing_fill_signals = sum(
                1 for block in trailing
                if ("___" in block.text
                    or re.search(r"\\(?:underline|underbar|hspace|rule)\s*\{",
                                 block.text)
                    or _FILL_PROMPT_TAIL_RE.search(block.text.strip()))
            )
            if (trailing_fill_signals * 2 >= len(trailing)
                    and not any(
                        mechfix.has_complete_choice_options(
                            block.text, known_choice=True)
                        or mechfix.has_choice_answer_blank(block.text)
                        for block in trailing)):
                for block in trailing:
                    block.section = "填空题（由选择题段后的连续填空恢复）"
    return blocks


def _repair_solution_run(run: list) -> None:
    if len(run) < 3:
        return
    first = run[0]
    if any(block.group != first.group for block in run):
        return
    numbers = [block.number for block in run]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return
    for block in run:
        block.section = "解答题（由连续小问题面恢复）"


def split_and_prep(raw_md: str, *, keep_images: bool = True,
                   num_template: str = "", only_numbers=None,
                   note_sink=None, run_quality_checks: bool = True,
                   boundary_mode: str = blocksplit.BOUNDARY_MODE_AUTO) -> list:
    """切块 + 机械排版，不跑 LLM。是 run() 的前半段，拆出来供人工审核暂停点复用
    （见 converter.convert_file_to_blocks）：暂停点要先切好块给人工看，LLM 判定
    要等审核完（合并/拆分/删除/调序）之后才跑。

    only_numbers 在这里过滤（而不是留到渲染时像 run() 那样按题号跳过）：
    人工审核页应该只看到用户要的这些题，多余的块摆在审核页里只会让人误以为
    还得管它们。
    """
    mode = blocksplit.normalize_boundary_mode(boundary_mode)
    blocks, note = blocksplit.split_blocks_with_note(
        raw_md, num_template=num_template, boundary_mode=mode)
    if note and note_sink is not None:
        note_sink(note)
    if not blocks:
        logger.warning("机械切块没切出任何块，原文可能不是题目文档")
        return []
    for b in blocks:
        b.text = mechfix.normalize_block(b.text, keep_images=keep_images)
    if mode != blocksplit.BOUNDARY_MODE_WHITELIST:
        blocks = _recover_unnumbered_choice_blocks(blocks)
    blocks = _repair_choice_section_drift(blocks)
    blocks = _repair_fill_section_drift(blocks)
    blocks = _repair_generic_choice_section(blocks)
    blocks = _repair_solution_section_drift(blocks)
    blocks = _repair_fill_prefix_in_solution_section(blocks)
    # 体检在过滤之前（理由同 run()：过滤后的题号序列本来就不连续）
    if run_quality_checks and note_sink is not None:
        check_numbering = mode != blocksplit.BOUNDARY_MODE_WHITELIST
        pairing = blocksplit.pair_blocks(
            blocks, check_number_gaps=check_numbering)
        for line in qualcheck.report(
                blocks, pairing, check_numbering=check_numbering):
            note_sink(line)
    if only_numbers:
        blocks = _filter_by_numbers(blocks, only_numbers)
    return blocks


def _sequential_group(blocks) -> list[tuple]:
    """按当前顺序把块分组：遇 zone=stem 的块开一道新题，紧随其后的 zone=solution
    块都归入它，直到遇见下一个 stem。

    **这是退化档，不是首选**——见 group_blocks 的选择逻辑。只在坐标配对确实用不上
    （题号大面积缺失，通常是人工审核拆分出的新块）时才用它。

    审核前就有孤儿解析（在第一道题之前、或整份文档没有任何 stem 块）时，直接
    丢弃：人工审核阶段本该把它们删除或挪到正确位置，跳过 AI 也好、送入 AI 也好
    都不该凭空生成一道没有题干的"题"。
    """
    groups: list[tuple] = []
    cur = None
    for b in blocks:
        if b.zone == "stem":
            cur = (b, [])
            groups.append(cur)
        elif cur is not None:
            cur[1].append(b)
    return groups


def _coordinate_group(blocks) -> list[tuple]:
    """按 (group, number) 坐标配对分组，形状与 _sequential_group 一致。"""
    pr = blocksplit.pair_blocks(blocks)
    return [(st, [so] if so is not None else []) for st, so in pr.paired]


def group_blocks(
        blocks, *,
        boundary_mode: str = blocksplit.BOUNDARY_MODE_AUTO) -> list[tuple]:
    """题块 → [(题块, [解析块…])…]，坐标配对优先、顺序分组兜底。

    **顺序分组不能当首选**（2026-08-07 在 60 份真实产物上标定后改的）：真实试卷
    压倒性地是「所有题干在前、所有解析统一附在卷末」的形态，切出来的 zone 序列
    长这样 `SSSSSSSSSSSSssssssssssss`。这种形态下「紧跟在题干后面的解析属于这道
    题」这条不变量根本不成立——12 份解析会全部挂到最后一道题上，前 11 题一份解析
    都没有。11 份带解析的产物 100% 命中，pair_blocks 在同一批上是 12/12 全配对。

    所以恢复以 pair_blocks 的坐标配对为主。顺序分组保留为退化档，理由仍然成立：
    人工审核拆分出的新块没有题号，坐标配对对它们无能为力。判据是「题块取得到
    题号的比例」——`missing_numbers` 过半就说明这批块的坐标已经不可信（审核期
    大量拆分），改按顺序走；否则一律用坐标。

    两条路都产出同一个形状，调用方（render_without_ai / normalize_and_render）
    不需要知道走了哪档。
    """
    mode = blocksplit.normalize_boundary_mode(boundary_mode)
    stems = [b for b in blocks if b.zone == "stem"]
    if not stems:
        groups = _sequential_group(blocks)
        return (groups if mode == blocksplit.BOUNDARY_MODE_WHITELIST
                else _order_complete_numbered_groups(groups))
    missing = sum(1 for b in stems if b.number is None)
    if missing * 2 > len(stems):
        logger.info("题块题号缺失 %d/%d，配对退化为按顺序分组", missing, len(stems))
        groups = _sequential_group(blocks)
    else:
        groups = _coordinate_group(blocks)
    return (groups if mode == blocksplit.BOUNDARY_MODE_WHITELIST
            else _order_complete_numbered_groups(groups))


def _order_complete_numbered_groups(groups: list[tuple]) -> list[tuple]:
    """题号完整且唯一时按题号排序，证据不足时保持识别顺序。

    PDF 多栏阅读顺序可能把 14、15 识别在 12、13 前面；总题数仍完整时继续沿 OCR
    顺序落库，会制造永久错误的 order。只有所有题号均可读、无重复并且从最小值到
    最大值连续时才排序。分组卷常在 A/B 组重复 1..N，重复号会自动退出本规则。
    """
    if len(groups) < 2:
        return groups
    numbers = [getattr(stem, "number", None) for stem, _sols in groups]
    if not all(isinstance(number, int) for number in numbers):
        return groups
    ordered = sorted(numbers)
    if (len(set(numbers)) != len(numbers)
            or ordered != list(range(ordered[0], ordered[-1] + 1))):
        return groups
    return sorted(groups, key=lambda group: group[0].number)


def _strip_repeated_stem_from_solution(stem_text: str, solution_text: str) -> str:
    """答案卷逐题重抄题干时，只保留分歧处之后的答案与解析。

    必须至少逐字符相同 8 字，且共同前缀覆盖原题干一半以上才剥；普通“解：由题
    意……”即使偶然同词也达不到覆盖率，不会被截断。填空题通常在末尾答题线处分
    歧，剥完会从具体答案开始；选择题完整重抄时则从答案／解析标记开始。
    """
    limit = min(len(stem_text), len(solution_text))
    common = 0
    while common < limit and stem_text[common] == solution_text[common]:
        common += 1
    if common < 8 or common * 2 < len(stem_text):
        return solution_text
    trimmed = solution_text[common:].lstrip(" \t　,，.。:：;；")
    return trimmed or solution_text


def render_without_ai(
        blocks, *, include_solution: bool = True,
        boundary_mode: str = blocksplit.BOUNDARY_MODE_AUTO) -> str:
    """跳过 AI 标准化：把块渲染成老格式 `- [题型]` 块。

    题型必须在这里利用 block.section 判定并带给下游。机械切块时还看得到
    「二、多项选择题」这类分区标题，渲染后标题不会进入每道题正文；若不在这里
    写标签，importer.guess_type() 只能看到同样的 A-D 选项，会把所有多选题都判成
    单选题。判定复用 blocknorm 的机械兜底规则，AI 降级与完全不送 AI 两条路同源。

    题干/解析的题号在这里先剥掉（mechfix.strip_lead_number）再由 `_with_number`
    以统一格式（`3. `）补回题干最前面。原文题号写法五花八门（`3）` `第三题`
    `**3.**`、还可能带分值标注），先剥后补等于把它归一化成 `block_number` 与
    `strip_leading_number` 两边口径都认的那一种；解析块的题号只剥不补（解析不入
    库成题，带号只会残留进正文）。
    """
    groups = group_blocks(blocks, boundary_mode=boundary_mode)
    out: list[str] = []
    for stem, sols in groups:
        qtype = blocknorm._guess_type(stem.text, stem.section, stem.number)
        body = mechfix.strip_lead_number(stem.text, stem.number).strip()
        body = _normalize_body_layout(body, qtype)
        body = _with_number(body, stem.number)
        tag = _TAG[qtype]
        parts = [f"{tag} {body}" if body else tag]
        if include_solution and sols:
            sol_text = "\n\n".join(
                t for t in (_strip_repeated_stem_from_solution(
                    mechfix.strip_lead_number(stem.text, stem.number).strip(),
                    mechfix.strip_lead_number(s.text, s.number).strip())
                            for s in sols) if t)
            if sol_text:
                if not sol_text.lstrip().startswith("【解析】"):
                    sol_text = "【解析】\n" + sol_text
                parts.append(sol_text)
        out.append("- " + _indent("\n".join(parts)))
    return "\n\n".join(out)


def normalize_and_render(blocks, client, *, keep_images: bool = True,
                         include_solution: bool = True,
                         boundary_mode: str = blocksplit.BOUNDARY_MODE_AUTO) -> str:
    """人工审核过的块 → 逐块 LLM 判定 + 规范化渲染，供「送入 AI 标准化」分支用。

    与 run() 的差别只在配对入口：这里走 group_blocks（坐标配对为主、题号大面积
    缺失时才退化成按顺序），run() 直接用 pair_blocks 因为那时块还没被人工动过。
    切块、机械排版、图片拦截都已经在审核暂停前做过，这里不重复做，也不落
    _dump 诊断产物（暂停前的中间产物目录此时已被 project-alpha 的 _cleanup_temp
    清理，没有稳定的落盘位置）。
    """
    normed = blocknorm.normalize_blocks(blocks, client, keep_images=keep_images)
    by_index = _reconcile(blocks, normed)
    groups = group_blocks(blocks, boundary_mode=boundary_mode)

    out: list[str] = []
    for stem, sols in groups:
        nb = by_index.get(stem.index)
        if nb is None:
            continue
        sol_text = ""
        if include_solution:
            sol_text = nb.solution
            if not sol_text.strip() and sols:
                parts = []
                for s in sols:
                    snb = by_index.get(s.index)
                    parts.append((snb.solution or snb.body) if snb else s.text)
                sol_text = "\n\n".join(p.strip() for p in parts if p.strip())
        out.append(_render(nb, sol_text, stem.number))
    logger.info("人工审核后送入 AI 标准化完成：输出 %d 题", len(out))
    return "\n\n".join(out)


def run(raw_md: str, client, *, keep_images: bool = True,
        include_solution: bool = True, only_numbers=None,
        artifact_dir=None, name: str = "blocks",
        num_template: str = "", note_sink=None,
        boundary_mode: str = blocksplit.BOUNDARY_MODE_AUTO) -> str:
    """跑完整条逐块路径，返回老格式的规范化 md。

    include_solution=False 时仍然照常判类型（判「哪段是解析」才知道该扔掉哪段），
    只是渲染时不输出解析——比让模型「假装没看见解析」更稳，也免得解析文字被
    当成题干残留在题目里。

    num_template 是用户指定的题号模板（见 blocksplit.compile_dialect），空串=自动。
    note_sink 是个单参可调用对象，切块阶段有话要对用户说时调它一次（两种情形：
    指定的模板没切开已回退自动、大量正文没归入任何题被丢弃，见
    blocksplit.split_blocks_with_note）。返回值是 md 文本、没地方捎带这句话，
    而这句话不能只进日志——用户看不到日志，只会对着切歪的结果猜原因。
    """
    mode = blocksplit.normalize_boundary_mode(boundary_mode)
    blocks, note = blocksplit.split_blocks_with_note(
        raw_md, num_template=num_template, boundary_mode=mode)
    if note and note_sink is not None:
        note_sink(note)
    if not blocks:
        logger.warning("机械切块没切出任何块，原文可能不是题目文档")
        return ""
    for b in blocks:
        b.text = mechfix.normalize_block(b.text, keep_images=keep_images)
    if mode != blocksplit.BOUNDARY_MODE_WHITELIST:
        blocks = _recover_unnumbered_choice_blocks(blocks)
    blocks = _repair_choice_section_drift(blocks)
    blocks = _repair_fill_section_drift(blocks)
    blocks = _repair_generic_choice_section(blocks)
    blocks = _repair_solution_section_drift(blocks)
    blocks = _repair_fill_prefix_in_solution_section(blocks)

    # 体检在过滤之前、也在 LLM 之前：题号空洞这类结构信号要看**整份文档**才成立，
    # 过滤完只剩用户要的那几道题，序列本来就是不连续的，检出来的全是噪声。
    if note_sink is not None:
        check_numbering = mode != blocksplit.BOUNDARY_MODE_WHITELIST
        pairing = blocksplit.pair_blocks(
            blocks, check_number_gaps=check_numbering)
        for line in qualcheck.report(
                blocks, pairing, check_numbering=check_numbering):
            note_sink(line)

    # only_numbers 在 LLM **之前**过滤。原先是在渲染时按题号跳过的，意味着「只取
    # 最后 3 道大题」也要为全卷 20 多个块各付一次 LLM 调用，其中七成的结果直接扔掉。
    # 与 split_and_prep 对齐后两条路径的过滤时机也一致了。
    if only_numbers:
        blocks = _filter_by_numbers(blocks, only_numbers)
        if not blocks:
            logger.warning("按题号过滤后没有剩下任何块")
            return ""

    normed = blocknorm.normalize_blocks(blocks, client, keep_images=keep_images)
    by_index = _reconcile(blocks, normed)
    res = blocksplit.pair_blocks(
        blocks, check_number_gaps=(
            mode != blocksplit.BOUNDARY_MODE_WHITELIST))
    _dump(artifact_dir, name, blocks, normed, res)

    out: list[str] = []
    ordered_pairs = [
        (stem, [sol] if sol is not None else []) for stem, sol in res.paired]
    if mode != blocksplit.BOUNDARY_MODE_WHITELIST:
        ordered_pairs = _order_complete_numbered_groups(ordered_pairs)
    for stem, solutions in ordered_pairs:
        sol = solutions[0] if solutions else None
        nb = by_index.get(stem.index)
        if nb is None:
            continue
        sol_text = ""
        if include_solution:
            sol_text = nb.solution                      # 混合块：解析就在本块内
            if not sol_text.strip() and sol is not None:
                snb = by_index.get(sol.index)
                sol_text = (snb.solution or snb.body) if snb else sol.text
        out.append(_render(nb, sol_text, stem.number))

    no_sol = sum(1 for s, x in res.paired
                 if x is None and not (by_index.get(s.index)
                                       and by_index[s.index].solution.strip()))
    logger.info("逐块路径完成：输出 %d 题，缺解析 %d，孤儿解析 %d，异常 %d 条",
                len(out), no_sol, len(res.orphan_solutions), len(res.conflicts))
    for c in res.conflicts[:5]:
        logger.warning("  · %s", c)
    return "\n\n".join(out)
