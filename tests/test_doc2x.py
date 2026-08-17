import io
import json
import re
import tempfile
import unittest
import zipfile
import zlib
from contextlib import nullcontext
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import crypto_utils
import converter
import doc2x_client
import doc2x_store
import qualcheck


class _Response:
    def __init__(self, payload=None, *, status=200, content=b""):
        self._payload = payload
        self.status_code = status
        self._content = content

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def iter_content(self, chunk_size=1024 * 1024):
        del chunk_size
        yield self._content


class _Session:
    def __init__(self, export_zip: bytes):
        self.export_zip = export_zip
        self.calls = []
        self.parse_polls = 0
        self.export_polls = 0

    @staticmethod
    def _ok(data):
        return _Response({"code": "success", "data": data})

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/parse/preupload"):
            return self._ok({"uid": "uid-1", "url": "https://upload.test/one"})
        if url.endswith("/parse/status"):
            self.parse_polls += 1
            if self.parse_polls == 1:
                return self._ok({"status": "processing", "progress": 50})
            return self._ok({
                "status": "success", "progress": 100,
                "result": {"pages": [{"page_idx": 0, "score": 91,
                                        "layout": {"blocks": []}}]},
            })
        if url.endswith("/convert/parse"):
            return self._ok({"status": "processing", "url": ""})
        if url.endswith("/convert/parse/result"):
            self.export_polls += 1
            if self.export_polls == 1:
                return self._ok({"status": "processing", "url": ""})
            return self._ok({"status": "success", "url": "https://download.test/out"})
        raise AssertionError((method, url))

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return _Response(status=200)

    def get(self, url, **kwargs):
        self.calls.append(("DOWNLOAD", url, kwargs))
        return _Response(status=200, content=self.export_zip)


def _export_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("bundle/paper.md", "1. 题目\n\n![](images/a.png)\n")
        archive.writestr("bundle/images/a.png", b"png")
    return output.getvalue()


