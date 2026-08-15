"""年份试卷审计工具回归。"""

import tempfile
import unittest
from pathlib import Path

from tools.audit_exam_year import audit_year


class ExamYearAuditTests(unittest.TestCase):
    def test_reports_constraint_moved_after_extremum_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            bank = root / "bank"
            assets = root / "_assets"
            source.mkdir()
            assets.mkdir()
            pdf = source / "试卷.pdf"
            pdf.write_bytes(b"%PDF-1.7\nA")
            paper = bank / "试卷"
            paper.mkdir(parents=True)
            (paper / "试卷.pdf").write_bytes(pdf.read_bytes())
            (paper / "第1题.md").write_text(
                "---\nid: q1\nnumber: 1\ntype: 单选题\nsource: 试卷\n---\n"
                "满足约束条件 $\\displaystyle \\left\\{\\begin{aligned}"
                "x+y&\\geqslant2\\\\ x+2y&\\leqslant4"
                "\\end{aligned}\\right.$ 则 z 的最大值是 "
                "$\\displaystyle y\\geqslant0$\n\n"
                "$\\displaystyle A.$ 1\n$\\displaystyle B.$ 2\n"
                "$\\displaystyle C.$ 3\n$\\displaystyle D.$ 4\n",
                encoding="utf-8")

            report = audit_year(source, bank, assets)
            issues = report["papers"][0]["issues"]
            self.assertTrue(any("约束条件疑似" in issue for issue in issues))

    def test_reports_noncanonical_mineru_option_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            bank = root / "bank"
            assets = root / "_assets"
            source.mkdir()
            assets.mkdir()
            pdf = source / "试卷.pdf"
            pdf.write_bytes(b"%PDF-1.7\nA")
            paper = bank / "试卷"
            paper.mkdir(parents=True)
            (paper / "试卷.pdf").write_bytes(pdf.read_bytes())
            (paper / "第1题.md").write_text(
                "---\nid: q1\nnumber: 1\ntype: 单选题\nsource: 试卷\n---\n"
                "题干 (A) 1 (B) 2 $\\displaystyle C$ 3 $\\displaystyle D$ 4\n",
                encoding="utf-8")

            report = audit_year(source, bank, assets)
            issues = report["papers"][0]["issues"]
            self.assertTrue(any("未统一为 A." in issue for issue in issues))

    def test_reports_missing_folder_broken_numbers_and_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "2025"
            bank = root / "bank" / "高考卷" / "2025"
            assets = root / "bank" / "_assets"
            source.mkdir(parents=True)
            assets.mkdir(parents=True)
            (source / "2025年全国I卷.pdf").write_bytes(b"%PDF-1.7\nA")
            (source / "2025年北京卷.pdf").write_bytes(b"%PDF-1.7\nB")
            paper = bank / "2025年全国I卷"
            paper.mkdir(parents=True)
            (paper / "2025年全国I卷.pdf").write_bytes(b"%PDF-1.7\nA")
            (paper / "第1题.md").write_text(
                "---\nid: q1\nnumber: 1\ntype: 单选题\nsource: 2025年全国I卷\n---\n"
                "题干\nA. 1\nB. 2\n![[missing.png]]\n", encoding="utf-8")
            (paper / "第3题.md").write_text(
                "---\nid: q3\nnumber: 3\ntype: 解答题\nsource: 错误题源\n---\n$公式\n@@BODY\n",
                encoding="utf-8")

            report = audit_year(source, bank, assets)
            self.assertEqual(report["missing_paper_folders"], ["2025年北京卷"])
            self.assertEqual(report["papers"][0]["missing_numbers"], [2])
            joined = "\n".join(report["papers"][0]["issues"])
            self.assertIn("选择题选项不完整", joined)
            self.assertIn("引用图片不存在", joined)
            self.assertIn("题源与试卷文件夹名不一致", joined)
            self.assertIn("数学公式定界符疑似不配对", joined)
            self.assertIn("残留识别协议标记", joined)


if __name__ == "__main__":
    unittest.main()
