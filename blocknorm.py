"""逐块 LLM 判定与规范化（新路径的 ④ 步）。

与现路径（project-alpha/src/normalizer.py 整篇规范化）的根本差别：
块数在调用 LLM 之前已由 blocksplit 定死，每次调用只处理**一个块**。于是
  - 不会被 max_tokens 截断 → 不需要续传、不需要「已完成清单」、不需要跨轮去重；
  - 模型漏写/多写都只影响它自己那一块，不再连带丢别的题；
  - 每块可并行，墙钟时间不随题数线性增长。

本步要模型做三件它比正则擅长的事：
  ① 判块类型：纯题目 / 纯解析 / 题目+解析（混合块要指明解析从哪句开始）；
  ② 判题型标签 `[单选]/[多选]/[填空]/[解答]`——单选与多选选项外形相同，只能靠
     blocksplit 采集到的 section 分区标题（如「二、多选题…有多项符合」）来判，
     所以 section 必须随块一起发给模型；
  ③ 做机械层不敢做的排版：裸字母包裹、选项分行、表格转管道表格、填空位补齐。

输出用极简的行标记协议（`@@KIND` / `@@TYPE` / `@@BODY` / `@@SOL`）而不是 JSON：
块正文里 LaTeX 反斜杠和引号极多，JSON 转义一旦写错整块就废；行标记只要求模型
别在正文里顶格写 `@@`，容错高得多。解析不出来时按「纯题目」保守回退，不丢内容。
"""

import concurrent.futures
import dataclasses
import logging
import random
import re
import time

import mechfix

logger = logging.getLogger(__name__)

# 并发度：MinerU 那步已经是两路并行的量级，这里对同一个 LLM 端点别开太猛，
# 免得撞上服务商的速率限制反而更慢。8 是实测比较稳的折中。
_MAX_WORKERS = 8

# 单块重试次数。实测（36 块并发 8）同一份原文两次跑，一次有 5 块降级、一次 0 块
# 降级，降级的块单独重跑全部一次通过——即失败是并发下的偶发调用错误（限流/瞬时
# 断连），不是内容或协议问题。llm_client.chat 本身不重试，所以一次抖动就把那块
# 永久钉成机械兜底结果。重试放在这里而不是 llm_client：那是老路径也会共用的模块，
# 不能因为新路径的需要改它的行为。
_RETRIES = 2
_BACKOFF = 1.5

_SYSTEM_PROMPT = """你是数学题目排版规范化专家。你会收到**一个**题块（已由程序按题号切好），\
请判断它的内容类型并规范化排版。

# 输出格式（严格遵守，不要输出任何解释、开场白、代码块围栏）
@@KIND <question|solution|both>
@@TYPE <单选|多选|填空|解答>
@@BODY
<题干正文，规范化后>
@@SOL
<解析正文，规范化后>

- `@@KIND question`：本块只有题目 → 只输出 @@BODY 段，不要写 @@SOL 段。
- `@@KIND solution`：本块只有解析/答案 → 只输出 @@SOL 段，不要写 @@BODY 段。
- `@@KIND both`：本块题目和解析都有 → 两段都输出，把解析部分放进 @@SOL。
- `@@TYPE` 在 KIND=solution 时也要给出你的最佳判断，用于交叉核对。
- 四个 `@@` 标记必须各自顶格独占一行。正文里**绝不允许**顶格出现 `@@`。

# 判定块类型
- 含 `【答案】``【解析】`「解答：」等标记、或整块是解题过程/答案字母 → 有解析成分.
- 题干与解析同块时，解析通常从「【答案】」「【解析】」「解：」「证明：」或答案\
字母（如「D」单独一行）开始. 这之前是题干，这之后是解析.
- 判不准时优先判 question，把可疑内容留在 @@BODY——宁可让人工在校对页移走，\
也不要把题干误当解析删掉.

# 判定题型标签
优先用用户消息里给出的「所属分区」标题：含「单选」「只有一项符合」→ 单选；\
含「多选」「不定项」「有多项符合」「部分选对」→ 多选；含「填空题」→ 填空；\
含「解答题」「计算题」「证明题」→ 解答. 没有分区信息时按特征兜底：有 `___` → \
填空；有 `（1）（2）` 小问 → 解答；有 A/B/C/D 选项 → 单选（单选多选外形相同，\
无分区依据时一律判单选）.

# 排版规范（红线：零篡改。只改排版，绝不修改、增删、推导任何数学内容与解题步骤）
1. **绝不臆造**：草稿没有解析就不要写 @@SOL；不要自己解题.
2. **禁用中文句号**：`。` 一律改为英文点号加空格 `. `；句末、题干末尾不留任何句号.
3. **公式**：一律 `$\\displaystyle ...$` 行内形式，`$` 与内容不留空格，不加反引号，\
分式用 `\\dfrac`. 不要出现 `$$` 行间公式.
4. **裸字母必须包裹**：代表数学对象的拉丁字母（变量、点、函数名、**选项标签 \
A/B/C/D**）一律写成 `$\\displaystyle A$` 形式，哪怕只有一个字母. 注意「A 组」\
这类分组名不是数学对象，不要包裹.
5. **选项排版**：选项字母写 `$\\displaystyle A.$` 等；四个选项都短则同一行、\
用多个空格分隔；较长则每个选项独占一行.
6. **序号层级**：一级小问统一 `（1）（2）`，二级统一 `（i）（ii）`，各自独立成段、\
段间空一行. 公式内的括号保持半角不变.
7. **填空题**末尾必须有 `___` 填空位，原文有横线/留白的统一换成 `___`.
8. **表格必须保留并改写成 Markdown 管道表格**，表头下接 `| --- | --- |` 分隔行，\
不许残留任何 HTML 标签（`<table>``<tr>``<td>``<br>` 等），表内不许有空行.
9. **图片引用**：形如 `![](images/xxx.jpg)` 的引用**原样保留在原位置**，不得删除\
或改写路径.
10. 题干**不要**保留原文的大题标题、分区标题、分组小标题、题号前缀之外的装饰文字\
（如「公众号：xxx」「题号位置： 17 18」这类 OCR 噪声要删掉）.
"""

