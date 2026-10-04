"""来源版本登记的哈希与并发命名行为。"""

from __future__ import annotations

import concurrent.futures
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
                    output, "名称", is_directory=True, source_key=f"key-{i}"),
                range(3)))
        self.assertEqual(
            {Path(row["path"]).name for row in records},
            {"名称_2", "名称_3", "名称_4"})
        self.assertEqual(len({row["version_id"] for row in records}), 3)

    def test_only_committed_records_are_listed_and_same_key_is_reused(self):
        output = self.root / "out"
        output.mkdir()
        first = source_versions.reserve_version(
            output, "名称", is_directory=False, source_key="same")
        repeated = source_versions.reserve_version(
            output, "另名", is_directory=False, source_key="same")
        self.assertEqual(first["version_id"], repeated["version_id"])
        self.assertEqual(source_versions.list_versions("out"), [])
        committed = source_versions.commit_version(first, {"source_hash": "abc"})
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(len(source_versions.list_versions("out")), 1)


if __name__ == "__main__":
    unittest.main()
