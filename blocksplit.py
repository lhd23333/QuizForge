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
from difflib import SequenceMatcher
import html
import logging
import re

# 中文数字转整数复用 importer 的实现，不再抄第三份（normalizer 里已有一份，
# 那份在 project-alpha 里、按约定不改）。
from importer import _cn_to_int

logger = logging.getLogger(__name__)

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
# 宽松档额外认出的括号编号。这里只用于判断它是否只是严格主题内部的小问；
# 题号解析仍统一走 _NUM_LINE_LOOSE_RE，避免两套正则产生不同切块口径。
_LOOSE_SUBQUESTION_HEAD_RE = re.compile(
    r"^(?:[(（]\s*(\d{1,3})\s*[)）]|(\d{1,3})\s*[)）])\s*")
# 误命中排除：`1.1.5 函数…`（小节号）这类「数字.数字.」链，正文侧再跟一个点号数字
_VERSION_TAIL_RE = re.compile(r"^\d{1,3}\s*[.．、]")
# PDF 正文前常集中放“第 1 题图”“第2题图”这类插图说明。它有完整的
# “第/题”双锚点，方言评分会把它误判成比真正 ``1. 题干`` 更强的题号体系，
# 随后整份只按图片说明切块。仅排除题号后只剩“图/示意图”的独立说明；
# “第1题 图中小球……”仍是有正文的正常题号，不能误伤。
_QUESTION_FIGURE_CAPTION_RE = re.compile(
    r"^(?:示意)?图(?:\s*[（(][A-Za-z0-9一二三四五六七八九十]+[)）])?$"
)

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

# `2025 江苏高中联赛预赛第 1 题`：题号前粘一段卷名前缀（年份+地区+赛事名）。
# 这是题库导出的主流写法，2026-08-06 那批 11 份预赛卷全是它，而上面两条正则
# 一档都认不出——`_CN_TI_RE` 把 `第` 锚在行首，前面还有 12 个字符就没戏了。
# 后果不是「切得不好」而是**静默丢正文**：重庆那份 15 道题只切出 2 块
# （cover 0.14），剩下的全落进 _split_pass 末尾那句「首个题号之前的零散行直接
# 丢弃」，不报错、不留痕。
#
# 放宽的口径是「前缀任意、但整行必须**收在** `第N题` 上」，两个条件都必需：
#   ① 行尾收尾 —— 只有 MinerU 把题号提成独立标题行时才这样。散文里的引用
#      （`……的解法见第 3 题的第 (2) 问`）后面必然还有字，一律不匹配；
#   ② 前缀不含句末标点与括号（`。．.！!？?；;：:，,、（）()【】`）—— 卷名前缀
#      是连续的一串字（`2025 江苏高中联赛预赛`），跨句子的文本必然带标点。
# 前缀另设 20 字符上限，且用非贪婪：`第` 出现多次时取最后一个（`第一部分 第3 题`
# 这样的行仍取到 3，不会把「一」当题号）。
#
# 在 55 份真实产物上标定（2026-08-06）：只命中那 3 份带卷名前缀的预赛卷，
# 各 11/11/16 行且题号全程递增（LIS 与命中数相等），其余 52 份**零命中**。
# 捕获组 2 恒为空串：题号独占一行，题干在后续行——与 `15.(13 分)` 那种「块的
# 起点对、首行取不到题干」的情形同构，下游本来就按整块正文处理，不受影响。
_CN_TI_PREFIX_RE = re.compile(
    r"^[^。．.！!？?；;：:，,、（）()【】]{0,20}?"
    r"第\s*(\d{1,3})\s*[题題]\s*[.．、:：]?\s*()$")


@dataclasses.dataclass(frozen=True)
class _Dialect:
    """一种题号写法。strict/loose 对应 split_blocks 的严格档与放宽档。

    guard_chain 控制是否套用 _VERSION_TAIL_RE 那道「链式数字」排除：它是为
    `1.1.5` `0. 2 = 0. 5` 这类**纯数字**误命中准备的，而 `第N题` 有「第」「题」
    两个汉字锚点、不会跟小节号混，套上反而会把 `第10 题. 3. 求…` 这种题干误杀。

    numeral 说明 strict/loose 第 1 个捕获组抓到的是哪种数字，`_parse_number` 靠
    它决定用 int() 还是 _cn_to_int() 解析——`第一题` 这种中文题号 int() 会抛。

    prefixed 是**同一种题号写法、但行首粘了段卷名前缀**时的备用正则（见
    _CN_TI_PREFIX_RE）。它只在 strict/loose 都不匹配时才试，所以无前缀的卷子
    一行都不会改判；捕获组语义与 strict/loose 相同。不是所有方言都该有它——
    只有带汉字/字母锚点的写法放宽了才安全，纯 `x.` 加前缀等于认「任意行尾的
    数字加点」。None 表示这个方言不接受前缀。
    """

    name: str
    strict: "re.Pattern[str]"
    loose: "re.Pattern[str]"
    guard_chain: bool = True
    weight: float = 1.0
    numeral: str = "arabic"          # "arabic" | "chinese"
    prefixed: "re.Pattern[str] | None" = None


# ── 题号模板 ────────────────────────────────────────────────────────────────
# 自动判方言（_detect_dialect）解决的是「常见写法认错」，解决不了「这份文档的写法
# 我们压根没收录」。而收录方式如果只能改代码，用户拿到一份 `【第5题】` 的卷子就只
# 能等下次发版。所以开一个口子：让用户自己写题号长什么样，编译成方言当场用。
#
# 模板语法刻意只有两个占位符，其余字符字面匹配：
#   x → 阿拉伯数字题号（1~3 位）        X → 中文数字题号（`一` `十二`）
# 例：`x.` `x、` `(x)` `第x题` `第X题` `【第x题】` `Qx.`
#
# 编译时做三件容错，都是 OCR 产物的实测特征，不是想当然：
#   ① 任意两个字符之间都允许空白——`第1 题` 的空格是 MinerU 竖排切分留下的，
#      写模板的人不会想到要写它（AGMC 那份卷子就是这样，见 _DIALECTS 注释）；
#   ② 半角/全角同义：`.`↔`．`、`(`↔`（`、`)`↔`）`、`:`↔`：`、`[`↔`【`、`]`↔`】`；
#   ③ 题号前允许粘 1~4 个答案字母（`AD 9. 一组数据…`），与 _NUM_LINE_RE 同一
#      理由：不允许的话这一题连同整段解析会被静默丢掉。
_TPL_EQUIV = {
    ".": "[.．]", "．": "[.．]",
    "(": "[(（]", "（": "[(（]",
    ")": "[)）]", "）": "[)）]",
    ":": "[:：]", "：": "[:：]",
    "[": r"[\[【]", "【": r"[\[【]",
    "]": r"[\]】]", "】": r"[\]】]",
    "、": "[、]",
}
_TPL_CN_CLASS = "[一二三四五六七八九十百零]+"


class TemplateError(ValueError):
    """题号模板写得不合法。消息直接给用户看，所以写成中文、说清该怎么改。"""