class Doc2XClientTests(unittest.TestCase):
    def test_full_flow_exports_markdown_images_and_layout_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4 test")
            session = _Session(_export_zip())
            result = doc2x_client.Doc2XClient("secret", session=session).parse_pdf(
                pdf, extract_dir=root / "out", poll_interval=0)

            self.assertEqual((91,), result.page_scores)
            self.assertIn("![](images/a.png)", result.markdown)
            self.assertEqual(b"png", (root / "out/images/a.png").read_bytes())
            meta = json.loads((root / "out/paper_doc2x.json").read_text("utf-8"))
            self.assertEqual(91, meta["pages"][0]["score"])
            self.assertEqual(result.markdown,
                             (root / "out/paper_raw.md").read_text("utf-8"))
            preupload = session.calls[0]
            self.assertEqual("v3-2026", preupload[2]["json"]["model"])
            export = next(call for call in session.calls
                          if call[0] == "POST" and call[1].endswith("/convert/parse"))
            self.assertEqual("dollar", export[2]["json"]["formula_mode"])
            self.assertEqual(0, export[2]["json"]["formula_level"])

    def test_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zip_path = root / "bad.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("paper.md", "text")
                archive.writestr("../images/escape.png", b"bad")
            with self.assertRaisesRegex(doc2x_client.Doc2XError, "不安全路径"):
                doc2x_client._safe_extract_markdown(zip_path, root / "out", "paper")
            self.assertFalse((root / "images/escape.png").exists())

    def test_business_error_has_actionable_message(self):
        class ErrorSession(_Session):
            def request(self, method, url, **kwargs):
                del method, url, kwargs
                return _Response({"code": "parse_quota_limit", "msg": "quota"})

        client = doc2x_client.Doc2XClient("secret", session=ErrorSession(b""))
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            with self.assertRaisesRegex(doc2x_client.Doc2XError, "额度不足"):
                client.parse_pdf(pdf, extract_dir=Path(tmp) / "out")

    def test_v3_layout_repairs_scrambled_figure_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_dir = Path(tmp)
            blocks = []
            filenames = {}
            for index, letter in enumerate("ABCD"):
                x = 100 + index * 100
                filename = f"0_{x}_200_80_60_0.jpg"
                filenames[letter] = filename
                (image_dir / filename).write_bytes(letter.encode())
                parent = f"group-{letter}"
                blocks.extend([
                    {"id": parent, "type": "FigureGroup",
                     "bbox": [x - 20, 190, x + 80, 270]},
                    {"id": f"fig-{letter}", "type": "Figure", "parent_id": parent,
                     "bbox": [x, 200, x + 80, 260],
                     "src": f"https://img.test/a.jpg?x={x}&y=200&w=80&h=60&r=0"},
                    {"id": f"cap-{letter}", "type": "Caption", "parent_id": parent,
                     "bbox": [x - 20, 250, x, 270], "text": f"{letter}."},
                ])
            meta = {"pages": [{"page_idx": 0, "layout": {"blocks": blocks}}]}
            markdown = (
                "4. 选择正确图形\n\n"
                f"![](images/{filenames['D']})\n\nD.\n\nA.\n\n"
                f"![](images/{filenames['A']})\n\nB.\n\n"
                f"![](images/{filenames['B']})\n\n"
                f"![](images/{filenames['C']})\n\n5. 下一题")
            repaired, count = doc2x_client._repair_figure_choice_order(
                markdown, meta, image_dir)
            self.assertEqual(1, count)
            expected = [f"{letter}.\n\n![](images/{filenames[letter]})"
                        for letter in "ABCD"]
            self.assertTrue(all(part in repaired for part in expected))
            self.assertLess(repaired.index("A."), repaired.index("B."))
            self.assertLess(repaired.index("B."), repaired.index("C."))
            self.assertLess(repaired.index("C."), repaired.index("D."))
            self.assertIn("5. 下一题", repaired)

            guarded = markdown.replace(
                f"![](images/{filenames['B']})",
                f"![](images/{filenames['B']})\n\n不得删除的选项文字")
            unchanged, count = doc2x_client._repair_figure_choice_order(
                guarded, meta, image_dir)
            self.assertEqual(0, count)
            self.assertEqual(guarded, unchanged)

    def test_cached_layout_infers_missing_cd_labels_and_moves_next_question_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_dir = Path(tmp)
            blocks = [
                {"id": "q1", "type": "Text", "text": "1. 第一题（ ）",
                 "bbox": [90, 180, 1500, 260]},
                {"id": "group-a", "type": "FigureGroup",
                 "bbox": [90, 260, 437, 515]},
                {"id": "cap-a", "type": "Caption", "parent_id": "group-a",
                 "text": "A.", "bbox": [90, 370, 134, 409]},
                {"id": "fig-a", "type": "Figure", "parent_id": "group-a",
                 "bbox": [146, 266, 437, 509],
                 "src": "https://img.test/a?x=146&y=266&w=291&h=243&r=0"},
                {"id": "label-b", "type": "Text", "text": "B.",
                 "bbox": [443, 371, 482, 409]},
                {"id": "fig-b", "type": "Figure", "bbox": [494, 264, 832, 511],
                 "src": "https://img.test/b?x=494&y=264&w=338&h=247&r=0"},
                {"id": "fig-c", "type": "Figure", "bbox": [843, 262, 1179, 511],
                 "src": "https://img.test/c?x=843&y=262&w=336&h=249&r=0"},
                {"id": "fig-d", "type": "Figure", "bbox": [1190, 259, 1508, 515],
                 "src": "https://img.test/d?x=1190&y=259&w=318&h=256&r=0"},
                {"id": "q2", "type": "Text", "text": "2. 第二题（ ）",
                 "bbox": [90, 511, 1128, 668]},
                {"id": "fig-q2", "type": "Figure", "bbox": [1138, 523, 1554, 845],
                 "src": "https://img.test/q2?x=1138&y=523&w=416&h=322&r=0"},
            ]
            names = [
                "0_146_266_291_243_0.jpg", "0_494_264_338_247_0.jpg",
                "0_843_262_336_249_0.jpg", "0_1190_259_318_256_0.jpg",
                "0_1138_523_416_322_0.jpg",
            ]
            for name in names:
                # 合集缓存会同时保留 Doc2X 原名和加 exam_ 命名空间后的副本；布局
                # 映射不能因同一裁图出现两种文件名就放弃修复。
                (image_dir / name).write_bytes(b"image")
                (image_dir / ("exam_" + name)).write_bytes(b"image")
            refs = [f"![](images/exam_{name})" for name in names]
            markdown = (
                "1. 第一题（ ）\n\nA.\n\n" + refs[0]
                + "\n\nB.\n\n" + refs[1] + "\n\n" + refs[2]
                + "\n\n" + refs[3] + "\n\n" + refs[4]
                + "\n\n2. 第二题（ ）\n\nA. 甲 B. 乙 C. 丙 D. 丁")
            meta = {"pages": [{"page_idx": 0, "layout": {"blocks": blocks}}]}

            repaired, moved, choices = doc2x_client.repair_markdown_from_layout(
                markdown, meta, image_dir)

            self.assertEqual(1, moved)
            self.assertEqual(1, choices)
            first, second = repaired.split("2. 第二题", 1)
            self.assertNotIn(refs[4], first)
            self.assertIn(refs[4], second)
            for letter, ref in zip("ABCD", refs[:4]):
                self.assertIn(f"{letter}.\n\n{ref}", first)


