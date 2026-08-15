"""利用 MinerU 页面坐标恢复多图选择题的图片归属。

Markdown 只保留一维阅读顺序；两栏或 2x2 图片选项经 MinerU 展平后，常出现图片跑到
选项标签之前、题干示意图落到 D 项之后的情况。content_list.json 仍保留每个文本块和
图片的 ``page_idx + bbox``，本模块据此做保守的一对一匹配：优先使用 A-D 坐标锚点；
锚点也丢失时，只接受同页同规格且构成唯一规则四图组的布局，否则原文逐字不动。
"""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import mechfix


_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*images/([^\s)]+)\s*\)")
_LABEL_RE = re.compile(r"(?<![A-Za-z])(?P<label>[A-DＡ-Ｄ])\s*[.．、)]")
_WEAK_MATH_LABEL_RE = re.compile(
    r"[（(]\s*\$\s*(?:\\displaystyle\s*)?"
    r"(?P<label>[A-DＡ-Ｄ])\s*\$\s*[)）]"
)
_SOLUTION_RE = re.compile(
    r"(?m)^\s*(?:#{1,6}\s*)?(?:"
    r"【\s*(?:参考)?(?:答案|解析)[^】]*】|"
    r"(?:参考)?答案\s*[:：]|解析\s*[:：]|解答\s*[:：])"
)
_FULLWIDTH = str.maketrans("ＡＢＣＤ", "ABCD")


