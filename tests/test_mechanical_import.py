import tempfile
import unittest
from pathlib import Path

import blockpipe
import blocksplit
import converter
import filestore
import importer
import mechfix
import qualcheck


class MechanicalImportTests(unittest.TestCase):
    @staticmethod
    def _two_unit_collection(first_numbers, *, first_overrides=None,
                             first_title="# 精练一：运动学基础",
                             preamble="封面"):
        overrides = first_overrides or {}
        first = "\n\n".join(
            overrides.get(number, f"{number}. 第{number}题完整正文，保持原有内容。")
            for number in first_numbers
        )
        second = "\n\n".join(
            f"{number}. 第二专题第{number}题完整正文。"
            for number in (1, 2, 3)
        )
        return (f"{preamble}\n\n{first_title}\n\n# 一、选择题\n\n{first}"
                f"\n\n# 精练二：动力学基础\n\n{second}")

    def test_question_figure_captions_do_not_override_real_dot_numbers(self):
        raw = """第 1 题图

![](images/one.png)

第2题图

![](images/two.png)

1. 第一题完整题干。A. 甲 B. 乙 C. 丙 D. 丁

2. 第二题完整题干。A. 甲 B. 乙 C. 丙 D. 丁

3. 第三题完整题干。A. 甲 B. 乙 C. 丙 D. 丁"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([1, 2, 3], [block.number for block in blocks])
        self.assertTrue(blocks[0].text.startswith("1. 第一题完整题干"))

    def test_priority_stars_before_question_numbers_keep_normal_split(self):
        raw = """★1．第一题完整题干。

★★2．第二题完整题干。

☆3．第三题完整题干。"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([1, 2, 3], [block.number for block in blocks])
        self.assertTrue(blocks[0].text.startswith("★1．"))

    def test_single_main_question_keeps_parenthesized_subquestions_together(self):
        raw = """19. 如图，已知函数与图形满足下列关系。

![](images/question-19.png)

(1) 求函数的解析式；

(2) 求参数的取值范围；

(3) 证明所得结论。"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual(1, len(blocks))
        self.assertEqual(19, blocks[0].number)
        self.assertIn("![](images/question-19.png)", blocks[0].text)
        for marker in ("(1)", "(2)", "(3)"):
            self.assertIn(marker, blocks[0].text)

    def test_main_question_number_one_keeps_right_paren_subquestions_together(self):
        raw = """1. 已知数列满足给定条件。

1) 求数列通项；

2) 求前若干项和；

3) 证明一个不等式。"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual(1, len(blocks))
        self.assertEqual(1, blocks[0].number)
        for marker in ("1)", "2)", "3)"):
            self.assertIn(marker, blocks[0].text)

    def test_single_main_question_keeps_one_parenthesized_subquestion(self):
        raw = """19. 单题截图中的主问题。

(1) 证明唯一的小问。"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual(1, len(blocks))
        self.assertEqual(19, blocks[0].number)
        self.assertIn("(1) 证明唯一的小问", blocks[0].text)

    def test_parenthesized_top_level_numbers_still_use_loose_split(self):
        raw = """(1) 第一题题干。

(2) 第二题题干。

