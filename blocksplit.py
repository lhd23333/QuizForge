"""机械切块：把 MinerU 原文按题号切成带元数据的块，供逐块 LLM 处理。

为什么要有这一层（与现有整篇规范化路径的差别）：
现路径把整份草稿一次交给 LLM 输出全部题块，**块数由模型决定** —— 它少吐一题
就是漏题，多吐一题靠 `_dedup_key` 的 80 字符指纹去猜，跨轮续传还要靠「已完成
清单」让模型自己定位。漏题/重复/漏解析/解析错配这四类问题全部源于此。

这里把块数改成在调用 LLM 之前由代码定死：切块 → 每块单独判定与规范化 →
按（组, 题号）程序化配对。配不上的能明确报出来，不再是静默错配。

切分不是一条正则能干的活（下列全部实测于 project-alpha/output/raw_md 的真实产物）：
  - 卷首「注意事项」也是 1. 2. 3.，且紧挨真题号 1. 之前；
  - `一、二、三` 在高考卷里是题型分区（阿拉伯号 1..19 跨区连续递增），在题集里
    却是大题号（其下 1. 2. 3. 是小问）—— 同一个正则两种含义，必须先判体系；
  - 分组题集每组题号从 1 重开（A 组 1-7、B 组 1-9），解析区再按同样分组重编一遍；
  - MinerU 会把一部分题号提成 `## ` 标题，行尾还可能粘 OCR 垃圾（「题号位置： 17 18」）；
  - `1.1.5 函数：…` 这种小节号、OCR 拆开的 `0. 2 = 0. 5 \\times 0. 3` 会误命中题号；
  - 题号行可能只有 `15.(13 分)`，题干正文在后续行 —— 块的起点对，但首行取不到题干。
所以走两趟：先做文档级判定（剥预导语、识别分区/分组/题干区与解析区、定编号体系），
再据此切块。本模块只做结构，不改一个字的正文排版。

题号识别分三档降级（2026-08-03 加的后两档，见 split_blocks / _split_by_markers）：
严格档只认 `12.` `12．` `12、`；整份都切不出 ≥2 块时才放宽到 `12)` `12）`
（这类编号在题集里存在，但 `1)` 也可能是解答题的小步骤，所以不能一上来就认）；
连放宽档也切不出来，说明题号被 OCR 吃了，退到按 `## 标题` 与 `【答案】` 这类
结构标记切。老实现只有严格档一档，非 `1.` 体系的文档会整份退化成一个巨块——
下游按块调 LLM，一个巨块等于回到了整篇规范化，漏题问题原封不动地回来。

三档降级管的是**同一种题号写法**的宽严，管不了「写法本身就不同」的文档：
`第 5 题` 这类题号一档都认不出，而正文里的 OCR 碎片（`√313` 拆行后像 `313.`）
反而会切出一堆垃圾块、让降级永不触发（2026-08-04 实测，见 _DIALECTS）。所以
在三档之前还有一层**题号方言**：切块前先判本文档用哪种写法，赢家独占。以后
遇到新写法只需往 _DIALECTS 加一条，不必再动切分主逻辑。
"""

import dataclasses
import re

# 中文数字转整数复用 importer 的实现，不再抄第三份（normalizer 里已有一份，
# 那份在 project-alpha 里、按约定不改）。
from importer import _cn_to_int

# 行首 markdown 标题记号：MinerU 常把题号/小标题提成 `## `，判定时先剥掉再看正文
_MD_HEAD_RE = re.compile(r"^\s*#{1,6}\s*")

# 题号行：剥掉 `#` 后形如 `12. 正文` / `12．正文` / `12、正文`。
# 允许题号前粘 1~4 个答案字母：MinerU 会把上一题的答案糊到下一题号前面
# （实测 `AD 9. 一组数据 …`），不允许的话这一题连同其整段解析会被静默丢掉。
_NUM_LINE_RE = re.compile(r"^(?:[A-DＡ-Ｄ]{1,4}\s+)?(\d{1,3})\s*[.．、]\s*(.*)$")
# 放宽档：额外认 `12)` `12）` `(12)` `第12题`。这些编号真实存在于题集，但
# `1)` `(2)` 同样是解答题小步骤的写法，混进来会把一题劈成好几块，所以只在
# 严格档切不出东西时才启用（见 split_blocks 的三档降级）。
_NUM_LINE_LOOSE_RE = re.compile(
    r"^(?:[A-DＡ-Ｄ]{1,4}\s+)?[(（]?\s*第?\s*(\d{1,3})\s*[.．、)）]\s*[题]?\s*(.*)$")
