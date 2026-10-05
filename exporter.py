"""导出：题目列表 → Markdown → pandoc → TeX →（xelatex）→ PDF。

复用 project-alpha 的导出链路与 exam_template.tex。
关键避坑（来自 project-alpha 调试档案）：
  1. 选项间必须加空行，否则 pandoc 会把连续行合并成一段
  2. 模板已含 tightlist / none-counter / array / longtable / calc 兼容补丁
  3. 文件统一 UTF-8（不加 BOM）
  4. 日志/异常文本用 ASCII 标记，避免 Windows GBK 终端 emoji 崩溃
"""

import base64
import collections
import functools
import logging
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import zipfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

import config
import export_tables
import mechfix
import template_pipeline
import tex_sandbox


class ExportError(Exception):
    """导出过程出错。"""


class ExportCancelled(ExportError):
    """用户主动终止导出。

    与普通失败分开成独立类型：导出任务服务要把它归到「已终止」而不是「失败」。
    ``_export_unlocked`` 捕获它时会删掉本次导出的半成品工作目录——用户按的是
    「不要了」，留下半个 PDF 或几百 MB 中间文件只会让人以为导出还活着。
    """


def _cancelled(cancel) -> bool:
    """容忍 None 与任意带 is_set() 的取消标记（threading.Event / 测试假对象）。"""
    return bool(cancel is not None and cancel.is_set())


def _check_cancel(cancel) -> None:
    if _cancelled(cancel):
        raise ExportCancelled("导出已终止")


def _resolve_image_source(raw_name: str) -> Path | None:
    """把 Obsidian 图片引用安全解析到 ``IMAGES_DIR`` 内的普通文件。

    图片名来自 Markdown，不能直接参与 ``IMAGES_DIR / name``：绝对路径会吞掉前缀，
    ``..`` 和目录符号链接也都可能逃出题库。这里同时检查词法路径、解析后的边界以及
    路径链上的每一级符号链接；调用方把 SVG 换成同名 PDF 时必须再次调用本函数。
    """
    value = str(raw_name or "").strip().replace("\\", "/")
    relative = Path(value)
    if (not value or relative.is_absolute() or relative.drive
            or any(part in ("", ".", "..") for part in relative.parts)):
        return None

    root_path = Path(config.IMAGES_DIR).absolute()
    try:
        if root_path.is_symlink():
            return None
        root = root_path.resolve(strict=True)
    except OSError:
        return None

    candidate = root.joinpath(*relative.parts)
    cursor = root
    try:
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return None
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if root != resolved.parent and root not in resolved.parents:
        return None
    if not resolved.is_file():
        return None
    return resolved


# 工作目录隔离解决“文件互删”，并发槽解决“同时跑多个 XeLaTeX 抢爆内存”。
# 这是进程内边界：PDF 导出、DOCX 导出（word_exporter）与讲义局部编译
# （handout_exporter）共用同一把槽，避免三条路各自放行后叠加超限。
_EXPORT_SLOTS = threading.BoundedSemaphore(
    max(1, int(getattr(config, "EXPORT_CONCURRENCY", 1)))
)

# xelatex 每 shipout 一页会在 stdout 打一行 ``[N]``（`[1]`、`[2]` …，页码可带
# 前后缀如 `[12 <...>]`）。统计其中最大页码就是“已排出几页”，是编译阶段唯一
# 可得的真实进度信号；纯文本估算看不到 TeX 实际断页。
_PAGE_MARK_RE = re.compile(r"\[(\d+)[^\]]*\]")


# 完整选项标签：优先匹配 $\displaystyle A.$（含美元整体），否则裸 A. / A．
# 两个分支分别用 finditer 定位“标签起点”，只在起点切分，绝不切进 $...$ 内部
# （否则 $ 落单，会被 pandoc 转义成 \$，导致公式失效）。
_LABEL_DOLLAR = re.compile(r"\$\\displaystyle\s*[A-D][.．]\s*\$")
_LABEL_BARE = re.compile(r"(?<![A-Za-z\\])[A-D][.．]")

# 单个 $...$ 数学区（不处理嵌套，只按最近一对 $ 配对，同 _MATH_SPLIT_RE 的取舍）。
# 用来挡住「裸标签落在数学区内部」的误切，见 _label_hits 的 docstring。
_MATH_RE = re.compile(r"\$[^$]*\$")

# 第三种标签形态：**标签与选项内容同在一个数学区里**，如 `$\displaystyle A.~4$`。
# 匹配的是数学区的**开头**（`$` + 可选 \displaystyle + 标签），因此切点落在 `$` 上
# ——正好是数学区边界，切开后两侧各自成对。这类正文匹配不上 _LABEL_DOLLAR（那条要
# 求标签自成一对 $），旧代码只能退到 _LABEL_BARE 切进数学区内部，切出半个 $ 让
# xelatex 报 `! Missing $ inserted.`（线上 id=665）。补上这一档后它既能正确分列、
# 又不会切坏公式。
# 空白只允许同行内的（[ \t]，不含换行）：`\s*` 会让这条正则从上一行的收尾 $ 跨到
# 下一行的裸标签上（线上 id=626），把本来配对好的公式切散。
_LABEL_MATH_OPEN = re.compile(r"\$[ \t]*(?:\\displaystyle[ \t]*)?[A-D][.．]")


def _label_pattern(text: str) -> re.Pattern:
    """选这段正文该用哪条标签正则。三档按「切点安全性」从高到低试：

      1. _LABEL_DOLLAR   `$\\displaystyle A.$ 内容`  —— 标签自成一对 $，最规范
      2. _LABEL_MATH_OPEN `$\\displaystyle A.~内容$` —— 切点落在数学区开头的 $ 上
      3. _LABEL_BARE     `A. 内容`                  —— 裸标签，切点在数学区外

    取第一个能凑够 2 个不同字母的档。顺序不能反：裸正则能匹配上前两档内部的
    `A.`，先试它就会切进数学区（`! Missing $ inserted.`，见 _label_hits）。
    """
    for pattern in (_LABEL_DOLLAR, _LABEL_MATH_OPEN, _LABEL_BARE):
        if len({letter for letter, _s in _label_hits(text, pattern)}) >= 2:
            return pattern
    return _LABEL_BARE


def _label_hits(text: str, pattern: re.Pattern) -> list[tuple[str, int]]:
    """定位选项标签 → [(字母, 起点), ...]，**跳过落在 $...$ 内部的裸标签**。

    为什么要跳：`$\\displaystyle A.~4$` 这种「标签与选项内容同在一个 $...$ 里」的
    写法（规范化 md 的常见产物）匹配不上 _LABEL_DOLLAR —— 那条正则要求标签自成
    一对 $ —— 于是退到 _LABEL_BARE，而裸正则会直接切在数学区内部的 `A.` 上。
    切开处两侧各留半个 $，pandoc 把落单的 $ 转义成 \\$，xelatex 报
    `! Missing $ inserted.`（线上 id=665 就是这个形态）。
    _LABEL_DOLLAR 匹配的是完整 `$..$` 标签、本身配对，故只在裸模式下过滤。

    切分与列数判定的**唯一入口**：_split_at（拆挤行选项）与 _choice_spans
    （切题干/选项/尾部）都走这里，两处不会再各自漏掉这道过滤。
    """
    spans = [(m.start(), m.end()) for m in _MATH_RE.finditer(text)]
    hits: list[tuple[str, int]] = []
    for m in pattern.finditer(text):
        if pattern is _LABEL_BARE:
            # 裸标签：落在数学区**内部**的不算（切在那里会让 $ 落单）
            if any(s < m.start() < e for s, e in spans):
                continue
        elif pattern is _LABEL_MATH_OPEN:
            # 这一档的切点是「数学区的起始 $」，所以匹配到的 $ 必须真是某个数学区的
            # 开头。不校验的话 `… N =$\n A. $\displaystyle (1,2)$` 里那个**收尾**的
            # $ 也会被 `\s*` 跨过换行匹配上（线上 id=626），切开后 `=` 那半个公式的
            # $ 反而落单 —— 正是本档要修的毛病，方向还搞反了。
            if not any(s == m.start() for s, _e in spans):
                continue
        mm = re.search(r"[A-D]", m.group(0))
        if mm:
            hits.append((mm.group(0), m.start()))
    return hits


def _split_at(line: str, pattern: re.Pattern) -> list[str]:
    """在 pattern 每个匹配的起点处切分（保留标签），返回非空片段。

    候选起点走 _label_hits：数学区内部的裸标签不能当切点，切在那里会让 $ 落单
    （见那个函数的 docstring）。
    """
    starts = [start for _letter, start in _label_hits(line, pattern)]
    if len(starts) < 2:
        return [line]
    starts.append(len(line))
    parts = []
    # 第一个标签之前的内容（题干残留，通常没有）单独保留
    head = line[: starts[0]].strip()
    if head:
        parts.append(head)
    for i in range(len(starts) - 1):
        seg = line[starts[i]: starts[i + 1]].strip()
        if seg:
            parts.append(seg)
    return parts


def _format_options(body: str) -> str:
    """把挤在一行的 A./B./C./D. 选项拆成段落（段间空行）。

    避坑 #1：pandoc 会合并无空行的连续行，所以每个选项独占一段。
    只处理明显含多个选项标签的行，避免误伤正文。
    """
    lines = body.splitlines()
    out: list[str] = []
    for line in lines:
        # 「够不够两个标签」与「切在哪」用同一套候选（都走 _label_hits/_label_pattern）：
        # 用裸 findall 计数、用过滤后的起点切，会在「数学区里有两个 A./B.」时数到 2
        # 却切不出片段，那行原样漏过去 —— 与其不一致，不如两处同源。
        pattern = _label_pattern(line)
        if len({letter for letter, _s in _label_hits(line, pattern)}) >= 2:
            out.append("\n\n".join(_split_at(line, pattern)))
        else:
            out.append(line)
    return "\n".join(out)


# 选择题作答括号：按用户要求去掉题干末尾的作答留白+空括号（原为 $\qquad(\qquad)$）。
# 保留此常量（拼接处仍引用）以便将来需要时一处开关；选项仍由 _raw 另起一段渲染。
_ANSWER_BRACKET = ""

# 题干开头的「题号」和「分值」：导出时统一由 _render_block 重新编号，正文里残留的
# 原始题号/分值（如 `19.（17分）`、`（17 分）`）要剥掉。两条规则各自可选、按序剥：
#   1. 题号：1~3 位数字 + 分隔符(. ． 、 , ， ) ）)，但分隔符后不能紧跟数字——
#      用负向前瞻挡掉小数(3.14)和千分位(1,234)，避免误删正文里的数字。
#   2. 分值：括号内必须含「分」字才剥，从而不碰来源标注 `【2024 天津，19】`
#      （中括号、无「分」）等含数字但非分值的内容。
_LEAD_NUM_RE = re.compile(r"^\s*\d{1,3}\s*[.．、,，)）](?!\d)\s*")
_LEAD_SCORE_RE = re.compile(r"^\s*[（(]\s*\d+\s*分\s*[)）]\s*")
# 残留「（N）」题号（N≥2，2026-10-04 审查：306 道真实题以此开头，渲染出
# 「1.（10）…」双题号；解答题还会被当首个 \qsubopen 多缩进一级）。要求数字后
# 紧跟右括号，天然不咬分值形态「（10 分）」；N==1 保留（无法与合法「（1）」小问
# 起始区分，宁可少剥）。
_LEAD_PAREN_NUM_RE = re.compile(r"^\s*[（(]\s*(\d{1,3})\s*[)）]\s*")


def _strip_leading_label(body: str) -> str:
    """剥掉题干开头残留的原始题号与分值（见上方三条正则的说明）。"""
    s = body.lstrip()
    s = _LEAD_NUM_RE.sub("", s, count=1)
    s = _LEAD_SCORE_RE.sub("", s, count=1)
    m = _LEAD_PAREN_NUM_RE.match(s)
    if m and int(m.group(1)) >= 2:
        s = s[m.end():]
    return s

# 图片位置哨兵：staging 阶段（_stage_images._rewrite）把每个图引用**原位**换成
# QFIGSLOT<n>，n 即该题正文里图片出现的序号（从 0 起，与 img_layouts 的 i 同源）。
#
# 为什么留哨兵而不是像旧版那样把图抽到题末：抽走之后「图在题干中间」的信息就丢了，
# 图只能排在题末、原来的位置留下一段空白（用户报的正是这个）。留哨兵则把「位置」
# 一路带到渲染分支，由 plan_figs 判定每张图该原位排、排题末、还是配到某个选项上。
#
# 形态与表格 base64 令牌是同一套手法：纯大写字母+数字，不含 `.`、`$`、
# `（`、反斜杠，故 _format_options / _LABEL_BARE / _SUBQ_LINE_RE /
# _escape_stray_backslash 这批按行按标签扫描的正则一个都咬不到它。
# **唯一需要显式剔除它的地方是 _visible_len**（否则选项里的哨兵被算成可见宽度，
# 选项被估长 → choice_cols 退化成 1 列，与表格令牌同一个坑）。
_SLOT_SENT = "QFIGSLOT"
_SLOT_RE = re.compile(r"QFIGSLOT(\d+)")


# 命令的“额外水平宽度”权重（命令名本身不占可见宽，只按其视觉体积加成）：
# - 只增高不增宽的（箭头/上划线/向量装饰）→ 0
# - 略微增宽的（根号有个勾、希腊字母约 1 个字符宽）→ 1
# 未列出的命令默认 +1。分式单独处理（见 _frac_width），不走这张表。
_CMD_WIDTH = {
    "vec": 0, "overrightarrow": 0, "overline": 0, "hat": 0, "bar": 0,
    "dot": 0, "tilde": 0, "widehat": 0, "widetilde": 0, "boldsymbol": 0,
    "mathrm": 0, "mathbf": 0, "left": 0, "right": 0, "displaystyle": 0,
    "sqrt": 1, "cdot": 1, "times": 1, "div": 1, "pm": 1, "mp": 1,
}
_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
# 关系/二元运算符字符：displaystyle 下两侧各带 ~0.5 字符的粗间距，计宽时 +1。
_OP_CHARS = "=<>+\u2212\u00b1\u2213"


def _frac_width(m) -> str:
    """把 \\frac{分子}{分母} 折成一个宽度约为 max(分子,分母)+1 的占位串。
    分式上下堆叠，水平宽度取分子/分母较宽者，而非两者相加；再 +1 计分数线
    与两侧的竖直留白，且下限 3——旧写法把 \\dfrac{1}{2} 估成 1 个字符宽，
    displaystyle 分式密集的选项被误判「短」排成 4 列后溢出（2026-10-04 审查）。
    返回等宽占位（用 'x' 填充），供外层继续计可见字符。"""
    num, den = m.group(1), m.group(2)
    return "x" * max(max(_visible_len(num), _visible_len(den), 1) + 1, 3)


def _visible_len(text: str) -> int:
    """粗估一段数学/文本的可见水平宽度（以西文字符宽为单位），用于决定 tasks 列数。
    精确宽度要靠 TeX 量盒子（成本高），这里用启发式：
      1. 先把 \\frac{}{} 折成 max(分子,分母) 宽的占位（分式堆叠，不横向累加）；
      2. 命令名本身不算宽，只按 _CMD_WIDTH 加其视觉体积（箭头/向量=0，根号=1，默认1）；
      3. 中文等全角字符按 2 宽计（渲染约占两个西文字符）；
      4. 其余可见字符（数字/字母/运算符）各计 1，花括号/空白/$ 不计。
    """
    s = _TABLE_TOKEN_RE.sub("", text)        # 表格令牌不占可见宽（base64 串很长，
                                             # 不剔掉会把选项估成超长 → 强制 1 列）
    s = _SLOT_RE.sub("", s)                  # 图片位置哨兵同理：选项里配了图的题
                                             # （四图配选项）不剔掉会被估成超长 → 1 列
    s = _LABEL_DOLLAR.sub("", s)             # 去 $\displaystyle A.$ 标签
    # `$\displaystyle A.~4$` 这档只去掉标签部分、保留后面的 $ 和内容（内容要计宽）
    s = _LABEL_MATH_OPEN.sub("$", s, count=1)
    s = re.sub(r"(?<![A-Za-z\\])[A-D][.．]", "", s, count=1)  # 去裸标签
    s = s.replace("$", "")
    # 1. 分式先行折叠（可能嵌套，反复替换到稳定）
    prev = None
    while prev != s:
        prev = s
        s = _FRAC_RE.sub(_frac_width, s)
    # 2. 命令按类型加成，随后从串里移除命令名
    width = 0
    for name in re.findall(r"\\([a-zA-Z]+)", s):
        width += _CMD_WIDTH.get(name, 1)
    s = re.sub(r"\\[a-zA-Z]+", "", s)
    # 3-4. 逐字符计宽：全角×2，花括号/空白不计，其余×1；关系/二元运算符
    # 额外 +1——displaystyle 下两侧的 \thickmuskip/\medmuskip 粗间距约一个字符
    # 宽（2026-10-04 审查：`R<P<Q` 式选项原估 5、实际排版 ~7，4 列格互相压字）。
    s = re.sub(r"[{}\s]", "", s)
    for ch in s:
        w = 2 if ord(ch) > 0x2E7F else 1   # CJK/全角起点之后按 2 宽
        if ch in _OP_CHARS:
            w += 1
        width += w
    return width


def choice_cols(parts: list[str], width_fraction: float = 1.0) -> int:
    """按最长选项的可见宽度和可用栏宽决定列数。

    整页宽时仍是短(<=8)→4、中(<=26)→2、长→1；阈值为每格的内容宽度预留了
    A-D 标签与标签后间距。图文分栏时把两档阈值按文字栏占整页的比例同步缩小。
    此前选项虽然按公式长度选了两列，却仍拿整页阈值判断已经被右图压窄的左栏，
    长根式会从左栏溢出并盖到图片上。

    公开函数：页面渲染（qrender.render_body）与导出（_choice_tasks）共用，
    保证卡片上的选项列数与 PDF 里 tasks 环境的列数一致。
    """
    try:
        fraction = min(1.0, max(0.1, float(width_fraction)))
    except (TypeError, ValueError):
        fraction = 1.0
    longest = max((_visible_len(p) for p in parts), default=0)
    if longest <= 8 * fraction:
        return 4
    if longest <= 26 * fraction:
        return 2
    return 1


def _practice_choice_cols(parts: list[str]) -> int:
    """双栏刷题的选择题列数：按半页栏宽采用更保守的 4/2/1 列阈值。

    普通试卷的阈值面向整页版心，直接复用会把部分中等选项误判成 4 列；随后即使
    用不可换行盒保护完整选项，也只会变成越过单元格边界。双栏按最长可见宽度
    <=6 排 4 列、<=18 排 2 列，其余排 1 列，让“多列不换行”与“不溢出”同时成立。
    4 列阈值 2026-10-04 由 7 收紧到 6：`R<P<Q` 类不等式选项计宽修正后为 7，
    恰卡旧阈值放行 4 列并实测互相压字（审查：300 题样本 45 个 tasks 块溢出）。
    """
    longest = max((_visible_len(p) for p in parts), default=0)
    if longest <= 6:
        return 4
    if longest <= 18:
        return 2
    return 1


def _escape_choice_option(text: str) -> str:
    """转义进入 raw ``tasks`` 环境的选项文本，保护其中的普通 TeX 字符。"""
    parts = _MATH_SPLIT_RE.split(str(text or ""))
    for i, part in enumerate(parts):
        if i % 2 == 0:
            parts[i] = _tex_text(part)
    return "".join(parts)


def split_choice_options(body: str) -> tuple[str, list[str], str] | None:
    """选择题正文 → (题干, [选项…], 尾部)；识别不到有效选项区时返回 None。

    选项标签（`$\\displaystyle A.$` 或裸 `A.`）在 body 里可能挤在同一行、
    每个独占一行（长选项常见）、或两者混合。故不按「行」找，而是在**整个 body**
    上用 finditer 定位所有 A~D 标签起点，从第一个标签切开：其前为题干，
    每个标签起点到下一标签起点为一个选项。选项内部多余换行/空格压成单空格
    （选项文本本应连续）。少于 2 个不同标签则判定为「非标准选择题」返回 None，
    由调用方回退，避免误伤。

    **末选项的右边界是「本行结束」，不是「正文结束」**：最后一个标签之后没有下一个
    标签兜着，直接取到结尾会把选项区后面的内容（「参考公式：…」这类附注）整段吞进
    D 里。吞进去不只是难看——`choice_cols` 按最长选项算列数，末项一长就退化成 1 列，
    整题看着「没分列」（实测线上 id=413 正是如此：三项宽 3/3/4，末项 109）。
    故在末标签所在行的换行处截断，其后内容作为「尾部」单独返回，由调用方排在选项
    之后（`_choice_tasks` 接在 tasks 环境后、`qrender` 放进 .q-tail），**内容一个字
    都不丢**，只是不再算进选项宽度。

    按行截断的依据：线上 100 道选择题里「非末选项跨行」**0 例**——规范化 md 产出的
    选项一律单行。真出现末选项折行的极端数据，代价也只是那半句被排到选项下方，
    而不是整题丢分列；反过来「取到结尾」的代价是**默默**丢掉分列，更难发现。
    图片标记不在此列：两条路径都在调用本函数前就把图摘走了
    （导出侧 `_extract_mark`、页面侧 `qrender._strip_imgs`），故尾部不会是图。

    公开函数：导出（_choice_tasks → tasks 环境）与页面（qrender.render_body →
    选项网格）共用，两侧分列结果因此天然一致——**别在 JS 里重写一遍**。
    """
    spans = _choice_spans(body)
    if spans is None:
        return None
    stem_end, opt_spans, opts_end = spans
    stem = mechfix.strip_choice_answer_blank(body[:stem_end].rstrip())
    opts = [re.sub(r"\s+", " ", body[s:e].strip()) for s, e in opt_spans]
    tail = body[opts_end:].strip()
    return stem, opts, tail


def _choice_spans(body: str) -> tuple[int, list[tuple[int, int]], int] | None:
    """选择题正文 → (题干终点, [(选项起, 选项止), ...], 尾部起点)，全是 body 上的下标。

    从 split_choice_options 里拆出来的**同一套切分**（切法与理由全见那个函数的
    docstring），只是返回下标而非切好的字符串：plan_figs 要判断「某个图片哨兵落在
    题干里、某个选项里、还是选项区之后」，只有下标能回答这个问题。
    split_choice_options 现在就是本函数 + 取子串，切分规则因此仍只有一份。
    """
    # 标签形态三档由 _label_pattern 选，数学区内部的裸标签由 _label_hits 滤掉
    # （否则切开处 $ 落单 → pandoc 转义成 \$ → xelatex `! Missing $ inserted.`）。
    seq = _label_hits(body, _label_pattern(body))
    if len({s[0] for s in seq}) < 2:
        return None
    # 题干里可能出现「图象 C.」「论断 B.」这类假标签：从"第一个标签"切会把题干
    # 截断、把提问句当第一个选项（2026-10-04 审查实测 2007 安徽理卷第 6 题）。
    # 选项区的真标签必然构成字母**严格递增**序列——取最长递增段的起点作切点，
    # 它之前的假标签留给题干。无任何递增段（散点/重号等坏数据）则放弃切分，
    # 整题按原文渲染比切歪更稳。
    best_from, best_len = 0, 1
    run_from, run_len = 0, 1
    for i in range(1, len(seq)):
        if seq[i][0] > seq[i - 1][0]:
            run_len += 1
        else:
            run_from, run_len = i, 1
        if run_len > best_len:
            best_from, best_len = run_from, run_len
    if best_len < 2:
        return None
    if best_from > 0:
        seq = seq[best_from:]

    # 末选项到本行末为止，行以后的内容归尾部（见 split_choice_options 的 docstring）
    last_start = seq[-1][1]
    nl = body.find("\n", last_start)
    # MinerU 的图片选项通常把最后一个选项写成“D.\nQFIGSLOT...文字”。
    # 仅取 D 标签所在行会把 D 误判为尾部，四图配选项因此失效。只要最后
    # 一个标签后紧邻图片哨兵，就把后续内容视为该选项，题干/解析已在调用
    # 方切开，不会吞掉下一题。
    continuation = body[last_start:] if nl < 0 else body[nl:]
    # 续接仅当"标签行之后无空行"——旧版 `\s*` 跨空行，会把「选项区之后空一行
    # 再放的题末图」也吞进末选项，误触发四图配对、整题版式被推倒重排
    # （2026-10-04 审查）。(?<!\n) 保证哨兵所在行之前没有空行（"\n\nQS" 不续）。
    image_option_tail = bool(re.search(r"(?<!\n)\n[ \t]*QFIGSLOT\d+\b", continuation))
    opts_end = len(body) if nl < 0 or image_option_tail else nl

    spans: list[tuple[int, int]] = []
    for i, (_letter, start) in enumerate(seq):
        end = seq[i + 1][1] if i + 1 < len(seq) else opts_end
        if body[start:end].strip():          # 空片段丢掉（与旧实现一致）
            spans.append((start, end))
    if len(spans) < 2:
        return None
    return seq[0][1], spans, opts_end


def _choice_tasks(body: str, want_parts: bool = False,
                  nowrap_multicol: bool = False,
                  width_fraction: float = 1.0):
    """选择题渲染：题干末尾加作答括号并另起一行，选项用 tasks 环境自适应分列。

    选项标签（`$\\displaystyle A.$` 或裸 `A.`）在 body 里可能：
      - 挤在同一行（`A. .. B. .. C. .. D. ..`），或
      - 每个选项独占一行（长选项常见），或两者混合。
    故不按“行”找，而是在**整个 body**上用 finditer 定位所有 A~D 标签起点，
    从第一个标签切开：其前为题干、从标签起到下一标签为一个选项、最后一个选项
    之后可能有残留。识别不到 >=2 个连续标签则退回 _format_options（不误伤非标准题）。
    图片标记已由调用方 _q_md 通过 _extract_mark 提前摘掉，这里不用再处理。

    want_parts=True 时不把「题干」和「选项 tasks 环境」拼成一个字符串返回，而是
    分开返回 (stem_with_bracket, tasks_env_raw)，供 _q_md 在“选择题+图文分栏”
    场景下把题干整题宽渲染、只让选项和图片进两栏（见 _place_choice_split）。
    识别不到有效选项区时返回 (None, 已处理正文)，调用方应退回普通整题渲染。

    选项区之后的尾部（「参考公式：…」这类附注，见 split_choice_options）接在
    tasks 环境**之后**单独成段：它不属于任何选项，混进 D 会让列数退化成 1。
    want_parts 分支把它作为第三个返回值单独交出，**不能并进 tasks_env**——
    tasks_env 会被 _place_choice_split 塞进 raw-latex 块，尾部若跟着进去就绕过了
    pandoc，里面的 markdown 一律失效。

    nowrap_multicol 仅供双栏刷题使用：4 列或 2 列时用 \\mbox 把每个选项锁成
    不可换行整体；1 列仍允许自然换行。这样不会出现同一选项在窄栏里折成两行，
    同时长选项仍可通过 1 列布局正常排版。

    普通模式的列数判定走公开的 choice_cols，与题库页面保持一致；双栏刷题因为
    实际栏宽只有半页，改走 _practice_choice_cols，避免照搬整页阈值后强行不换行
    导致内容越过单元格。
    """
    parts = split_choice_options(body)
    if parts is None:
        formatted = _format_options(body)
        return (None, formatted, "") if want_parts else formatted
    stem, opts, tail_text = parts
    # 尾部若以「答案/解析/解法/评析」开头：解析混进题干的污染形态，从渲染中
    # 撤下（题库原文不动，便于按日志回捞修正），避免整段答案印在选项下方。
    if tail_text and _TAIL_SOLUTION_LEAD_RE.match(tail_text):
        logger.warning("[导出] 选择题尾部文本疑似解析混入，已从渲染撤下：%.60s…",
                       tail_text.replace("\n", " "))
        tail_text = ""

    cols = (_practice_choice_cols(opts) if nowrap_multicol
            else choice_cols(opts, width_fraction))
    # tasks 环境进入 raw LaTeX；选项文本不再经过 Pandoc 的普通文本转义，
    # 因此必须自行保护 TeX 特殊字符（尤其百分号会注释掉剩余题项并触发
    # ``Runaway argument ... environment tasks``）。数学区由
    # ``_escape_choice_option`` 保持原样，普通文本只转义 TeX specials。
    rendered_opts = [_escape_choice_option(p) for p in opts]
    rendered_opts = ([f"\\mbox{{{p}}}" for p in rendered_opts]
                     if nowrap_multicol and cols > 1 else rendered_opts)
    tasks_body = "\n".join(f"  \\task {p}" for p in rendered_opts)
    tasks_env = f"\\begin{{tasks}}({cols})\n{tasks_body}\n\\end{{tasks}}"

    if want_parts:
        return stem + _ANSWER_BRACKET, tasks_env, tail_text

    # 题干 + 作答括号，再另起一行接 tasks 环境（raw-latex 块天然另起段落）
    out = stem + _ANSWER_BRACKET + _raw(tasks_env)
    return f"{out}\n{tail_text}\n" if tail_text else out


# ---------------------------------------------------------------------------
# 分页/留白 raw-LaTeX 原语
# ---------------------------------------------------------------------------

CLEARPAGE = "\n\n```{=latex}\n\\clearpage\n```\n"
# 半页块：每题严格占正文高度的一半（\textheight 不含页眉页脚），两题塔满一页。
# 用 \vbox to 0.5\textheight，内容顶对齐、\vss 吸收底部余白；块间 \nointerlineskip
# 消除基线间距累加，保证两块之和恰为 \textheight，不会把第二题挤到下一页。
_HALF_OPEN = ("\n\n```{=latex}\n\\nointerlineskip\\vbox to 0.5\\textheight\\bgroup"
              "\\vskip0pt\\noindent\\begin{minipage}[t]{\\linewidth}\n```\n")
_HALF_CLOSE = "\n\n```{=latex}\n\\end{minipage}\\vss\\egroup\n```\n"

