"""规范化：加载 Skill prompt，调用 DeepSeek，超长则续传。

关键：单次调用让 DeepSeek 看到全文，才能正确关联「题干区+解析区」分离排版的
题目与解析；但 max_tokens 会截断长文档，需分多轮续传。

续传为什么难（核心）：DeepSeek 每轮调用**无状态**，看不到自己上轮的输出，
只能在收到的「完整草稿 + 指令」里自行定位「下一道该写的题」。若只告诉它
「从第 N 题继续」，长草稿 + 大 N 时它定位不可靠 → 重复已写过的题。

因此改用两层防重复：
  ① 续传时把**已完成题目的开头清单**显式列给模型（精确跳过名单，
     比让它自己数可靠得多）；
  ② 我方按**归一化题干**去重：每轮新块若与已收题干重复则丢弃。
无论模型是否听话，最终合并结果都不含重复题。
（历史 bug：曾用正则找「第N题」计数，输出里根本没这四字 → 计数恒为 0 →
每轮都从第 1 题重来 → 长文档 ~17 题后整段重复。）
"""

import logging
from pathlib import Path

from .deepseek_client import DeepSeekClient

logger = logging.getLogger(__name__)

DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "templates" / "normalize_prompt.md"
)


def load_system_prompt(template_path: str | Path | None = None) -> str:
    """读取规范化 Skill prompt 文本。"""
    path = Path(template_path) if template_path else DEFAULT_PROMPT_PATH
    if not path.is_file():
        raise FileNotFoundError(f"未找到 prompt 模板: {path}")
    return path.read_text(encoding="utf-8")