# 误命中排除：`1.1.5 函数…`（小节号）这类「数字.数字.」链，正文侧再跟一个点号数字
_VERSION_TAIL_RE = re.compile(r"^\d{1,3}\s*[.．、]")

# ── 题号方言 ────────────────────────────────────────────────────────────────
# `第 5 题` 这类题号在上面两条正则里一档都认不出：严格档没有 `第...题` 分支，
# 放宽档的 `第?\s*(\d+)\s*[.．、)）]\s*[题]?` 要求数字后**先有分隔符**才允许跟
# `题`，而实际写法是 `第1 题`（数字后直接空格接「题」）。
#
# 但只补一条正则不够。实测 AGMC 竞赛卷（测试集/2026.8-solutions-junior.pdf）：
# 严格档在这份文档上切出了 59 块 —— 全是垃圾。目录页的 LaTeX 小节号 `2.1`
# `2.2`…`5.3` 命中 25 次，正文里被 OCR 竖排拆开的公式（`3` / `√` / `5` 三行，
# 于是 `3.` `5.` 甚至 `313.`（那是 `√313`）都像题号）又命中 34 次。因为 59 ≥ 2，
# split_blocks 的三档降级**永不触发**，唯一带 `第?` 的放宽档根本没机会跑。
# 也就是说：真正的毛病不是「认不出 `第 x 题`」，而是「垃圾题号抢先占了位」。
#
# 所以做法是先判方言、再切块：全文统计每种方言的命中情况，**赢家独占**本次
# 切分，其余方言的正则完全不参与。这样正文里零散的 `3.` `313.` 不再有机会把
# 一份 `第 x 题` 体系的卷子搅成 59 块。以后再遇到新写法（`【第5题】`、`Q5.`
# 之类）只需往 _DIALECTS 里加一条，不用动切分主逻辑。
#
# 判据是**递增性优先，不是命中数**：这份文档里垃圾 `n.` 命中 34 次、真题号只有
# 25 次，光比命中数会选错。真题号在全文单调递增（1..25），OCR 碎片是乱序的，
# 所以比「最长严格递增子序列长度」——它同时惩罚乱序和稀少，一个指标够了。

# `第1 题` / `第 12 题．` / `## 第3题 求…`；可带 LaTeX 小节号前缀（`§2.1 第1 题`）
_CN_TI_RE = re.compile(
    r"^(?:§\s*[\d.]+\s*)?第\s*(\d{1,3})\s*[题題]\s*[.．、:：]?\s*(.*)$")


@dataclasses.dataclass(frozen=True)
class _Dialect:
    """一种题号写法。strict/loose 对应 split_blocks 的严格档与放宽档。

    guard_chain 控制是否套用 _VERSION_TAIL_RE 那道「链式数字」排除：它是为
    `1.1.5` `0. 2 = 0. 5` 这类**纯数字**误命中准备的，而 `第N题` 有「第」「题」
    两个汉字锚点、不会跟小节号混，套上反而会把 `第10 题. 3. 求…` 这种题干误杀。
    """

    name: str
    strict: "re.Pattern[str]"
    loose: "re.Pattern[str]"
    guard_chain: bool = True
    weight: float = 1.0


# 默认方言 = 老实现的行为。它必须排在第一位且在打平时胜出，
# 这样绝大多数 `1.` 体系的试卷走的还是原来那条路，改动零风险。
_DEFAULT_DIALECT = _Dialect("arabic-dot", _NUM_LINE_RE, _NUM_LINE_LOOSE_RE)
_DIALECTS = (
    _DEFAULT_DIALECT,
    # weight=3：`第N题` 有「第」「题」两个汉字锚点，误命中率远低于裸 `N.`——
    # 后者在数学卷里跟小数、选项、竖排拆行的根式全都撞。所以同样长的递增序列，
    # 这个方言的证据强得多。没有权重时实测短文档会选错：`√313` 拆出的
    # `5.` `313.` 碎片凑出的递增长度可以盖过真实的两道 `第 x 题`。
    _Dialect("cn-di-ti", _CN_TI_RE, _CN_TI_RE, guard_chain=False, weight=3.0),
)

