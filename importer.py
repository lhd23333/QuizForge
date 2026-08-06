"""规范化 Markdown 题目导入。

主输入是 project-alpha 规范化输出：每题一个顶层 `- ` 无序列表项，题间空行
（切分算法原样沿用 project-alpha/src/validator.py:_split_by_question）。

但导入页的文本框和 .md 上传是**开放输入**：用户经常直接粘一份手写/网上抄来的
题目，那种文本没有 `- `，只有 `1. 2. 3.` 题号。老实现只认 `- `，这类输入会被
整份当成"一道题"，校对页只出一张巨大的题卡——功能上等于不可用。所以切分改成
两种模式（见 split_questions）：契约格式走 `- `，其余走题号。
"""

import re

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


def split_solution(block: str) -> tuple[str, str | None]:
    """把一题块按 `【解析】` 标记切成 (题干, 解析)。

    规范化输出里解析紧跟题干、在同一个 `- ` 块内、独占一行、以 `【解析】` 开头
    （见 project-alpha/templates/normalize_prompt.md）。找到首个这样的行，
    之前为题干、之后（含该行）为解析。无标记则解析为 None。
    """
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("【解析】"):
            stem = "\n".join(lines[:i]).strip()
            solution = "\n".join(lines[i:]).strip()
            return stem, (solution or None)
    return block.strip(), None


# 题号提取：与 project-alpha/src/normalizer.py 的 _block_number 保持一致，
# 支持阿拉伯数字与中文大写题号。`_cn_to_int` 还被 blocksplit.py 复用（判定
# chinese 编号体系下的大题号），不要在别处再抄一份。
# 尾部允许 `题`：`第4题 已知…` 这种写法 split_questions 认得（_NUM_START_RE），
# 这里也必须认——两边不一致的后果是「只取第 4 题」时它被算成漏题误报。
#
# `\D{0,4}` 是留给题号前的零星标点（`（`、`**`、`## ` 等）的，**不够装题型标签**：
# `[解答] ` 是 5 个字符，`[单选] ` 也是。所以标签必须先剥掉再匹配，不能靠放宽这个
# 上界来兼容——放宽到能装下标签，`解析第3步` 这类正文就会被当成题号行。
_NUM_RE = re.compile(r"^\D{0,4}?(\d{1,3})\s*[.．、,，)）题]")
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


# 选项标签：$\displaystyle A.$ 或裸 A.
_OPTION_RE = re.compile(r"(\$\\displaystyle\s*)?[A-D][.．]")

# 逐块识别路径（blockpipe.py）在每个 `- ` 块正文开头打的题型标签：
# `[单选]` `[多选]` `[填空]` `[解答]`。AI 依据草稿里的大题标题（「二、多选题」
# 「有多项符合」等）判定，比纯正文特征可靠——单选/多选正文无异。
_TYPE_TAG_RE = re.compile(r"^\s*[\[【]\s*(单选|多选|填空|解答)\s*[\]】]\s*")
_TAG_TO_TYPE = {"单选": "单选题", "多选": "多选题", "填空": "填空题", "解答": "解答题"}


def strip_type_tag(body: str) -> str:
    """剥掉块首题型标签 `[单选]/[多选]/[填空]/[解答]`（连同其后空白），返回干净题干。
    入库/展示正文都不应带这个标签——它只是识别用的元数据。无标签则原样返回。"""
    return _TYPE_TAG_RE.sub("", body, count=1)


def guess_type(body: str) -> str:
    """判定题型：优先读块首题型标签，无标签再按正文特征粗判。

    单选题与多选题正文特征完全相同（都是 ABCD 选项），无法靠正文区分，只能靠
    识别时 AI 打的标签或人工在校对页指定。无标签的选择题一律回退为「单选题」。
    """
    m = _TYPE_TAG_RE.match(body)
    if m:
        return _TAG_TO_TYPE[m.group(1)]
    # 无标签兜底：有 A./B./C./D. 选项 → 单选题（多选无从纯正文判定）
    labels = set(re.findall(r"[A-D][.．]", body))
    if {"A.", "A．"} & labels and len({l[0] for l in labels}) >= 3:
        return "单选题"
    # 含填空位 ___ → 填空题
    if "___" in body:
        return "填空题"
    # 含分小问 （1）（2） → 解答题
    if re.search(r"（\s*[1１]\s*）", body):
        return "解答题"
    return "解答题"
