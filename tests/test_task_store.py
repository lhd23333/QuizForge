"""task_store 的资料库任务快照回归。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import task_store


class LibraryTaskStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tasks_path = Path(self._tmp.name) / "conversion_tasks.json"
        self._patch = mock.patch.object(config, "TASKS_PATH", self._tasks_path)
        self._patch.start()
        task_store._write_unlocked(task_store._empty())

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_library_kind_round_trip(self):
        self.assertIn("library", task_store.KINDS)
        task_store.save("library", "pdf-1", {
            "status": "done", "operation": "split", "output": "a.pdf",
        })

        self.assertEqual(
            task_store.load("library"),
            [("pdf-1", {
                "status": "done", "operation": "split", "output": "a.pdf",
            })],
        )
        raw = self._tasks_path.read_text(encoding="utf-8")
        self.assertIn('"library"', raw)

    def test_mark_interrupted_updates_active_nodes_and_keeps_terminal_nodes(self):
        task_store.save("library", "batch-1", {
            "status": "queued", "running": 1,
            "groups": [
                {"status": "running", "in_flight": True},
                {"status": "done", "output": "ready.pdf"},
            ],
        })

        restored = task_store.mark_interrupted(
            "library", {"queued", "running"}, "后端重启，请手动重试")
        payload = dict(restored)["batch-1"]
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["error"], "后端重启，请手动重试")
        self.assertEqual(payload["running"], 0)
        self.assertIsInstance(payload["interrupted_at"], float)
        self.assertEqual(payload["groups"][0]["status"], "interrupted")
        self.assertFalse(payload["groups"][0]["in_flight"])
        self.assertEqual(payload["groups"][1]["status"], "done")
        self.assertEqual(payload["groups"][1]["output"], "ready.pdf")

        persisted = dict(task_store.load("library"))["batch-1"]
        self.assertEqual(persisted["status"], "interrupted")

    def test_mark_interrupted_does_not_rewrite_terminal_task(self):
        task_store.save("library", "done-1", {
            "status": "done", "finished_at": 123,
        })
        before = self._tasks_path.read_bytes()
        restored = task_store.mark_interrupted(
            "library", {"running"}, "不应覆盖终态")
        self.assertEqual(dict(restored)["done-1"]["status"], "done")
        self.assertEqual(self._tasks_path.read_bytes(), before)

    def test_purge_expired_includes_library_tasks(self):
        old = time.time() - 9 * 86400
        snapshots = task_store._empty()
        snapshots["library"]["old-1"] = {
            "updated_at": old,
            "payload": {"status": "interrupted", "path": "old.pdf"},
        }
        task_store._write_unlocked(snapshots)

        removed = task_store.purge_expired(days=7)
        self.assertEqual(removed, [{"status": "interrupted", "path": "old.pdf"}])
        self.assertEqual(task_store.load("library"), [])

    def test_purge_expired_keeps_unresolved_source_tasks(self):
        """等待用户处理的来源任务不随 7 天过期清理：记录一旦消失，同内容的
        源文件会被扫描器当成新文件自动重放（可能重复消耗 OCR 额度）。"""
        old = time.time() - 9 * 86400
        snapshots = task_store._empty()
        snapshots["source"]["s-failed"] = {
            "updated_at": old,
            "payload": {"status": "failed", "source_key": "k1"},
        }
        snapshots["source"]["s-done"] = {
            "updated_at": old,
            "payload": {"status": "committed", "source_key": "k2"},
        }
        task_store._write_unlocked(snapshots)

        removed = task_store.purge_expired(days=7)
        self.assertEqual([row["status"] for row in removed], ["committed"])
        self.assertEqual([tid for tid, _ in task_store.load("source")],
                         ["s-failed"])

    def test_mark_interrupted_validates_arguments(self):
        with self.assertRaises(ValueError):
            task_store.mark_interrupted("unknown", {"running"}, "x")
        with self.assertRaises(ValueError):
            task_store.mark_interrupted("library", {"running"}, "")

    def test_source_namespace_coexists_and_legacy_snapshots_are_preserved(self):
        legacy = {
            "job": {"j1": {"updated_at": 1, "payload": {"status": "done"}}},
            "batch": {"b1": {"updated_at": 2, "payload": {"count": 3}}},
            "library": {"l1": {"updated_at": 3, "payload": {"path": "x.pdf"}}},
        }
        self._tasks_path.write_text(
            __import__("json").dumps(legacy), encoding="utf-8")

        self.assertEqual(task_store.load("job"), [("j1", {"status": "done"})])
        self.assertEqual(task_store.load("batch"), [("b1", {"count": 3})])
        self.assertEqual(task_store.load("library"), [("l1", {"path": "x.pdf"})])
        task_store.save("source", "s1", {"status": "done"})
        self.assertEqual(task_store.load("source"), [("s1", {"status": "done"})])
        self.assertEqual(task_store.load("job"), [("j1", {"status": "done"})])


if __name__ == "__main__":
    unittest.main()
