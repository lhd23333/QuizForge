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

所有替换都避开 `$...$` 内部还是刻意作用于其内部，逐条在函数注释里写明——
公式内外规则相反的地方（中文括号）弄错会直接改坏 LaTeX。
"""

import re

# 行内公式片段：非贪婪匹配一对 `$`，用于把正文与公式分段处理。
# 不处理 `$$`（行间公式）——normalize_prompt 要求转成行内，但那是语义改写，
# 留给 LLM；这里遇到 `$$` 原样放过，不去猜它的边界。
_INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)((?:[^$\\]|\\.)*)\$(?!\$)")

# 图片引用：`![alt](path)`
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

# MinerU 的下划线填空位：`\_\_\_\_` / `____` / 全角 `＿＿`，统一成三个半角下划线
_BLANK_RE = re.compile(r"(?:\\?_){3,}|＿{2,}")


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


def fix_frac(text: str) -> str:
    """`\\frac` → `\\dfrac`（只在公式内）。已是 `\\dfrac` 的不重复替换。"""
    return _map_parts(
        text, on_math=lambda s: re.sub(r"\\frac(?![a-zA-Z])", r"\\dfrac", s))


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
    """
    def _on_text(seg: str) -> str:
        return seg.replace("。", ". ")

    lines = []
    for line in text.split("\n"):
        line = _map_parts(line, on_text=_on_text)
        line = re.sub(r"[ \t]+$", "", line)
        line = re.sub(r"\.\s+$", "", line)       # 末尾由句号换来的 `. ` 去掉
        lines.append(line)
    return "\n".join(lines)


def fix_blank(text: str) -> str:
    """各种填空位写法统一成 `___`（只在公式外，避免动 LaTeX 下标）。"""
    return _map_parts(text, on_text=lambda s: _BLANK_RE.sub("___", s))


def strip_images(text: str) -> str:
    """删掉图片引用（keep_images=False 时用）。整行只有图片则连行一起去掉。"""
    lines = [l for l in text.split("\n")
             if not (l.strip() and not _IMG_RE.sub("", l).strip())]
    return "\n".join(_IMG_RE.sub("", l) for l in lines)


def normalize_block(text: str, keep_images: bool = True) -> str:
    """机械规范化一个块。顺序有讲究，见各步注释。

    先 fix_frac 再 add_displaystyle：后者会把公式内容 strip 并重组，先做替换
    免得重复扫描。fix_period 放在公式处理之后——它按行处理并删行末点号，
    而 add_displaystyle 不改变行结构，两者互不干扰。
    """
    if not keep_images:
        text = strip_images(text)
    text = fix_frac(text)
    text = add_displaystyle(text)
    text = fix_period(text)
    text = fix_blank(text)
    # 收敛连续空行：切块时保留了原文空行，三行以上压成两行（Markdown 段落）
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
