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
from pathlib import Path

import blocknorm
import blocksplit
import mechfix

logger = logging.getLogger(__name__)

_TAG = {"单选": "[单选]", "多选": "[多选]", "填空": "[填空]", "解答": "[解答]"}


def _indent(text: str) -> str:
    """把块正文的第二行起缩进两空格。

    老格式靠「行首 `- ` 且非两空格缩进」判题界（importer.split_questions），
    续行不缩进的话，正文里任何以 `- ` 开头的行都会被当成新题起点而把一题劈开。
    """
    lines = text.split("\n")
    return "\n".join([lines[0]] + ["  " + l if l.strip() else "" for l in lines[1:]])


def _render(nb, sol_text: str) -> str:
    """把一个题（含解析）渲染成一个老格式 `- ` 块。"""
    body = nb.body.strip()
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


def run(raw_md: str, client, *, keep_images: bool = True,
        include_solution: bool = True, only_numbers=None,
        artifact_dir=None, name: str = "blocks") -> str:
    """跑完整条逐块路径，返回老格式的规范化 md。

    include_solution=False 时仍然照常判类型（判「哪段是解析」才知道该扔掉哪段），
    只是渲染时不输出解析——比让模型「假装没看见解析」更稳，也免得解析文字被
    当成题干残留在题目里。
    """
    blocks = blocksplit.split_blocks(raw_md)
    if not blocks:
        logger.warning("机械切块没切出任何块，原文可能不是题目文档")
        return ""
    for b in blocks:
        b.text = mechfix.normalize_block(b.text, keep_images=keep_images)

    normed = blocknorm.normalize_blocks(blocks, client, keep_images=keep_images)
    by_index = _reconcile(blocks, normed)
    res = blocksplit.pair_blocks(blocks)
    _dump(artifact_dir, name, blocks, normed, res)

    want = set(only_numbers) if only_numbers else None
    out: list[str] = []
    for stem, sol in res.paired:
        nb = by_index.get(stem.index)
        if nb is None:
            continue
        # only_numbers 兜底与老路径一致：取不到题号的块保守保留，宁多勿漏
        if want is not None and stem.number is not None and stem.number not in want:
            continue
        sol_text = ""
        if include_solution:
            sol_text = nb.solution                      # 混合块：解析就在本块内
            if not sol_text.strip() and sol is not None:
                snb = by_index.get(sol.index)
                sol_text = (snb.solution or snb.body) if snb else sol.text
        out.append(_render(nb, sol_text))

    no_sol = sum(1 for s, x in res.paired
                 if x is None and not (by_index.get(s.index)
                                       and by_index[s.index].solution.strip()))
    logger.info("逐块路径完成：输出 %d 题，缺解析 %d，孤儿解析 %d，异常 %d 条",
                len(out), no_sol, len(res.orphan_solutions), len(res.conflicts))
    for c in res.conflicts[:5]:
        logger.warning("  · %s", c)
    return "\n\n".join(out)