def normalize(
    raw_markdown: str,
    client: DeepSeekClient,
    template_path: str | Path | None = None,
    max_rounds: int = 8,
    include_solution: bool = False,
    keep_images: bool = False,
    only_numbers: list[int] | None = None,
) -> str:
    """规范化原始 markdown，超长时自动续传，返回合并后的完整输出。

    include_solution=False（默认）时只输出题干、省略解析，单次即可完成、最快。
    keep_images=False（默认）时沿用 base prompt 的「剔除图片」规则；
    QuizForge 需要插图时传 True，追加一条覆盖规则要求保留 ![](images/...) 引用。
    only_numbers（默认 None=全部）：仅规范化指定题号的题（如 [8,11,14,18,19]
    压轴题）。既让模型只输出这些题（输出更短、单轮更快、省 token），又在我方
    按题号兜底过滤，双保险。
    默认值保持 project-alpha 自身 CLI 行为不变。
    """
    system_prompt = load_system_prompt(template_path)
    if only_numbers:
        nums_str = "、".join(str(n) for n in only_numbers)
        system_prompt += (
            "\n\n# 本次特别要求（只处理指定题号·优先级高于「逐题输出全部」规则）\n"
            f"**只输出题号为 {nums_str} 的这几道题**，其余题目一律**完全跳过、不要输出**。"
            "题号以草稿中每道题自身标注的编号为准。若某个指定题号在草稿中找不到，"
            "跳过它即可，不要臆造。被选中的题仍按下方全部规范照常输出（含题干、"
            "选项、以及按其他开关决定的解析/图片）。"
        )
    if keep_images:
        # QuizForge 插图路径：覆盖 base prompt 的「剔除图片」红线，要求原样保留
        # 图片引用。MinerU 解压的图在 extract_dir/images/ 下，converter 再拦截拷贝。
        system_prompt += (
            "\n\n# 本次特别要求（保留图片·优先级高于上方「剔除图片」规则）\n"
            "题目中的图片（几何图、函数图、统计图等）是题干的一部分，**必须保留**。"
            "凡草稿中形如 `![](images/xxx.jpg)` 的图片引用，**原样保留在其所属题目的 "
            "`- ` 块内、对应位置**，不得删除、不得改写路径、不得转成文字描述。"
            "若 alt 是“题干图”或“选项A/B/C/D”，这是程序根据 PDF 坐标恢复出的归属，"
            "必须与对应题干或选项绑定，禁止换位。"
            "仅忽略页眉页脚 logo 等与题目无关的装饰图（若能判断）。"
        )
    if not include_solution:
        system_prompt += (
            "\n\n# 本次特别要求（优先级高于上方解析相关规则）\n"
            "只输出每道题的**题干**：来源、题号、题型、topics、题干正文、选项。"
            "**绝不输出【解析】【解题思路】【实测数据】等任何解答内容**，即使草稿中"
            "存在解析也一律省略，保证题干紧凑、一次输出完整。"
        )
    else:
        # QuizForge「同时识别解析」路径：强化解析保留 + 题解关联，让 DeepSeek 把
        # 每道题的解析准确归到对应题目后（仅在草稿含解析时生效，绝不臆造）。
        # 图片规则跟随 keep_images：开启时解析里的配图也要保留，不能再说「剔除图片」，
        # 否则与上方保留图片规则自相矛盾 → 模型删掉解析配图。
        img_rule = (
            "解析正文中形如 `![](images/xxx.jpg)` 的配图（辅助线图、函数图等）"
            "**同样必须原样保留**在该题解析对应位置，不得删除或改写路径。"
            if keep_images else
            "剔除图片等"
        )
        system_prompt += (
            "\n\n# 本次特别要求（解析保留与关联·优先级高于上方解析相关规则）\n"
            "1. **保留解析**：草稿中每道题若有解析/答案/解题过程，必须规范化输出，"
            "不得省略。仅当草稿确实没有某题解析时才省略该题解析，**绝不臆造或推导**。\n"
            "2. **识别解析位置**：解析可能出现在三种位置，都要正确识别——"
            "（a）紧跟在题干之后；（b）集中在全文末尾的「参考答案」「答案与解析」等区块；"
            "（c）以题号列表形式给出（如「1. B」「2.（1）…」）。\n"
            "3. **严格按题号/顺序关联**：把每道题的解析放进该题所在的 `- ` 块内、"
            "题干之后、下一题 `- ` 之前，并以独占一行的 `【解析】` 标记开头。"
            "务必让第 N 题配第 N 题的解析，**不得错配、不得张冠李戴**。\n"
            "3.5 **分组结构（关键·防错配）**：草稿可能把题目分成多个组（如"
            "「A 组」「B 组」「第一部分」「第二部分」等小标题），**每组的题号都从 1 "
            "重新开始**；解析区通常也按同样的分组、同样的组内题号重新编号。此时"
            "**关联必须同时匹配「组别 + 组内题号」**：A 组第 1 题只能配解析区 A 组第 1 "
            "题的解析，绝不能配 B 组第 1 题的解析。识别分组的依据优先用小标题；"
            "题干每题若带唯一【来源】（如【2018 全国Ⅰ，9】），也可用来交叉核对是否配对正确。"
            "输出的各题**不要保留组别小标题**，仍是平铺的 `- ` 列表，但组内解析必须归位正确。\n"
            "4. **选择题合并答案**：若答案以「1-5 BCADA」等合并形式给出，"
            "在能可靠对应时按题号拆分到各题，写成该题的 `【解析】`。\n"
            f"5. 解析正文同样遵守上方全部排版规范（英文点号、`$\\displaystyle$` 包裹公式、"
            f"`\\dfrac` 分式、{img_rule}）。"
        )
    blocks: list[str] = []       # 已确认完整、去重后的题块（每块含前导 "- "）
    seen_keys: set[str] = set()  # 已收题干的归一化键，用于跨轮去重
    stale_rounds = 0             # 连续无新增/改善的轮数，用于防死循环

    for round_i in range(max_rounds):
        if round_i == 0:
            user_content = (
                "请把下面的数学题目草稿按规则规范化，逐题输出（每题用 `- ` 无序列表项开头，"
                "题间空一行，禁止输出任何 `## ` 标题或 `字段:: 值` 元数据行）：\n\n"
                f"<草稿>\n{raw_markdown}\n</草稿>"
            )
        else:
            # 「已完成清单」= 真正完成的题。include_solution 时，只有**已带解析**的
            # 题才算完成；仅题干、缺解析的题不列入 → 模型会重新输出它们（这次力争带
            # 解析），配合下方「更丰富者替换」补齐解析。不识别解析时，有题干即算完成。
            def _is_complete(b: str) -> bool:
                if not include_solution:
                    return True
                return any(ln.strip().startswith("【解析】") for ln in b.splitlines())
            done_blocks = [b for b in blocks if _is_complete(b)]
            done_list = "\n".join(
                f"{i + 1}. {_block_head(b)}" for i, b in enumerate(done_blocks))
            missing_hint = ""
            todo = [b for b in blocks if not _is_complete(b)]
            if todo:
                todo_list = "\n".join(f"- {_block_head(b)}" for b in todo)
                missing_hint = (
                    f"\n\n<待补解析清单>（这些题已有题干但**缺解析**，请**重新完整输出**"
                    f"它们，务必带上 `【解析】`）\n{todo_list}\n</待补解析清单>"
                )
            user_content = (
                f"你正在分多轮规范化同一份草稿，因长度限制上一轮被截断。"
                f"下面【已完成清单】列出已经规范化好（含解析）的 {len(done_blocks)} 道题，"
                f"请**跳过这些题，继续输出草稿中其余尚未完成的题**，"
                f"格式严格同前，**不要重复已完成清单里的题**，也不要输出开场白。"
                f"{missing_hint}\n\n"
                f"<已完成清单>\n{done_list}\n</已完成清单>\n\n"
                f"<原始草稿>\n{raw_markdown}\n</原始草稿>"
            )

        logger.info("规范化第 %d 轮（已完成 %d 题）...", round_i + 1, len(blocks))
        content, finish = client.chat(system_prompt, user_content)

        new_blocks = _split_top_blocks(content)
        # 被 max_tokens 截断时，末块多半不完整 → 丢弃，下一轮重新生成它
        if finish == "length" and len(new_blocks) > 1:
            new_blocks = new_blocks[:-1]

        # 我方去重兜底：按归一化题干键过滤掉与已收题重复的块。
        # 前缀比对（非精确相等）：同题在不同轮里题干可能一处干净、一处尾部被
        # 污染（如答案被误塞进题干），此时短键是长键的前缀，仍应判为重复。
        # 「更丰富者替换」：若重复块带解析、而已收的同题块没解析，用新块替换旧块
        # ——修长文档尾部题多轮续传时「先落题干、解析补不回」的漏解析问题。
        def _has_sol(b: str) -> bool:
            return any(ln.strip().startswith("【解析】") for ln in b.splitlines())

        added = 0
        dropped = 0
        improved = 0
        for b in new_blocks:
            key = _dedup_key(b)
            hit = _find_seen_index(key, blocks)
            if hit is not None:
                # 命中已收题：仅当新块带解析、旧块无解析时替换（补齐解析）
                if include_solution and _has_sol(b) and not _has_sol(blocks[hit]):
                    blocks[hit] = b
                    improved += 1
                else:
                    dropped += 1
                continue
            seen_keys.add(key)
            blocks.append(b)
            added += 1
        logger.info("  本轮新增 %d 题（补解析 %d，丢弃重复 %d），累计 %d 题，finish_reason=%s",
                    added, improved, dropped, len(blocks), finish)

        # 是否还有「缺解析」的题待补（仅 include_solution 时才追补）
        incomplete = (include_solution
                      and any(not _has_sol(b) for b in blocks))

        if finish != "length" and not incomplete:
            break  # 正常结束（stop）且无缺解析题，全部输出完成

        # 防死循环：连续两轮都没有任何进展（新增或补解析）就停
        progressed = (added > 0) or (improved > 0)
        stale_rounds = 0 if progressed else stale_rounds + 1
        if stale_rounds >= 2:
            logger.warning("连续 %d 轮无进展，停止续传（可能尾部题解析确实缺失）",
                           stale_rounds)
            break

    if only_numbers:
        # 我方兜底：只留题号在名单内的块（模型偶尔会多吐几题）。取不到题号的块，
        # 保守保留（宁多勿漏，交由用户在校对页取舍），避免误删。
        want = set(only_numbers)
        filtered = []
        for b in blocks:
            n = _block_number(b)
            if n is None or n in want:
                filtered.append(b)
        blocks = filtered

    return "\n\n".join(blocks)