def compile_dialect(template: str, *, name: str = "custom",
                    weight: float = 1.0) -> _Dialect:
    """把用户写的题号模板编译成 _Dialect。语法见 _TPL_EQUIV 上方注释。

    编译出的正则形如 `^(?:字母前缀)?<模板><\\s*>(.*)$`，两个捕获组的含义与手写
    方言完全一致（组 1 题号、组 2 题号之后的正文），所以 _parse_number /
    _split_pass 不需要为「自定义方言」分支。

    guard_chain 只在模板是**纯数字加分隔符**（`x.` `(x)`）时打开：那种模板跟
    `1.1.5` 小节号、被 OCR 拆开的小数完全同形，必须留着那道排除；模板里只要有
    汉字或字母锚点（`第x题` `Qx.`），同形风险就没了，反而是那道排除会把
    `第10 题. 3. 求…` 这类正文以数字开头的题干误杀。
    """
    tpl = (template or "").strip()
    if not tpl:
        raise TemplateError("题号模板不能为空")
    if len(tpl) > 24:
        raise TemplateError("题号模板过长（最多 24 个字符）")
    holders = [c for c in tpl if c in ("x", "X")]
    if not holders:
        raise TemplateError(
            "模板里必须有一个 x 或 X 代表题号数字，比如 x. 或 第X题")
    if len(holders) > 1:
        raise TemplateError(
            "模板里只能有一个 x 或 X（题号只有一个数字），比如 第x题")

    numeral = "chinese" if holders[0] == "X" else "arabic"
    # 有没有汉字/字母锚点，决定 guard_chain（见 docstring）
    has_anchor = any(
        c not in ("x", "X") and (c.isalpha() or ord(c) > 0x2E80) for c in tpl)

    parts: list[str] = []
    for ch in tpl:
        if ch == "x":
            parts.append(r"(\d{1,3})")
        elif ch == "X":
            parts.append(f"({_TPL_CN_CLASS})")
        elif ch.isspace():
            continue                     # 空白一律由下面的 \s* 统一承担
        elif ch in _TPL_EQUIV:
            parts.append(_TPL_EQUIV[ch])
        else:
            parts.append(re.escape(ch))
    # `\s*` 插在每两个片段之间：`第1 题` 那个空格靠它兜住
    body_pat = r"\s*".join(parts)
    pat = re.compile(r"^(?:[A-DＡ-Ｄ]{1,4}\s+)?\s*" + body_pat + r"\s*(.*)$")
    # 第四件容错：题号前允许粘一段卷名（`2025 江苏高中联赛预赛第 1 题`）。
    # 用户写 `第x题` 时想表达的是「题号长这样」，不是「行首顶格」——2026-08-06
    # 那批预赛卷用户手填了 `第 x 题` 仍然全丢，就是因为编译出的正则锚在 ^。
    # 口径与 _CN_TI_PREFIX_RE 完全一致（整行收在题号上 + 前缀不含句末标点），
    # 理由见那条正则的注释。**只在模板有汉字/字母锚点时给**：纯 `x.` 加上前缀
    # 等于认「任意行尾的数字加点」，正文里的小数、页码全会命中。
    prefixed = None
    if has_anchor:
        prefixed = re.compile(r"^[^。．.！!？?；;：:，,、（）()【】]{0,20}?"
                              + body_pat + r"\s*()$")
    return _Dialect(name=name, strict=pat, loose=pat,
                    guard_chain=not has_anchor, weight=weight,
                    numeral=numeral, prefixed=prefixed)


# 默认方言 = 老实现的行为。它必须排在第一位且在打平时胜出，
# 这样绝大多数 `1.` 体系的试卷走的还是原来那条路，改动零风险。
_DEFAULT_DIALECT = _Dialect("arabic-dot", _NUM_LINE_RE, _NUM_LINE_LOOSE_RE)

# `第一题` / `第 十二 题．`：中文数字题号。与 _CN_TI_RE 同形，只是数字部分换成
# 中文数字类。此前一档都认不出（_CN_TI_RE 只认 `\d`，_SEC_LINE_RE 认的是
# `一、` 而非 `第一题`），这类文档只能整份退化成一个巨块。
_CN_TI_CN_RE = re.compile(
    r"^(?:§\s*[\d.]+\s*)?第\s*([一二三四五六七八九十百零]+)\s*[题題]\s*[.．、:：]?\s*(.*)$")

_DIALECTS = (
    _DEFAULT_DIALECT,
    # weight=3：`第N题` 有「第」「题」两个汉字锚点，误命中率远低于裸 `N.`——
    # 后者在数学卷里跟小数、选项、竖排拆行的根式全都撞。所以同样长的递增序列，
    # 这个方言的证据强得多。没有权重时实测短文档会选错：`√313` 拆出的
    # `5.` `313.` 碎片凑出的递增长度可以盖过真实的两道 `第 x 题`。
    #
    # prefixed 让它同时认带卷名前缀的 `2025 江苏高中联赛预赛第 1 题`。挂在同一个
    # 方言上而不是新开一条：它认的是同一种题号写法，只是行首多了段卷名。分成两条
    # 会让 _detect_dialect 里两个几乎同分的候选互相抢，打平顺序成了行为的一部分。
    _Dialect("cn-di-ti", _CN_TI_RE, _CN_TI_RE, guard_chain=False, weight=3.0,
             prefixed=_CN_TI_PREFIX_RE),
    # 中文数字题号同样有「第」「题」双锚点，给同样的权重。排在阿拉伯版之后：
    # 两者不会同时命中同一行（数字类不重叠），顺序只影响打平顺序。
    _Dialect("cn-di-ti-cn", _CN_TI_CN_RE, _CN_TI_CN_RE,
             guard_chain=False, weight=3.0, numeral="chinese"),
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
    r"(选择题|多选|单选|填空题|实验题|解答题|计算题|证明题|应用题|本题共|本大题|部分)")

# 教辅书里的练习层级标题不使用「一、选择题」格式，但同样会让题号重新从 1 开始。
# 《高考必刷题椭圆》的第三页就是「核心题型 …」后重新编号 1~6；旧实现没把这行
# 当分区，_find_restart 便把第二套题号误判成卷末解析区，六道题全部变成孤儿解析、
# 最终静默消失。这里只认教辅中独立成行的固定栏目词，不把任意 Markdown 标题都算
# 分区，避免正文小标题随意重置题号上下文。
_PRACTICE_SECTION_RE = re.compile(
    r"^(?:核心题型(?:\s|$|[①②③④⑤⑥⑦⑧⑨⑩])|"
    r"刷(?:基础|提分|易错|素养|提升|能力|高分|综合)(?:\s|$|[▶▷])|"
    r"易错点\s*[▶▷])")

# 分组标题：`A 组` `B组` `一组`（整行只有这个）
_GRP_LINE_RE = re.compile(r"^([A-DＡ-Ｄa-d]|[一二三四五六七八九十]+)\s*组\s*$")

# 解析区起始标题：`参考答案` `答案与解析` `参考答案及评分标准` 等。也兼容
# `《某某试卷》参考答案` 和 `数学试卷参考答案及评分标准` 这种带卷种前缀的标题。
# 裸前缀只收固定的“数学试卷/数学试题/试题”，整条正则仍要求标题占满一整行；
# 不能放宽成任意“……试卷”前缀，否则“请对照本试卷参考答案”也会被误判为边界。
_ANS_LINE_RE = re.compile(
    r"^(?:(?:[《〈].{1,100}[》〉]|数学(?:试卷|试题)?|试题|"
    r"[^。！？!?：:]{1,100}数学)\s*)?"
    r"(?:参考答案(?:(?:与|及)(?:试题)?解析|(?:与|及)评分标准)?"
    r"|答案(?:(?:与|及)解析|(?:与|及)(?:评分标准|评分细则))?"
    r"|参考解答|答案解析|解析)\s*[:：]?\s*$")

