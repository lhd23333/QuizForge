import threading
import time
import unittest
from unittest.mock import patch

import ocr_pool


class _CodedError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class OcrPoolTests(unittest.TestCase):
    def setUp(self):
        with ocr_pool._lock:
            for loads in ocr_pool._inflight.values():
                loads.clear()
            for backend in ocr_pool._cooldown_until:
                ocr_pool._cooldown_until[backend] = 0.0

    def test_least_busy_rotates_real_requests(self):
        seen = []
        barrier = threading.Barrier(2)

        def callback(value):
            seen.append(value)
            barrier.wait(timeout=1)
            return value

        with patch.object(ocr_pool.doc2x_store, "resolve_all",
                          return_value=["key-a", "key-b"]):
            # resolver 映射在导入时绑定函数，直接替换映射才能隔离测试数据。
            with patch.dict(ocr_pool._RESOLVERS,
                            {"doc2x": lambda: ["key-a", "key-b"]}):
                threads = [threading.Thread(
                    target=lambda: ocr_pool.run("doc2x", callback)) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)

        self.assertCountEqual(["key-a", "key-b"], seen)
        self.assertTrue(all(value == 0 for value in
                            ocr_pool.inflight_counts()["doc2x"]))

    def test_quota_error_switches_credential_once(self):
        seen = []

        def callback(value):
            seen.append(value)
            if len(seen) == 1:
                raise _CodedError("parse_quota_limit")
            return "ok"

        with patch.dict(ocr_pool._RESOLVERS,
                        {"doc2x": lambda: ["key-a", "key-b"]}):
            self.assertEqual("ok", ocr_pool.run("doc2x", callback))
        self.assertEqual(2, len(seen))
        self.assertNotEqual(seen[0], seen[1])

    def test_explicit_fallback_credential_takes_priority_over_pool(self):
        """显式兼容参数不能被设置页凭证池的轮转起点抢走。"""
        with patch.dict(ocr_pool._RESOLVERS,
                        {"doc2x": lambda: ["stored-key"]}), \
                patch.object(ocr_pool._ROTATION["doc2x"], "next",
                             return_value=1):
            used = ocr_pool.run(
                "doc2x", lambda credential: credential,
                fallback="explicit-key")

        self.assertEqual("explicit-key", used)

    def test_concurrency_error_retries_same_credential_after_cooldown(self):
        seen = []

        def callback(value):
            seen.append(value)
            if len(seen) == 1:
                raise _CodedError("parse_concurrency_limit")
            return "ok"

        with patch.dict(ocr_pool._RESOLVERS,
                        {"doc2x": lambda: ["key-a", "key-b"]}), \
                patch.object(ocr_pool.config, "OCR_LIMIT_COOLDOWN_SECONDS", 0):
            self.assertEqual("ok", ocr_pool.run("doc2x", callback))
        # 跨窗口轮转计数器的起点有意跨进程持久化，不能假定必从 key-a 开始；
        # 这条用例真正钉的是并发限流后仍使用同一份凭证重试。
        self.assertEqual(2, len(seen))
        self.assertEqual(seen[0], seen[1])
        self.assertIn(seen[0], ("key-a", "key-b"))

    def test_unknown_error_is_not_replayed(self):
        calls = []

        def callback(value):
            calls.append(value)
            raise ValueError("坏文件")

        with patch.dict(ocr_pool._RESOLVERS,
                        {"mineru": lambda: ["token-a", "token-b"]}):
            with self.assertRaisesRegex(ValueError, "坏文件"):
                ocr_pool.run("mineru", callback)
        # 轮转起点由跨窗口共享计数器决定，测试不能假设本进程一定从第一个候选
        # 开始；这里真正要钉的是未知错误只调用一次、不会换号重放。
        self.assertEqual(1, len(calls))
        self.assertIn(calls[0], ("token-a", "token-b"))

    def test_resume_state_finds_original_token_without_resubmitting(self):
        seen = []

        def callback(value):
            seen.append(value)
            if len(seen) < 3:
                raise _CodedError("resume_token_mismatch")
            return "continued"

        with patch.dict(ocr_pool._RESOLVERS, {
                "mineru": lambda: ["token-a", "token-b", "token-c"]}):
            self.assertEqual("continued", ocr_pool.run("mineru", callback))

        self.assertEqual(3, len(seen))
        self.assertEqual(3, len(set(seen)))

    def test_second_credential_runtime_error_is_not_hidden(self):
        calls = []

        def callback(value):
            calls.append(value)
            if len(calls) == 1:
                raise _CodedError("parse_quota_limit")
            raise RuntimeError("备用凭证实际失败")

        with patch.dict(ocr_pool._RESOLVERS,
                        {"doc2x": lambda: ["key-a", "key-b"]}):
            with self.assertRaisesRegex(RuntimeError, "备用凭证实际失败"):
                ocr_pool.run("doc2x", callback)

    def test_process_semaphore_context_releases_slot(self):
        semaphore = ocr_pool._ProcessSemaphore("OCR.Test", 1)
        try:
            with semaphore:
                self.assertEqual(1, semaphore.limit)
            with semaphore:
                self.assertEqual(1, semaphore.limit)
        finally:
            semaphore.close()

    def test_shared_cooldown_keeps_latest_deadline(self):
        cooldown = ocr_pool._SharedCooldown("OCR.Test")
        try:
            now = time.time()
            cooldown.mark(now + 1)
            cooldown.mark(now + 0.5)
            self.assertGreaterEqual(cooldown.deadline(), now + 1)
        finally:
            cooldown.close()


if __name__ == "__main__":
    unittest.main()