# 自适应槽位（见 exam_template.tex 的 \qslotopen/\qslotclose）：目标槽位放不下
# 就自动升级 1/4 → 半页 → 整页，并按本页余量自行决定是否 \clearpage。
# 与 _HALF_OPEN 的区别是「固定高度」vs「目标高度 + 超出则升级」——後者才能实现
# 「超过半页的题独占整页、下一题另起一页」，而这个判断只有 TeX 量得准。
_SLOT_QUARTER = 0.25
_SLOT_HALF = 0.5

# inline 解析不能放进固定高度的作答槽位：槽位会把正文装进 lrbox/minipage，
# 而解析（尤其是 wrapfigure）必须在题号列表和测量盒外紧跟题后渲染。遇到这些
# 布局时，inline 模式改用自然高度题块；none/separate 仍使用原有槽位。
_INLINE_NATURAL_LAYOUTS = {"half", "slot_half", "slot_quarter"}


def _slot_open(frac: float) -> str:
    return (f"\n\n```{{=latex}}\n\\qslotopen{{{frac}}}\n```\n")


_SLOT_CLOSE = "\n\n```{=latex}\n\\qslotclose\n```\n"

# 槽位比例是相对「本页可用高度」还是「整个版心」——两类规则的语义不同，见
# exam_template.tex 里 \ifqslotpagerel 的注释。note/handout/lecture 数的是「一页
# 几题」（首页有标题块也得放满），exam 要的是「半张纸的作答空间」（不许压缩）。
_SLOT_PAGEREL_MODES = {"note", "handout", "lecture"}


def _slot_mode_latex(rel: str) -> str:
    return f"```{{=latex}}\n\\qslotpagerel{rel}\n```\n\n"
# 半页块里原位图的限高（\textheight 倍数）。块高锁死 0.5\textheight，题干文字、
# 选项、作答空间都要从这里面出，故给图留约三成、剩下的够排 8~10 行正文。
# 全页上限是 \qfigmaxh 的默认 0.4（\qhcapdefault），半页块单独压到这个值。
_HALF_INLINE_HCAP = 0.15
# practice 尾图/切片图下发的 \qfigmaxh 局部覆盖。与模板 \qhcappractice(0.22) 同值：
# 模板 qpracticebegin 里也设 0.22，两处都是真相源，改动必须同步。
# （图片高度上限全景见 exam_template.tex 顶部「图片高度上限全景」块。）
_PRACTICE_IMG_HCAP = 0.22

# 题型归类
# 单选、多选在导出里是两个独立大题，但渲染层（ABCD 选项 + 作答括号）完全一致，
# 故合并成 _CHOICE 供 _q_md 的渲染分支判断；分大题只在 _paginate_exam 按 _SINGLE
# / _MULTI 分桶。
_SINGLE = {"单选题"}
_MULTI = {"多选题"}
_CHOICE = _SINGLE | _MULTI
_BLANK = {"填空题"}
_SOLVE = {"解答题"}
_CN_NUM = ["一", "二", "三", "四", "五", "六"]


def _norm_split(v) -> str | None:
    """把图片位置字段归一化，向后兼容旧布尔值。

    历史数据里 img_split 存的是整数 1（= 仅选项与图分栏），升级为多态字符串后：
      - None / 0 / "" / "off"  → None（关闭）
      - "full"                 → "full"（选择题：题干+选项一并与图左右分栏）
      - "sub"                  → "sub"（解答题：仅小问与图分栏，题干整行）
      - "between"              → "between"（题干与选项/小问之间）
      - "after"                → "after"（完整题目之后）
      - "pair"                 → "pair"（选择题：四张图一图配一选项，见 plan_figs）
      - 其余真值（1 / True / "opts" / 旧数据）→ "opts"（仅选项与图分栏）

    注意本函数**不区分「没设过」和「明确关掉」**（两者都归 None）。要用到这层区别
    的默认值判定见 resolve_split。
    """
    if v is None or v == 0 or v == "" or v == "off":
        return None
    if v == "full":
        return "full"
    if v in ("sub", "between", "after"):
        return v
    if v == "pair":
        return "pair"
    return "opts"


def resolve_split(qtype, img_split, has_img: bool) -> str | None:
    """算出**实际生效**的图文分栏模式，处理「没设过」时的默认值。

    默认值只在 img_split 为 SQL NULL（= 用户从未点过这题的分栏按钮）时生效：
      - 选择题 + 有图 → "full"（整题分栏）。带图的选择题几乎总是「图在右、题文在
        左」的版式，默认关掉等于每道图题都要手点一次。
      - 其余情况 → None（关）。**解答题默认不小问分栏**：小问分栏会把小问挤进半
        栏宽，小问一长就很难看，只在用户明确要求时才开。
    明确关掉的存 "off"（见 db.set_img_split），不是 NULL，故仍归 None ——
    否则默认值会把「关」重新变成「开」，按钮点了没反应。

    页面（qrender.render_body）与导出（_q_md）共用本函数，两侧默认值因此一致。

    has_img 由调用方给，语义是「**有尾图**」（plan_figs 的 has_tail），不是「有图」：
    默认分栏是把图挪到右栏，只有本来就排在题末的图才该被这么挪。真夹在题干文字
    中间的图（plan_figs 判 pos="stem"）若也触发默认分栏，就会被拽出原位——那正是
    要修的毛病。库里现有带图选择题的图都在「题干末尾、选项之前」，plan_figs 判为
    尾图，故默认分栏行为与改动前一致。
    """
    if img_split is None and has_img and qtype in _CHOICE:
        return "full"
    return _norm_split(img_split)


# 分小问序号（行首）：（1）/（2）… 阿拉伯数字为一级小问；（i）（ii）… 罗马小写、
# （Ⅰ）（Ⅱ）… Unicode 罗马数字（U+2160–U+217F）为二级小问。两者在导出里落在
# **同一个缩进列**（不再为二级另开一层，见 _render_subquestions），两条正则仍分开
# 写以保留语义、便于将来重新分级。括号内须“全是”对应字符才算序号，故 （多选）/
# （本题12分）/（0,-2） 不会误伤；行首以 $ 开头的公式行也不匹配（不以 （ 开头）。
_SUBQ_TOP_RE = re.compile(r"^（\s*[0-9０-９]+\s*）")
_SUBQ_SUB_RE = re.compile(r"^（\s*(?:[ivxIVX]+|[Ⅰ-ⅿ]+)\s*）")
_SUBQ_LINE_RE = re.compile(
    r"^[ \t　]*（\s*(?:[0-9０-９]+|[ivxIVX]+|[Ⅰ-ⅿ]+)\s*）"
)

# 解析里常见的分步/分法小标题：方法1/方法一、解法1/解法一、证法1/证法一……
# 独占一行时才算——这几个词本身也会出现在句子中间（"这种解法…"），只在行首匹配
# 才不会误把叙述性文字断开。数字后允许紧跟冒号/顿号或什么都不接（"方法一"独占一
# 行、下一行才是内容，与"方法一：设…"同一行两种写法都要认）。
_METHOD_HEADER_RE = re.compile(
    r"^[ \t　]*(?:方法|解法|证法|解答|做法)\s*"
    r"(?:[0-9０-９]+|[一二三四五六七八九十]+)\s*[:：.．、]?\s*"
)


def _break_subquestions(body: str) -> str:
    """让每个分小问（（1）（2）/（i）（ii）/（Ⅰ）…）另起一行。

    规范化 md 里小题各占一行，但有时仅以“单个换行”分隔；pandoc 会把段内单
    换行并成空格，使多个小题挤在一行。这里在每个“行首即小题序号”的行前补一个
    空行（独立段落 → 强制换行），并去掉其前导缩进（避免 ≥4 空格被当成代码块）。
    已有空行分隔的（本就正常换行）不会重复插空行。数学式 $...$ 里的括号不受影响
    （它们不在行首、且不以 （ 开头）。
    """
    lines = body.splitlines()
    out: list[str] = []
    for line in lines:
        if _SUBQ_LINE_RE.match(line):
            stripped = line.lstrip(" \t　")
            if out and out[-1].strip() != "":
                out.append("")          # 与上文空行分隔 → 另起一段
            out.append(stripped)
        else:
            out.append(line)
    return "\n".join(out)


def _break_solution_lines(text: str) -> str:
    """解析正文里的分小问序号、方法/解法/证法标题，各自另起一段。

    与 _break_subquestions 同样的手法（行前补空行强制断段），但多认一种标记：
    _METHOD_HEADER_RE。解析不走 _render_subquestions 那套缩进列表——那是题干用的
    \\qsubopen/\\qsubitem 编号列表宏，解析只要求“自动换行”，不需要另起一级缩进，
    所以这里只插空行，不生成列表标记。
    """
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if _SUBQ_LINE_RE.match(line) or _METHOD_HEADER_RE.match(line):
            stripped = line.lstrip(" \t　")
            if out and out[-1].strip() != "":
                out.append("")
            out.append(stripped)
        else:
            out.append(line)
    return "\n".join(out)


def _render_subquestions(body: str) -> str:
    """把分小问渲染成「单层」缩进列表：（1）（2）… 与（i）（ii）/（Ｉ）（Ⅱ）… 共用
    同一个缩进列（用 exam_template.tex 里的 \\qsubopen/\\qsubitem/\\qsubclose 三个
    list 宏实现，与 _q_md 外层 \\qopen 题号列同一套机制）。

    **不再为（i）另开一层嵌套**（2026-07 改）：此前（i）（ii）会嵌在（1）之后再
    缩进一级，三级题号叠起来把正文推得很靠右。现在只保留两级缩进——题号 \\qopen
    一列、全部小问 \\qsubopen 一列，（i）与（1）左端对齐。要恢复三级缩进就把
    _SUBQ_SUB_RE 分支改回单独开 \\qsubopen（见 git 历史）。

    按行扫描：遇到任一级小问序号就开列表（首个）或续 \\qsubitem（后续）；其余行
    原样保留（跟随上一个 \\item 的段落内容，pandoc 换行后天然悬挂缩进对齐到该列
    文字起点，而非序号本身）。全文结束前补齐未收掉的列表。
    未识别到任何小问序号时原样返回（不引入任何列表结构，等价于旧版直通行为）。
    """
    lines = body.splitlines()
    out: list[str] = []
    open_list = False
    for line in lines:
        stripped = line.lstrip(" \t　")
        m = _SUBQ_TOP_RE.match(stripped) or _SUBQ_SUB_RE.match(stripped)
        if m:
            marker, rest = stripped[:m.end()], stripped[m.end():]
            # Fandol 不含 Unicode 罗马数字 Ⅰ-ⅿ，直接排会只报 Missing character
            # 后静默缺字。只规范序号字符本身，保留中文全角括号与正文原样。
            marker = "".join(
                unicodedata.normalize("NFKC", ch)
                if "\u2160" <= ch <= "\u217f" else ch
                for ch in marker
            )
            out.append(_raw(f"\\qsubitem{{{marker}}}" if open_list
                            else f"\\qsubopen{{{marker}}}"))
            open_list = True
            if rest.strip():
                out.append(rest)
        else:
            out.append(line)
    if open_list:
        out.append(_raw("\\qsubclose"))
    return "\n".join(out)


def _num_wrap(num: int | None, core: str) -> str:
    """把题号包成独占一列的悬挂缩进块：\\qopen{num.} 开一层 list（题号即 \\item
    标签），核心正文（core，仍交给 pandoc 正常渲染）跟在其后作为该 item 的段落
    内容，续行自动悬挂缩进对齐到正文起点而非题号；\\qclose 收尾。
    所有导出模式（list/note/lecture/exam/exam_std/handout）都经 _q_md 这一处
    统一调用，保证题号列/悬挂缩进效果一致。
    """
    if num is None:
        return core
    return _raw(f"\\qopen{{{num}.}}") + core + _raw("\\qclose")


def _q_md(num: int | None, body: str, qtype: str = None, img_align: str = None,
          img_width=None, img_split=False, img_layouts=None,
          img_files: list[str] = None,
          plan_body: str = None, inline_hcap: float = None,
          choice_nowrap_multicol: bool = False,
          practice_image_wrap: bool = False,
          box_context: bool = False) -> str:
    """单题 Markdown：题号 + 正文。选择题用 tasks 环境分列排选项 + 作答括号，
    其余题型按原逻辑把挤行选项拆成空行分段。

    图片位置：正文里带着 _stage_images 原位留下的 QFIGSLOT 哨兵，先用 plan_figs
    判出每张图该原位排（图后面还有正文）、排题末、还是配到某个选项上；原位图就地
    换成图片块（_fill_slots），尾图再交给 _place_image 依题型/题卡设置（对齐/宽度/
    图文分栏）决定排版宏，向后兼容旧版默认效果。

    选择题 + 图文分栏是特例：题干不分栏（整题宽渲染），只把选项和图片分左右两栏
    （见 _place_choice_split）——不同于填空题“整题分栏”，故在这里单独截断成
    stem/tasks_env 两部分，不走 _place_image 的通用 split 分支。
    选择题 + 四张图配选项又是一例：tasks 环境放不了图，走 _pair_grid_latex 的
    minipage 网格（见 plan_figs 的 pair 判定）。

    img_files 是 _stage_images 落地的本地文件名列表，下标 = 哨兵编号；不给（老调用
    方/无图）时一切图片分支都不会触发。
    inline_hcap 给原位图一个更小的限高（\\textheight 倍数），供半页块用：那里正文
    高度锁死 0.5\\textheight，按全页上限排的原位图会撑破版面——但把图**挪到题末**
    并不能省下高度，只是把「图夹在文字中间」这个需求丢了，所以限高而不是改落位。
    只作用于原位图：尾图走 _place_image 的既有分支，尺寸与改动前逐字一致。
    plan_body 只在 inline 解析模式下给：那时 body 后面已经拼上了解析文本，若照它判
    落位，题干末尾的图会因「后面还有字」被误判成原位图（默认分栏也就跟着丢了）。
    传原始题干即可让判定与不带解析时逐字一致。"""
    body = _strip_leading_label(body)  # 剥掉正文残留的原始题号/分值，导出统一重编号
    marks = _marks_of(img_files)
    layouts = _parse_layouts(img_layouts)
    # img_split 传原始值（不经 _norm_split）：plan_figs 要靠 NULL/"off" 的区别判
    # 配对的默认值，见它的 docstring
    plan = plan_figs(_strip_leading_label(plan_body) if plan_body else body,
                     qtype, img_layouts, img_split,
                     practice=choice_nowrap_multicol)

    # 四图配选项：一图配一选项的 minipage 网格，不走 tasks，也不走图文分栏
    if plan["pair"] and qtype in _CHOICE:
        parts = split_choice_options(body)
        if parts is not None:
            stem, opts, opt_tail = parts
            support_ids = [s["i"] for s in plan["slots"]
                           if s["i"] not in set(plan["pair_map"])]
            if support_ids:
                stem = _fill_slots(stem, support_ids, marks, layouts, plan,
                                   hcap=inline_hcap)
            grid = _pair_grid_latex([_drop_slots(o) for o in opts], marks,
                                    plan["pair_map"], plan["pair_cols"])
            core = _drop_slots(stem) + _ANSWER_BRACKET + grid
            if opt_tail:
                core = f"{core}\n{_drop_slots(opt_tail)}\n"
            return _num_wrap(num, core)

    # 原位图（pos="stem"）就地换成图片块 —— 这是「图留在文字中间」的落地点。
    # 尾图的哨兵先留着，等下面各分支决定好版式再由 _drop_slots 清掉。
    inline_ids = [s["i"] for s in plan["slots"] if s["pos"] == "stem"]
    tail_ids = [s["i"] for s in plan["slots"] if s["i"] not in set(inline_ids)]
    if inline_ids:
        body = _fill_slots(body, inline_ids, marks, layouts, plan,
                           hcap=inline_hcap)
    body = _drop_slots(body)

    # 以下沿用旧结构，但「图」只剩尾图：分栏/绕排都只针对它们。
    # resolve_split 的 has_img 看的是**有尾图**（不是有图）：真夹在题干中间的图
    # 不该被默认分栏拽出原位，见 resolve_split 的 docstring。
    split_mode = resolve_split(qtype, img_split, bool(tail_ids))
    tail_ids = [i for i in tail_ids if i < len(marks)]
    # 分栏消费首个完整“视觉图片组”，不能再拿第一张、把其余图片拼到题末。
    # 后一种旧写法会把连续两图拆开，使第二张掉到分栏下方。
    if tail_ids:
        split_unit, rest_ids = _split_first_unit(tail_ids, plan)
        w0 = _split_unit_width(split_unit, layouts, img_width)
        tail = _figs_latex_planned(rest_ids, marks, layouts, plan)
    else:
        split_unit = None
        w0 = img_width
        tail = ""
    if (practice_image_wrap and split_mode in ("opts", "full", "sub")
            and split_unit):
        # 双栏刷题的外层栏宽已经只有半页，若继续在栏内套左右 minipage，题干会被
        # 二次压成约 1/4 页宽。这里把所有图文分栏模式统一降维成右浮图：正文先在
        # 图左侧绕排，超过图片高度后自动回到完整栏宽，图片下方的空间不会浪费。
        if qtype in _CHOICE:
            core = _choice_tasks(
                body, nowrap_multicol=choice_nowrap_multicol)
        elif qtype in _SOLVE:
            # wrapfig 不能与 list 嵌套；双栏环绕图时保留小问标记的普通段落形态，
            # 避免 _render_subquestions 生成 qsubopen/qsubitem 列表后把浮图逼走。
            core = _format_options(_break_subquestions(body))
        else:
            core = _format_options(_break_subquestions(body))
        return _place_practice_wrap(
            num, core, split_unit, marks, layouts, tail, width=w0)
    if qtype in _CHOICE:
        if split_mode in ("between", "after") and tail_ids:
            stem, tasks_env, opt_tail = _choice_tasks(
                body, want_parts=True,
                nowrap_multicol=choice_nowrap_multicol)
            if stem is not None:
                figures = _figs_latex_planned(
                    tail_ids, marks, layouts, plan, img_width, img_align)
                options = tasks_env + (f"\n\n{opt_tail}\n" if opt_tail else "")
                core = (stem + figures + options if split_mode == "between"
                        else stem + options + figures)
                return _num_wrap(num, core)
        if split_mode in ("opts", "full") and split_unit:
            txt_frac, _img_frac = _split_fracs(w0)
            stem, tasks_env, opt_tail = _choice_tasks(
                body, want_parts=True,
                nowrap_multicol=choice_nowrap_multicol,
                width_fraction=txt_frac)
            if stem is not None:
                # full=题干+选项一并进左栏、图进右栏；opts=题干占整行、仅选项与图分栏
                full = split_mode == "full"
                return _place_choice_split(
                    num, stem, tasks_env, split_unit, marks, layouts, tail,
                    full=full, width=w0, opt_tail=opt_tail, hcap=inline_hcap)
            core = tasks_env  # 识别不到选项区，tasks_env 此时是已处理正文（回退用法）
        else:
            core = _choice_tasks(
                body, nowrap_multicol=choice_nowrap_multicol)
    elif qtype in (_BLANK | _SOLVE) and split_mode == "between" and tail_ids:
        parts = _split_stem_subs(body)
        if parts is not None:
            figures = _figs_latex_planned(
                tail_ids, marks, layouts, plan, img_width, img_align)
            subs = _render_subquestions(
                _format_options(_break_subquestions(parts[1])))
            return _num_wrap(num, parts[0] + figures + subs)
        # 实验题偶尔没有规范小问序号；此时仍保证图片跟在完整题干后，不因默认布局
        # 写入了 between 就丢图或报错。
        core = _render_subquestions(_format_options(_break_subquestions(body)))
    elif qtype in _SOLVE and split_mode == "sub" and split_unit:
        # 解答题“仅小问分栏”：题干整行、小问与图左右分栏。切不出小问则回退普通排版
        parts = _split_stem_subs(body)
        if parts is not None:
            return _place_solve_split(
                num, parts[0], parts[1], split_unit, marks, layouts, tail,
                width=w0, hcap=inline_hcap)
        core = _render_subquestions(_format_options(_break_subquestions(body)))
    elif qtype in _SOLVE:
        # 解答题：多级小问（（1）（2）… 顶层 / （i）（ii）… 嵌套）渲染成多级缩进列表
        core = _render_subquestions(_format_options(_break_subquestions(body)))
    else:
        # 其余非选择题：先让分小问（（1）（2）…）各自另起一行，再拆挤行选项
        core = _format_options(_break_subquestions(body))
    return _place_image(_num_wrap(num, core), marks, qtype, img_align,
                        img_width, split_mode in ("opts", "full", "sub"),
                        layouts, tail_ids, plan,
                        boxed=box_context or practice_image_wrap,
                        hcap=inline_hcap)


# block 上带图片本地文件名列表的键（题干 / 解析各一份）。下标即 QFIGSLOT 哨兵编号。
# 旧版把文件名嵌在正文末尾的 \qfigmark{file}{cap} 块里，由 _extract_mark 摘回；
# 改成原位哨兵后正文里只剩编号，文件名只能随 block 单独带（见 _img_fields）。
_IMG_FILES_KEY = "_img_files"
_SOL_IMG_FILES_KEY = "_sol_img_files"


def _marks_of(files: list[str]) -> list[tuple[str, str]]:
    """本地文件名列表 → [(文件名, 空图注), ...]。

    题号已经在题目前出现，正式试卷中的题图属于题目内容，不再给每张图重复生成
    「第N题图」。保留二元组形状是为了不扰动既有图片排版函数；以后若增加用户手动
    图注，可继续使用第二项，而不必把自动题号重新塞回来。
    """
    return [(name, "") for name in (files or [])]


def _drop_slots(text: str) -> str:
    """清掉正文里所有 QFIGSLOT 哨兵（图已在别处排好，原位不再需要占位）。

    紧跟哨兵的空行会连带压掉：哨兵独占一段时（`\\n\\nQFIGSLOT0\\n\\n`）只删 token
    会留下三个连续换行，pandoc 把它当成一个空段落，PDF 里多出一截空白。
    """
    out = re.sub(r"[ \t]*" + _SLOT_RE.pattern + r"[ \t]*\n?", "", text)
    return re.sub(r"\n{3,}", "\n\n", out).rstrip()


# ---------------------------------------------------------------------------
# 多图逐图排版：img_layouts
# ---------------------------------------------------------------------------
#
# img_layouts 是 questions 表上的 JSON 列，形如 [{"i":0,"w":40,"align":"left"},
# {"i":1,"w":45}]，i = 该题正文里图片出现的序号（从 0 起）。序号的三处来源必须一致：
# 前端 image-layout.js 的 `.body img` 遍历下标、_extract_mark 返回列表的下标、
# 这里的 i —— 三者都源自正文图片引用的原始先后顺序，故天然对齐。
# 旧的 img_width/img_align 列保留不动，作为 i=0 的兜底（老题无 img_layouts 时
# 导出效果与改动前完全一致），也留作回滚余地。
_LAYOUT_ALIGNS = ("left", "center", "right")