@dataclass(frozen=True)
class _Box:
    page: int
    bbox: tuple[float, float, float, float]

    @property
    def center(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    @property
    def size(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox
        return (max(1.0, x1 - x0), max(1.0, y1 - y0))


@dataclass(frozen=True)
class _Anchor:
    label: str
    page: int
    x: float
    y: float


@dataclass(frozen=True)
class _Layout:
    images: dict[str, _Box]
    anchors: tuple[_Anchor, ...]


def _norm_bbox(value) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        box = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    # VLM model.json 用 0—1，content_list 的其它后端多用 0—1000；统一后再算距离。
    if max(abs(item) for item in box) <= 1.5:
        box = tuple(item * 1000 for item in box)
    return box


def _anchors_from_text(text: str, page: int, bbox) -> list[_Anchor]:
    """在文本框内按字符位置近似标签坐标；独立标签和同一行 A/B/C/D 都适用。"""
    box = _norm_bbox(bbox)
    if not text or box is None:
        return []
    x0, y0, x1, y1 = box
    lines = text.splitlines() or [text]
    line_h = max(1.0, (y1 - y0) / len(lines))
    out = []
    for row, line in enumerate(lines):
        width = max(1, len(line))
        for match in _LABEL_RE.finditer(line):
            label = match.group("label").translate(_FULLWIDTH)
            x = x0 + (match.start() + 0.5) / width * (x1 - x0)
            y = y0 + (row + 0.5) * line_h
            out.append(_Anchor(label, page, x, y))
    return out


def load_layout(extract_dir: str | Path) -> _Layout | None:
    """读取 MinerU v4 解压出的 content_list；格式异常时返回 None。"""
    root = Path(extract_dir)
    candidates = [
        path for path in root.glob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    ]
    if not candidates:
        return None
    # 同名文件强制 OCR 重试时，目录里可能同时留着两轮随机 UUID 产物；UUID 的
    # 字典序与生成先后无关，必须按修改时间取最后一轮，否则会拿旧坐标修新 Markdown。
    latest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    try:
        rows = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None

    images: dict[str, _Box] = {}
    anchors: list[_Anchor] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            page = int(row.get("page_idx", 0))
        except (TypeError, ValueError):
            continue
        box = _norm_bbox(row.get("bbox"))
        kind = str(row.get("type", ""))
        # MinerU 会把曲线图、函数图像等视觉块标成 chart，但 Markdown 仍输出同样
        # 的 ``![](images/...)``。若只收 image，恰好四张图表选项会全部失去坐标，
        # 后续即使布局完全确定也无法恢复 A-D。
        if kind in {"image", "chart"} and box is not None:
            name = Path(str(row.get("img_path", ""))).name
            if name:
                images[name] = _Box(page, box)
        if kind in {"text", "title", "list"}:
            text = row.get("text")
            if not isinstance(text, str):
                text = "\n".join(str(x) for x in row.get("list_items", []) if x)
            anchors.extend(_anchors_from_text(text, page, row.get("bbox")))
    return _Layout(images, tuple(anchors)) if images else None


def _layout_box(path: str, layout: _Layout) -> _Box | None:
    """按 Markdown 图片路径查坐标，兼容双文件缓存的一层命名空间前缀。

    题干、解析合并时会把文件改名为 ``exam_<原名>`` / ``solution_<原名>``，但
    MinerU content_list 仍记录原名。精确键永远优先；只有精确键不存在时才剥一层
    已知前缀，并且原名在布局中必须唯一。绝不递归剥前缀，避免
    ``exam_exam_x.jpg`` 被误配到 ``x.jpg``。
    """
    name = Path(path).name
    exact = layout.images.get(name)
    if exact is not None:
        return exact
    match = re.match(r"^(?:exam|solution)_(.+)$", name)
    if match is None:
        return None
    original = match.group(1)
    matches = [box for key, box in layout.images.items()
               if Path(key).name == original]
    return matches[0] if len(matches) == 1 else None


def _point_box_distance(anchor: _Anchor, image: _Box) -> float:
    if anchor.page != image.page:
        return 5000.0
    x0, y0, x1, y1 = image.bbox
    dx = max(x0 - anchor.x, 0.0, anchor.x - x1)
    dy = max(y0 - anchor.y, 0.0, anchor.y - y1)
    cx, cy = image.center
    # 到矩形边缘的距离是主项；少量中心距离用于两个相邻图片等距时打破平局。
    return math.hypot(dx, dy) + 0.16 * math.hypot(anchor.x - cx, anchor.y - cy)


def _shape_penalty(images: tuple[_Box, ...]) -> float:
    """四个选项图通常同规格；用对数偏差排除尺寸明显不同的题干示意图。"""
    aspects = [math.log(w / h) for w, h in (image.size for image in images)]
    areas = [math.log(w * h) for w, h in (image.size for image in images)]
    med_a = sorted(aspects)[len(aspects) // 2]
    med_s = sorted(areas)[len(areas) // 2]
    return 55 * sum(abs(value - med_a) for value in aspects) + \
        35 * sum(abs(value - med_s) for value in areas)


def _best_assignment(paths: list[str], layout: _Layout) -> dict[str, str] | None:
    """返回 A-D → 图片文件名；低置信度时不猜。"""
    available = [(path, _layout_box(path, layout)) for path in paths]
    available = [(path, box) for path, box in available if box is not None]
    if len(available) < 4:
        return None

    labels = "ABCD"
    best = None
    for page in sorted({box.page for _, box in available}):
        page_images = [(path, box) for path, box in available if box.page == page]
        if len(page_images) < 4:
            continue
        by_label = {}
        cx = sum(box.center[0] for _, box in page_images) / len(page_images)
        cy = sum(box.center[1] for _, box in page_images) / len(page_images)
        for label in labels:
            choices = [a for a in layout.anchors if a.page == page and a.label == label]
            choices.sort(key=lambda a: math.hypot(a.x - cx, a.y - cy))
            by_label[label] = choices[:3]
        if any(not by_label[label] for label in labels):
            continue

        for anchors in itertools.product(*(by_label[label] for label in labels)):
            # 四个标签必须来自同一局部区域；混到同页另一道题时跨度会明显超过图片组。
            ax = [anchor.x for anchor in anchors]
            ay = [anchor.y for anchor in anchors]
            if max(ax) - min(ax) > 950 or max(ay) - min(ay) > 650:
                continue
            for chosen in itertools.permutations(page_images, 4):
                boxes = tuple(item[1] for item in chosen)
                score = sum(_point_box_distance(anchor, box)
                            for anchor, box in zip(anchors, boxes))
                score += _shape_penalty(boxes)
                if best is None or score < best[0]:
                    best = (score, chosen)
    if best is None or best[0] / 4 > 235:
        return None
    return {label: item[0] for label, item in zip(labels, best[1])}


def _reading_order_assignment(paths: list[str], layout: _Layout) -> dict[str, str] | None:
    """把同一组选项图按视觉行、行内从左到右映射为 A-D。

    Markdown 整组漏掉 A-D 时，content_list 里的单字母锚点容易与同页其他题的数学
    字母混淆；但四张候选图本身的二维阅读顺序仍稳定。这里只接收恰好四张已有 bbox
    的图，先按纵坐标聚成视觉行，再按横坐标排序。
    """
    items = [(path, _layout_box(path, layout)) for path in paths]
    if len(items) != 4 or any(box is None for _, box in items):
        return None
    if len({box.page for _, box in items}) != 1:
        return None
    heights = sorted(box.size[1] for _, box in items)
    row_tolerance = max(20.0, 0.45 * (heights[1] + heights[2]) / 2)
    rows: list[list[tuple[str, _Box]]] = []
    for item in sorted(items, key=lambda pair: pair[1].center[1]):
        cy = item[1].center[1]
        if not rows:
            rows.append([item])
            continue
        mean_y = sum(box.center[1] for _, box in rows[-1]) / len(rows[-1])
        if abs(cy - mean_y) <= row_tolerance:
            rows[-1].append(item)
        else:
            rows.append([item])
    ordered = [item for row in rows
               for item in sorted(row, key=lambda pair: pair[1].center[0])]
    return {label: item[0] for label, item in zip("ABCD", ordered)}


def _uniform_visual_assignment(
        paths: list[str], layout: _Layout) -> dict[str, str] | None:
    """从四张图的规则视觉布局恢复 A-D；尺寸或排列含糊时拒绝。

    这条路径没有 A-D 坐标锚点可依赖，所以判据刻意比
    :func:`_reading_order_assignment` 严格：四图不仅要同页，还要近似同规格，并且只能
    是一行四图、两行两图或一列四图。这样不会把散落的题干示意图凑成选项。
    """
    items = [(path, _layout_box(path, layout)) for path in paths]
    if len(items) != 4 or any(box is None for _, box in items):
        return None
    boxes = [box for _, box in items]
    if len({box.page for box in boxes}) != 1:
        return None

    widths = [box.size[0] for box in boxes]
    heights = [box.size[1] for box in boxes]
    areas = [width * height for width, height in zip(widths, heights)]
    # 真实 MinerU 裁边会有小幅差异，1.35 能覆盖同组选项；明显扁长/放大的题干图
    # 会越界。面积再设一层上限，防止宽高偏差同时贴着边界通过。
    if (max(widths) / min(widths) > 1.35
            or max(heights) / min(heights) > 1.35
            or max(areas) / min(areas) > 1.65):
        return None

    median_width = sorted(widths)[2]
    median_height = sorted(heights)[2]
    row_tolerance = max(20.0, 0.35 * median_height)
    rows: list[list[tuple[str, _Box]]] = []
    for item in sorted(items, key=lambda pair: pair[1].center[1]):
        cy = item[1].center[1]
        if not rows:
            rows.append([item])
            continue
        mean_y = sum(box.center[1] for _, box in rows[-1]) / len(rows[-1])
        if abs(cy - mean_y) <= row_tolerance:
            rows[-1].append(item)
        else:
            rows.append([item])

    row_sizes = [len(row) for row in rows]
    if row_sizes == [4]:
        if max(box.center[1] for box in boxes) - min(
                box.center[1] for box in boxes) > row_tolerance:
            return None
    elif row_sizes == [2, 2]:
        top = sorted(rows[0], key=lambda pair: pair[1].center[0])
        bottom = sorted(rows[1], key=lambda pair: pair[1].center[0])
        column_tolerance = max(25.0, 0.45 * median_width)
        if any(abs(top[index][1].center[0] - bottom[index][1].center[0])
               > column_tolerance for index in range(2)):
            return None
    elif row_sizes == [1, 1, 1, 1]:
        if max(box.center[0] for box in boxes) - min(
                box.center[0] for box in boxes) > max(
                    25.0, 0.45 * median_width):
            return None
    else:
        return None

    ordered = [item for row in rows
               for item in sorted(row, key=lambda pair: pair[1].center[0])]
    return {label: item[0] for label, item in zip("ABCD", ordered)}


def _unique_visual_assignment(
        paths: list[str], layout: _Layout) -> dict[str, str] | None:
    """找唯一规则四图组选项；五图以上只有能唯一排除题干图时才返回。"""
    if len(paths) < 4:
        return None
    # 任何引用缺坐标都意味着还有未知候选，不能宣称已唯一排除题干图。
    if any(_layout_box(path, layout) is None for path in paths):
        return None
    candidates: list[dict[str, str]] = []
    seen: set[frozenset[str]] = set()
    for chosen in itertools.combinations(paths, 4):
        assignment = _uniform_visual_assignment(list(chosen), layout)
        if assignment is None:
            continue
        key = frozenset(assignment.values())
        if key not in seen:
            seen.add(key)
            candidates.append(assignment)
    return candidates[0] if len(candidates) == 1 else None


def _markdown_labels(text: str) -> dict[str, re.Match] | None:
    """找一组按 A→B→C→D 出现且窗口最短的 Markdown 选项标签。"""
    hits = {label: [] for label in "ABCD"}
    for match in _LABEL_RE.finditer(text):
        hits[match.group("label").translate(_FULLWIDTH)].append(match)
    if any(not hits[label] for label in "ABCD"):
        return None
    best = None
    for a in hits["A"]:
        for b in (item for item in hits["B"] if item.start() > a.end()):
            for c in (item for item in hits["C"] if item.start() > b.end()):
                for d in (item for item in hits["D"] if item.start() > c.end()):
                    score = d.end() - a.start()
                    if best is None or score < best[0]:
                        best = (score, {"A": a, "B": b, "C": c, "D": d})
    return best[1] if best else None


def _weak_math_labels(text: str) -> dict[str, re.Match] | None:
    """识别 MinerU 常见的 ``($\\displaystyle A$)`` 弱选项标签。"""
    matches = list(_WEAK_MATH_LABEL_RE.finditer(text))
    labels = [match.group("label").translate(_FULLWIDTH) for match in matches]
    if labels != list("ABCD"):
        return None
    return dict(zip(labels, matches))


def repair_block(text: str, layout: _Layout) -> tuple[str, bool]:
    """修复一道题；解析区图片完全不参与，避免把解题步骤图挪进选项。"""
    solution = _SOLUTION_RE.search(text)
    cut = solution.start() if solution else len(text)
    question, tail = text[:cut], text[cut:]
    labels = _markdown_labels(question)
    weak_labels = _weak_math_labels(question) if labels is None else None
    refs = list(_IMG_RE.finditer(question))
    if len(refs) < 4:
        return text, False

    paths = [match.group(2) for match in refs]
    if weak_labels is not None and len(refs) == 4:
        # 强制 OCR 常把四张图先展平，再输出 ``($A$)…($D$)``；标签与图片虽在
        # Markdown 中错位，content_list 的二维坐标仍可靠。四图、空答题括号、
        # A-D 恰好各一次三个条件同时成立才重建，避免把概率事件字母当作选项。
        if not mechfix.has_choice_answer_blank(question):
            return text, False
        assignment = _reading_order_assignment(paths, layout)
        if assignment is None:
            return text, False
        start = min(refs[0].start(), weak_labels["A"].start())
        clean = question[:start].rstrip()
        for label in "ABCD":
            clean += (f"\n\n({label})\n\n"
                      f"![选项{label}](images/{assignment[label]})")
        return clean.rstrip() + ("\n\n" if tail else "") + tail.lstrip(), True

    assignment = _best_assignment(paths, layout)
    if (assignment is None and labels is not None
            and mechfix.has_choice_answer_blank(question)):
        # 标签文本框可能在 content_list 中缺失，或被排在图片下方导致距离评分拒绝；
        # Markdown 已有完整 A-D 时，唯一的规则四图组足以安全兜底。五图以上也必须
        # 只有一个候选四元组，才能断言其余图片是题干图。
        assignment = _unique_visual_assignment(paths, layout)
    if (assignment is None and labels is None
            and mechfix.has_choice_answer_blank(question)):
        # Markdown 连 A-D 都丢失时，只能接受更严格的同规格规则布局；恰好四图不再
        # 依赖 content_list 里的文字锚点，五图以上则要求唯一排除题干图。
        assignment = _unique_visual_assignment(paths, layout)
    if assignment is None:
        return text, False
    option_paths = set(assignment.values())
    stem_refs = [match.group(0) for match in refs if match.group(2) not in option_paths]

    if labels is None:
        # MinerU 有时把 A-D 标签留在 content_list 坐标层，却从 Markdown 正文中整组
        # 漏掉。必须同时有空答题括号和可靠坐标分配才补标签；题干另有示意图时还要求
        # 至少五张图，避免拿题干图冒充缺失的某个选项。
        if not mechfix.has_choice_answer_blank(question):
            return text, False
        has_stem_caption = bool(re.search(r"(?:正视图|主视图|俯视图|题干图)", question))
        if has_stem_caption and len(refs) < 5:
            return text, False
        reading_order = _reading_order_assignment(list(option_paths), layout)
        if reading_order is None:
            return text, False
        assignment = reading_order
        option_paths = set(assignment.values())
        stem_refs = [match.group(0) for match in refs
                     if match.group(2) not in option_paths]
        clean = _IMG_RE.sub("", question)
        clean = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", clean).rstrip()
        semantic = [re.sub(r"!\[[^\]]*\]", "![题干图]", ref, count=1)
                    for ref in stem_refs]
        if semantic:
            clean += "\n\n" + "\n\n".join(semantic)
        for label in "ABCD":
            # 用括号标签而不是行末 ``A.``：后续句号清洗会删除行末点号，而 ``(A)``
            # 能稳定穿过机械层，再由题型专属排版统一成 ``$\displaystyle A.$``。
            clean += (f"\n\n({label})\n\n"
                      f"![选项{label}](images/{assignment[label]})")
        return clean.rstrip() + ("\n\n" if tail else "") + tail.lstrip(), True

    # 先移走题目区全部图片，再按语义重插；解析区不动。alt 写入临时语义提示，既帮助
    # 文本 LLM 保持归属，最终转 Obsidian 双链时又会被拦截器安全丢弃。
    clean = _IMG_RE.sub("", question)
    clean = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", clean)
    labels = _markdown_labels(clean)
    if labels is None:
        return text, False
    for label in reversed("ABCD"):
        match = labels[label]
        ref = f"![选项{label}](images/{assignment[label]})"
        clean = clean[:match.end()] + "\n\n" + ref + clean[match.end():]
    labels = _markdown_labels(clean)
    if stem_refs and labels is not None:
        semantic = [re.sub(r"!\[[^\]]*\]", "![题干图]", ref, count=1)
                    for ref in stem_refs]
        pos = labels["A"].start()
        clean = clean[:pos].rstrip() + "\n\n" + "\n\n".join(semantic) + \
            "\n\n" + clean[pos:].lstrip()
    return clean.rstrip() + ("\n\n" if tail else "") + tail.lstrip(), True


def repair_document(md_text: str, extract_dir: str | Path) -> tuple[str, int]:
    """逐题修复文档，返回（新文本，修复题数）；没有坐标文件时零改动。"""
    layout = load_layout(extract_dir)
    if layout is None or not md_text:
        return md_text, 0
    # 延迟导入以避免 converter → imgorder → blocksplit 在模块加载期形成环。
    import blocksplit
    import collection_structure

    # 合集每个单元都会从第 1 题重新编号。若直接把整本书交给 split_blocks，第二次
    # 从 1 重启常被判成答案区，后面几十个单元的图片修复根本不会运行。先复用合集
    # 自己的结构分组；普通试卷没有至少两个可靠标题，按原来的整篇路径处理。
    try:
        units = collection_structure.split_markdown_units(
            md_text, label="图片坐标恢复")
        chunks = [unit.markdown for unit in units]
    except collection_structure.CollectionStructureError:
        chunks = [md_text]

    repaired = md_text
    count = 0
    cursor = 0
    for chunk in chunks:
        for block in blocksplit.split_blocks(chunk):
            new_text, changed = repair_block(block.text, layout)
            if not changed:
                continue
            position = repaired.find(block.text, cursor)
            if position < 0:
                # 同一题块若已被前一步包含式替换，绝不能回头误改前面内容相同的题。
                continue
            repaired = (repaired[:position] + new_text
                        + repaired[position + len(block.text):])
            cursor = position + len(new_text)
            count += 1
    return repaired, count
