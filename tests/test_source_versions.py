"""来源版本登记的哈希与并发命名行为。"""

from __future__ import annotations

import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import source_versions


class SourceVersionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(
            config, "SOURCE_VERSIONS_PATH", self.root / "versions.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_source_key_deduplicates_same_content_and_changes_with_hash(self):
        path = self.root / "source.pdf"
        path.write_bytes(b"one")
        first_hash = source_versions.sha256_file(path)
        key = source_versions.version_key("好卷", path, first_hash)
        changed_hash = source_versions.sha256_file(path)
        self.assertEqual(key, source_versions.version_key("好卷", path, changed_hash))
        path.write_bytes(b"two")
        self.assertNotEqual(
            key,
            source_versions.version_key("好卷", path, source_versions.sha256_file(path)))

    def test_parallel_reservations_allocate_unique_suffixes(self):
        output = self.root / "out"
        output.mkdir()
        (output / "名称").mkdir()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            records = list(pool.map(
                lambda i: source_versions.reserve_version(
                    output, "名称", is_directory=True, source_key=f"key-{i}",
                    profile="好卷"),
                range(3)))
        self.assertEqual(
            {Path(row["path"]).name for row in records},
            {"名称_2", "名称_3", "名称_4"})
        self.assertEqual(len({row["version_id"] for row in records}), 3)

    def test_only_committed_records_are_listed_and_same_key_is_reused(self):
        output = self.root / "out"
        output.mkdir()
        first = source_versions.reserve_version(
            output, "名称", is_directory=False, source_key="same", profile="好卷")
        repeated = source_versions.reserve_version(
            output, "另名", is_directory=False, source_key="same", profile="好卷")
        self.assertEqual(first["version_id"], repeated["version_id"])
        self.assertEqual(source_versions.list_versions("好卷"), [])
        committed = source_versions.commit_version(first, {"source_hash": "abc"})
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(len(source_versions.list_versions("好卷")), 1)
        self.assertEqual(source_versions.list_versions("好题"), [])

    def test_corrupt_registry_is_quarantined_and_write_is_blocked(self):
        path = config.SOURCE_VERSIONS_PATH
        original = b"not-json"
        path.write_bytes(original)
        with self.assertRaises(source_versions.SourceVersionsCorruptError):
            source_versions.reserve_version(
                self.root, "name", is_directory=False, source_key="key", profile="好卷")
        self.assertEqual(path.read_bytes(), original)
        backups = list(path.parent.glob(path.name + ".corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_cancel_releases_only_uncommitted_absent_target(self):
        output = self.root / "out"
        output.mkdir()
        record = source_versions.reserve_version(
            output, "名称", is_directory=False, source_key="failed", profile="好卷")
        self.assertTrue(source_versions.cancel_reservation(record["version_id"]))
        retry = source_versions.reserve_version(
            output, "名称", is_directory=False, source_key="retry", profile="好卷")
        self.assertEqual(Path(retry["path"]).name, "名称.md")

        committed = source_versions.commit_version(retry, {})
        with self.assertRaises(ValueError):
            source_versions.cancel_reservation(committed["version_id"])

    def test_recycle_pending_committed_version_keeps_its_reserved_path(self):
        output = self.root / "out"
        output.mkdir()
        record = source_versions.reserve_version(
            output, "名称", is_directory=False, source_key="recycled", profile="好卷")
        source_versions.commit_version(record, {})
        source_versions.mark_recycle_pending(record["version_id"], "private path C:/secret")
        with self.assertRaises(ValueError):
            source_versions.commit_version(record, {})
        next_record = source_versions.reserve_version(
            output, "名称", is_directory=False, source_key="another", profile="好卷")
        self.assertEqual(Path(next_record["path"]).name, "名称_2.md")
        persisted = config.SOURCE_VERSIONS_PATH.read_text(encoding="utf-8")
        self.assertNotIn("secret", persisted)
        with self.assertRaises(ValueError):
            source_versions.cancel_reservation(record["version_id"])

    def test_cancel_refuses_a_reserved_path_that_already_exists(self):
        output = self.root / "out"
        output.mkdir()
        record = source_versions.reserve_version(
            output, "名称", is_directory=False, source_key="existing", profile="好卷")
        Path(record["path"]).write_text("created", encoding="utf-8")
        with self.assertRaises(ValueError):
            source_versions.cancel_reservation(record["version_id"])

    def test_legacy_reserve_signature_derives_profile_from_settings(self):
        output = self.root / "custom-output"
        output.mkdir()
        settings_path = self.root / "settings.json"
        settings_path.write_text(json.dumps({"profiles": {
            "真实档案": {"input_dir": str(output), "output_dir": str(output),
                         "enabled": True},
        }}), encoding="utf-8")
        with mock.patch.object(config, "SOURCE_SETTINGS_PATH", settings_path), \
                mock.patch.object(config, "SOURCE_WORKSPACE_DIR", self.root / "workspace"):
            record = source_versions.reserve_version(
                output, "名称", is_directory=False, source_key="legacy-call")
        self.assertEqual(record["profile"], "真实档案")

    def test_commit_publishes_file_via_hidden_temp_without_residue(self):
        """暂存与目标不同目录时，发布要经"目标同卷隐藏临时名 + 原子替换"；
        目标是真实路径且不残留 .qf-tmp 临时件。"""
        stage = self.root / "stage"
        stage.mkdir()
        output = self.root / "publish-out"
        output.mkdir()
        source = self.root / "源.png"
        source.write_bytes(b"img")
        card = stage / "卡.md"
        card.write_text("hello", encoding="utf-8")
        record = source_versions.reserve_version(
            output, "卡", is_directory=False, source_key="publish", profile="好题")
        with mock.patch("source_recycle.send_to_recycle_bin") as recycle:
            committed = source_versions.commit_outputs(record, stage, {
                "source_path": str(source),
                "files": [{"path": "卡.md",
                           "sha256": source_versions.sha256_file(card)}]})
        recycle.assert_called_once()
        self.assertEqual(committed["status"], "committed")
        target = Path(record["path"])
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")
        self.assertEqual(list(output.glob(".卡.md.qf-tmp-*")), [])

    def test_commit_publishes_directory_version(self):
        stage = self.root / "stage2"
        stage.mkdir()
        output = self.root / "dir-out"
        output.mkdir()
        source = self.root / "源卷.pdf"
        source.write_bytes(b"pdf")
        version_root = stage / "圆梦杯"
        version_root.mkdir()
        (version_root / "第1题.md").write_text("a", encoding="utf-8")
        (version_root / "第2题.md").write_text("b", encoding="utf-8")
        files = [
            {"path": "圆梦杯/第1题.md",
             "sha256": source_versions.sha256_file(version_root / "第1题.md")},
            {"path": "圆梦杯/第2题.md",
             "sha256": source_versions.sha256_file(version_root / "第2题.md")},
        ]
        record = source_versions.reserve_version(
            output, "圆梦杯", is_directory=True, source_key="dir", profile="好卷")
        with mock.patch("source_recycle.send_to_recycle_bin"):
            source_versions.commit_outputs(record, stage, {
                "source_path": str(source), "files": files})
        target = Path(record["path"])
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "第1题.md").read_text(encoding="utf-8"), "a")
        self.assertEqual(list(output.glob(".圆梦杯.qf-tmp-*")), [])

    def test_publish_verification_failure_cleans_temp_and_leaves_no_target(self):
        stage_file = self.root / "x.md"
        stage_file.write_text("data", encoding="utf-8")
        output = self.root / "ver-out"
        output.mkdir()
        with self.assertRaises(ValueError):
            source_versions._publish_item(
                stage_file, output / "x.md",
                [{"path": "x.md", "sha256": "0" * 64}],
                is_directory=False, version_id="v1")
        self.assertFalse((output / "x.md").exists())
        self.assertEqual(list(output.glob(".x.md.qf-tmp-*")), [])

    def test_dangling_reserved_with_existing_target_is_healed(self):
        """发布成功但账本未写成会留下"reserved 且目标已存在"的悬挂行；
        再次预留必须自愈重分配，而不是让同一指纹永久卡死。"""
        output = self.root / "heal-out"
        output.mkdir()
        first = source_versions.reserve_version(
            output, "名称", is_directory=False, source_key="d1", profile="好题")
        Path(first["path"]).write_text("published", encoding="utf-8")
        again = source_versions.reserve_version(
            output, "名称", is_directory=False, source_key="d1", profile="好题")
        self.assertNotEqual(again["version_id"], first["version_id"])
        self.assertEqual(Path(again["path"]).name, "名称_2.md")

    def test_non_active_status_rows_do_not_block_reserve(self):
        """中断标记行（无 path 产出）不能把显式重试挡在 reserve 外面。"""
        output = self.root / "status-out"
        output.mkdir()
        config.SOURCE_VERSIONS_PATH.write_text(json.dumps([
            {"version_id": "old", "profile": "好题", "source_key": "kk",
             "path": str(output / "x.md"), "is_directory": False,
             "status": "interrupted"},
        ]), encoding="utf-8")
        record = source_versions.reserve_version(
            output, "x", is_directory=False, source_key="kk", profile="好题")
        self.assertNotEqual(record["version_id"], "old")
        self.assertEqual(record["status"], "reserved")


if __name__ == "__main__":
    unittest.main()
