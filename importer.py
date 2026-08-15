"""规范化 Markdown 题目导入。

主输入是 project-alpha 规范化输出：每题一个顶层 `- ` 无序列表项，题间空行
（切分算法原样沿用 project-alpha/src/validator.py:_split_by_question）。

但导入页的文本框和 .md 上传是**开放输入**：用户经常直接粘一份手写/网上抄来的
题目，那种文本没有 `- `，只有 `1. 2. 3.` 题号。老实现只认 `- `，这类输入会被
整份当成"一道题"，校对页只出一张巨大的题卡——功能上等于不可用。所以切分改成
两种模式（见 split_questions）：契约格式走 `- `，其余走题号。
"""

import re

import mechfix

# 围栏代码块起止行（``` / ~~~，允许 markdown 惯例的最多三个前导空格）。
# 代码块里的 `- foo` 和 `1. foo` 是代码内容，不是题界。
_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
# markdown 表格行。表格的分隔行写作 `|---|---|`，其单元格里也可能有 `- `。
_TABLE_ROW_RE = re.compile(r"^\s{0,3}\|")

# 题号行：`12. 正文` / `12．` / `12、` / `12)` / `第12题`。
# 允许前面粘 markdown 标题记号与加粗标记——MinerU 与不少题库网站会把题号写成
# `## 12.` 或 `**12.**`，不认这两种就等于认不出题号。
_NUM_START_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__|\*)?\s*第?\s*(\d{1,3})"
    r"\s*(?:\*\*|__|\*)?\s*([.．、)）题])")
# 误命中排除：`1.1.5 函数…` 这类小节号、`0. 2 = 0. 5` 这类被 OCR 拆开的小数——
# 题号后面又紧跟一个「数字.」就不是题号。
_SECTION_TAIL_RE = re.compile(r"^\s*\d{1,3}\s*[.．、]")
# 纯标题/分割线行：判断"第一个题号之前那段文字"是卷名还是一道没编号的题时用
_HEAD_ONLY_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s|\*\*|\*\s|=+\s*$|-{3,}\s*$)")


def _fence_mask(lines: list[str]) -> list[bool]:
    """逐行标记是否落在围栏代码块内（围栏行本身也算"内"，不当题界）。"""
    mask: list[bool] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            mask.append(True)
            in_fence = not in_fence
            continue
        mask.append(in_fence)
    return mask


def _num_start(line: str):
    """行首题号 → 题号整数；不是题号行返回 None。"""
    m = _NUM_START_RE.match(line)
    if not m:
        return None
    num = int(m.group(1))          # \d 含全角数字，int() 也认，故全角题号自然可用
    if num == 0:
        return None                # 题号不会是 0，命中的多是被拆开的小数
    if _SECTION_TAIL_RE.match(line[m.end():]):
        return None
    return num


def _number_starts(lines: list[str], fenced: list[bool]) -> list[int]:
    """挑出题号行的行号，要求题号成**递增序列**。

    只看"像题号"会切得一塌糊涂：小问 `1)`、选项后的 `2.`、被 OCR 拆开的小数都长
    得像题号。真题号的可靠特征是全局递增，所以先收集全部候选，再取能连成最长
    递增序列的那一组。允许最多跳 2 个号（OCR 吃掉一两个题号是常事），比号小的
    候选一律当小问丢掉。锚点在前 5 个候选里逐个试，免得开头一个假题号带偏整份。
    """
    cands: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if fenced[i] or _TABLE_ROW_RE.match(line):
            continue
        num = _num_start(line)
        if num is not None:
            cands.append((i, num))

    best: list[int] = []
    for a in range(min(len(cands), 5)):
        picked = [cands[a][0]]
        expect = cands[a][1] + 1
        for idx, num in cands[a + 1:]:
            if expect <= num <= expect + 2:
                picked.append(idx)
                expect = num + 1
        if len(picked) > len(best):
            best = picked
    return best


def _lead_is_question(lead: str) -> bool:
    """第一个题界之前那段文字是不是一道（没编号的）题，而非卷名/小节标题。

    判据是"有没有一行像正文的长句"：标题、分割线、加粗卷名都短且带记号。
    宁可多留一张题卡让用户自己取消勾选，也不要静默吞掉一道题。
    """
    for line in lead.splitlines():
        s = line.strip()
        if not s or _HEAD_ONLY_RE.match(line):
            continue
        if len(s) >= 40:
            return True
    return False


