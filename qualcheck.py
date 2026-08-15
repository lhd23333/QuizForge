"""切块结果的质量体检：只报告、不修补。

和 `optcheck` 同一个家族，分工不同：
  - `optcheck` 看 **MinerU 原文**，查「选项标签在、内容没了」（识别阶段的丢失）；
  - 本模块看 **切完块之后的结构**，查「块的形状不对」（切分阶段的漏切/错判）。

为什么值得单独做这一层：现有的两个信号灵敏度都不够。`_drop_note` 的阈值是丢字
占比 25%，按「整份被吃掉」标定的（正常文档最高 8.96%，两次事故 79% / 86%）；
漏掉两道题只占 5%，页面上一个字都不会说。`pair_blocks` 的账目只报「配不上」，
而漏切的那道题根本没进过账——它被并进了上一块，账面上完全正常。

所以这里的每一条都是**结构自洽性**检查：拿文档自己的其它部分当参照，不需要
外部真值，也不花任何 API 额度。

一贯的态度（CLAUDE.md「报告不修补」）：丢了的内容补不回来，猜比留一个明显的洞
更糟。所有函数只返回给用户看的话，绝不改 blocks。

标定（2026-08，41 份留档语料，`_qc_sweep` 那轮）
------------------------------------------------
五条检查全部逐份核过真值，**每一条初版都误报得离谱**，收紧后的成绩：

    检查项            初版命中   收紧后   真值
    题号空洞            3 份      3 份    3/3 真漏（OCR 吃掉题号后的点）
    声明数不符          —         3 份    3/3 真漏（与上同 3 份，另发现末尾漏题）
    超长块              7 份      0 份    7 份全是误报（两解法并列的正常题）
    选项不足四项        7 份      1 份    6 份误报（MinerU 吃掉标签后的点）
    题干区带解析标记    7 份      1 份    6 份误报（题解同页的正常版式）

41 份里 28 份一条都不报。这个「大部分文档安静」的比例是这一层能用的前提：
一个天天响的警报等于没有警报，真出事那次也会被一起无视。所以每条检查的注释里
都写着它当初为什么误报——改阈值前先读那段，别凭「更严谨」的直觉放宽。
"""

import logging
import re
import statistics

import mechfix
import optcheck

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 阈值与正则。**全部在 2026-08 的 41 份留档语料上逐条标定过**，每条都核到了真值
# （见各函数注释）。口径是「宁可漏报也不要天天报」——天天报的提示等于没有提示，
# 用户学会无视它之后，真出事的那次也一样被无视。改任一条前先跑
# `tools/eval_split.py` 之外的语料抽查，别凭「更严谨」直觉放宽。
# ---------------------------------------------------------------------------

# 超长块判定：非空白字符数 > 同分区中位数 × 这个倍数。
# 4.0 是拿留档语料标定的：正常试卷里最长的解答题相对中位数一般在 2~3 倍
# （解答题正文本来就比选择题长好几倍），4 倍以上才更像「两道题并进了一块」。
_LONG_BLOCK_RATIO = 4.0

# 少于这么多个题块就不做超长判定：样本太少，中位数本身不可靠（3 个块的中位数
# 就是中间那个，随便一道解答题都能让另外两个"超长"）。
_MIN_BLOCKS_FOR_OUTLIER = 6

# 选项数异常只在「分区标题明说是选择题」时判。没有分区信息就不判：填空题正文里
# 引用 `A. ` 之类的情况太多，无据地数标签必然误报。
_CHOICE_SECTION_RE = re.compile(r"单选|多选|选择题|不定项")

# 解析标记出现在题干区。这几个是解析的强标记（`解：`/`证明：` 不算——解答题的
# 题干里「求证：」很常见，`证明` 二字本身不足以判定）。
_SOL_MARK_RE = re.compile(r"【答案】|【解析】|【详解】")

# 扫描背面透字被 OCR 当正文时，数学区常堆出一串互不相关的希腊量与偏导命令。
# 单个 \partial / \xi 完全可能是正常高数题，不能见到就报；同一题块累计至少 5 个
# 这类低频命令才提示。该检查只报告、不删除，避免机械层凭语义猜测正文。
_OCR_NOISE_MATH_RE = re.compile(
    r"\\(?:partial|xi|zeta|delta|Omega|dot|scriptsize)\b")
