"""机械排版转换：只做**无需判断**的那一部分，其余留给逐块 LLM。

为什么要划这条线：templates/normalize_prompt.md 里的排版规范有二十来条，其中
一部分是纯字符级重写（`\\frac`→`\\dfrac`、中文句号→英文点号），LLM 做这些既慢
又偶尔漏；另一部分需要语义判断（裸字母包裹要区分「点 A」与「A 组」的 A、选项
按长度分行、表格转管道表格），机械做必然出错。

所以这里只收安全子集，且刻意**不碰**下面这些（交给 LLM 在单块上做，比现在在
整篇上做更准）：
  - 裸拉丁字母包裹 `$\\displaystyle $`：`点A` 要包，`A 组`/`A. 选项` 的 A 不能同等对待；
  - 选项按长度分行对齐：要数视觉宽度；
  - `<table>` → Markdown 管道表格：要判合并单元格；
  - 填空题末尾补 `___`：得先知道它是不是填空题。

**"裸字母包 `$`" 这条 2026-08-07 在 45 份真实 MinerU 产物上量过，结论是必须继续
不做**（别再凭 normalize_prompt 第 3 条那句"死命令"重开）：公式外的裸单字母共
4104 处，按上下文分类后能安全包的只有「行首选项标签 `A.`」52 处（1.3%）。剩下
64% 是 `_split_math` 认不出的破损公式碎片（MinerU 会输出 `P _ {2 0 2 5}` 这种
带空格的形态，`$` 配不上就整段落到"公式外"）、17% 是图片文件名里的十六进制串、
10% 是 `A 组`/`k 步` 这类不该包的。盲包会把 734 个图片哈希和 2647 个公式碎片
包进 `$`，直接改坏正文——收益 1.3%、代价是改坏 98%。

所有替换都避开 `$...$` 内部还是刻意作用于其内部，逐条在函数注释里写明——
公式内外规则相反的地方（中文括号）弄错会直接改坏 LaTeX。
"""

import re

# 行内公式片段：非贪婪匹配一对 `$`，用于把正文与公式分段处理。
# 不处理 `$$`（行间公式）——normalize_prompt 要求转成行内，但那是语义改写，
# 留给 LLM；这里遇到 `$$` 原样放过，不去猜它的边界。
_INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)((?:[^$\\]|\\.)*)\$(?!\$)")

# MinerU 会用 HTML ``<sub>`` 表示它从 PDF 字形位置推断出的“下沉文本”。这并不
# 等同于数学下标：``a<sub>n</sub>`` 通常是下标，但 ``<sub>中,</sub>``、
# ``<sub>+</sub>`` 乃至整句被套住也很常见。页面端不能直接信任 OCR HTML，因此
# 标签若不在入库前归一化，最终会以字面量 ``<sub>`` 显示。
_HTML_SUB_RE = re.compile(
    r"<sub(?:\s[^>]*)?>(?P<body>[\s\S]*?)</sub\s*>", re.I)
_HTML_SUB_TAG_RE = re.compile(r"</?sub(?:\s[^>]*)?>", re.I)
_HTML_SUP_RE = re.compile(
    r"<sup(?:\s[^>]*)?>(?P<body>[\s\S]*?)</sup\s*>", re.I)
_HTML_SUP_TAG_RE = re.compile(r"</?sup(?:\s[^>]*)?>", re.I)
_MATH_WITH_SUB_RE = re.compile(
    r"(?P<base>(?<!\$)\$(?!\$)(?:[^$\\]|\\.)+\$(?!\$))"
    r"(?P<space>[ \t　]*)"
    r"<sub(?:\s[^>]*)?>(?P<body>[\s\S]*?)</sub\s*>", re.I)
_RADICAL_WITH_SUB_RE = re.compile(
    r"(?P<root>[√])(?P<space>[ \t　]*)"
    r"<sub(?:\s[^>]*)?>(?P<body>[\s\S]*?)</sub\s*>", re.I)
# 纯文本基符只收能机械确认的几类。小写字母限定为单字符，避免把
# ``tan<sub>γ</sub>`` 误写成 ``tan_γ``；大写双字母兼容 ``AA<sub>1</sub>`` 这类
# 几何记号。其余无法确认基符的标签只剥外壳、保留内容。
_PLAIN_WITH_SUB_RE = re.compile(
    r"(?<![0-9A-Za-z])(?P<base>[A-Z]{1,4}|[a-z]|[α-ωΑ-Ωξζηλμ]|∁)"
    r"(?i:<sub(?:\s[^>]*)?>)(?P<body>[\s\S]*?)(?i:</sub\s*>)")
_MATH_WITH_SUP_RE = re.compile(
    r"(?P<base>(?<!\$)\$(?!\$)(?:[^$\\]|\\.)+\$(?!\$))"
    r"(?P<space>[ \t　]*)"
    r"<sup(?:\s[^>]*)?>(?P<body>[\s\S]*?)</sup\s*>", re.I)
_PLAIN_WITH_SUP_RE = re.compile(
    r"(?<![0-9A-Za-z])(?P<base>[0-9A-Z]|[a-z]|[α-ωΑ-Ωξζηλμ])"
    r"(?i:<sup(?:\s[^>]*)?>)(?P<body>[\s\S]*?)(?i:</sup\s*>)")
_IMAGE_WITH_SUP_RE = re.compile(
    r"(?P<image>!\[[^\]]*\]\([^)]*\))[ \t　]*"
    r"<sup(?:\s[^>]*)?>\s*(?P<body>[A-Za-z])\s*</sup\s*>", re.I)
_VECTOR_WITH_SUP_ARTIFACT_RE = re.compile(
    r"<sup(?:\s[^>]*)?>\s*#\s*</sup\s*>\s*"
    r"<sup(?:\s[^>]*)?>\s*[»→]\s*</sup\s*>\s*"
    r"(?P<points>[A-Z]{2,3})", re.I)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SUB_TRAILING_PUNCT_RE = re.compile(r"^(?P<body>[\s\S]*?)(?P<punct>[,.;:，。；：]?)$")
_LEADING_RADICAND_RE = re.compile(
    r"^\s*(?P<body>(?<!\$)\$(?!\$)(?:[^$\\]|\\.)+\$(?!\$)|\d+(?:\.\d+)?)"
    r"(?P<tail>[\s\S]+)$")
_SAFE_SUB_PAYLOAD_RE = re.compile(
    r"(?:[0-9A-Za-zα-ωΑ-Ωξζηλμ]+|"
    r"[0-9A-Za-zα-ωΑ-Ωξζηλμ]+(?:\s*[+\-−]\s*"
    r"[0-9A-Za-zα-ωΑ-Ωξζηλμ]+)+|"
    r"\\[A-Za-z]+(?:\s*\{[^{}]+\})?)$")

# 双栏/分栏抽取有时把约束方程组的最后一行排到“最大值是/最小值是”后面，例如
# ``满足约束条件 {x+y>=2, x+2y<=4}，则 z 的最大值是 y>=0``。最值答案不可能是
# 一个不等式；再要求紧邻的前文确有 aligned 约束组，才能安全把它移回组内。
_MISPLACED_CONSTRAINT_RE = re.compile(
    r"(?P<lead>满足约束条件\s*)"
    r"(?P<open>\$(?:\\displaystyle\s*)?\\left\\\{\\begin\{aligned\})"
    r"(?P<rows>[\s\S]*?)"
    r"(?P<close>\\end\{aligned\}\\right\.\$)"
    r"(?P<prompt>\s*则[\s\S]{0,240}?(?:最大|最小)值是\s*)"
    r"\$(?:\\displaystyle\s*)?"
    r"(?P<constraint>[A-Za-z](?:\s*_\s*\{[^{}]+\})?\s*"
    r"(?:\\(?:geqslant|leqslant|geq|leq)|[<>≥≤])\s*"
    r"(?:[-+−]?\s*\d+(?:\.\d+)?|[A-Za-z]))\s*[,，]?\$[ \t　]*[,，]?",
    re.I,
)
_CONSTRAINT_PARTS_RE = re.compile(
    r"^(?P<lhs>[A-Za-z](?:\s*_\s*\{[^{}]+\})?)\s*"
    r"(?P<op>\\(?:geqslant|leqslant|geq|leq)|[<>≥≤])\s*"
    r"(?P<rhs>[\s\S]+)$",
    re.I,
)

# 图片引用：`![alt](path)`
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

# MinerU 的下划线填空位：`\_\_\_\_` / `____` / 全角 `＿＿`，统一成三个半角下划线
_BLANK_RE = re.compile(r"(?:\\?_){3,}|＿{2,}")

# 行首小问序号误用半角括号：MinerU 偶尔把 （1）（i）等小问标记 OCR 成半角
# `(1)` `(i)`，导出/展示端的 _SUBQ_LINE_RE 只认全角 （），半角的会被当成普通正文，
# 小问不换行、不缩进。只在行首匹配（同 exporter._SUBQ_LINE_RE 的口径），避免把
# `f(1)=2` 这类函数值引用误改成 `f（1）=2`。
_HALFWIDTH_SUBQ_RE = re.compile(
    r"^([ \t　]*)\(\s*([0-9]+|[ivxIVX]+)\s*\)")

