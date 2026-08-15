"""连续年份直接导入器的离线结构检查。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import import_exam_range_direct


class DirectExamImportTests(unittest.TestCase):
    def test_ready_stems_requires_questions_and_source_pdf(self):
        with tempfile.TemporaryDirectory() as raw:
            year = Path(raw)
            complete = year / "完整卷"
            complete.mkdir()
            (complete / "第1题.md").write_text("题目", encoding="utf-8")
            (complete / "原试卷.pdf").write_bytes(b"%PDF")
            no_pdf = year / "缺原卷"
            no_pdf.mkdir()
            (no_pdf / "第1题.md").write_text("题目", encoding="utf-8")

            self.assertEqual(
                import_exam_range_direct._ready_stems(year), {"完整卷"})

    def test_inspection_records_gap_and_incomplete_choice(self):
        group = {"md": """- [单选] 1. 第一题（ ）
A. 甲
B. 乙
C. 丙

- [填空] 3. 第三题 ___
"""}
        result = import_exam_range_direct._inspect_group(group)

        self.assertEqual(result["missing_numbers"], [2])
        self.assertEqual(result["noncanonical_choice_numbers"], [1])

    def test_inspection_blocks_duplicate_body(self):
        group = {"md": """- [填空] 1. 相同题干 ___

- [填空] 2. 相同题干 ___
"""}
        result = import_exam_range_direct._inspect_group(group)

        self.assertEqual(result["duplicate_body_count"], 1)

    def test_reusable_statuses_selects_done_matching_groups(self):
        with tempfile.TemporaryDirectory() as raw:
            tasks = Path(raw) / "tasks.json"
            tasks.write_text("""{
              "batch": {
                "old": {"payload": {"pack_folder_name": "2009", "groups": [
                  {"gid": 0, "filename": "甲卷.pdf", "status": "done", "md": "题目"},
                  {"gid": 1, "filename": "乙卷.pdf", "status": "error", "md": null}
                ]}}
              }
            }""", encoding="utf-8")
            with mock.patch.object(import_exam_range_direct.config,
                                   "TASKS_PATH", tasks):
                rows = import_exam_range_direct._reusable_statuses(
                    2009, {"甲卷.pdf", "乙卷.pdf"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["batch_id"], "old")
        self.assertEqual([g["filename"] for g in rows[0]["groups"]],
                         ["甲卷.pdf"])


if __name__ == "__main__":
    unittest.main()