_NO_IMG_RULE = """
# 本次特别要求（优先级高于上方第 9 条）
删除所有图片引用（`![](...)`、`<img ...>`），不留痕迹.
"""

_KIND_RE = re.compile(r"^@@KIND\s+(question|solution|both)\s*$", re.I | re.M)
_TYPE_RE = re.compile(r"^@@TYPE\s+(单选|多选|填空|解答)\s*$", re.M)
_BODY_RE = re.compile(r"^@@BODY[ \t]*$", re.M)
_SOL_RE = re.compile(r"^@@SOL[ \t]*$", re.M)


@dataclasses.dataclass
class NormBlock:
    """一个块经 LLM 判定+规范化后的结果。坐标字段从 Block 原样带过来。"""

    index: int
    number: int | None
    group: str | None
    zone: str
    kind: str                 # question / solution / both
    qtype: str                # 单选 / 多选 / 填空 / 解答
    body: str                 # 题干（kind=solution 时为空）
    solution: str             # 解析（kind=question 时为空）
    line_no: int
    degraded: bool = False    # True=LLM 失败或输出不合协议，本块用机械结果兜底


def _parse_reply(text: str) -> tuple[str | None, str | None, str, str]:
    """解析 LLM 回复的行标记协议，返回 (kind, qtype, body, solution)。

    kind 取不到时返回 None，由调用方决定回退策略——不在这里瞎猜，因为「这块是
    题还是解析」判错的代价（题干被当解析丢掉）远大于多留一段冗余文本。
    """
    mk = _KIND_RE.search(text)
    mt = _TYPE_RE.search(text)
    kind = mk.group(1).lower() if mk else None
    qtype = mt.group(1) if mt else None

    mb = _BODY_RE.search(text)
    ms = _SOL_RE.search(text)
    body = sol = ""
    if mb and ms:
        if mb.start() < ms.start():
            body, sol = text[mb.end():ms.start()], text[ms.end():]
        else:
            sol, body = text[ms.end():mb.start()], text[mb.end():]
    elif mb:
        body = text[mb.end():]
    elif ms:
        sol = text[ms.end():]
    return kind, qtype, _clean_section(body), _clean_section(sol)


def _clean_section(text: str) -> str:
    """去掉段内残留的 `@@` 标记行。

    实测（deepseek-v4-pro，长解析块）模型偶尔把 `@@SOL` 写两遍——「思路 1」「思路 2」
    各起一段时尤其容易。取第一个标记之后的全部文本会把第二个标记原样带进正文，
    校对页上就是一行突兀的 `@@SOL`。这里统一清掉，不区分是哪种标记：正文里任何
    顶格 `@@` 行都是协议残渣，没有合法用途。
    """
    lines = [l for l in text.splitlines() if not l.startswith("@@")]
    return "\n".join(lines).strip()