def _blocks_from(lines: list[str], starts: list[int], *,
                 strip_bullet: bool) -> list[str]:
    """按给定的题界行号把 lines 切成块，过滤空块。"""
    out: list[str] = []
    lead = "\n".join(lines[:starts[0]]).strip()
    if lead and _lead_is_question(lead):
        out.append(lead)
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(lines)
        chunk = "\n".join(lines[s:e]).strip()
        if strip_bullet and chunk.startswith("- "):
            chunk = chunk[2:].strip()
        if chunk:
            out.append(chunk)
    return out


def split_questions(text: str) -> list[str]:
    """把 md 切成每题一块，返回各题正文（`- ` 格式会去掉前导 "- "）。

    两种模式，按输入自己长什么样选，不要求用户先把格式改对：
      1. **契约格式**（≥2 个顶层 `- ` 项）：走原来的 `- ` 切分。规范化输出走这条，
         行为与老实现一致——只多了两道护栏：围栏代码块内与表格行内的 `- ` 不再
         当题界（老实现会把一段带列表的解析劈成好几"题"）。
      2. **题号格式**（没有 `- `，或 `- ` 只有一个）：按递增题号切（见
         _number_starts）。手写/粘贴的题目走这条，老实现在这里只会返回一整块。
    两种都切不出 ≥2 块时，整份作为一块返回——不猜，交给用户在校对页处理。

    题号**保留**在块正文里，block_number() 还要靠它做「只取某几题」的漏题检测。
    """
    if not text or not text.strip():
        return []
    lines = text.splitlines()
    fenced = _fence_mask(lines)

    # 顶层 `- `：注意 startswith("- ") 已经意味着行首不是空格，所以老实现里那句
    # `not line.startswith("  ")` 是恒真的死代码，这里不再保留。
    bullets = [i for i, line in enumerate(lines)
               if line.startswith("- ") and not fenced[i]
               and not _TABLE_ROW_RE.match(line)]
    if len(bullets) >= 2:
        return _blocks_from(lines, bullets, strip_bullet=True)

    numbered = _number_starts(lines, fenced)
    if len(numbered) >= 2:
        return _blocks_from(lines, numbered, strip_bullet=False)
    if bullets:
        return _blocks_from(lines, bullets, strip_bullet=True)

    body = text.strip()
    return [body] if body else []


# ── 解析标记 ──────────────────────────────────────────────────────────────
# 契约格式只有 `【解析】` 一种写法，但**开放输入没有契约**：粘贴的题、跳过 AI 的
# 逐块渲染、以及 docx/PDF 里"题目在前、答案在后"的卷子，答案是 `【答案】`
# `答案：` `解析：` `解：` 各写各的。老实现只认 `【解析】`，其余全部留在题干里
# ——症状就是题卡解析栏空着、答案混在题干末尾。
#
# 分两档，因为这两档的误伤代价不一样：
#   · 行首档（_SOL_HEAD_RE）宽一些，收 `解：` `答：` `证明：`（口径同
#     blocknorm._fallback_split）。这些字样出现在行首基本只有一种含义。允许前面
#     粘题号（`3. 【答案】B`）与 markdown 记号。
#   · 行内档（_SOL_INLINE_RE）只收 `【答案】` `【解析】` `答案：` `解析：` 这几个
#     **不可能出现在题干正文里**的。刻意不收 `解：`——`解不等式：` `解方程：`
#     这类题干写法会被从中劈开，把半句题干当成解析。
_SOL_HEAD_RE = re.compile(
    r"^[\s>]*(?:#{1,6}\s*)?(?:\*\*|__|\*)?\s*"
    r"(?:\d{1,3}\s*[.．、)）]\s*)?(?:\*\*|__)?\s*"
    r"(?:【\s*(?:参考)?(?:答案|解析|解答|证明)\s*】"
    r"|(?:参考)?答案\s*[:：]|解析\s*[:：]|解答\s*[:：]"
    r"|解\s*[:：]|答\s*[:：]|证明\s*[:：])")
_SOL_INLINE_RE = re.compile(
    r"(?:【\s*(?:参考)?(?:答案|解析)\s*】|(?:参考)?答案\s*[:：]|解析\s*[:：])")