# 公式外的中文标点 → 半角（normalize_prompt 排版规范第 1 条要求"一律英文标点"，
# 但那条提示词只点了句号，真实产物里逗号 3910 处、冒号 486、分号 163 全没管）。
# **句号不在这张表里**：它要连带删行末、替换成 `. `（带空格），规则不同，仍归
# fix_period 单独处理。冒号后不补空格——`解：` 这种后面直接接正文，补了反而散。
_CN_PUNCT = {"，": ", ", "；": "; ", "：": ": ", "？": "? ", "！": "! "}
_CN_PUNCT_RE = re.compile("[" + "".join(_CN_PUNCT) + "]")

# 函数名裸写 → 带反斜杠的命令（只在公式内）。MinerU 绝大多数已经输出 `\cos`，
# 但 45 份产物里仍有裸 `tan` 5 处；更要紧的是 AI 那条路会**制造**裸 `max`/`min`
# （实测输出里裸 max 4 处、裸 min 7 处），机械补比 LLM 稳。
# 只收 LaTeX 真有同名命令的那些——写 `\sec` 没问题，写 `\arccot` 会编译报错。
_MATH_FUNCS = ("arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "coth",
               "sin", "cos", "tan", "cot", "sec", "csc",
               "log", "ln", "lg", "exp", "max", "min", "sup", "inf",
               "lim", "det", "gcd", "deg", "dim", "ker", "hom", "arg")
# 前向断言挡住三种误伤：① 已带反斜杠的 `\cos`；② 更长单词的一部分（`cosh` 的
# `cos`、`arcsin` 的 `sin`——所以上面的元组按长度降序排，长的先匹配）；
# ③ 紧跟在字母后的（变量名 `xmin`）。后向 `(?![a-zA-Z])` 同理。
_FUNC_RE = re.compile(
    r"(?<![\\A-Za-z])(" + "|".join(_MATH_FUNCS) + r")(?![a-zA-Z])")

# 数域字体统一成 `\mathbb`（只在公式内）。MinerU 的字体判定完全随机，同一份卷子里
# `\mathbf{R}` 84 处、`\mathbb{R}` 75 处；AI 那条路不但不统一，还会加剧偏斜
# （输出 `\mathbf{R}` 359 / `\mathbb{R}` 79），normalize_prompt 里也没有这条规则。
# 所以这是机械补的，两条路都吃得到。`exam_template.tex` 已经 `\usepackage{amssymb}`。
_SET_FONTS = ("mathbb", "mathbf", "mathrm", "textbf", "boldsymbol", "mathcal")
# R/N/Z/Q 无条件转。看着"没有集合算符"的那 123 处 R 全是 `定义在 $\mathbf{R}$ 上`
# 这类，仍旧是数域，没有第二种含义。
_SET_RE = re.compile(
    r"\\(?:" + "|".join(_SET_FONTS) + r")\s*\{\s*([NZQR])\s*\}")
# **C 必须看左邻**（2026-08-07 标定）：142 处 `\mathrm{C}` 里 130 处是组合数
# `\mathrm{C}_5^3`，只有 12 处是复数集。无条件转会把组合数写成 `\mathbb{C}_5^3`，
# 意思全变。判据取「紧跟在集合算符后面」——`z \in \mathbf{C}` 转，`\mathrm{C}_n^k`
# 不转。组合数永远带下标，也永远不跟在 `\in` 后面，这条差别是硬的。
#
# **`\times` 刻意不在这张表里**：它对集合是笛卡尔积、对组合数是普通乘号，而真实
# 产物里全是后者——`\mathrm{C}_5^3 \times \mathrm{C}_5^2` 这种排列组合算式。带上
# `\times` 会多误伤 2 处、一个都救不回（去掉它这条规则在标定集上是 7/7 全对、
# 0 误伤）。同理没收 `\to`：`h: 2^E \to \mathbf{N}` 那种值域标注也不该改字体。
_SET_C_RE = re.compile(
    r"((?:\\in|\\notin|\\subseteq|\\subsetneq|\\subset|\\cup|\\cap|∈)\s*)"
    r"\\(?:" + "|".join(_SET_FONTS) + r")\s*\{\s*C\s*\}")


def _split_math(text: str):
    """把文本切成 [(是否公式, 片段), …]，供只改正文或只改公式的规则使用。"""
    parts: list[tuple[bool, str]] = []
    pos = 0
    for m in _INLINE_MATH_RE.finditer(text):
        if m.start() > pos:
            parts.append((False, text[pos:m.start()]))
        parts.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        parts.append((False, text[pos:]))
    return parts


def _map_parts(text: str, on_text=None, on_math=None) -> str:
    """按 _split_math 分段后分别施加 on_text / on_math，再拼回。"""
    out = []
    for is_math, seg in _split_math(text):
        fn = on_math if is_math else on_text
        out.append(fn(seg) if fn else seg)
    return "".join(out)


def _sub_payload(raw: str) -> tuple[str | None, str]:
    """拆出 ``<sub>`` 内可确认的数学负载与尾随标点。

    返回 ``(None, "")`` 表示它不是安全下标。已有行内公式外壳只拆最外层一对，
    ``\\displaystyle`` 也在这里去掉，避免生成嵌套 ``$``。
    """
    raw = raw.strip()
    match = _SUB_TRAILING_PUNCT_RE.match(raw)
    if not match:
        return None, ""
    body = match.group("body").strip()
    punct = match.group("punct")
    math = _INLINE_MATH_RE.fullmatch(body)
    if math:
        body = re.sub(r"^\s*\\displaystyle\s*", "", math.group(1)).strip()
    if (_CJK_RE.search(body) or not body
            or not _SAFE_SUB_PAYLOAD_RE.fullmatch(body)):
        return None, ""
    return body, punct


def _append_subscript(base_math: str, payload: str, punct: str) -> str:
    inner = base_math[1:-1].strip()
    # MinerU 偶尔在已经识别出的 LaTeX 下标后再重复附一份 HTML 下标，例如
    # ``$a _ { \mathrm { ~ i ~ } }$<sub>i</sub>``。再次追加会生成非法/歧义双下标。
    # 只比较最后一个 ``_`` 之后的压平文本；尾部还有其它算式时自然不会相等。
    tail = inner.rsplit("_", 1)[-1] if "_" in inner else ""
    flatten = lambda value: re.sub(
        r"[{}~\\\s]", "", re.sub(
            r"\\(?:displaystyle|mathrm|mathbf|mathit|text)\b", "", value))
    if tail and flatten(tail) == flatten(payload):
        return base_math + punct
    return f"${inner}_{{{payload}}}${punct}"


def normalize_html_subscripts(text: str) -> str:
    """把 MinerU 的 ``<sub>`` 归一化成 LaTeX，异常套层只去标签不删正文。

    用户规则的安全化口径：

    - 标签内容含中文：一定不是当前数学卷里的可靠下标，直接剥标签、保留正文；
    - 不含中文且“基符 + 下标负载”都可确认：转成 LaTeX ``_{...}``；
    - ``√<sub>3</sub>`` 是 MinerU 常见根号错位，恢复成 ``\\sqrt{3}``；
    - 无基符、只有标点/运算符或其它无法确认的情况：只剥标签，不猜数学语义。

    函数可重复执行；第一次处理完后不会残留成对 ``<sub>``。
    """
    if not text or "<sub" not in text.lower():
        return text

    def math_sub(match: re.Match) -> str:
        raw = match.group("body")
        if _CJK_RE.search(raw):
            return match.group("base") + match.group("space") + raw
        payload, punct = _sub_payload(raw)
        if payload is None:
            return match.group("base") + match.group("space") + raw
        return _append_subscript(match.group("base"), payload, punct)

    def radical_sub(match: re.Match) -> str:
        raw = match.group("body")
        payload, punct = _sub_payload(raw)
        if payload is not None:
            return f"$\\sqrt{{{payload}}}${punct}"
        # 真实卷常见 ``√<sub>3 的直线…</sub>`` 和 ``√<sub>2, 0), F(</sub>``：
        # 整段误套，但最前面的数字仍是可确认的被开方数。无论后段是否含中文都可
        # 提取；只收数字/完整行内公式前缀，字母串不猜，避免把 ``√3b`` 擅自解释
        # 成 ``√(3b)``。
        leading = _LEADING_RADICAND_RE.match(raw)
        if leading:
            payload, punct = _sub_payload(leading.group("body"))
            if payload is not None:
                return (f"$\\sqrt{{{payload}}}${punct}"
                        + leading.group("tail"))
        return match.group("root") + match.group("space") + raw

    def plain_sub(match: re.Match) -> str:
        raw = match.group("body")
        if _CJK_RE.search(raw):
            return match.group("base") + raw
        payload, punct = _sub_payload(raw)
        if payload is None:
            return match.group("base") + raw
        return f"${match.group('base')}_{{{payload}}}${punct}"

    text = _MATH_WITH_SUB_RE.sub(math_sub, text)
    text = _RADICAL_WITH_SUB_RE.sub(radical_sub, text)
    text = _PLAIN_WITH_SUB_RE.sub(plain_sub, text)
    # 剩余标签都无法确认基符或数学负载；只去外壳，绝不删除 OCR 正文。
    text = _HTML_SUB_RE.sub(lambda m: m.group("body"), text)
    # 极少数截断 OCR 只留下单边标签；外壳本身没有可保留信息，去掉后正文仍完整。
    return _HTML_SUB_TAG_RE.sub("", text)