def _fallback_split(text: str) -> tuple[str, str]:
    """LLM 不可用时的机械兜底：按解析标记把块切成 (题干, 解析)。

    只认独占一行或行首的 `【答案】`/`【解析】`/`解：`/`证明：`。切不出来就整块
    当题干——保证内容不丢，由用户在校对页处理。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*(【答案】|【解析】|解[:：]|证明[:：]|答[:：])", line):
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
    return text.strip(), ""


def _guess_type(text: str, section: str | None) -> str:
    """机械兜底判题型，规则与 prompt 里给模型的判据一致。"""
    sec = section or ""
    if re.search(r"多选|不定项|有多项符合|部分选对", sec):
        return "多选"
    if re.search(r"单选|只有一项|单项选择", sec):
        return "单选"
    if "填空" in sec:
        return "填空"
    if re.search(r"解答题|计算题|证明题|应用题", sec):
        return "解答"
    if "___" in text:
        return "填空"
    if re.search(r"（\s*[1１]\s*）|\(\s*1\s*\)", text):
        return "解答"
    labels = {m.group(1) for m in re.finditer(r"(?:^|\s)([A-D])\s*[.．]", text)}
    if len(labels) >= 3:
        return "单选"
    return "解答"


def _build_user_content(block, keep_images: bool) -> str:
    """拼一个块的 user 消息。坐标与分区标题必须带上——题型判定依赖分区标题，
    而单块视野里看不到它（它在几十行之外的大标题里）。"""
    meta = [f"块序号：{block.index + 1}"]
    if block.number is not None:
        meta.append(f"题号：{block.number}")
    if block.group:
        meta.append(f"所属分组：{block.group} 组")
    if block.section:
        meta.append(f"所属分区：{block.section}")
    meta.append("位置：" + ("解析区（全文末尾的答案区）" if block.zone == "solution"
                          else "题干区"))
    return ("以下是一个题块，请按规则判定类型并规范化：\n\n"
            + "\n".join(meta)
            + f"\n\n<块>\n{block.text}\n</块>")


def normalize_one(block, client, keep_images: bool = True) -> NormBlock:
    """规范化单个块。任何失败都降级为机械结果，绝不让一块的失败搞掉整次转换。"""
    system = _SYSTEM_PROMPT + ("" if keep_images else _NO_IMG_RULE)
    fb_body, fb_sol = _fallback_split(block.text)
    fb_type = _guess_type(block.text, block.section)

    def _degraded(reason: str) -> NormBlock:
        logger.warning("块 %d（原文第 %d 行）降级为机械结果：%s",
                       block.index + 1, block.line_no, reason)
        kind = ("solution" if block.zone == "solution"
                else ("both" if fb_sol else "question"))
        return NormBlock(
            index=block.index, number=block.number, group=block.group,
            zone=block.zone, kind=kind, qtype=fb_type,
            body="" if kind == "solution" else fb_body,
            solution=block.text.strip() if kind == "solution" else fb_sol,
            line_no=block.line_no, degraded=True)

    user = _build_user_content(block, keep_images)
    reason = ""
    kind = qtype = None
    body = sol = ""
    for attempt in range(_RETRIES + 1):
        if attempt:
            # 抖动一下再重试：整批是同时起跑的，等长退避会让失败的块又一起撞上去
            time.sleep(_BACKOFF * attempt * (1 + random.random()))
            logger.info("块 %d 第 %d 次重试（上次：%s）",
                        block.index + 1, attempt, reason)
        try:
            content, _finish = client.chat(system, user)
        except Exception as e:                    # 单块失败不中断整批
            reason = f"LLM 调用失败: {type(e).__name__}: {e}"
            continue
        kind, qtype, body, sol = _parse_reply(content or "")
        if kind is None:
            reason = "回复不含 @@KIND 标记"
            continue
        if kind == "question" and not body:
            reason = "判为纯题目但 @@BODY 为空"
            continue
        if kind == "solution" and not sol:
            reason = "判为纯解析但 @@SOL 为空"
            continue
        break
    else:
        return _degraded(reason)
    if kind == "both" and not (body and sol):
        # 只给出一段时不强行报废：有哪段用哪段，缺的那段留给机械兜底补
        body = body or fb_body
        sol = sol or fb_sol

    return NormBlock(
        index=block.index, number=block.number, group=block.group,
        zone=block.zone, kind=kind, qtype=qtype or fb_type,
        body=body, solution=sol, line_no=block.line_no)


def normalize_blocks(blocks, client, keep_images: bool = True,
                     max_workers: int = _MAX_WORKERS) -> list[NormBlock]:
    """并行规范化所有块，按原顺序返回。

    并行是这条路径省墙钟时间的关键：块之间完全独立，且每次调用都是纯 I/O 等待.
    顺序靠 index 复原，不依赖 future 完成顺序.
    """
    if not blocks:
        return []
    workers = max(1, min(max_workers, len(blocks)))
    results: list[NormBlock | None] = [None] * len(blocks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(normalize_one, b, client, keep_images): i
                for i, b in enumerate(blocks)}
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            results[i] = fut.result()   # normalize_one 内部已兜底，不会抛
    out = [r for r in results if r is not None]
    degraded = sum(1 for r in out if r.degraded)
    logger.info("逐块规范化完成：%d 块，其中降级 %d 块", len(out), degraded)
    return out