def _parse_layouts(img_layouts) -> dict[int, dict]:
    """img_layouts（JSON 字符串或已解码的 list）→ {序号: {"w": int|None, "align": str|None}}。

    解析失败/格式不符一律返回空字典 —— 排版设置坏了就退回旧的单图行为，
    绝不能因为一列脏数据让整份导出报错。
    """
    if not img_layouts:
        return {}
    data = img_layouts
    if isinstance(data, str):
        import json
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return {}
    if not isinstance(data, list):
        return {}
    out: dict[int, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        w = item.get("w")
        try:
            w = int(w) if w not in (None, "") else None
        except (TypeError, ValueError):
            w = None
        align = item.get("align")
        # stack：该图所在「连续图组」改成上下堆叠（默认并排）。只看组内首图那条，
        # 见 plan_figs 的分组逻辑。存在 img_layouts 里是为了不动 schema。
        out[idx] = {"w": w, "align": align if align in _LAYOUT_ALIGNS else None,
                    "stack": bool(item.get("stack"))}
    return out


def _layout_at(layouts: dict[int, dict], idx: int,
               width=None, align=None) -> tuple[object, object]:
    """取第 idx 张图的 (宽度, 对齐)：img_layouts 里有就用它，否则用传入的兜底值。

    兜底值即旧的 img_width/img_align 两列 —— 只对 i=0 有意义（那两列本就只描述首图），
    调用方给 idx>=1 传兜底时应传 None。
    """
    lay = layouts.get(idx)
    if not lay:
        return width, align
    return (lay["w"] if lay["w"] is not None else width,
            lay["align"] or align)


# 两图并排的宽度上限：两张图占比之和 <= 此值才并成一行（余下 5% 留给 \hfill 间隙）。
# 超过就各自独占一行 —— 硬塞会 Overfull \hbox 溢出版心。
_MULTI_ROW_MAX = 0.95

# 四图配选项模式下每张图的默认占宽（%）：45 使默认排成 2×2。
_PAIR_DEFAULT_W = 45
# 四图配选项排 4 列所要求的单图宽度上限（%）：超过就只排 2 列，否则横向必然溢出。
_PAIR_4COL_MAX_W = 24


def _rest_is_blank(body: str, start: int, end: int) -> bool:
    """body[start:end] 去掉图片哨兵后是否只剩空白。

    判「这张图后面还有没有正文」用。必须先去掉哨兵：题末连着几张图时，后一张的
    哨兵本身不算「正文」，否则每张图都会被判成「中间有图」。
    """
    return not _SLOT_RE.sub("", body[start:end]).strip()


def plan_figs(body: str, qtype: str = None, img_layouts=None,
              img_split=None, practice: bool = False) -> dict:
    """图片编排计划：每张图排在哪、哪几张并排、是否四图配选项。

    img_split 收的是 **questions.img_split 的原始列值**，不是 _norm_split 的结果 ——
    与 resolve_split 同一个理由：配对的默认值只在 SQL NULL（用户从未设过）时生效，
    而 _norm_split 把 NULL 和明确关掉的 "off" 一起归成 None，两者分不开就会让
    「关掉配对」点了没反应（默认值立刻把它变回开）。

    **页面（qrender）与导出（_q_md）唯一共用的图片落位规则**，与
    split_choice_options / choice_cols / resolve_split 同一个路数：规则只写一份，
    卡片上看到的版式与 PDF 因此天然一致。

    入参 body 是 staging 后**仍带 QFIGSLOT 哨兵**的正文（见 _SLOT_SENT）。
    页面侧的哨兵由 qrender._strip_imgs 写入，形态与导出侧一致。

    返回：
      slots  按图片序号排的落位列表，每项 {"i", "pos", "opt", "group"}：
             pos="stem" 原位（图后面还有正文，就地插图，正文不留空）
             pos="tail" 排在题末（含「图在题干末尾、选项之前」这一最常见形态）
             pos="opt"  配在第 opt 个选项上（仅四图配选项模式）
      groups 连续图分组（中间只有空白的相邻图归一组，每组最多 2 张），
             每项 {"ids": [图序号...], "row": 是否并排}
      pair / pair_cols / pair_map  四图配选项：是否启用、列数、每个选项对应的图序号
             （practice=True 时 4 列判定改用双栏半页阈值 _practice_choice_cols，
             与题面列数同源；页面卡片路径不传该参数、保持整页阈值）
      has_tail / has_any  供 resolve_split 判默认分栏用

    识别不出结构时一律退化成「全部当尾图」——与旧版行为等价，绝不因为编排判定
    失败让导出报错。
    """
    layouts = _parse_layouts(img_layouts)
    matches = list(_SLOT_RE.finditer(body))
    plan = {"slots": [], "groups": [], "pair": False, "pair_cols": 2,
            "pair_map": [], "has_tail": False, "has_any": bool(matches)}
    if not matches:
        return plan

    spans = _choice_spans(body) if qtype in _CHOICE else None
    opt_count = len(spans[1]) if spans else 0
    # 解答题的「题干/小问」分界，作用与选择题的「题干/选项区」分界对称：图正好卡在
    # 这个界上（后面只剩小问）时**可以**判成尾图，从而能进小问分栏的右栏。不给这条
    # 出路就是用户报的那个 bug —— has_tail=False，solve_split_reason 拦下请求、
    # 前端弹「这道题没有分小问」，而题里明明有小问。
    #
    # **但只在小问分栏真开着时才判尾图**：分栏关着的时候这张图该老老实实留在原位
    # （题干与小问之间），那正是本版「图夹在中间就排在中间」要的效果。无条件判成
    # 尾图会把它挪到全部小问之后 —— 等于把 v0.6.0 的老毛病放回来（实测线上 q394、
    # q450 这两道 img_split='off' 的题就会被挪走）。
    subs_at = (_subs_start(body)
               if qtype in _SOLVE and _norm_split(img_split) == "sub" else None)

    # --- 逐图定位 ---
    for m in matches:
        try:
            idx = int(m.group(1))
        except ValueError:            # 理论上不会（正则只收数字），防御性兜底
            continue
        pos, opt = "tail", None
        if spans:
            stem_end, opt_spans, opts_end = spans
            if m.start() < stem_end:
                # 题干区：后面还有正文 → 原位；只剩空白（图在选项之前）→ 尾图。
                # 后者正是库里现有带图选择题的形态，判成尾图才能保持默认分栏不变。
                pos = "stem" if not _rest_is_blank(body, m.end(), stem_end) else "tail"
            else:
                for oi, (s, e) in enumerate(opt_spans):
                    if s <= m.start() < e:
                        pos, opt = "opt", oi
                        break
        elif subs_at is not None and m.start() < subs_at:
            # 解答题题干区：图后面到小问之前还有题干正文 → 原位；只剩空白（图正好
            # 夹在题干与小问之间）→ 尾图，可进小问分栏右栏。与上面选择题那条对称。
            pos = "stem" if not _rest_is_blank(body, m.end(), subs_at) else "tail"
        elif not _rest_is_blank(body, m.end(), len(body)):
            pos = "stem"
        plan["slots"].append({"i": idx, "pos": pos, "opt": opt, "group": -1})

    if not plan["slots"]:
        return plan

    # --- 四图配选项判定 ---
    # 恰好 4 个选项 + 4 张选项图。题干可以另有辅助图：只要四张图已经分别落进
    # A-D 选项，就把它们配对，额外图片仍留在题干原位。旧产物若整题恰好四张图且
    # 全部排在选项之前，继续按正文顺序配 A-D。仿 resolve_split
    # 「带图选择题在用户没设过时默认分栏」的思路：默认开，用户可显式关成 "off"。
    pair_ok = False
    pair_slots = []
    if spans and opt_count == 4:
        pair_slots = [s for s in plan["slots"] if s["opt"] is not None]
        opts_of = [s["opt"] for s in pair_slots]
        if len(pair_slots) == 4 and sorted(opts_of) == [0, 1, 2, 3]:
            pair_ok = True
        elif (len(plan["slots"]) == 4
              and all(s["pos"] == "tail" for s in plan["slots"])):
            pair_ok = True
            pair_slots = list(plan["slots"])
        elif (len(plan["slots"]) == 4
              and sorted(s["opt"] for s in plan["slots"]
                         if s["opt"] is not None) == [0, 1, 2]
              and any(s["opt"] is None and s["pos"] == "tail"
                      for s in plan["slots"])):
            # A~C 已各配一图、第 4 张落在最后标签之后（MinerU 四图卷实测形态
            # `D.$\n\nQFIGSLOT3`，空行分隔）：第 4 张按正文顺序补为 D 的图。
            # 全库 284 道「4 图单选题」均为选项配图型（2026-10-04 抽样核验），
            # 误配风险限于「3 个选项图 + 恰在第 4 位的题干示意图」罕见形态。
            pair_ok = True
            pair_slots = sorted(plan["slots"], key=lambda s: s["i"])
    # 默认开、可显式关：NULL（没设过）→ 开，"pair" → 开，其余（"off"/分栏模式）→ 关。
    # 判 `is None` 而不是走 _norm_split，理由见 docstring。
    if pair_ok and (img_split is None or img_split == "pair"):
        plan["pair"] = True
        # pair_map[选项序号] = 图序号。图已落在选项里时按 opt 归位，整批在题末时
        # 按正文出现顺序配 A~D（规范化 md 里图的顺序就是选项顺序）。
        if all(s["opt"] is not None for s in pair_slots):
            plan["pair_map"] = [s["i"] for s in
                                sorted(pair_slots, key=lambda s: s["opt"])]
        else:
            plan["pair_map"] = [s["i"] for s in pair_slots]
        widths = [(layouts.get(s["i"]) or {}).get("w") or _PAIR_DEFAULT_W
                  for s in pair_slots]
        opts_text = [body[s:e] for s, e in spans[1]]
        # 4 列要求「图够窄」且「选项文字本身也排得下 4 列」。阈值与题面同源：
        # 双栏刷题（practice）用半栏的 _practice_choice_cols——整页阈值在窄栏里
        # 会放过 `R<P<Q` 类选项，4 列格必然过窄（2026-10-04 审查）。任一条不满足
        # 退回 2 列 —— 不退到 1 列：单列摆四张图太高，撑页。
        cols_ok = (_practice_choice_cols(opts_text) if practice
                   else choice_cols(opts_text))
        plan["pair_cols"] = 4 if (max(widths) <= _PAIR_4COL_MAX_W
                                  and cols_ok == 4) else 2
        paired = set(plan["pair_map"])
        for s in plan["slots"]:
            if s["i"] in paired:
                s["pos"], s["opt"] = "opt", plan["pair_map"].index(s["i"])
            else:
                # 额外图片是题干辅助图。即使它正好位于“题干末尾/选项之前”，配对
                # 模式下也应显示在选项网格之前，而不是被丢掉或挪到整题末尾。
                s["pos"], s["opt"] = "stem", None
        return plan

    # 用户明确选择了位置后，所有题干图片构成一个视觉组：分栏时整组进入右栏，
    # 非分栏时整组落在题干/选项（小问）的逻辑边界。这样三张以上图片也不会被拆成
    # “右栏一组、题末还掉几张”。未设置的旧题仍按原文相邻关系、每组最多两张处理。
    layout_mode = _norm_split(img_split)
    collect_all = layout_mode in ("opts", "full", "sub", "between", "after")
    if collect_all:
        ids = [s["i"] for s in plan["slots"]]
        plan["groups"] = [{"ids": ids, "row": False}]
        for s in plan["slots"]:
            s["pos"], s["opt"], s["group"] = "tail", None, 0
    # --- 旧题的连续图分组（每组最多 2 张）---
    else:
        for k, s in enumerate(plan["slots"]):
            adjacent = (
                k > 0
                and plan["groups"]
                and len(plan["groups"][-1]["ids"]) < 2
                and s["pos"] == plan["slots"][k - 1]["pos"]
                and s["opt"] == plan["slots"][k - 1]["opt"]
                and _rest_is_blank(body, matches[k - 1].end(), matches[k].start())
            )
            if adjacent:
                plan["groups"][-1]["ids"].append(s["i"])
            else:
                plan["groups"].append({"ids": [s["i"]], "row": False})
            s["group"] = len(plan["groups"]) - 1

    # 组内两张默认并排；组首图标了 stack 则堆叠；两图太宽塞不进一行也退回堆叠
    for g in plan["groups"]:
        if len(g["ids"]) < 2:
            continue
        first = layouts.get(g["ids"][0]) or {}
        total = sum(((layouts.get(i) or {}).get("w") or _DEFAULT_IMG_FRAC)
                    for i in g["ids"]) / 100
        # 明确位置模式下“左右排列”必须容纳任意张图，发射层会按比例缩放到一行；
        # 旧题继续保留宽度和不超过 95% 才并排的历史判据。
        g["row"] = (not first.get("stack")
                    and (collect_all or total <= _MULTI_ROW_MAX))

    plan["has_tail"] = any(s["pos"] == "tail" for s in plan["slots"])
    return plan


def _fig_box(name: str, cap: str, frac: float) -> str:
    """一张图的 \\qfigflexbox（占正文宽 frac，图下附说明）。"""
    return f"\\qfigflexbox{{{frac}}}{{{name}}}{{{cap}}}"


def _figs_rows_latex(rows: list[list[tuple[str, str, float, str]]],
                     hcap: float = None) -> str:
    """已排好行的图 → raw LaTeX。每项 (文件名, 说明, 占宽, 对齐)。

    一行两图：中间 \\hfill 顶开，忽略两图各自的 align（位置由并排关系决定）——
    这是「一题两图并列对比」最常见的诉求。
    一行一图：按自身 align 决定左/中/右。
    **唯一的图片行发射点**：按宽度自动配对（_figs_latex）和按 plan_figs 分组
    （_figs_latex_planned）都归到这里，两条路的行内间距/换行原语因此完全一致。

    hcap（\\textheight 倍数）非空时，这批图的高度上限临时压到该值：整段包在一对
    花括号里改 \\qfigmaxh，TeX group 结束自动恢复，故只影响这批图。半页块里的原位图
    用它——那里正文高度锁死 0.5\\textheight，按全页上限（0.4）排的原位图会撑破版面。
    """
    out = []
    for row in rows:
        if len(row) > 1:
            # 任意数量横排：按用户设置的宽度比例缩放到 96% 行宽内，余量交给
            # \hfill。不能直接照原百分比发射，三张默认 35% 会溢出正文。
            total = sum(item[2] for item in row) or 1
            scale = min(1.0, 0.96 / total)
            boxes = "\\hfill".join(
                _fig_box(item[0], item[1], item[2] * scale) for item in row)
            out.append("\\par\\nobreak\\vspace{0.3em}\\noindent\\hfill"
                       + boxes + "\\hfill\\par\\vspace{0.3em}")
        elif row:
            name, cap, frac, align = row[0]
            before = "" if align == "left" else "\\hfill"
            after = "" if align == "right" else "\\hfill"
            out.append("\\par\\nobreak\\vspace{0.3em}\\noindent"
                       + before + _fig_box(name, cap, frac) + after
                       + "\\par\\vspace{0.3em}")
    if not out:
        return ""
    body = "".join(out)
    if hcap is not None:
        # 花括号成组：\qfigmaxh 的修改随 group 结束自动回退，不必手动存旧值
        body = f"{{\\setlength{{\\qfigmaxh}}{{{hcap}\\textheight}}{body}}}"
    return _raw(body)


def _figs_latex(items: list[tuple[str, str, float, str]]) -> str:
    """多张图 → raw LaTeX，相邻两图占比之和 <= _MULTI_ROW_MAX 时并排成一行。

    没有编排计划（plan_figs）时的兜底配对：按宽度贪心两两成行。有计划时走
    _figs_latex_planned —— 那边的分组还看「两图在正文里是否真的相邻」和用户的
    堆叠选择，比单看宽度准。
    """
    if not items:
        return ""
    rows: list[list[tuple[str, str, float, str]]] = []
    i = 0
    while i < len(items):
        if (i + 1 < len(items)
                and items[i][2] + items[i + 1][2] <= _MULTI_ROW_MAX):
            rows.append([items[i], items[i + 1]])
            i += 2
        else:
            rows.append([items[i]])
            i += 1
    return _figs_rows_latex(rows)


def _plan_rows(ids: list[int], plan: dict) -> list[list[int]]:
    """把一批图序号按 plan_figs 的分组切成「一行一组」。

    同一个并排组（groups[g]["row"] 为真）里的图排一行，其余各自独占一行。
    """
    gmap = {s["i"]: s["group"] for s in plan.get("slots", [])}
    groups = plan.get("groups", [])
    rows: list[list[int]] = []
    for i in ids:
        g = gmap.get(i, -1)
        same = (rows and len(rows[-1]) < 2 and g >= 0
                and g == gmap.get(rows[-1][-1], -2)
                and g < len(groups) and groups[g].get("row"))
        if same:
            rows[-1].append(i)
        else:
            rows.append([i])
    return rows


def _plan_units(ids: list[int], plan: dict) -> list[dict]:
    """把图片切成页面/PDF共用的视觉单元。

    并排组和上下组都必须作为一个整体发射：前者要共享一行，后者要共享一个零间距
    容器。旧的 _plan_rows 会把上下组拆成两个单图行，每行各带外边距，正是两图之间
    出现白缝的根源。
    """
    want = set(ids)
    slot_groups = {s["i"]: s.get("group", -1) for s in plan.get("slots", [])}
    groups = plan.get("groups", [])
    units = []
    emitted = set()
    for idx in ids:
        if idx in emitted:
            continue
        gi = slot_groups.get(idx, -1)
        group = groups[gi] if 0 <= gi < len(groups) else None
        members = ([i for i in group.get("ids", []) if i in want]
                   if group else [idx])
        if not members:
            members = [idx]
        emitted.update(members)
        units.append({"ids": members, "row": bool(group and group.get("row"))})
    return units


def _split_first_unit(ids: list[int], plan: dict) -> tuple[dict, list[int]]:
    """取进入分栏右侧的首个视觉单元及剩余图片序号。

    必须先在完整 ids 上取视觉单元再拆；若先切成 ids[0]/ids[1:]，连续两图的组关系
    会被不可逆地拆散，第二张就会掉到分栏下方。
    """
    units = _plan_units(ids, plan)
    if not units:
        return {"ids": [], "row": False}, []
    rest = [i for unit in units[1:] for i in unit.get("ids", [])]
    return units[0], rest


def _split_unit_width(unit: dict, layouts: dict[int, dict],
                      first_width=None):
    """视觉单元进入右栏时占整道题的宽度百分比。

    横排组取两图宽度之和并留 2% 小间距；纵排组取最宽一张。单图未设宽度仍返回
    None，沿用历史默认的 48/48 分栏。
    """
    ids = unit.get("ids") or []
    if not ids:
        return first_width
    values = []
    explicit = False
    for pos, idx in enumerate(ids):
        w = (layouts.get(idx) or {}).get("w")
        if w is None and pos == 0:
            w = first_width
        if w is not None:
            explicit = True
        values.append(int(w) if w else _DEFAULT_IMG_FRAC)
    if len(ids) == 1:
        return values[0] if explicit else None
    if unit.get("row"):
        return min(70, sum(values) + 2)
    return min(70, max(values))


def _fig_item(idx: int, marks: list[tuple[str, str]], layouts: dict[int, dict],
              width=None, align=None) -> tuple[str, str, float, str]:
    """第 idx 张图 → _figs_rows_latex 的一项 (文件名, 说明, 占宽, 对齐)。"""
    name, cap = marks[idx]
    w, a = _layout_at(layouts, idx, width, align)
    return (name, cap or "", (int(w) if w else _DEFAULT_IMG_FRAC) / 100,
            a or "center")


def _fig_stack_latex(items: list[tuple[str, str, float, str]],
                     hcap: float = None) -> str:
    """连续上下图 → 一个居中的零结构间距图片组。

    只消除排版系统生成的行距；原图像素里自带的白边属于图片内容，不能自动裁掉，
    否则坐标轴、箭头等靠边信息可能被误删。
    """
    if not items:
        return ""
    body = "\\par\\nobreak\\vspace{0.3em}\\noindent\\hfill"
    body += "\\begin{minipage}{\\linewidth}\\centering"
    for pos, (name, _cap, frac, _align) in enumerate(items):
        if pos:
            body += "\\par\\nointerlineskip"
        body += (f"\\includegraphics[width={frac}\\linewidth,"
                 f"height=\\qfigmaxh,keepaspectratio]{{{name}}}")
    body += "\\end{minipage}\\hfill\\par\\vspace{0.3em}"
    if hcap is not None:
        # 组内总高预算按张数摊分：hcap 是"整组的图片高度预算"，旧实现把它当
        # 每张的限高，N 张竖排 = N×hcap 直接撑破半页块（2026-10-04 审查）。
        per = hcap / max(1, len(items))
        body = f"{{\\setlength{{\\qfigmaxh}}{{{per:.4f}\\textheight}}{body}}}"
    return _raw(body)


def _fig_unit_latex(unit: dict, marks: list[tuple[str, str]],
                    layouts: dict[int, dict], first_id: int,
                    width=None, align=None, hcap: float = None) -> str:
    items = [_fig_item(i, marks, layouts,
                       width if i == first_id else None,
                       align if i == first_id else None)
             for i in unit["ids"]]
    if len(items) > 1 and not unit.get("row"):
        return _fig_stack_latex(items, hcap)
    return _figs_rows_latex([items], hcap)


def _figs_latex_planned(ids: list[int], marks: list[tuple[str, str]],
                        layouts: dict[int, dict], plan: dict = None,
                        width=None, align=None, hcap: float = None) -> str:
    """一批图（按序号）→ raw LaTeX，行的划分照 plan_figs 的分组。

    width/align 是首图的兜底值（旧的 img_width/img_align 两列），只对 ids[0] 用。
    没有 plan 时退回 _figs_latex 的按宽度贪心配对（老行为）。
    """
    if not ids:
        return ""
    items0 = [_fig_item(i, marks, layouts,
                        width if i == ids[0] else None,
                        align if i == ids[0] else None) for i in ids]
    if not plan:
        return _figs_latex(items0)
    return "".join(_fig_unit_latex(unit, marks, layouts, ids[0], width, align,
                                   hcap=hcap)
                   for unit in _plan_units(ids, plan))


# 未自定义时单图占正文宽的默认比例（与前端 image-layout.js 的 DEFAULT_IMG_W 一致）
_DEFAULT_IMG_FRAC = 35


def _fill_slots(body: str, ids: list[int], marks: list[tuple[str, str]],
                layouts: dict[int, dict], plan: dict,
                width=None, align=None, hcap: float = None) -> str:
    """把正文里指定序号的 QFIGSLOT 哨兵**原位**换成图片 raw-latex 块。

    这是「图文混排（图留在文字中间）」的落地点：哨兵在哪，图就排在哪，图前后的
    文字各自成段，原位不再留空白。
    一个并排组的两张图由组内**第一个**哨兵一次性排成一行，组内其余哨兵就地清掉
    （两图并排必须在同一个 \\noindent 行里，分两处发射就成上下两行了）。
    未列入 ids 的哨兵原样留着，交由调用方后续处理（如尾图）。
    hcap 见 _figs_rows_latex：半页块用它压低原位图的限高。
    """
    if not ids:
        return body
    units = _plan_units(ids, plan)
    # 每个视觉单元由首图哨兵发射；组内其余哨兵删掉
    head_of = {unit["ids"][0]: unit for unit in units}
    drop = {i for unit in units for i in unit["ids"][1:]}
    want = set(ids)

    def _sub(m):
        idx = int(m.group(1))
        if idx not in want:
            return m.group(0)
        if idx in drop:
            return ""
        unit = head_of.get(idx)
        if not unit:
            return ""
        return _fig_unit_latex(unit, marks, layouts, idx, width, align, hcap)

    out = _SLOT_RE.sub(_sub, body)
    # 哨兵独占一段时删掉它会留下连续空行 → pandoc 多出一个空段落
    return re.sub(r"\n{3,}", "\n\n", out)


# 四图配选项：每格（选项+图）在网格里占的 \linewidth 比例。留 gutter 防 Overfull。
_PAIR_CELL_FRAC = {2: 0.47, 4: 0.23}


def _pair_grid_latex(opts: list[str], marks: list[tuple[str, str]],
                     pair_map: list[int], cols: int) -> str:
    """四图配选项 → raw LaTeX 网格（一格一「选项标签+文字+图」）。

    **不走 tasks 环境**：`\\task` 里放不了 \\includegraphics，这是这套网格必须
    另写的唯一原因。改用一排 \\qpairitem（minipage）+ \\hfill 顶开，每 cols 个
    换一行，行末补空盒占位，保证最后一行不被 \\hfill 拉散。
    选项文字里的公式/加粗**不经 pandoc**（整块是 raw latex），故 $...$ 原样交给
    xelatex —— 与 _choice_tasks 把 tasks 环境塞进 raw 块是同一个取舍。
    """
    frac = _PAIR_CELL_FRAC.get(cols, 0.47)
    cells = []
    for oi, text in enumerate(opts):
        idx = pair_map[oi] if oi < len(pair_map) else None
        name = marks[idx][0] if idx is not None and idx < len(marks) else ""
        cap = marks[idx][1] if idx is not None and idx < len(marks) else ""
        cells.append(f"\\qpairitem{{{frac}}}{{{text.strip()}}}"
                     f"{{{name}}}{{{cap or ''}}}")
    out = ["\\par\\nobreak\\vspace{0.3em}"]
    for start in range(0, len(cells), cols):
        row = cells[start:start + cols]
        pad = cols - len(row)
        out.append("\\noindent" + "\\hfill".join(row)
                   + ("\\hfill" + "\\hfill".join(
                       [f"\\hspace{{{frac}\\linewidth}}"] * pad) if pad else "")
                   + "\\par\\vspace{0.4em}")
    return _raw("".join(out))


def _split_fracs(width) -> tuple[float, float]:
    """图文左右分栏两列的 \\linewidth 占比 → (文列, 图列)。

    未设 width 时对半 0.48/0.48（老默认）；设了则图列按 width/100、文列取剩余
    （0.96 − 图列，留 4% 给 \\hfill 间隙）。图列夹在 [0.1, 0.7]：上限 0.7 保证文列
    至少留 0.26 可读，下限 0.1 对齐前端手柄最小占比——此前下限是 0.25，导致拖到
    10~25 全都渲染成 25%（"图片再怎么拖都缩不小"），把想缩小图的用户卡死。
    选择题/填空题/解答题三种分栏共用，缩放手柄拖出的 width 由此统一生效。
    """
    if width:
        img = min(0.7, max(0.1, int(width) / 100))
        return round(0.96 - img, 4), img
    return 0.48, 0.48


def _place_image(numbered_md: str, marks: list[tuple[str, str]],
                  qtype: str = None, align: str = None, width=None,
                  split=False, layouts: dict[int, dict] = None,
                  ids: list[int] = None, plan: dict = None,
                  boxed: bool = False, hcap: float = None) -> str:
    """把**尾图**按题卡设置拼到题干上，决定优先级：

      1. split 且题型为填空/解答题 → 整题左右对半两栏（文字左、图右）。
         选择题的分栏不走这里，见 _place_choice_split（题干不分栏，只分选项+图）。
      2. 设了 align 或 width（任一）、或有多张图 → \\qfigflexbox 自定义宽度，
         Python 侧拼 \\hfill 控制左/中/右（未设的一项各自兜底：align 默认右、
         width 默认 35%）。多图时按 plan_figs 的分组并排（见 _figs_latex_planned）。
      3. 未设任何自定义、单图、题型为填空题 → 旧版 \\qfigwrap（左文右图，段首绕排）。
      4. 其余（未设任何自定义、单图、非填空题）→ 旧版 \\qfig（题后靠右下）。
    无尾图时原样返回。

    ids 是**尾图在全题图片里的原始序号**（plan_figs 判为非原位的那些）；marks 和
    layouts 都按原始序号索引，故 ids 不能预先压缩成 0..n —— 压了宽度/对齐就会
    错位到别的图上。原位图已由 _fill_slots 排进正文，不再经过这里。
    首图（ids[0]）参与上述全部分支（含分栏），其余图一律拼到题末，
    但**各自的宽度/对齐照 img_layouts 生效**（旧版把它们写死成 \\qfig 固定尺寸，
    这是「只有第一张图能拖动缩放」的根源）。
    """
    layouts = layouts or {}
    if ids is None:
        ids = list(range(len(marks)))
    if not ids:
        return numbered_md
    first = ids[0]
    name, cap = marks[first]
    cap = cap or ""
    # 首图的宽度/对齐：img_layouts 里有就用它，退回旧的 img_width/img_align 两列
    width, align = _layout_at(layouts, first, width, align)
    tail = _figs_latex_planned(ids[1:], marks, layouts, plan, hcap=hcap)
    multi = len(ids) > 1

    if split and qtype in (_BLANK | _SOLVE):
        return _place_text_figure_split(
            numbered_md, ids, marks, layouts, plan or {}, width, hcap=hcap)

    # 多图时也走这条：所有尾图交给 _figs_latex_planned 统一排（同组的并排成一行）。
    # 旧的 \qfigwrap（绕排）与 \qfig（固定小图靠右下）都只容得下一张图。
    # boxed（测量盒/minipage/samepage 上下文：half/slot/slides/practice 栏）：
    # wrapfig 在这些环境里被强制浮动、图文分离（2026-10-04 审查三处实测），
    # 降级为题末自定义图（flexbox，无浮动体，语义等价）。
    if align or width or multi or boxed:
        return numbered_md + _figs_latex_planned(ids, marks, layouts, plan,
                                                 width, align, hcap=hcap)

    if qtype in _BLANK:
        fig_latex = f"\\qfigwrap{{{name}}}{{{cap}}}"
        return _raw(fig_latex).lstrip("\n") + "\n\n" + numbered_md + tail

    fig_latex = f"\\qfig{{{name}}}{{{cap}}}"
    return numbered_md + _raw(fig_latex) + tail


def _place_text_figure_split(markdown: str, ids: list[int],
                             marks: list[tuple[str, str]],
                             layouts: dict[int, dict], plan: dict,
                             width=None, hcap: float = None) -> str:
    """把一段 Markdown 与首个题末图片视觉组排成左文右图，剩余图片续排。

    hcap：所在盒子（半页/槽位）的图片高度预算——右栏限高不得超出它，
    否则半页槽里单靠右图就能把题干挤出版心（2026-10-04 审查）。"""
    unit, rest_ids = _split_first_unit(ids, plan)
    unit_width = _split_unit_width(unit, layouts, width)
    txt_frac, img_frac = _split_fracs(unit_width)
    img_side = _choice_unit_side(unit, marks, layouts, img_frac, hcap=hcap)
    open_block = (f"\n\n```{{=latex}}\n\\noindent\\begin{{minipage}}[t]"
                  f"{{{txt_frac}\\linewidth}}"
                  "\\setlength{\\parskip}{0pt}\\vspace{0pt}\n```\n")
    close_block = (f"\n\n```{{=latex}}\n\\end{{minipage}}"
                   f"\\hfill{img_side}\n```\n")
    rest = _figs_latex_planned(rest_ids, marks, layouts, plan)
    return open_block + markdown + close_block + rest


def _place_text_figure_wrap(markdown: str, ids: list[int],
                            marks: list[tuple[str, str]],
                            layouts: dict[int, dict], plan: dict,
                            width=None, before: str = "") -> str:
    """从图片引用位置开始放置 wrapfigure，图下恢复整行文字。

    存储层仍用 ``sol_img_split='full'`` 兼容旧题；只替换显示/导出层的
    排版实现。图片组占比继续由用户设置的 ``w`` 决定；对齐为 left
    时浮在左侧，right 浮在右侧，历史的 center/缺省值沿用原右图语义。
    ``before`` 是图片引用之前的整宽内容；``markdown`` 是引用之后才参与环绕的
    内容。两者不能再颠倒，否则图片会固定漂到整段解析右上角。
    本题末尾用 ``\\qwrapclear`` 收口，防止下一道题继续绕上一题的图。
    """
    unit, rest_ids = _split_first_unit(ids, plan)
    unit_width = _split_unit_width(unit, layouts, width)
    _txt_frac, img_frac = _split_fracs(unit_width)
    first = (unit.get("ids") or [ids[0]])[0]
    _first_width, align = _layout_at(layouts, first, width, None)
    side = "l" if align == "left" else "r"
    hcap = _split_img_hcap(img_frac)
    figure = _choice_unit_side(unit, marks, layouts, frac=1.0, hcap=hcap)
    wrap = (f"\\begin{{wrapfigure}}{{{side}}}{{{img_frac:.4f}\\linewidth}}"
            "\\vspace{-\\baselineskip}"
            f"{figure}\\end{{wrapfigure}}")
    clear = _raw("\\qwrapclear").strip("\n")
    rest = _figs_latex_planned(rest_ids, marks, layouts, plan)
    parts = []
    if before.strip():
        parts.append(before.strip("\n"))
    parts.append(_raw(wrap).strip("\n"))
    if markdown.strip():
        parts.append(markdown.strip("\n"))
    parts.append(clear)
    return "\n\n".join(parts) + rest


def _split_at_figure_slot(text: str, unit: dict) -> tuple[str, str] | None:
    """按视觉图片组的首个哨兵切开正文，并清掉同组其余哨兵。

    返回值左侧保持整宽，右侧从图片引用所在行开始参与环绕。只认 plan_figs 已经
    生成的数字哨兵，不做字符串猜测；找不到锚点时返回 None 让调用方安全回退。
    """
    ids = [int(item) for item in (unit.get("ids") or [])]
    if not ids:
        return None
    first = ids[0]
    anchor = next(
        (match for match in _SLOT_RE.finditer(text)
         if int(match.group(1)) == first),
        None,
    )
    if anchor is None:
        return None
    before = text[:anchor.start()].rstrip()
    after = text[anchor.end():]
    extra = set(ids[1:])
    if extra:
        after = _SLOT_RE.sub(
            lambda match: "" if int(match.group(1)) in extra else match.group(0),
            after,
        )
    return before, after.lstrip()


def _split_img_hcap(frac: float) -> float:
    """分栏图片列的高度上限（\\textheight 倍数），随列宽 frac 放大。

    此前固定 0.32\\textheight：列宽拖大后高度先触顶，横/方图无法随宽度继续放大
    （拖动“看不出效果”）。改为跟列宽成比例——让“宽度”成为主导缩放维度，横图/
    方图大小由列宽决定，只有极端竖图才触及此上限做溢出保护。系数 0.72 使方图在
    列宽内由宽度绑定；封顶 0.6\\textheight 防竖图撑破版面。

    高度上限全景见 exam_template.tex 顶部「图片高度上限全景」块；本函数的两项
    参数（系数、封顶）是分栏场景专属的 Python 侧决策，模板没有对应宏。
    """
    return min(0.6, round(frac * 0.72, 3))


def _choice_unit_side(unit: dict, marks: list[tuple[str, str]],
                      layouts: dict[int, dict], frac: float = 0.48,
                      hcap: float = None) -> str:
    """图文分栏右栏：完整渲染一个横排或纵排视觉图片组。"""
    ids = unit.get("ids") or []
    items = [_fig_item(i, marks, layouts) for i in ids]
    h = hcap if hcap is not None else _split_img_hcap(frac)
    prefix = (f"\\begin{{minipage}}[t]{{{frac}\\linewidth}}"
              "\\setlength{\\parskip}{0pt}\\vspace{0pt}\\centering")
    if len(items) > 1 and unit.get("row"):
        total = sum(item[2] for item in items) or 1
        inner = []
        for name, cap, item_frac, _align in items:
            # 两个内层 minipage + 间距若刚好等于 1.0\linewidth，TeX 的舍入误差会
            # 把第二张挤到下一行。只用 94% 分给图片、2% 作间距，余下 4% 当安全边。
            rel = max(0.05, item_frac / total * 0.94)
            inner.append(
                f"\\begin{{minipage}}[t]{{{rel:.4f}\\linewidth}}\\centering"
                f"\\includegraphics[width=\\linewidth,height={h}\\textheight,"
                f"keepaspectratio]{{{name}}}"
                + (f"\\par\\vspace{{0.2em}}{{\\footnotesize {cap}}}" if cap else "")
                + "\\end{minipage}")
        body = "\\hspace{0.02\\linewidth}".join(inner)
    elif len(items) > 1:
        widest = max(item[2] for item in items) or 1
        chunks = []
        for pos, (name, cap, item_frac, _align) in enumerate(items):
            rel = min(1.0, item_frac / widest)
            chunk = (f"\\includegraphics[width={rel:.4f}\\linewidth,"
                     f"height={h}\\textheight,keepaspectratio]{{{name}}}")
            if cap:
                chunk += f"\\par\\vspace{{0.2em}}{{\\footnotesize {cap}}}"
            if pos:
                chunk = "\\par\\nointerlineskip" + chunk
            chunks.append(chunk)
        body = "".join(chunks)
    else:
        name, cap, _item_frac, _align = items[0]
        body = (f"\\includegraphics[width=\\linewidth,height={h}\\textheight,"
                f"keepaspectratio]{{{name}}}"
                + (f"\\par\\vspace{{0.2em}}{{\\footnotesize {cap}}}" if cap else ""))
    return prefix + body + "\\end{minipage}"


def _place_practice_wrap(num: int | None, core: str, unit: dict,
                         marks: list[tuple[str, str]],
                         layouts: dict[int, dict], tail: str,
                         width=None) -> str:
    """双栏刷题中的图文分栏改为右浮图，正文在图下恢复完整栏宽。

    外层 ``multicols*`` 已把版心切成两栏，再套常规左右 minipage 会把文字压成约
    四分之一页宽。wrapfigure 只在图片实际高度范围内收窄正文；图片结束后的文字会
    自动占满当前外栏，因此既保留图文并置，也能利用图片下方空间。

    用户设置的图片宽度仍生效，但在双栏里封顶 46%：wrapfig 还会额外加入
    ``\\columnsep`` 作为图文间距，若照搬普通模式 70% 的上限，图旁只剩一两个汉字。
    """
    _txt_frac, img_frac = _split_fracs(width)
    img_frac = min(0.46, img_frac)
    figure = _choice_unit_side(unit, marks, layouts, frac=1.0,
                               hcap=_PRACTICE_IMG_HCAP)
    # wrapfig 与 list 环境不兼容：若沿用 _num_wrap 的 qopen 列表，宏包会发出
    # "Stationary wrapfigure forced to float" 并把图片漂到后页。题号因此改成紧跟浮图
    # 的 Markdown 加粗前缀，与题干由 pandoc 写成**同一个段落**；若把题号也塞进前面
    # 的 raw-LaTeX 块，pandoc 会在块结束后另开题干段，表现就是题号顶格、题干换行。
    number = f"**{num}.** " if num is not None else ""
    wrap = ("\\qwrapneed{0.24\\textheight}"
            f"\\begin{{wrapfigure}}{{r}}{{{img_frac:.4f}\\linewidth}}"
            "\\vspace{-\\baselineskip}"
            f"{figure}\\end{{wrapfigure}}")
    # 当前题结束前消耗完 wrapfig 的剩余环绕行，防止下一题标题继续绕上一题图片；
    # 长正文已经自然填满时 qwrapclear 不增加可见高度。宏定义见 exam_template.tex。
    clear = _raw("\\qwrapclear").strip("\n")
    return (_raw(wrap).lstrip("\n") + "\n\n" + number + core
            + "\n\n" + clear + tail)


def _place_choice_split(num: int, stem: str, tasks_env: str, unit: dict,
                        marks: list[tuple[str, str]], layouts: dict[int, dict],
                        tail: str, full: bool = False, width=None,
                        opt_tail: str = "", hcap: float = None) -> str:
    """选择题图文分栏。两种模式（题号仍经 _num_wrap 独占一列，见下方包裹）：

    - full=False（opts，默认）：题干整题宽渲染（走 pandoc 正常段落），另起一段后
      把选项 tasks 环境（左栏）与图片（右栏）对半两栏。tasks_env/图片都是现成
      raw LaTeX，故整段一次性作为 raw-latex 块拼接即可。
    - full=True：题干 + 选项一并进左栏、图片进右栏（整题左右对分）。题干含数学式/
      加粗，须由 pandoc 渲染，不能塞进 raw-latex 块，故借用填空题分栏同款“交错”手法：
      先用 raw 块开左栏 minipage → 题干 markdown 交给 pandoc 在栏内渲染 → 再用 raw
      块补上选项 tasks 环境、收左栏、拼右图栏。

    两栏都显式清零 \\parskip 并以 \\vspace{0pt} 起首，消除“图片本体没有 \\par
    触发段前间距、而左栏有”导致的顶部错位；图片限高 0.32\\textheight，避免选项
    内容少时图片仍把两栏撑得很不均衡。

    opt_tail 是选项区之后的附注（「参考公式：…」，见 split_choice_options）。它排在
    **两栏之下、整行宽**：这段文字通常较长，塞进左栏会把两栏高度拉得很不均衡；
    且它含 markdown/数学式，必须走 pandoc，不能进 raw-latex 块。
    """
    txt_frac, img_frac = _split_fracs(width)
    # 两栏之后、其余图片之前：附注是对选项的补充，应紧跟选项区
    tail = (f"\n\n{opt_tail}\n" if opt_tail else "") + tail
    right = _choice_unit_side(unit, marks, layouts, img_frac, hcap=hcap)

    if full:
        open_block = (f"\n\n```{{=latex}}\n\\noindent\\begin{{minipage}}[t]{{{txt_frac}\\linewidth}}"
                      "\\setlength{\\parskip}{0pt}\\vspace{0pt}\n```\n")
        # 收左栏前先补上选项 tasks（题干与选项间留一点段距），再收栏、拼右图栏
        close_block = (f"\n\n```{{=latex}}\n\\par\\vspace{{0.3em}}\n{tasks_env}"
                       f"\\end{{minipage}}\\hfill{right}\n```\n")
        core = open_block + stem + close_block + tail
        return _num_wrap(num, core)

    left = (f"\\begin{{minipage}}[t]{{{txt_frac}\\linewidth}}"
            f"\\setlength{{\\parskip}}{{0pt}}\\vspace{{0pt}}\n{tasks_env}"
            "\\end{minipage}")
    row = f"\\par\\vspace{{0.3em}}\\noindent{left}\\hfill{right}"
    core = stem + _raw(row) + tail
    return _num_wrap(num, core)


def _subs_start(body: str) -> int | None:
    """正文里「首个行首小问序号」的下标，没有小问返回 None。

    与 _split_stem_subs 同一条判定（同一个正则、同一次 _break_subquestions），
    只是返回下标而非切好的两段 —— plan_figs 要判断「这张图是不是正好夹在题干与
    小问之间」，只有下标能回答。切分规则因此仍只有一份：_split_stem_subs 判「切
    不切得出」，本函数给「切在哪」。

    下标必须直接在原正文上计算：_break_subquestions 会在首个小问前补空行，恰好会
    把分界下标向后推一位；plan_figs 随后却拿这个下标切原正文，于是会多切进小问的
    首字符，把「题干—图片—小问」误判成图片后仍有题干。行首正则本身已允许缩进，
    定位阶段无需先规范化；真正渲染小问时仍由 _split_stem_subs 做规范化。
    """
    lines = body.splitlines()
    pos = 0
    for line in lines:
        if _SUBQ_LINE_RE.match(line):
            return pos if pos else None      # 小问在最前面 = 没有题干，不算边界
        pos += len(line) + 1
    return None


def _split_stem_subs(body: str) -> tuple[str, str] | None:
    """把解答题正文按“首个行首小问序号（（1）（2）…）”切成 (题干, 小问部分)。

    题干 = 第一个小问之前的内容；小问部分 = 从第一个小问序号起到结尾。
    没有分小问（整题就一段）时返回 None，交由调用方回退到普通排版。
    先经 _break_subquestions 保证每个小问独占一行，再定位首个小问行。
    """
    normalized = _break_subquestions(body)
    lines = normalized.splitlines()
    for i, line in enumerate(lines):
        if _SUBQ_LINE_RE.match(line):
            stem = "\n".join(lines[:i]).strip()
            subs = "\n".join(lines[i:]).strip()
            if stem and subs:
                return stem, subs
            return None
    return None


def _place_solve_split(num: int, stem: str, subs: str, unit: dict,
                       marks: list[tuple[str, str]], layouts: dict[int, dict],
                       tail: str, width=None, hcap: float = None) -> str:
    """解答题“仅小问分栏”：题干整行宽渲染，之后把小问（左栏）与图片（右栏）左右
    分栏（题号仍经 _num_wrap 独占一列，见下方包裹）。题干走 pandoc 正常段落，
    小问含数学式/换行同样交给 pandoc（借用选择题 full 同款“交错”手法：raw 开左栏
    → 小问 markdown → raw 补收左栏 + 拼右图栏）。width 经 _split_fracs 决定两列
    占比；小问经 _render_subquestions 渲染出多级缩进（（1）（i）（ii）嵌套）。"""
    txt_frac, img_frac = _split_fracs(width)
    right = _choice_unit_side(unit, marks, layouts, img_frac, hcap=hcap)
    open_block = (f"\n\n```{{=latex}}\n\\par\\vspace{{0.3em}}\\noindent"
                  f"\\begin{{minipage}}[t]{{{txt_frac}\\linewidth}}"
                  "\\setlength{\\parskip}{0pt}\\vspace{0pt}\n```\n")
    close_block = f"\n\n```{{=latex}}\n\\end{{minipage}}\\hfill{right}\n```\n"
    # 小问文本经 _format_options 拆挤行选项（与非分栏路径一致），再渲染多级缩进
    subs_md = _render_subquestions(_format_options(subs))
    core = stem + open_block + subs_md + close_block + tail
    return _num_wrap(num, core)


def _raw(latex: str) -> str:
    """把一段 raw LaTeX 包成 pandoc 能透传的 fenced 块。"""
    return f"\n\n```{{=latex}}\n{latex}\n```\n"


def _slide_left_content(markdown: str) -> str:
    """把单道横版题目限制在页面左侧 70%，右侧保留课件留白。"""
    opening = _raw(
        "\\noindent\\begin{minipage}[t]{0.7\\linewidth}"
        "\\setlength{\\parskip}{0.7em}\\vspace{0pt}"
    ).strip("\n")
    closing = _raw("\\end{minipage}\\par").strip("\n")
    return opening + "\n\n" + markdown + "\n\n" + closing


# ---------------------------------------------------------------------------
# 分页计算：paginate() —— 导出与预览共用的唯一分页逻辑
# ---------------------------------------------------------------------------
#
# 输出：list[page]，page = list[block]。
# block 是 dict：
#   {"kind": "heading", "text": "一、选择题"}
#   {"kind": "keypoints", "text": "<用户文本>"}
#   {"kind": "question", "num": 3, "body": "...", "layout": "flow|half|full"}
# layout：flow=紧凑随文，half=固定半页，full=独占整页。
# 分页 = 每个子列表就是一页；渲染/预览据此加 \clearpage 或画 A4 卡片。


def _new_page(pages):
    pages.append([])
    return pages[-1]


def _img_fields(q: dict) -> dict:
    """从题目 dict 里取图片排版设置，塞进 block（供 _render_block/_place_image 用）。

    img_layouts 是多图逐图设置（JSON 列，见 _parse_layouts）；img_align/img_width
    保留作 i=0 的兜底，老题（无 img_layouts）导出效果与改动前完全一致。
    _IMG_FILES_KEY / _SOL_IMG_FILES_KEY 是 _stage_images 拷图后落下的本地文件名
    列表（下标 = QFIGSLOT 哨兵编号），必须一路带到 _render_block —— 旧版把文件名
    嵌在正文末尾的 \\qfigmark 块里，改成原位哨兵后正文里只剩编号。
    """
    return {"img_align": q.get("img_align"), "img_width": q.get("img_width"),
            "img_split": q.get("img_split"), "img_layouts": q.get("img_layouts"),
            "sol_img_split": q.get("sol_img_split"),
            "sol_img_layouts": q.get("sol_img_layouts"),
            _IMG_FILES_KEY: q.get(_IMG_FILES_KEY) or [],
            _SOL_IMG_FILES_KEY: q.get(_SOL_IMG_FILES_KEY) or []}


def paginate(questions: list[dict], mode: str = "list", keypoints: str = "",
             fullpage_ids=None, solution_mode: str = "none",
             std_opts: dict = None, bank_subject: str = "math",
             *, layout=None, grouped=None, columns=None, ratio=None,
             std_exam=False) -> list[list[dict]]:
    """把题目按模式分页，返回 list[page]（page=block 列表）。纯函数，无副作用。

    solution_mode='separate' 时，在题目页之后追加「解析」独占页（题号对应）。
    inline/none 由 _render_block 逐题处理，不影响分页。
    std_opts: 标准试卷（exam_std）的分值说明等选项。
    layout/grouped/columns/ratio/std_exam: 导出抽屉的四维度自定义组合；全不传
    （layout=None）时走原 mode 逻辑，行为与改动前逐字节一致。互斥/折算规则见
    resolve_export_layout。
    """
    spec = resolve_export_layout(layout=layout, grouped=grouped, columns=columns,
                                 ratio=ratio, std_exam=std_exam)
    _validate_export_options(mode=mode, solution_mode=solution_mode)
    # 与 Word 侧一致做 str 归一：Agent/插件可能按文档传数字 id，裸比较会让
    # 「独占整页」静默失效（2026-10-04 审查）。
    fullpage_ids = {str(v) for v in (fullpage_ids or [])}

    if spec is not None:
        pages = _paginate_custom(questions, spec, keypoints=keypoints,
                                 fullpage_ids=fullpage_ids, std_opts=std_opts,
                                 bank_subject=bank_subject)
    elif mode == "exam_std":
        pages = _paginate_exam_std(questions, std_opts or {}, bank_subject)
    elif mode == "exam":
        pages = _paginate_exam(questions, bank_subject)
    elif mode == "slides":
        pages = _paginate_slides(questions)
    elif mode == "practice":
        pages = _paginate_practice(questions, bank_subject)
    elif mode == "handout":
        pages = _paginate_handout(questions, keypoints, fullpage_ids)
    elif mode == "note":
        pages = _paginate_two(questions, fullpage_ids, start_num=1)
    elif mode == "lecture":
        pages = _paginate_lecture(questions)
    else:
        pages = _paginate_list(questions, bank_subject)

    if solution_mode == "separate":
        # 解析一页一条只属于 16:9 课件语义（含自定义的 one+wide 组合）。
        one_pp = spec.one_solution_per_page if spec is not None else (mode == "slides")
        pages = pages + _solution_pages(pages, one_per_page=one_pp)
    return pages


def _solution_pages(pages: list[list[dict]], one_per_page: bool = False) -> list[list[dict]]:
    """按已分页的题目顺序收集解析。

    纸张模式沿用「一页装若干条」；横版课件必须一题一页，否则几道解析会被挤进
    同一张 16:9 页面，既不适合投屏逐步讲解，也很容易超出版心。
    """
    items = []
    for page in pages:
        for b in page:
            if b.get("kind") == "question" and b.get("solution"):
                # 解析里的图片文件名随条目走，否则 solution_item 无从把哨兵换回图
                items.append((b["num"], b["solution"],
                              b.get(_SOL_IMG_FILES_KEY) or [],
                              b.get("sol_img_layouts"), b.get("sol_img_split")))
    if not items:
        return []
    if one_per_page:
        return [[{"kind": "solution_slide", "num": n, "text": s,
                  _SOL_IMG_FILES_KEY: f, "sol_img_layouts": l,
                  "sol_img_split": split}]
                for n, s, f, l, split in items]
    blocks = [{"kind": "solution_head"}]
    blocks += [{"kind": "solution_item", "num": n, "text": s,
                _SOL_IMG_FILES_KEY: f, "sol_img_layouts": l,
                "sol_img_split": split}
               for n, s, f, l, split in items]
    return [blocks]


def _paginate_slides(questions):
    """16:9 横版课件：严格保持题库顺序，每道题独占一页。"""
    return [[{"kind": "question", "num": i, "body": q["body"],
              "layout": "slide", "solution": q.get("solution"),
              "type": q.get("type"), **_img_fields(q)}]
            for i, q in enumerate(questions, 1)]


def _display_type_name(name: str, bank_subject: str) -> str:
    """只替换导出标题；题目类型字段仍保持“填空题”供既有排版逻辑判断。"""
    if bank_subject == "physics" and name == "填空题":
        return "实验题"
    return name


def _paginate_practice(questions, bank_subject="math"):
    """双栏刷题：按简单试卷的四类大题分桶，桶内连续流排。

    单选、多选、填空、解答题各自成段，空桶跳过且不占大题序号；同类题保持选入
    顺序，题号按分桶后的顺序连续。双栏里公式、图片和选项列数会改变真实高度，
    Python 不预估每栏题数，只把分区后的块交给 TeX 自动换栏换页。
    """
    single = [q for q in questions if q.get("type") in _SINGLE]
    multi = [q for q in questions if q.get("type") in _MULTI]
    blank = [q for q in questions if q.get("type") in _BLANK]
    solve = [q for q in questions
             if q.get("type") not in _SINGLE | _MULTI | _BLANK]

    page = []
    num = 1
    sec = 0
    for bucket, name in [(single, "单选题"), (multi, "多选题"),
                         (blank, "填空题"), (solve, "解答题")]:
        if not bucket:
            continue
        heading = f"{_CN_NUM[sec]}、{_display_type_name(name, bank_subject)}"
        heading_block = {"kind": "heading", "text": heading}
        if bucket is solve:
            # 仍保留分页结构中的 heading，方便页面摘要/既有调用识别大题层级；渲染
            # 时由 suppress_render 跳过，真正可见的标题随首道大题进入实测盒，避免孤行。
            heading_block["suppress_render"] = True
        page.append(heading_block)
        sec += 1
        for bucket_index, q in enumerate(bucket):
            page.append({
                "kind": "question", "num": num, "body": q["body"],
                "layout": "practice", "solution": q.get("solution"),
                "type": q.get("type") if bucket is solve else name,
                "difficulty": q.get("difficulty"),
                "practice_solve": bucket is solve,
                "practice_solve_index": bucket_index if bucket is solve else None,
                "heading": heading if bucket is solve and bucket_index == 0 else "",
                **_img_fields(q),
            })
            num += 1
    return [page] if page else []


def _paginate_two(questions, fullpage_ids, start_num=1):
    """讲义/笔记模式：每题目标半页（一页两题，所有题型统一）。

    题号按传入顺序连续编号，**不按题型分桶重排**——这两个模式是「按自己的顺序过
    题」的场景，重排会打乱用户在题库里的排序（试卷模式才分大题）。故同一页上可能
    既有 1/4 槽又有半页槽，混排由 TeX 的槽位序列自然处理。

    每题都超出目标槽位时自动升级（1/4 → 半页 → 整页），下一题另起一页——用户要求
    的「超出半页则该题独占一页」由 \\qslotclose 实现，不在这里预判。
    fullpage_ids 指定的题仍强制独占整页（题卡上的手动开关，优先于自适应）。
    """
    pages = []
    page = _new_page(pages)
    for i, q in enumerate(questions):
        num = start_num + i
        if q.get("id") in fullpage_ids:
            # 手动指定整页：仍走旧的 full 布局 + 显式分页，用户点了就一定独占
            if any(page):
                page = _new_page(pages)
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "full", "solution": q.get("solution"),
                         "type": q.get("type"), **_img_fields(q)})
            page = _new_page(pages)
            continue
        qtype = q.get("type")
        # 一页两题（用户规格）：所有题型统一目标半页。曾按「选择/填空各 1/4
        # 页、解答半页」细分，与 UI 文案「笔记（一页两题）」不符（2026-10-04
        # 用户反馈「一页两题失效」）；超半页自动升级整页的兜底不变。
        layout = "slot_half"
        page.append({"kind": "question", "num": num, "body": q["body"],
                     "layout": layout, "solution": q.get("solution"),
                     "type": qtype, **_img_fields(q)})
    # 去掉末尾空页
    return [p for p in pages if p]