def _append_superscript(base_math: str, payload: str, punct: str) -> str:
    inner = base_math[1:-1].strip()
    tail = inner.rsplit("^", 1)[-1] if "^" in inner else ""
    flatten = lambda value: re.sub(
        r"[{}~\\\s]", "", re.sub(
            r"\\(?:displaystyle|mathrm|mathbf|mathit|text)\b", "", value))
    if tail and flatten(tail) == flatten(payload):
        return base_math + punct
    return f"${inner}^{{{payload}}}${punct}"


def normalize_html_superscripts(text: str) -> str:
    """把 MinerU 的 ``<sup>`` 安全归一化；无法确认基符时只剥标签保留正文。"""
    if not text or "<sup" not in text.lower():
        return text

    def math_sup(match: re.Match) -> str:
        raw = match.group("body")
        if _CJK_RE.search(raw):
            return match.group("base") + match.group("space") + raw
        payload, punct = _sub_payload(raw)
        if payload is None:
            return match.group("base") + match.group("space") + raw
        return _append_superscript(match.group("base"), payload, punct)

    def plain_sup(match: re.Match) -> str:
        raw = match.group("body")
        payload, punct = _sub_payload(raw)
        if payload is None:
            return match.group("base") + raw
        return _append_superscript(f"${match.group('base')}$", payload, punct)

    # PDF 文本层偶尔把向量箭头拆成两个上浮字形 ``#``、``»``；该字形对后面紧跟
    # 2~3 个大写点名时语义唯一，可安全恢复为向量。必须在通用去标签之前处理。
    text = _VECTOR_WITH_SUP_ARTIFACT_RE.sub(
        lambda match: f"$\\overrightarrow{{{match.group('points')}}}$", text)
    # 紧跟图片的单字母上标通常是图内点名被 OCR 重复抽出；图片本身已保留该字母，
    # 再把它当正文留下只会在选项图后多出一个孤立字符。
    text = _IMAGE_WITH_SUP_RE.sub(lambda match: match.group("image"), text)
    text = _MATH_WITH_SUP_RE.sub(math_sup, text)
    text = _PLAIN_WITH_SUP_RE.sub(plain_sup, text)
    text = _HTML_SUP_RE.sub(lambda match: match.group("body"), text)
    return _HTML_SUP_TAG_RE.sub("", text)


def normalize_misplaced_constraints(text: str) -> str:
    """把被分栏抽到“最值是”后面的单条约束移回紧邻的 aligned 方程组。"""
    if not text or "满足约束条件" not in text:
        return text

    def replace_constraint(match: re.Match) -> str:
        parts = _CONSTRAINT_PARTS_RE.match(match.group("constraint").strip())
        if parts is None:
            return match.group(0)
        row = f"{parts.group('lhs').strip()}&{parts.group('op')}"
        row += parts.group("rhs").strip()
        rows = match.group("rows").rstrip()
        separator = " " if rows.endswith(r"\\") else r"\\ "
        return (match.group("lead") + match.group("open") + rows + separator
                + row + match.group("close") + match.group("prompt"))

    return _MISPLACED_CONSTRAINT_RE.sub(replace_constraint, text)


def fix_frac(text: str) -> str:
    """`\\frac` → `\\dfrac`（只在公式内）。已是 `\\dfrac` 的不重复替换。"""
    return _map_parts(
        text, on_math=lambda s: re.sub(r"\\frac(?![a-zA-Z])", r"\\dfrac", s))


def fix_number_sets(text: str) -> str:
    """数域字体统一成 `\\mathbb`（只在公式内）：`\\mathbf{R}` → `\\mathbb{R}`。

    R/N/Z/Q 无条件转，C 只在紧跟集合算符时转（组合数 `\\mathrm{C}_5^3` 同形，
    见 _SET_C_RE 的注释）。已经是 `\\mathbb{...}` 的原样通过——正则会把它重写成
    自己，结果不变。
    """
    def _on_math(seg: str) -> str:
        seg = _SET_RE.sub(lambda m: "\\mathbb{" + m.group(1) + "}", seg)
        return _SET_C_RE.sub(lambda m: m.group(1) + "\\mathbb{C}", seg)

    return _map_parts(text, on_math=_on_math)


def add_displaystyle(text: str) -> str:
    """给行内公式补 `\\displaystyle `（只在公式内、只补一次）。

    空公式（`$$` 之类）与已带 `\\displaystyle` 的跳过。`\\displaystyle` 必须紧贴
    左 `$`，normalize_prompt 第 3 条要求 `$` 与内部字符不留空格。
    """
    def _on_math(seg: str) -> str:
        inner = seg[1:-1]
        if not inner.strip() or "\\displaystyle" in inner:
            return seg
        return "$\\displaystyle " + inner.strip() + "$"

    return _map_parts(text, on_math=_on_math)


def fix_period(text: str) -> str:
    """中文句号 `。` → 英文点号 + 半角空格（只在公式外）。

    行末的句号直接删掉：normalize_prompt 第 1 条明确要求题干/选项/全文末尾
    不带任何形式的句号。行中的替换成 `. `，并吃掉紧随的多余空格。

    **行末点号只在「前一个字符不是数字」时删**：45 份真实产物里行末以 `.` 结尾
    的行有 857 处，其中 `= 0.` 这类是 OCR 把小数点留在了行末，删掉会改动数值；
    而 `___.` `矛盾.` `是唯一解.` 这类是句号，该删。用前一字符是否为数字划界，
    是这两类里唯一机械可判的差别。删不掉的那部分代价只是多个点号，比改错数值轻。
    """
    def _on_text(seg: str) -> str:
        return seg.replace("。", ". ")

    lines = []
    for line in text.split("\n"):
        line = _map_parts(line, on_text=_on_text)
        # 先去行末空白，再删点号——两步都在行末，顺序反了第二条永远匹配不上
        line = re.sub(r"[ \t]+$", "", line)
        line = re.sub(r"(?<![0-9])\.$", "", line)
        lines.append(re.sub(r"[ \t]+$", "", line))
    return "\n".join(lines)


def fix_punct(text: str) -> str:
    """中文逗号/分号/冒号/问号/叹号 → 半角 + 空格（只在公式外）。

    句号不在这里（见 _CN_PUNCT 注释与 fix_period）。**中文括号刻意不动**：
    normalize_prompt 第 5 条要求正文括号一律全角 `（）`，与本函数方向相反——
    这是"公式内外规则相反"的那一处，别顺手把它并进来。

    替换后压掉多余空白：原文常写 `，  ` 或 `， $`，直接换成 `, ` 会留双空格。
    """
    def _on_text(seg: str) -> str:
        seg = _CN_PUNCT_RE.sub(lambda m: _CN_PUNCT[m.group(0)], seg)
        return re.sub(r"([,;:?!]) +", r"\1 ", seg)

    lines = []
    for line in text.split("\n"):
        line = _map_parts(line, on_text=_on_text)
        lines.append(re.sub(r"[ \t]+$", "", line))   # 行末由替换带出的空格
    return "\n".join(lines)


def fix_func_names(text: str) -> str:
    """裸函数名 → 带反斜杠的命令（只在公式内）：`cos x` → `\\cos x`。

    只在公式内做。公式外的 `min`/`max` 是中文行文里的英文词或变量名，包上反斜杠
    会变成非法 LaTeX；而"斜体字母 vs 直立命令"这个差别本来也只在公式里看得见。
    """
    return _map_parts(
        text, on_math=lambda s: _FUNC_RE.sub(lambda m: "\\" + m.group(1), s))


def strip_lead_number(text: str, number: int | None = None) -> str:
    """剥掉块正文开头的原文题号（`1. ` / `2、` / `3）`）。

    跳过 AI 时用（走 AI 时 LLM 顺手就剥了）。不剥的话渲染出来是 `- 1. 若…`，
    契约格式的 `- ` 与原文题号叠在一起，导出后每题都顶着个多余编号。

    **给了 number 就只剥那个数**：`12. 1024`（第 12 题答案是 1024）与 `3.14`
    这类小数用正则区分不了，但 Block.number 是切块阶段已经定下的事实，照它剥
    就不用猜。取不到 number 时才退回宽正则——此时块本来就没参与题号配对，
    误剥一个小数的代价远小于每题都残留编号。
    """
    if number is not None:
        pat = re.compile(r"^[ \t　]*" + str(number) + r"\s*[.、．)）]\s*")
    else:
        pat = re.compile(r"^[ \t　]*\d{1,3}\s*[.、．)）]\s*")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():                 # 跳过开头空行，题号在第一行有内容处
            continue
        lines[i] = pat.sub("", line, count=1)
        break
    return "\n".join(lines)


