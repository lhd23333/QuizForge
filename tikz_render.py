"""TikZ 配图渲染：模型给的 tikzpicture 片段 → PDF（给 xelatex 用）+ SVG（给页面用）。

为什么是 TikZ 而不是让模型直接输出 SVG：页面能显示，但**导出 PDF 会静默掉图**
——`exam_template.tex` 用 graphicx + \\includegraphics，它不认 .svg；而 SVG→PDF
需要 inkscape / rsvg-convert / cairosvg / magick，本机一个都没有。反方向却是现成的：
TikZ → PDF 由 xelatex 直接编译，PDF → SVG 由 dvisvgm 转，本机 MiKTeX 自带这两个。
所以这里编译出**两份产物**：
  - `<hash>.pdf` —— 导出走这份。`exporter._stage_one` 见到正文里的 .svg 会自动
    换成同名 .pdf（那段代码已经在了，见它的注释），故排版链一行都不用改。
  - `<hash>.svg` —— 页面 <img> 走这份。纯 <path> 矢量，无级缩放。

与服务器版的唯一差异是**落盘位置**：那边是 `IMAGES_DIR/<scope>/`（多用户按人分目录），
单机版图片扁平存在 `_assets/` 下，故本模块不带 scope 参数。文件名仍取代码 hash，
天然不会与 MinerU 落的 `<题目id>_N.jpg` 撞名。

安全边界（这是本模块存在的第二个理由）：
TikZ 是图灵完备的 TeX 代码，直接编译模型输出等于执行不可信代码。三道闸**必须同时在**：
  1. `_validate()` 黑名单拦 \\write18 / \\input / \\usepackage / \\directlua 等；
  2. 编译一律带 `-no-shell-escape`（实测能挡住 \\immediate\\write18）；
  3. `openin_any=p`/`openout_any=p` 环境变量禁止读写工作目录之外的文件。
本应用只监听 127.0.0.1、没有登录，但这三道闸与「外网能不能访问」无关：不可信内容
来自**模型**，题库目录里还有用户全部的题和图，一个 \\input 就能把它们读出来回显。

模型只被允许给 tikzpicture **片段**，documentclass/宏包/字体全由本模块的模板提供。
字体用 ctex 的 `fontset=fandol`（随 TeX 发行分发）而不是 SimSun：换机器时不依赖
系统里装了哪套中文字体。
"""

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# 编译超时（秒）。实测单图编译本机 2.7s，给 60s 足够宽松；真卡住多半是模型写出了
# 无限循环的 \foreach，必须有上限，否则拖死那条后台线程。
COMPILE_TIMEOUT = 60
SVG_TIMEOUT = 30

# 模型可用的 TikZ 库白名单。提示词里同步列了同一份清单——
# 改这里必须改 `prompts/tikz_redraw.md` 的「硬性约束」第 2 条，否则模型会用到
# 没预载的库导致编译失败。
TIKZ_LIBRARIES = (
    "arrows.meta,angles,quotes,calc,patterns,intersections,"
    "decorations.pathmorphing,decorations.markings,positioning,"
    "shapes.geometric,math,through,backgrounds,fit,plotmarks"
)

# standalone + ctex(fandol) + 预载库
_DOC_TEMPLATE = r"""\documentclass[tikz,border=2pt]{standalone}
\usepackage[fontset=fandol]{ctex}
\usepackage{amsmath}
\usepackage{tikz}
\usetikzlibrary{%s}
\begin{document}
%s
\end{document}
"""