def _paginate_exam(questions, bank_subject="math"):
    """试卷模式：选填紧凑同页流排，解答题每题目标半页、自适应升级。全卷连续题号。

    - 大题序号「一二三」只对非空题型递增（缺某题型不会跳号或空标题）。
    - 题号全卷连续，从 1 递增，与题型无关。
    - **解答题不再强制另起一页**：填空题排完后若本页余量还够一个半页槽，就在本页
      接着放一道解答题（用户要求「空间足够则放进去」）。够不够由 TeX 量本页余量
      决定（\\qslotclose），Python 侧估算不准——所以这里不再插页边界，整段解答题
      交给同一页的槽位序列，让 TeX 自行断页。
    - 超过半页的解答题自动升级成整页并独占一页，下一题另起一页（升级逻辑同上，
      在 \\qslotclose 里）。
    """
    single = [q for q in questions if q.get("type") in _SINGLE]
    multi = [q for q in questions if q.get("type") in _MULTI]
    blank = [q for q in questions if q.get("type") in _BLANK]
    solve = [q for q in questions if q.get("type") not in _SINGLE | _MULTI | _BLANK]

    pages = []
    page = _new_page(pages)
    num = 1
    sec = 0  # 非空大题计数 → 一二三四

    def cn_heading():
        nonlocal sec
        text = _CN_NUM[sec]
        sec += 1
        return text

    # 单选、多选、填空：紧凑随文，同页流排；空桶跳过、不占大题号
    # 注意 type 仍传各自真实题型名（单选题/多选题都在 _CHOICE，走 tasks 选项渲染）
    for bucket, name in [(single, "单选题"), (multi, "多选题"), (blank, "填空题")]:
        if not bucket:
            continue
        page.append({"kind": "heading", "text":
                     f"{cn_heading()}、{_display_type_name(name, bank_subject)}"})
        for q in bucket:
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "flow", "solution": q.get("solution"),
                         "type": name, **_img_fields(q)})
            num += 1

    # 解答题：接着填空题往下排（不再 \clearpage），每题目标半页、由 TeX 自适应
    # 升级与断页。标题嵌入首题槽位内，随题绑定，不会掉队到上一页页底。
    if solve:
        solve_heading = f"{cn_heading()}、解答题"
        for idx, q in enumerate(solve):
            block = {"kind": "question", "num": num, "body": q["body"],
                     "layout": "slot_half", "solution": q.get("solution"),
                     "type": q.get("type"), **_img_fields(q)}
            if idx == 0:
                block["heading"] = solve_heading  # 标题绑定第一道解答题
            page.append(block)
            num += 1

    return [p for p in pages if p]


def _paginate_exam_std(questions, std_opts, bank_subject="math"):
    """标准试卷模式：在 exam 分区基础上叠加大题分值说明 + 解答题连续紧凑排。

    与 _paginate_exam 的差异：
      - 大题标题带 points（分值说明小字），来自 std_opts["section_points"]；
      - 解答题不再半页/2 题一页，改 layout="solve_compact"：随文连续排、
        每题后留固定作答空白，可跨页（更紧凑、更省纸）；
      - 卷首/保密说明由 build_markdown 拼在正文顶部，不在此分页。
    单选/多选/填空段与 exam 完全一致（layout="flow"，图片分栏照旧）。
    """
    sp = std_opts.get("section_points", {}) if std_opts else {}
    single = [q for q in questions if q.get("type") in _SINGLE]
    multi = [q for q in questions if q.get("type") in _MULTI]
    blank = [q for q in questions if q.get("type") in _BLANK]
    solve = [q for q in questions if q.get("type") not in _SINGLE | _MULTI | _BLANK]

    pages = []
    page = _new_page(pages)
    num = 1
    sec = 0

    def cn_heading():
        nonlocal sec
        text = _CN_NUM[sec]
        sec += 1
        return text

    for bucket, name, pkey in [(single, "单选题", "single"),
                               (multi, "多选题", "multi"),
                               (blank, "填空题", "blank")]:
        if not bucket:
            continue
        # 说明句按实际题数自动生成（题数 x、总分 = x * 每小题分），用户只填每小题分值
        page.append({"kind": "heading", "text":
                     f"{cn_heading()}、{_display_type_name(name, bank_subject)}",
                     "points": _std_section_desc(pkey, len(bucket),
                                                 sp.get(pkey, "")),
                     "colon": True})
        for q in bucket:
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "flow", "solution": q.get("solution"),
                         "type": name, **_img_fields(q)})
            num += 1

    if solve:
        page.append({"kind": "heading", "text": f"{cn_heading()}、解答题",
                     "points": _std_section_desc("solve", len(solve),
                                                 sp.get("solve", "")),
                     "colon": True})
        for q in solve:
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "solve_compact", "solution": q.get("solution"),
                         "type": q.get("type"), **_img_fields(q)})
            num += 1

    return [p for p in pages if p]


def _paginate_list(questions, bank_subject="math"):
    """清单模式：全部题目连续紧凑排（不分页），加单选/多选/填空/解答四类大题标题。

    保持原有的紧凑流排（layout="flow"、不插页边界，靠 TeX 自然断页），只多了大题
    标题。**代价是题目按题型分桶重排**：要出「一、单选题」这样的标题，同类题就必须
    连在一起，否则标题下会混进别的题型。题号随重排后的顺序连续编号（与试卷模式的
    分桶规则一致），因此清单里的题序不再等于题库里的排序。
    标题不带分值说明——清单是拿来速览/打印题面的，不是考卷。
    """
    single = [q for q in questions if q.get("type") in _SINGLE]
    multi = [q for q in questions if q.get("type") in _MULTI]
    blank = [q for q in questions if q.get("type") in _BLANK]
    solve = [q for q in questions if q.get("type") not in _SINGLE | _MULTI | _BLANK]

    page = []
    num = 1
    sec = 0
    for bucket, name in [(single, "单选题"), (multi, "多选题"),
                         (blank, "填空题"), (solve, "解答题")]:
        if not bucket:
            continue
        page.append({"kind": "heading", "text":
                     f"{_CN_NUM[sec]}、{_display_type_name(name, bank_subject)}"})
        sec += 1
        for q in bucket:
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "flow", "solution": q.get("solution"),
                         # 解答题桶里可能有其他题型，type 传原值不强制改名
                         "type": q.get("type") if bucket is solve else name,
                         **_img_fields(q)})
            num += 1
    return [page] if page else []


def _paginate_lecture(questions):
    """讲解模式：选择题与填空题一页两题（各半页），解答题一页一题（独占整页）。

    此前是所有题型一律独占整页——短选择题占一整张纸太浪费。题号按传入顺序连续
    编号，不分桶重排（同 _paginate_two 的理由）。
    解答题走显式 full + 分页，而不是自适应槽位：「一页一题」是确定的诉求，不需要
    量高度；选填题走半页槽位，超过半页时才自动升级。
    """
    pages = []
    page = _new_page(pages)
    for i, q in enumerate(questions, 1):
        qtype = q.get("type")
        if qtype in _CHOICE | _BLANK:
            page.append({"kind": "question", "num": i, "body": q["body"],
                         "layout": "slot_half", "solution": q.get("solution"),
                         "type": qtype, **_img_fields(q)})
            continue
        # 解答题独占整页：前面若已排了选填题，先收尾当前页
        if any(page):
            page = _new_page(pages)
        page.append({"kind": "question", "num": i, "body": q["body"],
                     "layout": "full", "solution": q.get("solution"),
                     "type": qtype, **_img_fields(q)})
        page = _new_page(pages)
    return [p for p in pages if p]


def _paginate_handout(questions, keypoints, fullpage_ids):
    """讲义模式：知识要点（填了才有）独占首页，之后一页两题。"""
    pages = []
    if keypoints.strip():
        pages.append([{"kind": "keypoints", "text": keypoints.strip()}])
    pages.extend(_paginate_two(questions, fullpage_ids, start_num=1))
    return pages


# ---------------------------------------------------------------------------
# 四维度自定义组合的分页（2026-10-05 导出抽屉）
# 只在 layout 非 None 时进入。命中等价表的组合直接复用上面的 legacy 分页器；
# 其余 8 个组合走下面 4 个薄函数。渲染层与模板零改动——这里只产出既有的
# layout 标记（flow / slot_half / full / practice / slide）与 heading 块。
# ---------------------------------------------------------------------------

_BUCKET_ORDER = (("single", "单选题"), ("multi", "多选题"),
                 ("blank", "填空题"), ("solve", "解答题"))


def _bucket_questions(questions):
    """按四类题型分桶（与 _paginate_list/_paginate_practice 同一归类），空桶剔除。"""
    buckets = {
        "single": [q for q in questions if q.get("type") in _SINGLE],
        "multi": [q for q in questions if q.get("type") in _MULTI],
        "blank": [q for q in questions if q.get("type") in _BLANK],
        "solve": [q for q in questions
                  if q.get("type") not in _SINGLE | _MULTI | _BLANK],
    }
    return [(pkey, name, buckets[pkey]) for pkey, name in _BUCKET_ORDER
            if buckets[pkey]]


def _bucket_heading(sec, pkey, name, count, bank_subject, std_points):
    """分桶大题标题块。std_points 非空时按标准试卷样式附分值说明（exam_std 语义）。"""
    block = {"kind": "heading",
             "text": f"{_CN_NUM[sec]}、{_display_type_name(name, bank_subject)}"}
    if std_points is not None:
        block["points"] = _std_section_desc(pkey, count, std_points.get(pkey, ""))
        block["colon"] = True
    return block


def _paginate_stream(questions, *, compact=False):
    """流式 / 紧凑 + 不分题型：严格按选入顺序，不重排、不加大题标题。

    compact=False（流式）：选填紧凑流排、解答题半页槽（放不下由 TeX 自动升级
    整页）——与试卷（exam）同一条「自动留空」规则，只是不按题型分桶；
    compact=True（紧凑）：全部 flow 连续排，零留白（相当于去掉分桶的清单）。
    """
    page = []
    for i, q in enumerate(questions, 1):
        qtype = q.get("type")
        solve = qtype not in _SINGLE | _MULTI | _BLANK
        layout = "flow" if (compact or not solve) else "slot_half"
        page.append({"kind": "question", "num": i, "body": q["body"],
                     "layout": layout, "solution": q.get("solution"),
                     "type": qtype, **_img_fields(q)})
    return [page] if page else []


