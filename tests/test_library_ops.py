import tempfile
import unittest
from pathlib import Path

import library_ops


class LibraryOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_folder_and_markdown_normalizes_newlines(self):
        folder = library_ops.create_folder(self.root, "", "资料")
        note = library_ops.create_markdown(
            self.root, folder.path, "例题", "第一行\r\n第二行\r第三行")

        self.assertEqual(folder.path, "资料")
        self.assertEqual(note.path, "资料/例题.markdown")
        self.assertEqual((self.root / "资料" / "例题.markdown").read_bytes(),
                         b"\xe7\xac\xac\xe4\xb8\x80\xe8\xa1\x8c\n\xe7\xac\xac\xe4\xba\x8c\xe8\xa1\x8c\n\xe7\xac\xac\xe4\xb8\x89\xe8\xa1\x8c")

    def test_rejects_traversal_hidden_and_invalid_names(self):
        for path in ("../outside", ".obsidian", "C:/outside"):
            with self.subTest(path=path), self.assertRaises(library_ops.LibraryOperationError):
                library_ops.create_folder(self.root, path, "新文件夹")
        for name in (".", "CON", "a?.md", " 前导", "尾部 ", "尾部. "):
            with self.subTest(name=name), self.assertRaises(library_ops.LibraryOperationError):
                library_ops.create_markdown(self.root, "", name)

    def test_rename_does_not_overwrite(self):
        (self.root / "原稿.pdf").write_bytes(b"one")
        (self.root / "已有.pdf").write_bytes(b"two")

        renamed = library_ops.rename_entry(self.root, "原稿.pdf", "新稿.pdf")
        self.assertEqual(renamed.path, "新稿.pdf")
        self.assertFalse((self.root / "原稿.pdf").exists())
        with self.assertRaises(library_ops.LibraryOperationError) as caught:
            library_ops.rename_entry(self.root, "新稿.pdf", "已有.pdf")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual((self.root / "已有.pdf").read_bytes(), b"two")

    def test_file_rename_keeps_original_extension(self):
        (self.root / "讲义.DOCX").write_bytes(b"docx")

        renamed = library_ops.rename_entry(self.root, "讲义.DOCX", "课程资料")
        self.assertEqual(renamed.path, "课程资料.DOCX")
        self.assertTrue((self.root / "课程资料.DOCX").exists())

        same_extension = library_ops.rename_entry(
            self.root, "课程资料.DOCX", "最终稿.docx")
        self.assertEqual(same_extension.path, "最终稿.DOCX")
        with self.assertRaises(library_ops.LibraryOperationError) as caught:
            library_ops.rename_entry(self.root, "最终稿.DOCX", "最终稿.pdf")
        self.assertEqual(caught.exception.code, "extension_change_rejected")
        self.assertEqual((self.root / "最终稿.DOCX").read_bytes(), b"docx")

    def test_reserved_roots_and_descendants_reject_all_generic_operations(self):
        (self.root / "普通" / "待处理.pdf").parent.mkdir()
        (self.root / "普通" / "待处理.pdf").write_bytes(b"pdf")
        (self.root / "目标").mkdir()

        for reserved in ("_assets", "_handouts", "_backups"):
            protected = self.root / reserved / "内部"
            protected.mkdir(parents=True)
            (protected / "资料.pdf").write_bytes(b"protected")
            operations = (
                lambda: library_ops.create_folder(self.root, reserved, "新文件夹"),
                lambda: library_ops.create_markdown(
                    self.root, f"{reserved}/内部", "新资料"),
                lambda: library_ops.rename_entry(
                    self.root, f"{reserved}/内部/资料.pdf", "改名"),
                lambda: library_ops.move_entry(
                    self.root, f"{reserved}/内部/资料.pdf", "目标"),
                lambda: library_ops.copy_entry(
                    self.root, f"{reserved}/内部/资料.pdf", "目标"),
                lambda: library_ops.move_entry(
                    self.root, "普通/待处理.pdf", reserved),
                lambda: library_ops.copy_entry(
                    self.root, "普通/待处理.pdf", reserved),
            )
            for operation in operations:
                with self.subTest(reserved=reserved, operation=operation):
                    with self.assertRaises(library_ops.LibraryOperationError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "reserved_path")

            with self.assertRaises(library_ops.LibraryOperationError) as caught:
                library_ops.rename_entry(self.root, "普通", reserved.upper())
            self.assertEqual(caught.exception.code, "reserved_path")
            self.assertEqual((protected / "资料.pdf").read_bytes(), b"protected")
            self.assertEqual((self.root / "普通" / "待处理.pdf").read_bytes(), b"pdf")

    def test_move_and_copy_supported_files(self):
        (self.root / "来源").mkdir()
        (self.root / "目标").mkdir()
        (self.root / "来源" / "讲义.docx").write_bytes(b"docx")
        (self.root / "来源" / "配图.png").write_bytes(b"png")

        copied = library_ops.copy_entry(self.root, "来源/讲义.docx", "目标")
        moved = library_ops.move_entry(self.root, "来源/配图.png", "目标")

        self.assertTrue(copied.copied)
        self.assertTrue((self.root / "来源" / "讲义.docx").exists())
        self.assertEqual((self.root / "目标" / "讲义.docx").read_bytes(), b"docx")
        self.assertFalse((self.root / "来源" / "配图.png").exists())
        self.assertEqual((self.root / "目标" / "配图.png").read_bytes(), b"png")

    def test_folder_copy_and_descendant_target(self):
        (self.root / "章节" / "子目录").mkdir(parents=True)
        (self.root / "章节" / "题目.md").write_text("题目", encoding="utf-8")
        (self.root / "归档").mkdir()

        copied = library_ops.copy_entry(self.root, "章节", "归档")
        self.assertEqual(copied.path, "归档/章节")
        self.assertTrue((self.root / "章节" / "题目.md").exists())
        self.assertTrue((self.root / "归档" / "章节" / "题目.md").exists())

        with self.assertRaises(library_ops.LibraryOperationError) as caught:
            library_ops.move_entry(self.root, "章节", "章节/子目录")
        self.assertEqual(caught.exception.code, "descendant_target")
        self.assertTrue((self.root / "章节").is_dir())


if __name__ == "__main__":
    unittest.main()
