"""已入库试卷缺题补丁的边界与哈希保护。"""

import hashlib
import unittest

from tools import apply_missing_exam_questions


class MissingExamQuestionPatchTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{
            "path": "高考卷/2016/某卷/第7题.md", "number": 7,
            "body": "第七题", "id": "q7",
        }]

    def test_valid_choice_patch(self):
        patch = {
            "folder": "高考卷/2016/某卷", "number": 8,
            "reference_number": 7,
            "expected_reference_body_sha256":
                hashlib.sha256("第七题".encode()).hexdigest(),
            "type": "单选题",
            "body": "第八题 ( )\nA. 1\nB. 2\nC. 3\nD. 4",
        }
        fixed = apply_missing_exam_questions.validate_patch(
            self.rows, "高考卷/2016", patch)
        self.assertEqual((fixed["number"], fixed["type"]), (8, "单选题"))

    def test_out_of_collection_is_rejected(self):
        patch = {"folder": "高考卷/2015/某卷", "number": 8,
                 "reference_number": 7, "body": "题干"}
        with self.assertRaisesRegex(ValueError, "目录越界"):
            apply_missing_exam_questions.validate_patch(
                self.rows, "高考卷/2016", patch)


if __name__ == "__main__":
    unittest.main()