def _paginate_adaptive(questions, bank_subject="math", *, grouped=True):
    """自适应版式（单栏）：流式排列，解答题不用半页槽——题后作答留白按难度与
    一级小问数自动给（与双栏刷题的解答题同一公式，_answer_space 的 practice
    参数，运行时可经 config.ANSWER_SPACE 覆写）。

    grouped=True 按题型分桶重排（试卷/exam 同构：题号按桶序全卷连续、桶标题
    独立成块），False 按选入顺序不重排。选填紧凑随文、无作答位；解答题留白量
    由题目属性（难度、小问数）决定，与半页槽（按整题实测高度升级、最少占半页）
    是两种取舍——自适应不为短题浪费半页，也不为长题自动升整页（长题自然跨页）。
    """
    page = []
    num = 0

    def add(q, qtype, solve):
        nonlocal num
        num += 1
        item = {"kind": "question", "num": num, "body": q["body"],
                "layout": "flow", "solution": q.get("solution"),
                "type": qtype, **_img_fields(q)}
        if solve:
            # difficulty 进 block：渲染层 _practice_answer_space 要读它算留白
            # （exam/practice 分页器同款处理；导出边界不能丢用户设的难度）。
            item["adaptive_solve"] = True
            item["difficulty"] = q.get("difficulty")
        page.append(item)

    if grouped:
        for sec, (pkey, name, bucket) in enumerate(_bucket_questions(questions)):
            page.append(_bucket_heading(sec, pkey, name, len(bucket),
                                        bank_subject, None))
            for q in bucket:
                add(q, q.get("type"), pkey == "solve")
    else:
        for q in questions:
            add(q, q.get("type"),
                q.get("type") not in _SINGLE | _MULTI | _BLANK)
    return [page] if page else []


def _paginate_practice_stream(questions, *, compact=False):
    """双栏 + 不分题型：按选入顺序，整题走 practice 盒（双栏里不设半页槽——
    \\qslotopen 的测高基于页/栏余量，在 multicols 里不成立，legacy 双栏本来
    就把整题交给 qpracticesolve 盒子）。

    compact=False（流式）：解答题用 qpracticesolve 作答盒、题间断栏（与 legacy
    双栏的解答题口径一致）；compact=True（紧凑）：全部题走 samepage 题干盒、
    不留作答空白——「不留空」在双栏下靠的就是不设 practice_solve，渲染层零改动。
    """
    page = []
    solve_idx = 0
    for i, q in enumerate(questions, 1):
        qtype = q.get("type")
        solve = qtype not in _SINGLE | _MULTI | _BLANK
        block = {"kind": "question", "num": i, "body": q["body"],
                 "layout": "practice", "solution": q.get("solution"),
                 "type": qtype, **_img_fields(q)}
        if solve and not compact:
            block["practice_solve"] = True
            block["practice_solve_index"] = solve_idx
            solve_idx += 1
        page.append(block)
    return [page] if page else []


def _paginate_one_grouped(questions, bank_subject="math", *,
                          slide_mode=False, std_points=None):
    """一页一题 + 分题型：分桶重排（题号按桶序连续），桶内每题独占一页。

    slide_mode（16:9 横版）：全部 layout="slide"（课件渲染，每题一张）；
    否则选填 slot_half（超半页自动升级）、解答 full 独占——与讲解（lecture）
    同一条规则，保证「一页一题」在 ±分题型两个选法下行为一致，差异只有重排
    与桶标题。桶标题是独立 heading 块：full / slide 两种 layout 都不消费题目
    block 上的 heading 字段（见 _render_block），绑定在题上会整段消失。
    """
    pages = []
    page = _new_page(pages)
    num = 1
    for sec, (pkey, name, bucket) in enumerate(_bucket_questions(questions)):
        page.append(_bucket_heading(sec, pkey, name, len(bucket),
                                    bank_subject, std_points))
        for q in bucket:
            if slide_mode:
                page.append({"kind": "question", "num": num, "body": q["body"],
                             "layout": "slide", "solution": q.get("solution"),
                             "type": q.get("type"), **_img_fields(q)})
                page = _new_page(pages)
            elif pkey == "solve":
                page.append({"kind": "question", "num": num, "body": q["body"],
                             "layout": "full", "solution": q.get("solution"),
                             "type": q.get("type"), **_img_fields(q)})
                page = _new_page(pages)
            else:
                page.append({"kind": "question", "num": num, "body": q["body"],
                             "layout": "slot_half", "solution": q.get("solution"),
                             "type": name, **_img_fields(q)})
            num += 1
    return [p for p in pages if p]


def _paginate_two_grouped(questions, fullpage_ids, bank_subject="math", *,
                          std_points=None):
    """一页两题 + 分题型：分桶重排，桶内每题半页槽（note 规则），每桶从新页起。

    fullpage_ids 的手动整页开关仍然生效（题卡上点过「独占整页」的题优先于
    自适应槽位，与 _paginate_two 同一口径）。
    """
    pages = []
    page = _new_page(pages)
    num = 1
    for sec, (pkey, name, bucket) in enumerate(_bucket_questions(questions)):
        if any(page):
            page = _new_page(pages)
        page.append(_bucket_heading(sec, pkey, name, len(bucket),
                                    bank_subject, std_points))
        for q in bucket:
            if q.get("id") in fullpage_ids:
                if any(page):
                    page = _new_page(pages)
                page.append({"kind": "question", "num": num, "body": q["body"],
                             "layout": "full", "solution": q.get("solution"),
                             "type": q.get("type"), **_img_fields(q)})
                page = _new_page(pages)
                num += 1
                continue
            page.append({"kind": "question", "num": num, "body": q["body"],
                         "layout": "slot_half", "solution": q.get("solution"),
                         "type": q.get("type"), **_img_fields(q)})
            num += 1
    return [p for p in pages if p]


def _paginate_custom(questions, spec, keypoints="", fullpage_ids=None,
                     std_opts=None, bank_subject="math"):
    """四维自定义组合的分页总入口：命中等价表复用 legacy 分页器，其余走薄策略。"""

    def dispatch():
        # 自适应 + 双栏按流式折算（与 resolve_export_layout 同一口径）：分题型
        # 命中 practice 等价表，不分题型落到下面的 practice_stream。
        key = ("flow" if spec.layout == "adaptive" and spec.columns == 2
               else spec.layout, spec.grouped, spec.columns, spec.ratio)
        sp = None
        if spec.std_head:
            sp = (std_opts or {}).get("section_points") or {}
            if key == ("flow", True, 1, "a4"):
                # 标准试卷预设的四维 + 开关：与 legacy exam_std 完全同一条路径
                return _paginate_exam_std(questions, std_opts or {}, bank_subject)
        legacy = _DIMS_TO_LEGACY.get(key)
        if legacy == "exam":
            return _paginate_exam(questions, bank_subject)
        if legacy == "list":
            return _paginate_list(questions, bank_subject)
        if legacy == "lecture":
            return _paginate_lecture(questions)
        if legacy == "note":
            return _paginate_two(questions, fullpage_ids, start_num=1)
        if legacy == "practice":
            return _paginate_practice(questions, bank_subject)
        if legacy == "slides":
            return _paginate_slides(questions)
        # —— 8 个新组合（薄策略；wide 与 columns=2 已被 resolve clamp 互斥）——
        if spec.wide:
            return _paginate_one_grouped(questions, bank_subject,
                                         slide_mode=True, std_points=sp)
        if spec.columns == 2:
            if spec.layout == "compact" and spec.grouped:
                return _paginate_list(questions, bank_subject)
            return _paginate_practice_stream(
                questions, compact=(spec.layout == "compact"))
        if spec.layout == "one":
            return _paginate_one_grouped(questions, bank_subject,
                                         slide_mode=False, std_points=sp)
        if spec.layout == "two":
            return _paginate_two_grouped(questions, fullpage_ids, bank_subject,
                                         std_points=sp)
        if spec.layout == "adaptive":
            return _paginate_adaptive(questions, bank_subject,
                                      grouped=spec.grouped)
        return _paginate_stream(questions, compact=(spec.layout == "compact"))

    pages = dispatch()
    # 知识要点是内容型字段（非空即生效）：与讲义（handout）同一规则，一页两题
    # 组合带知识要点时独占首页。其余组合没有「讲义首页」的语义，忽略。
    if keypoints and keypoints.strip() and spec.layout == "two":
        pages = [[{"kind": "keypoints", "text": keypoints.strip()}]] + pages
    return pages


# ---------------------------------------------------------------------------
# 渲染：把 paginate() 的页结构转成含 raw-LaTeX 的 Markdown
# ---------------------------------------------------------------------------


def _heading_latex(text: str, points: str = "", colon: bool = False) -> str:
    """大题标题的 raw-LaTeX（左对齐加粗）。

    points 非空则把分值说明嵌进标题、与标题同样式，而非散在标题外。两种连接方式：
      - colon=False（默认）：中文括号包裹，「一、选择题（本题共 8 小题…）」。
      - colon=True：冒号连接，「一、单选题：本题共 8 小题，每小题 5 分，共 40 分。
        在每小题给出的四个选项中，只有一项是符合题目要求的」——标准试卷（exam_std）
        用这种，说明句较长、括号包起来读着累（用户指定的格式）。
    用户手填文本需转义；colon=False 且已自带全角括号时不再重复包裹。
    """
    label = text
    if points and points.strip():
        p = _latex_escape(points.strip())
        if colon:
            label = f"{text}：{p}"
        elif p.startswith("（") or p.startswith("("):
            label = f"{text}{p}"
        else:
            label = f"{text}（{p}）"
    # \nobreak（=\penalty10000）：禁止标题与其后首题之间断页——页数一多时
    # 「三、解答题」会孤零零留在页底（2026-10-04 审查实测）。
    return (f"\\par\\noindent{{\\large\\bfseries {label}}}"
            f"\\par\\nobreak\\vspace{{0.4em}}")


def _half_block(num: int, body: str, heading: str = "", qtype: str = None,
                img_align: str = None, img_width=None, img_split=False,
                img_layouts=None, img_files: list[str] = None,
                solution_md: str = "") -> str:
    """把一题包进固定半页高的 minipage；可在题前嵌入大题标题（随题绑定）。

    原位图仍**留在文字中间**（这是本功能在试卷模式下唯一的落地点：exam/two 模式
    把每道非整页题都排成半页块，解答题全都在这里，而图夹在题干中间恰恰是解答题
    最常见的形态）。撑破版面的风险由 _HALF_INLINE_HCAP 限高解决，不靠把图挪到题末
    ——挪走并不省高度，只是把需求丢了。
    尾图不受影响：分栏/绕排的既有优先级与尺寸都与改动前逐字一致。
    """
    inner = _q_md(num, body, qtype, img_align, img_width, img_split, img_layouts,
                  img_files, inline_hcap=_HALF_INLINE_HCAP, box_context=True)
    if heading:
        # 标题的 raw-LaTeX 直接拼进 minipage 顶部，再接题目 Markdown
        inner = _raw(_heading_latex(heading)).strip("\n") + "\n\n" + inner
    if solution_md:
        # inline 解析进盒：纯文字解析没有 wrapfig 冲突（含图解析在 _render_block
        # 已改走 natural 分支不进槽盒），题+解析一起参与槽位高度决策——旧版
        # 「有解析就整体退出槽位」使「一页两题」在有解析的题上失效（2026-10-04 审查）。
        inner = inner + "\n\n" + solution_md
    return (_HALF_OPEN + inner + _HALF_CLOSE)


def _slot_block(num: int, body: str, frac: float, heading: str = "",
                qtype: str = None, img_align: str = None, img_width=None,
                img_split=False, img_layouts=None,
                img_files: list[str] = None, solution_md: str = "") -> str:
    """把一题包进自适应槽位（frac = 目标高度占版心比例）。

    与 _half_block 的差别只在盒子：这里内容超出目标就升级到更大的槽位，且断页
    由 TeX 按本页余量自行决定（见 exam_template.tex 的 \\qslotclose）。
    原位图的限高沿用 _HALF_INLINE_HCAP：槽位可能只有 1/4 页，按全页上限排的图会
    撑破槽位；升级机制只保证内容不丢，不代表可以让图任意大。
    """
    inner = _q_md(num, body, qtype, img_align, img_width, img_split, img_layouts,
                  img_files, inline_hcap=_HALF_INLINE_HCAP, box_context=True)
    if heading:
        inner = _raw(_heading_latex(heading)).strip("\n") + "\n\n" + inner
    if solution_md:
        # inline 解析进槽：同 _half_block——纯文字解析参与槽位高度与分页决策。
        inner = inner + "\n\n" + solution_md
    return (_slot_open(frac) + inner + _SLOT_CLOSE)


def _natural_question_block(num: int, body: str, heading: str = "",
                            qtype: str = None, img_align: str = None,
                            img_width=None, img_split=False,
                            img_layouts=None,
                            img_files: list[str] = None) -> str:
    """渲染不带固定槽位的题块。

    inline 解析必须紧跟题后；固定半页/槽位会把题干放进测量盒，无法安全插入
    解析中的 wrapfigure。此分支只在 inline 且题目确有解析时使用，其他导出仍
    保持原有作答槽位布局。
    """
    inner = _q_md(num, body, qtype, img_align, img_width, img_split,
                  img_layouts, img_files)
    if heading:
        inner = _raw(_heading_latex(heading)).strip("\n") + "\n\n" + inner
    return inner


def _solution_body(text: str, files: list[str] = None,
                   sol_img_layouts=None, sol_img_split=None) -> str:
    """解析正文（含图）→ Markdown，支持原位图、题末图和图文混排。

    解析文本与题干一样经过 _stage_images，正文里同样带着 QFIGSLOT 哨兵；这里不摘
    就会把裸 `QFIGSLOT0` 直接印进 PDF。解析不参与四图配选项；除「原位填 +
    尾图拼到末尾」外，可用历史值 sol_img_split="full" 开启题末图文混排。
    每图的宽度/对齐仍可逐图设置（sol_img_layouts，与题干 img_layouts 同构、序号
    各自独立编号）。
    **哨兵必须在这里就换掉**：inline 模式下解析会被拼到题干后面，而两边的哨兵编号
    各自从 0 起（题干用 _IMG_FILES_KEY，解析用 _SOL_IMG_FILES_KEY），拼完再统一
    替换就会张冠李戴。历史值 sol_img_split="full" 仍保留，但输出改为
    wrapfigure：文字超过图片高度后恢复整行，避免旧 minipage 右栏图下留白。
    """
    text = _break_solution_lines(_strip_solution_leading_label(text))
    marks = _marks_of(files)
    if not marks:
        return _format_options(_drop_slots(text))
    plan = plan_figs(text, None, sol_img_layouts, sol_img_split)
    layouts = _parse_layouts(sol_img_layouts)
    all_ids = [s["i"] for s in plan["slots"] if s["i"] < len(marks)]
    inline_ids = [s["i"] for s in plan["slots"] if s["pos"] == "stem"]
    tail_ids = [s["i"] for s in plan["slots"]
                if s["i"] not in set(inline_ids) and s["i"] < len(marks)]
    if sol_img_split == "full" and all_ids:
        unit, rest_ids = _split_first_unit(all_ids, plan)
        split = _split_at_figure_slot(text, unit)
        if split is not None:
            before_text, after_text = split
            inline_set = set(inline_ids)
            rest_inline = [item for item in rest_ids if item in inline_set]
            rest_tail = [item for item in rest_ids if item not in inline_set]
            if rest_inline:
                after_text = _fill_slots(
                    after_text, rest_inline, marks, layouts, plan)
            before_md = _format_options(_drop_slots(before_text))
            after_md = _format_options(_drop_slots(after_text))
            wrapped = _place_text_figure_wrap(
                after_md, unit.get("ids") or [], marks, layouts, plan,
                width=_DEFAULT_IMG_FRAC, before=before_md)
            return wrapped + _figs_latex_planned(
                rest_tail, marks, layouts, plan)
    if inline_ids:
        text = _fill_slots(text, inline_ids, marks, layouts, plan)
    markdown = _format_options(_drop_slots(text))
    return markdown + _figs_latex_planned(tail_ids, marks, layouts, plan)


def _solution_md(text: str, files: list[str] = None, sol_img_layouts=None,
                 sol_img_split=None) -> str:
    """解析正文的 Markdown；不额外添加“【解析】”等可见前缀。"""
    return _solution_body(text, files, sol_img_layouts, sol_img_split)


def _render_block(b: dict, solution_mode: str = "none") -> str:
    """单个 block → Markdown 片段。"""
    kind = b["kind"]
    if kind == "heading":
        if b.get("suppress_render"):
            return ""
        return _raw(_heading_latex(b["text"], b.get("points", ""),
                                   colon=b.get("colon", False)))
    if kind == "keypoints":
        return (_raw("\\par\\noindent{\\large\\bfseries 知识要点}"
                     "\\par\\vspace{0.4em}") + "\n" + b["text"])
    if kind == "solution_head":
        return _raw("\\begin{center}{\\LARGE\\bfseries 参考解析}\\end{center}"
                    "\\vspace{0.6em}")
    if kind == "solution_item":
        sol_body = _solution_body(b["text"], b.get(_SOL_IMG_FILES_KEY),
                                  b.get("sol_img_layouts"),
                                  b.get("sol_img_split"))
        # sol_body 可能以 raw-LaTeX fenced block（wrapfigure）开头；围栏必须从行首
        # 开始，否则 Pandoc 会把它当成普通反引号文本，图片混排随即失效。
        return _expand_tables(f"**{b['num']}.**\n\n{sol_body}")
    if kind == "slide_cover":
        return _raw(f"\\qslidecover{{{_latex_escape(b['text'])}}}")
    if kind == "solution_slide":
        sol_body = _solution_body(b["text"], b.get(_SOL_IMG_FILES_KEY),
                                  b.get("sol_img_layouts"),
                                  b.get("sol_img_split"))
        head = _raw(f"\\qslidehead{{第 {b['num']} 题解析}}")
        return _expand_tables(head + sol_body)
    # question
    if kind != "question":
        # 未知 kind 旧行为是"掉进 question 分支"静默渲染/KeyError（2026-10-04
        # 审查）；显式报出来源，避免新 block 类型被错渲。
        raise ExportError(
            f"未知的排版块类型 {kind!r}：可能是新版 paginate 新增的 block kind，"
            "_render_block 缺少对应分支")
    layout = b.get("layout", "flow")
    body = b["body"]
    sol = b.get("solution")
    # inline：题后紧接解析。受限槽位由下面的自然题块分支处理，避免把解析放进
    # qslot/half 的测量盒；其余布局直接在题块之后追加。
    img_align = b.get("img_align")
    img_width = b.get("img_width")
    img_split = b.get("img_split")   # 原样传字符串（opts/full/sub），_norm_split 归一化
    img_layouts = b.get("img_layouts")   # 多图逐图设置，JSON 串，见 _parse_layouts
    img_files = b.get(_IMG_FILES_KEY)     # 下标 = QFIGSLOT 哨兵编号，见 _stage_images
    # 解析必须在题号的 qopen/qclose 列表**外**另排：wrapfig 在 list 里会明确警告
    # ``wrapfigure used inside a conflicting environment``，随后把图片强制漂到解析末尾，
    # 页面就仍然是“上面全是文字、下面单独一张图”。题干先由 _q_md 完整收口，再接
    # 解析，wrapfigure 才能真正让文字沿图侧边环绕并在图下恢复整行。
    inline_solution = ""
    if solution_mode == "inline" and sol:
        inline_solution = _solution_md(
            sol, b.get(_SOL_IMG_FILES_KEY), b.get("sol_img_layouts"),
            b.get("sol_img_split"))
    # 仅当解析**含图**才退出槽位：wrapfig 进不了测量盒/list（见下方注释）。
    # 纯文字解析留在槽内参与分页决策（2026-10-04 审查：旧版一刀切使三种模式的
    # 「一页两题」承诺在有解析的题上整体失效）。
    natural_inline = bool(
        inline_solution and layout in _INLINE_NATURAL_LAYOUTS
        and b.get(_SOL_IMG_FILES_KEY))

    if layout == "slide":
        # 课件页的题号放进顶部色条，正文不再重复显示「1.」。_q_md 仍负责选择题
        # 分栏、图片落位和小问缩进，避免横版模式另造一套题目渲染逻辑。
        head = _raw(f"\\qslidehead{{第 {b['num']} 题}}")
        md = _slide_left_content(
            head + _q_md(None, body, b.get("type"), img_align, img_width,
                         img_split, img_layouts, img_files, box_context=True)
            + ("\n\n" + inline_solution if inline_solution else "")
        )
    elif layout == "half":
        if natural_inline:
            md = (_natural_question_block(
                b["num"], body, heading=b.get("heading", ""),
                qtype=b.get("type"), img_align=img_align,
                img_width=img_width, img_split=img_split,
                img_layouts=img_layouts, img_files=img_files)
                  + "\n\n" + inline_solution)
        else:
            md = _half_block(
                b["num"], body, heading=b.get("heading", ""),
                qtype=b.get("type"), img_align=img_align,
                img_width=img_width, img_split=img_split,
                img_layouts=img_layouts, img_files=img_files,
                solution_md=inline_solution)
    elif layout in ("slot_half", "slot_quarter"):
        if natural_inline:
            md = (_natural_question_block(
                b["num"], body, heading=b.get("heading", ""),
                qtype=b.get("type"), img_align=img_align,
                img_width=img_width, img_split=img_split,
                img_layouts=img_layouts, img_files=img_files)
                  + "\n\n" + inline_solution)
        else:
            # 自适应槽位：目标高度放不下就升级（半页→整页 / 1/4→半页→整页），
            # 断页由 TeX 按本页余量决定。
            frac = _SLOT_HALF if layout == "slot_half" else _SLOT_QUARTER
            md = _slot_block(
                b["num"], body, frac, heading=b.get("heading", ""),
                qtype=b.get("type"), img_align=img_align,
                img_width=img_width, img_split=img_split,
                img_layouts=img_layouts, img_files=img_files,
                solution_md=inline_solution)
    elif layout == "practice":
        practice_solve = bool(b.get("practice_solve"))
        md = _q_md(b["num"], body, b.get("type"), img_align, img_width,
                   img_split, img_layouts, img_files,
                   choice_nowrap_multicol=True,
                   practice_image_wrap=True)
        if (practice_solve
                and not (solution_mode == "inline" and sol)):
            # 双栏刷题只给解答题留少量作答区；选择、填空题题后直接接下一题。
            md += _raw(
                "\\par\\nobreak\\vspace*"
                f"{{{_practice_answer_space(body, b.get('difficulty'))}}}"
            )
        if practice_solve:
            if b.get("heading"):
                md = (_raw(_heading_latex(b["heading"])).strip("\n")
                      + "\n\n" + md)
            wrapper = (_raw("\\begin{qpracticesolve}").strip("\n")
                       + "\n\n" + md + "\n\n"
                       + _raw("\\end{qpracticesolve}").strip("\n"))
            if int(b.get("practice_solve_index") or 0) > 0:
                wrapper = (_raw("\\columnbreak").strip("\n")
                           + "\n\n" + wrapper)
            md = wrapper
        else:
            # 正常长度题尽量整题留在同一栏，避免题干最后两行孤零零续到右栏；
            # samepage 只包题干。解析图文混排含 wrapfigure，不能进入该冲突环境。
            md = (_raw("\\begin{samepage}").strip("\n") + "\n\n" + md
                  + "\n\n" + _raw("\\end{samepage}").strip("\n"))
        if inline_solution:
            # qpracticesolve 是 vbox，samepage 也是 wrapfig 明确不支持的环境；必须等
            # 题干的包装全部收口后再追加解析，图片才会在正文侧边正常环绕。
            md += "\n\n" + inline_solution
    else:
        md = _q_md(b["num"], body, b.get("type"), img_align, img_width,
                   img_split, img_layouts, img_files)
        if b.get("adaptive_solve"):
            # 自适应版式：解答题作答留白随难度与一级小问数自动变化（与双栏刷题
            # 同一公式）；留白必须落在题干之后、解析之前——作答区不能被解析隔开。
            md = md + _raw(
                f"\\vspace{{{_practice_answer_space(body, b.get('difficulty'))}}}")
        if inline_solution:
            md += "\n\n" + inline_solution
        # flow / full / solve_compact 都直接排
        if layout == "solve_compact":
            # 标准试卷解答题：随文连续排，题后留作答空白（可跨页），比半页更紧凑。
            # 空白高度按小问数量给（_solve_answer_space），不再是固定 5.5em——
            # 单问小题和四五问的大题此前留白一样多，前者显得空、后者写不下。
            md = md + _raw(f"\\vspace{{{_solve_answer_space(body)}}}")
    # 表格令牌在此展开成 raw-latex 块：此前所有按行/按标签扫描的正则都已跑完，
    # 不会再把 tabular 切坏（见 _stash_tables 注释）
    return _expand_tables(md)


def _render_pages(pages: list[list[dict]], solution_mode: str = "none") -> str:
    """页结构 → 整份 Markdown，页间插 \\clearpage。"""
    page_md = []
    for page in pages:
        blocks = [_render_block(b, solution_mode) for b in page]
        page_md.append("\n\n".join(blocks))
    return CLEARPAGE.join(page_md)


def _render_practice_pages(pages: list[list[dict]],
                           solution_mode: str = "none") -> str:
    """双栏刷题页：每个显式分页段各自开启双栏，避免 clearpage 留在环境内部。

    `multicols*` 不平衡末页：先填满左栏再流向右栏，正好对应刷题册的阅读顺序。
    解题页（separate 解析）不套多栏：解析图文混排含 wrapfigure，而 wrapfig 与
    multicols 冲突（图会被抛到栏末、位置全乱）；解析整页宽单栏排也更便读
    （2026-10-04 P1-9）。
    """
    wrapped = []
    for page in pages:
        blocks = "\n\n".join(_render_block(b, solution_mode) for b in page)
        if any(b.get("kind") == "solution_head" for b in page):
            wrapped.append(blocks)
            continue
        wrapped.append(
            _raw("\\qpracticebegin").strip("\n") + "\n\n" + blocks
            + "\n\n" + _raw("\\qpracticeend").strip("\n")
        )
    return CLEARPAGE.join(wrapped)


_MODES = {
    "list": "清单模式",
    "note": "笔记模式",
    "lecture": "讲解模式",
    "slides": "横版课件模式",
    "practice": "双栏刷题模式",
    "exam": "试卷模式",
    "exam_std": "标准试卷模式",
    "handout": "讲义模式",
}

# PDF 导出入口与 Word 导出入口共用这组稳定枚举。未知值不能默默回落到清单模式，
# 否则界面拼写错误会生成一份看似成功、实际版式完全不同的试卷。
SUPPORTED_MODES = frozenset(_MODES)
SUPPORTED_SOLUTION_MODES = frozenset({"none", "inline", "separate"})
SUPPORTED_FORMATS = frozenset({"pdf", "tex", "zip"})

# —— 四维度布局规格（2026-10-05 导出抽屉）——
# 「版式 / 分类模式 / 页面模式 / 比例」从 mode 枚举里拆出来，成为四个独立维度。
# 历史上四者收敛在 mode 一个枚举里，只能覆盖 6 种有效组合，用户在自定义里选
# 「一页两题 + 分题型」这类组合就没有生成路径。本层只服务自定义组合：调用方
# 不传 layout（None）时完全不经过这里，旧调用方 / 旧任务快照 / Agent / Word
# 链路的 mode 行为一个字节不变（tests/test_export_golden.py 逐字符钉住）。
#
# 为什么不直接扩 mode 枚举：template_pipeline.SUPPORTED_MODES 是硬编码 8 值
# 元组，所有已注册自定义模板的 supported_modes 都按它校验，Word 侧与任务面板
# 另有多份白名单——新增枚举值会让全部存量模板与白名单失效。四维参数则不碰
# 任何既有枚举。
EXPORT_LAYOUTS = ("flow", "compact", "adaptive", "one", "two")
EXPORT_RATIOS = ("a4", "wide")
_LAYOUT_LABELS = {"flow": "流式", "compact": "紧凑", "adaptive": "自适应",
                  "one": "一页一题", "two": "一页两题"}
_RATIO_LABELS = {"a4": "A4", "wide": "横版 16:9"}

# 四维组合 → 既有 mode 的等价表：命中即整体复用对应 legacy 路径（分页器、标题
# 样式、槽位基准、模板变量全同），保证「预设 = 把选项选好」——同一组四维不管
# 从预设还是自定义进入，产物逐字节一致（tests/test_export_layout_spec.py 对拍
# 钉住）。std_exam 是另开的正交开关（见 resolve_export_layout）：它把
# (flow, 分题型, 单栏, A4) 提升为 exam_std 完整路径，避免「选了标准试卷再动
# 一个维度，卷头静默消失」。
_DIMS_TO_LEGACY = {
    ("flow", True, 1, "a4"): "exam",
    ("compact", True, 1, "a4"): "list",
    ("one", False, 1, "a4"): "lecture",
    ("two", False, 1, "a4"): "note",
    ("flow", True, 2, "a4"): "practice",
    ("one", False, 1, "wide"): "slides",
}


@dataclass(frozen=True)
class ExportLayoutSpec:
    """自定义导出组合归一化后的规格（layout=None 时由 resolve 返回 None）。

    前 5 个字段是 clamp 后的用户维度；其余是按它们推导的派生值——分页、标题、
    槽位基准、模板变量、任务面板各消费点统一读这里，避免各自重复推导出不一致。
    compat_mode 是命中等价表时的 legacy mode（否则 "list"）：自定义组合的标题
    样式等按它锚定，与预设路径保持同源。
    """

    layout: str
    grouped: bool
    columns: int
    ratio: str
    std_exam: bool
    wide: bool
    two_columns: bool
    pagerel: bool
    one_solution_per_page: bool
    std_head: bool
    compat_mode: str
    template_mode: str
    label: str


def _as_bool(value, default: bool = False) -> bool:
    """宽松布尔解析：表单 / 快照 / Agent 可能给 bool、1/0 或 "1"/"0"/""。

    空串视为「未提供」返回 default——表单没填和明确填 0 是两种语义，不能把
    没填的复选框静默当成「关」。"""
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "":
            return default
        return text not in ("0", "false", "off", "no")
    return bool(value)