# 独占一行的"参考答案"大标题。切块是按题号切的，这一行落在**上一题**的块尾
# （它前面没有新题号），不剥掉的话最后一道题的题干会拖着个"参考答案"。
_ANS_HEAD_LINE_RE = re.compile(
    r"^[\s>]*(?:#{1,6}\s*)?(?:\*\*|__|\*)?\s*"
    r"(?:参考答案(?:与解析)?|答案(?:与|及)?解析|答案解析|详细解析|详解|答案|解析)"
    r"\s*[:：]?\s*(?:\*\*|__|\*)?\s*$")


def _split_at_markers(block: str) -> tuple[str, str | None]:
    """按 `答案/解析` 字样把块切成 (题干, 解析)，找不到标记则解析为 None。

    行首档优先整行切；同一轮里行内档只在该行**标记前面还有内容**时才切（否则
    等价于行首档）。取首个命中处，后面的全部归解析——答案区一旦开始就不会再回到
    题干（`答案：B 解析：因为…` 是一段连续的东西）。
    """
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if _SOL_HEAD_RE.match(line):
            return ("\n".join(lines[:i]).strip(),
                    "\n".join(lines[i:]).strip() or None)
        m = _SOL_INLINE_RE.search(line)
        if m and line[:m.start()].strip():
            marker = m.group(0)
            prefix = line[:m.start()]
            if (re.fullmatch(r"(?:参考)?答案\s*[:：]", marker)
                    and prefix.rstrip().endswith("的")):
                # ``数学问题的答案：已知数列……`` 中“答案”是题干里的普通名词，
                # 不是解析标记。只排除紧邻“的”的裸答案冒号；独占行、【答案】以及
                # ``题干……答案：B`` 仍沿用原有开放输入兼容规则。
                continue
            head = "\n".join(lines[:i] + [line[:m.start()]]).strip()
            tail = "\n".join([line[m.start():]] + lines[i + 1:]).strip()
            return head, (tail or None)
    return block.strip(), None


def strip_answer_head(text: str) -> str:
    """剥掉块尾独占一行的「参考答案」类大标题（见 _ANS_HEAD_LINE_RE）。"""
    lines = text.rstrip().splitlines()
    while lines and (not lines[-1].strip()
                     or _ANS_HEAD_LINE_RE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines).strip()


# 一份文档里出现**两套题号**（题目 1..N 之后答案又从 1 数一遍）时，切分只会老实
# 按题号切出 2N 块，后一半在校对页表现为"一堆只有答案字母的题"。判据照抄
# blocksplit._find_restart（那条是 2026-08-05 在真实产物上标定的）：回退处此前已
# 见过 ≥5 的题号、回退后至少还有 3 块。**门槛不能降**——短小节重新编号（"二、
# 填空题"后题号从 1 重开）长得一模一样，降门槛会把后半张卷子整个当成答案扔掉。
_RESTART_MIN_SEEN = 5
_RESTART_MIN_TAIL = 3


def find_number_restart(numbers: list) -> int | None:
    """题号序列里"回到 1"的下标（第二套题号的起点）；不构成回退返回 None。"""
    seen_max = 0
    for i, n in enumerate(numbers):
        if n is None:
            continue
        if n == 1 and seen_max >= _RESTART_MIN_SEEN and \
                (len(numbers) - i) >= _RESTART_MIN_TAIL:
            return i
        seen_max = max(seen_max, n)
    return None


def pair_duplicate_numbering(blocks: list[str], cut: int | None = None):
    """两套题号 → [(题干块, 解析块或 None)…]；只有一套题号返回 None。

    配对**严格按题号**，不按顺序（同 blocksplit.pair_blocks）：后一套里缺号、多号
    都是常事，按顺序错一个就整条错位。第二套里配不上任何题号的块直接丢——它是
    "参考答案"标题页或前言，凭空变成一道题更糟。取不到题号的题干块保留、没解析。

    `cut` 是第二套题号的起始下标。`split_questions_with_restart` 切出来的块自己
    知道这个下标，直接传进来；不传则按各块首行的题号自己找（契约格式里孤儿解析
    自成一个 `- ` 块的情形走这一支）。
    """
    numbers = [block_number(b) for b in blocks]
    if cut is None:
        cut = find_number_restart(numbers)
    if cut is None or not 0 < cut < len(blocks):
        return None
    # 「参考答案」这类分区标题独占一块时（切块只在它前面没有新题号才会这样），
    # 它落在 cut 之前、会变成一道空题干的题。整块只有标题的丢掉——它不含题目内容。
    sol_by_num: dict[int, list[str]] = {}
    for b, n in zip(blocks[cut:], numbers[cut:]):
        if n is not None:
            sol_by_num.setdefault(n, []).append(b)
    out: list[tuple[str, str | None]] = []
    for b, n in zip(blocks[:cut], numbers[:cut]):
        if n is None and not strip_answer_head(strip_type_tag(b)).strip():
            continue                       # 整块只有「参考答案」这类分区标题
        sols = sol_by_num.get(n) if n is not None else None
        # 同一题号在答案区出现多次（答案与解析各占一块）时全部并进解析
        sol = "\n\n".join(strip_leading_number(strip_type_tag(s)).strip()
                          for s in sols) if sols else None
        out.append((b, sol or None))
    return out