(3) 第三题题干。"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([1, 2, 3], [block.number for block in blocks])
        self.assertTrue(all(block.zone == "stem" for block in blocks))

    def test_short_question_about_modelling_clay_is_not_dropped_as_preamble(self):
        raw = """1. 已知集合 $A=\\{1,2\\}$，求元素个数。

2. 现有橡皮泥制作的底面半径为 2 的圆柱，将其重新捏成球，求球的半径。

3. 已知函数 $f(x)=x^2$，求 $f(2)$。"""
        blocks = blocksplit.split_blocks(raw)
        self.assertEqual([1, 2, 3], [block.number for block in blocks])
        self.assertIn("橡皮泥", blocks[1].text)

    def test_exam_title_ending_in_math_reference_answer_starts_solution_zone(self):
        raw = """## 四、解答题
19. 求函数的最值。

# 金华十校 2026 年 4 月高三模拟考试 数学参考答案

## 三、填空题
12. $\\frac{5}{4}$ 13. 2 14. 3

## 四、解答题
19. 解析：由题意可得。"""
        blocks = blocksplit.split_blocks(raw)
        self.assertEqual("stem", blocks[0].zone)
        self.assertTrue(all(block.zone == "solution" for block in blocks[1:]))

    def test_answer_tables_and_compact_fill_answers_are_paired(self):
        raw = """# 数学试卷

## 一、单选题
1. 第一题。A. 甲 B. 乙 C. 丙 D. 丁
2. 第二题。A. 甲 B. 乙 C. 丙 D. 丁

## 三、填空题
3. 第三题___。
4. 第四题___。
5. 第五题___。

## 参考答案

## 一、单选题
<table><tr><td>1</td><td>2</td></tr><tr><td>A</td><td>BD</td></tr></table>

## 三、填空题
3. $x$ 4. $y$ 5. $z$
"""
        result = blocksplit.pair_blocks(blocksplit.split_blocks(raw))
        self.assertEqual(
            [1, 2, 3, 4, 5], [stem.number for stem, _ in result.paired])
        solutions = {stem.number: solution.text for stem, solution in result.paired}
        self.assertIn("【答案】A", solutions[1])
        self.assertIn("【答案】BD", solutions[2])
        self.assertIn("【答案】$x$", solutions[3])
        self.assertIn("【答案】$y$", solutions[4])
        self.assertIn("【答案】$z$", solutions[5])

    def test_math_wrapped_answer_number_is_paired(self):
        raw = """1. 第一题。

2. 第二题。

# 参考答案

1. 【答案】甲

${2}.{x}=1$
"""
        result = blocksplit.pair_blocks(blocksplit.split_blocks(raw))
        self.assertEqual(2, len(result.paired))
        self.assertIn("${x}=1$", result.paired[1][1].text)

    def test_mineru_sub_tags_are_normalized_without_dropping_text(self):
        raw = (
            "记 S<sub>n</sub> 为 {a<sub>$n$</sub>} 的前 n 项和, "
            "$p$<sub>$\\displaystyle 1$</sub> = √<sub>3</sub>. "
            "在三角形 <sub>中,</sub> <sub>BC</sub> <sub>=</sub> <sub>2,</sub> "
            "且 <sub>整段中文不能删除</sub>"
        )

        fixed = mechfix.normalize_html_subscripts(raw)

        self.assertIn("$S_{n}$", fixed)
        self.assertIn("{$a_{n}$}", fixed)
        self.assertIn("$p_{1}$", fixed)
        self.assertIn(r"$\sqrt{3}$.", fixed)
        self.assertIn("在三角形 中, BC = 2,", fixed)
        self.assertIn("整段中文不能删除", fixed)
        self.assertNotIn("<sub", fixed.lower())
        self.assertEqual(mechfix.normalize_html_subscripts(fixed), fixed)

    def test_mineru_sub_without_confirmed_base_only_drops_wrapper(self):
        raw = (
            "tan<sub>γ</sub> <sub>µ</sub> <sub>+</sub> <sub>,</sub> "
            "√<sub>2b si</sub> AA<sub>1</sub> BCC<sub>1</sub> ∁<sub>U</sub>A"
        )

        fixed = mechfix.normalize_html_subscripts(raw)

        self.assertIn("tanγ µ + ,", fixed)
        self.assertIn(r"$\sqrt{2}$b si", fixed)
        self.assertIn("$AA_{1}$", fixed)
        self.assertIn("$BCC_{1}$", fixed)
        self.assertIn("$∁_{U}$A", fixed)
        self.assertNotIn("<sub", fixed.lower())

    def test_radical_recovers_numeric_prefix_from_chinese_sub_wrapper(self):
        raw = ("√<sub>3 的直线</sub>; √<sub>$2$, 且条件成立</sub>; "
               "√<sub>2, 0), F (</sub>")
        fixed = mechfix.normalize_html_subscripts(raw)
        self.assertEqual(
            fixed,
            r"$\sqrt{3}$ 的直线; $\sqrt{2}$, 且条件成立; $\sqrt{2}$, 0), F (",
        )

    def test_normalize_block_handles_sub_before_displaystyle(self):
        fixed = mechfix.normalize_block("$p$<sub>$1$</sub> = √<sub>3</sub>")
        self.assertEqual(
            fixed,
            r"$\displaystyle p_{1}$ = $\displaystyle \sqrt{3}$",
        )

    def test_misplaced_constraint_is_moved_back_into_aligned_group(self):
        raw = ("若 x, y 满足约束条件 "
               "$\\left\\{\\begin{aligned}x+y&\\geqslant2,\\\\ "
               "x+2y&\\leqslant4,\\end{aligned}\\right.$ "
               "则 z=2x-y 的最大值是 $y\\geqslant0,$\n\n(A) -2")

        fixed = mechfix.normalize_block(raw)

        self.assertIn(
            r"x+2y&\leqslant4,\\ y&\geqslant0\end{aligned}", fixed)
        self.assertIn("最大值是\n\n(A)", fixed)
        self.assertNotIn(r"最大值是 $\displaystyle y\geqslant0", fixed)
        self.assertEqual(mechfix.normalize_misplaced_constraints(fixed), fixed)

    def test_constraint_repair_requires_an_aligned_constraint_group(self):
        raw = r"若 y\geqslant0, 则 z 的最大值是 $y\geqslant0$"
        self.assertEqual(mechfix.normalize_misplaced_constraints(raw), raw)

    def test_mineru_duplicate_html_subscript_is_not_appended_twice(self):
        raw = r"$a _ { \mathrm { ~ \ i ~ } }$ <sub>i</sub>"
        fixed = mechfix.normalize_html_subscripts(raw)
        self.assertEqual(fixed, r"$a _ { \mathrm { ~ \ i ~ } }$")
        self.assertNotIn("}_{i}", fixed)

    def test_mineru_sup_tags_are_normalized_without_dropping_text(self):
        raw = (
            "2<sup>k</sup>，$a$<sup>$\\displaystyle n$</sup>，"
            "![](images/x.jpg) <sup>N</sup>，<sup>整段中文</sup>"
        )
        fixed = mechfix.normalize_html_superscripts(raw)
        self.assertIn("$2^{k}$", fixed)
        self.assertIn("$a^{n}$", fixed)
        self.assertIn("![](images/x.jpg)", fixed)
        self.assertNotIn("images/x.jpg) N", fixed)
        self.assertIn("整段中文", fixed)
        self.assertNotIn("<sup", fixed.lower())
        self.assertEqual(mechfix.normalize_html_superscripts(fixed), fixed)

    def test_mineru_sup_vector_artifact_is_restored(self):
        raw = "向量 <sup>#</sup> <sup>»</sup> AP 与 <sup>#</sup><sup>»</sup> AB 垂直"
        fixed = mechfix.normalize_html_superscripts(raw)
        self.assertIn(r"$\overrightarrow{AP}$", fixed)
        self.assertIn(r"$\overrightarrow{AB}$", fixed)
        self.assertNotIn("<sup", fixed)
        self.assertNotIn("#", fixed)

    def test_image_adjacent_sup_letter_is_removed_as_duplicate_label(self):
        raw = "![](images/option-b.jpg) <sup>N</sup>"
        self.assertEqual(
            mechfix.normalize_html_superscripts(raw),
            "![](images/option-b.jpg)",
        )

    def test_intrusive_section_and_next_question_head_are_removed(self):
        raw = (
            "10. 点 P 为所在棱的 四、解答题中点, 则正确的是 ( ) 17 记S "
            "(A) 甲 (B) 乙 (C) 丙 (D) 丁"
        )
        fixed = mechfix.normalize_intrusive_column_text(raw)
        self.assertIn("所在棱的 中点", fixed)
        self.assertNotIn("四、解答题", fixed)
        self.assertNotIn("17 记S", fixed)
        self.assertIn("(A) 甲", fixed)

    def test_reference_answer_heading_with_paper_title_starts_solution_zone(self):
        raw = """1. 第一题

2. 第二题

## 《2026年某地数学试卷》参考答案

1. 【答案】A

2. 【答案】B
"""
        blocks = blocksplit.split_blocks(raw)
        self.assertEqual([block.number for block in blocks], [1, 2, 1, 2])
        self.assertEqual(
            [block.zone for block in blocks],
            ["stem", "stem", "solution", "solution"],
        )
        self.assertNotIn("参考答案", "\n".join(block.text for block in blocks))

    def test_reference_answer_and_scoring_heading_starts_solution_zone(self):
        # 武汉五月供题的真实 MinerU 标题没有书名号，且在“参考答案”后带
        # “及评分标准”；旧规则两头都不认，答案页因此被当成了六道新题。
        headings = (
            "数学试卷参考答案及评分标准",
            "参考答案及评分标准",
        )
        for heading in headings:
            with self.subTest(heading=heading):
                raw = f"""1. 第一题

2. 第二题

# {heading}

1. 第一题解析

2. 第二题解析
"""
                blocks = blocksplit.split_blocks(raw)
                self.assertEqual(
                    [block.zone for block in blocks],
                    ["stem", "stem", "solution", "solution"],
                )
                self.assertNotIn(
                    heading, "\n".join(block.text for block in blocks))

    def test_reference_answer_sentence_is_not_solution_heading(self):
        raw = """1. 请对照本试卷参考答案，判断说法是否正确。

2. 第二题
"""
        blocks = blocksplit.split_blocks(raw)
        self.assertEqual([block.zone for block in blocks], ["stem", "stem"])

    def test_wuhan_scoring_heading_keeps_nineteen_questions(self):
        # 真实卷的结构是题干 1—19，答案速查从 12 起形成题号块，随后解答题解析
        # 为 15—19。标题若没切换 zone，机械渲染会把这六块当成新题，19 变 25。
        stems = "\n\n".join(f"{number}. 第{number}题" for number in range(1, 20))
        solutions = """12. 第12题答案 13. 第13题答案 14. 第14题答案

15. 第15题解析

16. 第16题解析

17. 第17题解析

18. 第18题解析

19. 第19题解析
"""
        raw = f"{stems}\n\n# 数学试卷参考答案及评分标准\n\n{solutions}"

        blocks = blockpipe.split_and_prep(raw)
        paired = blocksplit.pair_blocks(blocks)
        rendered = blockpipe.render_without_ai(blocks, include_solution=True)

        self.assertEqual(
            [stem.number for stem, _ in paired.paired], list(range(1, 20)))
        self.assertEqual(
            [solution.number for _, solution in paired.paired if solution],
            [12, 15, 16, 17, 18, 19],
        )
        self.assertEqual(len(importer.split_questions(rendered)), 19)
        self.assertEqual(paired.number_gaps, [])

    def test_mineru_retries_pdf_with_forced_ocr_on_replacement_character(self):
        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, path, **kwargs):
                self.calls.append(kwargs)
                return (("正常文本" if kwargs.get("force_ocr") else "含�乱码"), "full.md")

        client = FakeMineru()
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            extract_dir = Path(tmp) / "published"
            text, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("试卷.pdf"), extract_dir,
                note_sink=notes.append)
        self.assertEqual(text, "正常文本")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(extract_dir, client.calls[0]["resume_dir"])
        self.assertEqual("text-layer", client.calls[0]["resume_key"])
        self.assertTrue(client.calls[1]["force_ocr"])
        self.assertEqual(extract_dir, client.calls[1]["resume_dir"])
        self.assertEqual("forced-ocr", client.calls[1]["resume_key"])
        self.assertIn("强制 OCR", notes[0])

    def test_mineru_retries_number_gap_and_keeps_better_result(self):
        def paper(numbers):
            return "\n\n".join(f"{n}. 第{n}题正文" for n in numbers)

        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, path, **kwargs):
                self.calls.append(kwargs)
                numbers = range(1, 20) if kwargs.get("force_ocr") else [
                    1, 2, *range(4, 20)]
                return paper(numbers), "full.md"

        client = FakeMineru()
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("缺题卷.pdf"), Path(tmp) / "published",
                note_sink=notes.append)
        self.assertIn("3. 第3题正文", text)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(client.calls[1]["force_ocr"])
        self.assertIn("题号断档", notes[0])
        self.assertFalse(any(
            qualcheck.MANUAL_REVIEW_MARKER in note for note in notes))

    def test_collection_first_pass_does_not_retry_whole_book_for_number_gaps(self):
        def paper(numbers):
            return "\n\n".join(f"{n}. 第{n}题正文" for n in numbers)

        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, path, **kwargs):
                self.calls.append(kwargs)
                return paper([1, 2, *range(4, 20)]), "full.md"

        client = FakeMineru()
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("无书签合集.pdf"), Path(tmp) / "published",
                collection=True)

        self.assertIn("4. 第4题正文", text)
        self.assertEqual(1, len(client.calls))
        self.assertNotIn("force_ocr", client.calls[0])

    def test_collection_retry_merges_unique_missing_question_into_forced_ocr(self):
        text_layer = self._two_unit_collection(
            (1, 2, 3, 4, 5),
            first_overrides={
                3: "3. 文本层找回的第三题正文足够完整，不能再被整本覆盖丢失。"
            },
            first_title="## 精练一：运动学基础",
            preamble="文本层封面含�乱码",
        )
        forced = self._two_unit_collection(
            (1, 2, 4, 5),
            first_overrides={
                2: "2. 强制 OCR 的第二题正文必须保留，不能换回文本层。"
            },
            preamble="强制 OCR 封面",
        )

        class FakeMineru:
            def parse_pdf(self, path, **kwargs):
                del path
                return (forced if kwargs.get("force_ocr") else text_layer), "full.md"

        with tempfile.TemporaryDirectory() as tmp:
            result, _ = converter._parse_mineru_with_ocr_retry(
                FakeMineru(), Path("合集.pdf"), Path(tmp) / "published",
                collection=True)

        self.assertIn("文本层找回的第三题正文足够完整", result)
        self.assertIn("强制 OCR 的第二题正文必须保留", result)
        self.assertIn("# 精练一：运动学基础", result)
        self.assertNotIn("## 精练一：运动学基础", result)
        self.assertIn("# 一、选择题", result)

    def test_collection_variant_rejects_short_duplicate_and_wrong_order_candidates(self):
        primary = self._two_unit_collection((1, 2, 4, 5))
        short = self._two_unit_collection(
            (1, 2, 3, 4, 5), first_overrides={3: "3. A"})
        duplicate = self._two_unit_collection(
            (1, 2, 3, 3, 4, 5),
            first_overrides={
                3: "3. 重复的第三题正文内容足够长但坐标并不唯一。"
            })
        wrong_order = self._two_unit_collection((1, 2, 4, 3, 5))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            short_result = converter._merge_collection_ocr_variants(
                primary, root / "primary", short, root / "short")
            duplicate_result = converter._merge_collection_ocr_variants(
                primary, root / "primary", duplicate, root / "duplicate")
            wrong_order_result = converter._merge_collection_ocr_variants(
                primary, root / "primary", wrong_order, root / "wrong")

        self.assertIsNotNone(short_result)
        self.assertEqual(0, short_result[1])
        self.assertNotIn("3. A", short_result[0])
        self.assertIsNone(duplicate_result)
        self.assertIsNone(wrong_order_result)

    def test_collection_variant_copies_only_selected_text_layer_images(self):
        primary = self._two_unit_collection((1, 2, 4, 5))
        alternate = self._two_unit_collection(
            (1, 2, 3, 4, 5),
            first_overrides={
                3: ("3. 文本层第三题有足够完整的可见正文和一张必要配图。\n\n"
                    "![](images/only-text-layer.png)")
            })

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary_dir = root / "primary"
            alternate_images = root / "alternate" / "images"
            alternate_images.mkdir(parents=True)
            (alternate_images / "only-text-layer.png").write_bytes(b"image-body")
            # 未被选中的图片不得跟着整棵文本层目录混入主结果。
            (alternate_images / "unused.png").write_bytes(b"unused")

            result = converter._merge_collection_ocr_variants(
                primary, primary_dir, alternate, root / "alternate")

            self.assertIsNotNone(result)
            merged, inserted, replaced = result
            self.assertEqual((1, 0), (inserted, replaced))
            reference = converter._IMG_REF_RE.search(merged)
            self.assertIsNotNone(reference)
            copied_name = reference.group(2)
            self.assertTrue(copied_name.startswith("text_layer_"))
            self.assertEqual(
                b"image-body",
                (primary_dir / "images" / copied_name).read_bytes())
            self.assertFalse((primary_dir / "images" / "unused.png").exists())

    def test_collection_variant_replaces_only_clearly_better_same_number(self):
        primary = self._two_unit_collection(
            (1, 2, 3, 4, 5),
            first_overrides={
                2: "2. 下列说法正确的是（ ） A.甲 B.乙"
            })
        alternate = self._two_unit_collection(
            (1, 2, 3, 4, 5),
            first_overrides={
                2: "2. 下列说法正确的是（ ） A.甲 B.乙 C.丙 D.丁"
            })

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = converter._merge_collection_ocr_variants(
                primary, root / "primary", alternate, root / "alternate")

        self.assertIsNotNone(result)
        merged, inserted, replaced = result
        self.assertEqual((0, 1), (inserted, replaced))
        self.assertIn("C.丙 D.丁", merged)

    def test_mineru_batch_state_survives_publish_until_downstream_cache(self):
        class FakeMineru:
            def parse_pdf(self, path, **kwargs):
                del path
                resume_dir = Path(kwargs["resume_dir"])
                resume_dir.mkdir(parents=True, exist_ok=True)
                state = resume_dir / ".mineru_task_text-layer.json"
                state.write_text('{"batch_id":"batch-kept"}', encoding="utf-8")
                return "1. 正常题干", "full.md"

        with tempfile.TemporaryDirectory() as tmp:
            extract_dir = Path(tmp) / "published"
            converter._parse_mineru_with_ocr_retry(
                FakeMineru(), Path("可恢复卷.pdf"), extract_dir,
                collection=True)

            state = extract_dir / ".mineru_task_text-layer.json"
            self.assertEqual(
                '{"batch_id":"batch-kept"}', state.read_text(encoding="utf-8"))

    def test_mineru_does_not_replace_long_text_layer_with_gapless_short_ocr(self):
        def paper(numbers):
            return "\n\n".join(f"{n}. 第{n}题正文" for n in numbers)

        class FakeMineru:
            def parse_pdf(self, path, **kwargs):
                numbers = (range(1, 10) if kwargs.get("force_ocr")
                           else [*range(1, 21), 22])
                return paper(numbers), "full.md"

        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = converter._parse_mineru_with_ocr_retry(
                FakeMineru(), Path("短前缀卷.pdf"), Path(tmp) / "published",
                note_sink=notes.append)

        self.assertIn("22. 第22题正文", text)
        self.assertNotIn("仅识别出 9 个题号", text)
        self.assertTrue(any("保留文本层结果" in note for note in notes))
        self.assertTrue(any(
            qualcheck.MANUAL_REVIEW_MARKER in note
            and "保留的文本层仍缺题号 21" in note
            for note in notes))

    def test_mineru_retries_scrambled_choice_and_keeps_better_result(self):
        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, path, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("force_ocr"):
                    return ("1. 题干 ( ) (A) 1 (B) 2 (C) 3 (D) 4", "full.md")
                return ("1. 题干 ( ) (A) 1 (B) (C) 3 (D) 4", "full.md")

        client = FakeMineru()
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("错序卷.pdf"), Path(tmp) / "published",
                note_sink=notes.append)
        self.assertIn("(B) 2", text)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("选项错序", notes[0])

    def test_mineru_retries_image_choice_and_keeps_more_complete_images(self):
        def paper(image_count):
            images = "\n".join(
                f"![](images/{index}.jpg)" for index in range(image_count))
            return f"1. 如图，侧视图是 ( )\n{images}"

        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, path, **kwargs):
                self.calls.append(kwargs)
                return paper(5 if kwargs.get("force_ocr") else 4), "full.md"

        client = FakeMineru()
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("图片选项卷.pdf"), Path(tmp) / "published",
                note_sink=notes.append)
        self.assertEqual(text.count("![]("), 5)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("标签残缺", notes[0])

    def test_mineru_retry_only_publishes_images_from_selected_result(self):
        def paper(numbers):
            return "\n\n".join(f"{n}. 第{n}题正文" for n in numbers)

        class FakeMineru:
            def __init__(self, retry_is_better):
                self.retry_is_better = retry_is_better

            def parse_pdf(self, path, **kwargs):
                forced = bool(kwargs.get("force_ocr"))
                images_dir = Path(kwargs["extract_dir"]) / "images"
                images_dir.mkdir(parents=True, exist_ok=True)
                image_name = "forced-only.png" if forced else "first-only.png"
                (images_dir / image_name).write_bytes(
                    b"forced image" if forced else b"first image")
                if forced:
                    numbers = (range(1, 21) if self.retry_is_better
                               else range(1, 9))
                else:
                    numbers = [1, 2, *range(4, 21)]
                return paper(numbers), "full.md"

        cases = (
            (False, "first-only.png", "forced-only.png"),
            (True, "forced-only.png", "first-only.png"),
        )
        for retry_is_better, expected, rejected in cases:
            with self.subTest(retry_is_better=retry_is_better):
                with tempfile.TemporaryDirectory() as tmp:
                    extract_dir = Path(tmp) / "published"
                    text, _ = converter._parse_mineru_with_ocr_retry(
                        FakeMineru(retry_is_better), Path("images.pdf"),
                        extract_dir)

                    expected_numbers = range(1, 21) if retry_is_better else [
                        1, 2, *range(4, 21)]
                    for number in expected_numbers:
                        self.assertIn(f"{number}. 第{number}题正文", text)
                    self.assertTrue((extract_dir / "images" / expected).is_file())
                    self.assertFalse((extract_dir / "images" / rejected).exists())
                    self.assertEqual(
                        [expected],
                        [path.name for path in
                         (extract_dir / "images").iterdir()],
                    )

    def test_mineru_keeps_text_layer_when_forced_ocr_significantly_shrinks(self):
        first_text = (
            "1. 含�乱码但正文完整\n"
            + "这是必须保留的完整题干内容。" * 35
            + "\n![](images/first.png)"
        )
        retry_text = "1. 重试后的短文。"

        class FakeMineru:
            def parse_pdf(self, path, **kwargs):
                forced = bool(kwargs.get("force_ocr"))
                if not forced:
                    images_dir = Path(kwargs["extract_dir"]) / "images"
                    images_dir.mkdir(parents=True, exist_ok=True)
                    (images_dir / "first.png").write_bytes(b"first image")
                return (retry_text if forced else first_text), "full.md"

        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            extract_dir = Path(tmp) / "published"
            text, _ = converter._parse_mineru_with_ocr_retry(
                FakeMineru(), Path("缩水卷.pdf"), extract_dir,
                note_sink=notes.append)

            self.assertEqual(first_text, text)
            self.assertTrue(
                (extract_dir / "images" / "first.png").is_file())
            self.assertTrue(any(
                qualcheck.MANUAL_REVIEW_MARKER in note
                and "显著缩水" in note
                and "图片由 1 张减至 0 张" in note
                for note in notes
            ))

    def test_mineru_retries_grouped_math_noise_and_keeps_cleaner_result(self):
        noisy = (
            r"1. 题干 $\dot{\delta}_{\scriptsize V}:="
            r"\dot{\delta}_{\scriptsize V}$"
        )

        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, path, **kwargs):
                self.calls.append(kwargs)
                return ("1. 正常题干" if kwargs.get("force_ocr") else noisy), "full.md"

        client = FakeMineru()
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("数学噪声卷.pdf"), Path(tmp) / "published",
                note_sink=notes.append)
        self.assertEqual(text, "1. 正常题干")
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(client.calls[1]["force_ocr"])
        self.assertIn("异常数学命令", notes[0])

    def test_mineru_retries_repeated_tiny_layout_commands(self):
        noisy = r"1. 题干 $\scriptscriptstyle A+\scriptsize B$"

        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, path, **kwargs):
                self.calls.append(kwargs)
                return ("1. 正常题干" if kwargs.get("force_ocr") else noisy), "full.md"

        client = FakeMineru()
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("字号噪声卷.pdf"), Path(tmp) / "published")
        self.assertEqual(text, "1. 正常题干")
        self.assertEqual(len(client.calls), 2)

    def test_mineru_retries_vector_glyph_artifact(self):
        class FakeMineru:
            def __init__(self):
                self.calls = []

            def parse_pdf(self, path, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("force_ocr"):
                    return (r"1. 向量 $\overrightarrow{AP}$", "full.md")
                return ("1. 向量 <sup>#</sup><sup>»</sup> AP", "full.md")

        client = FakeMineru()
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = converter._parse_mineru_with_ocr_retry(
                client, Path("向量卷.pdf"), Path(tmp) / "published",
                note_sink=notes.append)
        self.assertIn(r"\overrightarrow{AP}", text)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("异常数学命令", notes[0])

    def test_full_import_preview_reports_internal_number_gap(self):
        import app
        raw = "\n\n".join(f"- {n}. 第{n}题" for n in (1, 2, 4, 5))
        _preview, _folders, missing = app._build_import_preview(raw)
        self.assertEqual(missing, [3])

    def test_proof_instruction_is_not_moved_wholly_to_solution(self):
        import app
        raw = ("- [解答] 18. 证明: 在复数范围内，方程 "
               "$|z|^2+(1-i)\\overline{z}=1$ 无解")

        preview, _folders, missing = app._build_import_preview(
            raw, existing_fps=set(), all_cols=[])

        self.assertIsNone(missing)
        self.assertIn("证明", preview[0]["body"])
        self.assertIn("无解", preview[0]["body"])
        self.assertEqual(preview[0]["solution"], "")

    def test_numbered_source_material_and_backup_question_are_not_new_questions(self):
        raw = """三、填空题，本题共3小题
12. 第一题【答案】1【解析】第一题解析
13. 第二题【答案】2【解析】第二题解析
14. 正式题【答案】3
【解析】正式题解析
【题目来源】教材复习题
6. 设函数 f(x)，证明恒等式
14.(改编备用)备用题，不属于正式试卷
【答案】4
【解析】备用题解析
四、解答题
15. 正式解答题【答案】略【解析】解答题解析
"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([block.number for block in blocks], [12, 13, 14, 15])
        self.assertIn("6. 设函数", blocks[2].text)
        self.assertNotIn("改编备用", blocks[2].text)
        self.assertIn("正式解答题", blocks[3].text)

    def test_number_restart_without_inline_solution_still_marks_answer_section(self):
        raw = "\n".join(
            [f"{number}. 第 {number} 题" for number in range(1, 7)]
            + [f"{number}. 第 {number} 题解析" for number in range(1, 4)]
        )

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual(len(blocks), 9)
        self.assertEqual([block.zone for block in blocks[:6]], ["stem"] * 6)
        self.assertEqual([block.zone for block in blocks[6:]], ["solution"] * 3)

    def test_number_restart_after_practice_heading_starts_new_stem_section(self):
        raw = "\n".join(
            ["## 刷基础"]
            + [f"{number}. 基础第 {number} 题" for number in range(1, 16)]
            + ["## 核心题型 ①椭圆的性质/T1~2"]
            + [f"{number}. 提升第 {number} 题" for number in range(1, 7)]
        )

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual(len(blocks), 21)
        self.assertEqual([block.number for block in blocks[-6:]], list(range(1, 7)))
        self.assertTrue(all(block.zone == "stem" for block in blocks))
        self.assertTrue(all("核心题型" in (block.section or "")
                            for block in blocks[-6:]))

    def test_decimal_ocr_noise_between_consecutive_numbers_is_not_a_question(self):
        raw = """## 刷基础
13. 第十三题
14. 第十四题
8.0 $\\partial$ .9 $\\zeta$ .6
## 刷易错
15. 第十五题
"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([block.number for block in blocks], [13, 14, 15])
        self.assertIn(r"8.0 $\partial$", blocks[1].text)

    def test_decimal_voltage_line_between_questions_is_not_a_question(self):
        raw = """7. 第七题实验读数如下。
2.30V、5.29V
8. 第八题完整题干。
"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([7, 8], [block.number for block in blocks])
        self.assertIn("2.30V、5.29V", blocks[0].text)

    def test_forward_decimal_fragment_between_one_and_two_is_not_question_three(self):
        raw = """1. 第一题图示读数如下。
3.75 0.25
2. 第二题完整题干。
"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([1, 2], [block.number for block in blocks])
        self.assertIn("3.75 0.25", blocks[0].text)

    def test_dense_ocr_math_noise_is_reported_but_not_deleted(self):
        raw = """## 刷基础
1. 正常题目
2. 正常题目
3. 正常题目
4. 正常题目
5. 正常题目
6. 题干 $\\partial+\\partial n+\\xi+\\zeta+\\delta$
"""
        blocks = blocksplit.split_blocks(raw)

        notes = qualcheck.report(blocks, blocksplit.pair_blocks(blocks))

        self.assertTrue(any("扫描背面透字" in note for note in notes))
        self.assertIn(r"\partial+\partial", blocks[-1].text)
        mixed = (r"$y^{\mathrm{\scriptsize \perp}}"
                 r"\|\dot{\mathcal H}\|\dot{\mathcal H}$")
        self.assertTrue(qualcheck.has_dense_ocr_math_noise(mixed))

    def test_high_confidence_structure_warnings_require_manual_review(self):
        gap_raw = """1. 第一题

2. 第二题

4. 第四题
"""
        gap_blocks = blocksplit.split_blocks(gap_raw)
        gap_notes = qualcheck.report(
            gap_blocks, blocksplit.pair_blocks(gap_blocks))

        option_raw = """一、选择题
1. 请选择正确答案 ( ) (A) 甲 (B) 乙
"""
        option_blocks = blocksplit.split_blocks(option_raw)
        option_notes = qualcheck.report(
            option_blocks, blocksplit.pair_blocks(option_blocks))

        self.assertTrue(any(
            "题号不连续" in note
            and qualcheck.MANUAL_REVIEW_MARKER in note
            for note in gap_notes))
        self.assertTrue(any(
            "选项不足四项" in note
            and qualcheck.MANUAL_REVIEW_MARKER in note
            for note in option_notes))

    def test_dropped_body_requires_review_but_template_fallback_does_not(self):
        discarded = "无法归入任何题目的大段正文。" * 30
        raw = f"{discarded}\n\n1. 第一题\n\n2. 第二题"

        blocks, dropped_note = blocksplit.split_blocks_with_note(raw)
        fallback_blocks, fallback_note = blocksplit.split_blocks_with_note(
            "1. 第一题\n\n2. 第二题", num_template="第x题")

        self.assertEqual([1, 2], [block.number for block in blocks])
        self.assertIn(qualcheck.MANUAL_REVIEW_MARKER, dropped_note)
        self.assertEqual([1, 2], [block.number for block in fallback_blocks])
        self.assertIn("已回退到自动判定", fallback_note)
        self.assertNotIn(qualcheck.MANUAL_REVIEW_MARKER, fallback_note)

    def test_whitelist_boundaries_are_union_and_preserve_explicit_context(self):
        raw = """# 一、选择题
A 组
1．第一题
第 7 题 第二题
第 三 题 第三题
【第9题】第四题
# 参考答案
1. 【答案】甲
"""

        blocks = blocksplit.split_blocks(
            raw, boundary_mode="whitelist", num_template="【第x题】")

        self.assertEqual([1, 7, 3, 9, 1], [block.number for block in blocks])
        self.assertTrue(all(block.section == "一、选择题" for block in blocks))
        self.assertTrue(all(block.group == "A" for block in blocks[:4]))
        self.assertTrue(all(block.zone == "stem" for block in blocks[:4]))
        self.assertEqual("solution", blocks[-1].zone)
        self.assertIsNone(blocks[-1].group)

    def test_whitelist_keeps_repeated_out_of_order_boundaries_as_four_blocks(self):
        raw = """1. 第一块
7. 第二块
2. 第三块
7. 第四块
"""

        blocks = blockpipe.split_and_prep(raw, boundary_mode="whitelist")
        groups = blockpipe.group_blocks(blocks, boundary_mode="whitelist")

        self.assertEqual([1, 7, 2, 7], [block.number for block in blocks])
        self.assertEqual([1, 7, 2, 7], [stem.number for stem, _ in groups])
        self.assertEqual(4, len(blocks))

    def test_whitelist_zero_or_one_match_never_falls_back_to_auto(self):
        no_match = "1、第一题\n2、第二题"
        one_match = "1. 第一题\n2、仍属于第一题正文\n3、仍属于第一题正文"

        empty, empty_note = blocksplit.split_blocks_with_note(
            no_match, boundary_mode="whitelist")
        single, single_note = blocksplit.split_blocks_with_note(
            one_match, boundary_mode="whitelist")

        self.assertEqual([], empty)
        self.assertIn("第 1 页未找到白名单题号", empty_note)
        self.assertEqual([1], [block.number for block in single])
        self.assertIn("2、仍属于第一题正文", single[0].text)
        self.assertNotIn("已回退", single_note)

    def test_whitelist_disables_numbering_checks_but_keeps_other_warnings(self):
        raw = """一、选择题，本题共5小题
1. 请选择正确答案 ( ) (A) 甲 (B) 乙
7. 普通题目
"""
        notes = []

        blocks = blockpipe.split_and_prep(
            raw, boundary_mode="whitelist", note_sink=notes.append)

        self.assertEqual([1, 7], [block.number for block in blocks])
        self.assertTrue(any("选项不足四项" in note for note in notes))
        self.assertFalse(any("题号不连续" in note for note in notes))
        self.assertFalse(any("题数与原卷声明不符" in note for note in notes))

    def test_whitelist_page_break_drops_next_page_prefix_without_pollution(self):
        raw = ("1. 第一页题目\n第一页正文\n"
               f"{blocksplit.SOURCE_PAGE_BREAK}\n"
               "第二页页眉杂文\n2. 第二页题目\n第二页正文")

        blocks, _note = blocksplit.split_blocks_with_note(
            raw, boundary_mode="whitelist")

        self.assertEqual([1, 2], [block.number for block in blocks])
        self.assertNotIn("第二页页眉杂文", blocks[0].text)
        self.assertNotIn("第二页页眉杂文", blocks[1].text)
        self.assertTrue(all(
            blocksplit.SOURCE_PAGE_BREAK not in block.text for block in blocks))

    def test_whitelist_page_without_boundary_requires_manual_review(self):
        raw = ("1. 第一页题目\n"
               f"{blocksplit.SOURCE_PAGE_BREAK}\n"
               "第二页只有无法归属的正文")

        blocks, note = blocksplit.split_blocks_with_note(
            raw, boundary_mode="whitelist")

        self.assertEqual([1], [block.number for block in blocks])
        self.assertNotIn("第二页只有无法归属的正文", blocks[0].text)
        self.assertIn("第 2 页未找到白名单题号，未归入题目", note)
        self.assertIn(qualcheck.MANUAL_REVIEW_MARKER, note)

    def test_significant_orphan_solution_is_reported_but_short_answer_is_quiet(self):
        long_solution = "无法配对但不得静默丢失的详细推导。" * 20
        raw = f"""1. 第一题题干

2. 第二题题干

# 参考答案

99. 【解析】{long_solution}
"""
        blocks = blocksplit.split_blocks(raw)
        pair_result = blocksplit.pair_blocks(blocks)

        note = qualcheck.check_unpaired_content(blocks, pair_result)

        self.assertIn("无法与题目配对", note)
        self.assertIn("可能不会进入最终题目", note)
        self.assertTrue(qualcheck.requires_manual_review(note))
        self.assertEqual(1, len(pair_result.orphan_solutions))

        short_raw = """1. 第一题题干

2. 第二题题干

# 参考答案

3. 【答案】A
"""
        short_blocks = blocksplit.split_blocks(short_raw)
        self.assertEqual(
            "",
            qualcheck.check_unpaired_content(
                short_blocks, blocksplit.pair_blocks(short_blocks)),
        )

    def test_split_and_prep_sends_significant_pairing_loss_to_shared_note_sink(self):
        raw = """1. 第一题题干

2. 第二题题干

# 参考答案

88. 【解析】这一整段无法配对的解析正文必须在单图和批量任务共用的提示通道显示。{tail}
""".format(tail="详细推导过程。" * 30)
        notes = []

        blocks = blockpipe.split_and_prep(raw, note_sink=notes.append)

        self.assertTrue(blocks)
        self.assertTrue(any("无法与题目配对" in note for note in notes))

    def test_missing_separator_is_recovered_only_from_number_gap(self):
        raw = """一、单选题
6．第六题
7 第七题
8．第八题
10．第十题
11 第十一题
12．第十二题
"""
        blocks = blocksplit.split_blocks(raw)
        self.assertEqual([block.number for block in blocks], [6, 7, 8, 10, 11, 12])
        self.assertIn("第七题", blocks[1].text)
        self.assertIn("第十一题", blocks[4].text)

    def test_answer_noun_phrase_is_not_split_as_solution(self):
        raw = ("12. 这款软件的激活码为下面数学问题的答案: 已知数列 1, 2, 4, "
               "求最小整数 N (A) 110 (B) 220 (C) 330 (D) 440")

        stem, solution = importer.split_solution(raw, scan_markers=True)

        self.assertEqual(stem, raw)
        self.assertIsNone(solution)

    def test_inline_numbered_question_is_recovered_only_from_gap(self):
        raw = """一、单选题
1. 第一题 (A) 甲 (B) 乙 (C) 丙 (D) 丁 2. $z=1+i$ 的值为 (A) 1 (B) 2 (C) 3 (D) 4
3. 第三题 (A) 1 (B) 2 (C) 3 (D) 4
"""
        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([block.number for block in blocks], [1, 2, 3])
        self.assertNotIn("2. $z", blocks[0].text)
        self.assertIn("$z=1+i$", blocks[1].text)

    def test_last_inline_choice_is_recovered_across_section_boundary(self):
        raw = """一、选择题
7. 第七题 (A) 甲 (B) 乙 (C) 丙 (D) 丁 8. 在如图的平面图形中 (A) 子 (B) 丑 (C) 寅 (D) 卯
二、填空题
9. 第九题的答案为 ___
"""
        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([block.number for block in blocks], [7, 8, 9])
        self.assertIn("第七题", blocks[0].text)
        self.assertIn("在如图", blocks[1].text)
        self.assertNotIn("8. 在如图", blocks[0].text)

    def test_inline_question_wrapped_in_sub_tag_is_recovered(self):
        raw = """一、单选题
3. 第三题 (A) 1 (B) 2 (C) 3 (D) 4 <sub>4. 已知向量条件 (A) 1 (B) 2 (C) 3 (D) 4</sub>
5. 第五题 (A) 1 (B) 2 (C) 3 (D) 4
"""
        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([block.number for block in blocks], [3, 4, 5])
        self.assertNotIn("<sub>", blocks[0].text)
        self.assertNotIn("</sub>", blocks[1].text)

    def test_unnumbered_choice_is_recovered_from_double_options_and_gap(self):
        raw = """一、单选题
4. 第一题题干
(A) 甲 (B) 乙 (C) 丙 (D) 丁
若 x, y 满足条件, 则最大值是
(A) -2 (B) 4 (C) 8 (D) 12
6. 第六题 (A) 1 (B) 2 (C) 3 (D) 4
"""
        blocks = blockpipe.split_and_prep(raw)

        self.assertEqual([block.number for block in blocks], [4, 5, 6])
        self.assertNotIn("若 x", blocks[0].text)
        self.assertIn("若 x", blocks[1].text)

    def test_unnumbered_choice_recovery_requires_two_complete_quartets(self):
        raw = """一、单选题
4. 第一题题干 (A) 甲 (B) 乙 (C) 丙 (D) 丁
若 x 满足条件, 则结论成立
6. 第六题 (A) 1 (B) 2 (C) 3 (D) 4
"""
        blocks = blockpipe.split_and_prep(raw)

        self.assertEqual([block.number for block in blocks], [4, 6])
        self.assertIn("若 x", blocks[0].text)

    def test_missing_first_number_after_section_heading_is_recovered(self):
        raw = """一、选择题
12. 第十二题 (A) 1 (B) 2 (C) 3 (D) 4
二、填空题
$\\left(\\frac{1}{3}+x\\right)^{10}$ 的展开式中最大系数为 ____
14. 第十四题 ____
15. 第十五题 ____
"""
        blocks = blocksplit.split_blocks(raw)
        self.assertEqual([block.number for block in blocks], [12, 13, 14, 15])
        self.assertIn("展开式中最大系数", blocks[1].text)
        self.assertIn("填空题", blocks[1].section)

    def test_mangled_solution_numbers_are_recovered_from_gap(self):
        raw = """一、填空题
6. 第六题
7. 第七题
8. 第八题
19. 第十九题
20. 第二十题
21. 第二十一题
参考答案
6. 第六题解析。基础题7.6
8. 第八题解析
19. 第十九题解析
(2)(1)第一问答案 (2)第二问答案
21. 第二十一题解析
"""
        blocks = blocksplit.split_blocks(raw)
        solutions = [block for block in blocks if block.zone == "solution"]
        self.assertEqual([block.number for block in solutions], [6, 7, 8, 19, 20, 21])
        self.assertIn("7. 6", solutions[1].text)
        self.assertIn("(1)第一问答案", solutions[4].text)

    def test_inline_solution_number_before_detail_marker_is_recovered(self):
        raw = """一、选择题
1. 第一题 A. 甲 B. 乙 C. 丙 D. 丁
2. 第二题 A. 甲 B. 乙 C. 丙 D. 丁
3. 第三题 A. 甲 B. 乙 C. 丙 D. 丁
参考答案
1. A【详解】第一题说明。2. B【详解】第二题说明。
3. C【详解】第三题说明。
"""

        solutions = [block for block in blocksplit.split_blocks(raw)
                     if block.zone == "solution"]

        self.assertEqual([block.number for block in solutions], [1, 2, 3])
        self.assertNotIn("2. B", solutions[0].text)
        self.assertIn("第二题说明", solutions[1].text)

    def test_second_detail_marker_recovers_unnumbered_tail_solution(self):
        raw = """一、选择题
1. 第一题 A. 甲 B. 乙 C. 丙 D. 丁
2. 第二题 A. 甲 B. 乙 C. 丙 D. 丁
参考答案
1. A【详解】第一题说明。

（2）第二题短答案

【详解】第二题说明。
"""

        solutions = [block for block in blocksplit.split_blocks(raw)
                     if block.zone == "solution"]

        self.assertEqual([block.number for block in solutions], [1, 2])
        self.assertIn("第二题短答案", solutions[0].text)
        self.assertIn("【详解】第二题说明", solutions[1].text)

    def test_duplicate_choice_blocks_merge_full_stem_and_options(self):
        raw = """一、选择题
8. 第八题 A. 甲 B. 乙 C. 丙 D. 丁
9. 在光滑水平面上，质量相同的甲乙两小球沿同一直线相向运动，碰撞前后总动量守恒，要求判断下列结论（ ）
9. 在光滑水平面上，质量相同的甲乙两小球沿同一直线相向运动，碰撞前后总动量守恒，判断下列结论 A. 甲 B. 乙 C. 丙 D. 丁
10. 第十题 A. 甲 B. 乙 C. 丙 D. 丁
"""

        stems = [block for block in blocksplit.split_blocks(raw)
                 if block.zone == "stem"]

        self.assertEqual([block.number for block in stems], [8, 9, 10])
        self.assertIn("质量相同的甲乙两小球", stems[1].text)
        self.assertIn("A. 甲", stems[1].text)

    def test_duplicate_choice_blocks_with_different_stems_are_not_merged(self):
        raw = """一、选择题
8. 第八题 A. 甲 B. 乙 C. 丙 D. 丁
9. 在光滑水平面上，质量相同的甲乙两小球沿同一直线相向运动，碰撞前后总动量守恒，判断碰撞性质（ ）
9. 在竖直平面内，带电粒子从静止开始进入匀强磁场做圆周运动，已知半径和周期，判断电荷性质 A. 甲 B. 乙 C. 丙 D. 丁
10. 第十题 A. 甲 B. 乙 C. 丙 D. 丁
"""

        stems = [block for block in blocksplit.split_blocks(raw)
                 if block.zone == "stem"]

        self.assertEqual([block.number for block in stems], [8, 9, 9, 10])
        self.assertNotIn("A. 甲", stems[1].text)

    def test_duplicate_choice_blocks_without_continuous_neighbor_are_not_merged(self):
        raw = """一、选择题
5. 第五题 A. 甲 B. 乙 C. 丙 D. 丁
9. 在光滑水平面上，质量相同的甲乙两小球沿同一直线相向运动，碰撞前后总动量守恒，要求判断下列结论（ ）
9. 在光滑水平面上，质量相同的甲乙两小球沿同一直线相向运动，碰撞前后总动量守恒，判断下列结论 A. 甲 B. 乙 C. 丙 D. 丁
13. 第十三题 A. 甲 B. 乙 C. 丙 D. 丁
"""

        stems = [block for block in blocksplit.split_blocks(raw)
                 if block.zone == "stem"]

        self.assertEqual([block.number for block in stems], [5, 9, 9, 13])

    def test_duplicate_choice_blocks_with_short_stems_are_not_merged(self):
        raw = """一、选择题
8. 第八题 A. 甲 B. 乙 C. 丙 D. 丁
9. 两球相撞后如何运动（ ）
9. 两球相撞后如何运动 A. 甲 B. 乙 C. 丙 D. 丁
10. 第十题 A. 甲 B. 乙 C. 丙 D. 丁
"""

        stems = [block for block in blocksplit.split_blocks(raw)
                 if block.zone == "stem"]

        self.assertEqual([block.number for block in stems], [8, 9, 9, 10])

    def test_truncated_duplicate_solution_keeps_long_copy(self):
        short = "同一解析的开头文字" * 5
        long_tail = "后续完整推导" * 60
        raw = f"""一、选择题
1. 第一题 A. 甲 B. 乙 C. 丙 D. 丁
2. 第二题 A. 甲 B. 乙 C. 丙 D. 丁
参考答案
1. A【{chr(35814)}解】{short}
1. A【{chr(35814)}解】{short}{long_tail}
2. B【{chr(35814)}解】第二题说明。
"""

        solutions = [block for block in blocksplit.split_blocks(raw)
                     if block.zone == "solution"]

        self.assertEqual([block.number for block in solutions], [1, 2])
        self.assertIn(long_tail, solutions[0].text)

    def test_standalone_solution_duplicate_keeps_long_copy_after_zone_fallback(self):
        short = "同一解析的开头文字" * 5
        long_tail = "后续完整推导" * 60
        raw = f"""# 通用练习参考答案
1. A【{chr(35814)}解】{short}
1. A【{chr(35814)}解】{short}{long_tail}
2. B【{chr(35814)}解】第二题说明。
"""

        blocks = blocksplit.split_blocks(raw)

        self.assertEqual([block.number for block in blocks], [1, 2])
        self.assertIn(long_tail, blocks[0].text)
        self.assertNotIn(short + "\n1.", blocks[0].text)

    def test_duplicate_number_filling_single_gap_is_relabelled(self):
        raw = """一、选择题
9. 第九题 A. 甲 B. 乙 C. 丙 D. 丁
11. 实为第十题 A. 甲 B. 乙 C. 丙 D. 丁
11. 第十一题 A. 甲 B. 乙 C. 丙 D. 丁
12. 第十二题 A. 甲 B. 乙 C. 丙 D. 丁
"""

        stems = [block for block in blocksplit.split_blocks(raw)
                 if block.zone == "stem"]

        self.assertEqual([block.number for block in stems], [9, 10, 11, 12])
        self.assertTrue(stems[1].text.startswith("10."))

    def test_duplicate_number_is_not_relabelled_when_gap_number_exists(self):
        raw = """一、选择题
10. 已存在的第十题 A. 甲 B. 乙 C. 丙 D. 丁
9. 第九题 A. 甲 B. 乙 C. 丙 D. 丁
11. 另一道第十一题 A. 甲 B. 乙 C. 丙 D. 丁
11. 真正的第十一题 A. 甲 B. 乙 C. 丙 D. 丁
12. 第十二题 A. 甲 B. 乙 C. 丙 D. 丁
"""

        stems = [block for block in blocksplit.split_blocks(raw)
                 if block.zone == "stem"]

        self.assertEqual(
            [block.number for block in stems],
            [10, 9, 11, 11, 12],
        )

    def test_trailing_shifted_duplicate_numbers_use_complete_solution_evidence(self):
        raw = """一、选择题
1. 第一题 A. 甲 B. 乙 C. 丙 D. 丁
2. 第二题 A. 甲 B. 乙 C. 丙 D. 丁
2. 实为第三题 A. 甲 B. 乙 C. 丙 D. 丁
3. 实为第四题 A. 甲 B. 乙 C. 丙 D. 丁
# 参考答案与解析
1. A【解析】第一题。
2. B【解析】第二题。
3. C【解析】第三题。
4. D【解析】第四题。
"""

        blocks = blocksplit.split_blocks(raw)
        stems = [block for block in blocks if block.zone == "stem"]

        self.assertEqual([block.number for block in stems], [1, 2, 3, 4])
        self.assertTrue(stems[2].text.startswith("3."))
        self.assertTrue(stems[3].text.startswith("4."))

    def test_trailing_shifted_duplicate_numbers_require_complete_solution(self):
        raw = """一、选择题
1. 第一题 A. 甲 B. 乙 C. 丙 D. 丁
2. 第二题 A. 甲 B. 乙 C. 丙 D. 丁
2. 另一道第二题 A. 甲 B. 乙 C. 丙 D. 丁
3. 第三题 A. 甲 B. 乙 C. 丙 D. 丁
# 参考答案与解析
1. A【解析】第一题。
2. B【解析】第二题。
3. C【解析】第三题。
"""

        stems = [block for block in blocksplit.split_blocks(raw)
                 if block.zone == "stem"]

        self.assertEqual([block.number for block in stems], [1, 2, 2, 3])

    def test_number_range_continuation_is_merged_into_same_question(self):
        raw = """一、选择题
1. 第一题 A. 甲 B. 乙 C. 丙 D. 丁
2. 铁环从左到右依次编号为 1、2、
2...24. 在重力作用下自然下垂。A. 甲 B. 乙 C. 丙 D. 丁
3. 第三题 A. 甲 B. 乙 C. 丙 D. 丁
"""

        stems = [block for block in blocksplit.split_blocks(raw)
                 if block.zone == "stem"]

        self.assertEqual([block.number for block in stems], [1, 2, 3])
        self.assertIn("1、2、2...24", stems[1].text)

    def test_no_ai_render_keeps_section_question_type(self):
        raw = """一、选择题，本题共1小题，每小题只有一个选项符合要求
1. 单选题 A. 1 B. 2 C. 3 D. 4
二、多项选择题
2. 多选题 A. 1 B. 2 C. 3 D. 4
三、填空题
3. 填空题 ___
四、解答题
4. 求证结论
"""
        blocks = blockpipe.split_and_prep(raw)

        md = blockpipe.render_without_ai(blocks)
        questions = importer.split_questions(md)

        self.assertEqual(
            [importer.guess_type(question) for question in questions],
            ["单选题", "多选题", "填空题", "解答题"],
        )

    def test_image_only_choice_answer_blank_is_classified_as_single(self):
        raw = """一、选择题
1. 如图，侧视图是 ( )
![](images/stem.jpg)
![](images/a.jpg)
![](images/b.jpg)
![](images/c.jpg)
![](images/d.jpg)
"""
        md = blockpipe.render_without_ai(blockpipe.split_and_prep(raw))
        self.assertTrue(md.startswith("- [单选] 1."))

    def test_no_ai_render_applies_choice_and_subquestion_layout(self):
        raw = """一、选择题
1. 选择正确结论 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
二、解答题
2. 求解问题 (1) 求第一问；(2) 求第二问
"""
        md = blockpipe.render_without_ai(blockpipe.split_and_prep(raw))
        self.assertIn("$\\displaystyle A.$ 甲", md)
        self.assertIn("\n\n  （1）求第一问", md)
        self.assertIn("\n\n  （2）求第二问", md)

    def test_consecutive_solution_blocks_repair_fill_section_drift(self):
        raw = """三、填空题
13. 第一空 ___
14. 第二空 ___
15. 第三空 ___
16. 第四空 ___
17. 数列问题 (1) 求通项；(2) 求最小值
18. 三角形问题 (1) 求面积；(2) 判断是否存在
19. 立体几何问题 (1) 证明垂直；(2) 求余弦值
"""
        blocks = blockpipe.split_and_prep(raw)
        by_number = {block.number: block for block in blocks}
        self.assertIn("填空", by_number[16].section)
        self.assertIn("解答题", by_number[17].section)
        md = blockpipe.render_without_ai(blocks)
        questions = importer.split_questions(md)
        self.assertEqual(
            [importer.guess_type(question) for question in questions[-3:]],
            ["解答题", "解答题", "解答题"],
        )

    def test_generic_choice_section_with_fill_run_is_repaired(self):
        raw = """一、选择题
1. 集合的交集为 $\\underline{\\qquad}$
2. 极限等于 \\qquad
3. 复数的模为 .
4. 函数的反函数为
5. 最小值是
6. 行列式的值为
二、选择题
7. 正确的是 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
"""
        blocks = blockpipe.split_and_prep(raw)
        by_number = {block.number: block for block in blocks}
        self.assertTrue(all("填空题" in by_number[number].section
                            for number in range(1, 7)))
        self.assertIn("选择题", by_number[7].section)
        md = blockpipe.render_without_ai(blocks)
        questions = importer.split_questions(md)
        self.assertEqual(
            [importer.guess_type(question) for question in questions[:6]],
            ["填空题"] * 6,
        )

    def test_generic_choice_run_keeps_type_when_one_option_is_ocr_damaged(self):
        raw = """一、选择题
1. 第一题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
2. 第二题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
3. 第三题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
4. 第四题 ( ) (A) 甲 (B) 乙 (△) 丙 (D) 丁
"""
        blocks = blockpipe.split_and_prep(raw)
        md = blockpipe.render_without_ai(blocks)
        questions = importer.split_questions(md)
        self.assertEqual(
            [importer.guess_type(question) for question in questions],
            ["单选题"] * 4,
        )
        self.assertFalse(mechfix.has_complete_choice_options(blocks[-1].text))
        self.assertIn("第 4 题(只见 ABD)", qualcheck.check_option_count(blocks))

    def test_generic_choice_majority_does_not_relabel_trailing_fill_questions(self):
        raw = """一、选择题
1. 第一题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
2. 第二题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
3. 第三题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
4. 第四题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
5. 第五题的值为
6. 第六题的答案是
7. 第七题的参数等于
8. 第八题中 b =
"""
        blocks = blockpipe.split_and_prep(raw)
        md = blockpipe.render_without_ai(blocks)
        questions = importer.split_questions(md)

        self.assertEqual(
            [importer.guess_type(question) for question in questions],
            ["单选题"] * 4 + ["填空题"] * 4,
        )

    def test_solution_section_with_leading_fill_run_is_repaired(self):
        raw = """三、解答题
11. 向量的数量积为 ___
12. 函数的零点个数为 ___
13. 山的高度为 ___m
14. 圆的半径是 ___
15. 线段之比为 ___
16. 已知曲线与直线
（1）求曲线方程；
（2）求交点坐标。
"""
        blocks = blockpipe.split_and_prep(raw)
        md = blockpipe.render_without_ai(blocks)
        questions = importer.split_questions(md)

        self.assertEqual(
            [importer.guess_type(question) for question in questions],
            ["填空题"] * 5 + ["解答题"],
        )

    def test_fill_heading_with_choice_run_is_repaired(self):
        raw = """一、填空题
1. 第一题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
2. 第二题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
3. 第三题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
4. 第四题 ( ) (A) 甲 (B) 乙 (C) 丙 (D) 丁
二、填空题
5. 真填空题 ___
"""
        blocks = blockpipe.split_and_prep(raw)
        by_number = {block.number: block for block in blocks}
        self.assertTrue(all("单选题" in by_number[number].section
                            for number in range(1, 5)))
        self.assertIn("填空题", by_number[5].section)
        md = blockpipe.render_without_ai(blocks)
        questions = importer.split_questions(md)
        self.assertEqual(
            [importer.guess_type(question) for question in questions[:4]],
            ["单选题"] * 4,
        )

    def test_missing_question_with_stray_closing_paren_is_recovered(self):
        raw = """三、填空题
15. 椭圆上一点的坐标为 ___
) 16. 学生到工厂劳动实践，制作模型所需原料质量为 ___
四、解答题
17. 为了解两种离子的残留程度，进行统计试验
"""
        blocks = blockpipe.split_and_prep(raw)
        self.assertEqual([block.number for block in blocks], [15, 16, 17])
        by_number = {block.number: block for block in blocks}
        self.assertTrue(by_number[16].text.startswith("16. 学生到工厂"))
        self.assertNotIn(") 16", by_number[15].text)

    def test_answer_and_exam_analysis_heading_pairs_repeated_stems(self):
        raw = """一、填空题
1. 已知集合的交集为 ___
参考答案与试题解析
一、填空题
1. 已知集合的交集为 3.【思路分析】根据集合定义可得答案。
"""
        blocks = blockpipe.split_and_prep(raw)
        self.assertEqual([block.zone for block in blocks], ["stem", "solution"])
        md = blockpipe.render_without_ai(blocks)
        self.assertEqual(len(importer.split_questions(md)), 1)
        self.assertIn("【解析】", md)
        self.assertIn("3.【思路分析】", md)
        self.assertEqual(md.count("已知集合的交集为"), 1)

    def test_embedded_math_choice_labels_are_recovered(self):
        raw = (r"题干 ( ) (A) $2x-y=0(\mathrm{B})$ $2x+y=0"
               r"(\mathrm{C})$ $x-y=0(\mathrm{D})$ $x+y=0$")
        fixed = mechfix.normalize_block(raw)
        self.assertTrue(mechfix.has_complete_choice_options(fixed))
        normalized = mechfix.normalize_choice_options(fixed)
        self.assertIn("$\\displaystyle B.$", normalized)
        self.assertIn("$\\displaystyle D.$", normalized)

    def test_trailing_math_choice_label_is_recovered(self):
        raw = (r"题干 ( ) A. $x$ B. $y\mathrm{C}$ . $z$ "
               r"D. $w$")
        fixed = mechfix.normalize_block(raw)
        normalized = mechfix.normalize_choice_options(fixed)

        self.assertTrue(mechfix.has_complete_choice_options(fixed))
        self.assertIn("$\\displaystyle C.$ $\\displaystyle z$", normalized)
        self.assertNotIn(r"\mathrm{C}", normalized)

    def test_missing_a_label_after_figure_is_recovered(self):
        raw = ("3. 由表格可知结果为 ( )\n\n"
               "![](images/table.jpg)\n\n"
               "2.0m/s B. 1.8m/s C. 1.7m/s D. 1.5m/s")
        fixed = mechfix.normalize_block(raw)
        normalized = mechfix.normalize_choice_options(fixed)

        self.assertTrue(mechfix.has_complete_choice_options(fixed))
        self.assertIn("$\\displaystyle A.$ 2.0m/s", normalized)

    def test_missing_a_label_without_figure_is_not_guessed(self):
        raw = "题干 ( ) 普通正文 B. 乙 C. 丙 D. 丁"
        self.assertEqual(mechfix.normalize_missing_first_choice_label(raw), raw)

    def test_isolated_axis_label_after_option_image_is_removed(self):
        raw = """题干 ( )
(A) ![](images/a.jpg)
(B) ![](images/b.jpg)
(C) ![](images/c.jpg)
y
(D) ![](images/d.jpg)
"""
        fixed = mechfix.normalize_choice_options(raw)
        self.assertNotIn("\ny\n", fixed)
        self.assertIn("$\\displaystyle C.$ ![](images/c.jpg)", fixed)
        self.assertIn("$\\displaystyle D.$ ![](images/d.jpg)", fixed)

    def test_single_fill_block_with_subquestions_does_not_repair_section(self):
        raw = """三、填空题
13. 分两步填写：(1) 第一空；(2) 第二空
14. 普通填空 ___
"""
        blocks = blockpipe.split_and_prep(raw)
        self.assertIn("填空", blocks[0].section)

    def test_parenthesized_choice_labels_are_not_reported_missing(self):
        raw = """一、单选题，本题共1小题
1. 题干 (A) 选项甲 (B) 选项乙 (C) 选项丙 (D) 选项丁
"""
        blocks = blocksplit.split_blocks(raw)
        self.assertEqual(qualcheck.check_option_count(blocks), "")

        image_raw = """一、单选题
1. 看图选择 ( )
(A)
![](images/a.jpg)
(B)
![](images/b.jpg)
(C)
![](images/c.jpg)
(D)
![](images/d.jpg)
"""
        self.assertEqual(
            qualcheck.check_option_count(blocksplit.split_blocks(image_raw)), "")

        interval_raw = """一、单选题
10. 题干 (A) [6,14] (B) [6,12] (C) [8,14] (D) [8,12]
"""
        self.assertEqual(
            qualcheck.check_option_count(blocksplit.split_blocks(interval_raw)), "")

        trailing_blank_raw = """一、单选题
2. 计算结果为 (A) -5i (B) 5i (C) -5 (D) 5 （）
"""
        trailing_block = blocksplit.split_blocks(trailing_blank_raw)[0]
        self.assertTrue(mechfix.has_complete_choice_options(trailing_block.text))
        normalized = mechfix.normalize_choice_options(trailing_block.text)
        self.assertIn("$\\displaystyle A.$ -5i", normalized)
        self.assertIn("$\\displaystyle D.$ 5 （）", normalized)

    def test_choice_quality_check_uses_mechanical_quartet_and_answer_blank(self):
        canonical_raw = r"""一、单选题
1. 题干 ( ) $\displaystyle A.$ 甲 $\displaystyle B.$ 乙 $\displaystyle C.$ 丙 $\displaystyle D.$ 丁
"""
        self.assertEqual(
            qualcheck.check_option_count(blocksplit.split_blocks(canonical_raw)), "")

        mislabeled_fill_raw = """一、选择题
1. 若集合 A = {1, 2}, B = {2, 3}, 则 A 与 B 的交集为 ___
"""
        self.assertEqual(
            qualcheck.check_option_count(blocksplit.split_blocks(mislabeled_fill_raw)), "")

        incomplete_raw = """一、选择题
1. 请选择正确答案 ( ) (A) 甲 (B) 乙
"""
        self.assertIn(
            "只见 AB",
            qualcheck.check_option_count(blocksplit.split_blocks(incomplete_raw)),
        )

    def test_long_block_length_ignores_html_table_markup(self):
        markup = "<table>" + "<tr><td></td></tr>" * 80 + "<tr><td>数据</td></tr></table>"
        self.assertEqual(qualcheck._nonspace(markup), 2)

    def test_choice_labels_are_canonicalized_and_render_as_grid(self):
        import qrender

        raw = ("集合 $\\displaystyle A$ 与 $\\displaystyle B$ 满足条件 ( ) "
               "(A) [6,14] (B) [6,12] "
               "$\\displaystyle C$ [8,14] $\\displaystyle D$ [8,12]")
        fixed = mechfix.normalize_choice_options(raw)

        self.assertEqual(fixed.count("$\\displaystyle A.$"), 1)
        self.assertIn("\n$\\displaystyle B.$ [6,12]", fixed)
        self.assertIn("\n$\\displaystyle C.$ [8,14]", fixed)
        self.assertIn("\n$\\displaystyle D.$ [8,12]", fixed)
        self.assertNotIn("(A)", fixed)
        self.assertEqual(mechfix.normalize_choice_options(fixed), fixed)
        rendered = str(qrender.render_body(fixed, "单选题"))
        self.assertIn('class="q-opts"', rendered)
        self.assertIn('data-cols=', rendered)
        self.assertIn(
            "$\\displaystyle A.$ 正弦",
            mechfix.normalize_choice_options(
                "题干 ( ) (A) ) 正弦 (B) 乙 (C) 丙 (D) 丁"),
        )

    def test_incomplete_choice_sequence_is_not_rewritten(self):
        raw = "设点 $\\displaystyle A$、$\\displaystyle B$、$\\displaystyle C$"
        self.assertEqual(mechfix.normalize_choice_options(raw), raw)

    def test_nondotted_complete_options_are_still_classified_as_choice(self):
        raw = ("题干 ( ) $\\displaystyle A$ 1 $\\displaystyle B$ 2 "
               "$\\displaystyle C$ 3 $\\displaystyle D$ 4")
        self.assertEqual(importer.guess_type(raw), "单选题")
        self.assertEqual(__import__("blocknorm")._guess_type(raw, ""), "单选")

    def test_latex_quad_answer_blank_allows_unambiguous_weak_options(self):
        raw = ("函数的值为 $(\\quad)$ "
               "$\\displaystyle A$ 1 $\\displaystyle B$ 2 "
               "$\\displaystyle C$ 3 $\\displaystyle D$ 4")
        fixed = mechfix.normalize_choice_options(raw)

        self.assertTrue(mechfix.has_complete_choice_options(fixed))
        self.assertIn("\n$\\displaystyle B.$ 2", fixed)

    def test_known_choice_without_answer_blank_allows_exact_weak_labels(self):
        raw = ("函数值为 "
               "$\\displaystyle A$ -1 $\\displaystyle B$ 0 "
               "$\\displaystyle C$ 1 $\\displaystyle D$ 2")
        fixed = mechfix.normalize_choice_options(raw, known_choice=True)

        self.assertIn("$\\displaystyle A.$ -1", fixed)
        self.assertIn("\n$\\displaystyle D.$ 2", fixed)

    def test_known_choice_still_rejects_extra_event_letters(self):
        raw = ("事件关系为 "
               "$\\displaystyle A$ 事件 $\\displaystyle A$ 与事件 $\\displaystyle B$ 独立 "
               "$\\displaystyle B$ 结论乙 $\\displaystyle C$ 结论丙 "
               "$\\displaystyle D$ 结论丁")
        self.assertEqual(
            mechfix.normalize_choice_options(raw, known_choice=True), raw)

    def test_preview_uses_parenthesized_labels_before_math_points(self):
        import app

        raw = ("- [多选] 11. 已知点 A, B, C, P, Q, 则 ( )\n"
               "  (A) C 的准线为 y=-1 (B) 直线 AB 与 C 相切\n"
               "  (C) |OP||OQ|>|OA|^2 (D) |BP||BQ|>|BA|^2")
        preview, _folders, missing = app._build_import_preview(raw)

        self.assertIsNone(missing)
        body = preview[0]["body"]
        self.assertEqual(body.count("$\\displaystyle A.$"), 1)
        self.assertIn("\n$\\displaystyle B.$", body)
        self.assertIn("\n$\\displaystyle C.$", body)
        self.assertIn("\n$\\displaystyle D.$", body)

    def test_scrambled_choice_does_not_mix_stem_letters_into_options(self):
        raw = ("事件 $\\displaystyle A$、$\\displaystyle B$ 独立，值为 ( ) "
               "$\\displaystyle 1/4$ $\\displaystyle 1$ "
               "$\\displaystyle A$ $\\displaystyle 0$ "
               "$\\displaystyle B$ $\\displaystyle C$ "
               "$\\displaystyle 1/2$ $\\displaystyle D$ $\\displaystyle 1$")
        self.assertFalse(mechfix.has_complete_choice_options(raw))
        self.assertTrue(mechfix.looks_like_choice_options(raw))
        self.assertEqual(mechfix.normalize_choice_options(raw), raw)

    def test_four_math_points_without_answer_blank_are_not_options(self):
        raw = ("抛物线上有 $\\displaystyle A$、$\\displaystyle B$、"
               "$\\displaystyle C$、$\\displaystyle D$ 四点，求其关系 ___")
        self.assertFalse(mechfix.has_complete_choice_options(raw))
        self.assertFalse(mechfix.looks_like_choice_options(raw))
        self.assertEqual(mechfix.normalize_choice_options(raw), raw)

    def test_canonical_labels_win_over_math_letters_inside_options(self):
        raw = ("事件 $\\displaystyle A$ 与 $\\displaystyle B$ 的对立事件为 ( ) "
               "$\\displaystyle A.$ $\\displaystyle A\\cap B$ "
               "$\\displaystyle B.$ $\\displaystyle A\\cup B$ "
               "$\\displaystyle C.$ $\\displaystyle \\bar A\\cap\\bar B$ "
               "$\\displaystyle D.$ $\\displaystyle \\bar A\\cup\\bar B$")
        fixed = mechfix.normalize_choice_options(raw)
        self.assertTrue(mechfix.has_complete_choice_options(fixed))
        self.assertEqual(fixed.count("$\\displaystyle A.$"), 1)
        self.assertIn("\n$\\displaystyle B.$ $\\displaystyle A\\cup B$", fixed)

    def test_bare_right_parenthesis_label_completes_image_options(self):
        raw = (
            "关系图线正确的是 ( )\n\nA)\n\n![选项A](images/a.jpg)\n\n"
            "(B)\n\n![选项B](images/b.jpg)\n\n"
            "(C)\n\n![选项C](images/c.jpg)\n\n"
            "(D)\n\n![选项D](images/d.jpg)")

        self.assertTrue(mechfix.looks_like_choice_options(raw))
        self.assertTrue(mechfix.has_complete_choice_options(raw))
        fixed = mechfix.normalize_choice_options(raw)
        self.assertIn("$\\displaystyle A.$ ![选项A]", fixed)

    def test_right_parenthesis_inside_function_is_not_choice_label(self):
        raw = "已知 f(A) 的值，求对应函数关系。"

        self.assertFalse(mechfix.looks_like_choice_options(raw))
        self.assertFalse(mechfix.has_complete_choice_options(raw))

    def test_weak_choice_labels_do_not_consume_probability_events(self):
        raw = ("某试验中事件发生的概率为 ( ) "
               "$\\displaystyle A$ 事件 $\\displaystyle A$ 与事件 $\\displaystyle B$ 独立 "
               "$\\displaystyle B$ 事件 $\\displaystyle A$ 与事件 $\\displaystyle B$ 互斥 "
               "$\\displaystyle C$ 结论丙 $\\displaystyle D$ 结论丁")
        self.assertTrue(mechfix.looks_like_choice_options(raw))
        self.assertFalse(mechfix.has_complete_choice_options(raw))
        self.assertEqual(mechfix.normalize_choice_options(raw), raw)

    def test_solution_subquestions_are_split_without_touching_math(self):
        raw = ("已知 $\\displaystyle f(1)=2$ 且点为 $\\displaystyle (1,2)$ . "
               "(1) 求函数值; (2) 证明结论")
        fixed = mechfix.normalize_subquestion_layout(raw)

        self.assertIn("$\\displaystyle f(1)=2$", fixed)
        self.assertIn("$\\displaystyle (1,2)$", fixed)
        self.assertIn("\n\n（1）求函数值;\n\n（2）证明结论", fixed)
        self.assertEqual(mechfix.normalize_subquestion_layout(fixed), fixed)
        headed = "## $\\displaystyle 18$. 题干\n（1）求值;\n\n（2）证明"
        self.assertEqual(mechfix.normalize_subquestion_layout(headed), headed)

    def test_trailing_duplicate_subquestion_number_is_repaired(self):
        raw = "题干 (1) 求第一问; (2) 求第二问; (2) 是否存在第三种情况"
        fixed = mechfix.normalize_subquestion_layout(raw)
        self.assertIn("\n\n（1）求第一问", fixed)
        self.assertIn("\n\n（2）求第二问", fixed)
        self.assertIn("\n\n（3）是否存在第三种情况", fixed)
        reference = "题干 (1) 求证; (2) 求值. 由（2）可得结论"
        self.assertNotIn("（3）", mechfix.normalize_subquestion_layout(reference))

    def test_subquestion_references_are_not_split(self):
        raw = "由条件（1）和（2）可得结论"
        self.assertEqual(mechfix.normalize_subquestion_layout(raw), raw)

    def test_no_ai_render_respects_inline_multiple_choice_marker(self):
        raw = """一、选择题
1. （多选）下列说法正确的是 A. 1 B. 2 C. 3 D. 4
"""
        blocks = blockpipe.split_and_prep(raw)

        md = blockpipe.render_without_ai(blocks)
        questions = importer.split_questions(md)

        self.assertEqual(importer.guess_type(questions[0]), "多选题")

    def test_solution_heading_does_not_create_empty_solution_section(self):
        raw = filestore._join_sections("题干", "## 【解析】\n推导过程", [])
        stem, sections = filestore._split_sections(raw)
        self.assertEqual(stem, "题干")
        self.assertEqual(sections, [("解析", "推导过程")])

        inline_answer = filestore._join_sections(
            "题干", "## 【答案】 $BD$\n\n【解析】推导过程", [])
        stem, sections = filestore._split_sections(inline_answer)
        self.assertEqual(stem, "题干")
        self.assertEqual(
            sections, [("解析", "【答案】 $BD$\n\n【解析】推导过程")])

    def test_fill_question_gets_answer_line_before_trailing_image(self):
        body = "已知函数，求 $f(1)$。\n\n![[figure.png]]"
        fixed = mechfix.ensure_fill_blank(body, "填空题")
        self.assertEqual(fixed, "已知函数，求 $f(1)$。  ___\n\n![[figure.png]]")
        self.assertEqual(mechfix.ensure_fill_blank(fixed, "填空题"), fixed)
        latex_blank = r"结果为 $\underline{\hspace{2cm}}$"
        self.assertEqual(mechfix.ensure_fill_blank(latex_blank, "填空题"), latex_blank)
        self.assertNotIn("___", mechfix.ensure_fill_blank("求证。", "解答题"))
        self.assertEqual(
            mechfix.ensure_fill_blank(r"结果为 $x=\_$", "填空题"),
            r"结果为 $x=$ ___",
        )

    def test_solution_layout_separates_methods_and_collapses_blank_lines(self):
        raw = """解法一：先求导，
再判断单调性。



解法二（几何法）：作辅助线。
（1）先证明平行。



（2）再计算长度。"""
        fixed = mechfix.normalize_solution_layout(raw)
        self.assertEqual(
            fixed,
            "解法一：先求导， 再判断单调性。\n\n"
            "解法二（几何法）：作辅助线。\n\n"
            "（1）先证明平行。\n\n（2）再计算长度。",
        )
        self.assertNotIn("\n\n\n", fixed)

    def test_answer_table_mixed_choice_ranges_and_complete_order_are_preserved(self):
        raw = r"""一、选择题：第 1 ~ 2 题只有一项符合要求，第 3 ~ 4 题有多项符合要求。
1. 第一题 (A) 甲 (B) 乙 (C) 丙 (D) 丁
3. 第三题 (A) 甲 (B) 乙 (C) 丙 (D) 丁
4. 第四题 (A) 甲 (B) 乙 (C) 丙 (D) 丁
2. 第二题 (A) 甲 (B) 乙 (C) 丙 (D) 丁

# 参考答案与解析
<table><tr><td>题号</td><td>1</td><td>2</td><td>3</td><td>4</td></tr><tr><td>答案</td><td>C</td><td>D</td><td>AD</td><td>BC</td></tr></table>
"""
        notes = []
        blocks = blockpipe.split_and_prep(raw, note_sink=notes.append)
        self.assertNotIn("题号不连续", "".join(notes))

        md = blockpipe.render_without_ai(blocks)
        questions = importer.split_questions(md)
        self.assertEqual([importer.block_number(q) for q in questions], [1, 2, 3, 4])
        self.assertEqual(
            [importer.guess_type(q) for q in questions],
            ["单选题", "单选题", "多选题", "多选题"],
        )
        for question, answer in zip(questions, ("C", "D", "AD", "BC")):
            _stem, solution = importer.split_solution(question, scan_markers=True)
            self.assertIsNotNone(solution)
            self.assertIn(f"【答案】{answer}", solution)

    def test_solution_card_collapses_ocr_blank_lines_but_keeps_formula(self):
        import qrender

        rendered = str(qrender.render_solution(
            "解得\n\n\n$$\nx=1\n$$\n\n(2 分)"))
        self.assertNotIn("\n\n", rendered)
        self.assertIn("$$\nx=1\n$$", rendered)


if __name__ == "__main__":
    unittest.main()