def resolve_export_layout(layout=None, grouped=None, columns=None, ratio=None,
                          std_exam=False) -> "ExportLayoutSpec | None":
    """四维度原始值 → ExportLayoutSpec；layout 为 None 时返回 None（legacy 路径）。

    clamp 规则（前端会同步禁用矛盾组合，这里只是后端兜底，不报错）：
      - 横版 16:9 只支持「一页一题」：ratio=wide 强制 layout=one、columns=1；
      - 「一页一题 / 一页两题」是单栏页结构：columns=2 一律回落单栏；
      - 卷首开关 std_exam 只在单栏 A4 下生效（横版是课件没有卷头概念；双栏里
        卷头与双栏标题各行其是，与其渲染错位，不如明确只在单栏 A4 生效）。
    非法枚举值不静默回落——与 mode 校验同一原则，拼写错误要立刻报出来。
    """
    if layout is None:
        return None
    if not isinstance(layout, str) or layout not in EXPORT_LAYOUTS:
        raise ExportError(f"不支持的版式：{layout}")
    if ratio in (None, ""):
        ratio = "a4"
    if not isinstance(ratio, str) or ratio not in EXPORT_RATIOS:
        raise ExportError(f"不支持的比例：{ratio}")
    if columns in (None, ""):
        columns = 1
    try:
        columns = int(columns)
    except (TypeError, ValueError):
        columns = 0
    if columns not in (1, 2):
        raise ExportError(f"不支持的页面模式：{columns}")
    grouped = _as_bool(grouped, default=True)
    std_exam = _as_bool(std_exam)

    wide = ratio == "wide"
    if wide:
        layout = "one"          # 横版只支持一页一题
        columns = 1
    elif columns == 2 and layout in ("one", "two"):
        columns = 1             # 一页 N 题是单栏页结构

    std_head = std_exam and columns == 1 and not wide
    # 自适应 + 双栏 ≡ 流式 + 双栏：自适应的留白公式本来就取自双栏刷题（见
    # _paginate_adaptive），查等价表时按流式折算——分题型下命中 practice 预设，
    # 产物与流式双栏逐字节一致，不会把「分题型」静默丢掉。
    key = ("flow" if layout == "adaptive" and columns == 2 else layout,
           grouped, columns, ratio)
    if std_head and key == ("flow", True, 1, "a4"):
        compat = "exam_std"     # 与标准试卷预设完全同一条路径
    else:
        compat = _DIMS_TO_LEGACY.get(key, "list")

    # 模板门禁折算（_resolve_template_path 的 ensure_mode 用）：按组合实际需要的
    # 运行时宏对齐 template_pipeline._RUNTIME_BY_MODE——横版要 qslide 宏、双栏要
    # qpractice 宏、槽位类要 qslot 宏、卷头要 qnotebox。同类里取要求更宽松的那个
    # （如流式解答题的槽位宏在 exam 与 note 下相同，取 exam 即可）。
    if wide:
        template_mode = "slides"
    elif columns == 2:
        template_mode = "practice"
    elif std_head:
        template_mode = "exam_std"
    elif layout == "two":
        template_mode = "note"
    elif layout == "one":
        template_mode = "lecture"
    elif layout == "compact":
        template_mode = "list"
    else:
        template_mode = "exam"

    label = (f"{_LAYOUT_LABELS[layout]}·"
             f"{'分题型' if grouped else '不分题型'}·"
             f"{'双栏' if columns == 2 else '单栏'}·{_RATIO_LABELS[ratio]}")
    return ExportLayoutSpec(
        layout=layout, grouped=grouped, columns=columns, ratio=ratio,
        std_exam=std_exam, wide=wide, two_columns=columns == 2,
        pagerel=layout in ("one", "two") and not wide,
        one_solution_per_page=wide, std_head=std_head,
        compat_mode=compat, template_mode=template_mode, label=label)


def _validate_export_options(*, mode: str, solution_mode: str,
                             fmt: str | None = None) -> None:
    """校验导出公开枚举，避免未知参数静默走默认分支。"""
    if not isinstance(mode, str) or mode not in SUPPORTED_MODES:
        raise ExportError(f"不支持的导出模式：{mode}")
    if (not isinstance(solution_mode, str)
            or solution_mode not in SUPPORTED_SOLUTION_MODES):
        raise ExportError(f"无效的解析位置：{solution_mode}")
    if fmt is not None and (not isinstance(fmt, str) or fmt not in SUPPORTED_FORMATS):
        raise ExportError(f"不支持的导出格式：{fmt}")

# —— 作答空间统一规则（2026-10-04 P1-10 收敛）——
# 此前 solve_compact/exam_std 与 practice 各有一套公式：单位不同（em vs
# \baselineskip）、小问口径不同（所有小问行 vs 一级小问）、且都不可配置。
# 现收敛为一张声明表 + 一个计算函数 _answer_space：
#   * 单位一律正文行高 \baselineskip；
#   * 小问口径统一为「一级小问」（_SUBQ_TOP_RE 识别，(i) 这类次级小问不单独加行）；
#   * 两模式仅在参数上不同——练习册额外按难度线性加行（用户打样确认过的效果）；
#   * 运行时可经 config.ANSWER_SPACE（按 scope 的字段子集）覆写，供设置面板
#     或测试注入；无覆写时即下表默认值。
# solve 旧值 2em/0.5em/4em 按 1em≈0.8\baselineskip 折算为 1.6/0.4/3.2（视觉
# 差 <1%）；口径改为一问后，带 (i) 次问的题留白略减——这正是要治的不一致。
# 标准试卷不是用来作答的（考试用答题卡），留白只为卷面呼吸感；练习册相反，
# 是直接印给学生写的，作答区由本表按难度足额给。简单试卷（exam）的作答空间
# 由半页/整页槽位给足，不经过本表。
_ANSWER_SPACE_DEFAULT = {
    "solve":    {"base": 1.6, "per_sub": 0.4, "per_difficulty": 0.0, "max": 3.2},
    "practice": {"base": 3.0, "per_sub": 1.5, "per_difficulty": 1.0, "max": 12.0},
}


def _answer_space_spec(scope: str) -> dict:
    """取某个场景的作答空间参数：内置默认表 + config.ANSWER_SPACE 覆写合并。"""
    spec = dict(_ANSWER_SPACE_DEFAULT[scope])
    overrides = getattr(config, "ANSWER_SPACE", None) or {}
    spec.update(overrides.get(scope) or {})
    return spec


def _answer_space(body: str, scope: str, difficulty=None) -> str:
    """统一作答留白计算（正文行高倍数，随难度与一级小问数线性增长）。

    body 先经 _break_subquestions 归一，再数「一级小问」行（(1)(2)... 与 (i)(ii)
    ... 同属一级，见 _SUBQ_TOP_RE 注释）；识别不到任何小问（整题一段）时按 1 问
    计——不能给 0，没有作答空间无法写字。
    """
    spec = _answer_space_spec(scope)
    normalized = _break_subquestions(body)
    sub_count = sum(
        1 for line in normalized.splitlines()
        if _SUBQ_TOP_RE.match(line.lstrip(" \t　"))
    )
    sub_count = max(sub_count, 1)
    try:
        level = max(1, min(5, int(difficulty)))
    except (TypeError, ValueError):
        level = 3
    lines = (
        spec["base"]
        + spec["per_sub"] * (sub_count - 1)
        + spec["per_difficulty"] * (level - 1)
    )
    return f"{min(lines, spec['max']):.2f}\\baselineskip"


def _solve_answer_space(body: str) -> str:
    """标准试卷解答题作答留白（solve 场景，规则见 _ANSWER_SPACE_DEFAULT）。"""
    return _answer_space(body, "solve")


def _practice_answer_space(body: str, difficulty) -> str:
    """双栏刷题解答题作答留白（practice 场景，规则见 _ANSWER_SPACE_DEFAULT）。"""
    return _answer_space(body, "practice", difficulty)


# 标准试卷各题型的大题说明尾句（跟在「本题共 x 小题，每小题 y 分，共 z 分。」后）。
# 仿国标考卷措辞，与 exam-zh 的 02-math-basic 参照一致。
_STD_SECTION_TAIL = {
    "single": "在每小题给出的四个选项中，只有一项是符合题目要求的。",
    "multi": "在每小题给出的选项中，有多项符合题目要求。全部选对的得满分，"
             "部分选对的得部分分，有选错的得 0 分。",
    "blank": "",
    "solve": "解答应写出文字说明、证明过程或演算步骤。",
}
# 各题型每小题默认分值（用户没填时用）。仿新高考数学卷：单选 5 / 多选 6 / 填空 5。
# 解答题分值不齐（13/15/17…），故不设默认每题分，只在用户填了才算总分。
_STD_DEFAULT_POINTS = {"single": 5, "multi": 6, "blank": 5, "solve": None}


def _std_section_desc(kind: str, count: int, per_point: str) -> str:
    """生成大题说明：「本题共 x 小题，每小题 y 分，共 z 分。<尾句>」。

    x 由实际选入的题数决定（用户要求），y 取用户填的每小题分值、缺省用
    _STD_DEFAULT_POINTS，z = x * y 自动算。per_point 填不出数字（空/非法）时
    只出「本题共 x 小题。」+ 尾句，不编造分值。
    解答题各小题分值通常不等，用户填了每题分才算总分，否则只报题数。
    """
    p = (per_point or "").strip()
    try:
        per = int(p) if p else _STD_DEFAULT_POINTS.get(kind)
    except ValueError:
        per = None
    if per:
        head = f"本题共 {count} 小题，每小题 {per} 分，共 {per * count} 分。"
    else:
        head = f"本题共 {count} 小题。"
    tail = _STD_SECTION_TAIL.get(kind, "")
    return f"{head}{tail}" if tail else head


def _std_head_latex(title: str, secret_notice: str, exam_notes: str,
                    subject: str = "", info_bar: bool = True) -> str:
    """标准试卷卷首，仿国标考卷版式（exam-zh 02-math-basic 参照）：

      保密说明（左上角加粗）
      居中大标题
      居中科目副标题（subject 非空时）
      考生信息栏 姓名____ 班级____ 学号____（info_bar 时，两端撑开）
      卷首说明「注意事项」列表（exam_notes 非空时，逐条编号）
      分隔线

    所有用户文本转义。顺带收紧行距（\\parskip 0.2em）实现「题目间隔较小」，
    只影响本次导出、不改 exam_template.tex。
    """
    # \parskip 收紧行距；club/widow 惩罚拉满，减少「题干与小问被拆到两页」的孤/寡行
    # ★(U+2605) 归入 CJK 字符类：走中文字体（SimSun/FandolSong 有此字形），
    # 否则西文主字体缺字形，「绝密★启用前」的星号会显示为空白
    out = ["\\setlength{\\parskip}{0.2em}",
           "\\clubpenalty=10000\\widowpenalty=10000\\interfootnotelinepenalty=10000",
           "\\xeCJKDeclareCharClass{CJK}{\"2605}"]
    sn = (secret_notice or "").strip()
    if sn:
        # 保密说明置左上角（国标考卷惯例），不居中
        out.append(f"\\noindent{{\\bfseries {_latex_escape(sn)}}}\\par")
    # 标题同样要转义：其他模式的标题走 pandoc 的 `% title`（pandoc 自己会转义），
    # 只有 exam / exam_std 是自己拼 raw LaTeX，漏了转义时标题里一个 `_` 就让
    # xelatex 报 Missing $ inserted、整份导出失败。
    out.append(f"\\begin{{center}}{{\\LARGE {_latex_escape(title)}}}"
               f"\\end{{center}}")
    subj = (subject or "").strip()
    if subj:
        out.append("\\vspace{-0.2em}")
        out.append(f"\\begin{{center}}{{\\Large\\bfseries {_latex_escape(subj)}}}"
                   "\\end{center}")
    if info_bar:
        # 姓名/班级/学号：\hrulefill 画作答横线，\hspace 分隔，整行两端对齐
        out.append("\\vspace{0.4em}")
        out.append("\\noindent 姓名\\hrulefill\\hspace{2em}班级\\hrulefill"
                   "\\hspace{2em}准考证号\\hrulefill\\par")
    en = (exam_notes or "").strip()
    if en:
        lines = [ln.strip() for ln in en.splitlines() if ln.strip()]
        out.append("\\vspace{0.4em}")
        # 注意事项装进黑框（\qnotebox，见 exam_template.tex），标题在框内、
        # 逐条自动编号成 enumerate ——此前是裸文本 + 手写「1. 」序号，
        # 长条目折行后第二行顶到行首、与序号对不齐。
        items = []
        for ln in lines:
            # 用户自带的「1. 」「1、」序号剥掉：编号交给 enumerate，否则会出现
            # 「1. 1. 答题前…」双重序号
            items.append(_latex_escape(_LEAD_NUM_RE.sub("", ln)))
        body = ["{\\bfseries 注意事项：}\\par\\vspace{0.2em}",
                "\\begin{enumerate}[label=\\arabic*., leftmargin=1.8em,"
                " itemsep=0pt, topsep=0pt, parsep=0.15em]"]
        body += [f"\\item {it}" for it in items]
        body.append("\\end{enumerate}")
        out.append(f"\\qnotebox{{\\small {' '.join(body)}}}")
    out.append("\\vspace{0.6em}\\hrule\\vspace{0.6em}")
    return _raw("\n".join(out))


def build_markdown(questions: list[dict], title: str, mode: str = "list",
                   keypoints: str = "", fullpage_ids=None,
                   solution_mode: str = "none", std_opts: dict = None,
                   bank_subject: str = "math",
                   *, layout=None, grouped=None, columns=None, ratio=None,
                   std_exam=False) -> str:
    """按模式拼装整份 Markdown（含 raw-LaTeX 分页指令）。

    分页交给 paginate()（与预览同源），此处只把页结构渲染成 Markdown。
    标题处理：
      - exam / exam_std 模式：标题作为正文顶部的居中大字（不用 \\maketitle），与题目同页；
        exam_std 还在标题前后加保密说明/卷首说明并收紧行距；
      - 其他模式：走 pandoc 的 `% title` → \\maketitle（可能独占首页，可接受）。
    solution_mode: none 不出解析 / inline 题后 / separate 解析另起页。
    layout/grouped/columns/ratio/std_exam: 四维自定义组合（见 paginate）；全不传
    时走原 mode 逻辑，行为与改动前逐字节一致。
    """
    spec = resolve_export_layout(layout=layout, grouped=grouped, columns=columns,
                                 ratio=ratio, std_exam=std_exam)
    pages = paginate(questions, mode=mode, keypoints=keypoints,
                     fullpage_ids=fullpage_ids, solution_mode=solution_mode,
                     std_opts=std_opts, bank_subject=bank_subject,
                     layout=layout, grouped=grouped, columns=columns,
                     ratio=ratio, std_exam=std_exam)
    # 文档族判定：自定义组合按 spec 派生值，legacy 按 mode。等价组合（含 std_exam
    # 提升的 exam_std）下两者一致，由 golden 基线与等价对拍测试钉住。
    slide_doc = spec.wide if spec is not None else mode == "slides"
    two_col_doc = spec.two_columns if spec is not None else mode == "practice"
    std_doc = spec.std_head if spec is not None else mode == "exam_std"
    exam_doc = ((spec.compat_mode == "exam") if spec is not None
                else mode == "exam")
    if slide_doc:
        pages = [[{"kind": "slide_cover", "text": title}]] + pages
    body = (_render_practice_pages(pages, solution_mode)
            if two_col_doc else _render_pages(pages, solution_mode))
    # 槽位基准高度模式（见 exam_template.tex 的 \ifqslotpagerel）：
    # 讲义/讲解/笔记按「每页 N 题」平分本页可用高度——首页有标题块也要放满 N 题；
    # 简单试卷的「半页」是半张纸的作答空间，必须按整个版心算，放不下就整题挪下一页。
    if body:
        if spec is not None:
            rel = "true" if spec.pagerel else "false"
        else:
            rel = "true" if mode in _SLOT_PAGEREL_MODES else "false"
        body = _slot_mode_latex(rel) + body

    if slide_doc:
        # 封面由 \qslidecover 渲染，不能再给 pandoc title，否则会多出一张默认标题页。
        parts = ["% ", "", body]
    elif two_col_doc:
        # 标题与页眉页脚横跨两栏；正文自己的 qpracticebegin/qpracticeend 只包题目区。
        # 模板全局已有 \pagestyle{fancy}，这里再显式钉住首页，避免将来标题实现改回
        # \maketitle 或引入其它 page style 时，双栏首页悄悄退回 plain 丢掉页眉页脚。
        head = _raw(
            f"\\thispagestyle{{fancy}}"
            f"\\begin{{center}}{{\\LARGE {_latex_escape(title)}}}"
            f"\\end{{center}}\\vspace{{0.3em}}"
        )
        parts = ["% ", "", head, body]
    elif std_doc:
        so = std_opts or {}
        head = _std_head_latex(title, so.get("secret_notice", ""),
                               so.get("exam_notes", ""),
                               subject=so.get("subject", ""),
                               info_bar=so.get("info_bar", True))
        parts = ["% ", "", head, body]
    elif exam_doc:
        # 不给 pandoc title（首行留空），自己在正文顶部加居中标题。
        # 这里是 raw LaTeX，标题必须自己转义（走 `% title` 的模式由 pandoc 负责）。
        head = _raw(
            f"\\begin{{center}}{{\\LARGE {_latex_escape(title)}}}"
            f"\\end{{center}}\\vspace{{0.6em}}"
        )
        parts = ["% ", "", head, body]
    else:
        parts = [f"% {title}", "", body]
    return "\n".join(parts)


# 页眉页脚 6 位置 → pandoc metadata 变量名
_HF_KEYS = {
    "header_left": "hf_hl", "header_center": "hf_hc", "header_right": "hf_hr",
    "footer_left": "hf_fl", "footer_center": "hf_fc", "footer_right": "hf_fr",
}


def _latex_escape(s: str) -> str:
    """转义用户文本里的 LaTeX 特殊字符（占位符替换在此之后做）。"""
    repl = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
            "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}"}
    return "".join(repl.get(c, c) for c in s)


def _resolve_hf(text: str, title: str) -> str:
    """把一个页眉页脚位置的文本转成 LaTeX。

    顺序：先把占位符换成不含特殊字符的哨兵 → 转义用户其余文本
    （哨兵不受影响）→ 哨兵再换成真正的 LaTeX 命令。
    这样既转义了用户文本，又不会破坏 \\thepage 等命令。
    """
    if not text or not text.strip():
        return ""
    SENT_PAGE = "\x00PAGE\x00"
    SENT_TOTAL = "\x00TOTAL\x00"
    SENT_TITLE = "\x00TITLE\x00"
    s = text.replace("{页码}", SENT_PAGE)
    s = s.replace("{总页数}", SENT_TOTAL)
    s = s.replace("{标题}", SENT_TITLE)
    s = _latex_escape(s)
    s = s.replace(SENT_PAGE, r"\thepage")
    s = s.replace(SENT_TOTAL, r"\pageref{LastPage}")
    s = s.replace(SENT_TITLE, _latex_escape(title))
    return s


def _hf_variable_args(header_footer: dict, title: str) -> list[str]:
    """把 6 位置页眉页脚转成 pandoc -V 变量参数（-V 不转义，可含 LaTeX 命令）。"""
    args = []
    hf = header_footer or {}
    any_header = False
    for form_key, var in _HF_KEYS.items():
        val = _resolve_hf(hf.get(form_key, ""), title)
        if val:
            args += ["-V", f"{var}={val}"]
            if form_key.startswith("header"):
                any_header = True
    if any_header:
        args += ["-V", "hf_rule=1"]
    return args


def _clean_output():
    """兼容清理旧版平铺在 output/ 根部的 quiz_* 文件。

    v0.9.0 起每次导出都使用独占子目录，不再在导出开始时调用本函数。保留它只为
    运维脚本或人工清理旧版残留；若在请求链路里清理共享根目录，并发导出会把另
    一个仍在运行的 Pandoc/XeLaTeX 输入删掉。
    """
    for f in config.OUTPUT_DIR.glob("quiz_*"):
        try:
            f.unlink()
        except OSError:
            pass  # 文件被占用（如正在预览）就跳过，不影响本次导出


# 题目正文里的图片引用。单机版用 Obsidian 嵌入语法 ![[文件名]]：图片扁平存在
# config.ASSETS_DIR 下，没有 scope 子目录，也没有 alt 文本。
#
# 服务器版这里是 ![alt](/qimages/<scope>/<file>)（走 Flask 静态路由）。改成双链是
# 为了让同一份 md 在 Obsidian 里能直接渲染出图——这是做插件的前提。
#
# 只有一个捕获组（文件名），比服务器版的四组少 —— 双链没有 alt，扁平存放也没有
# scope。下游 _rewrite/_stage_one 已按这个形状调整。
# `[^\]|]` 排掉 `|`：Obsidian 支持 ![[图片|300]] 指定显示宽度，宽度不属于文件名。
_QIMG_EXPORT_RE = re.compile(r"!\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")

# 用于把正文按 $$...$$ / $...$ 切成数学与非数学片段。块公式必须排在前面匹配：
# 若仍只认单美元，`$$\begin{array}...$$` 两端会各自被误认成一个空公式，中间整段
# 反而落进「普通文本」，随后 _escape_stray_backslash 把所有 LaTeX 命令双写，最终
# 在 array 的 `&` 处报 Misplaced alignment tab character（全库横版导出第 111 题）。
# 不处理嵌套美元；块公式允许跨行，行内公式仍限制在单行内。
_MATH_SPLIT_RE = re.compile(r"(\$\$.*?\$\$|\$[^$\n]*\$)", re.S)
_BLOCK_MATH_RE = re.compile(r"\$\$(.*?)\$\$", re.S)

# 题库正文来自多种 OCR/LLM，数学区里偶尔直接混入 Unicode 数学字符。XeLaTeX 的
# Latin Modern 数学字体不按 Unicode 字符取字形，只给 Missing character 警告并把
# 符号悄悄丢掉；统一转成等价 LaTeX 命令后才能稳定跨字体、跨机器显示。
_MATH_UNICODE_REPLACEMENTS = {
    "∈\u0338": r"\notin ",
    "∈": r"\in ",
    "∠": r"\angle ",
    "⊕": r"\oplus ",
    "⊙": r"\odot ",
    "⋅": r"\cdot ",
    "∩": r"\cap ",
    "∪": r"\cup ",
    "⊥": r"\perp ",
    "∵": r"\because ",
    "∴": r"\therefore ",
    "⩽": r"\leqslant ",
    "⩾": r"\geqslant ",
    "△": r"\triangle ",
    "α": r"\alpha ",
    "β": r"\beta ",
    "γ": r"\gamma ",
    "η": r"\eta ",
    "θ": r"\theta ",
    "λ": r"\lambda ",
    "ξ": r"\xi ",
    "π": r"\pi ",
    "Ⅰ": r"\mathrm{I}",
    "Ⅱ": r"\mathrm{II}",
    "Ⅲ": r"\mathrm{III}",
    "ⅰ": r"\mathrm{i}",
    "ⅱ": r"\mathrm{ii}",
    # \textcircled 是文本命令；数学区必须通过 \text 切回文本模式，
    # 否则 XeLaTeX 会报 "Command \\textcircled invalid in math mode"。
    "①": r"\text{\textcircled{1}}",
    "②": r"\text{\textcircled{2}}",
    "③": r"\text{\textcircled{3}}",
    "④": r"\text{\textcircled{4}}",
    "⑤": r"\text{\textcircled{5}}",
    "⑥": r"\text{\textcircled{6}}",
    "、": r"\text{、}",
    "．": ".",
    # OCR 偶尔把“左/中/右”直接塞进数学下标；Latin Modern 数学字体没有中文字形。
    # 这三个位置标签用国际通行的 L/M/R 表示，避免静默空白。
    "左": r"\mathrm{L}",
    "中": r"\mathrm{M}",
    "右": r"\mathrm{R}",
}
_UNICODE_PRIME_RE = re.compile(r"′+")
_MATH_TEXT_COMMAND_RE = re.compile(
    r"\\(?:text|textbf|textit|textup|textsl|textsc|textmd|"
    r"textrm|textnormal|textsf|texttt|emph|mbox|hbox)\s*\{"
)
# 换行、制表符以外的 ASCII 控制字符不承载题意，却会以 ^^A / ^^D 进入 TeX 字体并
# 形成不可见缺字；U+F8F3 是 MinerU/MathType 遗留的分段大括号私用字形，库中四处
# 都落在正常标点旁，单独没有语义且任何通用字体都没有，导出副本中安全剔除。
_EXPORT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BOLD_MATH_GREEK_RE = re.compile(
    r"\\mathbf\s*\{\s*\\(?P<name>Delta|Xi)\s*\}"
)
_ROMAN_MATH_GREEK_RE = re.compile(
    r"\\mathrm\s*\{\s*\\(?P<name>Omega)\s*\}"
)
_ORPHAN_NOT_SCRIPT_RE = re.compile(r"\^\s*\{\s*\\not\s*\}")
# OCR 常把填空横线写成 ``______`` 或 Markdown 转义后的 ``\_\_\_``。前者若
# 落在数学区会被 TeX 当成连续下标，后者在普通文本里又可能被 Pandoc 拆成反斜杠和
# 强调标记。只认连续三个及以上，避免碰到 ``a_1`` 这类正常下标。
_FILL_BLANK_RE = re.compile(r"(?:\\_\s*){2,}\\_|_{3,}")
_FILL_BLANK_TEX = r"\underline{\hspace{2cm}}"
# 行内 `$` 的边界归一（实现见 _normalize_inline_math_boundaries）：`f  $` / `$ f`
# 这类写歪的边界、以及闭 `$` 右侧紧跟数字，都会让 pandoc 不把这一对当数学，转而
# 输出 `\$\displaystyle A.\$60` 这种转义串，xelatex 随即报
# `! Missing $ inserted.` 令整卷导出失败。
_SOLUTION_LEADING_LABEL_RE = re.compile(
    r"\A\s*(?:#{1,6}\s*)?(?:【\s*解析\s*】|解析\s*[：:])\s*",
    re.I,
)
# 选择题「尾部文本」（选项区之后、见 split_choice_options）以这些词开头时视为
# 解析混进题干的污染数据，从渲染中撤下；「参考公式：…」这类正常附注不受影响
# （2026-10-04 审查：线上联考卷实测 tail 为 1686 字的「答案 BC…解法一」整段解析，
# 原样会印在选项下方）。
_TAIL_SOLUTION_LEAD_RE = re.compile(r"^\s*(?:【\s*)?(?:答案|解析|解法|评析)")


def _strip_solution_leading_label(text: str) -> str:
    """只剥解析字段开头的结构标签，不改“解析如下”等真实正文。"""
    return _SOLUTION_LEADING_LABEL_RE.sub("", str(text or ""), count=1)


def _normalize_inline_math_boundaries(text: str) -> str:
    r"""归一化行内 ``$...$`` 的边界，让 Pandoc 稳定认出这是数学区。

    Pandoc 认一对行内数学要同时满足：开 ``$`` 右侧紧贴非空白、闭 ``$`` 左侧紧贴
    非空白、闭 ``$`` 右侧不是数字。任一条不满足，这一对 ``$`` 会被当普通文本转义
    成 ``\$``，正文里就出现 ``\$\displaystyle A.\$60``，xelatex 接着报
    ``! Missing $ inserted.`` 让整卷导出失败（2026-10-04 实测：高考真题批量导出
    第 10033 题，2025 年北京卷第 14 题）。

    两类历史坑，都在这里一次解决：
    - ``$\displaystyle f  $`` 边界写歪 → 收掉公式区内部的收尾空白；
    - ``$\displaystyle A.$60`` / ``$\neq$0`` 闭 ``$`` 撞上数字 → 补一个空格。
      旧实现拿 ``(?<!\$)\$(?!\$)[ \t\n]+`` 对全文裸 ``$`` 下手，分不清开闭，
      把闭 ``$`` 后面的空格也一起删了，正是上面那个样例的根因。

    ``$$…$$`` 不受这两条约束（实测 ``$$x$$5`` 照常出 display 数学），空公式
    ``$ $`` 也不动；只在 ``_MATH_SPLIT_RE`` 认出的行内数学区上动手，不碰区外文本。
    """
    if "$" not in text:
        return text
    parts = _MATH_SPLIT_RE.split(text)
    for i in range(1, len(parts), 2):
        span = parts[i]
        if span.startswith("$$"):
            continue
        inner = span[1:-1].strip(" \t\r\n")
        parts[i] = f"${inner}$" if inner else span
        tail = parts[i + 1]
        # 注意 tail[:1] 可能是空串，而 '' in "0123456789" 为真（子串判定），
        # 故必须先判非空，否则每次都会给行尾凭空补一个空格。
        if tail[:1] and tail[0] in "0123456789":
            parts[i + 1] = " " + tail
    return "".join(parts)


def _sanitize_export_text(text: str) -> str:
    """清掉不可见 OCR 控制码及无语义私用括号碎片，不修改 vault 文件。"""
    if not text:
        return text
    cleaned = _EXPORT_CONTROL_RE.sub("", text).replace("\uf8f3", "")
    # MinerU 偶尔在块公式首行残留冒号。Pandoc 会把「单独一行 $$」当术语、下一行
    # 的 `:` 当定义列表标记，最终把整段公式转成 description 而非数学环境。
    # 冒号位于显示公式开头且后面直接是数学命令时不承载题意，可以安全剥除。
    cleaned = re.sub(r"(?m)(^\s*\$\$\s*\n)\s*:\s*(?=[\\A-Za-z])", r"\1", cleaned)
    # 唯一一处组合斜线在闭合的 `$a$` 后、等号前，语义就是“不等于”；直接写成
    # 新的行内数学式，避免 U+0338 落进普通文本字体而消失。
    # 闭合美元后留空格：Pandoc 要求行内数学的闭合 `$` 后不能紧跟数字，`$\neq$0`
    # 会被误判成普通文本；写成 `$\neq$ 0` 才会稳定生成 `\(\neq\) 0`。
    # 行内 `$` 边界归一（`$\displaystyle f  $` → `$\displaystyle f$`）：写歪的边界
    # 让该 `$` 落单、被转义成 `\$`，配对崩坏后整卷 xelatex 失败（2026-10-04 审查
    # 实测样例：希望联盟夏令营 2024-2 第 3 题）。
    cleaned = _normalize_inline_math_boundaries(cleaned)
    return cleaned.replace("$\u0338=", r"$ $\neq$ ")