def _second_run(cands: list[tuple[int, int]], after: int) -> list[int]:
    """在 after 行之后的候选里找第二套题号（必须从 1 起）的最长递增序列。

    锚点**只认题号 1**，不像 `_number_starts` 那样在前 5 个候选里逐个试：这里已经
    有第一套题号定了调，"第二套"的定义就是编号回到 1。放宽锚点会让第一套里被
    `_number_starts` 丢掉的小问号（`1)` `2)`）攒成一条假的第二套。
    """
    rest = [(i, n) for i, n in cands if i > after]
    for k, (i, n) in enumerate(rest):
        if n != 1:
            continue
        picked = [i]
        expect = 2
        for j, m in rest[k + 1:]:
            if expect <= m <= expect + 2:
                picked.append(j)
                expect = m + 1
        return picked
    return []


def split_questions_with_restart(text: str):
    """按**两套题号**切：返回 `(所有块, 第二套的起始下标)`；不是两套题号返回 None。

    为什么不并进 `split_questions`：那个函数取的是最长递增序列，第二套题号编号更
    小，一律被当成小问丢掉——这是它的正确行为（小问 `1)` 与"答案区第 1 题"在单行
    上完全同形，靠递增性排除小问是唯一可靠的机械判据）。所以这里另开一条：先让
    它照常挑出第一套，再在第一套结束之后单独找一条从 1 起的第二套，两套都够门槛
    （见 `_RESTART_MIN_SEEN` / `_RESTART_MIN_TAIL`）才认。

    **契约格式（≥2 个顶层 `- `）直接返回 None**：那是 AI 规范化的输出，解析已经
    以 `【解析】` 跟在各自题里，没有第二套题号可言。
    """
    if not text or not text.strip():
        return None
    lines = text.splitlines()
    fenced = _fence_mask(lines)
    bullets = [i for i, line in enumerate(lines)
               if line.startswith("- ") and not fenced[i]
               and not _TABLE_ROW_RE.match(line)]
    if len(bullets) >= 2:
        return None

    first = _number_starts(lines, fenced)
    if len(first) < _RESTART_MIN_SEEN:
        return None
    cands: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if fenced[i] or _TABLE_ROW_RE.match(line):
            continue
        num = _num_start(line)
        if num is not None:
            cands.append((i, num))
    # 第一套里见过的最大题号要够大，否则"回到 1"更可能是小节重新编号
    seen_max = max((n for i, n in cands if i in set(first)), default=0)
    if seen_max < _RESTART_MIN_SEEN:
        return None
    second = _second_run(cands, first[-1])
    if len(second) < _RESTART_MIN_TAIL:
        return None

    starts = first + second
    blocks = _blocks_from(lines, starts, strip_bullet=False)
    # _blocks_from 会在首个题号之前那段像正文时多插一块，cut 要跟着挪
    lead = "\n".join(lines[:starts[0]]).strip()
    cut = len(first) + (1 if lead and _lead_is_question(lead) else 0)
    if cut >= len(blocks):
        return None
    return blocks, cut