# Doc2X 会把答案速查表保留为两行 HTML table，也会把三道填空答案压成一行，
# 偶尔还把题号本身包成 `${12}.`。这些内容此前全落在“首个解析题号之前”被丢弃，
# 题干虽然不少，答案却静默缺失。只在已经命中上面的答案区标题后展开，正文区的
# 表格、同行编号和数学公式不参与，避免把题面结构误切成解析块。
_ANSWER_TABLE_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_ANSWER_TABLE_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.I | re.S)
_MATH_WRAPPED_ANSWER_NO_RE = re.compile(
    r"^\s*\$\{\s*(\d{1,3})\s*\}\s*[.．、]\s*(\S.*?)\$\s*$")
_COMPACT_ANSWER_NO_RE = re.compile(r"(?:^|\s)(\d{1,3})\s*[.．、]\s+")


def _plain_table_cell(cell: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()


def _expand_answer_key_structures(raw_md: str) -> str:
    """把答案区的表格/同行答案展开成普通编号解析块；证据不足时逐字不动。"""
    lines = raw_md.splitlines()
    output: list[str] = []
    in_answer = False
    section = ""
    for line in lines:
        body = _MD_HEAD_RE.sub("", line).strip()
        if _ANS_LINE_RE.match(body):
            in_answer = True
            section = ""
            output.append(line)
            continue
        if not in_answer:
            output.append(line)
            continue
        if _SEC_KEYWORD_RE.search(body):
            section = body

        wrapped = _MATH_WRAPPED_ANSWER_NO_RE.match(body)
        if wrapped:
            output.append(f"{wrapped.group(1)}. ${wrapped.group(2)}$")
            continue

        rows = _ANSWER_TABLE_ROW_RE.findall(line)
        if len(rows) == 2:
            heads = [_plain_table_cell(cell)
                     for cell in _ANSWER_TABLE_CELL_RE.findall(rows[0])]
            values = [_plain_table_cell(cell)
                      for cell in _ANSWER_TABLE_CELL_RE.findall(rows[1])]
            # Doc2X 常把左上角表头也作为第一格保留：
            #   题号 | 1 | 2 ...
            #   答案 | C | D ...
            # 老判据要求 heads 全是数字，导致这种最标准的速查表反而整张被跳过。
            if (heads and values
                    and heads[0].strip() in {"题号", "题目", "题次"}
                    and values[0].strip() in {"答案", "参考答案"}):
                heads, values = heads[1:], values[1:]
            if (heads and len(heads) == len(values)
                    and all(re.fullmatch(r"\d{1,3}", item) for item in heads)):
                numbers = [int(item) for item in heads]
                if (all(number > 0 for number in numbers)
                        and all(a < b for a, b in zip(numbers, numbers[1:]))
                        and all(values)):
                    output.extend(
                        f"{number}. 【答案】{value}"
                        for number, value in zip(numbers, values))
                    continue

        # 填空答案通常正好三项；要求从行首开始、连续递增且每项非空。
        # 小数 `0.5` 的点后没有空格，不命中；解析正文里的 (1)(2) 也不是此格式。
        if "填空" in section:
            compact = list(_COMPACT_ANSWER_NO_RE.finditer(body))
            if len(compact) >= 3 and compact[0].start(1) == 0:
                numbers = [int(match.group(1)) for match in compact]
                values = [
                    body[match.end():compact[index + 1].start(1)].strip()
                    if index + 1 < len(compact) else body[match.end():].strip()
                    for index, match in enumerate(compact)
                ]
                if (numbers == list(range(numbers[0], numbers[0] + len(numbers)))
                        and all(values)):
                    output.extend(
                        f"{number}. 【答案】{value}"
                        for number, value in zip(numbers, values))
                    continue
        output.append(line)
    return "\n".join(output)

# 块内解析标记：`【答案】` `【解析】`，可带题号前缀（`## 3．【答案】 C`）
_SOL_MARK_RE = re.compile(r"^(?:\d{1,3}\s*[.．、]\s*)?(【答案】|【解析】|答案[:：]|解析[:：])")
# 教师版试卷常把题目来源、命题意图和解析紧跟在本题后面，其中可能原样引用教材
# 练习（如「6. 设……」）。只看行首题号会把引用题切成正式新题。这里仅用于判断
# 前一块是否已经进入答案/解析区；不拿它直接切正文，避免扩大开放输入的误伤面。
_INLINE_SOL_MARK_RE = re.compile(r"【\s*(?:参考)?(?:答案|解析)\s*】|(?:参考)?(?:答案|解析)\s*[:：]")

# 解析册逐题答案的强边界。与 _INLINE_SOL_MARK_RE 分开：这里只用于“同一解析块里
# 出现了第二个逐题详解”的结构恢复，不能把正文中普通的“答案：”都当成新题。
_DETAIL_MARK_RE = re.compile(r"【\s*(?:详解|解析)\s*】")

# 卷首「注意事项」特征词：命中即整段丢弃（它的 1. 2. 3. 会被误当成题号）
_PREAMBLE_KEYWORD_RE = re.compile(
    r"(答题卡|准考证号|考试结束|铅笔|涂黑|橡皮(?!泥|筋)|交回|本试卷共|考试时间|草稿纸|非选择题)")


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
    number_gaps: list[int] = dataclasses.field(default_factory=list)
    """题号序列里的空洞（`1,2,4` → `[3]`）。见 find_number_gaps。

    带默认值是为了不动 `PairResult(...)` 的既有构造点顺序；实际上 pair_blocks
    一律会填。
    """


# 单个空洞的容忍上限。一个洞里超过这么多个连号，判断上更像「题号是假的」而不是
# 「真丢了这么多题」——OCR 把 `√313` 拆成 `313.` 这类碎片会造出一个巨大的洞，
# 报「缺 4…312 共 309 题」纯属噪声。真实丢题是零星的（一两道），这个上限只挡噪声。
_MAX_GAP_RUN = 10


def find_number_gaps(numbers) -> list[int]:
    """题号序列里的空洞。`[1,2,4,5]` → `[3]`。

    为什么需要它（而不是继续只靠 `_drop_note`）：`_drop_note` 的阈值是丢字占比
    25%，是按「整份文档被吃掉」标定的（正常文档最高 8.96%，两次事故是 79% 与
    86%）。丢两道题只占 5% 左右，落在阈值之下，页面上一个字都不会说。而题号
    序列是原文自带的、免费的强校验：切出 1,2,4 就是明摆着第 3 题没了，不需要
    任何模型、任何阈值。这正是 v0.3.1 那次 `. .....4分` 静默吞一道题的形状。

    两个刻意的保守处：
      ① **按递增段分段处理**。真实试卷常按大题重新起号（`一、` 1-8，`二、` 1-4），
         整段序列上直接取 min..max 会把重起号报成「缺 5…8」。遇到不递增就开新段，
         段内才找洞。
      ② **单洞超过 _MAX_GAP_RUN 就不报**。见那个常量的注释。

    只报告、不修补——这是导入链一贯的态度（见 optcheck / `_drop_note`）：丢了的
    内容补不回来，猜比留个明显的洞更糟。
    """
    nums = [n for n in numbers if isinstance(n, int)]
    # 多栏 PDF 会把完整题号识别成 1..11,14,15,12,13。集合本身连续且无重复时，
    # 这不是“缺 12、13”，只是阅读顺序错了；排序由 blockpipe 处理，体检不应误报。
    ordered = sorted(nums)
    if (nums and len(set(nums)) == len(nums)
            and ordered == list(range(ordered[0], ordered[-1] + 1))):
        return []
    gaps: list[int] = []
    run: list[int] = []
    for n in nums:
        if run and n <= run[-1]:
            gaps.extend(_gaps_in_run(run))
            run = []
        run.append(n)
    gaps.extend(_gaps_in_run(run))
    return gaps


def _gaps_in_run(run: list[int]) -> list[int]:
    """一段严格递增的题号里的空洞（单洞过大视为假题号，整洞跳过）。"""
    out: list[int] = []
    for a, b in zip(run, run[1:]):
        if 1 < b - a <= _MAX_GAP_RUN + 1:
            out.extend(range(a + 1, b))
    return out


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

    # 题号空洞按组分别找：分组卷（A 卷/B 卷）两组各自从 1 起号，混在一起看
    # 序列会一直"不递增"，把整份文档切成一堆单元素段，什么也检不出来。
    gaps: list[int] = []
    for grp in dict.fromkeys(st.group for st in stems):
        gaps.extend(find_number_gaps(
            [st.number for st in stems if st.group == grp]))
    gaps = sorted(dict.fromkeys(gaps))
    if gaps:
        shown = "、".join(str(n) for n in gaps[:10])
        more = f" 等 {len(gaps)} 道" if len(gaps) > 10 else ""
        conflicts.append(f"题号不连续，缺第 {shown}{more} 题（可能被漏切或原卷本来没有）")
    return PairResult(paired=paired, orphan_solutions=orphans,
                      missing_numbers=missing, conflicts=conflicts,
                      number_gaps=gaps)


def _strip_head(line: str) -> str:
    """剥掉行首 markdown 标题和题目难度星标，返回正文部分。"""
    body = _MD_HEAD_RE.sub("", line).strip()
    # ``★1．``、``★★12．`` 中的星号只表示难度。限定为明确的实心/空心
    # 星号，不能泛化删除 Markdown ``*``，否则会误伤列表和强调语法。
    return re.sub(r"^[★☆]+\s*", "", body)


def _parse_number(body: str, loose: bool = False,
                  dialect: _Dialect | None = None) -> tuple[int | None, str]:
    """从已剥 `#` 的行首取题号，返回 (题号, 题号之后的正文)。取不到返回 (None, body)。

    排除两类实测误命中：
      - `1.1.5 函数：…` 小节号：题号后紧跟又一个「数字.」；
      - `0. 2 = 0. 5 \\times …`：OCR 把 `0.2` 拆开，题号为 0 或后接纯数字算式。

    loose=True 时额外认 `12)` `（12）` 这类编号（只由 split_blocks 的第二档降级
    传入，理由见该函数）。

    dialect 决定用哪套题号正则，None 为默认的 `1.` 体系（老行为）。方言由
    _detect_dialect 在切块前定死，或由用户的题号模板钉死，切块过程中不再换——
    见 _DIALECTS 与 compile_dialect 注释。
    """
    d = dialect or _DEFAULT_DIALECT
    m = (d.loose if loose else d.strict).match(body)
    if not m and d.prefixed is not None:
        # 卷名前缀档（`2025 江苏高中联赛预赛第 1 题`）。放在主正则之后而不是
        # 合成一条：主正则命中的行结果必须逐字节不变，前缀档只捡它漏下的。
        m = d.prefixed.match(body)
    if not m:
        return None, body
    rest = m.group(2).strip()
    if (d.name in {"cn-di-ti", "cn-di-ti-cn"}
            and _QUESTION_FIGURE_CAPTION_RE.fullmatch(rest)):
        return None, body
    if d.numeral == "chinese":
        num = _cn_to_int(m.group(1))
        if num is None:
            return None, body                  # `零`、纯「百」这类算不出数的写法
    else:
        num = int(m.group(1))
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


def _split_pass(raw_md: str, loose: bool,
                dialect: _Dialect | None = None,
                drop_sink: list[str] | None = None) -> list[Block]:
    """按题号切一趟。loose 控制题号正则的宽严（见 _parse_number）。

    两趟：先 _drop_preamble + _detect_scheme 做文档级判定，再按判定出的大题号
    体系逐行切块，沿途维护 分区标题 / 分组 / 区（题干区还是解析区）三个上下文。

    dialect 非 None 时**跳过自动判方言**，用调用方钉死的那个（用户写了题号模板）。
    _detect_scheme 仍照跑：它判的是「大题号是阿拉伯还是中文」，与题号写法是两件
    独立的事——钉死了 `第x题` 也还要知道 `一、二、` 是分区标题还是大题号。

    区（题干区/解析区）不在切块时判，留给 _assign_zones 后处理——切块时只看得到
    局部一行，判不准；切完拿到全序列反而简单可靠。

    drop_sink 非 None 时，把「因为还没见到第一个题号而丢弃」的行原样塞进去。
    只记这一类丢弃，不记目录点导引 / `§2.1` / 分区标题这些：后者是有意为之、
    丢的也不是正文；而前者一旦发生在正文上就是**静默丢题**（见函数末尾注释），
    调用方靠它算出提示语。切块本身不因此改变行为——报告与切分解耦。
    """
    lines = raw_md.splitlines()
    start = _drop_preamble(lines)
    if dialect is None:
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
    # 分区标题之后、首个可识别题号之前的正文。通常为空；若 OCR 恰好吃掉新分区
    # 第一题的题号，则这些行不能继续当普通前言静默丢弃，见题号分支的恢复条件。
    section_lead: list[str] = []
    section_lead_line_no = 0

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

        # 教辅栏目标题：只切换分区上下文，不进入任何题块。后续若题号从 1 重开，
        # _find_restart 会看到 section 已变化，按新练习组处理而不是误判成解析区。
        if _PRACTICE_SECTION_RE.match(body):
            flush()
            cur_lines, cur_num = [], None
            section = body
            continue

        # 分区标题：`一、单选题…`。arabic 体系下它只是上下文（题型依据）；
        # chinese 体系下它本身就是大题起点。
        m = _SEC_LINE_RE.match(body)
        if m and (_SEC_KEYWORD_RE.search(body) or scheme == "chinese"):
            flush()
            cur_lines, cur_num = [], None
            section = body
            section_lead = []
            section_lead_line_no = 0
            if scheme == "chinese":
                cur_lines = [raw_line]
                cur_num = _cn_to_int(m.group(1))
                cur_line_no = i + 1
            continue

        # 题号行：arabic 体系下是新块起点；chinese 体系下是小问，留在当前块内
        num, _rest = _parse_number(body, loose=loose, dialect=dialect)
        if num is not None and scheme == "arabic":
            flush()
            # 新分区第一道题的题号整行被 OCR 吃掉时，形态是：旧分区末题 12 →
            # 「二、填空题」→ 一段无号正文 → 14。只有上下题号恰好差 2、分区确实
            # 改变且中间正文非空时，才能证明那段是第 13 题；否则仍按前言记入丢弃
            # 账，绝不凭一段无号文字猜题。
            if section_lead:
                previous = blocks[-1] if blocks else None
                if (previous and isinstance(previous.number, int)
                        and num == previous.number + 2
                        and previous.section != section):
                    missing = previous.number + 1
                    lead_text = "\n".join(section_lead).strip()
                    blocks.append(Block(
                        index=len(blocks), number=missing,
                        text=f"{missing}. {lead_text}", section=section,
                        group=group, zone=zone, line_no=section_lead_line_no,
                        kind=_classify(lead_text)))
                elif drop_sink is not None:
                    drop_sink.extend(section_lead)
                section_lead = []
                section_lead_line_no = 0
            cur_lines = [raw_line]
            cur_num = num
            cur_line_no = i + 1
            continue

        if cur_lines:
            cur_lines.append(raw_line)
        elif section is not None and scheme == "arabic":
            if not section_lead:
                section_lead_line_no = i + 1
            section_lead.append(raw_line)
        # 首个题号之前的零散行（小节标题等）直接丢弃，不凭空造块。
        #
        # 「不凭空造块」是对的，但**不能不留痕**：2026-08-06 那批预赛卷的题号
        # 因为带卷名前缀而一行都没认出来，于是整份正文全走到这里被吃掉，重庆
        # 那份只剩 14% 的字，页面上既不报错也没有提示，用户只看到题少了。
        # 方言那个具体毛病已经修了（见 _CN_TI_PREFIX_RE），但下一种没见过的
        # 题号写法一定还会走到这条分支——所以这里记账，让症状自己说话。
        elif drop_sink is not None:
            drop_sink.append(raw_line)

    flush()
    if section_lead and drop_sink is not None:
        drop_sink.extend(section_lead)
    blocks = _drop_boilerplate(blocks)
    _assign_zones(blocks, saw_ans_head)
    blocks = _recover_missing_number_boundaries(blocks)
    blocks = _recover_unnumbered_solution_details(blocks)
    blocks = _repair_duplicate_coordinates(blocks)
    blocks = _repair_single_gap_duplicate_number(blocks)
    blocks = _repair_trailing_shifted_duplicate_numbers(blocks)
    blocks = _repair_decimal_backtrack_noise(blocks)
    blocks = _repair_embedded_numbered_material(blocks)
    return blocks


def _recover_missing_number_boundaries(blocks: list[Block]) -> list[Block]:
    """利用相邻题号的单个空洞，找回被 OCR 吃掉分隔符的题界。

    正常题号必须有点号，不能全局接受 ``7 正文``：公式行、分值和步骤编号都可能
    同形。这里只在两个同区块题号恰好夹出一个缺号时，回看左块内部的三种强形态：
    行首 ``7 正文``；答案中被上一句粘住的 ``…基础题7.6``；以及整十题号 ``20.``
    被 OCR 成 ``(2)``、后面紧跟第 (1) 问。缺号与上下界共同约束，避免扩大主正则。
    """
    repaired: list[Block] = []
    for pos, block in enumerate(blocks):
        next_block = blocks[pos + 1] if pos + 1 < len(blocks) else None
        if not (next_block and isinstance(block.number, int)
                and isinstance(next_block.number, int)
                and next_block.number == block.number + 2
                and block.zone == next_block.zone
                and block.group == next_block.group):
            repaired.append(block)
            continue
        missing = block.number + 1
        same_section = block.section == next_block.section
        patterns = []
        if same_section:
            patterns.append(re.compile(rf"(?m)^\s*{missing}(?=\s+\S)"))
        if block.zone == "stem":
            # PDF 双栏/上下标抽取会把下一题整个粘进上一题同一行：
            # ``…(D) 选项 2. $z=…`` 或 ``…<sub>4. 已知…</sub>``。主题号正则
            # 不能全局接受行内 ``N.``（小数和公式里太常见），这里只在上下题号
            # 恰好夹出一个洞时，再要求题干以常见起句或公式开头，才补这道边界。
            stem_head = (
                r"(?:\$|已知|若|设|在|记|执行|如图|函数|下列|某|为了|"
                r"学生|复数|向量|数列|抛物线|椭圆|双曲线|正方体|给定|定义|有)"
            )
            # 旧卷文本层偶尔把上一行公式/答题括号的孤立右括号挤到下一题题号前，
            # 形成 ``) 16. 学生到工厂……``。主题号正则不能全局接受这种写法，
            # 否则解答步骤也可能被拆题；只在相邻块恰好缺这一号、同分区且后文有
            # 题干起句时，把右括号连题号一起作为边界吃掉。
            patterns.append(re.compile(
                rf"(?m)^\s*[)）]\s*{missing}\s*[.．、]\s*(?={stem_head})",
                re.I,
            ))
            patterns.append(re.compile(
                rf"(?<!\d)(?P<sub><sub(?:\s[^>]*)?>\s*)?"
                rf"{missing}\s*[.．、]\s*(?={stem_head})", re.I))
        if block.zone == "solution" and same_section:
            patterns.append(re.compile(rf"(?<=题){missing}\.(?=\S)"))
            # 解析册常把下一题题号粘在上一题最后一句后面：
            # ``……故D错误。10.（1）……\n\n【详解】``。不能全局认行内 N.，
            # 公式小数和步骤号太多；这里只在相邻解析号恰好夹出单洞时，再要求
            # 题号后 320 字内出现逐题“【详解/解析】”强标记，才把它补成边界。
            patterns.append(re.compile(
                rf"(?<![\d.]){missing}\s*[.．、]\s*"
                rf"(?=[^\r\n]{{0,320}}(?:\r?\n\s*){{0,3}}"
                rf"【\s*(?:详解|解析)\s*】)"
            ))
            if missing % 10 == 0:
                patterns.append(re.compile(
                    rf"(?m)^\s*\(\s*{missing // 10}\s*\)(?=\s*\(\s*1\s*\))"))
        match = next((m for pattern in patterns if (m := pattern.search(block.text))), None)
        if match is None:
            repaired.append(block)
            continue
        before = block.text[:match.start()].rstrip()
        after = block.text[match.end():].lstrip()
        matched_boundary = block.text[match.start():match.end()].lstrip()
        explicit_stray_paren = matched_boundary.startswith((")", "）"))
        if not same_section and not explicit_stray_paren:
            # 选择题末题可能与前题粘在同一行，而下一块已进入填空题分区。此时不能
            # 仅凭行内 ``8. 在……`` 就跨分区拆题；要求分界两侧各自都能可靠找到
            # 完整 A—D，既覆盖真实的“最后一道选择题”，也避开正文数字/小数。
            import mechfix
            if not (mechfix.has_complete_choice_options(before, known_choice=True)
                    and mechfix.has_complete_choice_options(
                        after, known_choice=True)):
                repaired.append(block)
                continue
        if match.groupdict().get("sub"):
            # 可选的 <sub> 是 MinerU 为整段误套的版式标签，不属于任何一道题；
            # 拆界时成对去掉，避免上一题留下开标签、下一题留下闭标签。
            after = re.sub(r"</sub>", "", after, count=1, flags=re.I)
        if not before or not after:
            repaired.append(block)
            continue
        block.text = before
        recovered = Block(
            index=0, number=missing, text=f"{missing}. {after}",
            section=block.section, group=block.group, zone=block.zone,
            line_no=block.line_no + block.text.count("\n") + 1,
            kind=_classify(after),
        )
        repaired.extend((block, recovered))
    for index, block in enumerate(repaired):
        block.index = index
    return repaired


def _recover_unnumbered_solution_details(blocks: list[Block]) -> list[Block]:
    """用题干号与第二个“【详解】”找回解析册中完全丢失的题号。

    有些页只吃掉新题的题号和短答案，却保留了独立的 ``【详解】``。这时切块结果
    会把两题并成一个解析块。只有三条证据同时成立才拆：当前块属于解析区；块内
    至少有两个逐题详解标记；题干序列证明当前位置到下一解析锚点之间（或卷尾）
    恰好缺少连续题号。恢复块从第二个强标记开始，短答案若已粘在前文则原样保留
    在前块——宁可少移动一小段答案，也不凭空猜哪一段公式属于下一题。
    """
    expected_by_group: dict[str | None, list[int]] = {}
    for block in blocks:
        if block.zone != "stem" or not isinstance(block.number, int):
            continue
        numbers = expected_by_group.setdefault(block.group, [])
        if block.number not in numbers:
            numbers.append(block.number)
    for numbers in expected_by_group.values():
        numbers.sort()

    solution_positions: dict[str | None, list[tuple[int, Block]]] = {}
    for pos, block in enumerate(blocks):
        if block.zone == "solution" and isinstance(block.number, int):
            solution_positions.setdefault(block.group, []).append((pos, block))

    recovered_at: dict[int, list[Block]] = {}
    for group, positioned in solution_positions.items():
        expected = expected_by_group.get(group) or []
        if not expected:
            continue
        for item_index, (pos, block) in enumerate(positioned):
            markers = list(_DETAIL_MARK_RE.finditer(block.text))
            extra_count = len(markers) - 1
            if extra_count <= 0:
                continue
            next_number = None
            if item_index + 1 < len(positioned):
                next_number = positioned[item_index + 1][1].number
            candidates = [
                number for number in expected
                if number > block.number
                and (next_number is None or number < next_number)
            ]
            # 只能从当前题号的下一题起连续补；否则“第二个详解”与缺号之间没有
            # 唯一对应关系，保持原块并交给校对，绝不按数量硬摊。
            wanted = candidates[:extra_count]
            if (len(wanted) != extra_count
                    or wanted != list(range(block.number + 1,
                                            block.number + 1 + extra_count))):
                continue

            split_points = [marker.start() for marker in markers[1:]]
            parts: list[str] = []
            start = 0
            for split_point in split_points:
                parts.append(block.text[start:split_point].rstrip())
                start = split_point
            parts.append(block.text[start:].lstrip())
            if not all(part.strip() for part in parts):
                continue

            block.text = parts[0]
            additions: list[Block] = []
            for offset, (number, text) in enumerate(zip(wanted, parts[1:]), 1):
                additions.append(Block(
                    index=0,
                    number=number,
                    text=f"{number}. {text}",
                    section=block.section,
                    group=block.group,
                    zone="solution",
                    line_no=block.line_no + block.text.count("\n") + offset,
                    kind="solution",
                ))
            recovered_at[pos] = additions

    repaired: list[Block] = []
    for pos, block in enumerate(blocks):
        repaired.append(block)
        repaired.extend(recovered_at.get(pos, []))
    for index, block in enumerate(repaired):
        block.index = index
    return repaired


_CHOICE_A_RE = re.compile(r"(?<![A-Za-z\\])A\s*[.．]\s*")
_LEADING_QUESTION_NUMBER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:[★☆]+\s*)?(?:[A-DＡ-Ｄ]{1,4}\s+)?(?:"
    r"\d{1,3}\s*[.．、)）]|[(（]\s*\d{1,3}\s*[)）]|"
    r"第\s*(?:\d{1,3}|[一二三四五六七八九十百零]+)\s*[题題]"
    r"\s*[.．、:：]?)\s*"
)


