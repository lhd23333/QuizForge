"""OCR 后的多单元合集分组与题解配对。

这一层处理的是原始 Markdown，不是 PDF 页码。合集里的新单元经常
从页面中部开始，整页切 PDF 会丢内容或重复内容；OCR 后按文本边界切，
则能原样保留数学式与图片引用。

分组不把“精练”写死：优先找含中文序号的结构标题，再用题号是否
从 1 重开且基本连续来确认边界。标题候选不足时，还可由多组高度重合的
完整题号序列保守确认边界；“一、单选题”之类卷内大题标题只是分区，
不能将一份试卷拦腰切开。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import re
import unicodedata


class CollectionStructureError(ValueError):
    """合集文本无法唯一、安全分组时的用户可读错误。"""


@dataclass(frozen=True)
class MarkdownUnit:
    """一个从结构标题开始的 Markdown 单元。"""

    title: str
    topic: str
    ordinal: int | None
    markdown: str
    start_line: int
    question_numbers: tuple[int, ...]
    number_reset: bool = False
    generated_title: bool = False


@dataclass(frozen=True)
class MarkdownPair:
    """题干单元与可选解析单元。"""

    title: str
    exam: MarkdownUnit
    solution: MarkdownUnit | None


@dataclass(frozen=True)
class _TitleCandidate:
    line_index: int
    title: str
    topic: str
    ordinal: int | None
    strong: bool


_CN_NUMBER = "〇零一二三四五六七八九十百两壹贰叁肆伍陆柒捌玖拾佰"
_CN_NUMBER_RE = rf"[{_CN_NUMBER}]+"
_MD_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_LEADING_MARK_RE = re.compile(r"^\s*(?:>\s*)?(?:\*\*|__)?\s*")
_PRIORITY_MARK_RE = re.compile(r"^[★☆]+\s*")
_TRAILING_MARK_RE = re.compile(r"\s*(?:\*\*|__)?\s*$")
_SOLUTION_TAIL_RE = re.compile(
    # 外书名号后的“参考答案”是明确后缀；无书名号时只剔“参考答案”
    # 或前面有空白/分隔符的答案词。不能把题名“图像解析”末尾的“解析”
    # 当成文件后缀删掉。
    r"(?:[>》]\s*(?:参考)?(?:答案|解析|详解|解答)(?:与解析)?|"
    r"参考答案|\s+(?:答案解析|解析|详解|解答|答案))\s*$"
)

# 强标题：“精练十六：……”、“第十六讲 ……”。前缀不限于“精练”，
# 只要它是一个简短中文标题并且序号后有明确分隔符即可。
_PREFIX_TITLE_RE = re.compile(
    rf"^(?P<prefix>[\u3400-\u9fffA-Za-z]{{1,16}}?)(?P<num>{_CN_NUMBER_RE})"
    r"\s*[:：]\s*(?P<topic>.+)$"
)
_UNIT_TITLE_RE = re.compile(
    rf"^(?P<di>第)?\s*(?P<num>{_CN_NUMBER_RE})\s*"
    r"(?P<unit>套|卷|章|节|讲|练|单元|专题|部分|篇|回|次)"
    r"\s*(?:(?P<sep>[:：、.．\-])\s*)?(?P<topic>.*)$"
)
# 教辅专题常把阿拉伯序号放在结构词之后，例如“重难专题 16 圆锥曲线”。
# 这与“突破 1”之类卷内小节不同：这里只接受专题/单元/章/讲等稳定结构词，
# 并要求序号后仍有主题正文，因此不会把普通题号或小问标题当成合集边界。
_TRAILING_ARABIC_UNIT_TITLE_RE = re.compile(
    r"^(?P<prefix>[\u3400-\u9fffA-Za-z]{0,16}?)"
    r"(?P<unit>专题|单元|章|节|讲|套|卷)\s*"
    r"(?P<num>\d{1,3})\s*"
    r"(?:(?P<sep>[:：、.．\-])\s*)?(?P<topic>.+)$"
)
# 弱标题：“一、运动学基础”。这种写法也用于卷内题型分区，所以只在
# 全文找不到至少两个强标题时启用，并排除常见题型名。
_ORDINAL_TITLE_RE = re.compile(
    rf"^(?P<num>{_CN_NUMBER_RE})\s*[、.．:：]\s*(?P<topic>.+)$"
)
_GENERIC_SOLUTION_TITLE_RE = re.compile(
    rf"^[\u3400-\u9fffA-Za-z]{{1,24}}?(?P<num>{_CN_NUMBER_RE})\s*"
    r"(?:参考)?(?:答案|解析|详解|解答)(?:与解析)?\s*[:：]?$"
)
_QUESTION_SECTION_RE = re.compile(
    r"(?:单项|多项|不定项|单选|多选|选择|填空|实验|判断|"
    r"计算|解答|简答|非选择|作图|证明|答案|解析)题?(?:部分)?"
    r"(?:\s*[（(][^）)\n]{0,120}[）)])?\s*$"
)
_COVER_META_RE = re.compile(
    r"^(?:[（(]?(?:考察|考试)范围|[（(]?自测(?:时间|限时)|"
    r"学校|姓名|班级|总分|自评总分)\s*[:：]"
)
_FULLWIDTH_QUESTION_NUMBER_RE = re.compile(r"^(\d{1,3})\s*[．、]")
_QUESTION_NUMBER_RES = (
    # _plain_line 会把全角句点正规化成半角点，因此全角题号在正规化前
    # 由上面的专用表达式识别；这里的半角点继续排除 1.5 这类小数。
    re.compile(r"^(\d{1,3})\s*\.(?!\d)"),
    # 汉字正文紧跟“题”时不存在英文单词边界，不能用 \b。
    re.compile(r"^第\s*(\d{1,3})\s*[题題]"),
    re.compile(rf"^第\s*({_CN_NUMBER_RE})\s*[题題]"),
)


def _cn_to_int(value: str) -> int | None:
    """中文序号转整数，支持本功能需要的 1—999。"""
    text = unicodedata.normalize("NFKC", value or "")
    text = text.translate(str.maketrans({
        "两": "二", "〇": "零", "壹": "一", "贰": "二", "叁": "三",
        "肆": "四", "伍": "五", "陆": "六", "柒": "七", "捌": "八",
        "玖": "九", "拾": "十", "佰": "百",
    }))
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if not text:
        return None
    if "百" not in text and "十" not in text:
        result = 0
        for char in text:
            if char not in digits:
                return None
            result = result * 10 + digits[char]
        return result
    total = 0
    current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char == "百":
            total += (current or 1) * 100
            current = 0
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        else:
            return None
    return total + current


def _plain_line(line: str) -> str:
    value = unicodedata.normalize("NFKC", line or "")
    value = _MD_HEAD_RE.sub("", value)
    value = _LEADING_MARK_RE.sub("", value)
    # 教辅常在难题题号前加“★/★★”。它们只是难度标记，不属于题号，
    # 若保留会让整本合集误判为大量漏题，并阻断后续有界局部恢复。
    value = _PRIORITY_MARK_RE.sub("", value)
    value = _TRAILING_MARK_RE.sub("", value)
    return " ".join(value.strip().split())


def _without_solution_tail(value: str) -> str:
    text = _SOLUTION_TAIL_RE.sub("", value or "").strip()
    if text.startswith("《") and text.endswith("》"):
        text = text[1:-1].strip()
    else:
        text = text.strip("《》 ")
    return text


def _candidate(line: str, line_index: int) -> _TitleCandidate | None:
    title = _without_solution_tail(_plain_line(line))
    if not title or len(title) > 120:
        return None
    match = _PREFIX_TITLE_RE.match(title)
    if match:
        topic = match.group("topic").strip()
        if topic:
            return _TitleCandidate(
                line_index, title, topic, _cn_to_int(match.group("num")), True)
    match = _UNIT_TITLE_RE.match(title)
    if match:
        # “一次速度减为 0”是普通正文，不是“第一次……”的单元标题。
        # “次/回”即使带“第”也可能只是“第一次摆到最高点……”这类正文；
        # 只有整行到序号结束，或后面带冒号/顿号等明确分隔符才算标题。
        if not match.group("di") and match.group("unit") in ("次", "回"):
            return None
        topic = (match.group("topic") or "").strip()
        if (match.group("unit") in ("次", "回")
                and topic and not match.group("sep")):
            return None
        if (match.group("unit") == "部分"
                and _QUESTION_SECTION_RE.search(topic)):
            return None
        # “第一章”即使没有额外副标题也是完整结构标题。
        semantic = (match.group("unit") + topic).strip()
        return _TitleCandidate(
            line_index, title, semantic, _cn_to_int(match.group("num")), True)
    match = _TRAILING_ARABIC_UNIT_TITLE_RE.match(title)
    if match:
        topic = match.group("topic").strip()
        semantic = (match.group("prefix") + match.group("unit") + topic).strip()
        return _TitleCandidate(
            line_index, title, semantic, int(match.group("num")), True)
    match = _ORDINAL_TITLE_RE.match(title)
    if match:
        topic = match.group("topic").strip()
        if topic and not _QUESTION_SECTION_RE.search(topic):
            return _TitleCandidate(
                line_index, title, topic, _cn_to_int(match.group("num")), False)
    return None


def _title_candidates(lines: list[str]) -> list[_TitleCandidate]:
    candidates: list[_TitleCandidate] = []
    fenced = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        hit = _candidate(line, index)
        if hit is not None:
            candidates.append(hit)
    strong = [item for item in candidates if item.strong]
    return strong if len(strong) >= 2 else candidates


def _question_number(line: str) -> int | None:
    # 中文资料常写成“1．2021 年……”。全角句点是明确的题号符，不能在
    # NFKC 后把它与小数点混为一谈。
    raw_value = _MD_HEAD_RE.sub("", line or "")
    raw_value = _LEADING_MARK_RE.sub("", raw_value)
    raw_value = _PRIORITY_MARK_RE.sub("", raw_value)
    raw_match = _FULLWIDTH_QUESTION_NUMBER_RE.match(raw_value.strip())
    if raw_match:
        return int(raw_match.group(1))
    value = _plain_line(line)
    for index, pattern in enumerate(_QUESTION_NUMBER_RES):
        match = pattern.match(value)
        if not match:
            continue
        raw = match.group(1)
        return _cn_to_int(raw) if index == 2 else int(raw)
    return None


def _number_evidence(markdown: str) -> tuple[tuple[int, ...], bool]:
    """返回题号序列及“能确认是新单元”。

    判据取集合而不强求 OCR 阅读顺序：多栏页可能识别为
    ``1..11,14,15,12,13``，但题号本身仍完整。允许最多 20% 的少量缺号，
    可以容忍 OCR 吃掉一个题号，却不会把正文里的零散数字当成分组证据。
    """
    numbers: list[int] = []
    fenced = False
    for line in (markdown or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if stripped.startswith("|"):
            # 解析册常把选择题答案压成“题号 | 1 | 2 | …”表，表内没有
            # 普通的“1．”题号行。只接受明确题号表头，普通数据表仍忽略。
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells and re.sub(r"\s+", "", cells[0]) in ("题号", "题目"):
                for cell in cells[1:]:
                    hit = re.fullmatch(r"\s*(\d{1,3})\s*", cell)
                    if hit:
                        numbers.append(int(hit.group(1)))
            continue
        number = _question_number(line)
        if isinstance(number, int) and 1 <= number <= 300:
            numbers.append(number)
    unique = sorted(set(numbers))
    # 自动边界必须明确包含第 1 题。若 OCR 恰好吃掉第 1 题号，宁可让
    # 整本进入人工处理，也不能把正文小标题后的 2..N 静默误切成新组。
    if len(unique) < 2 or unique[0] != 1:
        return tuple(numbers), False
    expected = unique[-1] - unique[0] + 1
    if expected < 2 or unique[-1] > 300:
        return tuple(numbers), False
    coverage = len(unique) / expected
    return tuple(numbers), coverage >= 0.80


def _recovery_number_evidence(numbers: tuple[int, ...]) -> bool:
    """仅判断强标题分段是否足以启动有界局部恢复。

    这不是最终分组门：正式分组仍要求 80% 覆盖率，恢复完成后还会重新走
    ``split_markdown_units``。这里要求明确的第 1 题、至少五个不同题号且覆盖
    不低于 60%，只为让“题号已被 OCR 吃掉”的单元能够进入版面裁片恢复；
    不能把这个宽限用于配对或入库。
    """
    unique = sorted(set(numbers))
    if len(unique) < 5 or unique[0] != 1:
        return False
    expected = unique[-1]
    return expected >= 5 and len(unique) / expected >= 0.60


def _candidate_key(candidate: _TitleCandidate) -> str:
    value = unicodedata.normalize("NFKC", candidate.title).lower()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value)


def _confirmed_candidates(lines: list[str],
                           candidates: list[_TitleCandidate],
                           label: str, *, recovery_only: bool = False
                           ) -> list[_TitleCandidate]:
    """滤掉目录标题和重复栏眉，只留下有题号证据的正文边界。

    同名候选连续出现时看作一组：目录项恰好紧挨正文标题时取最后一个
    有效候选；正文标题在后续页重复时，后一个通常从中间题号继续，因而
    不会取代真正边界。正文开始后的异名无证据候选仍报错，避免静默吞组。
    """
    # 正文里的“步骤一：……／方法一：……”也满足宽泛的中文标题形式。
    # 它若恰好夹在第 1 题和第 2 题之间，按原候选逐段验证会让真正标题
    # 只剩一题而失效，并把这个小标题误当新单元，静默丢掉第 1 题。
    # 这里不写死“步骤/方法”等词，而只折叠一种可证明的续接形态：前一
    # 候选到它之间只有题号 1，后面则从 2 开始且基本连续，并且这个
    # 小标题序号没有向前推进。若前段本身已有 1..N 的完整证据，或后置
    # 标题序号正常前进，则不折叠；缺第 1 题时会按歧义停止，而不是把
    # 两个真正单元静默合并。
    candidates = list(candidates)
    while len(candidates) >= 2:
        removed = False
        for index in range(1, len(candidates)):
            previous = candidates[index - 1]
            current = candidates[index]
            following_line = (candidates[index + 1].line_index
                              if index + 1 < len(candidates) else len(lines))
            before_numbers, before_confirmed = _number_evidence(
                "".join(lines[previous.line_index:current.line_index]))
            after_numbers, _ = _number_evidence(
                "".join(lines[current.line_index:following_line]))
            before_unique = sorted(set(before_numbers))
            after_unique = sorted(set(after_numbers))
            after_expected = ((after_unique[-1] - after_unique[0] + 1)
                              if after_unique else 0)
            after_is_continuation = (
                len(after_unique) >= 2 and after_unique[0] == 2
                and len(after_unique) / max(1, after_expected) >= 0.80)
            ordinal_does_not_advance = (
                previous.ordinal is not None and current.ordinal is not None
                and current.ordinal <= previous.ordinal)
            if (not before_confirmed and before_unique == [1]
                    and after_is_continuation and ordinal_does_not_advance):
                del candidates[index]
                removed = True
                break
        if not removed:
            break

    runs: list[list[_TitleCandidate]] = []
    for candidate in candidates:
        if runs and _candidate_key(runs[-1][0]) == _candidate_key(candidate):
            runs[-1].append(candidate)
        else:
            runs.append([candidate])

    assessed: list[tuple[list[_TitleCandidate], _TitleCandidate | None]] = []
    for index, run in enumerate(runs):
        end = (runs[index + 1][0].line_index
               if index + 1 < len(runs) else len(lines))
        valid: list[tuple[_TitleCandidate, tuple[int, ...]]] = []
        for candidate in run:
            chunk = "".join(lines[candidate.line_index:end])
            numbers, confirmed = _number_evidence(chunk)
            if not confirmed and recovery_only:
                confirmed = _recovery_number_evidence(numbers)
            if confirmed:
                valid.append((candidate, numbers))
        # 若栏眉恰好重复在第 2 题前，后一个候选也会因“允许 OCR 漏掉
        # 第 1 题号”而有效；此时必须优先保留包含题号 1 的早候选，不能
        # 取最后一个而丢掉整道第 1 题。多个候选都含 1 时才取最后一个，
        # 用来跳过紧挨正文标题、文字完全相同的目录项。
        with_first = [item for item in valid if 1 in item[1]]
        chosen = ((with_first or valid)[-1][0] if valid else None)
        assessed.append((run, chosen))

    confirmed_indices = [i for i, (_, item) in enumerate(assessed) if item]
    if len(confirmed_indices) < 2:
        raise CollectionStructureError(
            f"「{label}」没有找到至少两个由基本连续题号确认的中文结构标题")
    first = confirmed_indices[0]
    for run, item in assessed[first:]:
        if item is None:
            raise CollectionStructureError(
                f"「{label}」正文中的候选标题“{run[0].title}”后没有检出"
                "从 1 开始且基本连续的题号，无法确认这是新分组")
    chosen = [item for _, item in assessed if item is not None]
    if recovery_only:
        # 宽限只能建立在整段强标题序号严格连续之上。正文里的“方法一：…”、
        # “步骤二：…”即便后面碰巧出现题号，也会破坏这一序列并在此停止。
        if (not all(item.strong and item.ordinal is not None for item in chosen)
                or any(right.ordinal != left.ordinal + 1
                       for left, right in zip(chosen, chosen[1:]))):
            raise CollectionStructureError(
                f"「{label}」的强结构标题序号不连续，不能放宽题号覆盖率启动恢复")
    return chosen


def _explicit_title_immediately_before(
        lines: list[str], start: int,
        first_question: int) -> tuple[int, str, bool] | None:
    """读取新第 1 题前连续的标题链，不跨过普通非空正文。

    普通正文、选项也常是独立一行；若把它们当标题，会从上一题末尾偷走
    内容。因此一遇普通非空行就停止。允许“试卷标题→选择题→第 1 题”
    这种连续标题链，并取前面的试卷标题；若链里只有题型标题，返回值
    第三项为真，由调用方拒绝把卷内换题型误判成新试卷。
    """
    nearest_section: tuple[int, str, bool] | None = None
    for line_index in range(first_question - 1, start, -1):
        raw = lines[line_index].strip()
        if not raw:
            continue
        is_heading = bool(re.match(r"^\s{0,3}#{1,6}\s+\S", raw))
        is_bold_title = bool(re.fullmatch(
            r"\s*(?:\*\*[^*]+\*\*|__[^_]+__)\s*", raw))
        if not (is_heading or is_bold_title):
            return nearest_section
        title = _without_solution_tail(_plain_line(raw))
        if (not title or len(title) > 120
                or _question_number(raw) is not None):
            return nearest_section
        is_section = bool(_QUESTION_SECTION_RE.search(title))
        if not is_section:
            return line_index, title, False
        if nearest_section is None:
            nearest_section = (line_index, title, True)
    return nearest_section


def _ordinary_reset_title(lines: list[str], start: int,
                          first_question: int) -> tuple[int, str] | None:
    """返回紧邻新第 1 题的普通标题；题型分区不作为试卷名。"""
    nearby = _explicit_title_immediately_before(lines, start, first_question)
    if nearby is not None and not nearby[2]:
        return nearby[0], nearby[1]

    # 附卷封面常在 H1 试卷名与“一、单选题”之间放学校/姓名/限时等短字段，
    # 有时第 1 题前还有一张题图。它们会让上面的“只跨标题和空行”搜索停住。
    # 这里只在最多 20 行的封面窗口内跨过白名单字段、题型标题和图片，遇到任意
    # 普通正文立即停止；因此不会越过上一题正文偷取更早标题。
    lower = max(start + 1, first_question - 20, 0)
    for line_index in range(first_question - 1, lower - 1, -1):
        raw = lines[line_index].strip()
        if not raw:
            continue
        title = _without_solution_tail(_plain_line(raw))
        is_heading = bool(re.match(r"^\s{0,3}#{1,6}\s+\S", raw))
        is_bold_title = bool(re.fullmatch(
            r"\s*(?:\*\*[^*]+\*\*|__[^_]+__)\s*", raw))
        if is_heading or is_bold_title:
            if (_question_number(raw) is None and title
                    and not _QUESTION_SECTION_RE.search(title)):
                return line_index, title
            continue
        if (_COVER_META_RE.match(title)
                or re.fullmatch(r"!\[[^]]*]\([^)]*\)", raw)):
            continue
        break
    return None


def _split_exam_by_number_resets(raw_markdown: str, *, label: str,
                                 minimum_coverage: float = 0.85,
                                 internal: bool = False
                                 ) -> list[MarkdownUnit]:
    """无可靠序号标题时，以多组完整题号重置保守拆分题干合集。

    单个 ``1.`` 也可能来自 OCR 重复或题内步骤，不能单独证明新试卷。
    因而每组都必须从 1 开始、至少有五个不同题号且达到调用方指定覆盖率，
    相邻两组的题号集合还要重合至少 80%。若后一组紧邻一个明确的普通 Markdown
    试卷标题，则标题与题号重启共同确认边界，允许两份试卷题量不同。强标题单元内
    继续找附卷时（``internal=True``），每个新边界都必须有这样的普通标题；顶层
    无标题合集仍可仅凭完整题号重启确认。题号集合判定不依赖出现顺序，可容忍多栏
    页面被 OCR 读成 ``1..11,14,15,12,13``。
    """
    text = (raw_markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    logical_lines = [line.rstrip("\n") for line in lines]

    hits: list[tuple[int, int]] = []
    fenced = False
    for line_index, line in enumerate(logical_lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or stripped.startswith("|"):
            continue
        number = _question_number(line)
        if isinstance(number, int) and 1 <= number <= 300:
            hits.append((line_index, number))

    if not hits or hits[0][1] != 1:
        if internal:
            return []
        raise CollectionStructureError(
            f"「{label}」没有可靠中文结构标题，题号序列也未从第 1 题开始")

    groups: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    ambiguous_short_reset = False
    section_reset_title = ""
    for hit in hits:
        if hit[1] == 1 and current:
            distinct = {number for _, number in current}
            if current[-1][1] == 1:
                # MinerU 偶尔连续输出两遍第 1 题，连续重复不能成为边界。
                current.append(hit)
                continue
            if len(distinct) >= 5:
                nearby = _explicit_title_immediately_before(
                    logical_lines, current[-1][0], hit[0])
                if internal:
                    ordinary = _ordinary_reset_title(
                        logical_lines, current[-1][0], hit[0])
                    if ordinary is None:
                        # 强标题单元内部可能按题型重新编号；没有独立试卷标题时不能
                        # 仅凭一个新“1.”把原单元切开，继续等待真正的附卷标题。
                        current.append(hit)
                        continue
                    nearby = (ordinary[0], ordinary[1], False)
                if nearby is not None and nearby[2]:
                    section_reset_title = nearby[1]
                groups.append(current)
                current = []
            else:
                if internal:
                    current.append(hit)
                    continue
                # 已经读到 2..N 后又出现 1，却还不足五题：既可能是短卷，
                # 也可能是题内编号。不能合并后假装证据完整。
                ambiguous_short_reset = True
        current.append(hit)
    if current:
        groups.append(current)

    if section_reset_title:
        raise CollectionStructureError(
            f"「{label}」的新第 1 题前是题型标题“{section_reset_title}”，"
            "这更可能是同一份试卷换题型，不能作为试卷边界")
    if internal and len(groups) < 2:
        return []
    if ambiguous_short_reset or len(groups) < 2:
        raise CollectionStructureError(
            f"「{label}」按题号重置未得到至少两组可确认的完整试卷")

    number_sets: list[set[int]] = []
    for index, group in enumerate(groups, 1):
        numbers = [number for _, number in group]
        unique = set(numbers)
        coverage = len(unique) / max(1, max(unique))
        if (numbers[0] != 1 or min(unique) != 1 or len(unique) < 5
                or coverage < minimum_coverage):
            raise CollectionStructureError(
                f"「{label}」按题号重置得到的第 {index} 组题号覆盖不足，"
                f"每组必须从 1 开始、至少五题且覆盖率不低于 "
                f"{minimum_coverage:.0%}")
        number_sets.append(unique)
    for index, (left, right) in enumerate(
            zip(number_sets, number_sets[1:]), 1):
        overlap = len(left & right) / max(1, len(left), len(right))
        right_title = _ordinary_reset_title(
            logical_lines, groups[index - 1][-1][0], groups[index][0][0])
        if overlap < 0.80 and right_title is None:
            raise CollectionStructureError(
                f"「{label}」按题号重置得到的第 {index}、{index + 1} 组"
                "题号重合度低于 80%，不能确认它们是同类连续试卷")

    starts: list[int] = []
    display_titles: list[str] = []
    generated_titles: list[bool] = []
    previous_last_question = -1
    for index, group in enumerate(groups, 1):
        first_question = group[0][0]
        nearby = _ordinary_reset_title(
            logical_lines, previous_last_question, first_question)
        starts.append(nearby[0] if nearby else first_question)
        display_titles.append(nearby[1] if nearby else f"第{index}组")
        generated_titles.append(nearby is None)
        previous_last_question = group[-1][0]

    units: list[MarkdownUnit] = []
    for index, (group, start, title, generated_title) in enumerate(
            zip(groups, starts, display_titles, generated_titles), 1):
        end = starts[index] if index < len(starts) else len(lines)
        units.append(MarkdownUnit(
            title=title,
            topic=title,
            ordinal=index,
            markdown="".join(lines[start:end]).strip(),
            start_line=start + 1,
            question_numbers=tuple(number for _, number in group),
            number_reset=True,
            generated_title=generated_title,
        ))
    return units


def _expand_number_reset_units(units: list[MarkdownUnit], *, label: str,
                               minimum_coverage: float
                               ) -> list[MarkdownUnit]:
    """把强标题末段中再次从 1 开始的完整试卷展开为独立单元。

    教辅常在“精练十九”后继续附 A/B 卷，附卷标题不含中文序号，旧逻辑会把它们
    全吞进最后一个精练。这里不猜标题措辞，只复用严格题号重启证据；无法确认两组
    时原单元逐字保留。恢复阶段可以用较低覆盖率建立临时边界，但最终仍会重新经过
    正式的 80% 分组门。
    """
    expanded: list[MarkdownUnit] = []
    for unit in units:
        resets = _split_exam_by_number_resets(
            unit.markdown, label=f"{label}“{unit.title}”",
            minimum_coverage=minimum_coverage, internal=True)
        if not resets:
            expanded.append(unit)
            continue
        for index, reset in enumerate(resets):
            if index == 0:
                reset = replace(
                    reset, title=unit.title, topic=unit.topic,
                    ordinal=unit.ordinal, generated_title=False)
            else:
                reset = replace(reset, ordinal=None)
            reset = replace(
                reset, start_line=unit.start_line + reset.start_line - 1)
            expanded.append(reset)
    return expanded


def split_markdown_units(raw_markdown: str, *, label: str = "合集"
                         ) -> list[MarkdownUnit]:
    """优先按结构标题拆分，标题不足时按多组完整题号重置拆分。

    首个标题之前的封面、目录不属于任何单元，不进入后续切题。
    任一候选标题后不存在连续题号时整份停止：宁可让人处理歧义，
    也不能静默把一组切成两组或吞掉内容。
    """
    text = (raw_markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    logical_lines = [line.rstrip("\n") for line in lines]
    candidates = _title_candidates(logical_lines)
    if len(candidates) < 2:
        # 解析册允许缺题，仍交给题干结构约束下的专用配对兜底；这里的
        # 五题/85%/相邻重合判据只用于确认题干本身存在多份完整试卷。
        if label != "解析合集":
            return _split_exam_by_number_resets(text, label=label)
        raise CollectionStructureError(
            f"「{label}」没有找到至少两个含中文序号的结构标题")
    candidates = _confirmed_candidates(lines, candidates, label)

    units: list[MarkdownUnit] = []
    for index, current in enumerate(candidates):
        end = (candidates[index + 1].line_index
               if index + 1 < len(candidates) else len(lines))
        chunk = "".join(lines[current.line_index:end]).strip()
        numbers, valid = _number_evidence(chunk)
        if not valid:  # 理论上已由 _confirmed_candidates 保证，保留防御断言。
            raise CollectionStructureError(
                f"「{label}」的结构标题“{current.title}”题号证据发生变化")
        units.append(MarkdownUnit(
            title=current.title,
            topic=current.topic,
            ordinal=current.ordinal,
            markdown=chunk,
            start_line=current.line_index + 1,
            question_numbers=numbers,
        ))
    if label == "解析合集":
        return units
    return _expand_number_reset_units(
        units, label=label, minimum_coverage=0.85)


def split_markdown_units_for_recovery(raw_markdown: str, *, label: str = "合集恢复"
                                      ) -> list[MarkdownUnit]:
    """为局部 OCR 恢复建立临时单元，绝不作为最终分组结果。

    正常资料直接返回正式分组。只有正式分组因少量漏号失败时，才允许连续强标题
    配合第 1 题、至少五个题号和 60% 覆盖率建立临时边界。调用方恢复完缺号后
    必须再次使用 85% 覆盖率的严格分组；否则这些临时单元没有入库资格。
    """
    try:
        return split_markdown_units(raw_markdown, label=label)
    except CollectionStructureError as strict_error:
        text = (raw_markdown or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines(keepends=True)
        logical_lines = [line.rstrip("\n") for line in lines]
        candidates = _title_candidates(logical_lines)
        if len(candidates) < 2 or not all(item.strong for item in candidates):
            raise strict_error
        try:
            candidates = _confirmed_candidates(
                lines, candidates, label, recovery_only=True)
        except CollectionStructureError:
            raise strict_error

        units: list[MarkdownUnit] = []
        for index, current in enumerate(candidates):
            end = (candidates[index + 1].line_index
                   if index + 1 < len(candidates) else len(lines))
            chunk = "".join(lines[current.line_index:end]).strip()
            numbers, _ = _number_evidence(chunk)
            if not _recovery_number_evidence(numbers):
                raise strict_error
            units.append(MarkdownUnit(
                title=current.title,
                topic=current.topic,
                ordinal=current.ordinal,
                markdown=chunk,
                start_line=current.line_index + 1,
                question_numbers=numbers,
            ))
        return _expand_number_reset_units(
            units, label=label, minimum_coverage=0.60)


def _topic_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower()
    # 星号重要程度、括号与空格是版式差异，不是专题语义。
    text = re.sub(r"[★☆*]+(?:重要)?[★☆*]+", "", text)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


_TOPIC_DECORATOR_RE = re.compile(
    r"(?:基础理解与分析|核心理解与分析|超重失重及|含自由落体|"
    r"最后强化|继续强化|基础|大全|各种|问题|强化|核心|理解|"
    r"综合|应用|分析|信息|概念)"
)


def _topic_core(value: str) -> str:
    """剔除常见标题修饰语，保留真正区分专题的部分。"""
    original = _topic_key(value)
    core = _TOPIC_DECORATOR_RE.sub("", original)
    return core or original


def _character_overlap(left: str, right: str) -> float:
    a, b = set(left), set(right)
    return len(a & b) / max(1, len(a | b))


def _topic_similarity(left: str, right: str) -> float:
    a, b = _topic_key(left), _topic_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    score = max(SequenceMatcher(None, a, b).ratio(),
                _character_overlap(a, b))
    core_a, core_b = _topic_core(a), _topic_core(b)
    if core_a == core_b and len(core_a) >= 3:
        return 1.0
    core_score = max(SequenceMatcher(None, core_a, core_b).ratio(),
                     _character_overlap(core_a, core_b))
    # 一边是另一边的扩展专题时可以放宽，但至少要有四个连续核心汉字；
    # “运动学基础/力学基础”这类只共享泛化后缀的标题不能因此通过。
    if (min(len(core_a), len(core_b)) >= 4
            and (core_a in core_b or core_b in core_a)):
        core_score = max(core_score, 0.90)
    return max(score, core_score)


def _topics_compatible(left: str, right: str) -> bool:
    """保守确认两个专题相同，不以整体字符相似度猜语义。

    一字之差可能就是完全不同的物理专题（动量/动能、第一/第二定律），
    因而只接受经过明确修饰语归一后的核心完全相同。模糊相似度仅供
    报告观察，不参与自动挂答案。
    """
    a, b = _topic_key(left), _topic_key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    core_a, core_b = _topic_core(a), _topic_core(b)
    if core_a == core_b:
        return True
    # OCR 标题有时把括号里的既有短语再附到末尾，例如
    # “功能关系与能量守恒1功能关系”。只容忍“尾巴已完整包含在主标题”
    # 这一种可证明的重复，不接受任意包含或字符近似。
    for longer, shorter in ((core_a, core_b), (core_b, core_a)):
        if longer.startswith(shorter):
            tail = longer[len(shorter):]
            if tail and tail in shorter:
                return True
    return False


def _generic_solution_label(title: str, topic: str, ordinal: int | None,
                            exam: MarkdownUnit) -> bool:
    """识别“提升精练二参考答案”这类只带序号、不复述专题名的解析标题。

    这类标题本身没有可供语义比较的 topic，但“相同中文序号 + 题号重置后
    与题干题号高重合”仍是三份独立证据。只对答案／解析等纯尾词放行；
    “精练二：光学”仍必须经过专题语义校验，不能借序号相同蒙混过关。
    """
    if exam.ordinal is None:
        return False
    if ordinal is None:
        match = _GENERIC_SOLUTION_TITLE_RE.match(title)
        ordinal = _cn_to_int(match.group("num")) if match else None
    if ordinal != exam.ordinal:
        return False
    topic_key = _topic_key(topic)
    return bool(_GENERIC_SOLUTION_TITLE_RE.match(title)) or topic_key in {
        "答案", "参考答案", "解析", "答案解析", "参考答案解析",
        "详解", "解答",
    }


def _generic_solution_title(candidate: _TitleCandidate | None,
                            exam: MarkdownUnit) -> bool:
    if candidate is None:
        return False
    return _generic_solution_label(
        candidate.title, candidate.topic, candidate.ordinal, exam)


def _split_solution_by_number_resets(
        raw_markdown: str, exams: list[MarkdownUnit]) -> list[MarkdownUnit]:
    """解析标题漏识别时，用题号重置和题干结构交叉确认分组。

    解析册的标题可能整行被 MinerU 漏掉，但每份答案通常仍会重新从第 1 题
    开始。单看 ``1.`` 不够安全：一道题的详解里也可能出现编号。因此这里
    同时要求前一段已经出现至少两个不同题号、最终段数与题干段数完全一致，
    并逐段核对题号集合重合度；能识别出的解析标题还必须与当前位置的题干
    专题一致。四层证据同时成立才允许补用题干标题，任何一层不成立都停止。

    解析 OCR 可能把少数“题号 + 答案”整行吃掉。常规仍要求 80% 重合；仅当
    强标题、中文序号、专题语义、严格递增题号子集、相邻组正常五项都能独立
    确认同一单元时，允许下降到 75%。这个例外不能接受额外题号或缺末题，
    避免相邻单元错位后仍靠标题蒙混。
    """
    text = (raw_markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    logical_lines = [line.rstrip("\n") for line in lines]
    candidates = _title_candidates(logical_lines)

    hits: list[tuple[int, int]] = []
    fenced = False
    for line_index, line in enumerate(logical_lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or stripped.startswith("|"):
            continue
        number = _question_number(line)
        if isinstance(number, int) and 1 <= number <= 300:
            hits.append((line_index, number))

    groups: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    for hit in hits:
        # 同一答案偶尔会被 OCR 重复成两个连续的“1.”。只有前一段已经
        # 出现至少两个不同题号时，新的 1 才能证明发生了单元重置。
        if hit[1] == 1 and len({number for _, number in current}) >= 2:
            groups.append(current)
            current = []
        current.append(hit)
    if current:
        groups.append(current)

    if len(groups) != len(exams):
        raise CollectionStructureError(
            f"题干检出 {len(exams)} 组，解析按题号重置检出 {len(groups)} 组，"
            "组数不一致，不能机械补齐漏识别标题")

    solution_sets = [set(number for _, number in group) for group in groups]
    exam_sets = [set(exam.question_numbers) for exam in exams]
    reset_overlaps = [
        len(solution_set & exam_set)
        / max(1, len(solution_set), len(exam_set))
        for solution_set, exam_set in zip(solution_sets, exam_sets)
    ]

    starts: list[int] = []
    titles: list[_TitleCandidate | None] = []
    previous_last_question = -1
    for group in groups:
        first_question = group[0][0]
        nearby = [candidate for candidate in candidates
                  if previous_last_question < candidate.line_index <= first_question]
        candidate = nearby[-1] if nearby else None
        if candidate is None:
            ordinary = _ordinary_reset_title(
                logical_lines, previous_last_question, first_question)
            if ordinary is not None:
                candidate = _TitleCandidate(
                    ordinary[0], ordinary[1], ordinary[1], None, False)
        starts.append(candidate.line_index if candidate else first_question)
        titles.append(candidate)
        previous_last_question = group[-1][0]

    units: list[MarkdownUnit] = []
    for index, (exam, group, start, candidate) in enumerate(
            zip(exams, groups, starts, titles), 1):
        end = starts[index] if index < len(starts) else len(lines)
        chunk = "".join(lines[start:end]).strip()
        numbers = tuple(number for _, number in group)
        solution_set = set(numbers)
        exam_set = set(exam.question_numbers)
        overlap = len(solution_set & exam_set) / max(
            1, len(solution_set), len(exam_set))
        deduplicated_numbers: list[int] = []
        for number in numbers:
            if not deduplicated_numbers or deduplicated_numbers[-1] != number:
                deduplicated_numbers.append(number)
        strictly_increasing = all(
            left < right for left, right in zip(
                deduplicated_numbers, deduplicated_numbers[1:]))
        neighbor_indices = [neighbor for neighbor in (index - 2, index)
                            if 0 <= neighbor < len(reset_overlaps)]
        neighbors_are_regular = all(
            reset_overlaps[neighbor] >= 0.80 for neighbor in neighbor_indices)
        strong_title_confirms_sparse_solution = (
            overlap >= 0.75
            and candidate is not None
            and candidate.strong
            and exam.ordinal is not None
            and candidate.ordinal == exam.ordinal
            and _topics_compatible(exam.topic, candidate.topic)
            and len(solution_set) >= 5
            and solution_set < exam_set
            and len(exam_set - solution_set) <= 3
            and max(solution_set) == max(exam_set)
            and strictly_increasing
            and neighbors_are_regular
        )
        if (len(solution_set) < 2 or min(solution_set) != 1
                or (overlap < 0.80
                    and not strong_title_confirms_sparse_solution)):
            raise CollectionStructureError(
                f"解析按题号重置得到的第 {index} 组与题干题号重合度不足，"
                "不能确认这是同一份练习")
        if (candidate is not None and not exam.generated_title
                and not _topics_compatible(exam.topic, candidate.topic)
                and not _generic_solution_title(candidate, exam)):
            raise CollectionStructureError(
                f"第 {index} 组题干“{exam.title}”与解析候选标题"
                f"“{candidate.title}”不能可靠对应，已停止机械补齐")
        units.append(MarkdownUnit(
            title=candidate.title if candidate else exam.title,
            topic=candidate.topic if candidate else exam.topic,
            ordinal=candidate.ordinal if candidate else exam.ordinal,
            markdown=chunk,
            start_line=start + 1,
            question_numbers=numbers,
            number_reset=True,
            generated_title=candidate is None,
        ))
    return units


def pair_markdown_collections(exam_markdown: str,
                              solution_markdown: str | None = None
                              ) -> list[MarkdownPair]:
    """题干与解析分别分组后，按专题语义+单调顺序一一配对。

    原文序号只是辅助信号：书稿里常有重号、跳号笔误，仅按序号
    会把整个后半册错位。两侧数量不同或任意一对标题过于不相似时直接拒绝。
    """
    exams = split_markdown_units(exam_markdown, label="题干合集")
    if solution_markdown is None:
        return [MarkdownPair(unit.title, unit, None) for unit in exams]
    try:
        solutions = split_markdown_units(solution_markdown, label="解析合集")
    except CollectionStructureError:
        solutions = _split_solution_by_number_resets(solution_markdown, exams)
    if len(exams) != len(solutions):
        # 标题法本身可能成功，但 MinerU 恰好漏掉整行标题而静默少分一组。
        # 此时也必须用题号重置重新确认，不能直接按较短列表错位配对。
        solutions = _split_solution_by_number_resets(solution_markdown, exams)
    if len(exams) != len(solutions):
        raise CollectionStructureError(
            f"题干检出 {len(exams)} 组，解析检出 {len(solutions)} 组，"
            "组数不一致，不能按位置硬配")

    compatibility = [
        [_topics_compatible(exam.topic, solution.topic)
         or _generic_solution_label(
             solution.title, solution.topic, solution.ordinal, exam)
         for solution in solutions]
        for exam in exams
    ]
    pairs: list[MarkdownPair] = []
    for index, (exam, solution) in enumerate(zip(exams, solutions), 1):
        if exam.number_reset:
            # 题干没有可靠标题时不能再用生成的“第 N 组”猜语义，只按已经
            # 严格确认的单调位置配对；解析仍必须组数一致且题号明显重合。
            exam_numbers = set(exam.question_numbers)
            solution_numbers = set(solution.question_numbers)
            overlap = len(exam_numbers & solution_numbers) / max(
                1, len(exam_numbers), len(solution_numbers))
            if overlap < 0.80:
                raise CollectionStructureError(
                    f"第 {index} 组题干与解析题号重合度不足，"
                    "不能按题号重置结果机械配对")
            if (not exam.generated_title and not solution.generated_title
                    and not _topics_compatible(exam.topic, solution.topic)
                    and not _generic_solution_label(
                        solution.title, solution.topic,
                        solution.ordinal, exam)):
                raise CollectionStructureError(
                    f"第 {index} 组题干“{exam.title}”与解析“{solution.title}”"
                    "都有明确标题但不能可靠对应，已停止自动配对")
            pairs.append(MarkdownPair(exam.title, exam, solution))
            continue
        row = compatibility[index - 1]
        if not row[index - 1]:
            raise CollectionStructureError(
                f"第 {index} 组题干“{exam.title}”与解析“{solution.title}”"
                "的专题标题不能可靠对应，已停止自动配对")
        candidate_solutions = [i for i, compatible in enumerate(row)
                               if compatible]
        candidate_exams = [i for i, rows in enumerate(compatibility)
                           if rows[index - 1]]
        if len(candidate_solutions) > 1 or len(candidate_exams) > 1:
            # 同名/泛化专题出现多次时，语义本身不足以定位。只有当前位置
            # 序号一致，且在所有可兼容候选中序号唯一，才允许自动配对。
            ordinal = exam.ordinal
            ordinal_matches = [
                i for i in candidate_solutions
                if ordinal is not None and solutions[i].ordinal == ordinal
            ]
            if (ordinal is None or solution.ordinal != ordinal
                    or ordinal_matches != [index - 1]):
                raise CollectionStructureError(
                    f"第 {index} 组专题“{exam.topic}”在题干或解析中重复，"
                    "序号也不能唯一定位，已停止自动配对")
        pairs.append(MarkdownPair(exam.title, exam, solution))
    return pairs
