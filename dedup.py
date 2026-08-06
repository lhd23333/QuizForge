r"""查重：归一化题目正文 → 指纹（完全重复）+ 相似度（措辞近似）。

纯函数，只用 Python 标准库（re / hashlib / difflib），不加依赖。

归一化目的：同一道题因公式写法（\dfrac vs \frac）、空白、标点、$ 包裹
差异不应判为不同。normalize 后得到「指纹文本」，指纹相同=完全重复。
"""

import re
import hashlib
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


def find_duplicates(items: list[dict], threshold: float = 0.85) -> list[dict]:
    """在题目列表里找重复。

    items: [{"id":.., "body":..}, ...]
    返回重复组列表，每组：
      {"kind": "exact"|"similar", "members": [item, ...], "score": 相似度或1.0}
    先按指纹分组找完全重复；再对「不同指纹」的题两两算相似度 ≥ threshold 配对。
    """
    groups = []

    # 1. 完全重复：按指纹分组
    by_fp: dict[str, list[dict]] = {}
    for it in items:
        by_fp.setdefault(fingerprint(it["body"]), []).append(it)
    exact_ids = set()
    for fp, members in by_fp.items():
        if len(members) > 1:
            groups.append({"kind": "exact", "members": members, "score": 1.0})
            exact_ids.update(m["id"] for m in members)

    # 2. 相似（近似）：只比不同指纹、且未在完全重复组里的题，两两比较
    #    归一化文本长度差异大的直接跳过（剪枝，避免明显不相似的比对）
    rest = [it for it in items if it["id"] not in exact_ids]
    norms = {it["id"]: normalize(it["body"]) for it in rest}
    paired = set()
    for i in range(len(rest)):
        a = rest[i]
        na = norms[a["id"]]
        for j in range(i + 1, len(rest)):
            b = rest[j]
            nb = norms[b["id"]]
            # 剪枝：长度相差超过 1.5 倍，相似度不可能达到 0.85
            la, lb = len(na), len(nb)
            if la and lb and (max(la, lb) / min(la, lb) > 1.5):
                continue
            score = SequenceMatcher(None, na, nb).ratio()
            if score >= threshold:
                key = (a["id"], b["id"])
                if key not in paired:
                    paired.add(key)
                    groups.append({"kind": "similar",
                                   "members": [a, b], "score": round(score, 3)})
    return groups