def _same_block_context(left: Block, right: Block) -> bool:
    """题号修复只能在同一题干/解析分区、同组、同题型分区内发生。"""
    return (
        left.zone == right.zone
        and left.group == right.group
        and left.section == right.section
    )


def _choice_stem_signature(text: str) -> str:
    """取得去掉题号与选项后的题干，用于证明两个同号块确为同一道题。"""
    option_match = _CHOICE_A_RE.search(text)
    stem = text[:option_match.start()] if option_match else text
    stem = _LEADING_QUESTION_NUMBER_RE.sub("", stem, count=1)
    return "".join(stem.split())


def _has_adjacent_number_evidence(blocks: list[Block], pos: int) -> bool:
    """同号双块前后至少一侧须有连续题号，排除正文中的偶然同号引用。"""
    current = blocks[pos]
    number = current.number
    if not isinstance(number, int):
        return False
    previous = blocks[pos - 1] if pos > 0 else None
    following = blocks[pos + 2] if pos + 2 < len(blocks) else None
    return bool(
        (previous is not None
         and _same_block_context(previous, current)
         and previous.number == number - 1)
        or
        (following is not None
         and _same_block_context(following, current)
         and following.number == number + 1)
    )


def _repair_duplicate_coordinates(blocks: list[Block]) -> list[Block]:
    """合并同坐标的互补题干，并丢掉高重合的短截断解析副本。

    强制 OCR 的双栏页会把同一道选择题识成两块：第一块题干完整但无选项，第二块
    题干截断却有完整 A—D。坐标相同且两块相邻时，把第一块正文与第二块选项拼回。
    解析册另有“同一答案先输出半句、随后又完整输出一遍”的形态；仅当短块不超过
    长块四分之一、两者前 120 个非空白字符相似度至少 0.70 时保留长块。
    """
    import mechfix

    repaired: list[Block] = []
    pos = 0
    while pos < len(blocks):
        first = blocks[pos]
        second = blocks[pos + 1] if pos + 1 < len(blocks) else None
        same_coordinate = (
            second is not None
            and isinstance(first.number, int)
            and first.number == second.number
            and _same_block_context(first, second)
        )
        if not same_coordinate:
            repaired.append(first)
            pos += 1
            continue

        # “依次编号为 1、2、\n3...24.” 里的续行会被 ``n.`` 方言把开头的
        # ``3.`` 误切成第二个第 3 题。两个点以上已经证明它是省略号/数列范围，
        # 再加上前一块同坐标且以枚举分隔符结尾，才允许把正文无损接回前块。
        # 普通两个不同的同号题不满足这两个限定条件，仍留给重复题号质量门处理。
        range_continuation = bool(
            first.zone == "stem"
            and re.match(
                rf"^\s*(?:#{{1,6}}\s*)?{first.number}\s*[.．]{{2,}}\s*\d",
                second.text,
            )
            and first.text.rstrip().endswith(("、", ",", "，"))
        )
        if range_continuation:
            first.text = first.text.rstrip() + second.text.lstrip()
            repaired.append(first)
            pos += 2
            continue

        if first.zone == "stem":
            first_complete = mechfix.has_complete_choice_options(
                first.text, known_choice=True)
            second_complete = mechfix.has_complete_choice_options(
                second.text, known_choice=True)
            if first_complete != second_complete:
                body = second if first_complete else first
                options = first if first_complete else second
                body_stem = _choice_stem_signature(body.text)
                option_stem = _choice_stem_signature(options.text)
                similarity = (
                    SequenceMatcher(None, body_stem, option_stem).ratio()
                    if min(len(body_stem), len(option_stem)) >= 30
                    else 0.0
                )
                option_match = _CHOICE_A_RE.search(options.text)
                if (option_match
                        and similarity >= 0.60
                        and _has_adjacent_number_evidence(blocks, pos)):
                    body.text = (body.text.rstrip() + "\n" +
                                 options.text[option_match.start():].lstrip())
                    repaired.append(body)
                    pos += 2
                    continue

        # 独立解析册没有题干块时，_undo_all_solution 会把整组 zone 回退成
        # stem，避免把普通题干误判为“全是解析”。但两个同号块各自都带
        # “【详解/解析】”强标记时，块内证据已经足以证明这是解析副本；继续
        # 沿用长度与前缀相似度双门，不能让 zone 回退漏掉同题的截断副本。
        strong_detail_duplicate = bool(
            _DETAIL_MARK_RE.search(first.text)
            and _DETAIL_MARK_RE.search(second.text)
        )
        if first.zone == "solution" or strong_detail_duplicate:
            left = "".join(first.text.split())
            right = "".join(second.text.split())
            short, long = ((first, second) if len(left) <= len(right)
                           else (second, first))
            short_text = left if short is first else right
            long_text = right if long is second else left
            sample = min(120, len(short_text), len(long_text))
            similarity = (SequenceMatcher(
                None, short_text[:sample], long_text[:sample]).ratio()
                if sample >= 40 else 0.0)
            if (len(short_text) * 4 <= len(long_text)
                    and similarity >= 0.70):
                repaired.append(long)
                pos += 2
                continue

        repaired.append(first)
        pos += 1

    for index, block in enumerate(repaired):
        block.index = index
    return repaired