class Doc2XStoreTests(unittest.TestCase):
    def test_key_is_encrypted_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_path = root / "doc2x_local.json"
            legacy_path = root / "doc2x.json"
            key_path = root / ".enc_key"
            with patch.object(doc2x_store.config, "DOC2X_LOCAL_KEY_PATH", store_path), \
                    patch.object(doc2x_store.config, "DOC2X_KEY_PATH", legacy_path), \
                    patch.object(crypto_utils, "_KEY_PATH", key_path):
                self.assertTrue(doc2x_store.set_key("sk-test-secret"))
                self.assertTrue(doc2x_store.has_key())
                self.assertEqual("sk-test-secret", doc2x_store.resolve())
                self.assertNotIn("sk-test-secret", store_path.read_text("utf-8"))
                doc2x_store.clear_key()
                self.assertFalse(doc2x_store.has_key())

    def test_legacy_file_and_new_keys_share_multi_key_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_path = root / "doc2x_local.json"
            legacy_path = root / "doc2x.json"
            key_path = root / ".enc_key"
            with patch.object(doc2x_store.config, "DOC2X_LOCAL_KEY_PATH", store_path), \
                    patch.object(doc2x_store.config, "DOC2X_KEY_PATH", legacy_path), \
                    patch.object(crypto_utils, "_KEY_PATH", key_path):
                legacy_path.write_text(json.dumps({"keys": [{
                    "id": "legacy-1", "label": "升级前", "added": "",
                    "key_enc": crypto_utils.encrypt_token("sk-legacy"),
                }]}), encoding="utf-8")
                self.assertEqual(["sk-legacy"], doc2x_store.resolve_all())
                self.assertTrue(doc2x_store.list_keys()[0]["legacy"])

                self.assertTrue(doc2x_store.add_key("sk-second", "备用号"))
                self.assertEqual(["sk-second", "sk-legacy"],
                                 doc2x_store.resolve_all())
                keys = doc2x_store.list_keys()
                self.assertEqual(["备用号", "升级前"], [item["label"] for item in keys])
                self.assertTrue(doc2x_store.remove_key(keys[0]["id"]))
                self.assertEqual(["sk-legacy"], doc2x_store.resolve_all())
                self.assertFalse(doc2x_store.remove_key(keys[1]["id"]))
                self.assertNotIn("sk-second", store_path.read_text("utf-8"))


class ConverterAdapterTests(unittest.TestCase):
    def test_real_single_question_raw_keeps_all_subquestions_through_finish(self):
        raw = r"""19. (本小题满分 17 分)

已知一簇双曲线，按规则构造点 $A_n$。

(1)求点 $A_2$ 的坐标；

(2)求 $a_3^{-1}+\cdots+a_{20}^{-1}$ 的值；

(3)求 $\theta$ 的最小值。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "single.png"
            Image.new("RGB", (20, 12), "white").save(image_path)
            cfg = SimpleNamespace(mineru_token="token",
                                  mineru_model_version="vlm")

            with patch.object(converter, "_RAW_MD_ROOT", root / "raw"), \
                    patch.object(converter, "_alpha_cwd",
                                 return_value=nullcontext()), \
                    patch.object(converter, "_load_config_for_user",
                                 return_value=cfg), \
                    patch.object(converter, "_parse_with_ocr_backend",
                                 return_value=(raw, "raw.md", {})), \
                    patch.object(converter, "_intercept_images",
                                 side_effect=lambda md, *_args, **_kwargs: md), \
                    patch.object(converter.corpus, "archive"):
                pending = converter.convert_file_to_blocks(
                    image_path, is_image=True)
                converter._ensure_src_on_path()
                with patch("src.pipeline._cleanup_temp"):
                    md = converter.finish_block_review(
                        pending, action="skip", include_solution=False)

            self.assertEqual(1, len(pending["blocks"]))
            self.assertIn("19. ", md)
            for marker in ("（1）", "（2）", "（3）"):
                self.assertIn(marker, md)

    def test_single_image_and_batch_pdf_share_pairing_loss_note(self):
        raw = """1. 第一题题干

