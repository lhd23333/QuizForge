"""新导入题目的图片布局默认规则。

这里只计算默认值，不读取 Flask 请求、配置文件或题库。调用方显式传入科目和
一图一选项判定函数，便于导入页、批量入库和离线测试复用同一套规则。
"""

import re
from collections.abc import Callable


QIMG_RE = re.compile(r"!\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")


def import_image_defaults(
    qtype: str,
    body: str,
    *,
    subject: str,
    pair_applies: Callable[[str, str], bool],
    requested_mode: str = "",
    requested_flow: str = "",
) -> tuple[str | None, list[dict], str]:
    """返回新导入题的 ``(图片位置, 逐图布局, 多图方向)``。"""
    image_count = len(QIMG_RE.findall(body or ""))
    if not image_count:
        return None, [], "column"
    allowed = {
        "单选题": ("pair", "opts", "full", "between", "after"),
        "多选题": ("pair", "opts", "full", "between", "after"),
        "解答题": ("sub", "full", "between", "after"),
        "填空题": ("full", "between", "after"),
    }.get(qtype, ())
    pair_default = (
        qtype in ("单选题", "多选题") and pair_applies(body, qtype)
    )
    if requested_mode in allowed:
        mode = requested_mode
    elif pair_default:
        mode = "pair"
    elif qtype in ("单选题", "多选题"):
        mode = "between" if image_count > 1 else "opts"
    elif qtype == "填空题":
        mode = "between" if subject == "physics" else "full"
    elif qtype == "解答题":
        mode = "after" if subject == "physics" else "sub"
    else:
        mode = None

    row_default = (
        (qtype in ("单选题", "多选题") and image_count > 1 and not pair_default)
        or (subject == "physics" and qtype in ("填空题", "解答题"))
    )
    flow = (
        requested_flow
        if requested_flow in ("row", "column")
        else "row" if row_default else "column"
    )

    # 一图一选项由专用网格控制，普通图片组的 stack/align 不应介入。
    if mode == "pair":
        return mode, [], flow
    lead: dict = {"i": 0}
    if subject == "physics" and qtype == "解答题":
        lead["align"] = "center"
    if image_count > 1 and flow == "column":
        lead["stack"] = True
    layouts = [lead] if len(lead) > 1 else []
    return mode, layouts, flow


def import_solution_image_defaults(
    solution: str,
) -> tuple[str | None, list[dict]]:
    """解析图片默认图文混排；多图作为一个纵向视觉组。"""
    image_count = len(QIMG_RE.findall(solution or ""))
    if not image_count:
        return None, []
    layouts = [{"i": 0, "stack": True}] if image_count > 1 else []
    return "full", layouts