def _repair_single_gap_duplicate_number(blocks: list[Block]) -> list[Block]:
    """把唯一的 ``9, 11, 11, 12`` 型 OCR 错号恢复为 ``9,10,11,12``。

    这不是看到重复号就改：必须四块相邻、同区同组同分区，且同时满足
    ``n, n+2, n+2, n+3``；同一上下文还不能已有 ``n+1``，才可证明第一条
    重复号正好占据唯一缺口。
    """
    for pos in range(len(blocks) - 3):
        a, b, c, d = blocks[pos:pos + 4]
        same_context = all(
            _same_block_context(a, item) for item in (b, c, d)
        )
        if not same_context or not all(
                isinstance(item.number, int) for item in (a, b, c, d)):
            continue
        if (b.number == a.number + 2
                and c.number == b.number
                and d.number == b.number + 1):
            missing = a.number + 1
            if any(
                    item.number == missing
                    and _same_block_context(a, item)
                    for item in blocks):
                continue
            old = b.number
            b.number = missing
            b.text = re.sub(
                rf"^(\s*(?:#{{1,6}}\s*)?){old}(\s*[.．、])",
                rf"\g<1>{missing}\g<2>",
                b.text,
                count=1,
            )
    for index, block in enumerate(blocks):
        block.index = index
    return blocks


