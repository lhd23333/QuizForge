"""好题单题专线与好资料纯文本出口的 converter 级行为（离线）。

OCR 层（_parse_with_ocr_backend / _prep_for_ocr）全部 mock，不发网络；
页锚注入用合成的 content_list + raw 对，不引入任何真实试卷内容。
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import blocksplit
import converter


class _ConverterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "sample.png"
        self.source.write_bytes(b"img")
        self.raw_root = self.root / "raw_md"
        self.raw_root.mkdir()
        self._patches = [
            mock.patch.object(converter, "_RAW_MD_ROOT", self.raw_root),
            mock.patch.object(converter, "_alpha_cwd",
                              lambda: contextlib.nullcontext()),
            mock.patch.object(converter, "_load_config_for_user",
                              return_value=None),
            mock.patch.object(converter, "_prep_for_ocr",
                              side_effect=lambda path, *a, **k: Path(path)),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        self.tmp.cleanup()

    def _write_content_list(self, stem: str, rows) -> Path:
        extract_dir = self.raw_root / stem
        extract_dir.mkdir(parents=True, exist_ok=True)
        (extract_dir / f"{stem}_content_list.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return extract_dir


class SingleBlockTests(_ConverterTestBase):
    def test_single_block_parses_number_and_rejects_page_markers(self):
        raw = ("<!-- quizforge:source-page-break -->\n"
               "3. 已知函数 $f(x)$，求最小值。")
        with mock.patch.object(converter, "_parse_with_ocr_backend",
                               return_value=(raw, "sample.md", {})):
            pending = converter.convert_file_to_blocks(
                self.source, "token", is_image=True, ocr_backend="mineru",
                single_block=True)
        self.assertTrue(pending.get("single_block"))
        self.assertEqual(len(pending["blocks"]), 1)
        block = pending["blocks"][0]
        # 题号必须在构造块前解析出来（渲染时按 number 归一化补回），
        # 残留的白名单页标记不能漏进题卡正文。
        self.assertEqual(block["number"], 3)
        self.assertEqual(block["zone"], "stem")
        self.assertNotIn("source-page-break", block["text"])
        self.assertIn("已知函数", block["text"])

    def test_single_block_without_number_keeps_text_intact(self):
        raw = "在平面直角坐标系中，则 $|OP|$ 的取值范围为 \\_\\_\\_\\_."
        with mock.patch.object(converter, "_parse_with_ocr_backend",
                               return_value=(raw, "sample.md", {})):
            pending = converter.convert_file_to_blocks(
                self.source, "token", is_image=True, ocr_backend="mineru",
                single_block=True)
        block = pending["blocks"][0]
        self.assertIsNone(block["number"])
        self.assertIn("在平面直角坐标系中", block["text"])

    def test_single_block_rejects_only_numbers_before_ocr(self):
        with self.assertRaises(converter.ConvertError) as caught:
            converter.convert_file_to_blocks(
                self.source, "token", is_image=True, ocr_backend="mineru",
                single_block=True, only_numbers=[1, 2])
        self.assertIn("单题模式", str(caught.exception))


class FinishReviewGuardTests(_ConverterTestBase):
    def _pending(self) -> dict:
        return {
            "blocks": [{"index": 0, "number": 3, "text": "3. 题目",
                        "section": None, "group": None, "zone": "stem",
                        "line_no": 1, "kind": "unknown"}],
            "extract_dirs": [], "keep_images": False, "source_name": "x.png",
            "boundary_mode": "auto", "ocr_backend": "mineru", "ocr_meta": {},
            "single_block": True,
        }

    def test_empty_llm_output_falls_back_to_mechanical_render(self):
        notes = []
        with mock.patch.object(converter, "_make_llm_client",
                               return_value=object()), \
                mock.patch("blockpipe.normalize_and_render", return_value=""), \
                mock.patch("blockpipe.render_without_ai",
                           return_value="- [解答] 3. 题目") as render:
            md = converter.finish_block_review(
                self._pending(), action="ai", include_solution=True,
                provider=object(), note_sink=notes.append)
        render.assert_called_once()
        self.assertIn("题目", md)
        self.assertTrue(notes)   # 回退必须留人工校对提示，不能静默

    def test_empty_llm_output_without_single_flag_still_raises(self):
        """护栏只服务单题专线；批量路径保持原行为（空输出=显式失败）。"""
        pending = self._pending()
        pending.pop("single_block")
        with mock.patch.object(converter, "_make_llm_client",
                               return_value=object()), \
                mock.patch("blockpipe.normalize_and_render", return_value=""):
            with self.assertRaises(converter.ConvertError):
                converter.finish_block_review(
                    pending, action="ai", include_solution=True,
                    provider=object())


class InjectPageBreaksTests(_ConverterTestBase):
    def test_reliable_injection_with_page_numbers(self):
        raw = "第一页内容段落甲。\n\n第二页内容段落乙。\n\n第三页内容段落丙。"
        rows = [
            {"type": "text", "text": "第一页内容段落甲。", "page_idx": 0},
            {"type": "text", "text": "第二页内容段落乙。", "page_idx": 1},
            {"type": "text", "text": "第三页内容段落丙。", "page_idx": 2},
        ]
        extract_dir = self._write_content_list("doc", rows)
        text, info = converter.inject_content_page_breaks(raw, extract_dir)
        self.assertEqual(info["status"], "reliable")
        self.assertEqual(info["page_total"], 3)
        self.assertIn(blocksplit.source_page_break_marker(2) + "\n"
                      + "第二页内容段落乙。", text)
        self.assertIn(blocksplit.source_page_break_marker(3) + "\n"
                      + "第三页内容段落丙。", text)

    def test_skipped_page_does_not_shift_later_page_numbers(self):
        """跳过的页不产生标记，后续页号不漂移（裸标记方案会在这翻车）。"""
        raw = "第一页内容段落甲。\n\n第三页内容段落丙。"
        rows = [
            {"type": "text", "text": "第一页内容段落甲。", "page_idx": 0},
            {"type": "text", "text": "这页的文本不在 markdown 里", "page_idx": 1},
            {"type": "text", "text": "第三页内容段落丙。", "page_idx": 2},
        ]
        extract_dir = self._write_content_list("doc2", rows)
        text, info = converter.inject_content_page_breaks(raw, extract_dir)
        self.assertEqual(info["status"], "partial")
        self.assertEqual(info["skipped"], [2])
        self.assertIn(blocksplit.source_page_break_marker(3), text)
        self.assertNotIn(blocksplit.source_page_break_marker(2), text)

    def test_duplicate_anchor_page_is_skipped(self):
        raw = "重复段落。\n\n重复段落。\n\n独一无二的第三页。"
        rows = [
            {"type": "text", "text": "重复段落。", "page_idx": 0},
            {"type": "text", "text": "重复段落。", "page_idx": 1},
            {"type": "text", "text": "独一无二的第三页。", "page_idx": 2},
        ]
        extract_dir = self._write_content_list("doc3", rows)
        text, info = converter.inject_content_page_breaks(raw, extract_dir)
        self.assertEqual(info["skipped"], [2])
        self.assertIn(blocksplit.source_page_break_marker(3), text)

    def test_out_of_order_anchor_is_skipped(self):
        raw = "第二页唯一开头。\n\n第一页唯一开头。"
        rows = [
            {"type": "text", "text": "第一页唯一开头。", "page_idx": 0},
            {"type": "text", "text": "第二页唯一开头。", "page_idx": 1},
        ]
        extract_dir = self._write_content_list("doc4", rows)
        text, info = converter.inject_content_page_breaks(raw, extract_dir)
        # 第 2 页的锚点出现在第 1 页之前（OCR 乱序）：跳过它，不注入逆序标记。
        self.assertEqual(info["skipped"], [2])
        self.assertNotIn("source-page-break", text)

    def test_missing_content_list_returns_raw_unavailable(self):
        raw = "没有任何页信息。"
        text, info = converter.inject_content_page_breaks(
            raw, self.root / "empty")
        self.assertEqual(text, raw)
        self.assertEqual(info["status"], "unavailable")


class PlainTextExitTests(_ConverterTestBase):
    def test_plain_text_exit_returns_text_with_page_breaks(self):
        raw = "第一页内容段落甲。\n\n第二页内容段落乙。"
        extract_dir = self._write_content_list("sample", [
            {"type": "text", "text": "第一页内容段落甲。", "page_idx": 0},
            {"type": "text", "text": "第二页内容段落乙。", "page_idx": 1},
        ])
        with mock.patch.object(converter, "_parse_with_ocr_backend",
                               return_value=(raw, "sample.md", {})), \
                mock.patch("corpus.archive") as archive, \
                mock.patch("src.pipeline._cleanup_temp"):
            text, info = converter.convert_file_plain_text(
                self.source, "token", is_image=True, ocr_backend="mineru",
                inject_pages=True)
        self.assertEqual(info["status"], "reliable")
        self.assertIn(blocksplit.source_page_break_marker(2), text)
        archive.assert_called_once()
        # 带标记的原文要重写回磁盘（corpus 留档磁盘优先于 texts，不重写的话
        # 留档里永远没有页码证据）。
        archived = (extract_dir / "sample_raw.md").read_text(encoding="utf-8")
        self.assertIn(blocksplit.source_page_break_marker(2), archived)

    def test_plain_text_exit_rejects_empty_body(self):
        with mock.patch.object(converter, "_parse_with_ocr_backend",
                               return_value=("   \n\n", "sample.md", {})):
            with self.assertRaises(converter.ConvertError) as caught:
                converter.convert_file_plain_text(
                    self.source, "token", is_image=True, ocr_backend="mineru")
        self.assertIn("正文为空", str(caught.exception))


class ProfileEndToEndTests(_ConverterTestBase):
    """mock 最底层 OCR，驱动 profile 层把 converter 新路径真跑一遍。"""

    def test_good_question_end_to_end_without_number(self):
        import source_profiles

        raw = ("在平面直角坐标系中, A、B、C、D 是圆 $x^{2} + y^{2} = 1$ "
               "上四个不同的点, 交点为 P, 则 $|OP|$ 的取值范围为 ___")
        with mock.patch.object(converter, "_parse_with_ocr_backend",
                               return_value=(raw, "sample.md", {})), \
                mock.patch("corpus.archive"), \
                mock.patch("src.pipeline._cleanup_temp"):
            result = source_profiles.convert_good_question(
                self.source, self.root / "stage", ocr_backend="mineru",
                engine="block", normalize=False)
        card = Path(result["staged_root"]) / result["cards"][0]
        self.assertEqual(card.name, "sample.md")
        text = card.read_text(encoding="utf-8")
        self.assertIn("在平面直角坐标系中", text)
        self.assertNotIn("source-page-break", text)
        # 真实渲染链之后的收尾：裸单字母包 `$\displaystyle $`（监管链路此前的缺口）
        self.assertIn("$\\displaystyle A$", text)
        self.assertIn("$\\displaystyle P$", text)
        self.assertFalse(result["warnings"])

    def test_good_paper_end_to_end_with_any_start_number(self):
        """好卷走原版链路（_build_import_preview）：题号从 12 起连续也全部
        成卡、不报缺 1..11；解析并进同一张题卡。"""
        import source_profiles

        raw = ("一、选择题\n\n"
               "12. 已知函数 $f(x)=x^{2}$，求最小值。\n"
               "A. $0$  B. $1$  C. $2$  D. $3$\n\n"
               "13. 判断 $g(x)=x^{3}$ 的奇偶性。\n"
               "解：$g(-x)=-g(x)$，故为奇函数。")
        with mock.patch.object(converter, "_parse_with_ocr_backend",
                               return_value=(raw, "sample.md", {})), \
                mock.patch("corpus.archive"), \
                mock.patch("src.pipeline._cleanup_temp"):
            result = source_profiles.convert_good_paper(
                self.source, self.root / "paper", ocr_backend="mineru",
                engine="block", normalize=False)
        names = sorted(Path(p).name for p in result["cards"])
        self.assertEqual(names, ["sample第12题.md", "sample第13题.md"])
        card = (Path(result["staged_root"]) / "sample" / "sample第12题.md"
                ).read_text(encoding="utf-8")
        self.assertIn("已知函数", card)
        self.assertIn("\nnumber: 12\n", card)
        self.assertNotIn("[单选]", card)   # 契约题型标签不进卡片正文
        self.assertFalse([w for w in result["warnings"] if "缺题号" in w])
        # 解析并入题卡：`## 解析` 段在，独立的解析卡不存在。
        second = (Path(result["staged_root"]) / "sample" / "sample第13题.md"
                  ).read_text(encoding="utf-8")
        self.assertIn("## 解析", second)
        self.assertIn("奇函数", second)

    def test_good_material_end_to_end_with_pages_and_index(self):
        import source_profiles

        raw = ("第一章 函数\n\n"
               "1. 已知 $f(x)=x^2$，求最小值。\n"
               "2. 判断 $g(x)$ 的奇偶性。\n\n"
               "第二章 导数\n\n"
               "3. 计算 $h(x)$ 的导数。")
        self._write_content_list("sample", [
            {"type": "text", "text": "第一章 函数", "page_idx": 0},
            {"type": "text", "text": "1. 已知 $f(x)=x^2$，求最小值。",
             "page_idx": 0},
            {"type": "text", "text": "2. 判断 $g(x)$ 的奇偶性。", "page_idx": 0},
            {"type": "text", "text": "第二章 导数", "page_idx": 1},
            {"type": "text", "text": "3. 计算 $h(x)$ 的导数。", "page_idx": 1},
        ])
        with mock.patch.object(converter, "_parse_with_ocr_backend",
                               return_value=(raw, "sample.md", {})), \
                mock.patch("corpus.archive"), \
                mock.patch("src.pipeline._cleanup_temp"):
            result = source_profiles.convert_good_material(
                self.source, self.root / "mat", ocr_backend="mineru",
                engine="block", normalize=False)
        names = sorted(Path(p).name for p in result["cards"])
        self.assertIn("sample第1题.md", names)
        self.assertIn("sample第2题.md", names)
        self.assertIn("sample第3题.md", names)
        self.assertIn("sample正文第1块.md", names)
        self.assertIn("sample正文第2块.md", names)
        self.assertIn("sample_页码索引.md", names)
        cards_dir = Path(result["staged_root"]) / "sample"
        q1 = (cards_dir / "sample第1题.md").read_text(encoding="utf-8")
        self.assertIn("source_page_start: 1", q1)
        q3 = (cards_dir / "sample第3题.md").read_text(encoding="utf-8")
        self.assertIn("source_page_start: 2", q3)
        body2 = (cards_dir / "sample正文第2块.md").read_text(encoding="utf-8")
        self.assertIn("第二章 导数", body2)
        index_text = (cards_dir / "sample_页码索引.md").read_text(
            encoding="utf-8")
        self.assertIn("[[sample第3题]]", index_text)
        self.assertEqual(result["page_info"]["status"], "reliable")


if __name__ == "__main__":
    unittest.main()
