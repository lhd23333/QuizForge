import tempfile
import unittest
from pathlib import Path
from unittest import mock

import filestore
import library_ops


class LibraryOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _write_question(path: Path, *, qid: str, order: float,
                        body: str = "题目", **extra) -> None:
        meta = {
            "id": qid,
            "created": "2026-01-01T00:00:00",
            "updated": "2026-01-01T00:00:00",
            "order": order,
            **extra,
        }
        path.write_text(
            filestore._render_raw(meta, body), encoding="utf-8", newline="\n")

    def test_create_folder_and_markdown_normalizes_newlines(self):
        folder = library_ops.create_folder(self.root, "", "资料")
        note = library_ops.create_markdown(
            self.root, folder.path, "例题", "第一行\r\n第二行\r第三行")

        self.assertEqual(folder.path, "资料")
        self.assertEqual(note.path, "资料/例题.md")
        meta, body = filestore._read_raw(self.root / "资料" / "例题.md")
        self.assertEqual(meta["quizforge_kind"], "document")
        self.assertEqual(body.lstrip("\n"), "第一行\n第二行\n第三行")

    def test_create_markdown_preserves_custom_frontmatter_and_body(self):
        note = library_ops.create_markdown(
            self.root,
            "",
            "课堂笔记.md",
            "---\r\ncustom_field: 保留\r\nnested:\r\n  enabled: true\r\n---\r\n\r\n# 正文\r\n内容\r\n",
        )

        meta, body = filestore._read_raw(self.root / note.path)
        self.assertEqual(meta["quizforge_kind"], "document")
        self.assertEqual(meta["custom_field"], "保留")
        self.assertEqual(meta["nested"], {"enabled": True})
        self.assertEqual(body.lstrip("\n"), "# 正文\n内容\n")

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

    def test_markdown_move_preserves_original_bytes_and_identity(self):
        source = self.root / "移动来源"
        target = self.root / "移动目标"
        source.mkdir()
        target.mkdir()
        self._write_question(
            source / "待移动.md", qid="source-id", order=2,
            custom_field="保留字段")
        self._write_question(target / "既有.md", qid="target-id", order=7)
        original_bytes = (source / "待移动.md").read_bytes()
        original_mtime = (source / "待移动.md").stat().st_mtime_ns

        result = library_ops.move_entry(self.root, "移动来源/待移动.md", "移动目标")

        self.assertEqual(result.path, "移动目标/待移动.md")
        self.assertFalse((source / "待移动.md").exists())
        meta, body = filestore._read_raw(target / "待移动.md")
        self.assertEqual(meta["id"], "source-id")
        self.assertEqual(meta["order"], 2)
        self.assertEqual(meta["custom_field"], "保留字段")
        self.assertEqual(body.lstrip("\n"), "题目")
        self.assertEqual((target / "待移动.md").read_bytes(), original_bytes)
        self.assertEqual((target / "待移动.md").stat().st_mtime_ns, original_mtime)

    def test_markdown_copy_refreshes_identity_and_internal_fields(self):
        source = self.root / "复制来源"
        target = self.root / "复制目标"
        source.mkdir()
        target.mkdir()
        original = source / "题卡.md"
        self._write_question(
            original, qid="old-id", order=2,
            body="题干 ![[共享图片.png]]",
            custom_field={"keep": True},
            _quizforge_import_scope="batch:old",
            _quizforge_import_index=3,
            _trash_original_path="旧位置/题卡.md",
            _trash_deleted_at="2026-01-02T00:00:00",
        )
        original_bytes = original.read_bytes()
        self._write_question(target / "既有.md", qid="target-id", order=4)
        self._write_question(
            target / "普通文档.md", qid="document-id", order=999,
            quizforge_kind="document")
        self._write_question(
            target / "讲义.md", qid="handout-id", order=888,
            quizforge_kind="handout")

        result = library_ops.copy_entry(self.root, "复制来源/题卡.md", "复制目标")

        copied_meta, copied_body = filestore._read_raw(target / "题卡.md")
        self.assertTrue(result.copied)
        self.assertNotEqual(copied_meta["id"], "old-id")
        self.assertEqual(copied_meta["order"], 5.0)
        self.assertNotEqual(copied_meta["created"], "2026-01-01T00:00:00")
        self.assertEqual(copied_meta["created"], copied_meta["updated"])
        self.assertEqual(copied_meta["custom_field"], {"keep": True})
        for key in (
                "_quizforge_import_scope", "_quizforge_import_index",
                "_trash_original_path", "_trash_deleted_at"):
            self.assertNotIn(key, copied_meta)
        self.assertEqual(copied_body.lstrip("\n"), "题干 ![[共享图片.png]]")
        self.assertEqual(original.read_bytes(), original_bytes)

    def test_markdown_extension_and_non_mapping_frontmatter_stay_raw(self):
        source = self.root / "普通文档来源"
        target = self.root / "普通文档目标"
        source.mkdir()
        target.mkdir()
        markdown_text = "# 普通资料\n\n不进入题卡索引。\n".encode("utf-8")
        unusual_md = "---\n- list-frontmatter\n---\n\n正文\n".encode("utf-8")
        invalid_md = b"\xff\xfe\x00not-utf8"
        document_md = filestore._render_raw(
            {"quizforge_kind": "document", "id": "document-id", "order": 12},
            "# 文档正文\n",
        ).encode("utf-8")
        handout_md = filestore._render_raw(
            {"quizforge_kind": "handout", "id": "handout-id", "order": 13},
            "# 讲义正文\n",
        ).encode("utf-8")
        (source / "资料.markdown").write_bytes(markdown_text)
        (source / "列表头.md").write_bytes(unusual_md)
        (source / "损坏编码.md").write_bytes(invalid_md)
        (source / "文档.md").write_bytes(document_md)
        (source / "讲义.md").write_bytes(handout_md)

        library_ops.copy_entry(self.root, "普通文档来源/资料.markdown", "普通文档目标")
        library_ops.copy_entry(self.root, "普通文档来源/列表头.md", "普通文档目标")
        library_ops.copy_entry(self.root, "普通文档来源/损坏编码.md", "普通文档目标")
        library_ops.copy_entry(self.root, "普通文档来源/文档.md", "普通文档目标")
        library_ops.copy_entry(self.root, "普通文档来源/讲义.md", "普通文档目标")

        self.assertEqual((target / "资料.markdown").read_bytes(), markdown_text)
        self.assertEqual((target / "列表头.md").read_bytes(), unusual_md)
        self.assertEqual((target / "损坏编码.md").read_bytes(), invalid_md)
        self.assertEqual((target / "文档.md").read_bytes(), document_md)
        self.assertEqual((target / "讲义.md").read_bytes(), handout_md)

    def test_folder_copy_and_descendant_target(self):
        (self.root / "章节" / "子目录").mkdir(parents=True)
        self._write_question(
            self.root / "章节" / "题目.md", qid="folder-old-id", order=1,
            body="题目 ![[章节图.png]]", custom_field="保留")
        (self.root / "章节" / "子目录" / "普通.md").write_text(
            "没有 frontmatter", encoding="utf-8")
        document_bytes = filestore._render_raw(
            {"quizforge_kind": "document", "custom_field": "原样保留"},
            "# 普通文档\n",
        ).encode("utf-8")
        (self.root / "章节" / "子目录" / "文档.md").write_bytes(document_bytes)
        (self.root / "归档").mkdir()

        copied = library_ops.copy_entry(self.root, "章节", "归档")
        self.assertEqual(copied.path, "归档/章节")
        self.assertTrue((self.root / "章节" / "题目.md").exists())
        self.assertTrue((self.root / "归档" / "章节" / "题目.md").exists())
        copied_meta, copied_body = filestore._read_raw(
            self.root / "归档" / "章节" / "题目.md")
        plain_meta, plain_body = filestore._read_raw(
            self.root / "归档" / "章节" / "子目录" / "普通.md")
        self.assertNotEqual(copied_meta["id"], "folder-old-id")
        self.assertNotEqual(plain_meta["id"], "普通")
        self.assertNotEqual(copied_meta["id"], plain_meta["id"])
        self.assertEqual(copied_meta["custom_field"], "保留")
        self.assertEqual(copied_body.lstrip("\n"), "题目 ![[章节图.png]]")
        self.assertEqual(plain_body.lstrip("\n"), "没有 frontmatter")
        self.assertEqual(
            (self.root / "章节" / "子目录" / "普通.md").read_text(
                encoding="utf-8"),
            "没有 frontmatter",
        )
        self.assertEqual(
            (self.root / "归档" / "章节" / "子目录" / "文档.md").read_bytes(),
            document_bytes,
        )

        with self.assertRaises(library_ops.LibraryOperationError) as caught:
            library_ops.move_entry(self.root, "章节", "章节/子目录")
        self.assertEqual(caught.exception.code, "descendant_target")
        self.assertTrue((self.root / "章节").is_dir())

    def test_folder_copy_rejects_junction_before_copytree(self):
        source = self.root / "含联接"
        target = self.root / "联接目标"
        source.mkdir()
        target.mkdir()
        junction = source / "联接"
        junction.mkdir()

        original_check = library_ops.is_link_or_junction

        def fake_check(path: Path) -> bool:
            return path == junction or original_check(path)

        with mock.patch.object(
                library_ops, "is_link_or_junction", side_effect=fake_check):
            with self.assertRaises(library_ops.LibraryOperationError) as caught:
                library_ops.copy_entry(self.root, "含联接", "联接目标")

        self.assertEqual(caught.exception.code, "symlink_rejected")
        self.assertFalse((target / "含联接").exists())


if __name__ == "__main__":
    unittest.main()
