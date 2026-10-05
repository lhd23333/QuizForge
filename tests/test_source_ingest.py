from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import source_ingest
import source_versions
import task_store


class SourceIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.in_dir = self.root / "好题"
        self.out_dir = self.root / "输出"
        self.in_dir.mkdir()
        self.out_dir.mkdir()
        self.settings = mock.patch.object(config, "SOURCE_SETTINGS_PATH",
                                          self.root / "settings.json")
        self.settings.start()
        self.tasks = mock.patch.object(config, "TASKS_PATH", self.root / "tasks.json")
        self.tasks.start()
        self.workspace = mock.patch.object(config, "SOURCE_WORKSPACE_DIR",
                                           self.root / "workspace")
        self.workspace.start()
        # 版本账本是真实磁盘文件（原地替换的产物排除靠它读），必须指向临时
        # 路径，否则测试会读写用户的真实来源版本登记。
        self.versions = mock.patch.object(config, "SOURCE_VERSIONS_PATH",
                                          self.root / "versions.json")
        self.versions.start()

    def tearDown(self):
        self.versions.stop()
        self.workspace.stop()
        self.tasks.stop()
        self.settings.stop()
        self.tmp.cleanup()

    def _service(self, profiles):
        return source_ingest.SourceIngestService(profiles=profiles, interval=999)

    def _in_place_profiles(self):
        """默认语义：输入目录 == 输出目录（原地替换）。"""
        return {"good_question": {"input_dir": str(self.in_dir),
                                  "output_dir": str(self.in_dir),
                                  "enabled": True}}

    # ------------------------------------------------------------------
    # 稳定性
    # ------------------------------------------------------------------

    def test_stability_requires_two_unchanged_observations(self):
        path = self.in_dir / "q.png"
        path.write_bytes(b"x")
        stable, state = source_ingest.is_stable_file(path, None, now=1.0)
        self.assertFalse(stable)
        stable, state = source_ingest.is_stable_file(path, state, now=2.0)
        self.assertTrue(stable)

    def test_missing_file_between_scans_is_not_fatal(self):
        path = self.in_dir / "gone.png"
        path.write_bytes(b"x")
        service = self._service(self._in_place_profiles())
        service.scan_once()
        path.unlink()  # 文件在观察间隙消失
        self.assertEqual(service.scan_once(), [])  # 不允许抛异常

    # ------------------------------------------------------------------
    # 核心回归：输入=输出（原地替换）
    # ------------------------------------------------------------------

    def test_in_place_scan_queues_new_file_on_second_pass(self):
        """回归：input==output 时新文件必须能入队（旧实现整树排除输出目录，
        把所有输入都挡在门外）。"""
        source = self.in_dir / "q.png"
        source.write_bytes(b"x")
        service = self._service(self._in_place_profiles())
        self.assertEqual(service.scan_once(), [])   # 第一次只观察
        queued = service.scan_once()                # 第二次稳定 → 入队
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["source"], str(source.resolve()))
        self.assertFalse(queued[0]["normalize_with_llm"])

    def test_in_place_scan_skips_registered_version_outputs(self):
        """已登记版本的产物不是输入：单卡 .md 与版本目录（含目录内图片）。"""
        card = self.in_dir / "旧卡.md"
        card.write_text("old", encoding="utf-8")
        version_dir = self.in_dir / "圆梦杯"
        version_dir.mkdir()
        (version_dir / "圆梦杯第1题.md").write_text("card", encoding="utf-8")
        (version_dir / "插图.png").write_bytes(b"img")
        config.SOURCE_VERSIONS_PATH.write_text(json.dumps([
            {"version_id": "v1", "profile": "good_question", "source_key": "k1",
             "path": str(card), "is_directory": False, "status": "committed"},
            {"version_id": "v2", "profile": "good_paper", "source_key": "k2",
             "path": str(version_dir), "is_directory": True, "status": "committed"},
        ]), encoding="utf-8")
        fresh = self.in_dir / "q.png"
        fresh.write_bytes(b"x")
        service = self._service(self._in_place_profiles())
        service.scan_once()
        queued = service.scan_once()
        self.assertEqual([row["source"] for row in queued],
                         [str(fresh.resolve())])

    def test_content_deduplicates_against_task_ledger(self):
        """input != output 的独立输出目录场景：同内容只入队一次。"""
        source = self.in_dir / "q.png"
        source.write_bytes(b"x")
        profiles = {"good_question": {"input_dir": str(self.in_dir),
                                      "output_dir": str(self.out_dir),
                                      "enabled": True}}
        service = self._service(profiles)
        service.scan_once()
        self.assertEqual(len(service.scan_once()), 1)
        # 同一 service 再扫：_known 挡下；新建 service（模拟重启）也挡下。
        self.assertEqual(service.scan_once(), [])
        restarted = self._service(profiles)
        restarted.scan_once()
        self.assertEqual(restarted.scan_once(), [])

    def test_output_subtree_nested_in_input_is_skipped(self):
        """输出是输入的真子目录 → 它是专用输出区，里面文件不算输入。"""
        nested = self.in_dir / "输出"
        nested.mkdir()
        (nested / "generated.png").write_bytes(b"out")
        fresh = self.in_dir / "q.png"
        fresh.write_bytes(b"x")
        profiles = {"good_question": {"input_dir": str(self.in_dir),
                                      "output_dir": str(nested),
                                      "enabled": True}}
        service = self._service(profiles)
        service.scan_once()
        queued = service.scan_once()
        self.assertEqual([row["source"] for row in queued],
                         [str(fresh.resolve())])

    def test_known_interrupted_task_is_not_requeued(self):
        """重启后不能自动重放可能已计费的中断任务；只允许用户显式重试。"""
        source = self.in_dir / "q.png"
        source.write_bytes(b"x")
        digest = hashlib.sha256(b"x").hexdigest()
        key = source_versions.version_key(
            "good_question", source.resolve(), digest)
        task_store.save("source", "t1", {
            "profile": "good_question", "source": str(source.resolve()),
            "source_hash": digest, "source_key": key,
            "output_dir": str(self.in_dir), "status": "interrupted"})
        service = self._service(self._in_place_profiles())
        service.scan_once()
        self.assertEqual(service.scan_once(), [])

    def test_pause_and_resume_control_scanning(self):
        source = self.in_dir / "q.png"
        source.write_bytes(b"x")
        service = self._service(self._in_place_profiles())
        service.pause("good_question")
        self.assertEqual(service.scan_once(), [])
        service.resume("good_question")
        self.assertIsInstance(service.scan_once(), list)

    # ------------------------------------------------------------------
    # 线程生命周期
    # ------------------------------------------------------------------

    def test_start_clears_residual_sentinel_from_previous_stop(self):
        """worker 忙时 stop() 投出的哨兵会残留；start() 必须清掉它，
        否则新 worker 拿到哨兵立即签退——"监控开着、永远不执行任务"。"""
        profiles = self._in_place_profiles()
        service = self._service(profiles)
        service._queue.put(source_ingest._SENTINEL)  # 模拟上次 stop 的残留
        executed = []
        service._execute = lambda task_id: executed.append(task_id)
        service.start()
        try:
            service._queue.put("t1")
            deadline = time.time() + 5
            while not executed and time.time() < deadline:
                time.sleep(0.02)
        finally:
            service.stop()
        self.assertEqual(executed, ["t1"])

    def test_stop_marks_queued_tasks_interrupted(self):
        """stop 时还没开工的任务不能静默丢在内存队列里。"""
        service = self._service(self._in_place_profiles())
        task_store.save("source", "t9", {
            "profile": "good_question", "source": str(self.in_dir / "x.png"),
            "source_key": "k", "output_dir": str(self.in_dir), "status": "queued"})
        service._queue.put("t9")
        service.stop()
        self.assertEqual(dict(task_store.load("source"))["t9"]["status"],
                         "interrupted")

    def test_execute_end_to_end_commits_and_recycles(self):
        """scan → execute → 提交全链路（假 OCR 替代真实付费调用）：产物落进
        输出目录、源文件交系统回收站、任务快照 committed、再次扫描不重复。"""
        import source_profiles

        source = self.in_dir / "q.png"
        source.write_bytes(b"image-bytes")
        service = self._service(self._in_place_profiles())
        service.scan_once()
        queued = service.scan_once()
        self.assertEqual(len(queued), 1)
        task_id = queued[0]["task_id"]

        def fake_convert(source_path, workspace, *, ocr_backend, engine,
                         provider=None, normalize=True):
            self.assertFalse(normalize)   # 默认机械链路
            self.assertIsNone(provider)   # 机械链路不得解析 LLM 配置
            card = Path(workspace) / "q第3题.md"
            card.write_text("3. 题目", encoding="utf-8")
            return {"cards": [card.name], "warnings": []}

        with mock.patch.object(source_profiles, "convert_good_question",
                               fake_convert), \
                mock.patch("source_recycle.send_to_recycle_bin") as recycle:
            service._execute(task_id)
        recycle.assert_called_once()
        tasks = dict(task_store.load("source"))
        self.assertEqual(tasks[task_id]["status"], "committed")
        self.assertTrue((self.in_dir / "q第3题.md").exists())
        # 已提交后再扫：账本挡住，不会重复转换
        self.assertEqual(service.scan_once(), [])

    def _fake_question_convert(self, card_name="q第3题.md"):
        def fake_convert(source_path, workspace, *, ocr_backend, engine,
                         provider=None, normalize=True):
            card = Path(workspace) / card_name
            card.write_text("3. 题目", encoding="utf-8")
            return {"cards": [card.name], "warnings": []}
        return fake_convert

    def test_in_place_keeps_source_subdirectory_structure(self):
        """原地替换 + 用户在监视目录里建了子文件夹：产物写回同一子目录。"""
        import source_profiles

        sub = self.in_dir / "2026年" / "函数"
        sub.mkdir(parents=True)
        source = sub / "q.png"
        source.write_bytes(b"image-bytes")
        service = self._service(self._in_place_profiles())
        service.scan_once()
        queued = service.scan_once()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["input_dir"], str(self.in_dir.resolve()))
        with mock.patch.object(source_profiles, "convert_good_question",
                               self._fake_question_convert()), \
                mock.patch("source_recycle.send_to_recycle_bin") as recycle:
            service._execute(queued[0]["task_id"])
        recycle.assert_called_once()
        self.assertTrue((sub / "q第3题.md").exists())
        self.assertFalse((self.in_dir / "q第3题.md").exists())

    def test_separate_output_does_not_mirror_subdirectories(self):
        """输入输出不同：不镜像子文件夹结构，产物平铺在输出根。"""
        import source_profiles

        sub = self.in_dir / "2026年"
        sub.mkdir()
        source = sub / "q.png"
        source.write_bytes(b"image-bytes")
        profiles = {"good_question": {"input_dir": str(self.in_dir),
                                      "output_dir": str(self.out_dir),
                                      "enabled": True}}
        service = self._service(profiles)
        service.scan_once()
        queued = service.scan_once()
        self.assertEqual(len(queued), 1)
        with mock.patch.object(source_profiles, "convert_good_question",
                               self._fake_question_convert()), \
                mock.patch("source_recycle.send_to_recycle_bin"):
            service._execute(queued[0]["task_id"])
        self.assertTrue((self.out_dir / "q第3题.md").exists())
        self.assertFalse((self.out_dir / "2026年").exists())

    # ------------------------------------------------------------------
    # 管理动作
    # ------------------------------------------------------------------

    def test_remove_allows_only_committed_records(self):
        service = self._service(self._in_place_profiles())
        task_store.save("source", "t1", {"status": "committed",
                                         "source_key": "k",
                                         "profile": "good_question"})
        task_store.save("source", "t2", {"status": "failed",
                                         "source_key": "k2",
                                         "profile": "good_question"})
        self.assertIsNone(service.remove("nope"))
        with self.assertRaises(ValueError):
            service.remove("t2")
        payload = service.remove("t1")
        self.assertEqual(payload["status"], "committed")
        self.assertNotIn("t1", dict(task_store.load("source")))

    def test_dismiss_hides_failed_records_but_keeps_fingerprint(self):
        """忽略 = 记录改 dismissed：红心计数下降、重启后指纹仍在（防重放）。"""
        service = self._service(self._in_place_profiles())
        task_store.save("source", "t1", {"status": "failed",
                                         "source_key": "k-failed",
                                         "profile": "good_question"})
        self.assertEqual(service.unfinished_count(), 1)
        payload = service.dismiss("t1")
        self.assertEqual(payload["status"], "dismissed")
        self.assertEqual(service.unfinished_count(), 0)
        # 新进程新服务：_known 从快照重建后仍认识该指纹，扫描器不会把内容
        # 未变的源文件当新文件重放（可能重复消耗 OCR 额度）。
        fresh = self._service(self._in_place_profiles())
        fresh._ensure_state()
        self.assertIn("k-failed", fresh._known)
        # 已忽略的记录可以重试恢复（"连已完成一起看"视图的操作入口）。
        retried = fresh.retry("t1")
        self.assertEqual(retried["status"], "queued")

    def test_dismiss_rejects_missing_and_committed_records(self):
        service = self._service(self._in_place_profiles())
        task_store.save("source", "t1", {"status": "committed",
                                         "source_key": "k",
                                         "profile": "good_question"})
        self.assertIsNone(service.dismiss("nope"))
        with self.assertRaises(ValueError):
            service.dismiss("t1")


if __name__ == "__main__":
    unittest.main()