def _normalize_fill_blank_markers(text: str) -> str:
    """把连续下划线占位符转成合法填空线，不修改题库原文。"""
    if not text or "_" not in text:
        return text
    parts = _MATH_SPLIT_RE.split(text)
    for i, part in enumerate(parts):
        replacement = (_FILL_BLANK_TEX if i % 2
                       else f"${_FILL_BLANK_TEX}$")
        parts[i] = _FILL_BLANK_RE.sub(lambda _match: replacement, part)
    return "".join(parts)


def _normalize_unicode_math_symbols(text: str) -> str:
    """把数学区中的 Unicode 符号转换为标准 LaTeX 命令。"""
    if not text:
        return text
    parts = _MATH_SPLIT_RE.split(text)

    def _replace_math_code(math: str) -> str:
        math = _UNICODE_PRIME_RE.sub(
            lambda m: "^{" + r"\prime" * len(m.group(0)) + "}", math)
        for char, latex in _MATH_UNICODE_REPLACEMENTS.items():
            math = math.replace(char, latex)
        return math.replace("\u0338", r"\not ")

    def _outside_text_commands(math: str) -> str:
        """只转换数学代码，保留 ``\text{...}`` 等文本模式参数。"""
        output: list[str] = []
        cursor = 0
        for match in _MATH_TEXT_COMMAND_RE.finditer(math):
            if match.start() < cursor:
                continue
            output.append(_replace_math_code(math[cursor:match.start()]))
            depth = 1
            end = match.end()
            while end < len(math) and depth:
                if math[end] == "\\" and end + 1 < len(math):
                    end += 2
                    continue
                if math[end] == "{":
                    depth += 1
                elif math[end] == "}":
                    depth -= 1
                end += 1
            output.append(math[match.start():end])
            cursor = end
        output.append(_replace_math_code(math[cursor:]))
        return "".join(output)

    for i in range(1, len(parts), 2):
        parts[i] = _outside_text_commands(parts[i])
    return "".join(parts)


def _normalize_unicode_text_symbols(text: str) -> str:
    """把普通文本区的数学符号改成行内 LaTeX，避免落进罗马文本字体缺字。"""
    if not text:
        return text
    replacements = {
        "⩽": r" $\leqslant$ ", "⩾": r" $\geqslant$ ",
        "⋅": r" $\cdot$ ", "∩": r" $\cap$ ", "∪": r" $\cup$ ",
        "⊥": r" $\perp$ ", "∵": r" $\because$ ", "∴": r" $\therefore$ ",
        "λ": r" $\lambda$ ",
        # 用行内数学保护导出器生成的反斜杠，\text 再切回文本模式执行 \textcircled。
        # 否则后续孤立反斜杠清理会把它当外来命令转义，PDF 显示字面量命令源码。
        "①": r" $\text{\textcircled{1}}$ ", "②": r" $\text{\textcircled{2}}$ ",
        "③": r" $\text{\textcircled{3}}$ ", "④": r" $\text{\textcircled{4}}$ ",
        "⑤": r" $\text{\textcircled{5}}$ ", "⑥": r" $\text{\textcircled{6}}$ ",
    }
    parts = _MATH_SPLIT_RE.split(text)
    for i in range(0, len(parts), 2):
        for char, latex in replacements.items():
            parts[i] = parts[i].replace(char, latex)
    return "".join(parts)


def _repair_invalid_math_font_wrappers(text: str) -> str:
    r"""修复会把数学符号送进错误字体的 OCR TeX 包裹。

    `\mathbf{\Delta}` / `\mathbf{\Xi}` 与 `\mathrm{\Omega}` 会尝试从罗马字体取
    数学符号，结果只报缺字并留空；前两者改用 amsmath 的 `\boldsymbol`，后者直接
    恢复为 `\Omega`。
    `^{\not}` 中的 \not 没有被修饰关系符，只会索取一个不存在的字体槽位，且这个
    上标本身不承载可见内容，因此从导出副本移除。数学区里的 `\scriptsize` 同样是
    无效字号命令（LaTeX 会警告并忽略），删除后保留其后的实际符号。
    """
    if not text:
        return text
    repaired = _BOLD_MATH_GREEK_RE.sub(
        lambda m: rf"\boldsymbol{{\{m.group('name')}}}", text)
    repaired = _ROMAN_MATH_GREEK_RE.sub(
        lambda m: rf"\{m.group('name')}", repaired)
    repaired = _ORPHAN_NOT_SCRIPT_RE.sub("", repaired)
    return repaired.replace(r"\scriptsize", "")


def _repair_nested_dollar_math(text: str) -> str:
    r"""去掉含内层行内公式的无效块公式外壳，保留内部内容与行内公式。

    OCR 偶尔会输出 `$$ $x$ $O$ $y$ $$`。TeX 不允许在 display math 中再次开启
    `$...$`，Pandoc 会生成 `\[ $x$ ... \]` 并报 Display math should end with $$。
    这种块的实际内容已经由内层公式逐段标明，所以只移除最外层 `$$` 即可；正常的
    `$$ (0, \\frac{1}{2}) $$` 不含内层美元，保持原样。
    """
    if not text or "$$" not in text:
        return text

    def _unwrap(match: re.Match) -> str:
        inner = match.group(1)
        return inner if "$" in inner else match.group(0)

    return _BLOCK_MATH_RE.sub(_unwrap, text)

# 同一个 TeX 原子不能连续挂两个同类脚标，`x^{2}^{,,}` 会让 XeLaTeX 直接报
# Double superscript。识别结果里偶尔会出现这种 OCR 痕迹；在两个脚标之间插一个
# 空原子变成 `x^{2}{}^{,,}`，既保留全部原始内容，也让第二个脚标有合法归属。
# 参数允许一层嵌套花括号，覆盖导入结果常见的 `^{\frac{a}{b}}` 形态。
_DUPLICATE_SCRIPT_RE = re.compile(
    r"(?P<first>(?P<mark>[_^])\s*\{(?:[^{}]|\{[^{}]*\})*\})"
    r"(?P<gap>\s*)(?P=mark)(?=\s*\{)"
)
# `\frac` / `\dfrac` / `\tfrac` 必须有分子、分母两个参数。OCR 有时只留下第一个，
# 如 `\dfrac{\mathrm{H}}$`；只在它紧邻数学区结束符时补空的第二参数，避免误改合法的
# `\frac{1}2` 这类 TeX 简写。空参数不增加内容，只让 XeLaTeX 能完整排出其余原文。
_MISSING_FRAC_ARG_RE = re.compile(
    r"(?P<frac>\\[dt]?frac\s*\{(?:[^{}]|\{[^{}]*\})*\})(?=\s*\$)"
)
# OCR 偶尔只保留 `\right` 命令却吃掉它后面的定界符，例如
# `$$\left|PF_1\right$$`。TeX 会因此在这一题直接中断整份导出；补不可见定界符
# `.` 不凭空添加数学内容，只让已识别到的左定界符可以正常闭合。
_MISSING_RIGHT_DELIM_RE = re.compile(r"\\right(?=\s*\$)")
# `\frac`/`\dfrac`/`\tfrac` 的无花括号参数只吃**一个记号**。紧跟其后若是
# `\mathrm` 这类要吃参数的字体命令，TeX 会把命令本身当成第二个参数，剩下
# `{e}` 逃进数学列表 → `Argument of \math@egroup has an extra }` 之后连环报错
# （题库实测 2 道：2018 全国 I 卷（文）第 21 题、2019 天津卷（文）第 20 题，
# 源数据写成 `\dfrac1\mathrm{e}`）。补全花括号不改变任何字符的可见含义，只是
# 让它成为 TeX 认得的 `\dfrac{1}{\mathrm{e}}`。
_BARE_FRAC_FONT_RE = re.compile(
    r"\\(?P<kind>[dtc]?frac)\s*(?P<num>\d|[A-Za-z])"
    r"\\(?P<font>mathrm|mathbf|mathit|mathsf|mathtt|mathcal|mathfrak|mathbb|boldsymbol)"
    r"\s*(?P<arg>\{[^{}]*\})"
)


def _repair_duplicate_math_scripts(text: str) -> str:
    """只在数学区修复连续同类脚标；不改 vault 原文，不丢任何字符。"""
    if not text or ("^" not in text and "_" not in text):
        return text
    parts = _MATH_SPLIT_RE.split(text)
    for i in range(1, len(parts), 2):
        previous = None
        while previous != parts[i]:
            previous = parts[i]
            parts[i] = _DUPLICATE_SCRIPT_RE.sub(
                lambda m: f"{m.group('first')}{m.group('gap')}{{}}{m.group('mark')}",
                parts[i],
            )
    return "".join(parts)


def _repair_incomplete_math_commands(text: str) -> str:
    """补齐数学区末尾可安全确定的缺失参数／定界符，不改识别内容。"""
    if not text:
        return text
    if "frac" in text:
        text = _MISSING_FRAC_ARG_RE.sub(r"\g<frac>{}", text)
        text = _BARE_FRAC_FONT_RE.sub(
            lambda m: "\\%s{%s}{\\%s%s}" % (m.group("kind"), m.group("num"),
                                            m.group("font"), m.group("arg")),
            text,
        )
    if "\\right" in text:
        text = _MISSING_RIGHT_DELIM_RE.sub(r"\\right.", text)
    return text


# `array` 列格式里有两类会硬停 XeLaTeX 的写法，都是 2026-10-05「历年预赛题」
# 导出实测暴露的（预览全程正常，只有 PDF 导出炸——网页 KaTeX 与 Obsidian 的
# MathJax 都比 LaTeX 宽容，看清这个差异，这是第二次因此类假象吃亏）：
# ① `:` 是 arydshln 宏包的「虚线竖线」语法，内置模板没有加载该宏包，xelatex
#    报 `Illegal pream-token (:)` 当场停住（2024 吉林第 9 题，编译在第 29 页
#    硬停、整份 PDF 报废）；
# ② preamble 列数少于表体单元格数（2021 四川第 8 题：`{c:c}` 2 列配 3×3
#    九宫格），-halt-on-error 下报 `Extra alignment tab` 同样硬停。
# 修法一律机械化、只改本次导出的内存副本：顶层 `:` 换成 `|`（虚线画成实线），
# 列数不足时在末尾补 `c`（给多出的单元格合法位置，不动任何可见内容）。
# 不给模板加载 arydshln——它与 longtable/colortbl 的兼容性是出名的坑。
# `@{...}` 参数组内的字符与嵌套 `*{n}{c}` 形式一律不碰（后者整段不匹配、跳过）。
_ARRAY_ENV_RE = re.compile(
    r"(\\begin\{array\}(?:\s*\[[^\]]*\])?\s*\{)([^{}]*)(\})(.*?)(\\end\{array\})",
    re.S,
)


def _fix_array_preamble(preamble: str, needed_cols: int = 0) -> str:
    """列格式顶层的 `:` 换 `|`；列数不足时末尾补 `c`。"""
    depth = 0
    cols = 0
    chars: list[str] = []
    for ch in preamble:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            if ch == ":":
                ch = "|"
            if ch in "clr":
                cols += 1
        chars.append(ch)
    if needed_cols > cols:
        chars.append("c" * (needed_cols - cols))
    return "".join(chars)


def _array_body_cols(body: str) -> int:
    """数 array 表体单行最多的顶层 `&` 数 +1；嵌套花括号里的 `&` 不计。

    `\\text{a&b}` 里的 `&` 是文本内容而不是列分隔，必须靠花括号深度排除，
    否则会把列数数多、给正常表格补出多余的列。
    """
    depth = 0
    best = 0
    current = 0
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "\\":
            if i + 1 < n and body[i + 1] == "\\":  # 行结束符：结算本行
                best = max(best, current)
                current = 0
                i += 2
                continue
            i += 2  # 跳过命令首字符（\hline / \text / \times …）
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "&" and depth == 0:
            current += 1
        i += 1
    best = max(best, current)
    return best + 1 if best else 0


def _repair_array_preambles(text: str) -> str:
    """只在数学区修复 array 列格式（`:`→`|`、列数不足补 `c`）；不改 vault 原文。"""
    if not text or "\\begin{array}" not in text:
        return text

    def _sub(m):
        body = m.group(4)
        return (m.group(1)
                + _fix_array_preamble(m.group(2), _array_body_cols(body))
                + m.group(3) + body + m.group(5))

    parts = _MATH_SPLIT_RE.split(text)
    for i in range(1, len(parts), 2):
        if "\\begin{array}" in parts[i]:
            parts[i] = _ARRAY_ENV_RE.sub(_sub, parts[i])
    return "".join(parts)


def _escape_stray_backslash(text: str) -> str:
    """把数学区（$...$ / $$...$$）之外孤立的反斜杠转成 pandoc 安全转义 \\\\。

    题目正文里偶尔会出现「甲\\乙」这类对角线表头写法，反斜杠本意只是普通符号。
    但 pandoc 判断「反斜杠+字符」是否算「用户手写的原始 LaTeX 命令」的规则很不
    稳定（比如 \\浇、\\alpha、\\bc 会被原样透传给 xelatex，导致
    "Undefined control sequence" 编译报错；而 \\def 等少数几个会被转义），
    双反斜杠 \\\\ 则总会被 pandoc 安全转成字面反斜杠字符，故统一改写。
    数学区内的反斜杠是真正的 LaTeX 命令（如 \\displaystyle、\\dfrac），必须原样保留，
    故按行内/块数学区切段，只处理非数学段。
    """
    if not text or "\\" not in text:
        return text
    parts = _MATH_SPLIT_RE.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 0:  # 偶数下标：非数学段；奇数下标是数学式，原样保留
            parts[i] = part.replace("\\", "\\\\")
    return "".join(parts)


def _merge_inline_math_newlines(text: str) -> str:
    """把行内公式（``$...$``）内部的换行合并为空格；``$$...$$`` 不动。

    为什么需要：``_MATH_SPLIT_RE`` 的行内数学约定「限制在单行内」（见其注
    释），而题库里有从上游 LaTeX 折行带进来的跨行行内公式（实测 2025 北京
    卷第 20 题解答的 ``$\\frac{2a-x_1-x_2}{x_2-x_1}\\n=\\frac{...}$``）。跨
    行的 ``$...$`` 认不出 → 一切「按数学区切段」的环节都把它当普通文本 →
    ``_escape_stray_backslash`` 把公式里的反斜杠全部双写 → PDF 里整段公式
    碎成「in / left( / frac1mathrm e」逐行文字（2026-10-04 真实导出故障）。
    在预处理链最前面把行内公式规范化为单行，下游全部既有假设（含 pandoc）
    即可自然成立。

    配对规则与 ``_MATH_SPLIT_RE`` 一致：``\\$`` 不算定界；``$$`` 成对跳过
    （块公式跨行合法）；未闭合的落单 ``$`` 之后不处理（保守，不火上浇油）。
    """
    if not text or "$" not in text:
        return text
    n = len(text)
    spans: list[tuple[int, int]] = []
    i = 0
    open_at = None
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "$":
            if text.startswith("$$", i):
                j = text.find("$$", i + 2)
                i = n if j < 0 else j + 2
                continue
            if open_at is None:
                open_at = i
            else:
                spans.append((open_at, i))
                open_at = None
        i += 1
    if not spans:
        return text
    out: list[str] = []
    last = 0
    for a, b in spans:
        out.append(text[last:a])
        inner = text[a:b + 1]
        inner = inner.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        out.append(inner)
        last = b + 1
    out.append(text[last:])
    return "".join(out)


# ---------------------------------------------------------------------------
# 表格：MinerU 输出的内联 HTML <table> → raw LaTeX tabular
# ---------------------------------------------------------------------------
#
# MinerU v4（vlm）识别到表格后输出的是**压在一行里的内联 HTML**：无 <html>/<body>
# 包裹、无 <th>（表头也是 <td>）、整表不含换行，形如
#   <table><tr><td>地区</td><td>平均分</td></tr><tr><td>河南</td><td>3.59</td></tr></table>
# pandoc 的 latex writer 不认识内联 raw HTML：它把每个单元格当成独立的松散段落吐
# 出去，PDF 里就成了「地区 / 平均分 / 河南 / 3.59」几行游离文字（已实测复现）。
#
# 为什么转成 raw LaTeX tabular，而不是转成 markdown 管道表格：
#   管道表格会被 pandoc 写成 longtable，而 longtable 必须处在「外层竖直模式」，
#   塞不进 minipage/\vbox —— 本项目 note/handout/exam 的半页块、以及图文分栏两栏
#   全是 minipage，一放进去就编译报错。tabular 没有这个限制，任何位置都能用，
#   且单元格里的 $...$ 数学式原样交给 xelatex，不经 pandoc 二次处理。
#
# 为什么先换成 base64 令牌、直到渲染末尾才展开成 LaTeX：
#   题干后面还要过 _format_options（按 A./B. 标签切段）、_break_subquestions 等
#   一批「按行扫描」的正则。若此刻正文里已经是 tabular，一行内同时出现
#   $\displaystyle A.$ 和 $\displaystyle B.$（拿表格排四个选项的题，MinerU 确实
#   会这么出）就会被切成多段、把 tabular 拆坏。base64 令牌只含 A-Za-z0-9-_=：
#   既没有 `.`（裸标签正则要求 [A-D] 后紧跟点号）、也没有 `$` 和 `（`，上述正则
#   一个都咬不到它，最后在 _render_block 统一展开。
_TABLE_RE = export_tables.TABLE_RE
_PIPE_SEP_RE = export_tables.PIPE_SEP_RE
_cell_text = export_tables.cell_text
_html_table_rows = export_tables.html_table_rows
_pipe_text_cells = export_tables.pipe_text_cells

# 非数学区里的 TeX 特殊字符 → 安全写法。逐字符一次性映射（不做链式 replace），
# 否则先换的 \textbackslash{} 里的花括号会被后一条规则再转义一遍。
_TEX_SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def _tex_text(seg: str) -> str:
    """转义一段**非数学**纯文本里的 TeX 特殊字符（见 _TEX_SPECIALS）。"""
    return "".join(_TEX_SPECIALS.get(ch, ch) for ch in seg)


def _cell_tex_text(text: str) -> str:
    """已经清洗的单元格纯文本 → 可直接进 tabular 的 LaTeX。"""
    parts = _MATH_SPLIT_RE.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 0:            # 偶数下标=非数学段；奇数下标是 $...$，原样保留
            parts[i] = _tex_text(part)
    return "".join(parts)


def _cell_tex(raw: str) -> str:
    """单元格 HTML → 可直接进 tabular 的 LaTeX 文本。

    先走 `_cell_text` 还原实体并剥标签，再按 $...$ 切段保护数学式；页面预览
    直接复用前一步的纯文本，因此两端认到的行列与可见内容不会漂移。
    """
    return _cell_tex_text(_cell_text(raw))


# 表格列宽阈值：列数 <= 此值用自然宽 l 列（内容多宽就多宽，紧凑）；超过则改用
# 等分的 p{} 列并允许折行，否则宽表会横向溢出版心（xelatex 只报 Overfull \hbox
# 警告、不报错，PDF 里表格直接伸出纸外，很难发现）。
_TABLE_NARROW_COLS = 4


def _table_tex(inner: str) -> str | None:
    """一张 <table> 的内部 HTML → raw LaTeX tabular（booktabs 三线）。

    首行当表头（MinerU 不出 <th>，但首行事实上就是表头）：加粗、下方 \\midrule。
    只有一行时不画 \\midrule（没有表体，画了会多出一条孤线）。
    列数取各行单元格数的最大值（含 colspan 累加），短行右侧补空格 & 对齐 ——
    MinerU 偶尔会漏掉尾部空单元格，不补齐会报 "extra alignment tab"。
    colspan 用 \\multicolumn 还原；rowspan 不还原（LaTeX 需 multirow 宏包，
    模板未引入；跨行单元格退化成只在首行显示，内容不丢）。
    识别不到任何行/单元格时返回 None，交由调用方保留原文（不静默吞内容）。
    """
    rows = [[(_cell_tex_text(text), span) for text, span in row]
            for row in _html_table_rows(inner)]
    return _rows_to_tex(rows)


def _rows_to_tex(rows: list[list[tuple[str, int]]]) -> str | None:
    """已解析的表格行（每格 (LaTeX 文本, colspan)）→ raw LaTeX tabular。
    HTML 表格与 markdown 管道表格共用此渲染，保证两种来源版式完全一致。"""
    if not rows:
        return None
    ncol = max(sum(span for _t, span in r) for r in rows)
    if ncol < 1:
        return None

    if ncol <= _TABLE_NARROW_COLS:
        colspec = "l" * ncol
    else:
        # 宽表按列数等分 \linewidth 并允许折行。每列实际占位 = p{} 宽 + 2\tabcolsep，
        # 故必须先把 N 列的列间距从总宽里扣掉再等分，否则表格宽 = \linewidth +
        # 2N\tabcolsep 必然溢出版心（7 列时溢出约 1cm，实测已复现）。
        colspec = (r"p{\dimexpr(\linewidth-%d\tabcolsep)/%d\relax}" % (2 * ncol, ncol)) * ncol

    def _row_tex(cells: list[tuple[str, int]], bold: bool = False) -> str:
        out = []
        for text, span in cells:
            body = f"\\textbf{{{text}}}" if bold and text else text
            out.append(f"\\multicolumn{{{span}}}{{l}}{{{body}}}" if span > 1 else body)
        pad = ncol - sum(span for _t, span in cells)
        out.extend([""] * max(0, pad))
        # booktabs 的 \midrule 和上一行的 `\\` 都接受紧随其后的 `[长度]` 可选参数。
        # 若首格正好是区间 `[25,35)`，TeX 会把整行吞成参数并报 Runaway argument。
        # `\relax` 只终止可选参数探测，不产生可见内容。
        if out and out[0].lstrip().startswith("["):
            out[0] = r"\relax " + out[0]
        return " & ".join(out) + r" \\"

    lines = [f"\\begin{{tabular}}{{@{{}}{colspec}@{{}}}}", r"\toprule",
             _row_tex(rows[0], bold=True)]
    if len(rows) > 1:
        lines.append(r"\midrule")
        lines.extend(_row_tex(r) for r in rows[1:])
    lines += [r"\bottomrule", r"\end{tabular}"]
    # 各行必须用换行拼接、不能直接首尾相连：ctex 下汉字是「字母」类，
    # `\midrule河南理` 会被 TeX 读成一个控制序列 → Undefined control sequence。
    # 换行同时也让产出的 .tex 可读（调试导出问题时要人眼看这段）。
    body = "\n".join(lines)
    # 整表居中独占一段：前后 \par 保证不与正文挤在同一行（表格在题干中间时常见）
    return ("\\par\\nobreak\\vspace{0.3em}\\noindent\\begin{center}\n"
            + body + "\n\\end{center}\\vspace{0.3em}\\par\\noindent ")


# 表格令牌：QFIGTABLE<base64 的 LaTeX>QFIGTABLEEND。base64 用 urlsafe 变体
# （A-Za-z0-9-_=），不含 `.` `$` `（` `\` —— 故 _format_options 的选项标签正则、
# _break_subquestions 的小问正则、_escape_stray_backslash 全都咬不到它。
# 展开在 _render_block 末尾（_expand_tables），与 _fill_caption 同一阶段。
_TABLE_TOKEN_RE = re.compile(r"QFIGTABLE([A-Za-z0-9\-_=]+)QFIGTABLEEND")


def _token(tex: str) -> str:
    """一段表格 LaTeX → 独立成段的 base64 令牌。"""
    b64 = base64.urlsafe_b64encode(tex.encode("utf-8")).decode("ascii")
    return f"\n\nQFIGTABLE{b64}QFIGTABLEEND\n\n"


# markdown 管道表格：连续 >=2 行、每行以 | 开头结尾，其中第二行是 |---|:--:| 分隔行。
# pandoc 本身认这种表，但会写成 longtable —— longtable 要求处在外层竖直模式，塞进
# minipage/\vbox（note/handout/exam 半页块、图文分栏两栏）会编译报错。故这里也接管，
# 与 HTML 表格走同一套 tabular 渲染。
def _pipe_cells(line: str) -> list[tuple[str, int]]:
    """一行管道表格 → 单元格列表。去掉首尾竖线后按 | 切，逐格转 LaTeX。
    管道表格无 colspan 概念，span 恒为 1。"""
    return [(_cell_tex_text(text), span)
            for text, span in _pipe_text_cells(line)]


def _stash_pipe_tables(text: str) -> str:
    """把 markdown 管道表格换成 base64 令牌（理由见 _PIPE_SEP_RE 注释）。

    按行扫描：找到「表头行 + 分隔行」就往下连收所有以 | 起头的行作为表体。
    不含分隔行的孤立 | 行不动（可能只是正文里的绝对值/集合竖线）。
    """
    if not text or "|" not in text:
        return text
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        head = lines[i].strip()
        if (head.startswith("|") and i + 1 < len(lines)
                and _PIPE_SEP_RE.match(lines[i + 1])):
            rows = [_pipe_cells(lines[i])]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(_pipe_cells(lines[j]))
                j += 1
            tex = _rows_to_tex(rows)
            if tex is not None:
                out.append(_token(tex))
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _stash_tables(text: str) -> str:
    """把正文里的表格（内联 HTML <table> 与 markdown 管道表格）换成 base64 令牌
    （令牌形态见 _TABLE_TOKEN_RE 注释）。

    转不出表格结构（_table_tex 返回 None）时保留原文不动 —— 宁可让那段退化成
    pandoc 的松散段落（旧版行为），也不静默丢掉题目内容。
    """
    if not text:
        return text

    if "<table" in text.lower():
        def _sub(m):
            tex = _table_tex(m.group(1))
            return m.group(0) if tex is None else _token(tex)
        text = _TABLE_RE.sub(_sub, text)
    return _stash_pipe_tables(text)


def _expand_tables(md: str) -> str:
    """把表格令牌展开成 pandoc 可透传的 raw-latex 围栏块（渲染最后一步）。"""
    def _sub(m):
        tex = base64.urlsafe_b64decode(m.group(1).encode("ascii")).decode("utf-8")
        return _raw(tex)
    return _TABLE_TOKEN_RE.sub(_sub, md)