def _repair_trailing_shifted_duplicate_numbers(blocks: list[Block]) -> list[Block]:
    """用完整解析题号证明并修复题干尾段整体少 1 的 OCR 错号。

    典型形态是题干 ``1..10,10,11``，而同组解析完整给出 ``1..12``。这里只在
    题干除唯一相邻重复点外严格连续、重复点后的整个尾段都恰好少 1，且解析侧
    完整覆盖 ``1..题干块数`` 时顺延尾段。缺解析时不猜，普通同号题也不会被改。
    """
    stem_groups: dict[str | None, list[Block]] = {}
    solution_groups: dict[str | None, list[Block]] = {}
    for block in blocks:
        if block.zone == "stem":
            stem_groups.setdefault(block.group, []).append(block)
        elif block.zone == "solution":
            solution_groups.setdefault(block.group, []).append(block)

    for group, stems in stem_groups.items():
        numbers = [block.number for block in stems]
        if len(numbers) < 3 or not all(isinstance(number, int)
                                       for number in numbers):
            continue
        duplicates = [pos for pos in range(1, len(numbers))
                      if numbers[pos] == numbers[pos - 1]]
        if len(duplicates) != 1:
            continue
        cut = duplicates[0]
        repeated = numbers[cut]
        if (numbers[:cut] != list(range(1, repeated + 1))
                or numbers[cut:] != list(range(repeated, len(numbers)))):
            continue
        solution_numbers = [
            block.number for block in solution_groups.get(group, [])
            if isinstance(block.number, int)
        ]
        if (len(solution_numbers) != len(stems)
                or sorted(solution_numbers) != list(range(1, len(stems) + 1))):
            continue

        for block in stems[cut:]:
            old = block.number
            block.number = old + 1
            block.text = re.sub(
                rf"^(\s*(?:#{{1,6}}\s*)?){old}(\s*[.．、)）])",
                rf"\g<1>{old + 1}\g<2>",
                block.text,
                count=1,
            )
    for index, block in enumerate(blocks):
        block.index = index
    return blocks