2. 第二题题干

# 参考答案

77. 【解析】{tail}
""".format(tail="无法配对的完整解析正文。" * 30)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "single.png"
            Image.new("RGB", (8, 8), "white").save(image_path)
            batch_pdf = root / "batch.pdf"
            batch_pdf.write_bytes(b"%PDF-1.4 test")
            cfg = SimpleNamespace(mineru_token="token",
                                  mineru_model_version="vlm")

            def fake_parse(*_args, **_kwargs):
                return raw, "raw.md", {}

            with patch.object(converter, "_RAW_MD_ROOT", root / "raw"), \
                    patch.object(converter, "_alpha_cwd",
                                 return_value=nullcontext()), \
                    patch.object(converter, "_load_config_for_user",
                                 return_value=cfg), \
                    patch.object(converter, "_parse_with_ocr_backend",
                                 side_effect=fake_parse):
                for source, is_image in ((image_path, True), (batch_pdf, False)):
                    with self.subTest(source=source.name):
                        notes = []
                        pending = converter.convert_file_to_blocks(
                            source, is_image=is_image, note_sink=notes.append)
                        self.assertTrue(pending["blocks"])
                        self.assertTrue(any(
                            "无法与题目配对" in note for note in notes))

    def test_images_to_pdf_keeps_pixels_and_dimensions_losslessly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            image = Image.new("RGB", (7, 5))
            pixels = [((x * 37) % 256, (y * 61) % 256, ((x + y) * 29) % 256)
                      for y in range(5) for x in range(7)]
            image.putdata(pixels)
            image.save(source, format="PNG")
            expected = image.tobytes()
            image.close()

            target = converter.images_to_pdf([source], root / "out.pdf")
            pdf = target.read_bytes()

            self.assertIn(b"/Filter /FlateDecode", pdf)
            self.assertNotIn(b"/DCTDecode", pdf)
            self.assertIn(b"/Width 7 /Height 5", pdf)
            self.assertIn(b"/MediaBox [0 0 7 5]", pdf)
            marker = b"/Subtype /Image"
            image_pos = pdf.index(marker)
            stream_pos = pdf.index(b"stream\n", image_pos) + len(b"stream\n")
            length_match = re.search(rb"/Length (\d+)", pdf[image_pos:stream_pos])
            self.assertIsNotNone(length_match)
            length = int(length_match.group(1))
            self.assertEqual(expected, zlib.decompress(
                pdf[stream_pos:stream_pos + length]))

    def test_images_to_pdf_embeds_jpeg_without_reencoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            Image.new("RGB", (11, 6), "white").save(
                source, format="JPEG", quality=91)
            jpeg = source.read_bytes()

            pdf = converter.images_to_pdf([source], root / "out.pdf").read_bytes()

            self.assertIn(b"/Filter /DCTDecode", pdf)
            self.assertIn(b"/Width 11 /Height 6", pdf)
            image_pos = pdf.index(b"/Subtype /Image")
            stream_pos = pdf.index(b"stream\n", image_pos) + len(b"stream\n")
            length = int(re.search(
                rb"/Length (\d+)", pdf[image_pos:stream_pos]).group(1))
            self.assertEqual(jpeg, pdf[stream_pos:stream_pos + length])

    def test_images_to_pdf_handles_gray_alpha_rotation_and_multiple_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gray = root / "gray.jpg"
            alpha = root / "alpha.png"
            rotated = root / "rotated.jpg"

            Image.new("L", (13, 7), 160).save(gray, "JPEG", quality=90)
            transparent = Image.new("RGBA", (9, 4), (255, 0, 0, 0))
            transparent.putpixel((0, 0), (0, 0, 0, 255))
            transparent.save(alpha, "PNG")
            base = Image.new("RGB", (5, 12), "white")
            exif = base.getexif()
            exif[274] = 6
            base.save(rotated, "JPEG", quality=90, exif=exif)

            pdf = converter.images_to_pdf(
                [gray, alpha, rotated], root / "out.pdf").read_bytes()

            self.assertIn(b"/Count 3", pdf)
            self.assertIn(b"/ColorSpace /DeviceGray", pdf)
            self.assertIn(b"/MediaBox [0 0 13 7]", pdf)
            self.assertIn(b"/MediaBox [0 0 9 4]", pdf)
            self.assertIn(b"/MediaBox [0 0 12 5]", pdf)
            self.assertEqual(1, pdf.count(b"/Filter /DCTDecode"))
            self.assertEqual(2, pdf.count(b"/Filter /FlateDecode"))

            # 透明 PNG 按白纸语义合成：首像素黑，其余透明像素变白。
            image_positions = [m.start() for m in re.finditer(b"/Subtype /Image", pdf)]
            alpha_pos = image_positions[1]
            stream_pos = pdf.index(b"stream\n", alpha_pos) + len(b"stream\n")
            length = int(re.search(
                rb"/Length (\d+)", pdf[alpha_pos:stream_pos]).group(1))
            pixels = zlib.decompress(pdf[stream_pos:stream_pos + length])
            self.assertEqual(b"\x00\x00\x00\xff\xff\xff", pixels[:6])

    def test_images_to_pdf_rejects_whole_group_when_one_image_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good.png"
            corrupt = root / "corrupt.png"
            output = root / "out.pdf"
            Image.new("RGB", (8, 6), "white").save(good, "PNG")
            corrupt.write_bytes(b"not a real png")

            with self.assertRaises(converter.ConvertError):
                converter.images_to_pdf([good, corrupt], output)

            self.assertFalse(output.exists())

    def test_images_to_pdf_cleans_partial_output_when_writer_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            output = root / "out.pdf"
            Image.new("RGB", (8, 6), "white").save(source, "PNG")

            def fail_after_partial_write(_pages, out_path):
                Path(out_path).write_bytes(b"%PDF-1.4 partial")
                raise OSError("simulated disk failure")

            with patch.object(converter, "_write_image_pdf",
                              side_effect=fail_after_partial_write), \
                    self.assertRaises(converter.ConvertError):
                converter.images_to_pdf([source], output)

            self.assertFalse(output.exists())

    def test_empty_doc2x_result_names_the_selected_backend(self):
        with self.assertRaisesRegex(converter.ConvertError, "Doc2X 没有从"):
            converter._ensure_raw_text(
                "![](images/page.png)", Path("paper.pdf"),
                ocr_backend=converter.OCR_DOC2X)

    def test_doc2x_path_does_not_require_mineru_token(self):
        seen = {}

        def fake_parse(_client, path, *, extract_dir):
            seen["provider"] = "doc2x"
            Path(extract_dir).mkdir(parents=True, exist_ok=True)
            return doc2x_client.Doc2XResult(
                "1. 第一题。\n\n2. 第二题。", "paper.md", "uid", "v3-2026", (90,))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            cfg = SimpleNamespace(mineru_token="placeholder",
                                  mineru_model_version="vlm")
            with patch.object(converter, "_RAW_MD_ROOT", root / "raw"), \
                    patch.object(converter, "_load_config_for_user",
                                 return_value=cfg) as load_cfg, \
                  patch.object(doc2x_client.Doc2XClient, "parse_pdf", fake_parse):
                pending = converter.convert_file_to_blocks(
                    pdf, ocr_backend=converter.OCR_DOC2X,
                    doc2x_api_key="doc2x-secret")
            self.assertEqual("doc2x", seen["provider"])
            self.assertEqual(2, len(pending["blocks"]))
            self.assertEqual("doc2x", pending["ocr_backend"])
            self.assertFalse(load_cfg.call_args.kwargs["require_mineru"])

    def test_high_confidence_text_and_doc2x_warnings_require_manual_review(self):
        notes = []
        converter._clean_mineru_text(
            "1. 含�乱码", Path("乱码卷.pdf"), note_sink=notes.append)

        def fake_parse(_client, _path, *, extract_dir):
            Path(extract_dir).mkdir(parents=True, exist_ok=True)
            return doc2x_client.Doc2XResult(
                "1. 第一题。", "paper.md", "uid", "v3-2026", (79,))

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(doc2x_client.Doc2XClient, "parse_pdf", fake_parse):
            root = Path(tmp)
            (root / "paper.pdf").write_bytes(b"%PDF-1.4")
            converter._parse_with_ocr_backend(
                root / "paper.pdf", root / "out", SimpleNamespace(),
                ocr_backend=converter.OCR_DOC2X, doc2x_api_key="secret",
                note_sink=notes.append)

        self.assertEqual(2, len(notes))
        self.assertTrue(all(
            qualcheck.MANUAL_REVIEW_MARKER in note for note in notes))

    def test_intercept_images_keeps_missing_reference_and_requires_review(self):
        original = "题干\n\n![题图](images/missing.png)"
        notes = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(converter.config, "ASSETS_DIR",
                             Path(tmp) / "assets"):
            converted = converter._intercept_images(
                original, Path(tmp) / "extract", "paper",
                note_sink=notes.append)

        self.assertEqual(original, converted)
        self.assertEqual(1, len(notes))
        self.assertIn(qualcheck.MANUAL_REVIEW_MARKER, notes[0])

    def test_intercept_images_keeps_corrupt_reference_and_requires_review(self):
        original = "题干\n\n![题图](images/broken.png)"
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "extract" / "images"
            image_dir.mkdir(parents=True)
            (image_dir / "broken.png").write_bytes(b"not a real png")
            assets_dir = root / "assets"

            with patch.object(converter.config, "ASSETS_DIR", assets_dir):
                converted = converter._intercept_images(
                    original, root / "extract", "paper",
                    note_sink=notes.append)

            self.assertEqual(original, converted)
            self.assertEqual(1, len(notes))
            self.assertIn(qualcheck.MANUAL_REVIEW_MARKER, notes[0])
            self.assertFalse(any(assets_dir.iterdir()))

    def test_intercept_images_avoids_overwrite_and_reuses_identical_asset(self):
        original = "题干\n\n![](images/chart.png)"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets_dir = root / "assets"

            def intercept(folder, color):
                image_dir = root / folder / "images"
                image_dir.mkdir(parents=True)
                Image.new("RGB", (4, 3), color).save(
                    image_dir / "chart.png", "PNG")
                with patch.object(converter.config, "ASSETS_DIR", assets_dir):
                    converted = converter._intercept_images(
                        original, root / folder, "paper")
                match = re.fullmatch(r"题干\n\n!\[\[([^\]]+)\]\]", converted)
                self.assertIsNotNone(match)
                return converted, match.group(1)

            first_md, first_name = intercept("first", "red")
            first_bytes = (assets_dir / first_name).read_bytes()
            second_md, second_name = intercept("second", "blue")

            self.assertNotEqual(first_md, second_md)
            self.assertNotEqual(first_name, second_name)
            self.assertEqual(first_bytes, (assets_dir / first_name).read_bytes())
            self.assertNotEqual(first_bytes, (assets_dir / second_name).read_bytes())

            repeated_md, repeated_name = intercept("repeated", "blue")
            self.assertEqual(second_md, repeated_md)
            self.assertEqual(second_name, repeated_name)
            self.assertEqual(2, len(list(assets_dir.iterdir())))

    def test_image_reference_loss_requires_manual_review(self):
        raw = ("1. 第一题\n\n![](images/one.png)\n\n"
               "2. 第二题\n\n![](images/two.png)")
        normalized = "- [解答] 1. 第一题\n\n- [解答] 2. 第二题\n\n![](images/two.png)"
        notes = []

        converter._check_preserved_image_refs(
            raw, normalized, note_sink=notes.append,
            include_solution=True)

        self.assertEqual(1, len(notes))
        self.assertIn(qualcheck.MANUAL_REVIEW_MARKER, notes[0])
        self.assertIn("one.png", notes[0])

    def test_image_reference_check_scopes_only_selected_numbers(self):
        raw = ("1. 第一题\n\n![](images/one.png)\n\n"
               "2. 第二题\n\n![](images/two.png)")
        normalized = "- [解答] 2. 第二题\n\n![](images/two.png)"
        notes = []

        converter._check_preserved_image_refs(
            raw, normalized, note_sink=notes.append,
            include_solution=False, only_numbers=[2])

        self.assertEqual([], notes)

    def test_dual_files_with_same_image_name_keep_both_sides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_dir = root / "exam"
            solution_dir = root / "solution"
            for folder, color in ((exam_dir, "red"), (solution_dir, "blue")):
                (folder / "images").mkdir(parents=True)
                Image.new("RGB", (4, 3), color).save(
                    folder / "images" / "same.png", "PNG")

            exam_md, solution_md = converter._merge_dual_image_trees(
                "1. 题干\n\n![](images/same.png)",
                "1. 解析\n\n![](images/same.png)",
                exam_dir, solution_dir)

            self.assertIn("images/exam_same.png", exam_md)
            self.assertIn("images/solution_same.png", solution_md)
            self.assertTrue((exam_dir / "images" / "exam_same.png").is_file())
            self.assertTrue((exam_dir / "images" / "solution_same.png").is_file())
            assets_dir = root / "assets"
            with patch.object(converter.config, "ASSETS_DIR", assets_dir):
                converted = converter._intercept_images(
                    exam_md + "\n\n" + solution_md, exam_dir, "paper")
            refs = re.findall(r"!\[\[([^\]]+)\]\]", converted)
            self.assertEqual(2, len(refs))
            self.assertNotEqual(refs[0], refs[1])
            self.assertNotEqual(
                (assets_dir / refs[0]).read_bytes(),
                (assets_dir / refs[1]).read_bytes())

    def test_html_image_reference_is_counted_and_intercepted(self):
        notes = []
        converter._check_preserved_image_refs(
            '1. 题干 <img src="images/chart.png" alt="图">',
            "- [解答] 1. 题干", note_sink=notes.append,
            include_solution=True)
        self.assertEqual(1, len(notes))
        self.assertIn("chart.png", notes[0])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "extract" / "images").mkdir(parents=True)
            Image.new("RGB", (4, 3), "green").save(
                root / "extract" / "images" / "chart.png", "PNG")
            with patch.object(converter.config, "ASSETS_DIR", root / "assets"):
                converted = converter._intercept_images(
                    '<img src="images/chart.png" alt="图">',
                    root / "extract", "paper")
            self.assertRegex(converted, r"^!\[\[paper_[0-9a-f]+_chart\.png\]\]$")

    def test_html_image_namespace_rewrites_src_not_matching_alt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "extract" / "images").mkdir(parents=True)
            Image.new("RGB", (4, 3), "green").save(
                root / "extract" / "images" / "x.png", "PNG")
            converted = converter._namespace_dual_images(
                '<img alt="x.png" src="images/x.png">',
                root / "extract", "exam")

            self.assertEqual(
                '<img alt="x.png" src="images/exam_x.png">', converted)

    def test_docx_preprocessing_writes_only_to_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "paper.docx"
            source.write_bytes(b"fake docx")
            adjacent_pdf = root / "paper.pdf"
            adjacent_pdf.write_bytes(b"real user pdf")
            work_dir = root / "work"

            def fake_pandoc(command, **_kwargs):
                Path(command[command.index("-o") + 1]).write_bytes(
                    b"generated intermediate pdf")
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(converter, "_image_only_docx_to_pdf",
                              return_value=None), \
                    patch.object(converter.subprocess, "run",
                                 side_effect=fake_pandoc):
                converted = converter._docx_to_pdf(source, work_dir)

            self.assertEqual(work_dir / "paper_word_input.pdf", converted)
            self.assertEqual(b"generated intermediate pdf",
                             converted.read_bytes())
            self.assertEqual(b"real user pdf", adjacent_pdf.read_bytes())

    def test_oversized_image_preprocessing_writes_only_to_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "photo.png"
            Image.new("RGB", (5, 4), "white").save(source, "PNG")
            adjacent_pdf = root / "photo.pdf"
            adjacent_pdf.write_bytes(b"real user pdf")
            work_dir = root / "work"

            with patch.object(converter, "_IMAGE_DIRECT_LIMIT_BYTES", 0):
                converted = converter._oversized_image_to_pdf(source, work_dir)

            self.assertEqual(work_dir / "photo_image_input.pdf", converted)
            self.assertTrue(converted.read_bytes().startswith(b"%PDF-1."))
            self.assertEqual(b"real user pdf", adjacent_pdf.read_bytes())


class AppRouteTests(unittest.TestCase):
    def test_settings_route_keeps_legacy_doc2x_and_lists_both_pools(self):
        import app as quiz_app

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(quiz_app.config, "DOC2X_KEY_PATH",
                             Path(tmp) / "doc2x.json"), \
                patch.object(quiz_app.config, "DOC2X_LOCAL_KEY_PATH",
                             Path(tmp) / "doc2x_local.json"), \
                patch.object(quiz_app.crypto_utils, "_KEY_PATH",
                             Path(tmp) / ".enc_key"):
            legacy = quiz_app.config.DOC2X_KEY_PATH
            legacy.write_text(json.dumps({"keys": [{
                "id": "legacy-1", "label": "升级前", "added": "",
                "key_enc": crypto_utils.encrypt_token("sk-legacy"),
            }]}), encoding="utf-8")
            client = quiz_app.app.test_client()
            response = client.post(
                "/settings/doc2x",
                data={"doc2x_key": "sk-secret", "doc2x_label": "legacy-label"},
                headers={"X-CSRF-Token": quiz_app._WRITE_TOKEN})
            self.assertEqual(302, response.status_code)
            self.assertTrue(legacy.is_file())
            self.assertNotIn("sk-legacy", legacy.read_text(encoding="utf-8"))
            self.assertEqual(["legacy-label", "升级前"],
                             [item["label"] for item in
                              quiz_app.doc2x_store.list_keys()])
            self.assertEqual({"sk-secret", "sk-legacy"},
                             set(quiz_app.doc2x_store.resolve_all()))

    def test_batch_rejects_multiple_non_image_files_without_silent_drop(self):
        import app as quiz_app

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(quiz_app.config, "BATCH_UPLOAD_DIR", Path(tmp)), \
                patch.object(quiz_app, "_persist_job"), \
                patch.object(quiz_app, "_persist_batch"), \
                patch.object(quiz_app.threading, "Thread") as thread_cls:
            response = quiz_app.app.test_client().post(
                "/batch-convert/create",
                data={
                    "_csrf_token": quiz_app._WRITE_TOKEN,
                    "groups[0][file]": [
                        (io.BytesIO(b"%PDF-1.4\n"), "one.pdf"),
                        (io.BytesIO(b"%PDF-1.4\n"), "two.pdf"),
                    ],
                }, content_type="multipart/form-data")

            self.assertEqual(400, response.status_code)
            self.assertIn("请拆成不同任务组", response.get_json()["error"])
            self.assertEqual([], list(Path(tmp).iterdir()))
            thread_cls.return_value.start.assert_not_called()

    def test_batch_multiple_images_are_merged_losslessly_in_order(self):
        import app as quiz_app

        def png(color):
            stream = io.BytesIO()
            Image.new("RGB", (16, 10), color).save(stream, format="PNG")
            stream.seek(0)
            return stream

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(quiz_app.config, "BATCH_UPLOAD_DIR", Path(tmp)), \
                patch.object(quiz_app, "_persist_job"), \
                patch.object(quiz_app, "_persist_batch"), \
                patch.object(quiz_app.threading, "Thread") as thread_cls:
            response = quiz_app.app.test_client().post(
                "/batch-convert/create",
                data={
                    "_csrf_token": quiz_app._WRITE_TOKEN,
                    "ocr_backend": "doc2x",
                    "groups[0][file]": [
                        (png("white"), "001.png"),
                        (png("black"), "002.png"),
                    ],
                }, content_type="multipart/form-data")
            self.assertEqual(200, response.status_code,
                             response.get_data(as_text=True))
            batch_id = response.get_json()["batch_id"]
            try:
                group = quiz_app._batch_jobs[batch_id]["groups"][0]
                merged = Path(group["file_path"])
                pdf = merged.read_bytes()
                self.assertEqual(".pdf", merged.suffix)
                self.assertEqual(2, pdf.count(b"/Type /Page "))
                self.assertEqual(2, pdf.count(b"/Filter /FlateDecode"))
                self.assertEqual(3, len(group["cleanup_paths"]))
                thread_cls.return_value.start.assert_called_once()
            finally:
                batch = quiz_app._batch_jobs.pop(batch_id, None)
                for group in (batch or {}).get("groups", []):
                    quiz_app._jobs.pop(group["job_id"], None)

    def test_batch_snapshot_keeps_selected_doc2x_backend(self):
        import app as quiz_app

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(quiz_app.config, "BATCH_UPLOAD_DIR", Path(tmp)), \
                patch.object(quiz_app, "_persist_job"), \
                patch.object(quiz_app, "_persist_batch"), \
                patch.object(quiz_app.threading, "Thread") as thread_cls:
            response = quiz_app.app.test_client().post(
                "/batch-convert/create",
                data={
                    "_csrf_token": quiz_app._WRITE_TOKEN,
                    "ocr_backend": "doc2x",
                    "groups[0][file]": (io.BytesIO(b"%PDF-1.4\n"), "paper.pdf"),
                }, content_type="multipart/form-data")
            self.assertEqual(200, response.status_code, response.get_data(as_text=True))
            batch_id = response.get_json()["batch_id"]
            try:
                group = quiz_app._batch_jobs[batch_id]["groups"][0]
                self.assertEqual("doc2x", group["ocr_backend"])
                self.assertEqual("doc2x", quiz_app._jobs[group["job_id"]]["ocr_backend"])
                thread_cls.return_value.start.assert_called_once()
            finally:
                batch = quiz_app._batch_jobs.pop(batch_id, None)
                for group in (batch or {}).get("groups", []):
                    quiz_app._jobs.pop(group["job_id"], None)


if __name__ == "__main__":
    unittest.main()