# 目录页的「点导引」：`第1 题. . . . . . . . . . . 4`。目录整整占了 3 页、
# 25 行假题号，_drop_preamble 只扫前 40 行救不回来（而且它按考务特征词判，
# 目录里没有那些词）。
#
# 判据是「行尾是一长串点号，点号之后什么都没有」，两个条件都必需：
#   ① 至少 12 个点号 —— 目录导引横跨整行（实测每行 40+ 个点）；
#   ② 点号串必须**收在行尾**（页码在 PDF 里另起一行，不在这一行上）。
#
# ② 是要害。解析里的 `. .....4分` 也是一串点号 —— 那是 LaTeX 的 `……4分`
# 得分标注被 OCR 成点号 —— 但它**后面跟着得分文字**，不在行尾。第一版规则
# 只数点号（≥6）不看位置，于是匹配上了它；而 19 题的解析是一整行，整道题
# 就被静默丢掉了（61 份既有文档的逐位回归把这一处抓了出来）。
# 宁可漏剥几行目录（最多多出几个短块，下游 LLM 判类型时会认出不是题），
# 也绝不能丢正文。
_TOC_LEADER_RE = re.compile(r"[.．·。](?:\s*[.．·。]){11,}\s*$")

# 独立成行的 LaTeX 小节号：`§2.1`。它紧挨题号行出现（实测 `§2.1` 与 `第1 题`
# 各占一行），不跳过会粘在上一题末尾。只认「整行只有 § + 数字」这一种形态。
_SECTION_MARK_RE = re.compile(r"^§\s*[\d.]+\s*$")

# 分区标题：`一、单选题…` `第二部分 …`（中文数字 + 顿号/点号）
_SEC_LINE_RE = re.compile(r"^(?:第\s*)?([一二三四五六七八九十]+)\s*[、．.]\s*(.*)$")
# 分区标题的正文特征：没有 `##` 记号时靠这些词确认它是标题而不是句子
_SEC_KEYWORD_RE = re.compile(
    r"(选择题|多选|单选|填空题|解答题|计算题|证明题|应用题|本题共|本大题|部分)")

# 分组标题：`A 组` `B组` `一组`（整行只有这个）
_GRP_LINE_RE = re.compile(r"^([A-DＡ-Ｄa-d]|[一二三四五六七八九十]+)\s*组\s*$")

# 解析区起始标题：`参考答案` `答案与解析` 等，整行短（长句里出现同样的词不算）
_ANS_LINE_RE = re.compile(
    r"^(参考答案(与解析)?|答案(与|及)?解析|参考解答|答案解析|解析|答案)\s*[:：]?\s*$")

# 块内解析标记：`【答案】` `【解析】`，可带题号前缀（`## 3．【答案】 C`）
_SOL_MARK_RE = re.compile(r"^(?:\d{1,3}\s*[.．、]\s*)?(【答案】|【解析】|答案[:：]|解析[:：])")

# 卷首「注意事项」特征词：命中即整段丢弃（它的 1. 2. 3. 会被误当成题号）
_PREAMBLE_KEYWORD_RE = re.compile(
    r"(答题卡|准考证号|考试结束|铅笔|涂黑|橡皮|交回|本试卷共|考试时间|草稿纸|非选择题)")


@dataclasses.dataclass
class Block:
    """一个切出来的块。正文未做任何排版改动，交给下游逐块规范化。

    number/section/group 是配对用的坐标：同一 (group, number) 的题块与解析块
    才允许配成一对。zone 区分题干区与解析区（同一份文档里题号会重来一遍）。
    """

    index: int              # 全局顺序，从 0 起
    number: int | None      # 块自身标注的题号，取不到为 None
    text: str               # 块正文（含题号行，原样）
    section: str | None     # 所属分区标题原文（`一、单选题…`），用于判 单选/多选
    group: str | None       # 所属分组标签（`A` `B`），无分组为 None
    zone: str               # "stem" 题干区 / "solution" 解析区
    line_no: int            # 块首行在原文中的行号（1 起），便于复盘
    kind: str               # 预分类：solution=纯解析（廉价规则命中）/ unknown=待 LLM 判

    def head(self, n: int = 60) -> str:
        """首行摘要，日志与人工核对用。"""
        first = self.text.strip().splitlines()
        return (first[0].strip()[:n] if first else "")