def _stage_images(questions: list[dict], stem: str, work_dir: Path) -> list[dict]:
    """把题目/解析里 ![[文件名]] 引用的图拷进本次工作目录供 xelatex 用。

    xelatex 在 work_dir 内跑，\\includegraphics 以该目录为基准找图。故：
      1. 从 config.IMAGES_DIR/<file> 把图拷成 work_dir/quiz_<stamp>_img_<n><ext>；
      2. 把 body/solution 里的 ![[...]] 改写为 ![](quiz_..._img_n.ext)
         （相对文件名，pandoc 转成 \\includegraphics{...}，xelatex 在 cwd 找到）。
    图缺失则去掉该图引用（不让 xelatex 因找不到文件报错中断整份导出）。
    返回改写后的题目 dict 列表（浅拷贝，不改原对象）。
    """
    import shutil

    cache: dict[str, str] = {}   # 原始 web 路径 -> 本地文件名（同图复用，去重拷贝）
    counter = [0]

    def _stage_one(fname: str) -> str | None:
        if fname in cache:
            return cache[fname]
        # 单机版图片扁平存在 ASSETS_DIR 下，没有 scope 子目录（服务器版是
        # IMAGES_DIR/<scope>/<file>）。文件名里的 <id>_N 前缀已经保证了唯一性。
        src = _resolve_image_source(fname)
        if src is None:
            return None
        # AI 重绘的 TikZ 配图在正文里存的是 .svg（页面 <img> 用那份），但
        # graphicx + xelatex **不认 svg**，认 PDF。tikz_render 编译时同时落了
        # 同名 .pdf，这里换过去 —— 换不到就退回原文件让下面的 is_file 判缺失。
        # 不做这一步的话导出会静默掉图（_rewrite 对 None 返回 ""）。
        if fname.lower().endswith(".svg"):
            pdf_name = str(Path(fname.replace("\\", "/")).with_suffix(".pdf"))
            pdf_src = _resolve_image_source(pdf_name)
            if pdf_src is None:
                return None   # 无配套 PDF：按图缺失处理，不让 xelatex 报错中断
            src = pdf_src
            fname = pdf_name
        ext = Path(fname).suffix or ".png"
        local = f"{stem}_img_{counter[0]}{ext}"
        counter[0] += 1
        try:
            shutil.copy2(src, work_dir / local)
        except OSError:
            return None
        cache[fname] = local
        return local

    def _rewrite(text: str) -> tuple[str, list[str]]:
        """把 _assets 里的图拷进 OUTPUT_DIR，并把每个图引用**原位**换成 QFIGSLOT<n>
        哨兵，返回 (带哨兵的正文, [本地文件名...])。

        原位留哨兵而不是像旧版那样抽到题末：抽走后「图在题干中间」的信息就丢了，
        图只能排在题末、原位留下一段空白。留着哨兵，plan_figs 才能判出每张图该
        原位排 / 排题末 / 配到某个选项上（见 _SLOT_SENT）。

        最终排版（题后靠右下 / 左文右图 / 自定义对齐宽度 / 图文分栏 / 四图配选项）
        仍由 _q_md 按题型和题卡设置决定。
        """
        if not text:
            return text, []

        figs: list[str] = []

        def _sub(m):
            local = _stage_one(m.group(1))
            if local is None:
                return ""   # 图缺失：删掉引用，避免编译中断
            figs.append(local)
            return f"{_SLOT_SENT}{len(figs) - 1}"   # 原位留哨兵

        return _QIMG_EXPORT_RE.sub(_sub, text).rstrip(), figs

    def _prep(text: str) -> tuple[str, list[str]]:
        """staging 阶段的正文预处理，顺序不能反：

        0. _merge_inline_math_newlines 把行内公式内部的换行合并为空格（跨行
           行内公式会让 _MATH_SPLIT_RE 认不出它，随后 _escape_stray_backslash
           把公式里的反斜杠全部双写，PDF 里公式碎成「in / left( / frac」逐行
           文字；2026-10-04 真实导出故障）；
        1. _sanitize_export_text 清掉不可见控制码与无语义的私用区括号碎片；
        2. _normalize_fill_blank_markers 把连续下划线转成合法的 TeX 填空线；
        3. _repair_nested_dollar_math 去掉「块公式内又嵌行内公式」的无效外壳；
        4. _normalize_unicode_math_symbols 把数学区 Unicode 符号转为标准 LaTeX；
        5. _normalize_unicode_text_symbols 把文本区字体不含的斜等号包成行内数学；
        6. _repair_invalid_math_font_wrappers 修复会吞掉 Δ/Ξ 的错误粗体包裹与孤立 not；
        7. _repair_duplicate_math_scripts 修复数学区的连续同类脚标；
        8. _repair_incomplete_math_commands 给数学区末尾缺分母的 frac 补空参数；
        9. _repair_array_preambles 修 array 列格式：`:`（arydshln 虚线语法）
           换 `|`，列数少于表体单元格数时末尾补 `c`（两种写法都会让 xelatex
           以 Illegal pream-token / Extra alignment tab 硬停）；
           以上几步都只改本次导出的内存副本，不改题库文件，也不丢 OCR 识别出的内容；
        10. _normalize_inline_math_boundaries 复核行内 `$` 边界 —— 上面几步（填空线
           转成行内公式、文本区符号包成行内数学）都可能新造出「闭 `$`
           紧跟数字」的非法边界，这里统一收口，保证交给 pandoc 的每一对行内数学
           都满足它的三条边界要求；
        11. _stash_tables 把内联 HTML <table> 换成 base64 令牌 —— 必须在
           _escape_stray_backslash 之前，否则表格里的反斜杠会先被双写成字面反斜杠，
           而表格单元格的转义由 _cell_tex/_tex_text 自己负责（两套转义会打架）；
           令牌本身只含 A-Za-z0-9-_=，不含反斜杠，后一步碰不到它。
        12. _escape_stray_backslash 处理正文里剩下的孤立反斜杠。
        13. _rewrite 原位留 QFIGSLOT 哨兵，图片文件名单独返回。
        """
        repaired = _merge_inline_math_newlines(text)
        repaired = _sanitize_export_text(repaired)
        repaired = _normalize_fill_blank_markers(repaired)
        repaired = _repair_nested_dollar_math(repaired)
        repaired = _normalize_unicode_math_symbols(repaired)
        repaired = _normalize_unicode_text_symbols(repaired)
        repaired = _repair_invalid_math_font_wrappers(repaired)
        repaired = _repair_duplicate_math_scripts(repaired)
        repaired = _repair_incomplete_math_commands(repaired)
        repaired = _repair_array_preambles(repaired)
        repaired = _normalize_inline_math_boundaries(repaired)
        return _rewrite(_escape_stray_backslash(_stash_tables(repaired)))

    staged = []
    for q in questions:
        nq = dict(q)
        # 图片文件名不再嵌在正文里（旧版是末尾的 \qfigmark 块），改成随题带一个
        # 列表，下标即哨兵编号 —— _render_block/_q_md 用 _IMG_FILES_KEY 取。
        nq["body"], files = _prep(q.get("body", ""))
        nq[_IMG_FILES_KEY] = files
        if q.get("solution"):
            nq["solution"], sol_files = _prep(q["solution"])
            nq[_SOL_IMG_FILES_KEY] = sol_files
        staged.append(nq)
    return staged


def _resolve_template_path(raw: str | Path | None, *, mode: str | None = None) -> Path:
    """校验导出模板路径，只允许内置资源或 Agent 模板目录中的普通文件。"""
    custom = raw is not None and str(raw).strip() != ""
    source = Path(raw).expanduser() if custom else Path(config.TEX_TEMPLATE)
    try:
        if source.is_symlink():
            raise ExportError("导出模板不能是符号链接")
    except OSError as exc:
        raise ExportError("导出模板不存在或无法读取") from exc
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExportError("导出模板不存在或无法读取") from exc
    if not resolved.is_file() or resolved.suffix.lower() != ".tex":
        raise ExportError("导出模板必须是普通 .tex 文件")

    try:
        builtin = Path(config.TEX_TEMPLATE).expanduser().resolve(strict=True)
        agent_root = Path(config.AGENT_TEMPLATES_DIR).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ExportError("导出模板目录无法解析") from exc
    if not custom:
        if resolved != builtin:
            raise ExportError("内置导出模板路径不一致")
        return resolved
    if resolved == builtin:
        return resolved
    if agent_root not in resolved.parents:
        raise ExportError("自定义导出模板必须位于 QuizForge 模板目录内")
    if custom:
        try:
            package_root = template_pipeline.package_root(resolved)
            info = template_pipeline.inspect_directory(package_root)
            if Path(info["entrypoint_path"]) != resolved:
                raise template_pipeline.TemplatePipelineError(
                    "导出路径不是模板清单声明的入口文件",
                    code="invalid_entrypoint")
            if mode:
                template_pipeline.ensure_mode(info, mode)
            # Agent 模板目录中的文件必须继续绑定目录元数据里的验证哈希。路径
            # 解析与真正导出之间即使有人手工改了文件，也不能编译未经确认的版本。
            if package_root.parent != agent_root:
                raise template_pipeline.TemplatePipelineError(
                    "自定义模板包必须是模板目录的直属条目",
                    code="template_not_registered", status=409)
            import agent_catalog
            try:
                row = agent_catalog.get_template(package_root.name)
            except agent_catalog.CatalogError as exc:
                raise template_pipeline.TemplatePipelineError(
                    "模板目录没有对应的目录记录",
                    code="template_not_registered", status=409) from exc
            if (not row.get("enabled") or row.get("status") != "enabled"
                    or row.get("validation_hash") != info["source_hash"]):
                raise template_pipeline.TemplatePipelineError(
                    "模板验证哈希已失效，需要重新验证并启用",
                    code="template_stale", status=409)
        except template_pipeline.TemplatePipelineError as exc:
            raise ExportError(str(exc)) from exc
    return resolved


def _stage_template_resources(source: Path, work_dir: Path) -> Path:
    """把用户模板及同目录资源复制到隔离工作区，保持相对引用可用。"""
    try:
        root = template_pipeline.package_root(source)
        info = template_pipeline.inspect_directory(root)
        target = template_pipeline.copy_package(root, work_dir)
    except (OSError, RuntimeError, template_pipeline.TemplatePipelineError) as exc:
        raise ExportError(f"导出模板资源复制失败：{exc}") from exc
    if Path(info["entrypoint_path"]) != source.resolve(strict=True):
        raise ExportError("导出模板入口与清单不一致")
    return target


def export(questions: list[dict], title: str = "试卷", fmt: str = "pdf",
           mode: str = "list", keypoints: str = "", fullpage_ids=None,
           header_footer: dict = None, solution_mode: str = "none",
           std_opts: dict = None, paper_tone: str = "white",
           wimath_logo: bool = False, bank_subject: str = "math",
           template_path: str | Path | None = None,
           cjk_font: str = "", latin_font: str = "",
           progress=None, cancel=None,
           *, layout=None, grouped=None, columns=None, ratio=None,
           std_exam=False) -> Path:
    """在有界编译槽内导出；公开签名向后兼容（新增可选字体/进度/取消参数）。

    progress: ``callable(stage, detail)`` 进度回调。stage 取值 staging /
        rendering / compiling / verifying；compiling 的 detail 带
        ``{"pass": 第几遍, "passes": 总遍数, "pages": 已排页数}``。回调在
        编译线程里执行，实现方自己保证线程安全；回调内部抛错会被吞掉——
        进度上报不能反过来杀死编译。
    cancel: 带 ``is_set()`` 的取消标记（threading.Event）。置位后终止整棵
        编译进程树并抛 ``ExportCancelled``；排队等待编译槽期间同样响应。
    layout/grouped/columns/ratio/std_exam: 导出抽屉的四维自定义组合；全不传
        时走原 mode 逻辑（旧调用方/快照零感知）。
    """
    _validate_export_options(mode=mode, solution_mode=solution_mode, fmt=fmt)
    # 循环轮询拿槽：排队期间被用户终止的任务不该继续占着工作线程白等
    # （导出任务面板的“终止”对排队中的任务也要立即生效）。
    while True:
        _check_cancel(cancel)
        if _EXPORT_SLOTS.acquire(timeout=0.5):
            break
    try:
        return _export_unlocked(
            questions, title=title, fmt=fmt, mode=mode, keypoints=keypoints,
            fullpage_ids=fullpage_ids, header_footer=header_footer,
            solution_mode=solution_mode, std_opts=std_opts,
            paper_tone=paper_tone, wimath_logo=wimath_logo,
            bank_subject=bank_subject, template_path=template_path,
            cjk_font=cjk_font, latin_font=latin_font,
            progress=progress, cancel=cancel,
            layout=layout, grouped=grouped, columns=columns, ratio=ratio,
            std_exam=std_exam,
        )
    finally:
        _EXPORT_SLOTS.release()


def _export_unlocked(questions: list[dict], title: str = "试卷", fmt: str = "pdf",
                     mode: str = "list", keypoints: str = "", fullpage_ids=None,
                     header_footer: dict = None, solution_mode: str = "none",
                     std_opts: dict = None,
                     paper_tone: str = "white",
                     wimath_logo: bool = False,
                     bank_subject: str = "math",
                     template_path: str | Path | None = None,
                     cjk_font: str = "", latin_font: str = "",
                     progress=None, cancel=None,
                     *, layout=None, grouped=None, columns=None, ratio=None,
                     std_exam=False) -> Path:
    """导出为 tex / pdf / zip（tex + 插图打包），返回产物路径。

    questions: dict 列表，每项含 id/body/type/solution。
    mode: list/note/lecture/slides/practice/exam/exam_std/handout。
    solution_mode: none 不出解析 / inline 题后 / separate 解析另起页。
    header_footer: 6 位置页眉页脚 dict（header_left/center/right, footer_*）。
    std_opts: 标准试卷 exam_std 选项（secret_notice/exam_notes/section_points）。
    paper_tone: 所有模式共用的纸张底色（white / cream）；非法值退回 white。
    template_path: 可选的已校验 TeX 模板。模板所在目录中的资源会复制到本次
        导出工作区；不传时继续使用内置 exam_template.tex。
    progress / cancel: 见 ``export``。被终止时删掉本次工作目录再抛出，
        不留半个 PDF 或几百 MB 中间文件。
    """
    if not questions:
        raise ExportError("没有题目可导出")
    _validate_export_options(mode=mode, solution_mode=solution_mode, fmt=fmt)
    # 四维组合在写任何文件之前归一（非法值要早于工作目录创建报错）；spec 为
    # None 时以下各消费点全走原 mode 逻辑。
    spec = resolve_export_layout(layout=layout, grouped=grouped, columns=columns,
                                 ratio=ratio, std_exam=std_exam)

    def emit(stage: str, detail: dict | None = None) -> None:
        if progress is None:
            return
        try:
            progress(stage, detail)
        except Exception:      # 进度上报不允许反向影响编译
            logger.exception("导出进度回调失败（已忽略）：%s", stage)

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # 每次导出使用独占目录。不能在这里清理共享 output/：两个用户同时导出时，
    # 后来的请求会删掉先来请求仍在编译的 md/tex/图片。随机后缀也避免同一秒内
    # 两次请求撞名；超龄目录统一交给 cleanup_output.py 清理。
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = config.OUTPUT_DIR / f"quiz_{stamp}_{uuid.uuid4().hex}"
    work_dir.mkdir()
    stem = f"quiz_{stamp}"
    md_path = work_dir / f"{stem}.md"
    tex_path = work_dir / f"{stem}.tex"
    pdf_path = work_dir / f"{stem}.pdf"

    try:
        _check_cancel(cancel)
        # 0. 暂存题目插图：把 ![[...]] 引用的图拷进本次工作目录并改写为本地文件名，
        #    使 xelatex 能就地找到（quiz_ 前缀 → 下次导出随中间产物一并清理）
        emit("staging")
        questions = _stage_images(questions, stem, work_dir)
        _check_cancel(cancel)

        # 1. 写 Markdown（UTF-8 无 BOM）
        emit("rendering")
        md_path.write_text(
            build_markdown(questions, title, mode=mode, keypoints=keypoints,
                           fullpage_ids=fullpage_ids, solution_mode=solution_mode,
                           std_opts=std_opts, bank_subject=bank_subject,
                           layout=layout, grouped=grouped, columns=columns,
                           ratio=ratio, std_exam=std_exam),
            encoding="utf-8",
        )

        # 2. pandoc → tex（页眉页脚用 -V 变量传入，不被 pandoc 转义）
        # 模板门禁按四维组合折算后的 mode 校验（宽版要 qslide 宏、双栏要
        # qpractice 宏、卷头要 qnotebox——见 resolve_export_layout）；legacy
        # 调用即原 mode，校验面不变。
        template_source = _resolve_template_path(
            template_path,
            mode=(spec.template_mode if spec is not None else mode))
        template_for_pandoc = _stage_template_resources(
            template_source, work_dir) if template_path is not None else template_source
        cmd = [config.PANDOC, str(md_path), "-o", str(tex_path),
               "--template", str(template_for_pandoc)]
        if spec is not None:
            # 模板几何变量由组合本身决定（16:9 页面尺寸 / 双栏窄边距），与
            # legacy slides/practice 的 -V 完全同源。
            if spec.wide:
                cmd += ["-V", "slides=1"]
            elif spec.two_columns:
                cmd += ["-V", "practice=1"]
        elif mode == "slides":
            cmd += ["-V", "slides=1"]
        elif mode == "practice":
            cmd += ["-V", "practice=1"]
        logo_name = _stage_wimath_logo(stem, work_dir, wimath_logo)
        if logo_name:
            cmd += ["-V", f"wimath_logo={logo_name}"]
        cmd += _paper_tone_variable_args(paper_tone)
        cmd += _font_variable_args(cjk_font, latin_font)
        cmd += _hf_variable_args(header_footer, title)
        _run(cmd, cwd=work_dir, step="pandoc", cancel=cancel)
        _check_cancel(cancel)
        if fmt == "tex":
            emit("verifying")
            return tex_path
        if fmt == "zip":
            emit("verifying")
            return _zip_tex(tex_path, stem, work_dir)

        # 3. xelatex → pdf（在 output 目录内跑，nonstopmode 容忍数据里的非法反斜杠）
        #    跑两遍：第一遍把总页数写进 .aux，第二遍 \pageref{LastPage} 才解析成真实数字
        #    （只跑一遍页脚「总页数」会显示 ??）。第二遍去掉 -halt-on-error，避免
        #    第一遍已生成 PDF 后因残留告警中断。
        if template_path is not None:
            try:
                tex_sandbox.compile_xelatex(
                    tex_path, passes=2,
                    timeout=_XELATEX_TIMEOUT_SECONDS, cancel=cancel,
                    progress=lambda info: emit("compiling", info))
            except tex_sandbox.TexSandboxError as exc:
                if getattr(exc, "code", "") == "compile_cancelled":
                    raise ExportCancelled("导出已终止") from exc
                raise ExportError(f"[xelatex 沙箱] {exc}") from exc
        else:
            for i in range(2):
                _check_cancel(cancel)
                # 「已排出第几页」只在内存里传给进度回调，不进任务快照——快照只在
                # 阶段/状态变化时落盘，逐页写会变成每秒一次 JSON 重写。
                page_state = {"max": 0, "sent": 0.0}

                def _on_line(line, *, _i=i, _state=page_state):
                    found = _PAGE_MARK_RE.findall(line)
                    if not found:
                        return
                    page = max(int(x) for x in found)
                    _state["max"] = max(_state["max"], page)
                    now = time.monotonic()
                    if now - _state["sent"] >= 0.5:
                        _state["sent"] = now
                        emit("compiling", {"pass": _i + 1, "passes": 2,
                                           "pages": _state["max"]})

                emit("compiling", {"pass": i + 1, "passes": 2, "pages": 0})
                _run(
                    [config.XELATEX, "-interaction=nonstopmode",
                     *(["-halt-on-error"] if i == 0 else []),
                     f"{stem}.tex"],
                    cwd=work_dir,
                    step="xelatex",
                    cancel=cancel,
                    on_line=_on_line,
                )
        emit("verifying")
        fatal = _xelatex_fatal_log(work_dir)
        if fatal:
            raise ExportError(f"[xelatex] 日志检测到致命错误：{fatal}{_tex_error_hint(work_dir)}")
        if not pdf_path.exists():
            raise ExportError("xelatex 未生成 PDF，请检查 .log 文件")
        return pdf_path
    except ExportCancelled:
        # 用户按的是「不要了」：半成品工作目录整体删掉。普通失败（ExportError）
        # 仍保留目录供排查——两条语义不同，不要合并成一个 except。
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _stage_wimath_logo(stem: str, work_dir: Path,
                       enabled: bool = False) -> str | None:
    """按需把内置 WIMath 矢量标志复制到本次隔离导出目录。"""
    if not enabled:
        return None
    source = Path(config.WIMATH_LOGO_PDF)
    if not source.is_file() or source.is_symlink():
        raise ExportError("WIMath 标志资源缺失，请重新安装 QuizForge")
    local = f"{stem}_img_wimath_logo.pdf"
    shutil.copy2(source, work_dir / local)
    return local


# 自定义纸色的唯一放行格式：严格 #RRGGBB（导出抽屉「自定义色卡」，2026-10-05）。
# 值域数学上封闭（7 字符 hex），所以可以安全地经 -V 直拼进模板的
# \definecolor{...}{HTML}{...}——模板侧不必再做二次校验。不要放宽为「# 开头
# 任意串」：模板变量不做上下文转义，注入防线只在 Python 这一处。
_PAPER_TONE_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\Z")


def _paper_tone_variable_args(paper_tone: str) -> list[str]:
    """把纸张底色转换成 pandoc 模板变量；白色不传变量即使用 PDF 默认白底。

    只认固定枚举与严格 #RRGGBB，不把表单值直接拼进命令。cream 传布尔变量后，
    PDF、单独 tex 和 tex+图片压缩包都在生成的 tex 里固化同一背景；预览与正式
    导出也天然同源。非法值一律按白色（不传变量）——路由层已归一，这里是重试
    回放 / Agent 调用等旁路的第二道防线。
    """
    if isinstance(paper_tone, str):
        value = paper_tone.strip()
        if value == "cream":
            return ["-V", "paper_cream=1"]
        if _PAPER_TONE_HEX_RE.fullmatch(value):
            # 传值剥掉 `#`：\definecolor 的 HTML 模型惯例不带 #（与模板里
            # qpapercream 的 {FAF8F1} 同构），由模板负责 {HTML}{$paper_color$}。
            return ["-V", f"paper_color={value[1:]}"]
    return []


# —— 导出面板字体选项（2026-10-04 新增；2026-10-05 默认改 Fandol）——
# value 为 fontspec 族名（连同 "-V cjk_font"/"latin_font" 进 pandoc 模板变量），
# label 供 UI 下拉直接渲染。第一项即下拉默认选中：FandolSong 随 TeX 分发
# （MiKTeX 自带、不在 Windows 字体注册表），任何机器导出都渲染同一套字——
# 用户 2026-10-05 明确要求默认 Fandol，换取跨机一致性；它缺 /ToUnicode、
# 个别阅读器可能显示空白，由导出面板备注提示“异常空白时可换字体”兜底，
# 不动这里的默认。"" 表示“自动”（本机回落链：思源黑体 → 微软雅黑 → Fandol），
# 保留给旧任务 / Agent 传空串的调用，语义不变。其余系统字体经注册表过滤。
_TEX_BUNDLED_CJK_FONTS = frozenset({"FandolSong"})
_EXPORT_CJK_FONT_CHOICES = (
    ("FandolSong", "Fandol 宋体（默认；TeX 自带）"),
    ("", "自动（思源黑体 → 微软雅黑 → Fandol）"),
    ("Noto Sans SC", "思源黑体 Noto Sans SC"),
    ("Noto Serif SC", "思源宋体 Noto Serif SC"),
    ("Microsoft YaHei", "微软雅黑"),
    ("SimSun", "宋体"),
    ("SimHei", "黑体"),
    ("KaiTi", "楷体"),
    ("FangSong", "仿宋"),
)
_EXPORT_LATIN_FONT_CHOICES = (
    ("", "默认（Latin Modern）"),
    ("Times New Roman", "Times New Roman"),
    ("Arial", "Arial"),
    ("Georgia", "Georgia"),
    ("Cambria", "Cambria"),
    ("Calibri", "Calibri"),
    ("Garamond", "Garamond"),
)


@functools.lru_cache(maxsize=1)
def _windows_font_names() -> frozenset:
    """本机已安装字体注册表键名集合（非 Windows / 读取失败时为空集）。"""
    names: set = set()
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
            index = 0
            while True:
                try:
                    name, _value, _type = winreg.EnumValue(key, index)
                except OSError:
                    break
                names.add(str(name))
                index += 1
    except OSError:
        pass
    return frozenset(names)


def _font_installed(family: str) -> bool:
    """注册表键名形如 `Noto Sans SC (TrueType)` / `SimSun & NSimSun (TrueType)`。"""
    return any(key.startswith(family) for key in _windows_font_names())


def available_export_fonts() -> dict:
    """导出面板的中/英文字体清单（只含本机可用项，供页面下拉渲染）。"""
    # FandolSong（TeX 自带）不在 Windows 字体注册表，恒保留；其余系统字体
    # 按本机注册表过滤。顺序即下拉顺序，首项是页面默认选中项。
    cjk = [(fid, label) for fid, label in _EXPORT_CJK_FONT_CHOICES
           if not fid or fid in _TEX_BUNDLED_CJK_FONTS or _font_installed(fid)]
    latin = [(fid, label) for fid, label in _EXPORT_LATIN_FONT_CHOICES
             if not fid or _font_installed(fid)]
    return {"cjk": cjk, "latin": latin}


def _font_variable_args(cjk_font: str, latin_font: str) -> list[str]:
    """字体选择 → pandoc -V 变量；只认清单内的名字（防任意串拼进 LaTeX 模板）。"""
    allowed_cjk = {fid for fid, _ in available_export_fonts()["cjk"] if fid}
    allowed_latin = {fid for fid, _ in available_export_fonts()["latin"] if fid}
    args: list[str] = []
    if cjk_font in allowed_cjk:
        args += ["-V", f"cjk_font={cjk_font}"]
    if latin_font in allowed_latin:
        args += ["-V", f"latin_font={latin_font}"]
    return args


def _zip_tex(tex_path: Path, stem: str, work_dir: Path) -> Path:
    """把主 .tex、图片和自定义模板资源打成一个可移植 ZIP。

    .tex 单独文件不便携带——里面 \\includegraphics 引用的图片是本次导出
    暂存在 OUTPUT_DIR 的相对文件名，脱离目录就编译不出图。故打包成 zip：
    解压到任意目录即可直接用 xelatex 编译（图片路径和模板相对引用原样能找到）。

    自定义模板会在同一个隔离工作目录中暂存 `.sty`、`.cls`、字体和图片等
    资源。只打包 ``stem_img_*`` 会让 ZIP 离开本机后无法编译，因此这里按
    模板资源白名单收集文件；日志、aux、PDF 和 Markdown 中间文件永远不进入
    用户产物。
    """
    resource_suffixes = {
        ".tex", ".sty", ".cls", ".bbx", ".cbx", ".def", ".cfg",
        ".bib", ".png", ".jpg", ".jpeg", ".webp", ".pdf", ".svg", ".eps",
        ".ttf", ".otf", ".txt", ".md", ".json", ".yaml", ".yml",
    }
    zip_path = work_dir / f"{stem}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(tex_path, arcname=tex_path.name)
        for item in sorted(work_dir.rglob("*")):
            if not item.is_file() or item.is_symlink() or item == tex_path:
                continue
            # 排除本次函数正在写入的 ZIP 及编译产物；主 tex 已在上面写入。
            if item == zip_path or item.name in {f"{stem}.md", f"{stem}.pdf"} \
                    or item.suffix.lower() not in resource_suffixes:
                continue
            arcname = item.relative_to(work_dir).as_posix()
            zf.write(item, arcname=arcname)
    return zip_path


def _tex_error_hint(work_dir: Path) -> str:
    """xelatex 失败时从日志定位首个错误的 tex 行号，并映射到最近的题号。

    映射手法：MiKTeX 日志的错误位置形如 ``l.519``；在该行向上找最近的
    ``\\qopen{...}``（题号所在行），即可把「! Missing $ inserted.」这类
    无上下文的报错落到具体某道题（2026-10-04 审查：单题坏 $ 让整卷失败，
    用户只看到一长串 LaTeX 日志、无法知道是哪一题）。
    """
    logs = sorted(work_dir.glob("*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return ""
    text = logs[0].read_text(encoding="utf-8", errors="replace")
    m = re.search(r"(?m)^l\.(\d+)", text)
    if not m:
        return ""
    line_no = int(m.group(1))
    texs = sorted(work_dir.glob("*.tex"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not texs:
        return ""
    lines = texs[0].read_text(encoding="utf-8", errors="replace").splitlines()
    for i in range(min(line_no, len(lines)) - 1, -1, -1):
        qm = re.search(r"\\qopen\{([^}]*)\}", lines[i])
        if qm:
            return (f"\n[定位] 错误出现在 tex 第 {line_no} 行，向上最近的题号是"
                    f"「{qm.group(1)}」——请对照该题检查未闭合的 $ 或非法字符。")
    return f"\n[定位] 错误出现在 tex 第 {line_no} 行（未能定位到具体题号）。"


def _xelatex_fatal_log(work_dir: Path) -> str:
    """识别 nonstopmode 下仍生成残缺 PDF 的致命 TeX 日志。"""
    logs = sorted(work_dir.glob("*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return ""
    text = logs[0].read_text(encoding="utf-8", errors="replace")
    if re.search(r"(?m)^!\s+(?:File ended while scanning|Emergency stop|Fatal error|Runaway)", text):
        for line in text.splitlines():
            if line.startswith("! "):
                return line[2:].strip()[:240]
        return "TeX 致命错误"
    return ""


# 单步外部命令的超时（秒）—— 是「防止挂死」的兜底，不是性能预算。
# 120 秒对一节课的小卷够用，但整库导出必然撞死在它上面：16282 题 ≈ 2400 页的
# 一份卷子，xelatex 单遍实测 172~200 秒（2026-10-05 高考真题全量导出实测），
# 120 秒会在第二遍中途被杀，用户只拿到第一遍的半成品或直接报「xelatex 超时」。
# 故按大导出重新取值：pandoc 保持 120 秒（9.8MB 实测约 20 秒），xelatex 放宽到
# 900 秒（约 4.5 倍余量）。
_PANDOC_TIMEOUT_SECONDS = 120
_XELATEX_TIMEOUT_SECONDS = 900


def _run(cmd: list[str], cwd: Path, step: str, *, cancel=None, on_line=None):
    """跑外部命令，失败抛 ExportError（参数列表形式，避免注入）。

    从 ``subprocess.run`` 换成 Popen + 读取线程，是为了两件事：① 用户终止导出
    时能立刻杀掉整棵编译进程树（xelatex 会再派生进程，只杀父进程端口不释放）；
    ② 逐行读取输出，把「已排出第几页」这类真实进度报给导出任务面板。
    仍走参数列表、不经 shell，注入面与旧实现相同。
    """
    timeout = (_XELATEX_TIMEOUT_SECONDS if step == "xelatex"
               else _PANDOC_TIMEOUT_SECONDS)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        if step == "pandoc":
            raise ExportError(
                "[pandoc] 未找到随软件附带的 Pandoc。安装包可能不完整，请重新安装 QuizForge"
            )
        if step == "xelatex":
            raise ExportError(
                "[xelatex] 本机尚未安装 XeLaTeX。可安装 MiKTeX 后重试，"
                "或改选“LaTeX 源码包”并上传 Overleaf 编译"
            )
        raise ExportError(f"[{step}] 找不到可执行文件：{cmd[0]}")
    except OSError as exc:
        raise ExportError(f"[{step}] 无法启动：{exc}")

    # 输出尾部滚动保留：失败时只要最后一段用于报错，全量累积在长编译下会吃内存。
    tail: collections.deque = collections.deque(maxlen=400)

    def _pump():
        # 必须持续读走管道：xelatex 输出量远超管道缓冲区，不读会把它自己
        # 阻塞在 write 上（表面症状就是“编译卡死”）。
        try:
            for line in proc.stdout or ():
                tail.append(line)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:   # 进度回调出错不能杀死编译
                        logger.exception("导出输出回调失败（已忽略）：%s", step)
        except (OSError, ValueError):
            pass

    pump = threading.Thread(target=_pump, name=f"qf-export-{step}", daemon=True)
    pump.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            if proc.poll() is not None:
                break
            if _cancelled(cancel):
                tex_sandbox.terminate_process_tree(proc)
                pump.join(timeout=2)
                raise ExportCancelled("导出已终止")
            if time.monotonic() > deadline:
                tex_sandbox.terminate_process_tree(proc)
                pump.join(timeout=2)
                raise ExportError(f"[{step}] 超时（>{timeout}s）")
            time.sleep(0.15)
        pump.join(timeout=5)    # 进程已退出，读取线程把管道排空后自行结束

        if proc.returncode != 0:
            output = "".join(tail)
            hint = _tex_error_hint(cwd) if step == "xelatex" else ""
            raise ExportError(
                f"[{step}] 退出码 {proc.returncode}: {output[-1600:]}{hint}")
    finally:
        # 显式关闭输出管道：迭代结束不会自动 close，漏掉它每次导出都会
        # 泄漏一个管道句柄（长会话里会持续积累）。
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass
