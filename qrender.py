"""页面侧题目正文渲染：把题目原文渲染成结构化 HTML，向 PDF 排版靠拢。

此前页面只做「转义 + 还原 <img>」（app.py 的 qimage 过滤器），正文靠 CSS
`white-space: pre-wrap` 原样显示，选项挤成一行、与导出的 PDF 版式差别很大。
本模块补上导出侧已有的结构判定，让卡片上看到的分列与 PDF 一致。

**规则只有一份**：选项切分与列数判定直接调 exporter.split_choice_options /
exporter.choice_cols —— 与导出走同两个函数。别在 JS 里重写一遍（那样必然漂移），
也别在这里另抄一套阈值。

范围（有意划定，见 ADR 讨论）：
  - 做：选择题选项分列、图片位置/宽度、图文左右分栏。
  - 不做：分页（半页块 / \\clearpage）——浏览器里没有「页」的概念；
          填空题的段落绕排（\\qfigwrap）——CSS float 的换行与 TeX 不同，
          只能近似，故页面统一按「图在下方」处理，不假装一致。

公式仍然交给前端渲染（KaTeX，见 static/js/math.js；本模块只吐 $...$ 原文，不碰数学区内容）。
"""

import base64
import re
from urllib.parse import quote

from markupsafe import Markup, escape

import exporter
import export_tables


def _asset_src(filename: str) -> str:
    """图片文件名 → 页面可用的 URL，对应 app.py 的 asset_serve 路由。

    不用 url_for：本模块的函数也被模板过滤器之外的地方调用，硬依赖请求上下文
    会让单元测试和离线调用都被迫起 Flask app。路由前缀是稳定的，直接拼即可。
    """
    return "/assets/" + quote(filename)

# 与 app.py 的 _QIMG_RE、exporter._QIMG_EXPORT_RE 同源：题目正文里的图片引用。
# 单机版是 Obsidian 双链嵌入 ![[filename]]（可带 |宽度 后缀），图片扁平存在
# config.ASSETS_DIR 下；服务器版那边是 ![alt](/qimages/<scope>/<file>)。
# 只有一个捕获组（文件名），没有 alt。
_QIMG_RE = re.compile(r"!\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")

# 未自定义时图片占正文宽的默认比例，与 exporter._DEFAULT_IMG_FRAC、
# image-layout.js 的 DEFAULT_IMG_W 三处一致
_DEFAULT_IMG_W = 35


def _img_tag(src: str, alt: str, width=None, full: bool = False,
             unit_width=None) -> str:
    """一张图的 <img>。width 为占正文宽的百分比（None → 默认 35%）。"""
    w = width if width else _DEFAULT_IMG_W
    cls = "q-img q-img-full" if full else "q-img"
    data = (f' data-unit-width="{unit_width:.3f}"'
            if unit_width is not None else "")
    style = (f' style="width:{unit_width:.3f}%"' if unit_width is not None
             else ("" if full else f' style="width:{w}%"'))
    return f'<img src="{src}" alt="{alt}" class="{cls}"{data}{style}>'


_TABLE_TOKEN_RE = re.compile(r"QPREVIEWTABLE([A-Za-z0-9\-_=]+)QPREVIEWTABLEEND")


def _table_html(rows: list[list[tuple[str, int]]]) -> str | None:
    """纯文本表格行 → 安全 HTML；首行与 PDF 一样作为表头。"""
    if not rows:
        return None
    ncol = max(sum(span for _text, span in row) for row in rows)
    if ncol < 1:
        return None

    def _row(cells, tag: str) -> str:
        out = []
        used = 0
        for text, span in cells:
            span = min(max(1, int(span)), ncol)
            used += span
            attr = f' colspan="{span}"' if span > 1 else ""
            out.append(f"<{tag}{attr}>{escape(text)}</{tag}>")
        out.extend(f"<{tag}></{tag}>" for _ in range(max(0, ncol - used)))
        return "<tr>" + "".join(out) + "</tr>"

    head = _row(rows[0], "th")
    body = "".join(_row(row, "td") for row in rows[1:])
    body_html = f"<tbody>{body}</tbody>" if body else ""
    return ('<div class="q-table-wrap" role="region" aria-label="题目表格" tabindex="0">'
            f'<table class="q-table"><thead>{head}</thead>{body_html}</table></div>')


def _table_token(html: str) -> str:
    data = base64.urlsafe_b64encode(html.encode("utf-8")).decode("ascii")
    return f"\n\nQPREVIEWTABLE{data}QPREVIEWTABLEEND\n\n"


def _stash_tables(text: str) -> str:
    """把 HTML/Markdown 表格换成安全 HTML 令牌，保护它不被选项切分正则咬坏。"""
    if not text:
        return text

    if "<table" in text.lower():
        def _html_sub(match):
            table = _table_html(export_tables.html_table_rows(match.group(1)))
            return _table_token(table) if table else match.group(0)
        text = export_tables.TABLE_RE.sub(_html_sub, text)

    if "|" not in text:
        return text
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        head = lines[i].strip()
        if (head.startswith("|") and i + 1 < len(lines)
                and export_tables.PIPE_SEP_RE.match(lines[i + 1])):
            rows = [export_tables.pipe_text_cells(lines[i])]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(export_tables.pipe_text_cells(lines[j]))
                j += 1
            table = _table_html(rows)
            if table:
                out.append(_table_token(table))
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _render_text(text: str) -> str:
    """一段普通正文 → 转义后的 HTML，图片引用还原成 <img>。

    与 app.py 的 qimage_filter 同款顺序：先整体转义（保护数学式里的 <>&），
    再替换图片标记 —— 反过来会把刚生成的标签也转义掉。
    """
    escaped = str(escape(_stash_tables(text)))

    def _to_img(m):
        return _img_tag(_asset_src(m.group(1)), "")

    escaped = _QIMG_RE.sub(_to_img, escaped)

    def _restore_table(match):
        return base64.urlsafe_b64decode(match.group(1)).decode("utf-8")

    return _TABLE_TOKEN_RE.sub(_restore_table, escaped)


def _strip_imgs(text: str) -> tuple[str, list[tuple[str, str]]]:
    """把正文里的图片引用换成位置哨兵，返回 (带哨兵正文, [(src, alt), ...])。

    与 exporter._stage_images 的 _rewrite 对称：两边都在**原位**留一个
    `QFIGSLOT{n}` 哨兵（exporter._SLOT_SENT），图片本体由各自的渲染分支按哨兵
    位置排回去 —— 这就是「图留在文字中间」两条链共用的机制，编排规则则统一由
    exporter.plan_figs 给（页面与 PDF 因此不会各排一套）。
    哨兵是纯大写字母+数字，不含 `![](...)`、`$`、`（`、反斜杠，所以后续的选项切分/
    小问识别/HTML 转义都不会碰它；正文里没被排出去的哨兵最后由 _drop_slots 清掉。
    图片序号（列表下标）与 img_layouts 里的 i、image-layout.js 的 `.body img`
    遍历顺序一致 —— 三者同源于正文里图片引用的原始先后顺序。
    """
    figs: list[tuple[str, str]] = []

    def _sub(m):
        figs.append((_asset_src(m.group(1)), ""))
        return f"{exporter._SLOT_SENT}{len(figs) - 1}"

    return _QIMG_RE.sub(_sub, text).rstrip(), figs


# 哨兵连同紧邻的空行一起匹配：`.q-stem` 是 pre-wrap，哨兵所在的空行不吃掉就会
# 在图片块上下各留一道空白（图片块本身是 block 级，不需要换行来分段）。
_SLOT_LINE_RE = re.compile(r"[ \t]*\n{0,2}[ \t]*" + exporter._SLOT_RE.pattern
                           + r"[ \t]*\n{0,2}")


def _drop_slots(text: str) -> str:
    """清掉没被排出去的哨兵（已成块的图那份哨兵，以及缺图的脏数据）。"""
    return _SLOT_LINE_RE.sub("", text).strip()


def _fig_div(figs: list[tuple[str, str]], idx: int, layouts: dict) -> str:
    """单张图 → 一个 .q-fig 块（按自身宽度/对齐）。

    对齐用 flex 的 justify-content（left/center/right），未设时默认居中 ——
    与 exporter._fig_item 的兜底一致。
    """
    src, alt = figs[idx]
    lay = layouts.get(idx) or {}
    align = lay.get("align") or "center"
    return (f'<div class="q-fig q-fig-{align}">'
            f'{_img_tag(src, alt, lay.get("w"))}</div>')


def _fig_row(figs: list[tuple[str, str]], ids: list[int], layouts: dict) -> str:
    """一组并排图 → 一个整体居中的 .q-fig-row（flex 一行）。

    每个 cell 自己占正文宽度的 w%，图片撑满 cell；不能让图片在一个无宽度 cell
    里再取 w%，否则会形成“百分比套百分比”的二次缩小。两图之间只留固定小间距，
    不再用 space-between 把它们顶到题卡两侧。
    """
    if len(ids) == 1:
        return _fig_div(figs, ids[0], layouts)
    widths = [(layouts.get(i) or {}).get("w") or _DEFAULT_IMG_W for i in ids]
    total = sum(widths) or 1
    # 三张以上默认宽度之和会超过 100%；按相对比例压进 98% 行宽，既完整横排又
    # 保留用户调出的大小关系。分栏右侧用的是同一公式（见 _split_fig_unit_html）。
    cells = "".join(
        f'<div class="q-fig-cell" style="flex:0 0 '
        f'{w / total * 98:.3f}%">'
        f'{_img_tag(figs[i][0], figs[i][1], full=True)}</div>'
        for i, w in zip(ids, widths))
    return f'<div class="q-fig-row">{cells}</div>'


def _fig_stack(figs: list[tuple[str, str]], ids: list[int], layouts: dict) -> str:
    """连续上下图 → 一个居中、零结构间距的图片组。"""
    images = "".join(
        _img_tag(figs[i][0], figs[i][1], (layouts.get(i) or {}).get("w"))
        for i in ids)
    return f'<div class="q-fig-stack">{images}</div>'


def _fig_unit(figs: list[tuple[str, str]], unit: dict, layouts: dict) -> str:
    ids = unit.get("ids") or []
    if len(ids) > 1 and not unit.get("row"):
        return _fig_stack(figs, ids, layouts)
    return _fig_row(figs, ids, layouts)


def _split_fig_unit_html(figs: list[tuple[str, str]], unit: dict,
                         layouts: dict) -> str:
    """分栏右栏里的一个完整视觉图片组，宽度换算为组内相对比例。"""
    ids = unit.get("ids") or []
    widths = [(layouts.get(i) or {}).get("w") or _DEFAULT_IMG_W for i in ids]
    if len(ids) > 1 and unit.get("row"):
        total = sum(widths) or 1
        cells = "".join(
            f'<div class="q-fig-cell" style="flex:0 0 {w / total * 98:.3f}%">'
            f'{_img_tag(figs[i][0], figs[i][1], full=True)}</div>'
            for i, w in zip(ids, widths))
        return f'<div class="q-fig-row q-fig-row-split">{cells}</div>'
    if len(ids) > 1:
        widest = max(widths) or 1
        images = "".join(
            _img_tag(figs[i][0], figs[i][1], full=True,
                     unit_width=w / widest * 100)
            for i, w in zip(ids, widths))
        return f'<div class="q-fig-stack q-fig-stack-split">{images}</div>'
    i = ids[0]
    return _img_tag(figs[i][0], figs[i][1], full=True)


def _figs_html(figs: list[tuple[str, str]], layouts: dict,
               ids: list[int] = None, plan: dict = None) -> str:
    """一批图（按序号）→ HTML，行的划分照 exporter.plan_figs 的分组。

    ids 省略时为全部图、每图独占一行（老调用形态）。
    """
    if ids is None:
        ids = list(range(len(figs)))
    ids = [i for i in ids if i < len(figs)]
    if not ids:
        return ""
    if not plan:
        return "".join(_fig_div(figs, i, layouts) for i in ids)
    return "".join(_fig_unit(figs, unit, layouts)
                   for unit in exporter._plan_units(ids, plan))


def _fill_slots_html(body: str, ids: list[int], figs: list[tuple[str, str]],
                     layouts: dict, plan: dict) -> str:
    """把正文里指定序号的哨兵**原位**换成图片块（与 exporter._fill_slots 对称）。

    一组并排图由组内**第一个**哨兵一次性排成一行，组内其余哨兵就地清掉 ——
    分两处发射就成上下两行了。未列入 ids 的哨兵原样留着。
    """
    if not ids:
        return body
    units = exporter._plan_units(ids, plan)
    head_of = {unit["ids"][0]: unit for unit in units}
    drop = {i for unit in units for i in unit["ids"][1:]}
    want = set(ids)

    def _sub(m):
        idx = int(m.group(1))
        if idx not in want:
            return m.group(0)
        if idx in drop:
            return ""
        return _fig_unit(figs, head_of.get(idx) or {"ids": [idx], "row": False},
                         layouts)

    # 与 _drop_slots 同款：连哨兵所在的空行一起换掉，图片块前后不多留空白
    return _SLOT_LINE_RE.sub(_sub, body)


def _options_html(opts: list[str], cols: int) -> str:
    """选项列表 → CSS Grid 网格。列数由 exporter.choice_cols 决定（与 PDF 同源）。

    grid 而非 flex：flex 的换行不保证同列对齐，grid 才能让 A/B/C/D 的左端在列上
    严格对齐（这正是「整齐排列」的诉求）。列数挂在 data-cols 上由 CSS 取用，
    避免在 style 里拼 grid-template-columns（CSP 与可读性都更好）。
    """
    cells = "".join(f'<span class="q-opt">{_drop_slots(_render_text(o))}</span>'
                    for o in opts)
    return f'<div class="q-opts" data-cols="{cols}">{cells}</div>'


def _pair_html(opts: list[str], figs: list[tuple[str, str]],
               pair_map: list[int], cols: int) -> str:
    """四图配选项 → 网格，一格「左侧选项标签 + 右侧图片」。

    对应 exporter._pair_grid_latex 的 minipage 网格，列数同样由 plan_figs 给
    （2 或 4），挂在 data-cols 上由 CSS 取用。每格里的图撑满格宽（宽度由列数
    决定，不再用各图自己的 w）—— 与 PDF 侧 \\qpairitem 的 width=\\linewidth 一致。
    """
    cells = []
    for oi, text in enumerate(opts):
        idx = pair_map[oi] if oi < len(pair_map) else None
        img = ""
        if idx is not None and idx < len(figs):
            src, alt = figs[idx]
            alt = alt or f"选项 {chr(65 + oi)} 图片"
            img = f'<img src="{src}" alt="{alt}" class="q-img q-img-full">'
        cells.append(f'<div class="q-pair-cell">'
                     f'<span class="q-pair-label">'
                     f'{_drop_slots(_render_text(text))}</span>{img}</div>')
    return (f'<div class="q-opt-pair" data-cols="{cols}">'
            f'{"".join(cells)}</div>')


# 题型集合借用 exporter 的定义，避免两处各写一份中文题型名字符串
_CHOICE = exporter._CHOICE
_BLANK = exporter._BLANK
_SOLVE = exporter._SOLVE


def render_body(text: str, qtype: str = None, img_layouts=None,
                img_width=None, img_align=None, img_split=None) -> Markup:
    """题目正文 → 结构化 HTML（页面卡片用）。

    选择题：题干 + 选项网格（列数与 PDF 同源，见 _options_html）。
    其余题型：正文原样（转义 + 还原图片），保持 pre-wrap 显示。
    图片：落位/编组一律照 exporter.plan_figs（与导出侧同一个函数）——
      - 图后面还有正文 → **原位**排在文字中间（.q-fig-row 就地插在段落之间）；
      - 图在题干末尾/选项之前 → 排在正文之后（最常见形态，版式与改动前一致）；
      - 相邻两图 → 并排一行（.q-fig-row），组首图标了 stack 则回到上下堆叠；
      - 四张图配四个选项 → .q-opt-pair 网格（见 _pair_html）。
    图文分栏（img_split）：题干整行、选项与首个**尾图**左右两栏，对应 exporter
    _place_choice_split 的 opts 模式；full 模式下题干也进左栏。

    任何一步识别不出结构就退回「原样渲染」——页面显示退化成旧效果可以接受，
    渲染报错让整个题库页 500 不可接受。
    """
    if not text:
        return Markup("")

    # 表格先换成无标点令牌：表格单元格里可能出现 A./B.，若等到 `_render_text`
    # 才处理，选择题切分会先把表格当成选项拆坏。
    text = _stash_tables(text)

    layouts = exporter._parse_layouts(img_layouts)
    # img_layouts 缺 i=0 时用旧的 img_width/img_align 两列兜底（老题），
    # 与 exporter._layout_at 的兜底逻辑对称
    if 0 not in layouts:
        w0 = None
        try:
            w0 = int(img_width) if img_width not in (None, "") else None
        except (TypeError, ValueError):
            w0 = None
        layouts[0] = {"w": w0, "align": img_align or None}

    body, figs = _strip_imgs(text)
    # img_split 传原始列值（不经 _norm_split）：plan_figs 要靠 NULL/"off" 的区别判
    # 四图配对的默认值，见它的 docstring
    plan = exporter.plan_figs(body, qtype, img_layouts, img_split)
    inline_ids = [s["i"] for s in plan["slots"] if s["pos"] == "stem"]
    tail_ids = [s["i"] for s in plan["slots"] if s["i"] not in set(inline_ids)]
    # resolve_split 而非 _norm_split：带图选择题在用户没设过时默认整题分栏，
    # 解答题默认不小问分栏 —— 与导出侧 _q_md 同一函数，默认值不会两边不一致。
    # has_img 传的是「有尾图」，理由见 exporter.resolve_split 的 docstring
    split_mode = exporter.resolve_split(qtype, img_split, bool(tail_ids))
    tail_ids = [i for i in tail_ids if i < len(figs)]

    if qtype in _CHOICE:
        parts = exporter.split_choice_options(body)
        if parts is not None:
            stem, opts, opt_tail = parts
            # 选项区之后的附注（「参考公式：…」）整行宽排在选项之下，与
            # exporter._place_choice_split 的 opt_tail 落位一致
            tail_html = (f'<div class="q-tail">'
                         f'{_drop_slots(_render_text(opt_tail))}</div>'
                         if opt_tail else "")
            stem_body = _stem_html(stem, inline_ids, figs, layouts, plan)
            # 四图配选项：一图配一选项的网格，不走选项网格也不走图文分栏
            if plan["pair"]:
                return Markup(stem_body
                              + _pair_html(opts, figs, plan["pair_map"],
                                           plan["pair_cols"])
                              + tail_html)
            cols = exporter.choice_cols(opts)
            if split_mode in ("opts", "full") and tail_ids:
                unit, _rest = exporter._split_first_unit(tail_ids, plan)
                width = exporter._split_unit_width(unit, layouts, img_width)
                text_fraction, _image_fraction = exporter._split_fracs(width)
                cols = exporter.choice_cols(opts, text_fraction)
            grid = _options_html(opts, cols)
            if split_mode in ("between", "after") and tail_ids:
                figures = _figs_html(figs, layouts, tail_ids, plan)
                return Markup(
                    stem_body + figures + grid + tail_html
                    if split_mode == "between"
                    else stem_body + grid + tail_html + figures)
            if split_mode and tail_ids:
                return Markup(_choice_split_html(stem_body, grid, figs, layouts,
                                                 tail_ids, plan,
                                                 full=split_mode == "full",
                                                 tail_html=tail_html))
            return Markup(stem_body + grid + tail_html
                          + _figs_html(figs, layouts, tail_ids, plan))

    # 填空题没有选项切分，旧页面因此直接掉到了最下面的普通渲染分支：frontmatter
    # 已写入 opts、按钮也亮着，PDF 会分栏，唯独题卡上看不到 `.q-split`。填空题的
    # 左栏就是完整正文，右栏仍消费首个尾图视觉组，与 exporter._q_md 的填空分支
    # 一致；原位图已由 `_stem_html` 留在文字中间，不会被强挪到右栏。
    if qtype in _BLANK and split_mode in ("opts", "full") and tail_ids:
        unit, rest = exporter._split_first_unit(tail_ids, plan)
        return Markup(
            _split_row(_stem_html(body, inline_ids, figs, layouts, plan),
                       figs, unit, layouts)
            + _figs_html(figs, layouts, rest, plan)
        )

    if qtype in _SOLVE and split_mode == "sub" and tail_ids:
        parts = exporter._split_stem_subs(body)
        if parts is not None:
            stem, subs = parts
            return Markup(_solve_split_html(
                _stem_html(stem, inline_ids, figs, layouts, plan),
                _stem_html(subs, inline_ids, figs, layouts, plan),
                figs, layouts, tail_ids, plan))

    if qtype in (_BLANK | _SOLVE) and split_mode == "between" and tail_ids:
        parts = exporter._split_stem_subs(body)
        if parts is not None:
            return Markup(
                _stem_html(parts[0], inline_ids, figs, layouts, plan)
                + _figs_html(figs, layouts, tail_ids, plan)
                + _stem_html(parts[1], inline_ids, figs, layouts, plan))

    if qtype in _SOLVE and split_mode == "full" and tail_ids:
        unit, rest = exporter._split_first_unit(tail_ids, plan)
        return Markup(
            _split_row(_stem_html(body, inline_ids, figs, layouts, plan),
                       figs, unit, layouts)
            + _figs_html(figs, layouts, rest, plan))

    return Markup(_stem_html(body, inline_ids, figs, layouts, plan)
                  + _figs_html(figs, layouts, tail_ids, plan))


def _stem_html(text: str, inline_ids: list[int], figs: list[tuple[str, str]],
               layouts: dict, plan: dict) -> str:
    """一段题干 → `.q-stem`，其中的原位图就地插成图片块。

    先转义再插图片块（与 _render_text 同款顺序）：反过来会把刚生成的标签转义掉。
    哨兵是纯字母数字，转义不会动它，所以「转义 → 按哨兵插块 → 清残留哨兵」这个
    顺序是安全的。
    """
    html = _render_text(text)
    if inline_ids:
        html = _fill_slots_html(html, inline_ids, figs, layouts, plan)
    return f'<div class="q-stem">{_drop_slots(html)}</div>'


def solve_split_reason(text: str, qtype: str = None, img_split=None) -> str | None:
    """解答题的「小问分栏」在这道题上能不能生效；不能则返回**具体原因**。

    条件与 render_body 里那个 sub 分支逐条对齐、调同一批函数：题型是解答题、
    生效模式是 "sub"、正文里有图、有可进右栏的尾图、且 _split_stem_subs 真能切出
    「题干 + 小问」。任一条不满足，render_body 就静默回退成普通渲染 —— 版式没变、
    chip 却亮着，与「点了没反应」无法区分（这正是本函数存在的原因）。路由据此在
    写库前拦下来，把原因回给前端，而不是回一个看着成功的 ok:true。

    **回原因字符串而不是 bool**：此前只回 bool，路由把所有失败一律报成「这道题没有
    分小问」。于是「图夹在题干中间、没有尾图」这种情况也报成没有小问 —— 题里明明
    有小问，用户看到的就是「勾了小问分栏，直接显示没有小问了」。措辞与真实原因不符
    的报错比没有报错更难查。

    条件重复写在这里而不是让 render_body 多回一个标志位：render_body 的返回值
    被模板 qbody 直接当 Markup 用（index/recycle_bin/_attach_qcard 三处），
    改签名要牵动全部调用方；这里只多调一次纯函数，两边共用同一组判定。
    """
    if not text or qtype not in _SOLVE:
        return "小问分栏仅解答题支持"
    body, figs = _strip_imgs(text)
    if not figs:
        return "这道题没有图片，无法图文分栏"
    if exporter._split_stem_subs(body) is None:
        return "这道题没有分小问（（1）（2）…），无法小问分栏"
    # 分栏是把图挪进右栏，只有尾图能被这么挪。图真夹在题干文字中间（后面还有题干
    # 正文）时这道题没有可进右栏的图 —— 那张图就该留在原位，分栏无从谈起。
    plan = exporter.plan_figs(body, qtype, None, img_split)
    if not plan["has_tail"]:
        return "这道题的图夹在题干文字中间，已按原位排版，不能再挪进右栏分栏"
    if exporter.resolve_split(qtype, img_split, True) != "sub":
        return "小问分栏未生效"
    return None


def solve_split_applies(text: str, qtype: str = None, img_split=None) -> bool:
    """solve_split_applies 的 bool 版（保留给不关心原因的调用方）。"""
    return solve_split_reason(text, qtype, img_split) is None


def pair_applies(text: str, qtype: str = None) -> bool:
    """「四图配选项」在这道题上**实际能不能生效**（供路由预检用）。

    与 solve_split_applies 同一个理由：plan_figs 判不出 pair 时渲染静默回退成普通
    选项网格，版式没变而 chip 亮着，用户只看到「点了没反应」。这里传
    img_split="pair" 去问 plan_figs，问的正是渲染时那条判定。
    """
    if not text or qtype not in _CHOICE:
        return False
    body, _figs = _strip_imgs(text)
    return exporter.plan_figs(body, qtype, None, "pair")["pair"]


def fig_groups(text: str, qtype: str = None, img_layouts=None,
               img_split=None) -> list[dict]:
    """正文里的「连续两图」分组：`[{"ids": [1, 2], "row": True}, ...]`。

    只回多图组 —— 单图组谈不上并排/堆叠，前端据此决定「并排/堆叠」chip 显不显示。
    分组直接来自 exporter.plan_figs，与卡片正文、PDF 同源，故 chip 的亮灭与眼前
    那两张图到底并排没并排必然一致。
    模板（app.py 的 qfig_groups）与改完排版后回包的路由（routes/questions.py）都调
    这一个函数：否则一次拖动之后 chip 状态与正文就能对不上。
    """
    if not text:
        return []
    body, _figs = _strip_imgs(text)
    plan = exporter.plan_figs(body, qtype, img_layouts, img_split)
    return [{"ids": g["ids"], "row": bool(g["row"])}
            for g in plan["groups"] if len(g["ids"]) > 1]


def stack_group_of(text: str, index: int, img_layouts=None,
                   qtype: str = None, img_split=None) -> list[int] | None:
    """第 index 张图所在的「连续图组」序号列表（供并排/堆叠开关用）。

    组只有一张图时返回 None —— 那时并排/堆叠无从谈起，路由据此拒绝，理由同
    pair_applies：不拦住就是一个只改了库、页面毫无变化的开关。
    分组规则来自 exporter.plan_figs，与渲染同源。
    """
    if not text:
        return None
    body, _figs = _strip_imgs(text)
    plan = exporter.plan_figs(body, qtype, img_layouts, img_split)
    for s in plan["slots"]:
        if s["i"] == index:
            g = plan["groups"][s["group"]] if 0 <= s["group"] < len(plan["groups"]) else None
            return g["ids"] if g and len(g["ids"]) > 1 else None
    return None


def swap_image_refs(text: str, i: int, j: int) -> str | None:
    """把正文里第 i、j 个图片引用**互换位置**，返回新正文（越界返回 None）。

    只动这两处引用的文本，图片文件本身与其余正文一字不改。按出现序号计数而不是
    str.replace：同一张图在正文里出现两次时 replace 会把两处都改掉（同
    tikz_redraw._replace_ref 的取舍）。
    调用方还需同步交换 img_layouts / img_original 里的序号，见 db.swap_images。
    """
    refs = [m.group(0) for m in _QIMG_RE.finditer(text or "")]
    if i == j or not (0 <= i < len(refs)) or not (0 <= j < len(refs)):
        return None
    seen = [0]

    def _sub(m):
        cur = seen[0]
        seen[0] += 1
        if cur == i:
            return refs[j]
        if cur == j:
            return refs[i]
        return m.group(0)

    return _QIMG_RE.sub(_sub, text)


def _split_row(txt_html: str, figs: list[tuple[str, str]], unit: dict,
               layouts: dict, width=None) -> str:
    """图文左右两栏的那一行：左栏文字、右栏一个完整视觉图片组。

    两栏宽度按 exporter._split_fracs（同一条公式还在 image-layout.js 里重复了
    一份，见那边 splitFracs 的注释）。横排组取两图宽度之和，纵排组取最大宽度；
    不能再只拿第一张，否则第二张会掉到分栏下方。
    """
    ids = unit.get("ids") or []
    first = ids[0]
    w = exporter._split_unit_width(unit, layouts, width)
    txt_frac, img_frac = exporter._split_fracs(w)
    right = (f'<div class="q-split-img" data-split-lead="{first}" '
             f'data-unit-count="{len(ids)}" '
             f'style="flex:0 0 {img_frac * 100:.1f}%">'
             f'{_split_fig_unit_html(figs, unit, layouts)}</div>')
    return (f'<div class="q-split">'
            f'<div class="q-split-text" style="flex:0 0 {txt_frac * 100:.1f}%">'
            f'{txt_html}</div>{right}</div>')


def _choice_split_html(stem_html: str, grid: str, figs: list[tuple[str, str]],
                       layouts: dict, tail_ids: list[int], plan: dict,
                       full: bool = False, tail_html: str = "") -> str:
    """选择题图文分栏：左栏文字、右栏首个尾图视觉组，两栏宽度按 exporter._split_fracs。

    full=True 时题干也进左栏（整题左右对分），否则题干整行、只有选项与图分栏 ——
    与 exporter._place_choice_split 的两个模式一一对应。
    首个视觉组之外的其余尾图不参与分栏，照 plan 的分组拼在两栏之后；组内图片必须
    全部留在右栏并保留横排/纵排方向。原位图已在 stem_html 里就地排好。
    tail_html（选项附注）同样排在两栏之下整行宽，与导出侧 opt_tail 落位一致。
    """
    # full：题干进左栏（整题对分）；opts：题干整行在两栏之上
    left = stem_html if full else ""
    head = "" if full else stem_html
    unit, rest = exporter._split_first_unit(tail_ids, plan)
    row = _split_row(left + grid, figs, unit, layouts)
    return (head + row + tail_html
            + _figs_html(figs, layouts, rest, plan))


def render_solution(text: str, sol_img_layouts=None,
                    sol_img_split=None) -> Markup:
    """解析正文 → 结构化 HTML（页面卡片用），与 exporter._solution_body 同源。

    解析不参与四图配选项 / 选择题选项网格；但可用历史字段
    sol_img_split="full" 开启图文混排：文字先在图片侧边环绕，超过图片
    高度后自动恢复整行。图片宽度/对齐仍可逐图设置（sol_img_layouts，
    序号与题干 img_layouts 各自独立编号）。保留 "full" 取值是为了让旧题
    无需迁移即可获得新效果。

    与 render_body 一样，任何一步识别不出结构就退回「原样渲染」，不让卡片 500。
    """
    if not text:
        return Markup("")
    text = _stash_tables(exporter._strip_solution_leading_label(text))
    text = exporter._break_solution_lines(text)
    # PDF 需要空段来强制分段；网页的解析容器配合 pre-line 已能按单换行展示。
    # 若把 OCR 留下的双空行原样送进 q-stem，再叠加 KaTeX 的块边距，就会出现截图
    # 里那种“每一步之间空一大片”。这里只收紧 HTML 展示，不改题库正文或 PDF。
    text = re.sub(r"\n[ \t　]*\n+", "\n", text)
    layouts = exporter._parse_layouts(sol_img_layouts)
    body, figs = _strip_imgs(text)
    if not figs:
        return Markup(f'<div class="q-stem">{_drop_slots(_render_text(body))}</div>')
    plan = exporter.plan_figs(body, None, sol_img_layouts, sol_img_split)
    inline_ids = [s["i"] for s in plan["slots"] if s["pos"] == "stem"]
    tail_ids = [s["i"] for s in plan["slots"]
                if s["i"] not in set(inline_ids) and s["i"] < len(figs)]
    text_html = _stem_html(body, inline_ids, figs, layouts, plan)
    if sol_img_split == "full" and tail_ids:
        unit, rest = exporter._split_first_unit(tail_ids, plan)
        ids = unit.get("ids") or []
        first = ids[0]
        width = exporter._split_unit_width(unit, layouts)
        # 单图未自定义宽度时 _split_unit_width 会返回 None，让传统硬分栏继续沿用
        # 48/48；解析混排的控制条默认值却明确是 35%。此处补上解析自己的默认值，
        # 避免界面显示 35%、实际浮图却占 48%。
        if width is None:
            width = _DEFAULT_IMG_W
        _text_frac, img_frac = exporter._split_fracs(width)
        align = (layouts.get(first) or {}).get("align") or "right"
        # 混排必须浮到一侧才能让行框环绕；居中旧值沿用原“右图”
        # 语义，显式选“左”时则把图放左侧。宽度仍按原有百分比精确生效。
        side = "left" if align == "left" else "right"
        image = _split_fig_unit_html(figs, unit, layouts)
        flow = (f'<div class="q-solution-flow q-solution-flow-{side}">'
                f'<div class="q-solution-flow-img" data-split-lead="{first}" '
                f'data-unit-count="{len(ids)}" '
                f'style="width:{img_frac * 100:.1f}%">{image}</div>'
                f'{text_html}</div>')
        return Markup(flow
                      + _figs_html(figs, layouts, rest, plan))
    return Markup(text_html + _figs_html(figs, layouts, tail_ids, plan))


def _solve_split_html(stem_html: str, subs_html: str,
                      figs: list[tuple[str, str]], layouts: dict,
                      tail_ids: list[int], plan: dict) -> str:
    """解答题「小问分栏」：题干整行、小问（左栏）与首个尾图视觉组（右栏）左右分栏，
    两栏宽度按 exporter._split_fracs —— 与 exporter._place_solve_split 对应。
    其余尾图不参与分栏，照 plan 的分组拼在两栏之后（与 _choice_split_html 一致）。
    """
    unit, rest = exporter._split_first_unit(tail_ids, plan)
    return (stem_html + _split_row(subs_html, figs, unit, layouts)
            + _figs_html(figs, layouts, rest, plan))