_OCR_NOISE_MATH_MIN = 5
_OCR_LAYOUT_NOISE_RE = re.compile(r"\\scriptsize\b")
_OCR_LAYOUT_NOISE_MIN = 2
_OCR_SCRIPT_SCRIPT_RE = re.compile(r"\\scriptscriptstyle\b")


def has_dense_ocr_math_noise(text: str) -> bool:
    """文本是否达到高置信 OCR 数学噪声阈值，识别重试与质量报告共用。"""
    noise_count = len(_OCR_NOISE_MATH_RE.findall(text))
    layout_count = len(_OCR_LAYOUT_NOISE_RE.findall(text))
    return (noise_count >= _OCR_NOISE_MATH_MIN
            or layout_count >= _OCR_LAYOUT_NOISE_MIN
            # 2019 全国 III 的文本层只有一个 ``scriptsize``，但同时重复抽出两个
            # ``\dot{\mathcal H}`` 字形。单独看都未达旧阈值，组合后已不可能是
            # 正常高中题面；混合阈值避免为偶发单个字号命令无谓重跑。
            or (layout_count >= 1 and noise_count >= 3)
            or _OCR_SCRIPT_SCRIPT_RE.search(text) is not None)

# 宽口径选项标签：分隔符可有可无，只要 `A`~`D` 后面接公式/汉字/数字/符号就算见到。
# **只用来给 `optcheck._LABEL_RE` 兜底、绝不单独使用**——它宽到会把解析散文里的
# 字母也算进去，单用必然漏报。两者取并集的理由见 check_option_count。
_LOOSE_LABEL_RE = re.compile(
    # 强制 OCR 会把标签识别成 ``(A)``，若不吃掉右括号，前瞻看到 ``)`` 就把
    # A/B/D 全漏掉，四项俱全仍会误报“只见 C”。该宽正则仍只在已确认的选择题
    # 分区里、与严格标签结果合并使用，不参与空内容检测。
    r"(?<![A-Za-z\\])(?:[（(]\s*)?([A-D])\s*(?:[)）]|[.．])?\s*"
    # ``(A)\n\n![](images/a.jpg)`` 是纯图片选项；叹号同样是实质内容起点。
    r"(?=[$[!（(一-鿿\-+±\d\\])")

# 分区标题自己声明的小题数：`本题共 8 小题` / `本题共八小题`。
_COUNT_RE = re.compile(r"共\s*(\d{1,2})\s*小题")
_CN_COUNT_RE = re.compile(r"共\s*([一二三四五六七八九十]{1,3})\s*小题")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _nonspace(s: str) -> int:
    # 超长块比较的是用户能看到的正文，不是 Markdown/HTML 载荷长度。表格的
    # ``<tr><td>`` 和图片哈希会把正常图表题放大数倍，2020 全国Ⅰ卷第 19 题
    # 就因此被误报成“吞并相邻题”。
    visible = re.sub(r"<[^>]+>", "", s)
    visible = re.sub(r"!?\[\[[^\]]+\]\]|!\[[^\]]*\]\([^)]*\)", "图", visible)
    return len("".join(visible.split()))


def _by_section(blocks) -> dict:
    """题干块按分区标题分桶，保持出现顺序。

    「同一份文档内比较」这个思路要落到分区上才成立：一份卷子里选择题和解答题的
    篇幅差一个量级，混在一起取中位数，解答题必然集体"超长"（实测 39070924 的
    中位数 651 字就是这么被拉出来的，而它最短的两块其实是评分说明）。
    """
    out: dict = {}
    for b in blocks:
        if b.zone == "stem":
            out.setdefault(b.section or "", []).append(b)
    return out