def split_solution(block: str, *, scan_markers: bool = False) -> tuple[str, str | None]:
    """把一题块按 `【解析】` 标记切成 (题干, 解析)。

    规范化输出里解析紧跟题干、在同一个 `- ` 块内、独占一行、以 `【解析】` 开头
    （见 project-alpha/templates/normalize_prompt.md）。找到首个这样的行，
    之前为题干、之后（含该行）为解析。无标记则解析为 None。

    `scan_markers=True` 时改走按 `答案/解析` 字样扫的那一档
    （`_split_at_markers`，其 `_SOL_HEAD_RE` 已经含 `【解析】`，是本函数默认口径的
    超集）——开放输入用这一档。默认关着：契约格式已经有确定标记，多认几种写法
    只多一份误伤风险。

    **不能"先按 `【解析】` 切、切不到再扫标记"**：`【答案】B` 与 `【解析】…` 两行
    都在时，前者在上、后者在下，先切 `【解析】` 会把 `【答案】B` 留在题干里——
    这正是要修的症状。扫标记那一档取的是**最靠前**的标记，两行都能归进解析。
    """
    if scan_markers:
        return _split_at_markers(block)
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("【解析】"):
            stem = "\n".join(lines[:i]).strip()
            solution = "\n".join(lines[i:]).strip()
            return stem, (solution or None)
    return block.strip(), None


# 题号提取：与 project-alpha/src/normalizer.py 的 _block_number 保持一致，
# 支持阿拉伯数字与中文大写题号，用于「只取题号」漏题检测。
# 尾部允许 `题`：`第4题 已知…` 这种写法 split_questions 认得（_NUM_START_RE），
# 这里也必须认——两边不一致的后果是「只取第 4 题」时它被算成漏题误报。
#
# `\D{0,4}` 是留给题号前的零星标点（`（`、`**`、`## ` 等）的，**不够装题型标签**：
# `[解答] ` 是 5 个字符，`[单选] ` 也是。所以标签必须先剥掉再匹配，不能靠放宽这个
# 上界来兼容——放宽到能装下标签，`解析第3步` 这类正文就会被当成题号行。
#
# `$\displaystyle N$` 前缀单列一支（2026-08-09 补）：MinerU 提成 `## ` 标题的题号
# 行里，数字被包成数学式，`\D{0,4}?` 装不下 `## $\displaystyle ` 这 16 个字符。
# 取不到题号的代价是文件名落回 uuid、且 `only_numbers` 那条路把它算成漏题上报。
# 放在裸数字那支之前试：两支都能匹配时（正文恰以数字开头）结果相同，顺序无关。
_NUM_RE = re.compile(
    r"^(?:\D{0,4}?|[^$]{0,6}?\$\\displaystyle\s*)(\d{1,3})\s*\$?\s*[.．、,，)）题]")
_CN_NUM_RE = re.compile(r"^[^一-鿿]{0,4}?第?\s*([一二三四五六七八九十百零]+)\s*[题、．.，,)）]")
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str):
    if not s:
        return None
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGIT.get(left, 1) if left else 1
        ones = _CN_DIGIT.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for ch in s:
        d = _CN_DIGIT.get(ch)
        if d is None:
            return None
        total = total * 10 + d
    return total


def block_number(block: str):
    """从题块首行提取题号（整数），支持阿拉伯与中文大写。取不到返回 None。

    块首可以带题型标签（`[解答] 第1 题 …`），本函数自己剥掉——**不要求调用方
    先剥**。逐块识别路径渲染出的块一律带标签（blockpipe._render），如果把剥标签
    的责任推给调用方，任何一处忘了剥都会静默退化成「取不到题号」，而
    `only_numbers` 那条路径上「取不到题号」是被当成漏题上报的。
    """
    first = block.lstrip()
    if first.startswith("- "):
        first = first[2:]
    lines = first.splitlines()
    first = lines[0].strip() if lines else ""
    # 前向引用 strip_type_tag：标签长什么样只在 _TYPE_TAG_RE 一处定义，别在这里再抄
    first = strip_type_tag(first).strip()
    m = _NUM_RE.match(first)
    if m:
        return int(m.group(1))
    m = _CN_NUM_RE.match(first)
    if m:
        return _cn_to_int(m.group(1))
    return None