_DECIMAL_BACKTRACK_RE = re.compile(r"^\s*\d{1,3}[.．]\d+")
_DECIMAL_FRAGMENT_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\d{1,3}[.．]\d+(?:\s+[\d.．]+)*\s*$")


def _repair_decimal_backtrack_noise(blocks: list[Block]) -> list[Block]:
    """把夹在连续题号之间、形如 ``8.0 ...`` 的 OCR 小数碎片并回前一题。

    《高考必刷题椭圆》第 14 题下方的背面透字被识成 ``8.0 \\partial ...``，主题号
    正则把开头的 ``8.`` 当成新题，序列变成 14、8、15；质量检查随即误报缺 9~14，
    校对页也多出一道垃圾题。不能全局禁止 ``数字.数字``，因为 ``1.2026 年...``
    可能真是“第 1 题正文以年份开头”。这里只在下一号恰好续接前一号、上下文相同
    且当前首行紧贴成小数写法时处理；常规要求题号回退，若整块只有一行纯数字小数
    （如 ``3.75 0.25``），也允许处理夹在 1、2 之间的向前误切。内容并回而不删除，
    保持机械层“宁可留噪声给人校对，也不猜删原文”的边界。
    """
    repaired: list[Block] = []
    for pos, block in enumerate(blocks):
        previous = repaired[-1] if repaired else None
        next_block = blocks[pos + 1] if pos + 1 < len(blocks) else None
        is_sandwiched = (
            previous is not None and next_block is not None
            and isinstance(previous.number, int)
            and isinstance(block.number, int)
            and isinstance(next_block.number, int)
            and (block.number < previous.number
                 or _DECIMAL_FRAGMENT_LINE_RE.fullmatch(block.text))
            and next_block.number == previous.number + 1
            and block.zone == previous.zone
            and block.group == previous.group
            and _DECIMAL_BACKTRACK_RE.match(block.text)
        )
        if is_sandwiched:
            previous.text = previous.text.rstrip() + "\n\n" + block.text.lstrip()
            continue
        repaired.append(block)
    for index, block in enumerate(repaired):
        block.index = index
    return repaired


def _repair_embedded_numbered_material(blocks: list[Block]) -> list[Block]:
    """合并解析中误切出的编号材料，并跳过明确标注的备用题。

    教师版资料会在一道题的答案后附「题目来源 / 知识考查 / 思路分析」，甚至把教材
    原题全文贴进解析；教材原题仍以 ``6.`` 这类题号开头。它与正式题的区别有三个
    同时成立的强信号：前一正式块已经出现答案/解析标记、两块仍在同一题型分区、
    新题号不大于前一正式题号。三条缺一不可，避免吞掉卷末另起的答案区或下一题型。

    ``14.(改编备用)`` 是另一种教师资料：它明确不是试卷采用题。若它紧跟在已含
    解析的同号正式题之后，则整块跳过；否则不凭「备用」两个字全局删除，防止误伤
    题干中讨论备用方案的正常题目。
    """
    repaired: list[Block] = []
    for block in blocks:
        if not repaired:
            repaired.append(block)
            continue
        previous = repaired[-1]
        same_section = block.section == previous.section
        numbered_backtrack = (
            isinstance(previous.number, int)
            and isinstance(block.number, int)
            and block.number <= previous.number
        )
        previous_has_solution = bool(_INLINE_SOL_MARK_RE.search(previous.text))
        same_stem_zone = previous.zone == block.zone == "stem"
        if same_section and numbered_backtrack and previous_has_solution and same_stem_zone:
            first_line = _strip_head(block.text.strip().splitlines()[0])
            if "备用" not in first_line:
                previous.text = previous.text.rstrip() + "\n\n" + block.text.lstrip()
            continue
        repaired.append(block)
    for index, block in enumerate(repaired):
        block.index = index
    return repaired


