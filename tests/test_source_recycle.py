"""来源自动导入的系统回收站与输出提交事务。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import source_recycle
import source_versions


class SourceRecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.versions = self.root / "versions.json"
        self.patch = mock.patch.object(config, "SOURCE_VERSIONS_PATH", self.versions)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_send_to_recycle_bin_uses_allow_undo_and_does_not_unlink_directly(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        with mock.patch.object(source_recycle, "_shell_delete") as shell:
            source_recycle.send_to_recycle_bin(source)
        shell.assert_called_once_with(source)
        self.assertTrue(source.exists())

    def test_manifest_requires_existing_hashed_files_and_safe_relative_paths(self):
        staged = self.root / "staged"
        staged.mkdir()
        card = staged / "card.md"
        card.write_text("# card", encoding="utf-8")
        digest = hashlib.sha256(card.read_bytes()).hexdigest()
        manifest = {"source_path": str(self.root / "source.png"),
                    "files": [{"path": "card.md", "sha256": digest}]}
        self.assertEqual(source_versions.validate_manifest(manifest)["files"][0]["path"],
                         "card.md")
        with self.assertRaises(ValueError):
            source_versions.validate_manifest({**manifest,
                "files": [{"path": "../escape.md", "sha256": digest}]})
        with self.assertRaises(ValueError):
            source_versions.validate_manifest({**manifest,
                "files": [{"path": "card.md", "sha256": digest},
                           {"path": "card.md", "sha256": digest}]})

    def test_commit_rejects_symlinked_staged_output(self):
        staged = self.root / "staged"
        staged.mkdir()
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        link = staged / "card.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("当前 Windows 权限不支持创建符号链接")
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            source_versions.commit_outputs(
                {"version_id": "x", "path": str(self.root / "out"),
                 "status": "reserved"}, staged,
                {"source_path": str(self.root / "source.png"),
                 "files": [{"path": "card.md", "sha256": digest}]})

    def test_commit_outputs_marks_recycle_pending_without_duplicate_version(self):
        output = self.root / "out"
        output.mkdir()
        staged = self.root / "staged"
        staged.mkdir()
        card = staged / "card.md"
        card.write_text("# card", encoding="utf-8")
        source = self.root / "source.png"
        source.write_bytes(b"source")
        digest = hashlib.sha256(card.read_bytes()).hexdigest()
        record = source_versions.reserve_version(
            output, "card", is_directory=True, source_key="s1", profile="好题")
        manifest = {"source_path": str(source),
                    "files": [{"path": "card.md", "sha256": digest}]}
        with mock.patch.object(source_recycle, "send_to_recycle_bin",
                               side_effect=OSError("shell failed")):
            result = source_versions.commit_outputs(record, staged, manifest)
        self.assertEqual(result["status"], "recycle_pending")
        self.assertTrue((output / "card" / "card.md").is_file())
        self.assertTrue(source.exists())
        self.assertEqual(len(source_versions.list_versions("好题")), 0)
        with mock.patch.object(source_recycle, "send_to_recycle_bin") as recycle:
            retried = source_versions.retry_recycle(record["version_id"])
        recycle.assert_called_once_with(source)
        self.assertEqual(retried["status"], "committed")
        self.assertEqual(len(source_versions.list_versions("好题")), 1)


if __name__ == "__main__":
    unittest.main()