@dataclasses.dataclass
class PairResult:
    """配对结果。paired 是最终产物，其余三项是**给用户看的账**。

    现路径的致命缺陷是错配不可检——组别小标题在输出里被丢掉，谁也不知道 A 组
    第 1 题配的是不是 A 组第 1 题的解析。这里把配不上的都留在账上明确报出来，
    宁可让用户看到「第 3 题没找到解析」，也不要静默塞一段别人的解析进去。
    """

    paired: list[tuple[Block, Block | None]]  # (题块, 解析块或 None)
    orphan_solutions: list[Block]             # 找不到对应题目的解析块
    missing_numbers: list[int]                # 题干区取不到题号的块数（诊断用）
    conflicts: list[str]                      # 同坐标撞车等异常，人读的说明


def pair_blocks(blocks: list[Block]) -> PairResult:
    """按 (group, number) 把解析块配到题块上。

    配对只认坐标，不做任何文本相似度猜测——坐标是原文自带的事实，猜测会引入
    新的错配。同坐标有多个解析块时按出现顺序取用，多出来的记进 conflicts。

    题块自带解析（题目+解析混合块，`kind == "mixed"` 或块内已有解析标记）不在
    这里处理：它的解析就在自己块内，配 None 即可，由下游按块内标记切开。
    """
    stems = [b for b in blocks if b.zone == "stem"]
    sols = [b for b in blocks if b.zone == "solution"]

    # 按坐标建索引；同坐标可能有多块（OCR 把一题解析拆成两段），按顺序排队
    bucket: dict[tuple[str | None, int | None], list[Block]] = {}
    for s in sols:
        bucket.setdefault((s.group, s.number), []).append(s)

    paired: list[tuple[Block, Block | None]] = []
    conflicts: list[str] = []
    used: set[int] = set()
    for st in stems:
        key = (st.group, st.number)
        queue = bucket.get(key) or []
        hit = next((x for x in queue if x.index not in used), None)
        if hit is not None:
            used.add(hit.index)
        paired.append((st, hit))

    orphans = [s for s in sols if s.index not in used]
    for s in orphans:
        conflicts.append(
            f"解析块（组 {s.group or '-'} 第 {s.number or '?'} 题, 原文第 "
            f"{s.line_no} 行）找不到对应题目")
    missing = [st.index for st in stems if st.number is None]
    if missing:
        conflicts.append(f"有 {len(missing)} 个题块取不到题号，无法参与配对")
    return PairResult(paired=paired, orphan_solutions=orphans,
                      missing_numbers=missing, conflicts=conflicts)


def _strip_head(line: str) -> str:
    """剥掉行首 markdown 标题记号，返回正文部分。"""
    return _MD_HEAD_RE.sub("", line).strip()


def _parse_number(body: str, loose: bool = False,
                  dialect: _Dialect | None = None) -> tuple[int | None, str]:
    """从已剥 `#` 的行首取题号，返回 (题号, 题号之后的正文)。取不到返回 (None, body)。

    排除两类实测误命中：
      - `1.1.5 函数：…` 小节号：题号后紧跟又一个「数字.」；
      - `0. 2 = 0. 5 \\times …`：OCR 把 `0.2` 拆开，题号为 0 或后接纯数字算式。

    loose=True 时额外认 `12)` `（12）` 这类编号（只由 split_blocks 的第二档降级
    传入，理由见该函数）。

    dialect 决定用哪套题号正则，None 为默认的 `1.` 体系（老行为）。方言由
    _detect_dialect 在切块前定死，切块过程中不再换——见 _DIALECTS 注释。
    """
    d = dialect or _DEFAULT_DIALECT
    m = (d.loose if loose else d.strict).match(body)
    if not m:
        return None, body
    num, rest = int(m.group(1)), m.group(2).strip()
    if num == 0:
        return None, body                      # 题号不会是 0
    if d.guard_chain and _VERSION_TAIL_RE.match(rest):
        return None, body                      # `1.1.5` / `0. 2 = 0. 5` 这类链式数字
    return num, rest