def fix_blank(text: str) -> str:
    """各种填空位写法统一成 `___`（只在公式外，避免动 LaTeX 下标）。"""
    return _map_parts(text, on_text=lambda s: _BLANK_RE.sub("___", s))


def ensure_fill_blank(text: str, qtype: str) -> str:
    """确认是填空题后补答题线；已有填空位时不重复添加。

    这一步不能放进 ``normalize_block``：机械规范化发生在题型确定之前，贸然给
    所有无选项题补线会污染解答题。图片通常是题干条件而非作答位置，因此把答题线
    插到末尾连续图片之前；没有尾图时直接接在最后一个正文段落后。
    """
    text = fix_blank(text).strip()
    # 强制 OCR 偶尔只保留一个转义下划线，并把它留在公式末尾：``$x=\_$``。
    # 单个 ``\_`` 不是有效答题线；只在等号后的公式收尾位置改写，避免动正文变量。
    text = re.sub(r"=\s*\\_\s*\$", "=$ ___", text)
    has_blank = ("___" in text or bool(re.search(
        r"\\(?:underline|underbar|hspace|rule)\s*\{|\\blank\b", text)))
    if qtype != "填空题" or has_blank:
        return text
    lines = text.split("\n")
    insert_at = len(lines)
    while insert_at > 0 and (not lines[insert_at - 1].strip()
                             or lines[insert_at - 1].lstrip().startswith("![[")):
        insert_at -= 1
    if insert_at:
        lines[insert_at - 1] = lines[insert_at - 1].rstrip() + "  ___"
    else:
        lines.insert(0, "___")
    return "\n".join(lines)


_SOLUTION_SECTION_RE = re.compile(
    r"^(?:#{1,6}\s+|!\[\[|\|)|^(?:[-*+]\s+|\d+[.)、]\s+)")
_METHOD_RE = re.compile(
    r"(?<!^)\s*(?=(?:解法|方法)\s*[一二三四五六七八九十\d]+\s*(?:[（(][^\n）)]*[）)])?\s*[:：]?|另解\s*[:：]?)")
# 行内小问仅在前一句已经收束后拆段；不能见到任意 `(1)` 就拆，坐标、函数值和
# 概率表达式中都有同形括号。原本位于行首的小问由后面的逐行逻辑自然保留。
_SUBQUESTION_RE = re.compile(r"(?<=[。；;])\s*(?=[（(][1-9]\d*[）)]\s*)")


def normalize_solution_layout(text: str) -> str:
    """保守整理解析段落，不改公式、措辞和推导顺序。

    - 多解法、小问各自另起段落；
    - 合并 OCR/PDF 造成的句中硬换行，让一句话保持在同一 Markdown 段落；
    - Markdown 标题、列表、表格、图片和独立公式不参与合并；
    - 全文空段统一为一个，阻断反复读写后“两行之间空三行”。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = _METHOD_RE.sub("\n\n", text)
    text = _SUBQUESTION_RE.sub("\n\n", text)
    lines = text.split("\n")
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        structural = bool(_SOLUTION_SECTION_RE.search(line) or line.startswith("$$"))
        previous = out[-1] if out else ""
        previous_structural = bool(previous and (
            _SOLUTION_SECTION_RE.search(previous) or previous.startswith("$$")))
        # 普通文本的单换行多为 PDF 行宽折行。合并时补一个空格，中文渲染不会受
        # 影响，而相邻 LaTeX/英文标记不会粘成新命令。
        if previous and not structural and not previous_structural:
            out[-1] = previous.rstrip() + " " + line
        else:
            out.append(line)
    return "\n".join(out).strip()


def fix_subq_parens(text: str) -> str:
    """行首小问序号 `(1)`/`(i)` 的半角括号改成全角 `（1）`/`（i）`。

    按行处理、只看行首（同 exporter._SUBQ_LINE_RE 的口径），不逐字符扫描整行——
    公式里 `f(1)=2`、坐标 `(1,2)` 这类半角括号必须原样保留，它们不会出现在行首。
    """
    lines = text.split("\n")
    return "\n".join(_HALFWIDTH_SUBQ_RE.sub(
        lambda m: m.group(1) + "（" + m.group(0)[len(m.group(1)) + 1:-1] + "）",
        line) for line in lines)


def strip_images(text: str) -> str:
    """删掉图片引用（keep_images=False 时用）。整行只有图片则连行一起去掉。"""
    lines = [l for l in text.split("\n")
             if not (l.strip() and not _IMG_RE.sub("", l).strip())]
    return "\n".join(_IMG_RE.sub("", l) for l in lines)


# ── 裸字母包 `$\displaystyle $`（导入预览的最后一步，**不在 normalize_block 里**）──
# 模块顶部那段"必须继续不做"说的是**在整篇/整块规范化里盲包**：那时正文里混着
# MinerU 的破损公式碎片（`P _ {2 0 2 5}`）与图片哈希文件名，无条件包会改坏 98%。
# 这里做的是同一件事的**带排除版**，且只在导入预览的最后一步作用于已经切好的
# 题干/解析（见 routes/import_convert.py 的 _build_import_preview）——那一层的
# 文本已经过 LLM 规范化或逐块规范化，公式该进 `$` 的基本都进去了。
#
# 三类排除对应那份标定里 98% 的代价来源，一个都不能省：
#   ① `_WRAP_SKIP_RE`：公式（`$…$` / `$$…$$`）、图片与链接、html 标签、行内代码、
#      公式外残留的 `\cmd`、图片文件名——占那 4104 处里的 81%；
#   ② 相邻字符是 `_ ^ { } \` 的跳过：这是破损公式碎片的机械判据（`P _ {…}` 的
#      `P` 右邻是 `_`），包了只会让碎片更难看。`$` 另算一档、且**只认零距离**，
#      理由见 `_MATHISH_NEIGHBOUR` 与 `_one` 里那条注释；
#   ⑤ `（i）`/`(I)` 这类单字母小问标号跳过（见 `_ROMAN_LABEL`）；
#   ③ 后面紧跟量词/名词（`A 组`、`k 步`）的跳过：那 10% 本来就不是数学符号。
# 选项标签 `A.` 连点号一起包成 `$\displaystyle A.$`（口径同 importer._OPTION_RE）
# ——那 52 处是标定里唯一"安全"的一类。**行首与行中同一个口径**：真实产物里四个
# 选项常常挤在一行（`…（ ）A. …B. …C. …`），只认行首会让同一行的 A 包成
# `$\displaystyle A.$`、B 包成 `$\displaystyle B$.`，点号一里一外。
#
# `_PROTECTED` 是"整行扫描时要跳过的区段"，`_WRAP_SKIP_RE` 在它之上多跳两类**只在
# 包裹时**要避开的东西。分成两个是因为 ④ 那条判据只能看 `$…$` 之外——`\cmd` 本身
# 正是它要找的信号，不能先被吃掉。
_PROTECTED = (
    r"\$\$(?:[^$]|\$(?!\$))*\$\$"                 # 行间公式
    r"|(?<!\$)\$(?!\$)(?:[^$\\]|\\.)*\$(?!\$)"    # 行内公式
    r"|!?\[[^\]]*\]\([^)]*\)"                     # 图片 / 链接
    r"|<[^>]+>"                                   # html 标签（MinerU 的 <table>）
    r"|`[^`]*`"                                   # 行内代码
)
_MATH_SPAN_RE = re.compile(_PROTECTED, re.I)
_WRAP_SKIP_RE = re.compile(
    _PROTECTED
    + r"|\\[A-Za-z]+"                             # 公式外残留的 LaTeX 命令
    + r"|[^\s（）()]*\.(?:png|jpe?g|gif|svg|bmp|webp)",   # 图片文件名
    re.I)

# 解答题小问可能被 PDF 抽取挤成 ``题干 (1)……;(2)……``。这里只负责找数字小问
# 标记；是否真是小问还要由 normalize_subquestion_layout 的连续编号与上下文判据确认。
_SUBQUESTION_MARKER_RE = re.compile(r"[（(]\s*([1-9]\d*)\s*[）)]")


def _sequential_subquestion_markers(text: str) -> list[re.Match]:
    """返回最完整的一组 ``（1）（2）……`` 小问标记；不可靠时返回空列表。"""
    protected = [(span.start(), span.end()) for span in _MATH_SPAN_RE.finditer(text)]

    def is_protected(start: int) -> bool:
        return any(left <= start < right for left, right in protected)

    markers = []
    for match in _SUBQUESTION_MARKER_RE.finditer(text):
        if is_protected(match.start()):
            continue
        if match.group(0).lstrip().startswith("(") and match.start() > 0:
            previous = text[match.start() - 1]
            if not (previous.isspace() or previous in "。；;：:？！?."):
                continue
        markers.append(match)

    chosen = []
    for index, marker in enumerate(markers):
        if int(marker.group(1)) != 1:
            continue
        candidate = [marker]
        expected = 2
        for following in markers[index + 1:]:
            if int(following.group(1)) != expected:
                break
            between = text[candidate[-1].end():following.start()].strip()
            # ``条件（1）和（2）`` 是正文引用而不是两道小问，只有一个“和”，不拆。
            if len(re.sub(r"\s+", "", between)) < 2:
                break
            candidate.append(following)
            expected += 1
        if len(candidate) >= 2:
            tail = text[candidate[-1].end():].strip()
            if len(re.sub(r"\s+", "", tail)) >= 2 and len(candidate) > len(chosen):
                chosen = candidate

    return chosen


def has_sequential_subquestions(text: str) -> bool:
    """正文是否含从 1 开始、至少两个连续且内容非空的小问。"""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return bool(cleaned and _sequential_subquestion_markers(cleaned))


def normalize_subquestion_layout(text: str) -> str:
    """让解答题中连续的（1）（2）……各自另起段落。

    规则刻意保守：至少要找到从 1 开始的两个连续编号，两个小问之间必须有实际
    内容；公式、图片、链接、HTML 与代码中的同形括号全部跳过。半角 ``(1)`` 还
    要求左侧是空白、行首或句读，避免把 ``f(1)`` 当成小问。调用方只应对解答题
    使用本函数。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return text
    # OCR 偶尔把最后一个顶层小问号重复成前一个（江苏 2020 第 20 题为 1,2,2）。
    # 仅修“全部标记恰为 1..n-1,n-1，最后重复号前有句末分隔符”的完整形状；
    # ``由（2）可得`` 这类正文引用前不是句末分隔符，不会命中。
    protected = [(span.start(), span.end()) for span in _MATH_SPAN_RE.finditer(text)]
    markers = [marker for marker in _SUBQUESTION_MARKER_RE.finditer(text)
               if not any(left <= marker.start() < right
                          for left, right in protected)]
    numbers = [int(marker.group(1)) for marker in markers]
    if (len(numbers) >= 3
            and numbers[:-1] == list(range(1, len(numbers)))
            and numbers[-1] == len(numbers) - 1):
        last = markers[-1]
        prefix = text[:last.start()].rstrip()
        tail = text[last.end():].strip()
        if prefix[-1:] in ";；。." and len(re.sub(r"\s+", "", tail)) >= 2:
            text = text[:last.start()] + f"（{len(numbers)}）" + text[last.end():]
    chosen = _sequential_subquestion_markers(text)
    if not chosen:
        return text

    result = text
    for marker in reversed(chosen):
        left = result[:marker.start()].rstrip()
        right = result[marker.end():].lstrip(" \t")
        # ``filestore._split_sections`` 会把正文首行的 Markdown 标题连内容提回题干，
        # 并固定用一个换行连接标题与内容；这里若强行加空段，写回再读取又会收成
        # 一个换行，迁移工具便永远认为同一道题“仍需修改”。标题后首问用单换行，
        # 视觉分段由标题自身承担，其余题干到小问之间仍保留 Markdown 空段。
        heading_only = bool(re.fullmatch(r"#{1,6}[^\n]+", left))
        separator = ("\n" if heading_only else "\n\n") if left else ""
        result = left + separator + f"（{marker.group(1)}）" + right
    return re.sub(r"\n{3,}", "\n\n", result).strip()

