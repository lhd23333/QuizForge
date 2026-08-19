"""资料库文件索引与写接口的定向回归。"""

from __future__ import annotations

import importlib
import base64
import io
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import config


_tmp = None
_old_config = {}
app_module = None


def setUpModule():
    global _tmp, _old_config, app_module
    _tmp = tempfile.TemporaryDirectory()
    root = Path(_tmp.name)
    paths = {
        "BANK_DIR": root / "bank",
        "TRASH_DIR": root / "bank" / ".trash",
        "ASSETS_DIR": root / "bank" / "_assets",
        "HANDOUTS_DIR": root / "bank" / "_handouts",
        "IMAGES_DIR": root / "bank" / "_assets",
        "PROVIDERS_PATH": root / "providers.json",
        "SELECTIONS_PATH": root / "selections.json",
        "TASKS_PATH": root / "tasks.json",
        "UPLOAD_DIR": root / "uploads",
        "BATCH_UPLOAD_DIR": root / "uploads" / "batch",
        "HISTORY_DIR": root / "history",
        "OUTPUT_DIR": root / "output",
    }
    _old_config = {name: getattr(config, name) for name in paths}
    for name, value in paths.items():
        setattr(config, name, value)
    config.BANK_DIR.mkdir(parents=True)
    app_module = importlib.import_module("app")
    app_module.filestore.invalidate_scan_cache(folder_structure=True)


def tearDownModule():
    for name, value in _old_config.items():
        setattr(config, name, value)
    app_module.filestore.invalidate_scan_cache(folder_structure=True)
    _tmp.cleanup()


class LibraryRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.headers = {
            "X-CSRF-Token": app_module._WRITE_TOKEN,
            "Content-Type": "application/json",
        }

    def test_lists_doc_and_docx_as_word(self):
        folder = config.BANK_DIR / "Word 索引"
        folder.mkdir()
        (folder / "旧讲义.doc").write_bytes(b"doc")
        (folder / "新讲义.DOCX").write_bytes(b"docx")
        (folder / "忽略.odt").write_bytes(b"odt")

        response = self.client.get(
            "/api/library/children", query_string={"path": "Word 索引"})

        self.assertEqual(response.status_code, 200)
        entries = {item["name"]: item["kind"]
                   for item in response.get_json()["entries"]}
        self.assertEqual(entries, {"旧讲义.doc": "word", "新讲义.DOCX": "word"})

    def test_junction_is_hidden_and_direct_path_is_rejected(self):
        junction = config.BANK_DIR / "模拟联接"
        junction.mkdir()
        (junction / "外部.pdf").write_bytes(b"pdf")
        original_check = app_module.library_ops.is_link_or_junction

        def fake_check(path: Path) -> bool:
            return path == junction or original_check(path)

        with mock.patch.object(
                app_module.library_ops, "is_link_or_junction",
                side_effect=fake_check):
            root_response = self.client.get("/api/library/children")
            direct_response = self.client.get(
                "/api/library/children", query_string={"path": "模拟联接"})

        self.assertEqual(root_response.status_code, 200)
        self.assertNotIn(
            "模拟联接",
            {item["name"] for item in root_response.get_json()["entries"]},
        )
        self.assertEqual(direct_response.status_code, 404)

    def test_create_folder_and_rename_file(self):
        created = self.client.post(
            "/api/library/folder",
            json={"parent": "", "name": "课程资料"},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json()["entry"]["path"], "课程资料")
        (config.BANK_DIR / "课程资料" / "原讲义.docx").write_bytes(b"docx")

        renamed = self.client.post(
            "/api/library/rename",
            json={"path": "课程资料/原讲义.docx", "name": "高数讲义"},
            headers=self.headers,
        )

        self.assertEqual(renamed.status_code, 200)
        data = renamed.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["entry"]["old_path"], "课程资料/原讲义.docx")
        self.assertEqual(data["entry"]["path"], "课程资料/高数讲义.docx")
        self.assertEqual(data["entry"]["kind"], "word")
        self.assertEqual(
            (config.BANK_DIR / "课程资料" / "高数讲义.docx").read_bytes(),
            b"docx",
        )

    def test_create_markdown_accepts_optional_text(self):
        parent = config.BANK_DIR / "Markdown 新建"
        parent.mkdir()
        created = self.client.post(
            "/api/library/markdown",
            json={"parent": "Markdown 新建", "name": "课堂笔记",
                  "text": "# 第一节\r\n\r\n正文"},
            headers=self.headers,
        )
        empty = self.client.post(
            "/api/library/markdown",
            json={"parent": "Markdown 新建", "name": "空白笔记.md"},
            headers=self.headers,
        )

        self.assertEqual(created.status_code, 201)
        created_entry = created.get_json()["entry"]
        self.assertEqual(created_entry["path"], "Markdown 新建/课堂笔记.md")
        self.assertEqual(created_entry["kind"], "markdown")
        self.assertEqual(empty.status_code, 201)
        self.assertEqual(empty.get_json()["entry"]["path"],
                         "Markdown 新建/空白笔记.md")
        created_text = (parent / "课堂笔记.md").read_text(encoding="utf-8")
        empty_text = (parent / "空白笔记.md").read_text(encoding="utf-8")
        self.assertIn("quizforge_kind: document", created_text)
        self.assertIn("# 第一节\n\n正文", created_text)
        self.assertIn("quizforge_kind: document", empty_text)

    def test_create_markdown_rejects_history_and_reserved_roots(self):
        for reserved in ("_assets", "_handouts", "_backups"):
            (config.BANK_DIR / reserved).mkdir(exist_ok=True)
        for parent in (".quizforge-history", "_assets", "_handouts", "_backups"):
            with self.subTest(parent=parent):
                response = self.client.post(
                    "/api/library/markdown",
                    json={"parent": parent, "name": "不得创建"},
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(response.get_json()["code"],
                              {"read_only", "reserved_path"})
                self.assertFalse((config.BANK_DIR / parent / "不得创建.md").exists())

    def test_markdown_creation_ui_contract(self):
        page = self.client.get("/library").get_data(as_text=True)
        script = (config.BASE_DIR / "static" / "js" / "library-tabs.js").read_text(
            encoding="utf-8")

        self.assertIn('id="library-new-markdown"', page)
        self.assertIn('aria-label="新建顶层 Markdown 文档"', page)
        self.assertIn("function createLibraryMarkdown(parent = '')", script)
        self.assertIn("fetchJson('/api/library/markdown'", script)
        self.assertIn("['new-markdown', '新建 Markdown']", script)
        self.assertIn("if (host) await loadChildren(host, parent, 0)", script)
        self.assertIn("openDocument({...data.entry", script)
        self.assertIn(
            "newMarkdownButton?.addEventListener('click', () => createLibraryMarkdown(''))",
            script,
        )

    def test_library_drag_and_card_ui_contract(self):
        script = (config.BASE_DIR / "static" / "js" / "library-tabs.js").read_text(
            encoding="utf-8")
        self.assertIn('button.draggable = true', script)
        self.assertIn("/api/library/transfer", script)
        self.assertIn("event.shiftKey", script)
        self.assertIn("/api/library/card-task", script)
        self.assertIn("split_mode: mode.value", script)
        self.assertIn("event.ctrlKey || event.metaKey", script)
        self.assertIn("openLibraryCardDialog(tab)", script)

    def test_transfer_moves_and_copies_entries(self):
        source = config.BANK_DIR / "转移来源"
        target = config.BANK_DIR / "转移目标"
        source.mkdir()
        target.mkdir()
        (source / "讲义.pdf").write_bytes(b"pdf")
        (source / "配图.png").write_bytes(b"png")

        copied = self.client.post(
            "/api/library/transfer",
            json={"path": "转移来源/讲义.pdf", "target": "转移目标",
                  "mode": "copy"},
            headers=self.headers,
        )
        moved = self.client.post(
            "/api/library/transfer",
            json={"path": "转移来源/配图.png", "target": "转移目标",
                  "mode": "move"},
            headers=self.headers,
        )

        self.assertEqual(copied.status_code, 200)
        self.assertEqual(copied.get_json()["entry"], {
            "path": "转移目标/讲义.pdf",
            "old_path": "转移来源/讲义.pdf",
            "kind": "pdf",
            "copied": True,
        })
        self.assertEqual(moved.status_code, 200)
        self.assertFalse((source / "配图.png").exists())
        self.assertEqual((target / "配图.png").read_bytes(), b"png")

    def test_card_task_accepts_markdown_and_defaults_to_temporary_collection(self):
        with mock.patch.object(
                app_module, "_queue_library_task",
                return_value={"task_id": "card-task"}) as queue:
            response = self.client.post(
                "/api/library/card-task",
                json={
                    "mode": "markdown",
                    "split_mode": "single",
                    "text": "1. 计算 $1+1$。",
                    "source": "课堂练习",
                    "include_solution": False,
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 202)
        queue.assert_called_once()
        operation, params = queue.call_args.args
        self.assertEqual(operation, "card_markdown")
        self.assertEqual(params["split_mode"], "single")
        self.assertFalse(params["include_solution"])
        self.assertEqual(params["target_collection"], "临时卡片")
        self.assertTrue((config.BANK_DIR / "临时卡片").is_dir())

    def test_card_task_retry_uses_card_validation(self):
        task_id = "failed-card-task"
        app_module._library_tasks[task_id] = {
            "task_id": task_id,
            "operation": "card_markdown",
            "params": {
                "text": "1. 计算 $1+1$。",
                "source": "练习",
                "target_collection": "",
                "split_mode": "multi",
                "include_solution": True,
                "boundary_mode": "auto",
            },
            "status": "error",
        }
        try:
            with mock.patch.object(
                    app_module, "_queue_library_task",
                    return_value={"task_id": "retry-task"}) as queue:
                response = self.client.post(
                    f"/api/library/task/{task_id}/retry",
                    headers=self.headers,
                )
            self.assertEqual(response.status_code, 202)
            queue.assert_called_once()
            operation, _params = queue.call_args.args
            self.assertEqual(operation, "card_markdown")
        finally:
            app_module._library_tasks.pop(task_id, None)

    def test_card_preview_respects_boundary_mode_and_single_mode(self):
        raw = "1. First question\n\n3. Third question"
        with self.assertRaises(app_module.library_ops.LibraryOperationError) as ctx:
            app_module._library_card_preview(
                raw, {"split_mode": "multi", "boundary_mode": "auto",
                      "include_solution": False})
        self.assertEqual(ctx.exception.code, "missing_question_numbers")
        cards = app_module._library_card_preview(
            raw, {"split_mode": "multi", "boundary_mode": "whitelist",
                  "include_solution": False})
        self.assertEqual(len(cards), 2)
        with self.assertRaises(app_module.library_ops.LibraryOperationError) as ctx:
            app_module._library_card_preview(
                "1. First\n\n2. Second",
                {"split_mode": "single", "boundary_mode": "auto",
                 "include_solution": False})
        self.assertEqual(ctx.exception.code, "multiple_cards_in_single_mode")

    def test_library_cards_use_temporary_title_and_idempotent_scope(self):
        folder = "临时卡片测试"
        chosen = [{
            "body": "A short question", "solution": "A short answer",
            "type": "填空题", "number": 9, "img_split": None,
            "img_layouts": [], "sol_img_split": None, "sol_img_layouts": [],
        }]
        params = {"target_collection": folder, "split_mode": "single",
                  "idempotency_scope": "library-test-temporary-1"}
        first = app_module._create_library_cards(chosen, params, "课堂来源")
        second = app_module._create_library_cards(chosen, params, "课堂来源")
        self.assertEqual(first, second)
        files = sorted((config.BANK_DIR / folder).glob("*.md"))
        self.assertEqual([path.stem for path in files], ["临时卡1"])
        self.assertIn("source: 课堂来源", files[0].read_text(encoding="utf-8"))

    def test_card_capture_multipart_contract(self):
        # 1x1 PNG，足够走真实图片格式校验而不引入测试资源文件。
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "YAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
        with mock.patch.object(
                app_module, "_queue_library_task",
                return_value={"task_id": "capture-task"}) as queue:
            response = self.client.post(
                "/api/library/card-capture",
                data={
                    "split_mode": "single",
                    "boundary_mode": "whitelist",
                    "include_solution": "false",
                    "stem_image": (io.BytesIO(png), "stem.png"),
                    "solution_image": (io.BytesIO(png), "solution.png"),
                },
                content_type="multipart/form-data",
                headers={"X-CSRF-Token": app_module._WRITE_TOKEN},
            )
        self.assertEqual(response.status_code, 202)
        queue.assert_called_once()
        operation, params = queue.call_args.args
        self.assertEqual(operation, "card_capture")
        self.assertEqual(params["stem_source"]["kind"], "upload")
        self.assertEqual(params["solution_source"]["kind"], "upload")
        for spec in (params["stem_source"], params["solution_source"]):
            (config.BATCH_UPLOAD_DIR / spec["name"]).unlink(missing_ok=True)

    def test_card_capture_accepts_desktop_pdf_region_tokens(self):
        config.BATCH_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        stem_name = f"library-card-{'1' * 32}.png"
        solution_name = f"library-card-{'2' * 32}.png"
        for name in (stem_name, solution_name):
            (config.BATCH_UPLOAD_DIR / name).write_bytes(b"png")
        try:
            with mock.patch.object(
                    app_module, "_queue_library_task",
                    return_value={"task_id": "capture-task"}) as queue:
                response = self.client.post(
                    "/api/library/card-capture",
                    data={
                        "split_mode": "single",
                        "boundary_mode": "whitelist",
                        "include_solution": "true",
                        "stem_capture_name": stem_name,
                        "solution_capture_name": solution_name,
                    },
                    headers={"X-CSRF-Token": app_module._WRITE_TOKEN},
                )

            self.assertEqual(response.status_code, 202)
            operation, params = queue.call_args.args
            self.assertEqual(operation, "card_capture")
            self.assertEqual(params["stem_source"]["name"], stem_name)
            self.assertEqual(params["solution_source"]["name"], solution_name)
        finally:
            for name in (stem_name, solution_name):
                (config.BATCH_UPLOAD_DIR / name).unlink(missing_ok=True)

    def test_invalid_capture_pair_does_not_delete_existing_stem_token(self):
        config.BATCH_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        stem_name = f"library-card-{'3' * 32}.png"
        missing_solution = f"library-card-{'4' * 32}.png"
        stem = config.BATCH_UPLOAD_DIR / stem_name
        stem.write_bytes(b"png")
        try:
            response = self.client.post(
                "/api/library/card-capture",
                data={
                    "split_mode": "single",
                    "boundary_mode": "auto",
                    "stem_capture_name": stem_name,
                    "solution_capture_name": missing_solution,
                },
                headers={"X-CSRF-Token": app_module._WRITE_TOKEN},
            )

            self.assertEqual(response.status_code, 404)
            self.assertTrue(stem.is_file())
        finally:
            stem.unlink(missing_ok=True)

    def test_cleanup_protects_nested_card_capture_uploads(self):
        import cleanup_output
        name = "library-card-active.png"
        expected = config.BATCH_UPLOAD_DIR / name
        payload = {"params": {
            "stem_source": {"kind": "upload", "name": name},
            "solution_source": {"kind": "upload", "name": "../escape.png"},
        }}
        self.assertIn(expected.resolve(), {
            path.resolve() for path in cleanup_output._payload_paths(payload)
        })

    def test_image_conversion_forwards_include_solution(self):
        image = config.BANK_DIR / "solution-forward.png"
        image.write_bytes(b"image-placeholder")
        with (mock.patch.object(app_module.converter, "_alpha_cwd",
                                return_value=nullcontext()),
              mock.patch.object(app_module.converter, "_load_config_for_user",
                                return_value=object()),
              mock.patch.object(app_module.converter, "_prep_for_ocr",
                                return_value=image),
              mock.patch.object(app_module.converter, "_ensure_src_on_path"),
              mock.patch.object(app_module.converter, "_convert_image",
                                return_value="converted") as convert_image):
            result = app_module.converter.convert_file(
                image, include_solution=True, is_image=True)
        self.assertEqual(result, "converted")
        self.assertTrue(convert_image.call_args.args[2])

    def test_conversion_page_filters_both_task_kinds_and_matches_badge(self):
        active_batch = {
            "status": "converting", "created_at": 1,
            "groups": [{"status": "pending", "reviewed": None}],
        }
        finished_batch = {
            "status": "done", "created_at": 2,
            "groups": [{"status": "done", "reviewed": "imported"}],
        }
        library_tasks = {
            "library-running": {
                "task_id": "library-running", "operation": "pdf_merge",
                "status": "running", "created_at": 1,
            },
            "library-error": {
                "task_id": "library-error", "operation": "card_markdown",
                "status": "error", "created_at": 2, "error": "restart",
            },
            "library-done": {
                "task_id": "library-done", "operation": "pdf_extract",
                "status": "done", "created_at": 3,
            },
        }
        batches = {
            "standard-active": active_batch,
            "standard-finished": finished_batch,
        }
        with (mock.patch.dict(app_module._batch_jobs, batches, clear=True),
              mock.patch.dict(app_module._library_tasks, library_tasks,
                              clear=True)):
            current_page = self.client.get("/batches").get_data(as_text=True)
            all_page = self.client.get("/batches?all=1").get_data(as_text=True)
            current = self.client.get("/batches/status").get_json()
            all_tasks = self.client.get("/batches/status?all=1").get_json()
            badge = app_module._inject_nav_badge()["nav_batch_count"]

        self.assertIn('data-bid="standard-active"', current_page)
        self.assertNotIn('data-bid="standard-finished"', current_page)
        self.assertIn('data-task-id="library-running"', current_page)
        self.assertIn('data-task-id="library-error"', current_page)
        self.assertNotIn('data-task-id="library-done"', current_page)
        self.assertIn("已停止", current_page)
        self.assertIn('data-bid="standard-finished"', all_page)
        self.assertIn('data-task-id="library-done"', all_page)
        self.assertEqual([row["batch_id"] for row in current["batches"]],
                         ["standard-active"])
        self.assertEqual(
            {row["task_id"] for row in current["library_tasks"]},
            {"library-running", "library-error"},
        )
        self.assertEqual(len(all_tasks["batches"]), 2)
        self.assertEqual(len(all_tasks["library_tasks"]), 3)
        self.assertEqual(badge, 3)

    def test_conversion_page_keeps_polling_with_one_task_kind_or_empty(self):
        active_batch = {
            "status": "converting", "created_at": 1,
            "groups": [{"status": "pending", "reviewed": None}],
        }
        active_library = {
            "library-only": {
                "task_id": "library-only", "operation": "pdf_merge",
                "status": "running", "created_at": 1,
            },
        }
        scenarios = (
            ("仅标准任务", {"standard-only": active_batch}, {}, True, False),
            ("仅资料库任务", {}, active_library, False, True),
            ("空任务页", {}, {}, False, False),
        )
        for label, batches, library_tasks, has_batch, has_library in scenarios:
            with self.subTest(label=label), \
                    mock.patch.dict(app_module._batch_jobs, batches, clear=True), \
                    mock.patch.dict(app_module._library_tasks, library_tasks,
                                    clear=True):
                page = self.client.get("/batches").get_data(as_text=True)
                status = self.client.get("/batches/status").get_json()
                self.assertIn('id="batches-overview"', page)
                self.assertIn("js/batches-overview.js", page)
                self.assertEqual('id="bo-list"' in page, has_batch)
                self.assertEqual('id="bo-library-list"' in page, has_library)
                self.assertEqual(bool(status["batches"]), has_batch)
                self.assertEqual(bool(status["library_tasks"]), has_library)

    def test_library_task_snapshot_failure_rolls_back_memory(self):
        task_id = "snapshot-failure-task"
        fake_uuid = mock.Mock(hex=task_id)
        with (mock.patch.object(app_module.uuid, "uuid4", return_value=fake_uuid),
              mock.patch.object(app_module, "_persist_library_task",
                                side_effect=OSError("disk full"))):
            with self.assertRaises(OSError):
                app_module._queue_library_task("pdf_merge", {"sources": []})
        self.assertNotIn(task_id, app_module._library_tasks)

    def test_errors_are_structured_and_history_never_reaches_disk_ops(self):
        self.client.post(
            "/api/library/folder",
            json={"parent": "", "name": "已有"},
            headers=self.headers,
        )
        conflict = self.client.post(
            "/api/library/folder",
            json={"parent": "", "name": "已有"},
            headers=self.headers,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["code"], "conflict")

        with mock.patch.object(app_module.library_ops, "create_folder") as operation:
            rejected = self.client.post(
                "/api/library/folder",
                json={"parent": ".quizforge-history", "name": "不得创建"},
                headers=self.headers,
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["code"], "read_only")
        operation.assert_not_called()

        invalid_mode = self.client.post(
            "/api/library/transfer",
            json={"path": "已有", "target": "", "mode": "link"},
            headers=self.headers,
        )
        self.assertEqual(invalid_mode.status_code, 400)
        self.assertEqual(invalid_mode.get_json()["code"], "invalid_mode")

        with mock.patch.object(app_module.library_ops, "transfer_entry") as operation:
            rejected = self.client.post(
                "/api/library/transfer",
                json={"path": ".quizforge-history/record/result.md",
                      "target": "", "mode": "copy"},
                headers=self.headers,
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["code"], "read_only")
        operation.assert_not_called()

        with mock.patch.object(app_module.library_ops, "transfer_entry") as operation:
            rejected = self.client.post(
                "/api/library/transfer",
                json={"path": "已有", "target": ".quizforge-history",
                      "mode": "move"},
                headers=self.headers,
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["code"], "read_only")
        operation.assert_not_called()

        with mock.patch.object(app_module.library_ops, "rename_entry") as operation:
            rejected = self.client.post(
                "/api/library/rename",
                json={"path": ".quizforge-history/record/result.md", "name": "新名称"},
                headers=self.headers,
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["code"], "read_only")
        operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