def split_blocks(raw_md: str, *, num_template: str = "") -> list[Block]:
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

    num_template 非空时按用户写的题号模板钉死方言（语法见 compile_dialect），
    自动判方言与三档降级都不再参与——降级的意义是「我猜不准，换个宽度再试」，
    而用户已经明确说了题号长什么样，再降级只会把他指定的口径悄悄换掉。

    但**钉死的模板切不出 ≥2 块时仍回退到自动**：选错模板（写了 `第x题` 而卷子
    其实是 `x.`）在自动路径下最多是切得不够好，在钉死路径下会退化成一个巨块，
    比不指定更糟。回退这件事必须让用户知道，所以由调用方拿
    `split_blocks_with_note` 取回说明，而不是静默咽下。
    """
    blocks, _note = split_blocks_with_note(raw_md, num_template=num_template)
    return blocks


# 丢弃行占全文比例超过这个值就提示用户（见 _drop_note）。
#
# 在 55 份真实产物上标定（2026-08-06）：正常文档最高 0.0896（那份的丢弃行是答案
# 速查表 `<table>` 和图片行，本来就不该进块），其余全在 0.056 以下、44 份为 0；
# 而 2026-08-06 真出事的两份预赛卷（钉死改动前的方言复现）是 0.86 和 0.79。
# 两侧各留 3 倍余量，0.25 落在这条很宽的沟里。宁可漏报也不误报：这条提示要是
# 在正常卷子上天天出现，用户下次就不看了，那比没有提示更糟。
_DROP_NOTE_RATIO = 0.25


def _drop_note(raw_md: str, dropped: list[str]) -> str:
    """丢弃量超阈值时给一句用户看得懂的话，否则空串。

    比的是**非空字符数**而不是行数：MinerU 的换行完全取决于版面，一道题可能占
    1 行也可能占 8 行，行数比不出严重程度。
    """
    if not dropped:
        return ""
    lost = len("".join("".join(l.split()) for l in dropped))
    whole = len("".join(raw_md.split()))
    if not whole or lost / whole < _DROP_NOTE_RATIO:
        return ""
    logger.warning("切块丢弃 %d 行 / %d 字（占全文 %.1f%%），已提示用户",
                   len(dropped), lost, 100 * lost / whole)
    # 这里不是一般版式提示：这些正文已明确不会进入任何题目。统一标记后，批量
    # “不审核直接入库”会保留识别草稿并暂停，避免以成功状态吞掉整段内容。
    import qualcheck
    return qualcheck.mark_manual_review(
        f"这份文档有 {len(dropped)} 行正文（约 {lost} 字，"
        f"占全文 {round(100 * lost / whole)}%）没能归入任何一道题，已被丢弃——"
        f"通常是题号写法不认识、切不出题号导致的。"
        f"请对照原文检查是否缺题，必要时在「题号格式」里手动指定题号写法。")


def split_blocks_with_note(raw_md: str, *,
                           num_template: str = "") -> tuple[list[Block], str]:
    """同 split_blocks，另返回一句给用户看的说明（正常时为空串）。

    说明有两种：
      ① 指定的题号模板没切开、已回退自动判定。校对页把它显示出来，用户才知道
         「我指定的模板没生效」，而不是对着一个巨块猜哪里出了问题；
      ② 首个题号之前的正文被大量丢弃（见 _split_pass 末尾与 _drop_note）。
    两种能同时成立，那就都说——它们指向的是同一件事的不同侧面。

    记账只取**胜出那一趟**的丢弃量：三档降级里落选的趟丢了多少与用户看到的结果
    无关，混进来只会虚报。marker 档没有那条丢弃分支（它按标记切，不需要先见到
    题号），所以走到第三档时天然没有这类丢弃可报。
    """
    if not raw_md or not raw_md.strip():
        return [], ""
    raw_md = _expand_answer_key_structures(raw_md)

    if num_template.strip():
        # 模板非法时不静默忽略：编译错误直接抛给调用方（路由层转成表单报错），
        # 免得用户以为模板生效了、实际上跑的是自动判定。
        pinned = compile_dialect(num_template)
        sink: list[str] = []
        blocks = _split_pass(raw_md, loose=False, dialect=pinned,
                             drop_sink=sink)
        if len(blocks) >= 2:
            logger.info("按题号模板 %r 切出 %d 块", num_template, len(blocks))
            return blocks, _drop_note(raw_md, sink)
        logger.warning("题号模板 %r 只切出 %d 块，回退自动判定",
                       num_template, len(blocks))
        auto, auto_note = split_blocks_with_note(raw_md)
        note = (f"按你指定的题号模板「{num_template}」只切出 {len(blocks)} 块，"
                f"已回退到自动判定（切出 {len(auto)} 块）。"
                f"模板可能与这份文档的题号写法不符。")
        return auto, f"{note}{auto_note}" if auto_note else note

    sink = []
    blocks = _split_pass(raw_md, loose=False, drop_sink=sink)
    if len(blocks) >= 2:
        return blocks, _drop_note(raw_md, sink)
    loose_sink: list[str] = []
    loose = _split_pass(raw_md, loose=True, drop_sink=loose_sink)
    if _loose_only_splits_strict_subquestions(blocks, loose):
        return blocks, _drop_note(raw_md, sink)
    if len(loose) >= 2:
        return loose, _drop_note(raw_md, loose_sink)
    marker = _split_by_markers(raw_md)
    if len(marker) >= 2:
        return marker, ""
    if blocks:
        return blocks, _drop_note(raw_md, sink)
    if loose:
        return loose, _drop_note(raw_md, loose_sink)
    return marker, ""


def _loose_only_splits_strict_subquestions(strict: list[Block],
                                            loose: list[Block]) -> bool:
    """判断宽松档是否只把一个有效主题内的连续括号小问拆成了新题。

    单张图片可能只有一道 ``19.`` 主问题，内部再列 ``(1)``、``(2)``、``(3)``。
    严格档完整切出一个块本来就是成功；旧降级条件只看块数不足 2，会改走宽松档，
    把三问误判成三道顶层题。这里只在下列证据同时成立时保留严格结果：严格档已有
    一个带题号的主块；宽松档首块仍是同一主块；余下块全部由从 1 开始连续递增的
    ``1)``/``(1)`` 行切出；两趟正文拼回后逐字（忽略空白）一致。

    真正以 ``1)``/``(1)`` 为顶层题号的文档在严格档没有主块，因而不会命中本规则，
    仍按宽松档正常切分。一个小问也要保留：单题截图可能只问一问，若仍要求至少
    两个，``19.`` 加 ``(1)`` 会被错误保存成第 19 题和第 1 题两道题。
    """
    if len(strict) != 1 or len(loose) < 2:
        return False
    main = strict[0]
    if main.number is None or loose[0].number != main.number:
        return False
    if loose[0].line_no != main.line_no:
        return False

    sub_numbers: list[int] = []
    for block in loose[1:]:
        first_line = block.text.strip().splitlines()[0] if block.text.strip() else ""
        match = _LOOSE_SUBQUESTION_HEAD_RE.match(_strip_head(first_line))
        if not match:
            return False
        number = int(match.group(1) or match.group(2))
        if block.number != number:
            return False
        sub_numbers.append(number)
    if sub_numbers != list(range(1, len(sub_numbers) + 1)):
        return False

    compact_strict = "".join(main.text.split())
    compact_loose = "".join("".join(block.text.split()) for block in loose)
    return compact_loose == compact_strict


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
    section_marker = object()
    current_section = section_marker
    for i, b in enumerate(blocks):
        if b.number is None or b.group is not None:
            continue
        # 新分区可以合法地从 1 重新编号。旧实现把全文只维护一个 seen_max，导致
        # 教辅书下一练习组的 1~N 被当成答案区；解析区没有新分区标题，仍会在同一
        # section 内回到 1，因此原有判定能力不受影响。
        if b.section != current_section:
            current_section = b.section
            seen_max = 0
        if b.number == 1 and seen_max >= 5 and (len(blocks) - i) >= 3:
            return i
        seen_max = max(seen_max, b.number)
    return None
