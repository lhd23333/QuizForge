"""查重候选剪枝与后台页面回归；不访问真实题库。"""

import random
import threading
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

    def test_checkpoint_is_called_without_changing_results(self):
        calls = []
        items = [
            {"id": "a", "body": "已知函数求最大值"},
            {"id": "b", "body": "已知函数求最大值是多少"},
        ]
        result = dedup.find_duplicates(
            items, threshold=0.5, checkpoint=lambda: calls.append(1))
        self.assertTrue(calls)
        self.assertEqual(result[0]["kind"], "similar")


class DedupRouteTests(unittest.TestCase):
    def setUp(self):
        import app
        self.app_module = app
        with app._dedup_jobs_lock:
            app._dedup_jobs.clear()

    def tearDown(self):
        with self.app_module._dedup_jobs_lock:
            self.app_module._dedup_jobs.clear()

    def test_page_does_not_start_scan_automatically(self):
        response = self.app_module.app.test_client().get("/dedup")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"data-auto-start", response.data)
        self.assertIn("开始扫描".encode("utf-8"), response.data)

    def test_running_job_can_pause_and_resume(self):
        gate = threading.Event()
        gate.set()
        with self.app_module._dedup_jobs_lock:
            self.app_module._dedup_jobs["test-job"] = {
                "status": "scanning", "threshold": 0.85, "total": 10,
                "compared": 3, "groups": None, "error": "",
                "created_at": 0, "resume_event": gate,
                "resume_status": "scanning",
            }
        headers = {"X-CSRF-Token": self.app_module._WRITE_TOKEN}
        client = self.app_module.app.test_client()

        paused = client.post(
            "/api/dedup/test-job/control", json={"action": "pause"},
            headers=headers)
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.get_json()["status"], "paused")
        self.assertFalse(gate.is_set())

        resumed = client.post(
            "/api/dedup/test-job/control", json={"action": "resume"},
            headers=headers)
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["status"], "scanning")
        self.assertTrue(gate.is_set())


if __name__ == "__main__":
    unittest.main()