import re as _re

# 题号：题块开头形如 "18." "18．" "18、" "（18）" "18 " 等，取首个 1~3 位数字
_NUM_RE = _re.compile(r"^\D{0,4}?(\d{1,3})\s*[.．、,，)）]")
# 中文大写题号：题块开头形如 "一、" "二．" "（三）" "第十一题" 等
_CN_NUM_RE = _re.compile(r"^[^一-鿿]{0,4}?第?\s*([一二三四五六七八九十百零]+)\s*[题、．.，,)）]")
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> int | None:
    """把中文数字（一~九十九，含「十」「二十一」这类）转成整数。失败返回 None。"""
    if not s:
        return None
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGIT.get(left, 1) if left else 1   # "十三"→1，"二十"→2
        ones = _CN_DIGIT.get(right, 0) if right else 0
        return tens * 10 + ones
    # 无「十」：单字或多位连读（如「一二」少见，逐位拼）
    total = 0
    for ch in s:
        d = _CN_DIGIT.get(ch)
        if d is None:
            return None
        total = total * 10 + d
    return total


def _block_number(block: str) -> int | None:
    """从题块首行提取题号（整数）。支持阿拉伯数字与中文大写题号。取不到返回 None。"""
    first = block.lstrip()
    if first.startswith("- "):
        first = first[2:]
    first = first.splitlines()[0] if first.splitlines() else ""
    first = first.strip()
    m = _NUM_RE.match(first)
    if m:
        return int(m.group(1))
    m = _CN_NUM_RE.match(first)
    if m:
        return _cn_to_int(m.group(1))
    return None