# ```tikz ... ``` 围栏（提示词要求的输出格式）。语言标记容错：tikz/latex/tex 都收，
# 甚至没有标记也认——模型偶尔漏掉标记，为此整个功能失败不值得。
_FENCE_RE = re.compile(r"```(?:tikz|latex|tex)?\s*\n(.*?)```", re.S | re.I)
# 兜底：没有围栏时直接找 tikzpicture 环境
_PIC_RE = re.compile(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", re.S)

# PGF 不会把 `(2*sqrt(6),0)` 的第一项自动当作数学表达式：它会在 `sqrt(6)`
# 的右括号处提前结束坐标，并把 `2*sqrt(6` 当成节点名，报 `No shape named ...`。
# 正确写法是 `({2*sqrt(6)},0)`。视觉模型偶尔会漏这层花括号，所以只对能明确
# 判定为算式的坐标分量补齐；普通数值、`(A)` 命名节点和 calc 库的 `($(A)!...$)`
# 都不碰。
_PGF_MATH_FUNC_RE = re.compile(
    r"\b(?:sqrt|sin|cos|tan|asin|acos|atan|atan2|abs|exp|ln|log10|"
    r"veclen|min|max|mod|pow)\s*\(", re.I)

# 危险命令黑名单。TikZ 是完整的 TeX，能读写文件、执行 shell、覆盖任意宏。
# `-no-shell-escape` 已经挡住 \write18，但多一道显式检查能给用户可读的报错，
# 也防住 shell-escape 之外的路子（\input 读盘上任意文件、\usepackage 引入
# 未预载的包导致编译失败、\output 改页面输出流把整份文档搅乱）。
_FORBIDDEN = (
    r"\write18", r"\immediate", r"\openout", r"\openin", r"\read",
    r"\input", r"\include", r"\usepackage", r"\RequirePackage",
    r"\documentclass", r"\directlua", r"\latelua", r"\luaescapestring",
    r"\special", r"\pdfximage", r"\includegraphics", r"\catcode",
    r"\csname", r"\expandafter\csname", r"\output", r"\shipout",
    r"\usetikzlibrary", r"\pgfsys@", r"\batchmode", r"\errorstopmode",
    r"\loop", r"\repeat",
)


class TikzError(Exception):
    """TikZ 渲染失败。message 直接给用户看，要说清是哪一步、下一步能怎么办。"""


def extract_tikz(reply: str) -> str:
    """从模型回复里抠出 tikzpicture 片段。抠不到抛 TikzError。

    提示词要求「只输出一个 ```tikz 代码块」，但模型偶尔会：漏掉语言标记、
    在代码块前后加一句解释、或者干脆不加围栏。这些都能救，不该让用户重试。
    真正救不了的只有「回复里根本没有 tikzpicture」。
    """
    if not reply or not reply.strip():
        raise TikzError("模型返回了空内容，请重试或调大 max_tokens。")

    # 先找围栏里的内容，再在其中定位 tikzpicture（围栏里可能带了 \documentclass，
    # 那种情况只取 tikzpicture 部分，剩下的外层结构由本模块的模板提供）
    for block in _FENCE_RE.findall(reply):
        m = _PIC_RE.search(block)
        if m:
            return m.group(0).strip()
    # 没有围栏（或围栏里没有环境）时全文兜底
    m = _PIC_RE.search(reply)
    if m:
        return m.group(0).strip()

    snippet = " ".join(reply.split())[:150]
    raise TikzError(
        "模型没有输出 TikZ 代码（找不到 \\begin{tikzpicture}）。"
        f"回复开头：{snippet}"
    )


def _split_coordinate_pair(content: str) -> tuple[str, str] | None:
    """把圆括号内容按顶层逗号拆成两个坐标分量；函数参数里的逗号不算。"""
    depth = 0
    brace_depth = 0
    for i, ch in enumerate(content):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
        elif ch == "," and depth == 0 and brace_depth == 0:
            # 三维坐标有两个顶层逗号，本修复只处理 TikZ 最常见的二维坐标。
            if _split_coordinate_pair(content[i + 1:]) is not None:
                return None
            return content[:i], content[i + 1:]
    return None


def _wrap_coordinate_component(component: str) -> str:
    """明确含 PGF 算式的坐标分量外包花括号，保留原有首尾空白。"""
    core = component.strip()
    if not core or (core.startswith("{") and core.endswith("}")):
        return component
    # 函数调用，以及乘除/幂运算，都必须交给 pgfmath 解析。单纯负数或小数本来
    # 就是合法坐标，不包，避免把正常模型输出全部改写一遍。
    if not (_PGF_MATH_FUNC_RE.search(core) or any(op in core for op in "*/^")):
        return component
    leading = component[:len(component) - len(component.lstrip())]
    trailing = component[len(component.rstrip()):]
    return f"{leading}{{{core}}}{trailing}"


def normalize_coordinate_math(code: str) -> str:
    """修正模型常见的裸 PGF 数学坐标，不改其它 TikZ 结构。"""
    # 长度上限仍由 _validate 给出用户可读错误；这里先拒绝做二次方扫描，避免异常
    # 长回复在进入既有上限检查前消耗不必要的 CPU。
    if len(code) > 20000:
        return code
    stack: list[int] = []
    replacements: list[tuple[int, int, str]] = []
    for i, ch in enumerate(code):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            start = stack.pop()
            pair = _split_coordinate_pair(code[start + 1:i])
            if pair is None:
                continue
            left, right = pair
            fixed_left = _wrap_coordinate_component(left)
            fixed_right = _wrap_coordinate_component(right)
            if (fixed_left, fixed_right) != (left, right):
                replacements.append((start + 1, i, fixed_left + "," + fixed_right))

    # 从后往前替换，前面的字符位置不受后面长度变化影响。候选二维坐标不会互相
    # 重叠；函数自己的圆括号没有顶层逗号，因此不会进入 replacements。
    fixed = code
    for start, end, value in reversed(replacements):
        fixed = fixed[:start] + value + fixed[end:]
    return fixed


def _validate(code: str) -> None:
    """黑名单检查。命中就拒，不做「清洗后放行」——清洗永远有绕过。"""
    low = code.lower()
    for bad in _FORBIDDEN:
        if bad.lower() in low:
            raise TikzError(
                f"TikZ 代码里含不允许的命令 `{bad}`（出于安全与编译稳定性禁用）。"
                f"配图只需要绘图命令，不应引入宏包或读写文件。"
            )
    if "\\begin{tikzpicture}" not in code:
        raise TikzError("TikZ 代码缺少 \\begin{tikzpicture} 环境。")
    if code.count("\\begin{tikzpicture}") != code.count("\\end{tikzpicture}"):
        raise TikzError("TikZ 代码的 tikzpicture 环境没有正确闭合。")
    # 括号配平：编译器报的错很难读，这里先给一句人话
    for open_c, close_c, name in (("{", "}", "花括号"), ("[", "]", "方括号")):
        if code.count(open_c) != code.count(close_c):
            raise TikzError(f"TikZ 代码的{name}数量不配平（{code.count(open_c)} 个 "
                            f"`{open_c}` 对 {code.count(close_c)} 个 `{close_c}`）。")
    if len(code) > 20000:
        raise TikzError(f"TikZ 代码过长（{len(code)} 字符，上限 20000）。")


def _xelatex_cmd() -> str:
    """xelatex 可执行路径：优先 config.XELATEX，否则用 PATH。

    config.XELATEX 本身已经是 `shutil.which("xelatex") or 写死的 MiKTeX 路径`，
    这里再判一次 is_file 是为了兜住「换了机器、那个写死路径不存在」的情形——
    别让整个功能因为一条陈旧的路径配置挂掉。
    """
    p = getattr(config, "XELATEX", "") or ""
    if p and Path(p).is_file():
        return p
    found = shutil.which("xelatex")
    if not found:
        raise TikzError("本机找不到 xelatex，无法编译 TikZ 配图。"
                        "请安装 MiKTeX 或 TeX Live（导出 PDF 也需要它）。")
    return found


def _dvisvgm_cmd() -> str:
    """dvisvgm 路径。缺它只影响页面预览那份 svg，但没有 svg 就没法给用户看效果，
    所以同样按硬失败处理，并在报错里点明它随 TeX 发行一起装。"""
    configured = getattr(config, "DVISVGM", "") or ""
    if configured and (not Path(configured).is_absolute() or Path(configured).is_file()):
        return configured
    found = shutil.which("dvisvgm")
    if not found:
        raise TikzError("本机找不到 dvisvgm，无法把配图转成页面可显示的 SVG。"
                        "它随 MiKTeX / TeX Live 分发，通常与 xelatex 在同一目录。")
    return found


def _run(cmd: list[str], cwd: Path, timeout: int, step: str) -> subprocess.CompletedProcess:
    """跑外部命令。TeX 相关的都要禁掉文件读写越界（openin/openout_any=p）。"""
    env = dict(os.environ)
    env["openin_any"] = "p"      # 只许读工作目录内
    env["openout_any"] = "p"     # 只许写工作目录内
    env["shell_escape"] = "f"
    env["max_print_line"] = "1000"
    try:
        return subprocess.run(
            cmd, cwd=str(cwd), timeout=timeout, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise TikzError(
            f"{step} 超时（>{timeout}s）。TikZ 代码可能含无法收敛的循环，请重新生成。"
        ) from e
    except OSError as e:
        raise TikzError(f"{step} 无法启动：{e}") from e


def _tex_error(log: str) -> str:
    """从 xelatex 的 log 里挑出人能看的那几行。"""
    lines = []
    for ln in log.splitlines():
        s = ln.strip()
        if s.startswith("!") or "Undefined control sequence" in s:
            lines.append(s)
        elif s.startswith("l.") and lines:
            lines.append(s)          # 出错行号紧跟在 ! 之后
        if len(lines) >= 6:
            break
    return " / ".join(lines) if lines else "（log 里没有可识别的错误行）"


def render(code: str) -> tuple[str, str]:
    """TikZ 片段 → (pdf 文件名, svg 文件名)，两份都落在 ASSETS_DIR 下。

    文件名取代码的 sha256 前 16 位，所以同一段代码重复渲染直接命中已有文件，
    不重复编译；不同题目生成了相同的图也自然共享一份。

    返回的是**文件名**而不是路径，调用方拼成 `![[<name>]]` 存进正文 —— 与
    filestore.save_image 落 jpg 的形式完全一致，于是 asset_serve 能直接发、
    qrender 的 <img> 不用改、exporter._stage_one 照常拷（它还会自动把 .svg
    换成同名 .pdf 给 graphicx）。
    """
    _validate(code)

    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
    name_pdf = f"tikz_{digest}.pdf"
    name_svg = f"tikz_{digest}.svg"
    dest_dir = config.ASSETS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = dest_dir / name_pdf
    out_svg = dest_dir / name_svg
    if out_pdf.is_file() and out_svg.is_file():
        logger.info("TikZ 命中缓存: %s", digest)
        return name_pdf, name_svg

    doc = _DOC_TEMPLATE % (TIKZ_LIBRARIES, code)
    with tempfile.TemporaryDirectory(prefix="tikz_") as td:
        work = Path(td)
        (work / "fig.tex").write_text(doc, encoding="utf-8")

        proc = _run([_xelatex_cmd(), "-no-shell-escape", "-interaction=nonstopmode",
                     "-halt-on-error", "fig.tex"],
                    work, COMPILE_TIMEOUT, "xelatex 编译")
        pdf = work / "fig.pdf"
        if not pdf.is_file():
            log = (work / "fig.log").read_text(encoding="utf-8", errors="replace") \
                if (work / "fig.log").is_file() else proc.stdout.decode("utf-8", "replace")
            logger.warning("TikZ 编译失败: %s", _tex_error(log))
            raise TikzError(f"TikZ 代码编译失败：{_tex_error(log)}")

        # PDF → SVG。--no-fonts 把文字转成 <path> 轮廓：页面上视觉与缩放都正常，
        # 代价是图里的文字不能被鼠标选中。换成 --font-format=woff 可保留可选文字，
        # 但要额外内嵌字体、体积更大且个别 CJK 字体转不干净，页面展示用不上，
        # 故取更稳的轮廓化（PDF 那份不受影响，导出的文字仍是真字体）。
        svg_proc = _run([_dvisvgm_cmd(), "--pdf", "--no-fonts",
                         "--output=fig.svg", "fig.pdf"],
                        work, SVG_TIMEOUT, "dvisvgm 转换")
        svg = work / "fig.svg"
        if not svg.is_file():
            detail = " ".join(svg_proc.stdout.decode("utf-8", "replace").split())[:200]
            raise TikzError(f"PDF 转 SVG 失败：{detail}")

        # 两份产物都齐了才落盘，避免出现「有 pdf 没 svg」的半成品被缓存命中
        shutil.copy2(pdf, out_pdf)
        shutil.copy2(svg, out_svg)

    logger.info("TikZ 渲染成功: %s (pdf=%d svg=%d)", digest,
                out_pdf.stat().st_size, out_svg.stat().st_size)
    return name_pdf, name_svg


def render_from_reply(reply: str) -> tuple[str, str, str]:
    """模型回复 → (tikz 源码, pdf 名, svg 名)。抠码 + 校验 + 编译一条龙。"""
    code = normalize_coordinate_math(extract_tikz(reply))
    name_pdf, name_svg = render(code)
    return code, name_pdf, name_svg