# ── 题号剥离 ──────────────────────────────────────────────────────────────
# 下面这两条正则**刻意不复用** `_NUM_RE`/`_CN_NUM_RE`。那两条是给 block_number
# 用的：它只是「找个数字来跟 only_numbers 比对」，认宽一点最多让漏题检测多认一
# 道；这里是真的把字从题干里删掉，认宽一点就是把题干内容改错，且预览剥错就直接
# 入库（提交路径只兜底剥题型标签，见 routes/import_convert.py 的 import_do）。
# 所以口径必须更窄，具体窄在哪见各自注释。
#
# 阿拉伯数字题号：`12. ` / `12．` / `12、` / `第12题` / `## 5.` / `**12.**`，
# 以及被 `$\displaystyle $` 包起来的 `## $\displaystyle 12$．`。
# 收尾符**不含** `)` `）` `,` `，`：
#   - `)` `）` 是小问写法（`（1）求对称轴` `(2) 求通项`），解答题/填空题的块经常
#     以小问打头，认了就把小问号削掉（blocksplit 那条「`1)` 既是题号也是小步骤」
#     的注释说的是同一个坑）；
#   - `,` `，` 只在中文数字那支有意义，阿拉伯数字后跟逗号的是正文（`2，3，5 这
#     三个数的最小公倍数`）。
#
# `$\displaystyle N$` 这一支是 2026-08-09 补的，**不是可选的宽容**：MinerU 把原卷
# 题号提成 `## ` 标题行时，行内数字一律被包成数学式（`## $\displaystyle 13$．
# 【2016 北京, 18】`）。不认它的后果不是「题号没剥干净」而是**整道题在页面和 PDF
# 上都空白**：这行会原样留在正文首行，被 `filestore._split_sections` 当成用户自定义
# 分区标题摘走，题干于是成了空字符串，而题卡（app.py 的 qbody）与导出（exporter）
# 都只读题干。一次真实导入 443 题里有 321 题栽在这里。
_STRIP_NUM_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?P<em>\*\*|__|\*)?\s*(?:\$\\displaystyle\s*)?"
    r"第?\s*(?P<n>\d{1,3})\s*\$?\s*(?:[.．、]|题)")

# 中文数字题号：`一、` / `第十二题` / `第三．`。要求**带 `第` 前缀**或**以 `、`
# 收尾**，二者缺一不剥。`_CN_NUM_RE` 的收尾符含 `，`（给 block_number 认题号行
# 够用），照搬过来会把 `二，三班共有学生 60 人`剥成 `三班共有学生 60 人`、
# `十，百，千位数字之和`剥成 `百，千位数字之和`——中文数字在正文里出现的频率远
# 高于阿拉伯数字，这层限制不能省。
_STRIP_CN_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?P<em>\*\*|__|\*)?\s*"
    r"(?:第\s*[一二三四五六七八九十百零]+\s*[题、．.]"
    r"|[一二三四五六七八九十百零]+\s*、)")

# 题号后紧跟的分值标注：`1.（10分）已知…`、`2.(5 分)已知…`、`3.（本题满分12分）`，
# 也可能单独占一行（`1.\n（10分）\n已知…`）。只在「剥完题号后剩下的最前面」这一
# 个位置匹配，不在正文任意处找「N 分」——避免把题干里真正提到的分数删掉。
_SCORE_LEAD_RE = re.compile(
    r"^[（(]\s*(?:共|每题|每小题|本题|本小题|满分){0,2}\s*"
    r"\d{1,3}(?:\.\d+)?\s*分\s*[)）]\s*")


def _strip_emphasis_tail(text: str, opener: str | None) -> str:
    """剥掉题号自带的 markdown 强调**收尾**符，`**12.**` 那个句点后面的 `**`。

    只有题号前真的吃掉了一个开启符才剥——强调符是配对的，单侧剥会把配对关系搞
    反：`3.**已知**函数` 的 `**` 是正文里的加粗开头，无脑剥掉会让后面那半个 `**`
    去跟更远处的记号配对，渲染出一大段莫名加粗的文字。
    """
    if not opener or not text.startswith(opener):
        return text
    # `*` 开启、剩下是 `**bold**` 时别剥：剥一个星号只会留下不配对的 `*bold**`
    if opener == "*" and text.startswith("**"):
        return text
    return text[len(opener):].lstrip()


