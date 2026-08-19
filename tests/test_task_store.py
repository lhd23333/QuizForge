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

    def test_mark_interrupted_validates_arguments(self):
        with self.assertRaises(ValueError):
            task_store.mark_interrupted("unknown", {"running"}, "x")
        with self.assertRaises(ValueError):
            task_store.mark_interrupted("library", {"running"}, "")


if __name__ == "__main__":
    unittest.main()
