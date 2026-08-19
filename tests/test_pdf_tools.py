"""资料库 PDF 页面工具的离线回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pypdf import PdfReader, PdfWriter

import library_pdf_tools


def _write_pdf(path: Path, widths: list[int], *, encrypted: bool = False) -> None:
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=500)
    writer.add_metadata({"/Title": "测试 PDF"})
    if encrypted:
        writer.encrypt("secret")
    with path.open("wb") as stream:
        writer.write(stream)


def _widths(path: Path) -> list[int]:
    return [int(page.mediabox.width) for page in PdfReader(str(path)).pages]


class PdfToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def assert_no_staging(self):
        self.assertEqual(list(self.root.glob(".quizforge-pdf-*.pdf")), [])

    def test_inspect_uses_one_based_pages_and_rejects_encrypted_pdf(self):
        source = self.root / "source.pdf"
        encrypted = self.root / "encrypted.pdf"
        _write_pdf(source, [100, 200])
        _write_pdf(encrypted, [100], encrypted=True)

        info = library_pdf_tools.inspect_pdf(source)

        self.assertEqual(info["page_count"], 2)
        self.assertEqual([page["number"] for page in info["pages"]], [1, 2])
        self.assertEqual([page["width"] for page in info["pages"]], [100.0, 200.0])
        self.assertEqual(info["metadata"]["title"], "测试 PDF")
        with self.assertRaises(library_pdf_tools.PdfToolError) as caught:
            library_pdf_tools.inspect_pdf(encrypted)
        self.assertEqual(caught.exception.code, "encrypted_pdf")

    def test_merge_preserves_input_order_and_uses_new_default_name(self):
        first = self.root / "甲.pdf"
        second = self.root / "乙.pdf"
        _write_pdf(first, [100, 110])
        _write_pdf(second, [200])

        output = library_pdf_tools.merge_pdfs([first, second])

        self.assertEqual(output, self.root / "甲-合并.pdf")
        self.assertEqual(_widths(output), [100, 110, 200])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assert_no_staging()

    def test_merge_rejects_existing_output_and_encrypted_input_without_partial(self):
        first = self.root / "first.pdf"
        second = self.root / "second.pdf"
        encrypted = self.root / "encrypted.pdf"
        output = self.root / "result.pdf"
        _write_pdf(first, [100])
        _write_pdf(second, [200])
        _write_pdf(encrypted, [300], encrypted=True)
        output.write_bytes(b"keep")

        with self.assertRaises(library_pdf_tools.PdfToolError) as caught:
            library_pdf_tools.merge_pdfs([first, second], output_path=output)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(output.read_bytes(), b"keep")

        encrypted_output = self.root / "encrypted-result.pdf"
        with self.assertRaises(library_pdf_tools.PdfToolError) as caught:
            library_pdf_tools.merge_pdfs(
                [first, encrypted], output_path=encrypted_output)
        self.assertEqual(caught.exception.code, "encrypted_pdf")
        self.assertFalse(encrypted_output.exists())
        self.assert_no_staging()

    def test_extract_uses_one_based_numbers_and_keeps_requested_order(self):
        source = self.root / "source.pdf"
        _write_pdf(source, [100, 200, 300])

        output = library_pdf_tools.extract_pages(source, [3, 1])

        self.assertEqual(_widths(output), [300, 100])
        with self.assertRaises(library_pdf_tools.PdfToolError) as caught:
            library_pdf_tools.extract_pages(
                source, [1, 1], output_path=self.root / "duplicate.pdf")
        self.assertEqual(caught.exception.code, "duplicate_page")
        self.assertFalse((self.root / "duplicate.pdf").exists())

    def test_reorder_requires_a_complete_permutation(self):
        source = self.root / "source.pdf"
        _write_pdf(source, [100, 200, 300])

        output = library_pdf_tools.reorder_pages(source, [3, 1, 2])

        self.assertEqual(_widths(output), [300, 100, 200])
        for order in ([1, 2], [1, 2, 2], [1, 2, 4]):
            target = self.root / f"bad-{len(order)}-{order[-1]}.pdf"
            with self.assertRaises(library_pdf_tools.PdfToolError):
                library_pdf_tools.reorder_pages(
                    source, order, output_path=target)
            self.assertFalse(target.exists())

    def test_rotate_only_accepts_quarter_turns_and_selected_pages(self):
        source = self.root / "source.pdf"
        _write_pdf(source, [100, 200, 300])

        output = library_pdf_tools.rotate_pages(source, [2, 3], 90)
        rotations = [int(page.rotation or 0)
                     for page in PdfReader(str(output)).pages]

        self.assertEqual(rotations, [0, 90, 90])
        invalid_output = self.root / "invalid-rotation.pdf"
        with self.assertRaises(library_pdf_tools.PdfToolError) as caught:
            library_pdf_tools.rotate_pages(
                source, [1], 45, output_path=invalid_output)
        self.assertEqual(caught.exception.code, "invalid_rotation")
        self.assertFalse(invalid_output.exists())

    def test_split_requires_complete_ranges_and_names_each_output(self):
        source = self.root / "source.pdf"
        _write_pdf(source, [100, 200, 300, 400])

        outputs = library_pdf_tools.split_pdf(source, [(1, 1), (2, 4)])

        self.assertEqual(outputs, [
            self.root / "source-第1页.pdf",
            self.root / "source-第2-4页.pdf",
        ])
        self.assertEqual([_widths(path) for path in outputs], [
            [100], [200, 300, 400],
        ])
        invalid_output = self.root / "source-第2页.pdf"
        with self.assertRaises(library_pdf_tools.PdfToolError) as caught:
            library_pdf_tools.split_pdf(source, [(1, 1), (3, 4)])
        self.assertEqual(caught.exception.code, "incomplete_ranges")
        self.assertFalse(invalid_output.exists())
        self.assert_no_staging()

    def test_split_preflights_all_conflicts_before_creating_outputs(self):
        source = self.root / "source.pdf"
        _write_pdf(source, [100, 200, 300])
        conflict = self.root / "source-第2-3页.pdf"
        conflict.write_bytes(b"keep")

        with self.assertRaises(library_pdf_tools.PdfToolError) as caught:
            library_pdf_tools.split_pdf(source, [(1, 1), (2, 3)])

        self.assertEqual(caught.exception.code, "conflict")
        self.assertFalse((self.root / "source-第1页.pdf").exists())
        self.assertEqual(conflict.read_bytes(), b"keep")
        self.assert_no_staging()

    def test_failed_validation_removes_staging_and_never_publishes(self):
        source = self.root / "source.pdf"
        output = self.root / "result.pdf"
        _write_pdf(source, [100])
        error = library_pdf_tools.PdfToolError(
            "模拟校验失败", code="output_validation_failed")

        with mock.patch.object(
                library_pdf_tools, "_validate_staged", side_effect=error):
            with self.assertRaises(library_pdf_tools.PdfToolError):
                library_pdf_tools.extract_pages(
                    source, [1], output_path=output)

        self.assertFalse(output.exists())
        self.assert_no_staging()

    def test_output_must_not_replace_source(self):
        source = self.root / "source.pdf"
        _write_pdf(source, [100, 200])
        original = source.read_bytes()

        with self.assertRaises(library_pdf_tools.PdfToolError) as caught:
            library_pdf_tools.extract_pages(source, [1], output_path=source)

        self.assertEqual(caught.exception.code, "output_is_source")
        self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