def strip_leading_number(stem: str) -> str:
    """剥掉题干最前面的题号，及紧随其后的分值标注，供入库前清洗题干显示用。

    题号只在导入校对阶段有用（block_number 拿它做漏题检测），题卡展示和入库正文
    都不需要。识别口径见 `_STRIP_NUM_RE`/`_STRIP_CN_RE` 上方注释——比
    block_number 窄，宁可漏剥留个题号让用户手删，也不要剥错删掉正文。
    """
    text = stem.lstrip()
    m = _STRIP_NUM_RE.match(text)
    if m:
        # 题号后紧跟另一个数字，说明这更像小数开头的题干本身（`1.5 千克的水…`
        # 剥完 "1." 剩下 "5 千克…"）。`_SECTION_TAIL_RE` 那条护栏只挡得住
        # `1.1.5` 这种 OCR 拆号残留，挡不住普通小数，这里单独判一次。
        # 题号也不会是 0，命中 `0.` 的多半是被拆开的小数。
        rest = text[m.end():]
        if int(m.group("n")) != 0 and not rest[:1].isdigit():
            text = _strip_emphasis_tail(rest.lstrip(), m.group("em"))
    else:
        m = _STRIP_CN_RE.match(text)
        if m:
            text = _strip_emphasis_tail(text[m.end():].lstrip(), m.group("em"))
    text = text.lstrip()
    text = _SCORE_LEAD_RE.sub("", text, count=1)
    # 分值标注和题号之间也可能夹着强调收尾符（`**15.（12分）** 已知数列`），剥完
    # 分值再补一次，否则会留下裸 `**`
    text = _strip_emphasis_tail(text, m.group("em") if m else None)
    return text.lstrip()


# 选项标签：$\displaystyle A.$ 或裸 A.
_OPTION_RE = re.compile(r"(\$\\displaystyle\s*)?[A-D][.．]")

# 规范化输出在每个 `- ` 块正文开头打的题型标签（见 project-alpha/templates/
# normalize_prompt.md）：`[单选]` `[多选]` `[填空]` `[解答]`。AI 依据草稿里的大题
# 标题（「二、多选题」「有多项符合」等）判定，比纯正文特征可靠——单选/多选正文无异。
_TYPE_TAG_RE = re.compile(r"^\s*[\[【]\s*(单选|多选|填空|解答)\s*[\]】]\s*")
_TAG_TO_TYPE = {"单选": "单选题", "多选": "多选题", "填空": "填空题", "解答": "解答题"}


def strip_type_tag(body: str) -> str:
    """剥掉块首题型标签 `[单选]/[多选]/[填空]/[解答]`（连同其后空白），返回干净题干。
    入库/展示正文都不应带这个标签——它只是识别用的元数据。无标签则原样返回。"""
    return _TYPE_TAG_RE.sub("", body, count=1)


# 选择题题干末尾的空括号 `（  ）`，让人填答案字母用的。是选择题最后一档兜底信号，
# 见 guess_type 里的次序说明。
_EMPTY_PAREN_RE = re.compile(r"[(（]\s*[)）]")


def guess_type(body: str) -> str:
    """判定题型：优先读块首题型标签，无标签再按正文特征粗判。

    单选题与多选题正文特征完全相同（都是 ABCD 选项），无法靠正文区分，只能靠
    规范化时 AI 打的标签或人工在校对页指定。无标签的选择题一律回退为「单选题」。

    **四档的先后次序是 2026-08-07 在 4769 道 AI 标注题上标定的，别重排**（现状
    97.6%）。三处反直觉的地方：

    ① **兜底是解答题，不是填空题**。"啥也没有 → 填空题"听着顺，实测低 10 个百分点
    （87.1%）：522 道解答题是单问的证明/求值题，压根没有 `（1）` 小问，会被整批
    判成填空。

    ② **`___` 必须排在 `（1）` 前面**。`___` 是这四个信号里最干净的一个（填空题
    1352/1426 命中，解答题只 2 例、单选 7 例误命中）；而带小问的填空题有 25 道，
    反过来排就把它们判成解答题。

    ③ **空括号排最后一档**。它专捞"选项被 MinerU 吃掉"的图片选项题——那些题只剩
    `（  ）` 和四张图，前三档全落空。放最后是因为解答题正文里也会出现空括号
    （`f(x)` 被识别坏之类），排前面会抢走真解答题；排最后净救回 28 道、不新增
    任何错判。
    """
    m = _TYPE_TAG_RE.match(body)
    if m:
        return _TAG_TO_TYPE[m.group(1)]
    # 无标签兜底：有 A./B./C./D. 选项 → 单选题（多选无从纯正文判定）
    if mechfix.has_complete_choice_options(body):
        return "单选题"
    # 含填空位 ___ → 填空题
    if "___" in body:
        return "填空题"
    # 含分小问 （1）（2） → 解答题
    if re.search(r"（\s*[1１]\s*）", body):
        return "解答题"
    # 只剩答题空括号 → 选项被 MinerU 吃成图片的选择题
    if _EMPTY_PAREN_RE.search(body):
        return "单选题"
    return "解答题"
