"""四维度导出组合（导出抽屉）的规格与分页测试。

三层覆盖：
  1) resolve_export_layout 的 clamp/派生（纯单元）；
  2) 等价表对拍——四维调用与对应 legacy mode 的 build_markdown 逐字节一致
     （「预设 = 把选项选好」的机械证明；golden 基线钉 legacy 侧不变）；
  3) 新组合的分页结构断言（layout 标记 / 桶标题 / 题号连续 / std_exam 卷头）。
"""

import unittest

import exporter

TITLE = "四维组合测试"
KEYPOINTS = "一、函数单调性"
STD_OPTS = {
    "subject": "数学",
    "info_bar": True,
    "secret_notice": "本试卷共 4 题。",
    "exam_notes": "考试时间 60 分钟。",
    "section_points": {"single": "5", "multi": "5", "blank": "5", "solve": "10"},
}


def _questions():
    return [
        {"id": "d1", "type": "单选题",
         "body": "1. 选 A 还是 B？（  ）\nA. 甲\nB. 乙\nC. 丙\nD. 丁"},
        {"id": "d2", "type": "多选题",
         "body": "2. 下列说法正确的是（  ）\nA. 甲\nB. 乙\nC. 丙\nD. 丁"},
        {"id": "d3", "type": "填空题", "body": "3. $1+1=$ ______。",
         "solution": "【解析】等于 2。"},
        {"id": "d4", "type": "解答题", "body": "4. 求证 $a^2+b^2\\ge 2ab$。",
         "solution": "【解析】配方。"},
    ]


