"""选项丢失检测：MinerU 原文里「有选项标签、没选项内容」的行。

为什么需要它（2026-08-05 实测）：MinerU 服务端把 `model_version=vlm` 从 3.4.0
静默升到 3.4.4 后，**行内公式选项会被整段漏识别**，只剩裸标签：

    1．设集合 $A = ...$ ，则 $A \\cap B =$ A． B． C． D. {1,2,3}
                                        ↑ 集合 {-7,-3} 等三项凭空消失

这类丢失在既有链路里**完全静默**：md、`content_list.json`、`layout.json` 的
span、`model.json` 的检测框四层全都没有那些内容——不是渲染丢的，是识别阶段就
漏了，所以下游任何一层都无从发现。DeepSeek 拿到的草稿本身就缺内容，它既补不出
来、也不会报错，最后是一道选项空白的题静默进题库。

判据取「标签之间没有实质字符」，不取 bbox 空隙。标定过（14 份真实产物）：
bbox 空隙区分不开——已知丢失行的 max gap/行高最小是 3.0，而健康选项行最大到
5.25（四栏排版的栏间空白本来就宽），两者严重重叠。而文本判据在同一批数据上
10/10 命中、3.4.0 的 148 个健康选项行 0 误报。

检测只读原文文本，不依赖 JSON：同一个检测器在 md 与 layout.json 上跑出的结果
逐行相同（JSON 里的内容与 md 一致，丢失是更上游发生的）。所以放在 raw_md 上做，
两条识别引擎（whole / block）都能用。

本模块只报告、不修改正文。丢了的内容凭空补不回来，猜一个填进去比空着更糟——
与 `/dedup` 剪枝「要么给出严格上界，要么别做」同一个取舍：宁可让用户看到
「第 1 题选项疑似缺失」，也不要静默交付一道残题。
"""

import dataclasses
import re

# 选项标签：`A．` `A.`，允许紧跟数字（实测 `A. -1013B．`，上一项内容与下一个标签
# 之间没有空格）。
#
# 前向断言排除两类误命中，都是实测出来的：
#   - 拉丁字母：LaTeX 命令名与变量里字母极多（`\Big.` `AB.`），跟着字母的 `A`
#     不是选项标签；
#   - 反斜杠：`\B` 之类的命令开头。
#
# **不认 `、` 作分隔符**：散文里枚举选项用的正是顿号——实测题目解析里
# 「选 A、B、C、D 占比分别为 19.95%、…」会被判成「A、B 内容为空」的假阳性。
# 真选项标签一律用 `．` 或 `.`，少认一种分隔符换掉这个误报很划算。
_LABEL_RE = re.compile(r"(?<![A-Za-z\\])([A-D])\s*[．.]")

# 超长行不查：那多半是整段解析或表格，里面的 A. B. 是正文而非选项。
_MAX_LINE_LEN = 600

# 至少要认出 3 个标签才算选项行。两个（`A. … B. …`）在数学正文里太常见
# （`A.` 可能是点 A 加句末点号），误报代价高于漏报。
_MIN_LABELS = 3


# 行首题号，用来把「第 137 行」翻译成用户能对照原卷找到的「第 3 题」。
# 只认行首、只认阿拉伯数字：这里不需要 blocksplit 那套方言判定，认不出来就
# 退回行号，代价只是提示语没那么好找，不会误报。
_LEAD_NUM_RE = re.compile(r"^\s*(\d{1,3})\s*[．.、]")


@dataclasses.dataclass(frozen=True)
class OptionGap:
    """一处疑似丢失。line_no 是 1 起的行号，便于与 raw_md 对账。"""

    line_no: int
    empty: str          # 内容为空的标签，如 "ABCD" / "BD"
    total: int          # 该行认出的标签总数
    text: str           # 行原文（截断），给用户看的上下文

    @property
    def question_no(self) -> int | None:
        """该行行首的题号，认不出返回 None。"""
        m = _LEAD_NUM_RE.match(self.text)
        return int(m.group(1)) if m else None

    def describe(self) -> str:
        labels = "、".join(self.empty)
        qn = self.question_no
        where = f"第 {qn} 题" if qn is not None else f"原文第 {self.line_no} 行"
        return f"{where}的选项 {labels} 没有内容"


def find_empty_options(text: str) -> list[OptionGap]:
    """扫描原文，返回所有「有标签、无内容」的行。

    逐行独立判断：选项行在 MinerU 输出里可能与题干同行、也可能独占一行，
    两种都能覆盖，而跨行拼接反而会把相邻两题的标签混在一起。
    """
    out: list[OptionGap] = []
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > _MAX_LINE_LEN:
            continue
        empty, total = _scan_line(line)
        if empty:
            out.append(OptionGap(line_no=i, empty="".join(empty), total=total,
                                 text=line.strip()[:200]))
    return out


def _scan_line(line: str) -> tuple[list[str], int]:
    """单行扫描，返回 (内容为空的标签列表, 认出的标签总数)。"""
    # 每个字母只取首次出现：选项内容里可能再出现 `A.`（`点 A. `），
    # 取首次命中即按 A→B→C→D 的自然顺序切段。
    pos: dict[str, tuple[int, int]] = {}
    for m in _LABEL_RE.finditer(line):
        c = m.group(1)
        if c not in pos:
            pos[c] = (m.start(), m.end())
    if len(pos) < _MIN_LABELS:
        return [], len(pos)

    order = [c for c in "ABCD" if c in pos]
    # 标签必须按 A<B<C<D 的位置顺序出现。乱序说明这些字母是正文里的数学对象
    # （`若 C. 在 B. 之外`），不是选项标签。
    starts = [pos[c][0] for c in order]
    if starts != sorted(starts):
        return [], len(pos)

    empty: list[str] = []
    for i, c in enumerate(order):
        seg_start = pos[c][1]
        seg_end = pos[order[i + 1]][0] if i + 1 < len(order) else len(line)
        if not line[seg_start:seg_end].strip():
            empty.append(c)
    return empty, len(order)


def build_note(gaps: list[OptionGap], *, max_items: int = 3) -> str:
    """把检测结果写成一句给用户看的话。没有丢失时返回空串。

    只列前 max_items 处、其余折成计数：这句话要显示在校对页的提示条里，
    列满十几行会把页面挤爆，而用户真正需要的是「哪几道题要重点核对」。
    """
    if not gaps:
        return ""
    head = "；".join(g.describe() for g in gaps[:max_items])
    more = ""
    if len(gaps) > max_items:
        more = f"，另有 {len(gaps) - max_items} 处同类问题"
    return (f"识别结果里有 {len(gaps)} 处选项只剩标签、没有内容（{head}{more}）。"
            f"这是 OCR 阶段就漏掉的内容，AI 无法补回，请对照原文手动补全这些选项。")