def check_number_gaps(gaps) -> str:
    """题号空洞（PairResult.number_gaps）→ 给用户的一句话。

    这是本模块里最该信的一条：题号序列是原文自带的事实，不是启发式。2026-08 留档
    语料里 41 份有 3 份报洞，逐份查过全是真漏——OCR 把题号后面的点吃掉了
    （`6 $\\mathrm{i}+...`），那道题就被并进上一块，块数、覆盖率全都正常。
    """
    if not gaps:
        return ""
    shown = "、".join(str(n) for n in gaps[:10])
    tail = f" 等 {len(gaps)} 道" if len(gaps) > 10 else ""
    return (f"题号不连续，缺第 {shown}{tail} 题。"
            "常见原因是原文题号后的点被识别丢了，那道题被并进了上一题——"
            "请对照原卷检查这几道；也可能原卷本来就没有这些题号。")


def check_declared_count(blocks) -> str:
    """分区标题声明的小题数 ≠ 该分区实际切出的题块数。

    **本模块里第二可信的一条**（仅次于题号空洞），因为判据同样是原文自带的事实
    而非启发式：`一、选择题：本题共 8 小题` 是卷子自己写的，只切出 7 块就是少了
    一道，不需要任何阈值。

    标定（41 份语料，29 份至少有一个分区声明了小题数）：命中 3 份，正是题号空洞
    也报的那 3 份（48bbfc03 / 93242003 / d87d8c57，已逐份核过是真漏）；另外 26 份
    声明了数量的卷子**一份都不报**。

    与 check_number_gaps 不重复的价值：题号空洞只能发现**夹在中间**的缺号
    （靠 `a+1..b-1` 推），分区**末尾**那道题漏了序列照样连续，只有声明数能看出来。
    d87d8c57 的「解答题共 5 小题、实际 4 块」就是这一类，空洞检测对它无能为力。
    """
    bad = []
    for sec, bl in _by_section(blocks).items():
        want = _declared_count(sec)
        if want is None or want == len(bl):
            continue
        bad.append((sec, want, len(bl)))
    if not bad:
        return ""
    parts = [f"「{_sec_label(s)}」声明 {w} 小题、实际切出 {g} 块"
             for s, w, g in bad[:4]]
    tail = f" 等 {len(bad)} 处" if len(bad) > 4 else ""
    return ("题数与原卷声明不符：" + "；".join(parts) + tail +
            "。切少了通常是某道题的题号被识别丢了、并进了上一题，请对照原卷核对；"
            "切多了多半是正文里的编号被当成了题号。")


def _declared_count(section: str) -> int | None:
    """分区标题里声明的小题数，取不到返回 None。"""
    m = _COUNT_RE.search(section or "")
    if m:
        return int(m.group(1))
    m = _CN_COUNT_RE.search(section or "")
    if m and m.group(1) in _CN_NUM:
        return _CN_NUM[m.group(1)]
    return None


def _sec_label(section: str) -> str:
    """分区标题截成给用户看的短标签。整条太长（真实标题常有 40+ 字，把分值、
    要求全写进去了），提示里贴全文会把真正的信息挤没。"""
    head = (section or "").strip().lstrip("#").strip()
    head = re.split(r"[:：]", head, maxsplit=1)[0]
    return head[:14] if head else "未命名分区"