def _lis_len(nums: list[int]) -> int:
    """最长严格递增子序列长度（O(n²) 够用，题号最多几百个）。

    用它衡量一串候选题号「像不像题号」：真题号在全文单调递增，OCR 把公式拆行
    造出来的假题号是乱序的。长度而非比例，是为了同时惩罚乱序与稀少——某方言
    只命中 2 行且递增，不该赢过命中 25 行且全程递增的那个。
    """
    if not nums:
        return 0
    best = [1] * len(nums)
    for i in range(1, len(nums)):
        for j in range(i):
            if nums[j] < nums[i] and best[j] + 1 > best[i]:
                best[i] = best[j] + 1
    return max(best)


def _detect_dialect(lines: list[str], start: int) -> _Dialect:
    """定本文档的题号方言：候选题号最长递增子序列最长者胜，打平取默认方言。

    为什么必须先判、且赢家独占：见 _DIALECTS 上方注释（实测一份 `第 x 题` 卷子
    被正文里的 OCR 碎片按 `1.` 体系切成 59 个垃圾块，且因为 59 ≥ 2 而永不降级）。

    统计时跳过目录点导引行 —— 目录里 `第1 题. . . . . 4` 有 25 行，它们的题号
    同样递增，两个方言都会被它拉高，留着等于让噪声参与投票。
    """
    scores: list[tuple[float, int, _Dialect]] = []
    for k, d in enumerate(_DIALECTS):
        nums: list[int] = []
        for line in lines[start:]:
            body = _strip_head(line)
            if not body or _TOC_LEADER_RE.search(body):
                continue
            num, _ = _parse_number(body, dialect=d)
            if num is not None:
                nums.append(num)
        run = _lis_len(nums)
        # 只有真的认出了题号才给权重加成：run < 2 说明这方言在本文档里没站住脚，
        # 加成会让一次偶然命中（正文里提一句「第 3 题」）压过成体系的 `1.` 题号。
        score = run * d.weight if run >= 2 else float(run)
        # -k 让打平时靠前的方言（默认方言在第 0 位）胜出
        scores.append((score, -k, d))
    scores.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scores[0][2]


def _drop_preamble(lines: list[str]) -> int:
    """返回正文起始下标，跳过卷首「注意事项」。

    注意事项本身就是 1. 2. 3. 编号、且紧挨真正的题号 1. 之前（实测高考卷），
    不剥掉就会凭空多出 3 道「题」并与真题号撞车。判据：在文档前部出现特征词
    （答题卡/准考证号/…）的编号行，一律跳过；遇到第一个分区标题或不含特征词的
    题号行即停。只扫前 40 行，避免误伤正文。
    """
    limit = min(len(lines), 40)
    start = 0
    for i in range(limit):
        body = _strip_head(lines[i])
        if not body:
            continue
        if _SEC_LINE_RE.match(body) and _SEC_KEYWORD_RE.search(body):
            return i                            # 分区标题到了，正文从这里开始
        num, rest = _parse_number(body)
        if num is None:
            continue
        if _PREAMBLE_KEYWORD_RE.search(rest):
            start = i + 1                       # 这是注意事项条目，正文至少从下一行起
        else:
            return start                        # 干净的题号行，停止剥离
    return start