def _split_top_blocks(text: str) -> list[str]:
    """按顶层 `- ` 切出题块（保留前导 "- "）。与导入侧切分规则一致：
    行首 `- ` 且非两空格缩进视为新题起点。首个 `- ` 之前的开场白丢弃。
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("- ") and not line.startswith("  "):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b.strip()]


def _block_head(block: str, n: int = 40) -> str:
    """取题块首行（去掉前导 "- "）前 n 个字符，作为「已完成清单」里的一行摘要。"""
    first = block.lstrip()
    if first.startswith("- "):
        first = first[2:]
    first = first.splitlines()[0].strip() if first.splitlines() else ""
    return first[:n]


import re as _re

# 去重归一化用：LaTeX 命令（\displaystyle 等）、图片引用、HTML 标签、非字母数字汉字字符。
_DK_IMG_RE = _re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 表格标记（HTML 标签名与属性）必须在 _DK_KEEP_RE 之前剥掉：标签名 table/tr/td 全是
# 拉丁字母，_DK_CMD_RE（只吃 \命令）与 _DK_KEEP_RE（只吃标点空白）都会把它们留下。
# 不剥的话，两道毫不相干的表格题归一化后都以 `tabletrtdtdtd…` 这串模板开头，真正
# 的单元格内容被挤出 80 字符指纹之外，再叠上 _key_seen 的前缀判等，就会把不同的
# 表格题误判成重复而丢题。剥掉标签后只有单元格文字参与判等，两题自然区分。
# 管道表格的 `|` 与 `---` 分隔行由 _DK_KEEP_RE 自然吃掉，无需单独处理。
_DK_TAG_RE = _re.compile(r"<[^>]*>")
_DK_CMD_RE = _re.compile(r"\\[a-zA-Z]+")
_DK_KEEP_RE = _re.compile(r"[^0-9a-z一-鿿]+")


def _dedup_key(block: str) -> str:
    """题块去重键：取题干（`【解析】`之前），重度归一化后取指纹。

    只用题干、不含解析，避免解析差异把同题判成不同；重度归一化（去 LaTeX 命令、
    图片引用、标点空白、统一小写）让模型续传时同题的排版/全角半角/来源标注差异
    不再影响判等 —— 这是「中间多一段近似重复」的根治（旧版只压空白，漏过近似块）。
    """
    stem = block
    for i, line in enumerate(block.splitlines()):
        if line.strip().startswith("【解析】"):
            stem = "\n".join(block.splitlines()[:i])
            break
    # 去前导 "- "
    stem = stem.lstrip()
    if stem.startswith("- "):
        stem = stem[2:]
    s = stem.lower()
    s = _DK_IMG_RE.sub("", s)      # 图片引用不参与判等（同题配图路径可能不同）
    s = _DK_TAG_RE.sub("", s)      # 去 <table>/<tr>/<td> 等表格标记（理由见 _DK_TAG_RE）
    s = _DK_CMD_RE.sub("", s)      # 去 \displaystyle \dfrac \frac 等命令名
    s = _DK_KEEP_RE.sub("", s)     # 只留数字/小写字母/汉字（吃掉标点、空白、$、全半角符号）
    # 取归一化后前 80 字符做指纹：足够长以区分不同题，又略过尾部细微差异
    return s[:80]


# 前缀去重的最小可信长度：短于此的键不做前缀判等，避免不同短题误判为重复
_KEY_MIN_PREFIX = 24


def _key_seen(key: str, seen: set[str]) -> bool:
    """判断 key 是否与已见键重复。除精确相等外，还判「一个是另一个的前缀」——
    同题题干一处干净、一处尾部被污染（如答案泄漏进题干）时，短键是长键前缀。
    仅当较短键长度 >= _KEY_MIN_PREFIX 时才启用前缀判等，防短题误杀。"""
    if key in seen:
        return True
    for s in seen:
        shorter, longer = (key, s) if len(key) <= len(s) else (s, key)
        if len(shorter) >= _KEY_MIN_PREFIX and longer.startswith(shorter):
            return True
    return False


def _find_seen_index(key: str, blocks: list[str]):
    """在已收 blocks 里找与 key 判为同题的块下标（精确或前缀判等）；无则 None。
    用于「更丰富者替换」——补齐尾部题多轮续传时缺失的解析。"""
    for i, b in enumerate(blocks):
        bk = _dedup_key(b)
        if key == bk:
            return i
        shorter, longer = (key, bk) if len(key) <= len(bk) else (bk, key)
        if len(shorter) >= _KEY_MIN_PREFIX and longer.startswith(shorter):
            return i
    return None
