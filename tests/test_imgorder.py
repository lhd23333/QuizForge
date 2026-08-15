"""多图片选择题的坐标归属与下游排版回归。"""

import json
import tempfile
import unittest
from pathlib import Path

import exporter
import imgorder
import qrender


class ChoiceImageOrderTests(unittest.TestCase):
    def _write_visual_rows(self, root: Path, rows):
        (root / "new_content_list.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    def _fixture(self, root: Path):
        rows = [
            {"type": "text", "text": "1. 观察下列图形，判断正确的是",
             "page_idx": 0, "bbox": [50, 40, 520, 90]},
            {"type": "image", "img_path": "images/stem.jpg",
             "page_idx": 0, "bbox": [650, 100, 930, 250]},
            {"type": "text", "text": "A.", "page_idx": 0,
             "bbox": [90, 390, 120, 420]},
            {"type": "image", "img_path": "images/a.jpg",
             "page_idx": 0, "bbox": [130, 350, 350, 550]},
            {"type": "text", "text": "B.", "page_idx": 0,
             "bbox": [510, 390, 540, 420]},
            {"type": "image", "img_path": "images/b.jpg",
             "page_idx": 0, "bbox": [550, 350, 770, 550]},
            {"type": "text", "text": "C.", "page_idx": 0,
             "bbox": [90, 690, 120, 720]},
            {"type": "image", "img_path": "images/c.jpg",
             "page_idx": 0, "bbox": [130, 650, 350, 850]},
            {"type": "text", "text": "D.", "page_idx": 0,
             "bbox": [510, 690, 540, 720]},
            {"type": "image", "img_path": "images/d.jpg",
             "page_idx": 0, "bbox": [550, 650, 770, 850]},
        ]
        (root / "new_content_list.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return (
            "1. 观察下列图形，判断正确的是\n\n"
            "![](images/a.jpg)\n\n![](images/b.jpg)\n\n"
            "A.\n\nB.\n\n![](images/c.jpg)\n\nC.\n\n"
            "![](images/d.jpg)\n\nD.\n\n![](images/stem.jpg)\n\n"
            "【解析】保持解析图片 ![](images/solution.jpg) 不动"
        )

    def test_bbox_recovers_stem_and_four_option_images(self):
        with tempfile.TemporaryDirectory() as td:
            raw = self._fixture(Path(td))
            repaired, count = imgorder.repair_document(raw, td)

        self.assertEqual(count, 1)
        self.assertLess(repaired.index("![题干图]"), repaired.index("A."))
        for label, name in zip("ABCD", "abcd"):
            self.assertIn(f"{label}.\n\n![选项{label}](images/{name}.jpg)", repaired)
        self.assertEqual(repaired.count("images/stem.jpg"), 1)
        self.assertIn("【解析】保持解析图片 ![](images/solution.jpg) 不动", repaired)

    def test_missing_layout_is_exact_noop(self):
        raw = "1. 题干\nA. 一\nB. 二\nC. 三\nD. 四"
        with tempfile.TemporaryDirectory() as td:
            repaired, count = imgorder.repair_document(raw, td)
        self.assertEqual((repaired, count), (raw, 0))

    def test_bbox_recovers_labels_omitted_from_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            raw = self._fixture(Path(td))
            question, solution = raw.split("【解析】", 1)
            question = "\n".join(
                line for line in question.splitlines()
                if line.strip() not in {"A.", "B.", "C.", "D."}
            )
            question = question.replace("判断正确的是", "判断正确的是 ( )")
            repaired, count = imgorder.repair_document(
                question + "【解析】" + solution, td)

        self.assertEqual(count, 1)
        for label, name in zip("ABCD", "abcd"):
            self.assertIn(f"({label})\n\n![选项{label}](images/{name}.jpg)", repaired)
        self.assertEqual(repaired.count("images/stem.jpg"), 1)

    def test_label_free_assignment_uses_visual_reading_order(self):
        with tempfile.TemporaryDirectory() as td:
            layout = imgorder.load_layout(td)
            self.assertIsNone(layout)

            root = Path(td)
            self._fixture(root)
            layout = imgorder.load_layout(root)
            # 故意以 B、A、D、C 的顺序传入，结果仍应按 bbox 的左到右、上到下排列。
            assigned = imgorder._reading_order_assignment(
                ["images/b.jpg", "images/a.jpg", "images/d.jpg", "images/c.jpg"],
                layout,
            )

        self.assertEqual(
            assigned,
            {"A": "images/a.jpg", "B": "images/b.jpg",
             "C": "images/c.jpg", "D": "images/d.jpg"},
        )

    def test_weak_math_labels_after_images_are_rebuilt_by_coordinates(self):
        rows = [
            {"type": "image", "img_path": f"images/{name}.jpg",
             "page_idx": 0, "bbox": bbox}
            for name, bbox in (
                ("a", [100, 200, 300, 380]), ("b", [500, 200, 700, 380]),
                ("c", [100, 500, 300, 680]), ("d", [500, 500, 700, 680]),
            )
        ]
        raw = (
            "1. 如图，选择正确图形 ( )\n\n"
            "![](images/b.jpg)\n($\\displaystyle A$)\nM\n"
            "![](images/a.jpg)\n($\\displaystyle B$)\nM\n"
            "![](images/d.jpg)\n($\\displaystyle C$)\n"
            "![](images/c.jpg)\n($\\displaystyle D$)"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "new_content_list.json").write_text(
                json.dumps(rows), encoding="utf-8")
            repaired, count = imgorder.repair_document(raw, root)

        self.assertEqual(count, 1)
        for label, name in zip("ABCD", "abcd"):
            self.assertIn(
                f"({label})\n\n![选项{label}](images/{name}.jpg)", repaired)
        self.assertNotIn("\nM\n", repaired)

    def test_strong_labels_fall_back_when_layout_has_no_text_anchors(self):
        rows = [
            {"type": "image", "img_path": f"images/{name}.jpg",
             "page_idx": 0, "bbox": bbox}
            for name, bbox in (
                ("a", [100, 200, 300, 380]), ("b", [500, 200, 700, 380]),
                ("c", [100, 500, 300, 680]), ("d", [500, 500, 700, 680]),
            )
        ]
        raw = (
            "1. 如图，选择正确图形 ( )\n\n"
            "![](images/b.jpg)\n(A)\nM\n"
            "![](images/a.jpg)\n(B)\nM\n"
            "![](images/d.jpg)\n(C)\n"
            "![](images/c.jpg)\n(D)"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "new_content_list.json").write_text(
                json.dumps(rows), encoding="utf-8")
            repaired, count = imgorder.repair_document(raw, root)

        self.assertEqual(count, 1)
        for label, name in zip("ABCD", "abcd"):
            self.assertIn(
                f"({label})\n\n![选项{label}](images/{name}.jpg)", repaired)

    def test_label_free_four_charts_need_no_text_anchors(self):
        rows = [
            {"type": "chart", "img_path": f"images/{name}.jpg",
             "page_idx": 0, "bbox": bbox}
            for name, bbox in (
                ("a", [90, 200, 290, 310]),
                ("b", [320, 202, 520, 312]),
                ("c", [550, 198, 750, 308]),
                ("d", [780, 201, 980, 311]),
            )
        ]
        raw = (
            "3. 下列图像中正确的是（ ）\n\n"
            "![](images/c.jpg)\n![](images/a.jpg)\n"
            "![](images/d.jpg)\n![](images/b.jpg)"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_visual_rows(root, rows)
            repaired, count = imgorder.repair_document(raw, root)

        self.assertEqual(count, 1)
        for label, name in zip("ABCD", "abcd"):
            self.assertIn(
                f"({label})\n\n![选项{label}](images/{name}.jpg)", repaired)

    def test_namespaced_refs_reuse_original_layout_and_keep_prefix(self):
        rows = [
            {"type": "chart", "img_path": f"images/{name}.jpg",
             "page_idx": 0, "bbox": bbox}
            for name, bbox in (
                ("a", [90, 200, 290, 310]),
                ("b", [320, 202, 520, 312]),
                ("c", [550, 198, 750, 308]),
                ("d", [780, 201, 980, 311]),
            )
        ]
        raw = (
            "3. 下列图像中正确的是（ ）\n\n"
            "![](images/exam_c.jpg)\n![](images/exam_a.jpg)\n"
            "![](images/exam_d.jpg)\n![](images/exam_b.jpg)"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_visual_rows(root, rows)
            repaired, count = imgorder.repair_document(raw, root)

        self.assertEqual(count, 1)
        for label, name in zip("ABCD", "abcd"):
            self.assertIn(
                f"({label})\n\n![选项{label}](images/exam_{name}.jpg)",
                repaired,
            )
        self.assertNotIn("(images/a.jpg)", repaired)

    def test_layout_namespace_exact_key_wins_and_fallback_must_be_unique(self):
        original = imgorder._Box(0, (0, 0, 10, 10))
        exact = imgorder._Box(1, (20, 20, 30, 30))
        layout = imgorder._Layout(
            {"a.jpg": original, "exam_a.jpg": exact}, ())
        self.assertIs(imgorder._layout_box("images/exam_a.jpg", layout), exact)
        self.assertIs(imgorder._layout_box("images/solution_a.jpg", layout), original)

        ambiguous = imgorder._Layout(
            {"left/a.jpg": original, "right/a.jpg": exact}, ())
        self.assertIsNone(
            imgorder._layout_box("images/exam_a.jpg", ambiguous))

    def test_layout_namespace_never_strips_two_prefix_layers(self):
        rows = [
            {"type": "image", "img_path": f"images/{name}.jpg",
             "page_idx": 0, "bbox": [80 + index * 220, 200,
                                        260 + index * 220, 310]}
            for index, name in enumerate("abcd")
        ]
        raw = "3. 下列图像中正确的是（ ）\n\n" + "\n".join(
            f"![](images/exam_exam_{name}.jpg)" for name in "abcd")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_visual_rows(root, rows)
            repaired, count = imgorder.repair_document(raw, root)

        self.assertEqual((repaired, count), (raw, 0))

    def test_label_free_five_images_excludes_unique_stem_layout(self):
        rows = [
            {"type": "image", "img_path": f"images/{name}.jpg",
             "page_idx": 0, "bbox": bbox}
            for name, bbox in (
                ("stem", [700, 60, 940, 140]),
                ("a", [80, 220, 260, 330]),
                ("b", [290, 218, 470, 332]),
                ("c", [510, 221, 690, 331]),
                ("d", [730, 219, 910, 333]),
            )
        ]
        raw = (
            "7. 根据题干示意图选择正确图像（ ）\n\n"
            "![](images/stem.jpg)\n![](images/b.jpg)\n"
            "![](images/d.jpg)\n![](images/a.jpg)\n![](images/c.jpg)"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_visual_rows(root, rows)
            repaired, count = imgorder.repair_document(raw, root)

        self.assertEqual(count, 1)
        self.assertEqual(repaired.count("images/stem.jpg"), 1)
        self.assertLess(repaired.index("![题干图](images/stem.jpg)"),
                        repaired.index("(A)"))
        for label, name in zip("ABCD", "abcd"):
            self.assertIn(
                f"({label})\n\n![选项{label}](images/{name}.jpg)", repaired)

    def test_label_free_ambiguous_five_images_is_noop(self):
        # 五张同规格图片同处一行，任意连续四张都像选项；没有证据能指出哪张是题干图。
        rows = [
            {"type": "image", "img_path": f"images/{index}.jpg",
             "page_idx": 0,
             "bbox": [60 + index * 180, 200, 210 + index * 180, 300]}
            for index in range(5)
        ]
        raw = "5. 选择正确图像（ ）\n\n" + "\n".join(
            f"![](images/{index}.jpg)" for index in range(5))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_visual_rows(root, rows)
            repaired, count = imgorder.repair_document(raw, root)

        self.assertEqual((repaired, count), (raw, 0))

    def test_label_free_nonuniform_four_images_is_noop(self):
        rows = [
            {"type": "image", "img_path": f"images/{name}.jpg",
             "page_idx": 0, "bbox": bbox}
            for name, bbox in (
                ("a", [80, 200, 260, 310]),
                ("b", [290, 200, 470, 310]),
                ("c", [510, 200, 690, 310]),
                ("wide", [710, 200, 980, 310]),
            )
        ]
        raw = "5. 选择正确图像（ ）\n\n" + "\n".join(
            f"![](images/{name}.jpg)" for name in ("a", "b", "c", "wide"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_visual_rows(root, rows)
            repaired, count = imgorder.repair_document(raw, root)

        self.assertEqual((repaired, count), (raw, 0))

    def test_collection_repairs_each_restarted_question_sequence(self):
        rows = []
        groups = []
        for group, page in (("first", 0), ("second", 1)):
            refs = []
            for index, label in enumerate("abcd"):
                name = f"{group}_{label}.jpg"
                rows.append({
                    "type": "chart", "img_path": f"images/{name}",
                    "page_idx": page,
                    "bbox": [80 + index * 220, 200,
                             260 + index * 220, 310],
                })
                refs.append(f"![](images/{name})")
            groups.append(
                f"精练{'一' if page == 0 else '二'}：图像专题\n\n"
                f"1. 选择正确图像（ ）\n\n" + "\n".join(refs)
                + "\n\n2. 普通题干\n\n3. 普通题干"
            )
        raw = "\n\n".join(groups)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_visual_rows(root, rows)
            repaired, count = imgorder.repair_document(raw, root)

        self.assertEqual(count, 2)
        for group in ("first", "second"):
            for label, name in zip("ABCD", "abcd"):
                self.assertIn(
                    f"({label})\n\n![选项{label}]"
                    f"(images/{group}_{name}.jpg)", repaired)

    def test_preview_and_pdf_plan_pair_four_options_with_stem_image(self):
        # staging 后的五个哨兵：0 是题干图，1—4 分别位于 A—D 选项。
        body = ("题干\nQFIGSLOT0\n"
                "A. QFIGSLOT1\nB. QFIGSLOT2\n"
                "C. QFIGSLOT3\nD. QFIGSLOT4")
        plan = exporter.plan_figs(body, "单选题")
        self.assertTrue(plan["pair"])
        self.assertEqual(plan["pair_map"], [1, 2, 3, 4])
        self.assertEqual(plan["slots"][0]["pos"], "stem")

        obsidian = ("题干\n![[stem.jpg]]\nA. ![[a.jpg]]\nB. ![[b.jpg]]\n"
                    "C. ![[c.jpg]]\nD. ![[d.jpg]]")
        html = str(qrender.render_body(obsidian, "单选题"))
        self.assertIn("stem.jpg", html)
        self.assertEqual(html.count("q-pair-cell"), 4)
        for name in "abcd":
            self.assertIn(f"{name}.jpg", html)


if __name__ == "__main__":
    unittest.main()
