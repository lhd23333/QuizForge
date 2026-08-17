r"""查重：归一化题目正文 → 指纹（完全重复）+ 相似度（措辞近似）。

纯函数，只用 Python 标准库（re / hashlib / difflib），不加依赖。

归一化目的：同一道题因公式写法（\dfrac vs \frac）、空白、标点、$ 包裹
差异不应判为不同。normalize 后得到「指纹文本」，指纹相同=完全重复。
"""

import re
import hashlib
import math
from collections import Counter
from difflib import SequenceMatcher

# 同义 LaTeX 命令归一（写法不同但含义相同）
_SYNONYM = [
    (r"\dfrac", r"\frac"),
    (r"\tfrac", r"\frac"),
    (r"\left", ""),
    (r"\right", ""),
    (r"\displaystyle", ""),
    (r"\,", ""), (r"\;", ""), (r"\:", ""), (r"\!", ""),  # 数学空白
]

# 要删除的标点/空白（中英文），归一化最后一步
_PUNCT = "，。、；：？！“”‘’（）()[]{}【】《》…—－-·,.;:?!\"'`~@#%^&*_+=|\\/<> \t\r\n　"
_PUNCT_SET = set(_PUNCT)


def normalize(body: str) -> str:
    """把题目正文归一化为「指纹文本」：去公式包裹/同义命令/LaTeX 命令/标点空白，转小写。"""
    s = body or ""
    # 1. 同义命令归一
    for a, b in _SYNONYM:
        s = s.replace(a, b)
    # 2. 删除 LaTeX 反斜杠命令（如 \triangle \sum \mathbb），保留其后普通字符
    s = re.sub(r"\\[a-zA-Z]+", " ", s)
    # 3. 删除剩余反斜杠、美元符、花括号包裹符
    s = s.replace("$", "").replace("\\", "")
    # 4. 删标点与空白，转小写
    s = "".join(ch for ch in s if ch not in _PUNCT_SET)
    return s.lower()


def fingerprint(body: str) -> str:
    """归一化后的 md5 十六进制指纹；指纹相同即完全重复。"""
    return hashlib.md5(normalize(body).encode("utf-8")).hexdigest()


def similarity(a: str, b: str) -> float:
    """两题归一化文本的相似度 0~1（difflib.SequenceMatcher）。"""
    na, nb = normalize(a), normalize(b)
    if not na and not nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def find_duplicates(items: list[dict], threshold: float = 0.85,
                    progress=None, checkpoint=None) -> list[dict]:
    """在题目列表里找重复。

    items: [{"id":.., "body":.., "fingerprint": 可选}, ...]
      带 fingerprint（库里存的那一列）时直接用，省掉整库重算一遍 normalize+md5。
    返回重复组列表，每组：
      {"kind": "exact"|"similar", "members": [item, ...], "score": 相似度或1.0}
    先按指纹分组找完全重复；再对「不同指纹」的题两两算相似度 ≥ threshold 配对。

    相似度那一步天然是 O(n²) 对，而 SequenceMatcher 本身又是 O(len²)，题量上千
    时页面会卡到几十秒。下面用两道**数学上严格**的剪枝把绝大多数对挡在
    SequenceMatcher 之前（见各自注释）——严格意味着被挡掉的对相似度必定
    < threshold，结果与逐对硬算完全一致，不是"抽样近似"。
    """
    groups = []

    # 1. 完全重复：按指纹分组
    by_fp: dict[str, list[dict]] = {}
    for index, it in enumerate(items):
        if checkpoint and index % 64 == 0:
            checkpoint()
        fp = it.get("fingerprint") or fingerprint(it["body"])
        by_fp.setdefault(fp, []).append(it)
    exact_ids = set()
    for fp, members in by_fp.items():
        if len(members) > 1:
            groups.append({"kind": "exact", "members": members, "score": 1.0})
            exact_ids.update(m["id"] for m in members)

    # 2. 相似（近似）：只比不同指纹、且未在完全重复组里的题
    rest = [it for it in items if it["id"] not in exact_ids]
    # 按归一化长度升序排，让长度剪枝能 break 而不只是 continue（见下）
    prepared = []
    for index, it in enumerate(rest):
        if checkpoint and index % 64 == 0:
            checkpoint()
        n = normalize(it["body"])
        prepared.append((len(n), n, Counter(n), it))
    prepared.sort(key=lambda x: x[0])

    # 长度上界：SequenceMatcher 的 ratio = 2M/(la+lb)，其中 M 是匹配字符数，
    # 显然 M ≤ min(la, lb)。设 la ≤ lb，则 ratio ≤ 2·la/(la+lb)，要它 ≥ t
    # 必须 lb ≤ la·(2-t)/t。t=0.85 时是 1.353 倍——比原来写死的 1.5 倍更紧，
    # 且这个系数随 threshold 自动变（原来那句注释里的 0.85 是写死的假设）。
    ratio_cap = (2.0 - threshold) / threshold if threshold > 0 else float("inf")

    # 倒排表只负责生成候选，后面的长度、多重集和 SequenceMatcher 仍逐层做严格判定。
    # 对题 a，若相似度 >= t（且 lb >= la），字符多重集交集至少为 ceil(t*la)。
    # 因此从 a 中挑出总出现次数 > la-ceil(t*la) 的若干字符后，合格的 b 必须至少
    # 含其中一个字符。按全库出现题数从少到多挑，通常一个生僻汉字就能把候选从
    # 万级缩到个位数；这是无损剪枝，不会漏掉原算法会命中的重复题。
    postings: dict[str, list[int]] = {}
    for index, (_length, _text, counts, _item) in enumerate(prepared):
        for char in counts:
            postings.setdefault(char, []).append(index)

    paired = set()
    if progress:
        progress(0, len(prepared))
    for i in range(len(prepared)):
        if checkpoint:
            checkpoint()
        la, na, ca, a = prepared[i]
        if progress and (i % 64 == 0 or i + 1 == len(prepared)):
            progress(i + 1, len(prepared))
        if not la:
            continue
        required_overlap = math.ceil(threshold * la)
        uncovered_limit = la - required_overlap
        covered = 0
        anchors = []
        for char in sorted(ca, key=lambda value: (len(postings[value]), value)):
            anchors.append(char)
            covered += ca[char]
            if covered > uncovered_limit:
                break
        candidates = set()
        for char in anchors:
            candidates.update(postings[char])
        for j in sorted(candidates):
            if checkpoint and j % 64 == 0:
                checkpoint()
            if j <= i:
                continue
            lb, nb, cb, b = prepared[j]
            # 已按长度升序，后面的只会更长 → 一旦超界，后面全部超界，直接 break
            if la and lb > la * ratio_cap:
                break
            if not la or not lb:
                continue
            # 字符多重集交集上界：每个匹配字符对都要各消耗两边一个相同字符，
            # 所以 M ≤ |多重集交集|，于是 ratio ≤ 2·inter/(la+lb)。这一步是
            # O(不同字符数)，比 SequenceMatcher 的 O(la·lb) 便宜得多，用来把
            # "长度接近但内容无关"的对（数学题里很常见）提前挡掉。
            inter = sum((ca & cb).values())
            if 2.0 * inter / (la + lb) < threshold:
                continue
            score = SequenceMatcher(None, na, nb).ratio()
            if score >= threshold:
                key = (a["id"], b["id"])
                if key not in paired:
                    paired.add(key)
                    groups.append({"kind": "similar",
                                   "members": [a, b], "score": round(score, 3)})
    return groups