class ResolveSpecTests(unittest.TestCase):
    """clamp 与派生值的单元测试。"""

    def test_none_layout_returns_none_legacy_path(self):
        self.assertIsNone(exporter.resolve_export_layout())
        self.assertIsNone(exporter.resolve_export_layout(grouped=True))

    def test_wide_clamps_to_single_column_one_per_page(self):
        spec = exporter.resolve_export_layout("two", True, 2, "wide")
        self.assertEqual((spec.layout, spec.columns), ("one", 1))
        self.assertTrue(spec.wide)
        self.assertFalse(spec.pagerel)
        self.assertTrue(spec.one_solution_per_page)
        self.assertEqual(spec.template_mode, "slides")

    def test_two_columns_clamps_page_layouts_to_single_column(self):
        spec = exporter.resolve_export_layout("one", True, 2, "a4")
        self.assertEqual(spec.columns, 1)
        self.assertFalse(spec.two_columns)

    def test_std_exam_promotes_flow_grouped_combo_to_exam_std(self):
        spec = exporter.resolve_export_layout("flow", True, 1, "a4", std_exam=True)
        self.assertEqual(spec.compat_mode, "exam_std")
        self.assertEqual(spec.template_mode, "exam_std")
        self.assertTrue(spec.std_head)

    def test_std_exam_ignored_for_wide_and_two_column(self):
        wide = exporter.resolve_export_layout("one", True, 1, "wide", std_exam=True)
        self.assertFalse(wide.std_head)
        multi = exporter.resolve_export_layout("flow", True, 2, "a4", std_exam=True)
        self.assertFalse(multi.std_head)

    def test_new_combos_fall_back_to_list_compat(self):
        spec = exporter.resolve_export_layout("two", True, 1, "a4")
        self.assertEqual(spec.compat_mode, "list")
        self.assertTrue(spec.pagerel)
        self.assertEqual(spec.template_mode, "note")

    def test_grouped_accepts_form_style_values(self):
        self.assertTrue(exporter.resolve_export_layout("flow", "1").grouped)
        self.assertFalse(exporter.resolve_export_layout("flow", "0").grouped)
        self.assertTrue(exporter.resolve_export_layout("flow", None).grouped)

    def test_invalid_dimensions_raise_export_error(self):
        for kwargs in (
            {"layout": "grid"},
            {"layout": "flow", "ratio": "16:9"},
            {"layout": "flow", "columns": "3"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(exporter.ExportError):
                    exporter.resolve_export_layout(**kwargs)


class EquivalenceTests(unittest.TestCase):
    """四维调用与 legacy mode 的产物必须逐字节一致。"""

    # (legacy mode, layout, grouped, columns, ratio, std_exam, keypoints)
    # note 与 handout 同为 (two, 不分, 单栏, A4)：四维分不出知识要点，keypoints
    # 属内容型字段（非空即生效）——空=note，非空=handout（[要点页]+note 分页）。
    EQUIVALENTS = [
        ("exam", "flow", True, 1, "a4", False, ""),
        ("list", "compact", True, 1, "a4", False, ""),
        ("lecture", "one", False, 1, "a4", False, ""),
        ("note", "two", False, 1, "a4", False, ""),
        ("handout", "two", False, 1, "a4", False, KEYPOINTS),
        ("practice", "flow", True, 2, "a4", False, ""),
        ("slides", "one", False, 1, "wide", False, ""),
        ("exam_std", "flow", True, 1, "a4", True, ""),
    ]

    def test_dimension_calls_match_legacy_byte_for_byte(self):
        for (mode, layout, grouped, columns, ratio, std_exam,
             keypoints) in self.EQUIVALENTS:
            for sol in ("none", "inline", "separate"):
                with self.subTest(mode=mode, solution=sol):
                    legacy = exporter.build_markdown(
                        _questions(), TITLE, mode=mode, keypoints=keypoints,
                        fullpage_ids={"d4"}, solution_mode=sol,
                        std_opts=dict(STD_OPTS))
                    dims = exporter.build_markdown(
                        _questions(), TITLE, mode="list", keypoints=keypoints,
                        fullpage_ids={"d4"}, solution_mode=sol,
                        std_opts=dict(STD_OPTS),
                        layout=layout, grouped=grouped, columns=columns,
                        ratio=ratio, std_exam=std_exam)
                    self.assertEqual(legacy, dims)

    def test_equivalence_holds_for_paginate_pages(self):
        for (mode, layout, grouped, columns, ratio, std_exam,
             keypoints) in self.EQUIVALENTS:
            with self.subTest(mode=mode):
                legacy = exporter.paginate(
                    _questions(), mode=mode, keypoints=keypoints,
                    fullpage_ids={"d4"}, std_opts=dict(STD_OPTS))
                dims = exporter.paginate(
                    _questions(), mode="list", keypoints=keypoints,
                    fullpage_ids={"d4"}, std_opts=dict(STD_OPTS),
                    layout=layout, grouped=grouped, columns=columns,
                    ratio=ratio, std_exam=std_exam)
                self.assertEqual(legacy, dims)


class NewComboPaginationTests(unittest.TestCase):
    """8 个新组合（等价表之外）的分页结构。"""

    def _paginate(self, **dims):
        return exporter.paginate(_questions(), mode="list", **dims)

    def _blocks(self, pages):
        return [b for p in pages for b in p]

    def test_stream_ungrouped_keeps_order_with_half_slot_solve(self):
        pages = self._paginate(layout="flow", grouped=False, columns=1, ratio="a4")
        blocks = self._blocks(pages)
        self.assertNotIn("heading", {b["kind"] for b in blocks})
        self.assertEqual([b["num"] for b in blocks], [1, 2, 3, 4])
        self.assertEqual([b["layout"] for b in blocks],
                         ["flow", "flow", "flow", "slot_half"])

    def test_compact_stream_uses_flow_everywhere(self):
        pages = self._paginate(layout="compact", grouped=False, columns=1, ratio="a4")
        self.assertEqual([b["layout"] for b in self._blocks(pages)], ["flow"] * 4)

    def test_one_grouped_buckets_with_headings(self):
        pages = self._paginate(layout="one", grouped=True, columns=1, ratio="a4")
        blocks = self._blocks(pages)
        headings = [b["text"] for b in blocks if b["kind"] == "heading"]
        self.assertEqual(headings, ["一、单选题", "二、多选题", "三、填空题",
                                    "四、解答题"])
        nums = [b["num"] for b in blocks if b["kind"] == "question"]
        self.assertEqual(nums, [1, 2, 3, 4])
        layouts = [b["layout"] for b in blocks if b["kind"] == "question"]
        # 选填走半页槽、解答 full 独占（与讲解同一条规则）
        self.assertEqual(layouts[:3], ["slot_half"] * 3)
        self.assertEqual(layouts[3], "full")

    def test_two_grouped_buckets_and_half_slots(self):
        pages = self._paginate(layout="two", grouped=True, columns=1, ratio="a4")
        blocks = self._blocks(pages)
        self.assertEqual(
            [b["text"] for b in blocks if b["kind"] == "heading"],
            ["一、单选题", "二、多选题", "三、填空题", "四、解答题"])
        slots = [b for b in blocks
                 if b["kind"] == "question" and b["layout"] == "slot_half"]
        self.assertEqual(len(slots), 4)
        # 桶标题与桶首题同页（标题不落单）
        for page in pages:
            if any(b["kind"] == "heading" for b in page):
                self.assertGreater(len(page), 1)

    def test_two_grouped_keypoints_page_only_when_provided(self):
        pages = self._paginate(layout="two", grouped=True, columns=1, ratio="a4")
        self.assertNotEqual(pages[0][0]["kind"], "keypoints")
        pages = exporter.paginate(_questions(), mode="list", keypoints=KEYPOINTS,
                                  layout="two", grouped=True, columns=1, ratio="a4")
        self.assertEqual(pages[0][0]["kind"], "keypoints")

    def test_wide_grouped_uses_slide_layout_with_cover_and_headings(self):
        pages = self._paginate(layout="one", grouped=True, columns=None, ratio="wide")
        blocks = self._blocks(pages)
        self.assertEqual({b["layout"] for b in blocks if b["kind"] == "question"},
                         {"slide"})
        self.assertEqual(sum(1 for b in blocks if b["kind"] == "heading"), 4)
        md = exporter.build_markdown(
            _questions(), TITLE, mode="list",
            layout="one", grouped=True, ratio="wide")
        self.assertIn("\\qslidecover{", md)
        self.assertTrue(md.startswith("% \n\n"))

    def test_two_column_stream_marks_practice_and_compact_drops_answer_box(self):
        flow_pages = self._paginate(layout="flow", grouped=False, columns=2, ratio="a4")
        flow_solve = [b for b in flow_pages[0] if b.get("practice_solve")]
        self.assertEqual(len(flow_solve), 1)
        compact_pages = self._paginate(layout="compact", grouped=False,
                                       columns=2, ratio="a4")
        self.assertFalse(any(b.get("practice_solve") for b in compact_pages[0]))
        md = exporter.build_markdown(
            _questions(), TITLE, mode="list",
            layout="flow", grouped=False, columns=2, ratio="a4")
        self.assertIn("\\qpracticebegin", md)

    def test_wide_separate_solutions_one_per_page(self):
        pages = self._paginate(layout="one", grouped=True, columns=None,
                               ratio="wide", solution_mode="separate")
        sol_pages = [p for p in pages if p and p[0]["kind"] == "solution_slide"]
        self.assertEqual(len(sol_pages), 2)   # 只有 d3/d4 带解析


class StdExamOverlayTests(unittest.TestCase):
    """std_exam 正交开关：自定义组合也保留卷头与分值说明。"""

    def test_std_head_appears_for_custom_combo(self):
        md = exporter.build_markdown(
            _questions(), TITLE, mode="list", std_opts=dict(STD_OPTS),
            layout="one", grouped=True, columns=1, ratio="a4", std_exam=True)
        self.assertIn("本试卷共 4 题。", md)
        self.assertIn("\\qnotebox", md)

    def test_section_headings_carry_points_for_grouped_combos(self):
        pages = exporter.paginate(
            _questions(), mode="list", std_opts=dict(STD_OPTS),
            layout="one", grouped=True, columns=1, ratio="a4", std_exam=True)
        heading = pages[0][0]
        self.assertEqual(heading["kind"], "heading")
        self.assertTrue(heading.get("points"))
        self.assertTrue(heading.get("colon"))

    def test_std_head_can_be_disabled_again(self):
        md = exporter.build_markdown(
            _questions(), TITLE, mode="list", std_opts=dict(STD_OPTS),
            layout="one", grouped=True, columns=1, ratio="a4", std_exam=False)
        self.assertNotIn("\\qnotebox", md)


if __name__ == "__main__":
    unittest.main()
