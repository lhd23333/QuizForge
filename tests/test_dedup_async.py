"""查重候选剪枝与后台页面回归；不访问真实题库。"""

import random
import unittest
from collections import Counter
from difflib import SequenceMatcher

import dedup


def _reference(items, threshold):
    """旧版逐对算法，作为无损候选剪枝的结果基准。"""
    groups = []
    by_fp = {}
    for item in items:
        by_fp.setdefault(dedup.fingerprint(item["body"]), []).append(item)
    exact_ids = set()
    for members in by_fp.values():
        if len(members) > 1:
            groups.append(("exact", tuple(item["id"] for item in members)))
            exact_ids.update(item["id"] for item in members)
    rest = [item for item in items if item["id"] not in exact_ids]
    prepared = []
    for item in rest:
        normalized = dedup.normalize(item["body"])
        prepared.append((len(normalized), normalized, Counter(normalized), item))
    prepared.sort(key=lambda value: value[0])
    cap = (2.0 - threshold) / threshold
    for i, (la, na, ca, a) in enumerate(prepared):
        for lb, nb, cb, b in prepared[i + 1:]:
            if la and lb > la * cap:
                break
            if not la or not lb:
                continue
            if 2.0 * sum((ca & cb).values()) / (la + lb) < threshold:
                continue
            if SequenceMatcher(None, na, nb).ratio() >= threshold:
                groups.append(("similar", (a["id"], b["id"])))
    return groups


class DedupCandidateTests(unittest.TestCase):
    def test_indexed_candidates_match_exhaustive_reference(self):
        rng = random.Random(20260813)
        alphabet = "函数几何代数概率向量abcdefxyz123456789"
        items = []
        for index in range(100):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(12, 45)))
            items.append({"id": f"q{index}", "body": text})
        items.extend([
            {"id": "exact-a", "body": "已知函数 f(x)=x^2，求最小值"},
            {"id": "exact-b", "body": "已知函数f(x)=x^2 求最小值。"},
            {"id": "similar-a", "body": "三角形ABC中已知角A等于六十度求边长"},
            {"id": "similar-b", "body": "三角形ABC中已知角A等于六十度求边长是多少"},
        ])
        for threshold in (0.5, 0.75, 0.85, 1.0):
            expected = _reference(items, threshold)
            actual = [
                (group["kind"], tuple(item["id"] for item in group["members"]))
                for group in dedup.find_duplicates(items, threshold)
            ]
            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