# ④ 公式外残留的裸 LaTeX。整行在 `$…$` 之外见到 `\cmd` 或 `{ } _ ^`，这一行就是
# **整条没进公式的破损残留**（`C M = \frac {4 x}{\sqrt {1 6 - x ^ {2}}}`、
# `\left| x - 1 \right| \leq 2`），此时行里的单字母全是公式里的变量，不是正文里
# 裸露的符号——逐个包只会包进一条本就渲染不出来的式子里，`$` 还会跟后面的配错对。
# ② 那条只看紧邻字符，管不到这种"字母在空格里、整行却是公式"的情形。
# 判据落在**整行**而不是单处：破损是整行级的，行内某个字母恰好左右干净不代表安全。
# 单个 `_` 才算信号，连续的不算——`___` 是填空题的空位（`guess_type` 就靠它判填空
# 题），把带空位的行整行跳过等于填空题一个字母都不包。
_RAW_TEX_RE = re.compile(r"\\[A-Za-z]+|[{}^]|(?<!_)_(?!_)")

# 孤立单字母：两侧都不是字母或数字。十六进制哈希、变量名 `xmin` 因此自然排除。
# 第 2 组捡走紧跟的点号，给选项标签那一档用（`A.` 整个包进去）。
_BARE_LETTER_RE = re.compile(
    r"(?<![0-9A-Za-z])([A-Za-z])(?![0-9A-Za-z])([.．])?")
# 紧邻这些字符说明它是破损公式的碎片，不是独立符号。
# **`$` 不在这里**：这一档允许中间隔空白，而「隔着空白的 `$`」正是选项挤在一行时的
# 常态（`A. $\dfrac{5}{2}$ B. 2` 里 `B` 的左邻 rstrip 完就是 `$`）。把 `$` 放进来会
# 让一行里除第一个之外的选项标签全部漏包——2026-08-08 真实库比对时 62 行都是这个。
# 贴着 `$` 的情形另有一条零距离检查（见 `_one`），破损公式碎片则由行级
# `_line_has_raw_tex` 兜住，两条都不依赖这个集合里有 `$`。
_MATHISH_NEIGHBOUR = set("_^{}\\")
# 小问罗马标号：`（i）求证…` `（I）求通项`。多字母的 `（ii）（iii）（iv）` 本来就不在
# `_BARE_LETTER_RE` 的口径里（它只认孤立单字母），只有 `i`/`I` 会漏进来被包成斜体，
# 同一份卷子里小问标号就一半斜一半正。补上这一档是为了口径一致，不是为了美观。
_ROMAN_LABEL = "iI"
# 字母后面紧跟这些字，那个字母是编号/代号而非数学符号（`A 组`、`k 步`、`B 站`）
_QUANTIFIER_RE = re.compile(r"^[ \t　]*[组班级类型层档步项种区卷册款届站]")

# ── 裸数字包 `$\displaystyle $`（与裸字母同一套排除，另加数字专有的几档）──────
# 2026-08-08 在本地真实库（96 题 922 行）上标定：公式外的裸数字 860 处，其中
# 67.0% 是真数学量，剩下 33% 分成六类，逐类都得挡掉——数字的排除面比字母宽，
# 因为数字还兼任题号、小问标号、分值、量词、序数。
#
# **最要紧的不是美观而是 `（1）`**（134 处，15.6%）：`exporter._SUBQ_LINE_RE` 认的是
# `^（\s*[0-9]+\s*）`，包成 `（$\displaystyle 1$）` 之后它一条都匹配不上，后果是**小问
# 不换行、不缩进**，不是"数字字体不一致"。`_break_subquestions` / `_split_stem_subs` /
# `_solve_answer_space` 三处都靠它，`fix_subq_parens` 存在的理由也正是这个。
#
# 数字串整体匹配：`0.025` 要连小数点一起包（切成 `$0$.$025$` 既难看又改变语义），
# 所以小数点在正则里面、且前后都用 `(?<![0-9A-Za-z.])` 挡住，免得从中间咬进去。
# 第 1 组是可选的正负号：`C. -2` 若只包数字会得到 `-$\displaystyle 2$`，那个减号
# 落在公式外，渲染成普通连字符而不是数学减号（真实库「范围/区间」那 49 处大半是
# 这个形状）。带不带由 `_one` 判——`1 - 2` 那种减法两侧都是数，不能把它当符号吃掉。
_BARE_NUMBER_RE = re.compile(
    r"(?<![0-9A-Za-z.．])([-−±+]?)(\d+(?:[.．]\d+)?)(?![0-9A-Za-z])")
# 数字紧跟 `%`：`80%` 包成 `$\displaystyle 80$%` 会把 `%` 留在公式外，而 `%` 在
# LaTeX 里是注释符——导出时它后面整行都会被吃掉。连着包又得写 `\%`（`dedup.normalize`
# 会把 `\` 删掉，指纹层面无碍，但那是把转义责任揽进这个只做机械替换的函数）。
# 真实库仅 2 处，直接跳过最省事。
_PERCENT_RE = re.compile(r"^\s*[%％]")
# 正负号左边是数字/右括号 → 那是二元运算符，不是这个数的符号（见 `_BARE_NUMBER_RE`）
_BINOP_LEFT_RE = re.compile(r"[0-9)\]}）]\s*$")
# 数字后面紧跟这些字 → 量词/单位/分值/年份，是计数不是数学量（83+4+4 处）。
# `分` 与 `年` 并进同一档：`不低于120分`、`记 -1 分`、`2025 年` 判据形状相同。
_NUM_QUANTIFIER_RE = re.compile(
    r"^[ \t　]*[分年个名位种条张次步项组题问班级类型层档区卷册款届人天月日时秒边角面倍成]")
