"""Agent 写操作快照与回滚回归测试。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_snapshots
import config


class SnapshotTests(unittest.TestCase):
    def test_snapshot_create_and_rollback_round_trip(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bank"
            snaps = Path(raw) / "snaps"
            target = root / "数学" / "a.md"
            target.parent.mkdir(parents=True)
            target.write_text("v1", encoding="utf-8")
            with mock.patch.object(config, "BANK_DIR", root), \
                    mock.patch.object(config, "AGENT_SNAPSHOTS_DIR", snaps):
                manifest = agent_snapshots.create_snapshot(
                    ["数学/a.md"], action="update_question", session_id="s1")
                self.assertIsNotNone(manifest)
                self.assertEqual(len(manifest["files"]), 1)
                target.write_text("v2", encoding="utf-8")
                result = agent_snapshots.rollback_snapshot(manifest["id"])
                self.assertEqual(result["restored"], ["数学/a.md"])
                self.assertIsNotNone(result["reverse_snapshot"])
                self.assertEqual(target.read_text(encoding="utf-8"), "v1")
                reverse = agent_snapshots.get_snapshot(
                    result["reverse_snapshot"])
                self.assertEqual(len(reverse["files"]), 1)

    def test_reserved_and_missing_paths_are_skipped(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bank"
            root.mkdir()
            snaps = Path(raw) / "snaps"
            with mock.patch.object(config, "BANK_DIR", root), \
                    mock.patch.object(config, "AGENT_SNAPSHOTS_DIR", snaps):
                self.assertIsNone(agent_snapshots.create_snapshot(
                    [".trash/x.md", "不存在.md", "../out.md"],
                    action="delete_questions"))

    def test_cleanup_removes_expired_snapshots(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bank"
            root.mkdir()
            (root / "a.md").write_text("x", encoding="utf-8")
            snaps = Path(raw) / "snaps"
            with mock.patch.object(config, "BANK_DIR", root), \
                    mock.patch.object(config, "AGENT_SNAPSHOTS_DIR", snaps):
                manifest = agent_snapshots.create_snapshot(
                    ["a.md"], action="tag_questions")
                self.assertIsNotNone(manifest)
                removed = agent_snapshots.cleanup_expired(
                    now=time.time() + 31 * 86400)
                self.assertEqual(removed, 1)
                self.assertEqual(agent_snapshots.list_snapshots(), [])
                self.assertIsNone(agent_snapshots.get_snapshot(manifest["id"]))


if __name__ == "__main__":
    unittest.main()
