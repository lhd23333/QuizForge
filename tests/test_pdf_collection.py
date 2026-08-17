"""试卷合集拆分回归：全部离线，不调用 OCR/LLM。"""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pypdf import PdfReader, PdfWriter

import pdf_collection


def _pdf_bytes(pages: int, bookmarks: list[tuple[str, int]], *, answer=False) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    root = writer.add_outline_item("试卷合集参考答案" if answer else "试卷合集", 0)
    for title, page in bookmarks:
        item = writer.add_outline_item(title, page, parent=root)
        # 真实题干合集同一首页还有“物理试题”书签，且下面继续挂大题书签；二者都
        # 不能被误判成独立试卷。
        if not answer:
            writer.add_outline_item("物理试题", page, parent=root)
            writer.add_outline_item("一、单项选择题", page, parent=item)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def _plain_pdf_bytes(pages: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class PdfCollectionTests(unittest.TestCase):
    def test_no_bookmarks_uses_distinct_fallback_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.pdf"
            path.write_bytes(_plain_pdf_bytes())
            with self.assertRaises(pdf_collection.NoBookmarksError):
                pdf_collection.discover_parts(path)

    def test_discovers_shallow_paper_bookmarks_and_skips_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "questions.pdf"
            path.write_bytes(_pdf_bytes(6, [("甲中学2026届月考(一)", 1),
                                            ("乙中学2026届月考(一)", 4)]))
            parts = pdf_collection.discover_parts(path)

        self.assertEqual([part.title for part in parts], [
            "甲中学2026届月考(一)", "乙中学2026届月考(一)"])
        self.assertEqual([(part.start, part.end) for part in parts], [(1, 4), (4, 6)])

    def test_rejects_paper_bookmarks_mixed_across_levels(self):
        writer = PdfWriter()
        for _ in range(9):
            writer.add_blank_page(width=595, height=842)
        root = writer.add_outline_item("2026届试卷合集", 0)
        writer.add_outline_item("甲中学2026届月考(一)", 1, parent=root)
        second = writer.add_outline_item("乙中学2026届月考(一)", 4, parent=root)
        writer.add_outline_item("丙中学2026届月考(一)", 7, parent=second)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed-level.pdf"
            with path.open("wb") as stream:
                writer.write(stream)
            with self.assertRaisesRegex(
                    pdf_collection.CollectionSplitError, "书签层级混杂"):
                pdf_collection.discover_parts(path)

    def test_pairs_by_normalized_title_even_when_answer_order_differs(self):
        exams = [
            pdf_collection.PdfPart("甲中学2026届月考（一）物理试题", 1, 3),
            pdf_collection.PdfPart("乙中学2026届月考(二)", 3, 5),
        ]
        answers = [
            pdf_collection.PdfPart("乙中学2026届月考（二）参考答案", 4, 7),
            pdf_collection.PdfPart("甲中学2026届月考(一)答案解析", 1, 4),
        ]
        pairs = pdf_collection.pair_parts(exams, answers)
        self.assertEqual([answer.title for _, answer in pairs], [
            "甲中学2026届月考(一)答案解析", "乙中学2026届月考（二）参考答案"])

    def test_rejects_unmatched_answer_instead_of_pairing_by_position(self):
        exams = [
            pdf_collection.PdfPart("甲中学2026届月考(一)", 1, 3),
            pdf_collection.PdfPart("乙中学2026届月考(一)", 3, 5),
        ]
        answers = [
            pdf_collection.PdfPart("甲中学2026届月考(一)参考答案", 1, 4),
            pdf_collection.PdfPart("丙中学2026届月考(一)参考答案", 4, 7),
        ]
        with self.assertRaisesRegex(pdf_collection.CollectionSplitError, "缺答案.*无题干"):
            pdf_collection.pair_parts(exams, answers)

    def test_splits_each_range_to_an_independent_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam = root / "exam.pdf"
            answer = root / "answer.pdf"
            exam.write_bytes(_pdf_bytes(6, [("甲中学2026届月考(一)", 1),
                                            ("乙中学2026届月考(一)", 4)]))
            answer.write_bytes(_pdf_bytes(
                8, [("甲中学2026届月考（一）参考答案", 1),
                    ("乙中学2026届月考(一)参考答案", 5)], answer=True))

            pairs = pdf_collection.split_collection_pair(exam, answer, root / "out")
            page_counts = [
                (len(PdfReader(str(pair.exam_path)).pages),
                 len(PdfReader(str(pair.solution_path)).pages))
                for pair in pairs
            ]

        self.assertEqual([pair.title for pair in pairs], [
            "甲中学2026届月考(一)", "乙中学2026届月考(一)"])
        self.assertEqual(page_counts, [(3, 4), (2, 3)])

    def test_batch_route_expands_one_collection_card_without_starting_ocr(self):
        import app as quiz_app

        exam = _pdf_bytes(6, [("甲中学2026届月考(一)", 1),
                              ("乙中学2026届月考(一)", 4)])
        answer = _pdf_bytes(8, [("甲中学2026届月考(一)参考答案", 1),
                                ("乙中学2026届月考(一)参考答案", 5)], answer=True)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(quiz_app.config, "BATCH_UPLOAD_DIR", Path(tmp)), \
                mock.patch.object(quiz_app, "_persist_job"), \
                mock.patch.object(quiz_app, "_persist_batch"), \
                mock.patch.object(quiz_app.threading, "Thread") as thread_cls:
            response = quiz_app.app.test_client().post(
                "/batch-convert/create",
                data={
                    "_csrf_token": quiz_app._WRITE_TOKEN,
                    "ocr_backend": "doc2x",
                    "groups[0][collection_mode]": "1",
                    "groups[0][file]": (io.BytesIO(exam), "题干合集.pdf"),
                    "groups[0][solution_file]": (io.BytesIO(answer), "答案合集.pdf"),
                }, content_type="multipart/form-data")

            self.assertEqual(200, response.status_code, response.get_data(as_text=True))
            payload = response.get_json()
            self.assertEqual(2, payload["count"])
            batch_id = payload["batch_id"]
            try:
                groups = quiz_app._batch_jobs[batch_id]["groups"]
                self.assertEqual([group["gid"] for group in groups], [0, 1])
                self.assertEqual([group["filename"] for group in groups], [
                    "甲中学2026届月考(一).pdf", "乙中学2026届月考(一).pdf"])
                self.assertTrue(all(group["collection_mode"] for group in groups))
                self.assertTrue(all(group["include_solution"] for group in groups))
                self.assertTrue(all(group["ocr_backend"] == "doc2x" for group in groups))
                self.assertEqual([len(group["cleanup_paths"]) for group in groups], [4, 2])
                self.assertEqual([
                    len(PdfReader(group["file_path"]).pages) for group in groups], [3, 2])
                thread_cls.return_value.start.assert_called_once()
            finally:
                batch = quiz_app._batch_jobs.pop(batch_id, None)
                for group in (batch or {}).get("groups", []):
                    quiz_app._jobs.pop(group["job_id"], None)

    def test_no_bookmark_route_registers_one_persistent_ocr_parent(self):
        import app as quiz_app

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(quiz_app.config, "BATCH_UPLOAD_DIR", Path(tmp)), \
                mock.patch.object(quiz_app, "_persist_job"), \
                mock.patch.object(quiz_app, "_persist_batch"), \
                mock.patch.object(quiz_app.threading, "Thread") as thread_cls:
            response = quiz_app.app.test_client().post(
                "/batch-convert/create",
                data={
                    "_csrf_token": quiz_app._WRITE_TOKEN,
                    "ocr_backend": "mineru",
                    "groups[0][collection_mode]": "1",
                    "groups[0][file]": (
                        io.BytesIO(_plain_pdf_bytes()), "无书签题干合集.pdf"),
                    "groups[0][solution_file]": (
                        io.BytesIO(_plain_pdf_bytes()), "无书签解析合集.pdf"),
                }, content_type="multipart/form-data")

            self.assertEqual(200, response.status_code, response.get_data(as_text=True))
            payload = response.get_json()
            self.assertEqual(1, payload["count"])
            batch_id = payload["batch_id"]
            try:
                group = quiz_app._batch_jobs[batch_id]["groups"][0]
                self.assertEqual("ocr_structure", group["collection_strategy"])
                self.assertFalse(group["collection_unit"])
                self.assertEqual(2, len(group["cleanup_paths"]))
                thread_cls.return_value.start.assert_called_once()
            finally:
                batch = quiz_app._batch_jobs.pop(batch_id, None)
                for group in (batch or {}).get("groups", []):
                    quiz_app._jobs.pop(group["job_id"], None)

    def test_ocr_parent_expands_to_independent_raw_children(self):
        import app as quiz_app

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(quiz_app, "_persist_job"), \
                mock.patch.object(quiz_app, "_persist_batch") as persist_batch, \
                mock.patch.object(quiz_app.task_store, "delete"), \
                mock.patch.object(
                    quiz_app.converter, "recognize_collection_units") as recognize:
            root = Path(tmp)
            exam = root / "exam.pdf"
            solution = root / "solution.pdf"
            exam.write_bytes(_plain_pdf_bytes())
            solution.write_bytes(_plain_pdf_bytes())
            units = []
            for index, title in enumerate(("训练一：运动学", "训练二：力学"), 1):
                workspace = root / f"collection_unit_{index}"
                workspace.mkdir()
                raw_path = workspace / f"collection_unit_{index}_raw.md"
                raw_path.write_text(f"# {title}\n\n1．题目\n\n2．题目", encoding="utf-8")
                units.append({
                    "title": title,
                    "raw_path": str(raw_path),
                    "workspace_dir": str(workspace),
                    "include_solution": True,
                    "ocr_meta": {"unit": index},
                })
            recognize.return_value = units

            batch_id = "batch-expand-test"
            parent_job_id = "parent-job"
            parent = {
                "gid": 4, "job_id": parent_job_id,
                "file_path": str(exam), "solution_path": str(solution),
                "include_solution": True, "only_numbers": None,
                "filename": "合集.pdf", "engine": "block",
                "ocr_backend": "mineru", "block_mode": "manual",
                "num_template": "", "cleanup_paths": [str(exam), str(solution)],
                "cleanup_dirs": [], "collection_mode": True,
                "collection_strategy": "ocr_structure", "collection_unit": False,
                "status": "pending", "md": None, "error": None,
                "pending": None, "note": "", "reviewed": None,
                "imported_count": 0,
            }
            quiz_app._jobs[parent_job_id] = {
                "status": "pending", "md": None, "error": None,
                "filename": "合集.pdf", "path": str(exam),
                "ocr_backend": "mineru",
            }
            quiz_app._batch_jobs[batch_id] = {
                "status": "converting", "groups": [parent],
                "running": 0, "cancelled": False,
            }
            try:
                quiz_app._expand_collection_parent(batch_id, parent)

                groups = quiz_app._batch_jobs[batch_id]["groups"]
                self.assertEqual(2, len(groups))
                self.assertEqual([4, 5], [group["gid"] for group in groups])
                self.assertTrue(all(group["collection_unit"] for group in groups))
                self.assertEqual([unit["raw_path"] for unit in units],
                                 [group["collection_raw_path"] for group in groups])
                self.assertEqual(2, len(groups[0]["cleanup_paths"]))
                self.assertEqual([], groups[1]["cleanup_paths"])
                self.assertEqual(
                    [True, False],
                    [group["owns_collection_originals"] for group in groups])
                self.assertEqual(
                    ["合集.pdf", "合集.pdf"],
                    [group["collection_source_filename"] for group in groups])
                # 子组字段跟 batch 一起持久化；重启恢复只读这个快照，不会重新
                # 按当前执行顺序挑一次“第一个完成的组”。
                self.assertTrue(any(
                    call.args[0] == batch_id
                    and [g.get("owns_collection_originals")
                         for g in call.args[1]["groups"]] == [True, False]
                    for call in persist_batch.call_args_list))
                self.assertNotIn(parent_job_id, quiz_app._jobs)
                self.assertTrue(all(group["job_id"] in quiz_app._jobs
                                    for group in groups))
                recognize.assert_called_once()
            finally:
                batch = quiz_app._batch_jobs.pop(batch_id, None)
                for group in (batch or {}).get("groups", []):
                    quiz_app._jobs.pop(group["job_id"], None)
                quiz_app._jobs.pop(parent_job_id, None)

    def test_structure_collection_auto_import_stores_whole_pdfs_only_once(self):
        import app as quiz_app

        groups = []
        for index in range(2):
            groups.append({
                "gid": index,
                "filename": f"专题{index + 1}.pdf",
                "file_path": "整集题干.pdf",
                "solution_path": "整集解析.pdf",
                "collection_strategy": "ocr_structure",
                "collection_unit": True,
                "owns_collection_originals": index == 0,
                "collection_source_filename": "复习精练上册.pdf",
                "md": f"第{index + 1}组转换结果",
                "include_solution": True,
                "only_numbers": None,
                "status": "done",
                "reviewed": None,
                "imported_count": 0,
                "attempt": 0,
            })
        batch_id = "structure-original-owner"
        batch = {
            "status": "done", "groups": groups, "files_cleaned": False,
            "auto_keep_original": True,
        }
        preview = [{
            "body": "题目", "solution": "解析", "type": "解答题",
            "dup": None, "number": 1,
        }]
        quiz_app._batch_jobs[batch_id] = batch
        try:
            with mock.patch.object(
                    quiz_app, "_build_import_preview",
                    return_value=(preview, [], None)), \
                    mock.patch.object(
                        quiz_app, "_auto_import_folder",
                        side_effect=["专题1", "专题2"]), \
                    mock.patch.object(
                        quiz_app.filestore, "create_questions_batch",
                        return_value=["qid"]), \
                    mock.patch.object(quiz_app, "_store_papers") as store, \
                    mock.patch.object(quiz_app, "_persist_batch"), \
                    mock.patch.object(quiz_app, "_maybe_finish_batch"):
                for group in groups:
                    quiz_app._auto_import_after_convert(
                        batch_id, group, attempt=0,
                        md_snapshot=group["md"])
        finally:
            quiz_app._batch_jobs.pop(batch_id, None)

        store.assert_called_once_with("专题1", [
            ("整集题干.pdf", "复习精练上册.pdf", "exam"),
            ("整集解析.pdf", "复习精练上册（答案）.pdf", "solution"),
        ])

    def test_structure_collection_respects_unchecked_keep_original(self):
        import app as quiz_app

        group = {
            "gid": 0, "filename": "专题1.pdf",
            "file_path": "整集题干.pdf", "solution_path": "整集解析.pdf",
            "collection_strategy": "ocr_structure", "collection_unit": True,
            "owns_collection_originals": True,
            "md": "转换结果", "include_solution": True,
            "only_numbers": None, "status": "done", "reviewed": None,
            "imported_count": 0, "attempt": 0,
        }
        batch_id = "structure-original-disabled"
        batch = {
            "status": "done", "groups": [group], "files_cleaned": False,
            "auto_keep_original": False,
        }
        preview = [{
            "body": "题目", "solution": "解析", "type": "解答题",
            "dup": None, "number": 1,
        }]
        quiz_app._batch_jobs[batch_id] = batch
        try:
            with mock.patch.object(
                    quiz_app, "_build_import_preview",
                    return_value=(preview, [], None)), \
                    mock.patch.object(
                        quiz_app, "_auto_import_folder",
                        return_value="专题1"), \
                    mock.patch.object(
                        quiz_app.filestore, "create_questions_batch",
                        return_value=["qid"]), \
                    mock.patch.object(quiz_app, "_store_papers") as store, \
                    mock.patch.object(quiz_app, "_persist_batch"), \
                    mock.patch.object(quiz_app, "_maybe_finish_batch"):
                quiz_app._auto_import_after_convert(
                    batch_id, group, attempt=0, md_snapshot=group["md"])
        finally:
            quiz_app._batch_jobs.pop(batch_id, None)

        store.assert_not_called()

    def test_failed_ocr_parent_reconvert_returns_to_pending_expansion(self):
        import app as quiz_app

        batch_id = "batch-parent-reconvert"
        job_id = "parent-reconvert-job"
        parent = {
            "gid": 0, "job_id": job_id,
            "file_path": "exam.pdf", "solution_path": "solution.pdf",
            "include_solution": True, "only_numbers": None,
            "filename": "合集.pdf", "engine": "block",
            "ocr_backend": "mineru", "block_mode": "no_ai",
            "num_template": "", "cleanup_paths": [], "cleanup_dirs": [],
            "collection_mode": True,
            "collection_strategy": "ocr_structure", "collection_unit": False,
            "status": "error", "md": None, "error": "结构未确认",
            "pending": None, "note": "", "reviewed": None,
            "imported_count": 0, "attempt": 0, "in_flight": False,
            "cancelled": False,
        }
        quiz_app._batch_jobs[batch_id] = {
            "status": "done", "groups": [parent], "running": 0,
            "cancelled": False, "files_cleaned": False,
        }
        quiz_app._jobs[job_id] = {
            "status": "error", "md": None, "error": "结构未确认",
        }
        try:
            with mock.patch.object(quiz_app, "_persist_job"), \
                    mock.patch.object(quiz_app, "_persist_batch"), \
                    mock.patch.object(quiz_app.threading, "Thread") as thread_cls:
                response = quiz_app.app.test_client().post(
                    f"/batch/{batch_id}/group/0/reconvert",
                    data={"_csrf_token": quiz_app._WRITE_TOKEN})

            self.assertEqual(302, response.status_code)
            self.assertEqual("pending", parent["status"])
            self.assertEqual(1, parent["attempt"])
            self.assertIs(quiz_app._convert_batch_worker,
                          thread_cls.call_args.kwargs["target"])
            thread_cls.return_value.start.assert_called_once()
        finally:
            quiz_app._batch_jobs.pop(batch_id, None)
            quiz_app._jobs.pop(job_id, None)

    def test_failed_collection_can_edit_cached_markdown_and_retry_as_single(self):
        import app as quiz_app

        batch_id = "batch-source-review"
        job_id = "source-review-job"
        parent = {
            "gid": 0, "job_id": job_id,
            "file_path": "exam.pdf", "solution_path": None,
            "include_solution": False, "only_numbers": None,
            "filename": "专题练习.pdf", "engine": "block",
            "ocr_backend": "doc2x", "block_mode": "no_ai",
            "num_template": "", "cleanup_paths": [],
            "cleanup_dirs": ["cache-exam"],
            "collection_cache_dirs": ["cache-exam"],
            "collection_mode": True,
            "collection_strategy": "ocr_structure", "collection_unit": False,
            "status": "error", "md": None, "error": "结构未确认",
            "pending": None, "note": "", "reviewed": None,
            "imported_count": 0, "attempt": 0, "in_flight": False,
            "cancelled": False,
        }
        quiz_app._batch_jobs[batch_id] = {
            "status": "done", "groups": [parent], "running": 0,
            "cancelled": False, "files_cleaned": False,
        }
        quiz_app._jobs[job_id] = {
            "status": "error", "md": None, "error": "结构未确认",
            "path": "exam.pdf", "solution_path": None,
        }
        snapshot = {
            "exam_markdown": "1．初稿", "solution_markdown": "",
            "ocr_meta": {"exam": {}, "solution": None},
            "revision": "a" * 64,
        }
        unit = {
            "raw_path": "unit/raw.md", "workspace_dir": "unit",
            "ocr_meta": snapshot["ocr_meta"], "include_solution": False,
        }
        try:
            with mock.patch.object(
                    quiz_app.converter, "collection_cache_is_editable",
                    return_value=True), \
                    mock.patch.object(
                        quiz_app.converter, "collection_cache_snapshot",
                        return_value=snapshot), \
                    mock.patch.object(
                        quiz_app.converter, "update_collection_cache_markdown",
                        return_value=snapshot) as update, \
                    mock.patch.object(
                        quiz_app.converter, "materialize_collection_cache_as_unit",
                        return_value=unit), \
                    mock.patch.object(quiz_app, "_persist_job"), \
                    mock.patch.object(quiz_app, "_persist_batch"), \
                    mock.patch.object(quiz_app.threading, "Thread") as thread_cls:
                client = quiz_app.app.test_client()
                page = client.get(
                    f"/batch/{batch_id}/group/0/source")
                response = client.post(
                    f"/batch/{batch_id}/group/0/source",
                    data={
                        "exam_markdown": "1．调整后",
                        "revision": snapshot["revision"],
                        "retry_mode": "single",
                    },
                    headers={"X-CSRF-Token": quiz_app._WRITE_TOKEN})

            self.assertEqual(200, page.status_code)
            self.assertIn("调整识别原文", page.get_data(as_text=True))
            self.assertEqual(302, response.status_code)
            update.assert_called_once()
            self.assertTrue(parent["collection_unit"])
            self.assertEqual("unit/raw.md", parent["collection_raw_path"])
            self.assertEqual("pending", parent["status"])
            self.assertEqual(1, parent["attempt"])
            self.assertIs(
                quiz_app._convert_batch_worker,
                thread_cls.call_args.kwargs["target"])
            thread_cls.return_value.start.assert_called_once()
        finally:
            quiz_app._batch_jobs.pop(batch_id, None)
            quiz_app._jobs.pop(job_id, None)

    def test_ocr_parent_never_falls_into_normal_two_file_conversion(self):
        import app as quiz_app

        batch_id = "batch-parent-guard"
        job_id = "parent-guard-job"
        parent = {
            "gid": 0, "job_id": job_id,
            "file_path": "whole-exam.pdf", "solution_path": "whole-solution.pdf",
            "collection_strategy": "ocr_structure", "collection_unit": False,
            "status": "pending", "attempt": 0,
        }
        quiz_app._batch_jobs[batch_id] = {
            "status": "converting", "groups": [parent],
        }
        quiz_app._jobs[job_id] = {"status": "pending"}
        try:
            with mock.patch.object(quiz_app, "_persist_batch"), \
                    mock.patch.object(quiz_app.converter,
                                      "convert_exam_and_solution") as convert:
                quiz_app._convert_one_group(batch_id, parent)

            self.assertEqual("pending", parent["status"])
            convert.assert_not_called()
        finally:
            quiz_app._batch_jobs.pop(batch_id, None)
            quiz_app._jobs.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