# 数字前面紧跟这些字 → 序数/图表编号，那个数字是标号（`第25百分位数`、`如图2所示`）
_NUM_ORDINAL_LEAD = ("第", "图", "表", "式", "组", "问")
# 解析里的分步小标题 `方法1：`（口径同 exporter._METHOD_HEADER_RE）。包了那条也认不出，
# 分法小标题就不再另起一段——与 `（1）` 同一类后果，不是字体问题。
_METHOD_LEAD_RE = re.compile(r"(?:方法|解法|证法|解答|做法)\s*$")


def _line_has_raw_tex(line: str) -> bool:
    """整行在公式之外是否残留裸 LaTeX（见 `_RAW_TEX_RE` 上方注释）。"""
    pos = 0
    for span in _MATH_SPAN_RE.finditer(line):
        if _RAW_TEX_RE.search(line[pos:span.start()]):
            return True
        pos = span.end()
    return bool(_RAW_TEX_RE.search(line[pos:]))


def _wrap_letters_in_text(seg: str, full: str, base: int) -> str:
    """把一段"非排除区"里的孤立字母包进 `$\\displaystyle $`。

    full/base 是该段在整行里的位置，用来看真正的左右邻居——段边界处的邻居在段内
    看不到（`$x$ P _ {2}` 里 `P` 的右邻 `_` 在同一段内，但左邻在上一段）。
    """
    def _one(m):
        i = base + m.start(1)
        left = full[:i].rstrip()
        right = full[i + 1:]
        if left[-1:] in _MATHISH_NEIGHBOUR:
            return m.group(0)
        stripped = right.lstrip()
        if stripped[:1] in _MATHISH_NEIGHBOUR:
            return m.group(0)
        # `$` 只在**零距离**贴着时才算碎片信号（`$x` / `x$`，`$` 没配上对、整段落到
        # 公式外的那种）。隔着空白的 `$` 不算：那是 `A. $\dfrac{5}{2}$ B. 2` 里上一个
        # 选项的公式刚闭合，`B` 是货真价实的选项标签。这两种情形上面那两条
        # `rstrip()/lstrip()` 的检查分不开，所以单独一条、不带空白容忍。
        if full[i - 1:i] == "$" or right[:1] == "$":
            return m.group(0)
        if _QUANTIFIER_RE.match(right):
            return m.group(0)
        # `（i）` `(I)` 是小问标号而非数学符号，包了就变斜体。多字母的 `（ii）（iv）`
        # 本来就不在 `_BARE_LETTER_RE` 口径里，只有单字母这一档会漏进来。
        if (m.group(1) in _ROMAN_LABEL
                and left[-1:] in "（(" and stripped[:1] in "）)"):
            return m.group(0)
        # A-D 后面紧跟点号 → 选项标签，点号一起包进去（行首行中同一个口径）
        dot = m.group(2) or ""
        if dot and m.group(1) in "ABCD":
            return "$\\displaystyle " + m.group(1) + dot + "$"
        return "$\\displaystyle " + m.group(1) + "$" + dot

    return _BARE_LETTER_RE.sub(_one, seg)


def _wrap_numbers_in_text(seg: str, full: str, base: int) -> str:
    """把一段"非排除区"里的裸数字包进 `$\\displaystyle $`。

    公式碎片那几条判据与字母共用（`_MATHISH_NEIGHBOUR`、零距离 `$`），另加数字
    专有的五档：行首题号、`（1）` 小问标号、量词/分值/年份、序数前缀、`方法1`。
    """
    def _one(m):
        sign, num = m.group(1), m.group(2)
        ns = base + m.start(2)            # 数字本身的起点
        e = base + m.end(2)
        # 符号左边（忽略空白）是数字或右括号说明它是**二元运算符**而不是这个数的
        # 正负号（`1 - 2`、`(a+b) - 3`），归给前一项——吃进公式会写出 `1 $-2$`。
        if sign and _BINOP_LEFT_RE.search(full[:base + m.start(1)]):
            sign = ""
        s = ns if not sign else base + m.start(1)
        left = full[:s].rstrip()
        right = full[e:]
        if left[-1:] in _MATHISH_NEIGHBOUR or right[:1] in _MATHISH_NEIGHBOUR:
            return m.group(0)
        if full[s - 1:s] == "$" or right[:1] == "$":
            return m.group(0)
        # `%` 是 LaTeX 注释符，不能留在公式外（见 `_PERCENT_RE`）
        if _PERCENT_RE.match(right):
            return m.group(0)
        # 行首题号 `1. ` `12、` `3）`：`strip_leading_number` 已经剥过第一行，剩下的
        # 是解析里 `15. 解: (1) …` 这种自带题号的行（真实库 8 处）。包了会让
        # `importer.block_number` 与 `strip_lead_number` 都认不出题号。
        if not left and right[:1] in ".、．)）":
            return m.group(0)
        # `（1）` 小问标号：包了 `exporter._SUBQ_LINE_RE` 就认不出，小问不换行不缩进
        if left[-1:] in "（(" and right.lstrip()[:1] in "）)":
            return m.group(0)
        if _NUM_QUANTIFIER_RE.match(right):
            return m.group(0)
        if left.endswith(_NUM_ORDINAL_LEAD):
            return m.group(0)
        if _METHOD_LEAD_RE.search(left):
            return m.group(0)
        # 被判成二元运算符的符号原样留在公式外，只把数字包进去
        dropped = m.group(1) if not sign else ""
        return dropped + "$\\displaystyle " + sign + num + "$"

    return _BARE_NUMBER_RE.sub(_one, seg)


def _wrap_by_line(text: str, wrap_seg) -> str:
    """逐行扫描：跳过 `_WRAP_SKIP_RE` 的区段，其余交给 wrap_seg 包。

    字母与数字共用这层骨架——排除区段、整行破损判定对两者完全一致，分开写迟早
    只改一处。
    """
    out_lines = []
    for line in text.split("\n"):
        if _line_has_raw_tex(line):
            out_lines.append(line)          # 整行是没进公式的破损残留，一个都不动
            continue
        pos = 0
        parts = []
        for sk in _WRAP_SKIP_RE.finditer(line):
            if sk.start() > pos:
                parts.append(wrap_seg(line[pos:sk.start()], line, pos))
            parts.append(sk.group(0))
            pos = sk.end()
        if pos < len(line):
            parts.append(wrap_seg(line[pos:], line, pos))
        out_lines.append("".join(parts))
    return "\n".join(out_lines)


_OPTION_PAREN_OUTER_RE = re.compile(
    r"[（(](\$\\displaystyle\s*[A-D][.．]?\$)[）)]")
_OPTION_PAREN_INNER_RE = re.compile(
    r"(\$\\displaystyle\s*)[（(]([A-D])([.．]?)[）)]")


def strip_option_parens(text: str) -> str:
    """剥掉选项标签外层多套的括号：`($\\displaystyle A.$)`／`$\\displaystyle (A).$`\
    都改回 `$\\displaystyle A.$`。

    两个来源都会产出这层多余括号：一是 LLM 把数学里「角度/坐标常用括号」的习惯\
    错用到选项标签上；二是原文本身就是 `(A)` 这种带括号的裸标签，`wrap_bare_letters`\
    只包字母不剥外层括号，包完变成 `($\\displaystyle A$)`——所以要放在 wrap 之后调。
    口径只认 A-D 且紧贴 `\\displaystyle` 标记，不碰 `f(A)`、`(a+b)`、`(1,2)` 这类\
    真实数学括号（标记与括号之间不允许夹别的字符）。
    """
    text = _OPTION_PAREN_OUTER_RE.sub(r"\1", text)
    text = _OPTION_PAREN_INNER_RE.sub(r"\1\2\3", text)
    return text


_CHOICE_LABEL_RE = re.compile(
    r"[（(]\s*\$\\displaystyle\s*([A-D])\s*[.．]?\$\s*[）)]"
    r"|\$\\displaystyle\s*[（(]\s*([A-D])\s*[）)]\s*[.．]?\$"
    r"|\$\\displaystyle\s*([A-D])\s*[.．]?\$"
    r"|[（(]\s*([A-D])\s*[）)]"
    r"|(?<![0-9A-Za-z\\])([A-D])[.．]"
    # MinerU/图片重排会把独占一行的 ``A)`` 原样留下。禁止左侧紧邻
    # 左括号，避免把 ``f(A)``、点坐标 ``(A)`` 中的字母再认一次。
    r"|(?<![0-9A-Za-z\\(（])([A-D])[)）](?=\s|$)")
