"""批次导入人工修订的哈希保护与题号恢复回归。"""

import hashlib
import unittest
from unittest import mock

from tools import import_reviewed_batch


class ReviewedBatchOverrideTests(unittest.TestCase):
    def test_archive_preview_can_skip_global_bank_scan(self):
        app = import_reviewed_batch.app
        with (mock.patch.object(app.filestore, "list_questions",
                                side_effect=AssertionError("不应扫描题库")),
              mock.patch.object(app.filestore, "all_collections",
                                side_effect=AssertionError("不应扫描文件夹"))):
            preview, folders, missing = app._build_import_preview(
                "- [填空] 1. $x=$ ___", existing_fps=set(), all_cols=[])

        self.assertEqual(len(preview), 1)
        self.assertEqual(folders, [])
        self.assertIsNone(missing)

    def test_replace_and_insert_missing_question(self):
        rows = [
            {"idx": 0, "number": 7, "body": "第七题与第八题粘连", "solution": "",
             "type": "单选题", "dup": False},
            {"idx": 1, "number": 9, "body": "第九题", "solution": "",
             "type": "单选题", "dup": False},
        ]
        digest = hashlib.sha256(rows[0]["body"].encode()).hexdigest()
        overrides = [
            {"gid": 2, "number": 7, "expected_body_sha256": digest,
             "body": "第七题", "type": "单选题"},
            {"gid": 2, "operation": "insert", "after_number": 7,
             "number": 8, "expected_reference_body_sha256":
                 hashlib.sha256("第七题".encode()).hexdigest(),
             "body": "第八题", "type": "单选题"},
        ]

        fixed = import_reviewed_batch._apply_review_overrides(rows, 2, overrides)

        self.assertEqual([row["number"] for row in fixed], [7, 8, 9])
        self.assertEqual(import_reviewed_batch._missing_numbers(fixed), [])

    def test_changed_body_rejects_override(self):
        rows = [{"idx": 0, "number": 1, "body": "已人工修改", "solution": "",
                 "type": "填空题", "dup": False}]
        overrides = [{"gid": 0, "number": 1,
                      "expected_body_sha256": "0" * 64, "body": "覆盖"}]

        with self.assertRaisesRegex(ValueError, "目标题干已变化"):
            import_reviewed_batch._apply_review_overrides(rows, 0, overrides)


if __name__ == "__main__":
    unittest.main()