def _detect_scheme(lines: list[str], start: int,
                   dialect: _Dialect | None = None) -> str:
    """判定大题号体系："arabic"（阿拉伯号是大题号）或 "chinese"（中文号是大题号）。

    判据是「谁在全文单调递增且覆盖面广」：
      - 高考卷：`一、二、三、四` 是题型分区，阿拉伯 1..19 跨区连续 → arabic；
      - 题集：`一、二、三` 是大题号，其下 1. 2. 3. 只是小问、每题内都从 1 重开 → chinese。
    实测同一个正则在这两类文档里含义相反，所以必须先判、不能写死。

    做法：数中文号行里带题型关键词的比例。带关键词（「单选题」「本题共」）说明它是
    分区标题而非大题号 → arabic。否则若中文号数量可观、且阿拉伯号明显多次重复
    （每个大题下都从 1 重开）→ chinese。
    """
    cn_total = 0
    cn_with_kw = 0
    arabic_nums: list[int] = []
    for line in lines[start:]:
        body = _strip_head(line)
        if not body:
            continue
        m = _SEC_LINE_RE.match(body)
        if m:
            cn_total += 1
            if _SEC_KEYWORD_RE.search(body):
                cn_with_kw += 1
            continue
        num, _ = _parse_number(body, dialect=dialect)
        if num is not None:
            arabic_nums.append(num)
    if cn_total == 0:
        return "arabic"
    if cn_with_kw * 2 >= cn_total:
        return "arabic"      # 中文号多为「一、单选题…」这类分区标题
    # 中文号不带题型关键词：若阿拉伯号反复从小数字重开，它们更像小问
    restarts = sum(1 for a, b in zip(arabic_nums, arabic_nums[1:]) if b <= a)
    if arabic_nums and restarts >= max(2, cn_total // 2):
        return "chinese"
    return "arabic"


def _classify(text: str) -> str:
    """廉价预分类：明显是纯解析的块直接定性，省一次 LLM 调用。

    判据取「块首就是答案标记」——解析区的块形如 `3．【答案】 C`（实测 1.1.5 的
    解析区 9/9 命中）。只认块首，不认块内出现：题干后紧跟解析的混合块必须交给
    LLM 判，不能在这里草率切开。其余一律 unknown。
    """
    first = _strip_head(text.strip().splitlines()[0]) if text.strip() else ""
    if _SOL_MARK_RE.match(first):
        return "solution"
    return "unknown"


def _split_pass(raw_md: str, loose: bool) -> list[Block]:
    """按题号切一趟。loose 控制题号正则的宽严（见 _parse_number）。

    两趟：先 _drop_preamble + _detect_scheme 做文档级判定，再按判定出的大题号
    体系逐行切块，沿途维护 分区标题 / 分组 / 区（题干区还是解析区）三个上下文。

    区（题干区/解析区）不在切块时判，留给 _assign_zones 后处理——切块时只看得到
    局部一行，判不准；切完拿到全序列反而简单可靠。
    """
    lines = raw_md.splitlines()
    start = _drop_preamble(lines)
    dialect = _detect_dialect(lines, start)
    scheme = _detect_scheme(lines, start, dialect)

    blocks: list[Block] = []
    cur_lines: list[str] = []
    cur_num: int | None = None
    cur_line_no = 0
    section: str | None = None
    group: str | None = None
    zone = "stem"        # 切块阶段一律记 stem，真正的区归属由 _assign_zones 定
    saw_ans_head = False  # 是否已过「参考答案」标题行（最强信号，直接定区）

    def flush():
        """把累积的行收成一个块。"""
        if not cur_lines:
            return
        text = "\n".join(cur_lines).strip()
        if not text:
            return
        blocks.append(Block(
            index=len(blocks), number=cur_num, text=text, section=section,
            group=group, zone=zone, line_no=cur_line_no, kind=_classify(text),
        ))

    for i in range(start, len(lines)):
        raw_line = lines[i]
        body = _strip_head(raw_line)
        if not body:
            if cur_lines:
                cur_lines.append(raw_line)
            continue

        # 目录点导引行（`第1 题. . . . . . 4`）：整行丢弃，不进块也不切块。
        # 这份卷子的目录占 3 页、25 行假题号，不丢会切出 25 个只含页码的空块。
        if _TOC_LEADER_RE.search(body):
            continue

        # 独立成行的 LaTeX 小节号（`§2.1`）：紧挨真题号行，丢弃避免粘到上一题末尾
        if _SECTION_MARK_RE.match(body):
            continue

        # 分组标题：`A 组`。不进任何块，只切换上下文
        m = _GRP_LINE_RE.match(body)
        if m:
            flush()
            cur_lines, cur_num = [], None
            group = m.group(1).strip()
            continue

        # 解析区起始标题：`参考答案与解析`。这是最强信号，之后的块全归解析区
        if _ANS_LINE_RE.match(body):
            flush()
            cur_lines, cur_num = [], None
            saw_ans_head = True
            zone = "solution"
            group = None
            continue

        # 分区标题：`一、单选题…`。arabic 体系下它只是上下文（题型依据）；
        # chinese 体系下它本身就是大题起点。
        m = _SEC_LINE_RE.match(body)
        if m and (_SEC_KEYWORD_RE.search(body) or scheme == "chinese"):
            flush()
            cur_lines, cur_num = [], None
            section = body
            if scheme == "chinese":
                cur_lines = [raw_line]
                cur_num = _cn_to_int(m.group(1))
                cur_line_no = i + 1
            continue

        # 题号行：arabic 体系下是新块起点；chinese 体系下是小问，留在当前块内
        num, _rest = _parse_number(body, loose=loose, dialect=dialect)
        if num is not None and scheme == "arabic":
            flush()
            cur_lines = [raw_line]
            cur_num = num
            cur_line_no = i + 1
            continue

        if cur_lines:
            cur_lines.append(raw_line)
        # 首个题号之前的零散行（小节标题等）直接丢弃，不凭空造块

    flush()
    blocks = _drop_boilerplate(blocks)
    _assign_zones(blocks, saw_ans_head)
    return blocks


def split_blocks(raw_md: str) -> list[Block]:
    """把 MinerU 原文切成带元数据的块列表。不修改正文，只做结构。

    三档降级取第一个切出 ≥2 块的结果：
      ① 严格题号（`12.` `12．` `12、`）——绝大多数试卷走这条，行为与老实现一致；
      ② 放宽题号（额外认 `12)` `（12）` `第12题`）；
      ③ 结构标记（`## 标题` / `【答案】` 行）——题号被 OCR 吃干净时的最后一招。
    为什么要降级而不是一开始就用宽正则：`1)` `(2)` 同样是解答题小步骤的写法，
    宽正则在正常试卷上会把一题劈成好几块，代价比"某些文档切不开"更大。反过来，
    严格档已经切出多块就说明这份文档的题号体系认出来了，不必再赌。

    为什么"切不开"是个必须修的问题：下游 blocknorm 按块调 LLM，整份退化成一个
    巨块等于回到整篇规范化，漏题/错配这些本模块要解决的问题会原封不动地回来。
    """
    if not raw_md or not raw_md.strip():
        return []

    blocks = _split_pass(raw_md, loose=False)
    if len(blocks) >= 2:
        return blocks
    loose = _split_pass(raw_md, loose=True)
    if len(loose) >= 2:
        return loose
    marker = _split_by_markers(raw_md)
    if len(marker) >= 2:
        return marker
    return blocks or loose or marker


def _split_by_markers(raw_md: str) -> list[Block]:
    """题号完全取不到时，按结构标记切块（第三档降级）。

    能用的标记只剩两类，都不依赖题号：
      - `## ` 级 markdown 标题：MinerU 会把每道题的首行提成标题；
      - `【答案】`/`【解析】` 行：解析区每条的起点。
    切出的块 number 一律为 None，配对阶段配不上会如实记进 conflicts——这正是
    本模块的设计意图（宁可报"第 N 题没找到解析"，不要静默错配）。分区/分组
    上下文在这一档不维护：既然连题号都没了，那些更弱的信号也不可信。
    """
    lines = raw_md.splitlines()
    start = _drop_preamble(lines)
    starts: list[int] = []
    for i in range(start, len(lines)):
        line = lines[i]
        body = _strip_head(line)
        if not body:
            continue
        if _MD_HEAD_RE.match(line) or _SOL_MARK_RE.match(body):
            starts.append(i)
    if not starts:
        return []

    blocks: list[Block] = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(lines)
        text = "\n".join(lines[s:e]).strip()
        if not text:
            continue
        blocks.append(Block(
            index=len(blocks), number=None, text=text, section=None,
            group=None, zone="stem", line_no=s + 1, kind=_classify(text),
        ))
    blocks = _drop_boilerplate(blocks)
    _assign_zones(blocks, False)
    return blocks


def _drop_boilerplate(blocks: list[Block]) -> list[Block]:
    """丢掉「注意事项」类块并重排 index。

    _drop_preamble 只扫开头 40 行，救不了文档中段又出现一遍的情形（实测一份 md
    把卷子与答案拼在一起，第 104 行起又是一整段注意事项）。这里按内容判：短块
    且命中考务特征词（答题卡/准考证号/…）即丢。要求短（< 120 字）以免误伤真题——
    真题正文里偶尔也提「答题卡」，但不会通篇只有这一句。
    """
    kept: list[Block] = []
    for b in blocks:
        body = b.text.strip()
        if len(body) < 120 and _PREAMBLE_KEYWORD_RE.search(body):
            continue
        kept.append(b)
    for i, b in enumerate(kept):
        b.index = i
    return kept


def _assign_zones(blocks: list[Block], saw_ans_head: bool) -> None:
    """原地判定每块属于题干区还是解析区。

    切块时只看得到一行，判不准；这里拿到全序列，用两个独立信号定界：
      ① 显式「参考答案」标题行已由 _split_pass 记在 zone 上，最可信，直接采信；
      ② 无该标题时，找**分界点**：第一个「此后 solution 块占绝对多数」的位置。
         实测两种形态都靠它救回来——1.1.5 是 A/B 组题干后再跟 A/B 组答案（题号
         按组重开，单看回退判不了），另一类是题干区 1..19 之后解析区又 1..19.
    分界点之后的块一律记 solution，即使个别块 kind 是 unknown（解析首行未必带
    【答案】），这是「区」比「块特征」更可靠的地方。
    """
    if not blocks:
        return
    if saw_ans_head:
        _undo_all_solution(blocks)
        return                      # 已有显式标题，_split_pass 里分好了

    n = len(blocks)
    is_sol = [b.kind == "solution" for b in blocks]
    total_sol = sum(is_sol)
    if total_sol == 0:
        # 没有任何 `【答案】` 标记时，退到题号回退信号：同一份文档里题干区
        # 1..19 走完，解析区又从 1 重来（实测 MinerU 把卷子与答案合在一份 md）。
        # 只认「回到 1 且此前已到过较大题号」，并要求回退后的块数够多，避免把
        # 偶发的乱序题号误判成整区。分组切换本身也会让题号回到 1，故带组的跳过。
        cut = _find_restart(blocks)
        if cut is not None:
            for b in blocks[cut:]:
                b.zone = "solution"
        return

    # 选切点 c：最大化「c 之前的非解析块 + c 之后的解析块」，即最干净的二分点。
    # 要求切点后确实有解析块占多数，否则宁可不切（避免把零散答案行误判成整区）。
    best_c, best_score = 0, -1
    prefix_sol = 0
    for c in range(n + 1):
        after_sol = total_sol - prefix_sol
        score = (c - prefix_sol) + after_sol
        if score > best_score:
            best_score, best_c = score, c
        if c < n and is_sol[c]:
            prefix_sol += 1

    tail = blocks[best_c:]
    if not tail or sum(1 for b in tail if b.kind == "solution") * 2 < len(tail):
        return                      # 切点之后解析块不过半，不成区，放弃
    for b in tail:
        b.zone = "solution"
    _undo_all_solution(blocks)      # 切点落在 0 处会清空题干区，同样不成立


def _undo_all_solution(blocks: list[Block]) -> None:
    """整份都被判成解析区时，把 zone 全部掰回 stem。

    zone 模型假定「解析区在文档末尾」，靠 `参考答案` 标题定界。但实测这份
    AGMC 竞赛卷（测试集/2026.8-solutions-junior.pdf）把一张答案速查表放在
    第 4 页、标题正是「参考答案」，于是 saw_ans_head 在**正文之前**就置了位，
    此后 25 道题全归解析区。

    后果是致命的：blockpipe 只渲染 pair_blocks 的 paired（题块列表），题干区
    为空 → 25 道题全成孤儿解析 → 整份文档输出空字符串。

    判据取「一道题块都不剩」——这不可能是真的文档结构（解析区总得有题目与之
    配对），只可能是定界定错了。掰回 stem 后，题干与解析仍在同一块内，由
    blocknorm 逐块问 LLM「这块是题目/解析/两者都有」来切开，那条路已经存在
    且比位置启发式可靠。不改 kind：kind 是块自身特征，没判错。
    """
    if all(b.zone == "solution" for b in blocks):
        for b in blocks:
            b.zone = "stem"


def _find_restart(blocks: list[Block]) -> int | None:
    """找题号「回到 1」的位置作为解析区起点；找不到返回 None。

    要求回退处此前已见过 >= 5 的题号（否则可能只是短小节重新编号），且回退后
    至少还有 3 块。带分组标签的块跳过——分组本身就会让题号从 1 重开。
    """
    seen_max = 0
    for i, b in enumerate(blocks):
        if b.number is None or b.group is not None:
            continue
        if b.number == 1 and seen_max >= 5 and (len(blocks) - i) >= 3:
            return i
        seen_max = max(seen_max, b.number)
    return None