_CANON_CHOICE_LABEL = "$\\displaystyle {}.$"
# OCR/LaTeX 常把选择题答题括号写成 ``(\quad)``，视觉上仍是空括号。把这些只
# 含排版空白命令的括号视作答题边界；含字母、数字或运算符的括号仍不是空括号。
_EMPTY_ANSWER_PAREN_RE = re.compile(
    r"[（(]\s*(?:\\(?:quad|qquad|;|,|!|:|\s)\s*)*[)）]")
_EMBEDDED_MATH_CHOICE_LABEL_RE = re.compile(
    r"\(\s*\\mathrm\s*\{\s*([B-D])\s*\}\s*\)\s*\$\s*\$\s*",
    re.I,
)
_TRAILING_MATH_CHOICE_LABEL_RE = re.compile(
    r"\\mathrm\s*\{\s*([B-D])\s*\}\s*\$\s*[.．]\s*(?=\$)",
    re.I,
)
_RAW_STRONG_CHOICE_LABEL_RE = re.compile(
    r"(?<![0-9A-Za-z\\])([A-D])[.．](?=\s)")


def has_choice_answer_blank(text: str) -> bool:
    """题干是否含选择题常用的空答题括号。

    这只是题型判定和 OCR 复核信号，不足以证明 A-D 选项正文完整，因此不能拿它
    直接改写选项。独立暴露这个判据，避免分类器、OCR 重试和图片归属各自维护一份
    略有差异的正则。
    """
    return _EMPTY_ANSWER_PAREN_RE.search(text) is not None


def strip_choice_answer_blank(text: str) -> str:
    """去掉题干末尾的空答题括号，保留括号内有内容的数学表达式。

    选择题的 ``（ ）`` 只用于原题答题，不应跟着题干进入卡片和试卷排版；
    但题干中间的区间、函数括号不能误删，因此只处理文本末尾且位于数学区外的
    空括号。导入阶段仍保留原括号，供题型识别和选项恢复使用。
    """
    if not text:
        return text
    end = len(text.rstrip())
    for match in reversed(list(_EMPTY_ANSWER_PAREN_RE.finditer(text[:end]))):
        if match.end() != end:
            continue
        if any(span.start() < match.start() < span.end()
               for span in _MATH_SPAN_RE.finditer(text)):
            continue
        return text[:match.start()].rstrip()
    return text


def normalize_embedded_choice_labels(text: str) -> str:
    """恢复被塞进上一选项公式末尾的 B/C/D 标签。

    MinerU 会把 ``(A) $式A(\\mathrm{B})$ $式B...`` 连成一行；只有同时具备
    ``\\mathrm{B-D}``、当前公式闭合和下一公式开启三个锚点才改写，普通公式里的
    ``\\mathrm{B}`` 不受影响。
    """
    text = _EMBEDDED_MATH_CHOICE_LABEL_RE.sub(
        lambda match: f"$ ({match.group(1).upper()}) $", text)
    # Doc2X 还会把下一项的字母粘在上一项公式的末尾：
    # ``$式B\mathrm{C}$ . $式C``。这里同时要求 B-D、上一公式闭合、点号和
    # 下一公式开启四个锚点，普通物理量 ``\mathrm{C}`` 不会被改写。
    return _TRAILING_MATH_CHOICE_LABEL_RE.sub(
        lambda match: f"$ {match.group(1).upper()}. ", text)


def normalize_missing_first_choice_label(text: str) -> str:
    """补回表格/题图后选项行唯一缺失的 ``A.`` 标签。

    只接受以下完整证据链：题干已有选择题答题空括号；某一行恰好只检出按序 B、C、D；
    B 前正文非空且不超过 80 字；该行上一非空行是图片。这样覆盖 Doc2X 把表格下方
    ``A. 2.0m/s`` 漏成 ``2.0m/s`` 的情形，不会把普通正文中的 B/C/D 点名猜成选项。
    """
    if not has_choice_answer_blank(text):
        return text
    lines = text.splitlines()
    for index, line in enumerate(lines):
        matches = list(_RAW_STRONG_CHOICE_LABEL_RE.finditer(line))
        if [match.group(1).upper() for match in matches] != list("BCD"):
            continue
        prefix = line[:matches[0].start()].strip()
        if (not prefix or len(prefix) > 80
                or not re.search(r"[0-9A-Za-z\u3400-\u9fff]", prefix)):
            continue
        previous = next((lines[pos].strip() for pos in range(index - 1, -1, -1)
                         if lines[pos].strip()), "")
        if not re.fullmatch(r"!\[[^\]]*\]\([^)]*\)", previous):
            continue
        lines[index] = "A. " + line.lstrip()
    return "\n".join(lines)


_COMPACT_CHOICE_LABEL_RE = re.compile(r"([A-D])[.．]")


def normalize_compact_choice_labels(text: str) -> str:
    """拆开被 OCR 压成一个无空格短串的完整 ``A. … B. … C. … D. …``。

    只处理答题空括号后的同一物理行，且该行必须恰好按 A—D 各出现一次、四段
    内容均非空并且段内没有空白。这个窄口径覆盖 ``A.3.5JB.3.1J…``，同时避开
    题干里的点名 A/B/C/D、公式、链接与图片文件名。证据不足时原样返回，继续让
    选项质量门报警，不能为了消警告猜测边界。
    """
    blanks = list(_EMPTY_ANSWER_PAREN_RE.finditer(text or ""))
    if not blanks:
        return text
    boundary = blanks[-1].end()
    line_end = text.find("\n", boundary)
    if line_end < 0:
        line_end = len(text)
    line = text[boundary:line_end]
    protected = [
        (span.start(), span.end()) for span in _MATH_SPAN_RE.finditer(line)
    ]
    hits = [
        match for match in _COMPACT_CHOICE_LABEL_RE.finditer(line)
        if not any(left <= match.start() < right for left, right in protected)
    ]
    if [match.group(1) for match in hits] != list("ABCD"):
        return text
    if line[:hits[0].start()].strip():
        return text
    payloads = []
    for index, match in enumerate(hits):
        stop = hits[index + 1].start() if index < 3 else len(line)
        payload = line[match.end():stop]
        if index == 3:
            # D 项后可能紧跟图中坐标轴的独立文本。紧凑串自身没有空白，故只把
            # 第一个空白前的原子视为 D 项；后缀原位保留，不吞进选项。
            compact = re.match(r"([^\s]{1,64})", payload)
            if compact is None:
                return text
            payload = compact.group(1)
        if (not payload or not payload.strip()
                or re.search(r"\s", payload)
                or len(payload) > 64):
            return text
        payloads.append(payload)
    if any(not re.search(r"[0-9A-Za-z\u3400-\u9fff]", item)
           for item in payloads):
        return text
    fixed_line = line
    for match in reversed(hits[1:]):
        fixed_line = fixed_line[:match.start()] + "\n" + fixed_line[match.start():]
    return text[:boundary] + fixed_line + text[line_end:]


_INTRUSIVE_SECTION_RE = re.compile(
    r"[ \t　]*(?:[一二三四五六七八九十]+|\d+)\s*[、.．]\s*"
    r"(?:单选题|多选题|多项选择题|填空题|实验题|解答题|计算题|证明题)[ \t　]*")
_QUESTION_START_WORD_RE = re.compile(
    r"\s*(\d{1,3})\s*[.．、]?\s*(?=(?:记|已知|在|设|若|如图|函数|数列))")


def normalize_intrusive_column_text(text: str) -> str:
    """清除多栏阅读顺序塞进当前选择题的分区标题和下一题开头。"""
    fixed = _INTRUSIVE_SECTION_RE.sub(" ", text)
    current_match = re.match(r"^\s*(\d{1,3})\s*[.．、)]", fixed)
    current = int(current_match.group(1)) if current_match else None
    blanks = list(_EMPTY_ANSWER_PAREN_RE.finditer(fixed))
    if not blanks:
        return fixed
    boundary = blanks[-1].end()
    option = next((match for match in _CHOICE_LABEL_RE.finditer(fixed)
                   if match.start() >= boundary), None)
    if option is None:
        return fixed
    gap = fixed[boundary:option.start()]
    stray = _QUESTION_START_WORD_RE.search(gap)
    if stray and (current is None or int(stray.group(1)) != current):
        fixed = fixed[:boundary] + gap[:stray.start()] + fixed[option.start():]
    return fixed