def find_option_count_anomalies(blocks) -> list[tuple[int | None, str]]:
    """返回已由分区确认、但只识别到 1—3 个选项标签的题号与标签集合。"""
    bad = []
    for b in blocks:
        section = b.section or ""
        if b.zone != "stem" or not _CHOICE_SECTION_RE.search(section):
            continue
        if mechfix.has_complete_choice_options(b.text, known_choice=True):
            continue
        # 泛称“选择题”可能是标题漂移的填空分区，仍要求答题括号/选项序列；但
        # ``blockpipe`` 已用整段多数证据落实成“单选题”的分区可以直接数标签，
        # 这样 OCR 吃掉答题括号及一个选项时也不会被误判成解答题后静默放过。
        specific = re.search(r"单选|多选|不定项", section) is not None
        if (not specific and not mechfix.has_choice_answer_blank(b.text)
                and not mechfix.looks_like_choice_options(b.text)):
            continue
        # 只在最后一个答题空之后数选项。物理题干常先定义物块 A/B，扫整块会把
        # “选项文字全丢”误报成“只见 AB”，继而掩盖真正需要局部重识别的形态。
        blanks = list(mechfix._EMPTY_ANSWER_PAREN_RE.finditer(b.text))
        tail = b.text[blanks[-1].end():] if blanks else b.text
        labels = {m.group(1) for m in optcheck._LABEL_RE.finditer(tail)}
        labels |= {m.group(1) for m in _LOOSE_LABEL_RE.finditer(tail)}
        if 1 <= len(labels) < 4:
            bad.append((b.number, "".join(sorted(labels))))
            continue
        # 明确单/多选分区 + 答题空 + 2—4 个独立公式，是“选项散文和标签被
        # MinerU 吃掉、只剩公式”的强信号。四张及以上图片则可能本来就是纯图
        # 选项，沿用既有图片质量门处理，不在这里误报。
        display_formulas = re.findall(
            r"(?ms)^\s*\$\$\s*\n?.+?\n?\s*\$\$\s*$", tail)
        images = re.findall(
            r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", tail, re.I)
        if (not labels and specific and blanks
                and 2 <= len(display_formulas) <= 4 and len(images) < 4):
            bad.append((b.number, ""))
    return bad


def check_option_count(blocks) -> str:
    """选择题的选项标签数不足 4。

    与 `optcheck` 不重叠：那边查「标签在、内容空」，这边查「标签整个没了」。
    只在分区标题明说是选择题时判（见 _CHOICE_SECTION_RE），且标签数 ≥1 才报——
    一个标签都没有更可能是分区标题串了行，不是选项丢了。

    标签取 `optcheck._LABEL_RE` 与 `_LOOSE_LABEL_RE` 的**并集**。原先只用前者，
    在语料上报了 7 份，逐份核完发现**几乎全是误报**：MinerU 经常把选项标签后面的
    点也吃掉，产出 `A $\\sqrt{3}$ B． C $\\sqrt{6}$ D $2\\sqrt{3}$`——四个选项都在，
    但严格正则只认带点的那个 `B`，于是报「只见 B」。严格正则本身不能放宽（它那两处
    窄是 CLAUDE.md 点名的雷区，放宽会让 `A. -1013B．` 整行漏检），所以另开一条宽的
    只用来兜底：**任一条数满四个就不报**。取并集后语料上只剩 1 份（ef224f07 第 8 题
    确实一个选项都没识别出来），是真的。
    """
    bad = find_option_count_anomalies(blocks)
    if not bad:
        return ""
    shown = "、".join(
        f"第 {n or '?'} 题(只见 {ls or '无标签'})" for n, ls in bad[:6])
    tail = f" 等 {len(bad)} 道" if len(bad) > 6 else ""
    return f"这些选择题的选项不足四项：{shown}{tail}，请对照原卷补全。"


def check_solution_in_stem(blocks) -> str:
    """题干区**末尾一小段**块里出现 `【答案】`/`【解析】`——答案区边界判早了。

    两处收紧，都是标定逼出来的。原先只要题干区有一个块带解析标记就报，语料上
    7 份命中，核完全是**题解同页**的正常卷子（29618c98：19 块全在题干区、
    pair_blocks 19/19 全配对 0 孤儿，答案确实印在每道题下面）。那种卷子每次导入
    都会挨一条毫无用处的提示。

      ① 命中数过半 → 不报。过半意味着「整份都这样」＝题解同页的版式，不是判错。
         判错的形态相反：只有卷末少数几块越了界。
      ② 命中的必须是**题干区末尾连续的一段** → 否则不报。答案区在文档末尾，
         边界判早只会让紧邻边界的那几块受影响，中间零散命中是别的原因
         （多半是题干里引用了「【答案】」字样），报了也没有可操作的动作。

    收紧后语料上只剩 1 份（62e75bd9，一份讲义体的资料，`##` 小标题当题号切的，
    最后一块确实混了内容）——这条从此是低频提示，报出来就值得看。
    """
    stems = [b for b in blocks if b.zone == "stem"]
    if not stems:
        return ""
    marked = [i for i, b in enumerate(stems) if _SOL_MARK_RE.search(b.text)]
    if not marked or len(marked) * 2 >= len(stems):
        return ""
    if marked != list(range(len(stems) - len(marked), len(stems))):
        return ""
    shown = "、".join(str(stems[i].number) if stems[i].number else "?"
                     for i in marked[:8])
    tail = f" 等 {len(marked)} 处" if len(marked) > 8 else ""
    return (f"题干区末尾的第 {shown}{tail} 题里带着答案/解析标记，"
            "多半是答案区的起点判早了。校对页里请把答案部分挪进解析栏。")


def check_long_blocks(blocks) -> str:
    """超长题块——最可能的成因是两道题没切开。

    判据是「与同一份文档里其它块比」而不是绝对字数：不同学段的卷子长度差一个
    量级，绝对阈值必然要么全份误报要么全份漏报。两处限定范围：

      ① **按分区比**，不跨分区比（见 _by_section）；
      ② **带解析标记的块不参与**。题解同页的卷子上，块的长度由解析写多长决定，
         而两解法并列的题（实测 29618c98 第 8 题：3644 字，全卷中位 855）能轻松
         超过 4 倍中位数——那是一道题的正常长度，不是两道题并了。把这类块整体
         排除掉之后，语料 41 份的误报从 7 份降到 **0 份**。

    代价：题解同页的卷子上这一条基本不工作了。可以接受——那种版式下真正管用的是
    题号空洞与声明数两条，它们不受解析长度影响。
    """
    bad = []
    stems = [b for b in blocks
             if b.zone == "stem" and not _SOL_MARK_RE.search(b.text)]
    for bl in _by_section(stems).values():
        if len(bl) < _MIN_BLOCKS_FOR_OUTLIER:
            continue
        sizes = [_nonspace(b.text) for b in bl]
        med = statistics.median(sizes)
        if med <= 0:
            continue
        bad += [b.number for b, n in zip(bl, sizes)
                if n > med * _LONG_BLOCK_RATIO]
    if not bad:
        return ""
    shown = "、".join(str(n) if n else "?" for n in bad[:6])
    tail = f" 等 {len(bad)} 处" if len(bad) > 6 else ""
    return (f"第 {shown}{tail} 题的篇幅明显长于同分区其它题，"
            "可能是相邻两题没切开（题号被识别丢了）。请对照原卷确认。")


def check_ocr_noise(blocks) -> str:
    """报告高度疑似扫描透字／文本层错位的数学命令堆积，不修改原文。"""
    bad = [b.number for b in blocks if b.zone == "stem"
           and has_dense_ocr_math_noise(b.text)]
    if not bad:
        return ""
    shown = "、".join(str(n) if n else "?" for n in bad[:6])
    tail = f" 等 {len(bad)} 处" if len(bad) > 6 else ""
    return (f"第 {shown}{tail} 题含大量互不相关的偏导／希腊字母／排版命令，"
            "疑似扫描背面透字或 PDF 文本层错位被 OCR 当成正文。机械模式不会猜删内容，"
            "请在校对页对照原卷删除乱码，或改用 AI 规范化尝试清理。")


_IMAGE_REF_RE = re.compile(
    r"!\[[^\]]*\]\([^)]*\)|!\[\[[^\]]+\]\]")

# 孤儿解析并不一定是事故：答案速查可能比题面多一两项，短答案 ``A`` / ``BD``
# 即使配不上也不值得把每份卷子都染红。只有“至少约 80 个可见内容单位，且占已切
# 内容至少 20%”才提示；若绝对量已很大（300 单位），占比放宽到 10%。图片按 40
# 单位计，不然一张图在字符账上只有 Markdown 文件名，整页图解会被错误视为零损失。
_UNPAIRED_MIN_UNITS = 80
_UNPAIRED_MIN_RATIO = 0.20
_UNPAIRED_LARGE_UNITS = 300
_UNPAIRED_LARGE_RATIO = 0.10
_IMAGE_CONTENT_UNITS = 40
MANUAL_REVIEW_MARKER = "【必须人工校对】"


def mark_manual_review(note: str) -> str:
    """给高置信内容异常加统一门控标记，重复调用也只保留一个标记。"""
    note = str(note or "")
    if not note or MANUAL_REVIEW_MARKER in note:
        return note
    return f"{MANUAL_REVIEW_MARKER}{note}"


def requires_manual_review(notes) -> bool:
    """转换提示里是否含不应免审入库的高置信结构异常。"""
    if isinstance(notes, str):
        return MANUAL_REVIEW_MARKER in notes
    return any(MANUAL_REVIEW_MARKER in str(note) for note in (notes or []))


def _content_units(text: str) -> int:
    """用于丢失账本的可见内容量；只比较量级，不把 Markdown 载荷当正文。"""
    images = len(_IMAGE_REF_RE.findall(text or ""))
    without_images = _IMAGE_REF_RE.sub("", text or "")
    return _nonspace(without_images) + images * _IMAGE_CONTENT_UNITS


def check_unpaired_content(blocks, pair_result) -> str:
    """报告会被坐标配对跳过的显著解析正文，不修改块，也不阻断转换。

    ``pair_blocks`` 一直把孤儿解析和冲突记在账上，但旧 ``report`` 没消费这两项；
    机械渲染随后只遍历 ``paired``，于是孤儿正文可以占原文一半、任务仍显示成功且
    note 为空。这里补上缺失的用户提示，同时守住两个低误报边界：

    - 题号缺失过半时，下游会退化为顺序分组，孤儿账已不能代表实际丢弃，不报告；
    - 普通短答案／速查表靠绝对量和占比双阈值过滤，不因一个 ``A`` 天天报警。

    这层只报告。开放 OCR 文本下无法仅凭坐标证明孤儿一定是正式内容，直接抛错会
    让正常的多余答案项也无法进入校对页；红色校对提示既能阻止“无感成功”，又把
    最终取舍留给能看到原卷的用户。
    """
    if pair_result is None:
        return ""
    orphans = list(getattr(pair_result, "orphan_solutions", None) or [])
    if not orphans:
        return ""

    stems = [b for b in blocks if b.zone == "stem"]
    missing = len(getattr(pair_result, "missing_numbers", None) or [])
    if not stems or missing * 2 > len(stems):
        return ""

    lost = sum(_content_units(block.text) for block in orphans)
    whole = sum(_content_units(block.text) for block in blocks)
    ratio = lost / whole if whole else 0.0
    significant = (
        lost >= _UNPAIRED_MIN_UNITS and ratio >= _UNPAIRED_MIN_RATIO
    ) or (
        lost >= _UNPAIRED_LARGE_UNITS and ratio >= _UNPAIRED_LARGE_RATIO
    )
    if not significant:
        return ""

    shown = "、".join(
        f"第 {block.number if block.number is not None else '?'} 题"
        for block in orphans[:6]
    )
    tail = f" 等 {len(orphans)} 块" if len(orphans) > 6 else ""
    logger.warning(
        "配对后有 %d 个孤儿解析块、约 %d 内容单位未进入题目（占已切内容 %.1f%%）",
        len(orphans), lost, 100 * ratio,
    )
    return (
        f"{MANUAL_REVIEW_MARKER}切块后有 {len(orphans)} 个解析块无法与题目配对"
        f"（{shown}{tail}），"
        f"约 {lost} 个字/图片内容、占已切内容 {round(100 * ratio)}%，"
        "按当前机械配对可能不会进入最终题目。请在拆题校对页核对题干/解析分区和题号；"
        "若原文件本来只有这些多余答案，可忽略此提示。"
    )


def report(blocks, pair_result=None) -> list[str]:
    """跑全部体检项，返回给用户看的话（没问题就是空列表）。

    顺序即重要性，也是可信度排序：前两条（题号空洞、声明数不符）的判据是原文
    自带的事实，后两条带阈值。语料上 41 份里 28 份一条都不报——这个比例是刻意的，
    见模块头。
    """
    notes = []
    if pair_result is not None:
        notes.append(mark_manual_review(
            check_number_gaps(getattr(pair_result, "number_gaps", []))))
        notes.append(check_unpaired_content(blocks, pair_result))
    notes.append(mark_manual_review(check_declared_count(blocks)))
    notes.append(mark_manual_review(check_ocr_noise(blocks)))
    notes.append(check_long_blocks(blocks))
    notes.append(mark_manual_review(check_option_count(blocks)))
    notes.append(check_solution_in_stem(blocks))
    return [n for n in notes if n]