def _choice_quartet(text: str, *, known_choice: bool = False):
    """返回最靠后的可靠 A—D 标签四元组；找不到则返回 None。"""
    hits = []
    for match in _CHOICE_LABEL_RE.finditer(text):
        letter = next((group for group in match.groups() if group), "")
        if letter:
            token = match.group(0)
            strong = (bool(re.search(r"[.．)）]", token))
                      or token.lstrip().startswith(("(", "（")))
            hits.append((letter, match.start(), match.end(), strong))
    answer_blanks = list(_EMPTY_ANSWER_PAREN_RE.finditer(text))
    # 常见排版是“题干（ ）A...D...”，但题干本身可能先出现集合 A、事件 B，不能用
    # “第一个 A-D 标签”判断括号在选项前还是选项后。改为检查括号之后能否按顺序
    # 找到 A-D：末尾答题括号后没有四项，不会误作边界（2020 全国Ⅱ卷第 2 题）。
    def has_ordered_abcd_after(boundary: int) -> bool:
        cursor = boundary
        for wanted in "ABCD":
            hit = next((item for item in hits
                        if item[0] == wanted and item[1] >= cursor), None)
            if hit is None:
                return False
            cursor = hit[2]
        return True

    leading_blanks = [blank for blank in answer_blanks
                      if has_ordered_abcd_after(blank.end())]
    if leading_blanks:
        # 选择题题干也常出现集合 A、事件 B；有明确答题括号时，只认它后面的标签。
        # 上海卷第 13 题正是靠这道边界避免把题干 A/B 与错序的选项 C/D 拼成假四元组。
        boundary = leading_blanks[-1].end()
        hits = [hit for hit in hits if hit[1] >= boundary]
    elif not known_choice:
        # 没有选择题答题括号时，`$A$、$B$、$C$、$D$` 更可能是四个几何点或
        # 集合变量。只保留带点/带括号的强标签，避免把普通题干改成四个选项。
        hits = [hit for hit in hits if hit[3]]
    def select(pool):
        for ai in range(len(pool) - 1, -1, -1):
            if pool[ai][0] != "A":
                continue
            indices = [ai]
            cursor = ai + 1
            for wanted in "BCD":
                while cursor < len(pool) and pool[cursor][0] != wanted:
                    cursor += 1
                if cursor >= len(pool):
                    break
                indices.append(cursor)
                cursor += 1
            if len(indices) == 4:
                chosen = [pool[index] for index in indices]
                for index, (_letter, _start, end, _strong) in enumerate(chosen):
                    stop = chosen[index + 1][1] if index < 3 else len(text)
                    if not text[end:stop].strip():
                        return None
                return chosen
        return None

    # 规范 A./B./C./D. 与题干里的数学变量同时出现时，必须优先用四个强标签；
    # 只有凑不齐强标签时，才退到强制 OCR 常见的 `$A$ $B$ $C$ $D$` 无点形态。
    strong_choice = select([hit for hit in hits if hit[3]])
    if strong_choice is not None:
        return strong_choice
    # 无点号的 `$A$ $B$ $C$ $D$` 与选项正文里的“事件 A、事件 B”完全同形。
    # 只有候选恰好就是 A/B/C/D 各一次时才允许弱标签兜底；多出任何 A—D 都交给
    # OCR/人工复核，不能靠跳过中间字母硬凑一个四元组。
    weak_choice = select(hits) if [hit[0] for hit in hits] == list("ABCD") else None
    if weak_choice is not None:
        return weak_choice
    return None


def has_complete_choice_options(text: str, *, known_choice: bool = False) -> bool:
    """正文是否含可可靠定位、内容非空的完整 A—D 选项。"""
    return _choice_quartet(text, known_choice=known_choice) is not None


def looks_like_choice_options(text: str) -> bool:
    """宽口径判断疑似选择题，仅用于题型兜底和质量告警，不用于改正文。"""
    blanks = list(_EMPTY_ANSWER_PAREN_RE.finditer(text))
    if not blanks:
        return has_complete_choice_options(text)
    tail = text[blanks[-1].end():]
    labels = set()
    for match in _CHOICE_LABEL_RE.finditer(tail):
        labels.add(next((group for group in match.groups() if group), ""))
    # A 被 OCR 成 operatorname{A} 时仍能看到 B/C/D；三项足以判“这是选择题”，
    # 但不足以安全重写，所以与 has_complete_choice_options 分成两个函数。
    return len(labels - {""}) >= 3


def normalize_choice_options(text: str, *, known_choice: bool = False) -> str:
    """把一组完整 A—D 选项统一成可稳定分列的规范格式。

    MinerU 常见三种非规范标签：``(A)``、``$\\displaystyle A$`` 和裸 ``A.``。
    页面与 PDF 的选项网格以规范标签为结构锚点；锚点漂移就会让四项退化成一整段。
    这里只在能找到按顺序出现且内容非空的完整 A/B/C/D 四元组时改写，并优先取
    最靠后的 A，避开题干里集合 A、集合 B 等数学对象。``known_choice=True`` 表示
    调用方已有可靠题型证据，此时即使答题空括号丢失，也允许“恰好 A-D 各一次”的
    弱标签；正文多出任何 A-D 仍拒绝。不能证明是完整选项时原文不动。

    四项各占一行只是 Markdown 的稳定存储形态；页面/PDF 仍按可见宽度自动排成
    4 列、2 列或 1 列，所以短选项不会被强制竖排。
    """
    chosen = _choice_quartet(text, known_choice=known_choice)
    if chosen is None:
        return text

    contents = []
    for index, (_letter, _start, end, _strong) in enumerate(chosen):
        stop = chosen[index + 1][1] if index < 3 else len(text)
        content = text[end:stop].strip()
        # ``(A) ) \sin x`` 是文本层把选项左括号与正文分离后的重复右括号；选项
        # 正文不可能合法地从孤立右括号开头，去掉它不会影响 ``(0, 1)`` 这类区间。
        content = re.sub(r"^[)）]\s*", "", content)
        # 图像坐标轴上的 x/y 有时又被文本层单独抽出，落在“本项图片之后、下一项
        # 标签之前”。只有内容已经含选项图且末尾仅剩孤立轴名时删除，普通文字选项
        # 与题干中的变量 x/y 都不受影响。
        content = re.sub(
            r"(?s)(!\[[^\]]*\]\([^)]*\))\s*\n+\s*[xyXY]\s*$",
            r"\1",
            content,
        )
        if not content:
            return text
        contents.append(content)

    prefix = text[:chosen[0][1]].rstrip()
    options = "\n".join(
        f"{_CANON_CHOICE_LABEL.format(letter)} {content}"
        for letter, content in zip("ABCD", contents))
    return (prefix + "\n\n" if prefix else "") + options


def wrap_bare_letters(text: str) -> str:
    """给正文里裸露的单个拉丁字母套上 `$\\displaystyle $`，逐行处理。

    排除口径见上方注释。已经在公式里的、图片路径里的、破损公式碎片里的字母一律
    不动——`$` 一旦包错位置，KaTeX 直接渲染成红字，比不包难看得多。
    """
    return _wrap_by_line(text, _wrap_letters_in_text)


def wrap_bare_numbers(text: str) -> str:
    """给正文里裸露的数字套上 `$\\displaystyle $`，逐行处理。

    与 `wrap_bare_letters` 同一套排除区段，另加数字专有的五档（见
    `_BARE_NUMBER_RE` 上方标定）。**必须在 `wrap_bare_letters` 之后调**：字母那步
    产出的 `$\\displaystyle A.$` 会被 `_WRAP_SKIP_RE` 当成行内公式跳过，顺序反了
    则 `A.` 里的字母仍是裸的、而 `（1）` 这类已经先被数字步保护住，两步互不干扰
    ——但反序时字母步会看到数字步刚插进去的 `$`，零距离判据把它两侧的字母全挡掉。
    """
    return _wrap_by_line(text, _wrap_numbers_in_text)


def normalize_block(text: str, keep_images: bool = True) -> str:
    """机械规范化一个块。顺序有讲究，见各步注释。

    先 fix_frac / fix_func_names 再 add_displaystyle：后者会把公式内容 strip 并
    重组，先做公式内的替换免得重复扫描。fix_period / fix_punct 放在公式处理
    之后——它们按行处理并删行末，而 add_displaystyle 不改变行结构，互不干扰。
    fix_punct 紧跟 fix_period：句号那条会产出 `. `，两条都在压行末空白，挨着
    做省一遍扫描，且顺序无关（作用的字符集不相交）。

    fix_number_sets 也必须排在 add_displaystyle 之前：它的 C 规则要看左邻是不是
    集合算符，而 add_displaystyle 会在公式开头插入 `\\displaystyle `——虽然不影响
    `\\in` 与 `\\mathbf{C}` 的相邻关系，但保持"公式内替换全在重组之前"这条一致，
    以后加规则不用逐条想。
    """
    if not keep_images:
        text = strip_images(text)
    # 必须在公式分段前处理：MinerU 常把基符与下标分别放进两个 ``$...$``，先合并
    # 才能避免后续 add_displaystyle 形成嵌套或相邻的破碎公式。
    text = normalize_html_subscripts(text)
    text = normalize_html_superscripts(text)
    text = normalize_embedded_choice_labels(text)
    text = normalize_missing_first_choice_label(text)
    text = normalize_compact_choice_labels(text)
    text = normalize_intrusive_column_text(text)
    text = normalize_misplaced_constraints(text)
    text = fix_frac(text)
    text = fix_func_names(text)
    text = fix_number_sets(text)
    text = add_displaystyle(text)
    text = fix_period(text)
    text = fix_punct(text)
    text = fix_blank(text)
    text = fix_subq_parens(text)
    # 收敛连续空行：切块时保留了原文空行，三行以上压成两行（Markdown 段落）
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
