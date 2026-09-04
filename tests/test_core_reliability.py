"""软件版核心可靠性回归：不访问网络、不触碰真实 vault。"""

from __future__ import annotations

import importlib
import html
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import base64
import unittest
from urllib.parse import parse_qs, unquote, urlsplit
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from werkzeug.datastructures import FileStorage
from PIL import Image

import config
import llm_client
import tikz_render


_tmp = None
app_module = None


def setUpModule():
    global _tmp, app_module
    _tmp = tempfile.TemporaryDirectory()
    root = Path(_tmp.name)
    config.BANK_DIR = root / "bank"
    config.TRASH_DIR = config.BANK_DIR / ".trash"
    config.ASSETS_DIR = config.BANK_DIR / "_assets"
    config.HANDOUTS_DIR = config.BANK_DIR / "_handouts"
    config.IMAGES_DIR = config.ASSETS_DIR
    config.PROVIDERS_PATH = root / "providers.json"
    config.SELECTIONS_PATH = root / "selections.json"
    config.TASKS_PATH = root / "tasks.json"
    config.UPLOAD_DIR = root / "uploads"
    config.BATCH_UPLOAD_DIR = config.UPLOAD_DIR / "batch"
    config.HISTORY_DIR = root / "history"
    config.OUTPUT_DIR = root / "output"
    app_module = importlib.import_module("app")


def tearDownModule():
    _tmp.cleanup()


class ImportLayoutDefaultTests(unittest.TestCase):
    def test_choice_defaults_preserve_pair_and_split_ordinary_multi_images(self):
        single = "题干\nA. 1 B. 2 C. 3 D. 4\n![[one.png]]"
        ordinary_multi = single + "\n![[two.png]]"
        paired = (
            "题干\nA. ![[a.png]] B. ![[b.png]] "
            "C. ![[c.png]] D. ![[d.png]]"
        )

        with mock.patch.object(config, "BANK_SUBJECT", "math"):
            self.assertEqual(
                app_module._import_image_defaults("单选题", single),
                ("opts", [], "column"))
            self.assertEqual(
                app_module._import_image_defaults("单选题", ordinary_multi),
                ("between", [], "row"))
            self.assertEqual(
                app_module._import_image_defaults("单选题", paired),
                ("pair", [], "column"))

    def test_math_fill_and_solve_defaults_use_text_image_columns(self):
        body = "题干\n（1）小问\n![[a.png]]\n![[b.png]]"
        with mock.patch.object(config, "BANK_SUBJECT", "math"):
            self.assertEqual(
                app_module._import_image_defaults("填空题", body),
                ("full", [{"i": 0, "stack": True}], "column"))
            self.assertEqual(
                app_module._import_image_defaults("解答题", body),
                ("sub", [{"i": 0, "stack": True}], "column"))

    def test_physics_defaults_put_figures_between_or_after_and_side_by_side(self):
        multi = "题干\n（1）小问\n![[a.png]]\n![[b.png]]"
        single = "题干\n（1）小问\n![[a.png]]"
        with mock.patch.object(config, "BANK_SUBJECT", "physics"):
            self.assertEqual(
                app_module._import_image_defaults("填空题", multi),
                ("between", [], "row"))
            self.assertEqual(
                app_module._import_image_defaults("解答题", single),
                ("after", [{"i": 0, "align": "center"}], "row"))
            self.assertEqual(
                app_module._import_image_defaults("解答题", multi),
                ("after", [{"i": 0, "align": "center"}], "row"))

    def test_solution_images_default_to_flow_and_multi_image_stack(self):
        self.assertEqual(
            app_module._import_solution_image_defaults("解析\n![[a.png]]"),
            ("full", []))
        self.assertEqual(
            app_module._import_solution_image_defaults(
                "解析\n![[a.png]]\n![[b.png]]"),
            ("full", [{"i": 0, "stack": True}]))


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        app_module.task_store._write_unlocked(app_module.task_store._empty())
        app_module._jobs.clear()
        app_module._batch_jobs.clear()

    def test_restart_marks_inflight_error_but_keeps_review_state(self):
        app_module.task_store.save("job", "j1", {
            "status": "converting", "path": "x.pdf", "md": None})
        app_module.task_store.save("batch", "b1", {
            "status": "converting", "running": 1, "groups": [{
                "gid": 0, "status": "awaiting_block_review", "pending": {"blocks": ["x"]},
                "reviewed": None,
            }]})
        app_module.restore_persisted_tasks()
        self.assertEqual(app_module._jobs["j1"]["status"], "error")
        self.assertIn("重启", app_module._jobs["j1"]["error"])
        self.assertEqual(app_module._batch_jobs["b1"]["groups"][0]["status"],
                         "awaiting_block_review")

    def test_single_group_cancel_does_not_cancel_other_groups(self):
        batch = {
            "status": "converting", "running": 1, "cancelled": False,
            "groups": [
                {"gid": 0, "status": "converting", "reviewed": None,
                 "cancelled": False, "md": None},
                {"gid": 1, "status": "pending", "reviewed": None,
                 "cancelled": False, "md": None},
            ],
        }
        app_module._batch_jobs["b2"] = batch
        client = app_module.app.test_client()
        res = client.post("/batch/b2/group/0/cancel",
                          headers={"X-CSRF-Token": app_module._WRITE_TOKEN})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(batch["groups"][0]["cancelled"])
        self.assertFalse(batch["groups"][1]["cancelled"])

    def test_task_snapshot_retries_transient_windows_replace_denial(self):
        store = app_module.task_store
        real_replace = store.os.replace
        attempts = []

        def flaky_replace(source, target):
            attempts.append((source, target))
            if len(attempts) < 3:
                raise PermissionError(5, "模拟 Windows 短暂拒绝访问")
            return real_replace(source, target)

        with (mock.patch.object(store.os, "replace", side_effect=flaky_replace),
              mock.patch.object(store.time, "sleep")):
            store._write_unlocked(store._empty())

        self.assertEqual(len(attempts), 3)
        self.assertTrue(config.TASKS_PATH.is_file())
        self.assertEqual(list(config.TASKS_PATH.parent.glob("tasks.json.*.tmp")), [])


class HistoryRouteTests(unittest.TestCase):
    def setUp(self):
        app_module.task_store._write_unlocked(app_module.task_store._empty())
        app_module._jobs.clear()
        app_module._batch_jobs.clear()
        shutil.rmtree(config.HISTORY_DIR, ignore_errors=True)
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.client = app_module.app.test_client()
        self.headers = {"X-CSRF-Token": app_module._WRITE_TOKEN}

    def test_history_reimport_creates_one_ready_group_without_ocr(self):
        source = config.UPLOAD_DIR / "history-source.pdf"
        source.write_bytes("%PDF-1.4\n历史原卷".encode("utf-8"))
        record = app_module.history_store.create_record(
            "历史试卷", [source], source_names=["历史试卷.pdf"])
        app_module.history_store.attach_markdown(
            record["id"], "1. 历史题目\n\n## 解析\n\n历史解析")

        page = self.client.get("/history")
        self.assertEqual(page.status_code, 200)
        self.assertIn("历史试卷", page.get_data(as_text=True))
        with mock.patch.object(
                app_module.converter, "convert_file",
                side_effect=AssertionError("历史 MD 再导入不应调用 OCR")):
            response = self.client.post(
                f"/history/{record['id']}/reimport", headers=self.headers)

        self.assertEqual(response.status_code, 302)
        batch_id = response.headers["Location"].rstrip("/").split("/")[-1]
        group = app_module._batch_jobs[batch_id]["groups"][0]
        job = app_module._jobs[group["job_id"]]
        self.assertEqual(group["status"], "done")
        self.assertTrue(group["history_reimport"])
        self.assertIn("历史题目", group["md"])
        self.assertEqual(group["history_id"], record["id"])
        self.assertTrue(group["include_solution"])
        self.assertEqual("auto", group["boundary_mode"])
        self.assertTrue(job["include_solution"])
        self.assertEqual("auto", job["boundary_mode"])

    def test_history_reimport_restores_boundary_and_solution_metadata(self):
        source = config.UPLOAD_DIR / "history-whitelist.pdf"
        source.write_bytes(b"%PDF-1.4\nwhitelist")
        record = app_module.history_store.create_record("散题记录", [source])
        app_module.history_store.attach_markdown(
            record["id"], "1. 散题",
            metadata={"include_solution": False,
                      "boundary_mode": "whitelist"})

        response = self.client.post(
            f"/history/{record['id']}/reimport", headers=self.headers)

        self.assertEqual(response.status_code, 302)
        batch_id = response.headers["Location"].rstrip("/").split("/")[-1]
        group = app_module._batch_jobs[batch_id]["groups"][0]
        job = app_module._jobs[group["job_id"]]
        self.assertFalse(group["include_solution"])
        self.assertEqual("whitelist", group["boundary_mode"])
        self.assertFalse(job["include_solution"])
        self.assertEqual("whitelist", job["boundary_mode"])

    def test_history_delete_restore_and_purge_routes(self):
        source = config.UPLOAD_DIR / "history-trash.pdf"
        source.write_bytes(b"%PDF-1.4\ntrash")
        record = app_module.history_store.create_record("回收测试", [source])

        deleted = self.client.post(
            f"/history/{record['id']}/delete", headers=self.headers)
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(app_module.history_store.list_records(), [])
        self.assertEqual(len(app_module.history_store.list_records(trashed=True)), 1)

        restored = self.client.post(
            f"/history/{record['id']}/restore", headers=self.headers)
        self.assertEqual(restored.status_code, 302)
        self.assertEqual(len(app_module.history_store.list_records()), 1)

        self.client.post(
            f"/history/{record['id']}/delete", headers=self.headers)
        purged = self.client.post(
            f"/history/{record['id']}/purge", headers=self.headers)
        self.assertEqual(purged.status_code, 302)
        self.assertEqual(app_module.history_store.list_records(trashed=True), [])

    def test_archive_uses_attempt_record_instead_of_new_group_record(self):
        source = config.UPLOAD_DIR / "attempt-source.pdf"
        source.write_bytes(b"%PDF-1.4\nattempt")
        previous = app_module.history_store.create_record("旧轮次", [source])
        current = app_module.history_store.create_record("新轮次", [source])
        group = {
            "filename": "重试任务.pdf", "file_path": str(source),
            "solution_path": None, "ocr_backend": "mineru",
            "include_solution": False, "boundary_mode": "whitelist",
            "history_id": current["id"],
        }

        app_module._archive_group_markdown(
            group, "旧轮次结果", record_id=previous["id"])

        self.assertEqual(
            app_module.history_store.read_markdown(previous["id"]),
            "旧轮次结果")
        metadata = app_module.history_store.get(previous["id"])["metadata"]
        self.assertFalse(metadata["include_solution"])
        self.assertEqual("whitelist", metadata["boundary_mode"])
        with self.assertRaises(app_module.history_store.HistoryError):
            app_module.history_store.read_markdown(current["id"])


class PersistenceAndCleanupTests(unittest.TestCase):
    def setUp(self):
        app_module.filestore.clear_selected()
        app_module.task_store._write_unlocked(app_module.task_store._empty())
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def test_selection_survives_store_reinitialization(self):
        app_module.filestore.toggle_selected("q-keep")
        app_module.filestore._selected.clear()
        app_module.filestore.init_store()
        self.assertEqual(app_module.filestore.count_selected(), 1)
        self.assertIn("q-keep", app_module.filestore._selected)

    def test_cleanup_keeps_active_upload_and_removes_orphan(self):
        active = config.UPLOAD_DIR / "active.pdf"
        stale = config.UPLOAD_DIR / "stale.pdf"
        active.write_bytes(b"active")
        stale.write_bytes(b"stale")
        old = time.time() - 48 * 3600
        os.utime(active, (old, old))
        os.utime(stale, (old, old))
        app_module.task_store.save("job", "active", {
            "status": "done", "path": str(active)})
        app_module.cleanup_output.run_cleanup(now=time.time())
        self.assertTrue(active.exists())
        self.assertFalse(stale.exists())

    def test_cleanup_removes_expired_collection_workspace_but_keeps_active(self):
        now = time.time()
        # TASKS_PATH 在 setUpModule 中指向临时目录；不能用未重定向的
        # DATA_DIR，否则测试会把工作区落进开发者的真实运行数据目录。
        ocr_root = config.TASKS_PATH.parent / "raw_md_cleanup_test"
        expired = ocr_root / "collection_unit_expired"
        active = ocr_root / "collection_unit_active"
        expired.mkdir(parents=True)
        active.mkdir(parents=True)
        (expired / "raw.md").write_text("expired", encoding="utf-8")
        (active / "raw.md").write_text("active", encoding="utf-8")

        payload_expired = {
            "status": "done",
            "groups": [{"cleanup_dirs": [str(expired), str(active)]}],
        }
        payload_active = {
            "status": "done",
            "groups": [{"cleanup_dirs": [str(active)]}],
        }
        snapshots = app_module.task_store._empty()
        snapshots["batch"] = {
            "expired": {
                "updated_at": now - 8 * 86400,
                "payload": payload_expired,
            },
            "active": {"updated_at": now, "payload": payload_active},
        }
        app_module.task_store._write_unlocked(snapshots)

        with mock.patch.object(app_module.converter, "_RAW_MD_ROOT", ocr_root):
            counts = app_module.cleanup_output.run_cleanup(now=now)

        self.assertFalse(expired.exists())
        self.assertTrue(active.exists())
        self.assertEqual(1, counts["workspaces"])

    def test_store_paper_is_idempotent_and_rejects_name_conflict(self):
        folder = app_module.filestore.get_or_create_collection("幂等原卷", "")
        source = config.UPLOAD_DIR / "same.pdf"
        source.write_bytes(b"%PDF-1.7\nsame")
        first = app_module.filestore.store_paper(
            source, folder, "同名卷.pdf", "exam")
        second = app_module.filestore.store_paper(
            source, folder, "同名卷.pdf", "exam")
        self.assertEqual(first, second)
        self.assertEqual(len(list((config.BANK_DIR / folder).glob("*.pdf"))), 1)

        source.write_bytes(b"%PDF-1.7\ndifferent")
        with self.assertRaises(FileExistsError):
            app_module.filestore.store_paper(
                source, folder, "同名卷.pdf", "exam")


class SecurityAndProviderTests(unittest.TestCase):
    def test_lightweight_write_token_authorizes_local_post(self):
        client = app_module.app.test_client()
        token = client.get("/api/write-token").get_json()["token"]
        response = client.post("/clear", headers={"X-CSRF-Token": token})
        self.assertIn(response.status_code, (302, 303))

    def test_write_request_requires_token(self):
        client = app_module.app.test_client()
        self.assertEqual(client.post("/clear").status_code, 400)
        self.assertIn(client.post(
            "/clear", headers={"X-CSRF-Token": app_module._WRITE_TOKEN}
        ).status_code, (302, 303))

    def test_exam_signature_rejects_fake_pdf(self):
        fake = FileStorage(stream=io.BytesIO(b"not a pdf"), filename="fake.pdf")
        with self.assertRaises(app_module._UploadRejected):
            app_module._check_exam_file(fake)
        real_header = FileStorage(
            stream=io.BytesIO(b"\xef\xbb\xbf%PDF-1.7\n"), filename="ok.pdf")
        app_module._check_exam_file(real_header)

    def test_provider_edit_preserves_key_when_blank(self):
        pid = app_module.providers.add_llm_provider(
            "旧名", "https://old.example/v1", "encrypted-key", "old-model", 1024)
        self.assertTrue(app_module.providers.update_llm_provider(
            pid, name="新名", base_url="https://new.example/v1",
            model="new-model", max_tokens=2048))
        row = app_module.providers.get_llm_provider(pid)
        self.assertEqual(row["api_key_enc"], "encrypted-key")
        self.assertEqual(row["name"], "新名")

    def test_settings_lists_only_visual_models_in_quick_switch(self):
        text_id = app_module.providers.add_llm_provider(
            "纯文本", "https://text.example/v1", "enc", "text-model", 1024)
        vision_id = app_module.providers.add_llm_provider(
            "视觉", "https://vision.example/v1", "enc", "vision-model", 16000,
            purposes=("redraw",), supports_vision=True)
        app_module.providers.set_active_llm_provider(vision_id, "redraw")

        html = app_module.app.test_client().get("/settings").get_data(as_text=True)
        quick = html.split('aria-label="配图重绘模型快速切换"', 1)[1]
        quick = quick.split('</div>\n    {% endif %}', 1)[0] if '</div>\n    {% endif %}' in quick else quick.split('<table', 1)[0]
        self.assertIn("vision-model", quick)
        self.assertNotIn("text-model", quick)
        self.assertIn(f'value="{vision_id}"', quick)
        self.assertNotIn(f'value="{text_id}"', quick)


class RedrawModelTests(unittest.TestCase):
    @staticmethod
    def _png_bytes(color=(0, 0, 0, 255)):
        buf = io.BytesIO()
        Image.new("RGBA", (2, 2), color).save(buf, format="PNG")
        return buf.getvalue()

    def test_image_model_detection_separates_images_api_from_qwen_vision(self):
        self.assertTrue(llm_client.is_image_generation_model("gpt-image-2"))
        self.assertTrue(llm_client.is_image_generation_model("openai/gpt-image-1"))
        self.assertFalse(llm_client.is_image_generation_model("dall-e-2"))
        self.assertFalse(llm_client.is_image_generation_model("dall-e-3"))
        self.assertFalse(llm_client.is_image_generation_model("qwen3-vl-plus"))

    def test_images_edit_decodes_base64_response(self):
        with tempfile.TemporaryDirectory() as raw:
            image = Path(raw) / "input.jpg"
            image.write_bytes(self._png_bytes())
            encoded = base64.b64encode(self._png_bytes((255, 255, 255, 255))).decode()
            edit = mock.Mock(return_value=SimpleNamespace(
                data=[SimpleNamespace(b64_json=encoded)]))
            client = object.__new__(llm_client.LLMClient)
            client.model = "gpt-image-2"
            client.base_url = "https://yapi.click/v1"
            client._client = SimpleNamespace(
                images=SimpleNamespace(edit=edit))

            result = client.edit_image("重绘", image)

        self.assertEqual(result, self._png_bytes((255, 255, 255, 255)))
        edit.assert_called_once()
        self.assertEqual(edit.call_args.kwargs["model"], "gpt-image-2")
        self.assertEqual(edit.call_args.kwargs["prompt"], "重绘")

    def test_image_model_redraw_saves_png_and_can_be_applied(self):
        qid = app_module.filestore.create_question(
            "图像模型重绘题\n![[redraw-input.png]]")
        config.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        (config.ASSETS_DIR / "redraw-input.png").write_bytes(self._png_bytes())
        provider = SimpleNamespace(model="gpt-image-2")
        client = SimpleNamespace(edit_image=mock.Mock(
            return_value=self._png_bytes((255, 255, 255, 255))))

        with mock.patch.object(app_module.providers, "resolve",
                              return_value=provider), \
                mock.patch.object(app_module.llm_client, "build_client",
                                  return_value=client):
            result = app_module.tikz_redraw.redraw(qid, 0)

        self.assertRegex(result["name"], r"^redraw_[0-9a-f]{16}\.png$")
        self.assertEqual(result["code"], "")
        app_module.tikz_redraw.validate_generated(result["name"])
        old = app_module.tikz_redraw.apply_redraw(qid, 0, result["name"])
        self.assertEqual(old, "redraw-input.png")
        self.assertIn(f"![[{result['name']}]]",
                      app_module.filestore.get_question(qid)["body"])
        self.assertEqual(
            app_module.filestore.get_img_original(qid, 0), "redraw-input.png")

    def test_qwen_redraw_keeps_provider_max_tokens(self):
        qid = app_module.filestore.create_question(
            "Qwen 视觉重绘题\n![[qwen-input.png]]")
        config.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        (config.ASSETS_DIR / "qwen-input.png").write_bytes(self._png_bytes())
        provider = SimpleNamespace(model="qwen3-vl-plus")
        client = SimpleNamespace(
            max_tokens=8192,
            chat_vision=mock.Mock(return_value=("tikz", "stop")))

        with mock.patch.object(app_module.providers, "resolve",
                              return_value=provider), \
                mock.patch.object(app_module.llm_client, "build_client",
                                  return_value=client), \
                mock.patch.object(
                    app_module.tikz_redraw.tikz_render, "render_from_reply",
                    return_value=("tikz", "tikz.pdf", "tikz.svg")):
            result = app_module.tikz_redraw.redraw(qid, 0)

        client.chat_vision.assert_called_once()
        self.assertEqual(client.chat_vision.call_args.kwargs["max_tokens"], 8192)
        self.assertEqual(result["name"], "tikz.svg")


class ImageVersionTests(unittest.TestCase):
    @staticmethod
    def _png_bytes(color):
        buf = io.BytesIO()
        Image.new("RGBA", (3, 2), color).save(buf, format="PNG")
        return buf.getvalue()

    def setUp(self):
        config.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        self.client = app_module.app.test_client()
        self.headers = {"X-CSRF-Token": app_module._WRITE_TOKEN}

    def _asset(self, name, color):
        (config.ASSETS_DIR / name).write_bytes(self._png_bytes(color))

    def test_old_original_metadata_is_migrated_to_versions(self):
        qid = app_module.filestore.create_question(
            "旧题迁移\n![[current.png]]")
        self._asset("current.png", (10, 20, 30, 255))
        app_module.filestore._update_meta_fields(
            qid, {"img_originals": [{"i": 0, "orig": "source.jpg"}]})

        versions = app_module.filestore.ensure_img_versions(qid)
        self.assertEqual(
            {(row["name"], row["kind"]) for row in versions if row["i"] == 0},
            {("source.jpg", "original"), ("current.png", "original")})
        meta, _body = app_module.filestore._read_raw(
            config.BANK_DIR / app_module.filestore.get_question(qid)["path"])
        self.assertIn("img_versions", meta)

    def test_api_lists_switches_and_deletes_versions_with_csrf(self):
        qid = app_module.filestore.create_question(
            "版本管理题\n![[source.jpg]]")
        self._asset("source.jpg", (0, 0, 0, 255))
        first = "redraw_1111111111111111.png"
        second = "redraw_2222222222222222.png"
        self._asset(first, (255, 0, 0, 255))
        self._asset(second, (0, 255, 0, 255))
        app_module.tikz_redraw.apply_redraw(qid, 0, first)
        app_module.filestore.remember_img_version(
            qid, 0, second, kind="generated", model="gpt-image-2",
            created="2026-09-01T12:00:00", prompt="保留坐标轴")

        listed = self.client.get(
            f"/question/{qid}/redraw/versions?index=0")
        self.assertEqual(listed.status_code, 200)
        rows = listed.get_json()["versions"]
        self.assertEqual({row["name"] for row in rows},
                         {"source.jpg", first, second})
        second_row = next(row for row in rows if row["name"] == second)
        self.assertEqual(second_row["model"], "gpt-image-2")
        self.assertFalse(second_row["current"])
        self.assertTrue(second_row["can_delete"])

        self.assertEqual(self.client.post(
            f"/question/{qid}/redraw/version/switch",
            json={"index": 0, "name": "source.jpg"}).status_code, 400)
        switched = self.client.post(
            f"/question/{qid}/redraw/version/switch",
            json={"index": 0, "name": "source.jpg"}, headers=self.headers)
        self.assertEqual(switched.status_code, 200)
        self.assertIn("![[source.jpg]]",
                      app_module.filestore.get_question(qid)["body"])
        self.assertTrue(next(row for row in switched.get_json()["versions"]
                             if row["name"] == "source.jpg")["current"])

        current_delete = self.client.post(
            f"/question/{qid}/redraw/version/delete",
            json={"index": 0, "name": "source.jpg"}, headers=self.headers)
        self.assertEqual(current_delete.status_code, 400)
        deleted = self.client.post(
            f"/question/{qid}/redraw/version/delete",
            json={"index": 0, "name": first}, headers=self.headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertNotIn(first, {row["name"] for row in deleted.get_json()["versions"]})
        self.assertFalse((config.ASSETS_DIR / first).exists())

    def test_shared_version_is_kept_when_deleting_one_question_metadata(self):
        qid = app_module.filestore.create_question(
            "共享版本题甲\n![[source-a.jpg]]")
        other = app_module.filestore.create_question(
            "共享版本题乙\n![[source-b.jpg]]")
        self._asset("source-a.jpg", (0, 0, 0, 255))
        self._asset("source-b.jpg", (0, 0, 0, 255))
        shared = "redraw_3333333333333333.png"
        self._asset(shared, (0, 0, 255, 255))
        app_module.tikz_redraw.apply_redraw(qid, 0, shared)
        app_module.filestore.remember_img_version(
            other, 0, shared, kind="generated", model="shared")

        with mock.patch.object(app_module.filestore, "_registered_bank_roots",
                               return_value=[config.BANK_DIR]):
            result = self.client.post(
                f"/question/{qid}/redraw/version/delete",
                json={"index": 0, "name": shared}, headers=self.headers)
        self.assertEqual(result.status_code, 400)
        # 当前版本不能删除；先切回原图，再删除共享历史版本。
        self.client.post(
            f"/question/{qid}/redraw/version/switch",
            json={"index": 0, "name": "source-a.jpg"}, headers=self.headers)
        with mock.patch.object(app_module.filestore, "_registered_bank_roots",
                               return_value=[config.BANK_DIR]):
            result = self.client.post(
                f"/question/{qid}/redraw/version/delete",
                json={"index": 0, "name": shared}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertTrue((config.ASSETS_DIR / shared).exists())

    def test_swap_images_moves_version_indexes_with_images(self):
        qid = app_module.filestore.create_question(
            "交换版本题\n![[a.jpg]]\n![[b.jpg]]")
        app_module.filestore.remember_img_version(qid, 0, "a.jpg", kind="original")
        app_module.filestore.remember_img_version(qid, 1, "b.jpg", kind="original")
        body = "交换版本题\n![[b.jpg]]\n![[a.jpg]]"
        app_module.filestore.swap_images(qid, 0, 1, body)
        rows = app_module.filestore.get_question(qid)["img_versions"]
        self.assertEqual({(row["i"], row["name"]) for row in rows},
                         {(0, "b.jpg"), (1, "a.jpg")})


class PageTests(unittest.TestCase):
    def test_search_controls_preserve_current_logical_scope(self):
        folder = app_module.filestore.get_or_create_collection(
            "搜索范围保持", "")
        app_module.filestore.create_question(
            "范围保持样例", qtype="解答题", difficulty="3",
            tags=["函数", "重点"], folder=folder)
        response = app_module.app.test_client().get("/", query_string=[
            ("collection", folder), ("tag", "函数"), ("tag", "重点"),
            ("match", "and"), ("type", "解答题"), ("difficulty", "3"),
            ("starred", "1"), ("sort", "difficulty"), ("q", "content:样例"),
        ])

        page = response.get_data(as_text=True)
        search_form = page.split('class="search-bar"', 1)[1].split("</form>", 1)[0]
        filter_form = page.split('id="filter-form"', 1)[1].split("</form>", 1)[0]
        clear_href = html.unescape(re.search(
            r'<a href="([^"]+)" class="btn btn-ghost" id="search-clear">',
            page).group(1))
        clear_args = parse_qs(urlsplit(clear_href).query)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(search_form.count('name="tag"'), 2)
        self.assertIn(f'name="collection" value="{folder}"', search_form)
        self.assertIn('name="difficulty" value="3"', search_form)
        self.assertIn('name="q" value="content:样例"', filter_form)
        self.assertNotIn("q", clear_args)
        self.assertEqual(clear_args["collection"], [folder])
        self.assertEqual(clear_args["tag"], ["函数", "重点"])
        self.assertEqual(clear_args["difficulty"], ["3"])

    def test_folder_links_keep_search_and_filters_while_changing_folder(self):
        target = app_module.filestore.get_or_create_collection(
            "搜索切换目标", "")
        response = app_module.app.test_client().get("/", query_string=[
            ("all", "1"), ("tag", "函数"), ("difficulty", "3"),
            ("q", "source:期中"),
        ])

        page = response.get_data(as_text=True)
        hrefs = [html.unescape(value) for value in re.findall(
            r'class="folder-link"[^>]+href="([^"]+)"', page)]
        target_href = next(
            value for value in hrefs
            if parse_qs(urlsplit(value).query).get("collection") == [target])
        args = parse_qs(urlsplit(target_href).query)

        self.assertEqual(args["q"], ["source:期中"])
        self.assertEqual(args["tag"], ["函数"])
        self.assertEqual(args["difficulty"], ["3"])
        self.assertNotIn("all", args)

    def test_invalid_search_is_shown_without_broadening_results(self):
        marker = "非法搜索不得展示这道题"
        app_module.filestore.create_question(marker, qtype="填空题")

        response = app_module.app.test_client().get(
            "/", query_string={"all": "1", "q": "starred:maybe"})
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("starred: 仅支持 true/false 或 1/0", page)
        self.assertIn('value="starred:maybe"', page)
        self.assertNotIn(marker, page)

    def test_select_all_rejects_invalid_search(self):
        response = app_module.app.test_client().post(
            "/select_all",
            data={"all": "1", "q": "starred:maybe"},
            headers={
                "X-CSRF-Token": app_module._WRITE_TOKEN,
                "Accept": "application/json",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("starred", response.get_json()["error"])

    def test_dedup_page_returns_without_scanning_bank(self):
        with mock.patch.object(
                app_module.filestore, "list_questions",
                side_effect=AssertionError("GET 页面不应同步扫描题库")):
            response = app_module.app.test_client().get("/dedup")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="dedup-progress"', html)
        self.assertIn("dedup-scan.js", html)

    def test_dedup_worker_produces_renderable_results(self):
        job_id = "unit-dedup"
        app_module._dedup_jobs[job_id] = {"status": "loading"}
        rows = [
            {"id": "a", "body": "同一道题", "type": "填空题", "source": "A"},
            {"id": "b", "body": "同一道题。", "type": "填空题", "source": "B"},
        ]
        try:
            with mock.patch.object(app_module.filestore, "list_questions", return_value=rows):
                app_module._dedup_worker(job_id, 0.85)
            result = app_module.app.test_client().get(
                f"/api/dedup/{job_id}").get_json()
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["groups"], 1)
            self.assertIn("完全重复", result["html"])
        finally:
            app_module._dedup_jobs.pop(job_id, None)

    def test_import_preview_does_not_scan_existing_bank_for_duplicates(self):
        raw = "- [填空] 1. $x=1$\n\n- [填空] 2. $x=1$"
        with mock.patch.object(
                app_module.filestore, "list_questions",
                side_effect=AssertionError("导入预览不应自动扫描历史题库")):
            preview, _folders, _missing = app_module._build_import_preview(
                raw, all_cols=[])

        self.assertEqual(len(preview), 2)
        self.assertIsNone(preview[0]["dup"])
        self.assertEqual(preview[1]["dup"], "本批重复")

    def test_whitelist_preview_preserves_number_restarts_and_skips_only_natural_gaps(self):
        raw = ("- [解答] 1. 第一题\n\n- [解答] 2. 第二题\n\n"
               "- [解答] 3. 第三题\n\n- [解答] 4. 第四题\n\n"
               "- [解答] 5. 第五题\n\n- [解答] 1. 回卷第一题\n\n"
               "- [解答] 2. 回卷第二题\n\n- [解答] 3. 回卷第三题")
        preview, _folders, missing = app_module._build_import_preview(
            raw, all_cols=[], boundary_mode="whitelist")

        self.assertEqual(
            [item["number"] for item in preview], [1, 2, 3, 4, 5, 1, 2, 3])
        self.assertIsNone(missing)

        gap_raw = "- [解答] 1. 第一题\n\n- [解答] 3. 第三题"
        _preview, _folders, natural_missing = app_module._build_import_preview(
            gap_raw, all_cols=[], boundary_mode="whitelist")
        _preview, _folders, requested_missing = app_module._build_import_preview(
            gap_raw, all_cols=[], boundary_mode="whitelist",
            only_numbers=[1, 2, 3])
        self.assertIsNone(natural_missing)
        self.assertEqual([2], requested_missing)

    def test_auto_import_uses_one_batch_write_without_bank_dedup(self):
        batch_id = "auto-import-performance"
        grp = {
            "gid": 0,
            "filename": "测试卷.pdf",
            "md": "转换结果",
            "include_solution": True,
            "only_numbers": None,
            "status": "done",
            "reviewed": None,
            "imported_count": 0,
        }
        batch = {
            "status": "converting",
            "groups": [grp],
            "files_cleaned": False,
        }
        app_module._batch_jobs[batch_id] = batch
        preview = [
            {"body": "题目一", "solution": "解析一", "type": "填空题",
             "dup": None, "number": 1},
            {"body": "题目二", "solution": "", "type": "解答题",
             "dup": None, "number": 2},
        ]
        try:
            with mock.patch.object(
                    app_module, "_build_import_preview",
                    return_value=(preview, [], None)) as build_preview, \
                    mock.patch.object(
                        app_module, "_auto_import_folder",
                        return_value="目标文件夹"), \
                    mock.patch.object(
                        app_module.filestore, "create_questions_batch",
                        return_value=["q1", "q2"]) as create_batch, \
                    mock.patch.object(
                        app_module.filestore, "create_question",
                        side_effect=AssertionError("自动入库不应逐题写入")), \
                    mock.patch.object(app_module, "_persist_batch"), \
                    mock.patch.object(app_module, "_maybe_finish_batch"):
                app_module._auto_import_after_convert(
                    batch_id, grp, attempt=0, md_snapshot=grp["md"])
        finally:
            app_module._batch_jobs.pop(batch_id, None)

        build_preview.assert_called_once_with(
            "转换结果", include_solution=True, only_numbers=None,
            boundary_mode="auto", existing_fps=set(), all_cols=[])
        create_batch.assert_called_once_with([
            {"body": "题目一", "solution": "解析一", "type": "填空题",
             "source": "测试卷", "number": 1},
            {"body": "题目二", "solution": "", "type": "解答题",
             "source": "测试卷", "number": 2},
        ], "目标文件夹",
            idempotency_scope="batch:auto-import-performance:group:0")
        self.assertEqual(grp["reviewed"], "imported")
        self.assertEqual(grp["imported_count"], 2)

    def test_imported_collection_refresh_uses_only_safe_refresh_and_clears_marker(self):
        batch_id = "safe-refresh-success"
        previous = [
            {"body": "旧第一题", "solution": "旧解析", "type": "单选题",
             "source": "专题", "number": 1},
        ]
        grp = {
            "gid": 0, "filename": "专题.pdf", "md": "新转换结果",
            "note": "", "include_solution": True, "only_numbers": None,
            "collection_strategy": "ocr_structure", "collection_unit": True,
            "status": "done", "reviewed": "imported", "imported_count": 1,
            "attempt": 2, "refresh_in_progress": True,
            "refresh_previous_items": previous, "auto_review_blocked": True,
        }
        batch = {"status": "done", "groups": [grp], "files_cleaned": False,
                 "auto_import": True, "auto_keep_original": False}
        preview = [{"body": "新第一题", "solution": "新解析",
                    "type": "单选题", "dup": None, "number": 1}]
        app_module._batch_jobs[batch_id] = batch
        try:
            with mock.patch.object(
                    app_module, "_build_import_preview",
                    return_value=(preview, [], None)), \
                    mock.patch.object(
                        app_module, "_auto_import_folder",
                        return_value="专题"), \
                    mock.patch.object(
                        app_module.filestore, "refresh_questions_batch",
                        return_value=["stable-qid"]) as refresh, \
                    mock.patch.object(
                        app_module.filestore, "create_questions_batch") as create, \
                    mock.patch.object(app_module, "_persist_batch"), \
                    mock.patch.object(app_module, "_maybe_finish_batch") as finish:
                app_module._auto_import_after_convert(
                    batch_id, grp, attempt=2, md_snapshot=grp["md"])
        finally:
            app_module._batch_jobs.pop(batch_id, None)

        refresh.assert_called_once_with([
            {"body": "新第一题", "solution": "新解析", "type": "单选题",
             "source": "专题", "number": 1},
        ], previous, "专题",
            idempotency_scope="batch:safe-refresh-success:group:0")
        create.assert_not_called()
        self.assertEqual("imported", grp["reviewed"])
        self.assertEqual(1, grp["imported_count"])
        self.assertNotIn("refresh_in_progress", grp)
        self.assertNotIn("refresh_previous_items", grp)
        self.assertNotIn("auto_review_blocked", grp)
        finish.assert_called_once_with(batch_id)

    def test_imported_collection_refresh_failure_keeps_old_state_and_baseline(self):
        batch_id = "safe-refresh-failure"
        previous = [{"body": "旧题", "solution": "旧解析", "type": "单选题",
                     "source": "专题", "number": 1}]
        grp = {
            "gid": 0, "filename": "专题.pdf", "md": "新转换结果",
            "note": "", "include_solution": True, "only_numbers": None,
            "collection_strategy": "ocr_structure", "collection_unit": True,
            "status": "done", "reviewed": "imported", "imported_count": 1,
            "attempt": 3, "refresh_in_progress": True,
            "refresh_previous_items": previous,
        }
        batch = {"status": "done", "groups": [grp], "files_cleaned": False,
                 "auto_import": True, "auto_keep_original": False}
        preview = [{"body": "新题", "solution": "新解析", "type": "单选题",
                    "dup": None, "number": 1}]
        app_module._batch_jobs[batch_id] = batch
        try:
            with mock.patch.object(
                    app_module, "_build_import_preview",
                    return_value=(preview, [], None)), \
                    mock.patch.object(app_module, "_auto_import_folder",
                                      return_value="专题"), \
                    mock.patch.object(
                        app_module.filestore, "refresh_questions_batch",
                        side_effect=ValueError("检测到用户编辑")), \
                    mock.patch.object(
                        app_module.filestore, "create_questions_batch") as create, \
                    mock.patch.object(app_module, "_persist_batch"), \
                    mock.patch.object(app_module, "_maybe_finish_batch") as finish:
                app_module._auto_import_after_convert(
                    batch_id, grp, attempt=3, md_snapshot=grp["md"])
        finally:
            app_module._batch_jobs.pop(batch_id, None)

        create.assert_not_called()
        self.assertEqual("error", grp["status"])
        self.assertEqual("imported", grp["reviewed"])
        self.assertEqual(1, grp["imported_count"])
        self.assertTrue(grp["refresh_in_progress"])
        self.assertIs(previous, grp["refresh_previous_items"])
        self.assertIn("旧题已保留且未覆盖", grp["error"])
        self.assertIn("检测到用户编辑", grp["error"])
        finish.assert_not_called()

    def test_reconvert_requires_explicit_refresh_flag_for_imported_group(self):
        batch_id = "safe-refresh-route"
        grp = {
            "gid": 0, "job_id": "job-safe-refresh", "filename": "专题.pdf",
            "md": "旧转换结果", "note": "", "include_solution": True,
            "only_numbers": None, "num_template": "", "engine": "block",
            "collection_strategy": "ocr_structure", "collection_unit": True,
            "status": "done", "reviewed": "imported", "imported_count": 1,
            "attempt": 0, "in_flight": False, "cancelled": False,
        }
        batch = {"status": "done", "groups": [grp], "files_cleaned": False,
                 "auto_import": True}
        old_preview = [{"body": "旧题", "solution": "旧解析",
                        "type": "单选题", "dup": None, "number": 1}]
        app_module._batch_jobs[batch_id] = batch
        client = app_module.app.test_client()
        try:
            with mock.patch.object(app_module.threading, "Thread") as thread:
                response = client.post(
                    f"/batch/{batch_id}/group/0/reconvert",
                    headers={"X-CSRF-Token": app_module._WRITE_TOKEN})
                self.assertEqual(302, response.status_code)
                thread.assert_not_called()
                self.assertNotIn("refresh_in_progress", grp)

            with mock.patch.object(
                    app_module, "_build_import_preview",
                    return_value=(old_preview, [], None)), \
                    mock.patch.object(app_module.threading, "Thread") as thread, \
                    mock.patch.object(app_module, "_persist_batch"):
                response = client.post(
                    f"/batch/{batch_id}/group/0/reconvert",
                    data={"refresh_imported": "1"},
                    headers={"X-CSRF-Token": app_module._WRITE_TOKEN})
                self.assertEqual(302, response.status_code)
                thread.return_value.start.assert_called_once_with()
        finally:
            app_module._batch_jobs.pop(batch_id, None)

        self.assertEqual("imported", grp["reviewed"])
        self.assertEqual(1, grp["imported_count"])
        self.assertTrue(grp["refresh_in_progress"])
        self.assertEqual(1, grp["attempt"])
        self.assertEqual("converting", grp["status"])
        self.assertIsNone(grp["md"])
        self.assertFalse(app_module._group_terminal(grp))

    def test_reconvert_missing_boundary_field_keeps_whitelist_and_forces_block(self):
        batch_id = "reconvert-whitelist-preserve"
        grp = {
            "gid": 0, "job_id": "job-whitelist-preserve",
            "filename": "散题.pdf", "file_path": None, "solution_path": None,
            "include_solution": False, "only_numbers": None,
            "num_template": "", "boundary_mode": "whitelist",
            "engine": "whole", "collection_strategy": "",
            "collection_unit": False, "status": "error", "reviewed": None,
            "attempt": 0, "in_flight": False, "cancelled": False,
        }
        app_module._batch_jobs[batch_id] = {
            "status": "done", "groups": [grp], "files_cleaned": False,
        }
        job = {
            "status": "error", "boundary_mode": "auto", "engine": "whole",
            "num_template": "", "only_numbers": None,
        }
        app_module._jobs[grp["job_id"]] = job
        try:
            with mock.patch.object(app_module, "_persist_batch"), \
                    mock.patch.object(app_module, "_persist_job") as persist_job, \
                    mock.patch.object(app_module.threading, "Thread") as thread:
                response = app_module.app.test_client().post(
                    f"/batch/{batch_id}/group/0/reconvert",
                    data={"only_numbers": "2-3", "num_template": "第x题"},
                    headers={"X-CSRF-Token": app_module._WRITE_TOKEN})
        finally:
            app_module._batch_jobs.pop(batch_id, None)
            app_module._jobs.pop(grp["job_id"], None)

        self.assertEqual(302, response.status_code)
        self.assertEqual("whitelist", grp["boundary_mode"])
        self.assertEqual("block", grp["engine"])
        self.assertEqual([2, 3], grp["only_numbers"])
        self.assertEqual("第x题", grp["num_template"])
        self.assertEqual("whitelist", job["boundary_mode"])
        self.assertEqual("block", job["engine"])
        self.assertEqual([2, 3], job["only_numbers"])
        self.assertEqual("第x题", job["num_template"])
        persist_job.assert_called_once_with(grp["job_id"], job)
        thread.return_value.start.assert_called_once_with()
        self.assertIsNone(grp["md"])
        self.assertFalse(app_module._group_terminal(grp))

    def test_auto_import_stops_for_required_manual_review(self):
        batch_id = "auto-import-quality-guard"
        grp = {
            "gid": 0,
            "filename": "异常卷.pdf",
            "md": "- [解答] 1. 题干",
            "note": "【必须人工校对】有正文可能不会进入最终题目",
            "status": "done",
            "reviewed": None,
        }
        batch = {"status": "done", "groups": [grp], "files_cleaned": False}
        app_module._batch_jobs[batch_id] = batch
        try:
            with mock.patch.object(app_module, "_persist_batch") as persist, \
                    mock.patch.object(
                        app_module.filestore, "create_questions_batch",
                        side_effect=AssertionError("质量异常时不得免审写入")):
                app_module._auto_import_after_convert(
                    batch_id, grp, attempt=0, md_snapshot=grp["md"])
        finally:
            app_module._batch_jobs.pop(batch_id, None)

        self.assertTrue(grp["auto_review_blocked"])
        self.assertIsNone(grp["reviewed"])
        persist.assert_called_once_with(batch_id, batch)

    def test_auto_import_stops_and_persists_when_question_numbers_are_missing(self):
        batch_id = "auto-import-missing-number-guard"
        grp = {
            "gid": 0,
            "filename": "缺题试卷.pdf",
            "md": "- [解答] 1. 第一题\n\n- [解答] 3. 第三题",
            "note": "",
            "include_solution": True,
            "only_numbers": None,
            "status": "done",
            "reviewed": None,
        }
        batch = {"status": "done", "groups": [grp], "files_cleaned": False}
        preview = [
            {"body": "第一题", "solution": "", "type": "解答题",
             "dup": None, "number": 1},
            {"body": "第三题", "solution": "", "type": "解答题",
             "dup": None, "number": 3},
        ]
        app_module._batch_jobs[batch_id] = batch
        try:
            with mock.patch.object(
                    app_module, "_build_import_preview",
                    return_value=(preview, [], [2])) as build_preview, \
                    mock.patch.object(app_module, "_persist_batch") as persist, \
                    mock.patch.object(
                        app_module.filestore, "create_questions_batch",
                        side_effect=AssertionError("题号缺失时不得免审写入")), \
                    mock.patch.object(app_module, "_maybe_finish_batch") as finish:
                app_module._auto_import_after_convert(
                    batch_id, grp, attempt=0, md_snapshot=grp["md"])
        finally:
            app_module._batch_jobs.pop(batch_id, None)

        build_preview.assert_called_once_with(
            grp["md"], include_solution=True, only_numbers=None,
            boundary_mode="auto", existing_fps=set(), all_cols=[])
        self.assertTrue(grp["auto_review_blocked"])
        self.assertIsNone(grp["reviewed"])
        self.assertIn(app_module.qualcheck.MANUAL_REVIEW_MARKER, grp["note"])
        self.assertIn("题号 2", grp["note"])
        persist.assert_called_once_with(batch_id, batch)
        finish.assert_not_called()

    def test_auto_import_stops_before_write_when_question_numbers_repeat(self):
        batch_id = "auto-import-duplicate-number-guard"
        grp = {
            "gid": 0,
            "filename": "重复题号试卷.pdf",
            "md": "转换结果",
            "note": "",
            "include_solution": True,
            "only_numbers": None,
            "status": "done",
            "reviewed": None,
            "attempt": 0,
        }
        batch = {"status": "done", "groups": [grp], "files_cleaned": False}
        preview = [
            {"body": "第九题甲", "solution": "", "type": "解答题",
             "dup": None, "number": 9},
            {"body": "第九题乙", "solution": "", "type": "解答题",
             "dup": None, "number": 9},
            {"body": "第十题", "solution": "", "type": "解答题",
             "dup": None, "number": 10},
        ]
        app_module._batch_jobs[batch_id] = batch
        try:
            with mock.patch.object(
                    app_module, "_build_import_preview",
                    return_value=(preview, [], None)), \
                    mock.patch.object(app_module, "_persist_batch") as persist, \
                    mock.patch.object(
                        app_module.filestore, "create_questions_batch",
                        side_effect=AssertionError("重复题号时不得免审写入")), \
                    mock.patch.object(app_module, "_maybe_finish_batch") as finish:
                app_module._auto_import_after_convert(
                    batch_id, grp, attempt=0, md_snapshot=grp["md"])
        finally:
            app_module._batch_jobs.pop(batch_id, None)

        self.assertTrue(grp["auto_review_blocked"])
        self.assertIsNone(grp["reviewed"])
        self.assertIn(app_module.qualcheck.MANUAL_REVIEW_MARKER, grp["note"])
        self.assertIn("重复题号 9", grp["note"])
        persist.assert_called_once_with(batch_id, batch)
        finish.assert_not_called()

    def test_whitelist_auto_import_allows_repeated_numbers(self):
        batch_id = "auto-import-whitelist-repeat"
        grp = {
            "gid": 0, "filename": "散题.pdf", "md": "转换结果", "note": "",
            "include_solution": True, "only_numbers": None,
            "boundary_mode": "whitelist", "status": "done", "reviewed": None,
            "imported_count": 0, "attempt": 0,
        }
        batch = {"status": "done", "groups": [grp], "files_cleaned": False}
        preview = [
            {"body": "第一题甲", "solution": "", "type": "解答题",
             "dup": None, "number": 1},
            {"body": "第一题乙", "solution": "", "type": "解答题",
             "dup": None, "number": 1},
            {"body": "第一题乙", "solution": "", "type": "解答题",
             "dup": "本批重复", "number": 2},
        ]
        app_module._batch_jobs[batch_id] = batch
        try:
            with mock.patch.object(
                    app_module, "_build_import_preview",
                    return_value=(preview, [], None)) as build_preview, \
                    mock.patch.object(app_module, "_auto_import_folder",
                                      return_value="散题"), \
                    mock.patch.object(
                        app_module.filestore, "create_questions_batch",
                        return_value=["q1", "q2"]) as create_batch, \
                    mock.patch.object(app_module, "_persist_batch"), \
                    mock.patch.object(app_module, "_maybe_finish_batch"):
                app_module._auto_import_after_convert(
                    batch_id, grp, attempt=0, md_snapshot=grp["md"])
        finally:
            app_module._batch_jobs.pop(batch_id, None)

        build_preview.assert_called_once_with(
            "转换结果", include_solution=True, only_numbers=None,
            boundary_mode="whitelist", existing_fps=set(), all_cols=[])
        create_batch.assert_called_once()
        self.assertEqual(2, len(create_batch.call_args.args[0]))
        self.assertEqual("imported", grp["reviewed"])
        self.assertEqual(2, grp["imported_count"])
        self.assertNotIn("auto_review_blocked", grp)

    def test_structure_collection_auto_import_requires_complete_numbered_unit(self):
        batch_id = "auto-import-collection-number-guard"
        grp = {
            "gid": 0,
            "filename": "专题一.pdf",
            "md": "转换结果",
            "note": "",
            "include_solution": True,
            "only_numbers": None,
            "collection_strategy": "ocr_structure",
            "collection_unit": True,
            "status": "done",
            "reviewed": None,
            "attempt": 0,
        }
        batch = {"status": "done", "groups": [grp], "files_cleaned": False}
        preview = [
            {"body": "无题号正文", "solution": "", "type": "解答题",
             "dup": None, "number": None},
            {"body": "第二题", "solution": "", "type": "解答题",
             "dup": None, "number": 2},
        ]
        app_module._batch_jobs[batch_id] = batch
        try:
            with mock.patch.object(
                    app_module, "_build_import_preview",
                    return_value=(preview, [], None)), \
                    mock.patch.object(app_module, "_persist_batch") as persist, \
                    mock.patch.object(
                        app_module.filestore, "create_questions_batch",
                        side_effect=AssertionError("合集子组题号不完整时不得免审写入")):
                app_module._auto_import_after_convert(
                    batch_id, grp, attempt=0, md_snapshot=grp["md"])
        finally:
            app_module._batch_jobs.pop(batch_id, None)

        self.assertTrue(grp["auto_review_blocked"])
        self.assertIsNone(grp["reviewed"])
        self.assertIn("有 1 道取不到题号", grp["note"])
        persist.assert_called_once_with(batch_id, batch)

    def test_batch_import_can_target_existing_parent_folder(self):
        parent = app_module.filestore.get_or_create_collection("高考卷", "")
        batch = {
            "target_parent_id": parent,
            "pack_folder_name": "2025",
            "per_task_folder": True,
        }
        grp = {"gid": 0, "filename": "2025年全国I卷.pdf"}
        folder = app_module._auto_import_folder(batch, grp)
        self.assertEqual(folder, "高考卷/2025/2025年全国I卷")
        self.assertTrue((config.BANK_DIR / "高考卷" / "2025"
                         / "2025年全国I卷").is_dir())

    def test_reviewed_year_batch_also_uses_one_folder_per_paper(self):
        parent = app_module.filestore.get_or_create_collection("高考卷", "")
        batch = {
            "target_parent_id": parent,
            "pack_folder_name": "2024",
            "auto_import": False,
            "per_task_folder": False,
        }
        folder = app_module._auto_import_folder(
            batch, {"gid": 0, "filename": "2024年北京卷.pdf"})
        self.assertEqual(folder, "高考卷/2024/2024年北京卷")

    def test_batch_create_rejects_missing_target_parent(self):
        client = app_module.app.test_client()
        data = {
            "target_parent_id": "不存在/越界",
            "groups[0][file]": (io.BytesIO(b"%PDF-1.7\n"), "卷子.pdf"),
        }
        response = client.post(
            "/batch-convert/create", data=data,
            headers={"X-CSRF-Token": app_module._WRITE_TOKEN},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertIn("目标父文件夹不存在", response.get_json()["error"])

    def test_import_page_defers_target_parent_folders(self):
        app_module.filestore.get_or_create_collection("高考卷", "")
        # 目录选择器已经改为按需展开：首页只输出根节点，不能构造完整目录树。
        with mock.patch.object(
                app_module.filestore, "list_collections_tree",
                side_effect=AssertionError("打开批量导入不应递归目录树")):
            response = app_module.app.test_client().get("/import")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="batch-target-parent"', response.data)
        self.assertIn(b'id="batch-target-parent-tree"', response.data)
        self.assertIn('题库根目录'.encode("utf-8"), response.data)
        self.assertNotIn('高考卷'.encode("utf-8"), response.data)
        children = app_module.app.test_client().get(
            "/collections/children", query_string={"parent": ""})
        self.assertEqual(children.status_code, 200)
        self.assertIn("高考卷", {row["name"] for row in children.get_json()["children"]})

    def test_import_card_accepts_images_and_writes_default_vertical_layout(self):
        def png(color):
            stream = io.BytesIO()
            Image.new("RGB", (24, 16), color).save(stream, format="PNG")
            stream.seek(0)
            return stream

        marker = f"导入配图回归-{time.time_ns()}"
        response = app_module.app.test_client().post(
            "/import",
            data={
                "action": "confirm", "keep": "0", "body_0": marker,
                "solution_0": "", "type_0": "填空题",
                "img_mode_0": "after", "img_flow_0": "column",
                "img_mode_touched_0": "1", "img_flow_touched_0": "1",
                "images_0": [(png("red"), "示意图一.png"),
                              (png("blue"), "示意图二.png")],
            },
            headers={"X-CSRF-Token": app_module._WRITE_TOKEN},
            content_type="multipart/form-data")

        self.assertEqual(response.status_code, 302)
        rec = next(q for q in app_module.filestore.list_questions()
                   if marker in q["body"])
        names = app_module._QIMG_RE.findall(rec["body"])
        self.assertEqual(len(names), 2)
        self.assertEqual(rec["img_split"], "after")
        self.assertEqual(rec["img_layouts"], [{"i": 0, "stack": True}])
        self.assertEqual(rec["folder"], "临时卡片")
        self.assertRegex(rec["title"], r"^临时卡\d+$")
        self.assertTrue(all((config.ASSETS_DIR / name).is_file() for name in names))

    def test_loaded_question_layout_and_type_actions_do_not_scan_vault(self):
        folder = app_module.filestore.get_or_create_collection("局部刷新测试", "")
        qid = app_module.filestore.create_question(
            "函数图像如下，零点为______。\n\n![[local-refresh.png]]",
            qtype="填空题", folder=folder)
        # 题卡已经加载后，该题路径应在缓存中；随后所有卡片按钮只能读这一份文件。
        app_module.filestore.collection_records_snapshot(folder)
        client = app_module.app.test_client()
        headers = {
            "X-CSRF-Token": app_module._WRITE_TOKEN,
            "Accept": "application/json",
        }
        with mock.patch.object(
                app_module.filestore, "_all_records",
                side_effect=AssertionError("单题按钮不应扫描题库")):
            layout = client.post(
                f"/question/{qid}/img_split", json={"mode": "opts"},
                headers=headers)
            type_set = client.post(
                f"/question/{qid}/type",
                json={"type": "填空题", "card_sort": "browse"},
                headers=headers)

        self.assertEqual(layout.status_code, 200)
        self.assertIn('class="q-split"', layout.get_json()["body_html"])
        self.assertEqual(type_set.status_code, 200)
        self.assertIn('class="card', type_set.get_json()["card_html"])

    def test_three_image_position_direction_and_swap_are_local(self):
        qid = app_module.filestore.create_question(
            "题干\n\n![[a.png]]\n\n![[b.png]]\n\n![[c.png]]\n\nA. 1 B. 2 C. 3 D. 4",
            qtype="单选题")
        client = app_module.app.test_client()
        headers = {"X-CSRF-Token": app_module._WRITE_TOKEN}

        placed = client.post(
            f"/question/{qid}/img_split", json={"mode": "between"},
            headers=headers)
        self.assertEqual(placed.status_code, 200)
        html = placed.get_json()["body_html"]
        self.assertLess(html.index('class="q-stem"'), html.index('class="q-fig-row"'))
        self.assertLess(html.index('class="q-fig-row"'), html.index('class="q-opts"'))
        self.assertEqual(placed.get_json()["groups"][0]["ids"], [0, 1, 2])

        stacked = client.post(
            f"/question/{qid}/img_stack",
            json={"index": 2, "stack": True, "field": "body"}, headers=headers)
        self.assertEqual(stacked.status_code, 200)
        self.assertIn('class="q-fig-stack"', stacked.get_json()["body_html"])

        swapped = client.post(
            f"/question/{qid}/img_swap",
            json={"index": 0, "with": 1, "field": "body"}, headers=headers)
        self.assertEqual(swapped.status_code, 200)
        self.assertFalse(swapped.get_json()["groups"][0]["row"])
        rec = app_module.filestore.get_question(qid)
        self.assertLess(rec["body"].index("b.png"), rec["body"].index("a.png"))

    def test_bulk_difficulty_uses_selected_ids_without_global_scan(self):
        folder = app_module.filestore.get_or_create_collection("批量按钮测试", "")
        qid = app_module.filestore.create_question(
            "批量难度测试题", qtype="填空题", folder=folder)
        app_module.filestore.collection_records_snapshot(folder)
        app_module.filestore.clear_selected()
        app_module.filestore.select_ids([qid])
        try:
            with mock.patch.object(
                    app_module.filestore, "_all_records",
                    side_effect=AssertionError("批量按钮不应扫描题库")):
                response = app_module.app.test_client().post(
                    "/difficulty_selected", data={"level": "4"},
                    headers={
                        "X-CSRF-Token": app_module._WRITE_TOKEN,
                        "Accept": "application/json",
                    })
                updated = app_module.filestore.get_question(qid)
        finally:
            app_module.filestore.clear_selected()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ids"], [qid])
        self.assertEqual(updated["difficulty"], "4")

    def test_export_source_is_optional_and_inserted_before_question_body(self):
        folder = app_module.filestore.get_or_create_collection("题源导出测试", "")
        qid = app_module.filestore.create_question(
            "原始题干", qtype="填空题", source="2026 $A_1$ 卷", folder=folder)
        app_module.filestore.clear_selected()
        app_module.filestore.select_ids([qid])
        try:
            with app_module.app.test_request_context(
                    "/export", method="POST", data={"scope": "selected"}):
                plain = app_module._collect_questions("selected")
                params = app_module._read_export_params()
            with app_module.app.test_request_context(
                    "/export", method="POST",
                    data={"scope": "selected", "show_source": "1"}):
                sourced = app_module._collect_questions("selected", show_source=True)
                sourced_params = app_module._read_export_params()
        finally:
            app_module.filestore.clear_selected()

        self.assertEqual(plain[0]["body"], "原始题干")
        self.assertFalse(params["show_source"])
        self.assertTrue(sourced_params["show_source"])
        self.assertEqual(sourced[0]["body"], r"【2026 \$A\_1\$ 卷】原始题干")

    def test_bulk_properties_update_selected_questions_and_append_notes(self):
        folder = app_module.filestore.get_or_create_collection("批量属性测试", "")
        first = app_module.filestore.create_question(
            "第一题", qtype="单选题", source="旧题源", note="原备注", folder=folder)
        second = app_module.filestore.create_question(
            "第二题", qtype="填空题", folder=folder)
        app_module.filestore.clear_selected()
        app_module.filestore.select_ids([first, second])
        try:
            response = app_module.app.test_client().post(
                "/questions/bulk-update",
                data={
                    "type": "解答题", "difficulty": "4", "starred": "on",
                    "source_mode": "set", "source": "统一题源",
                    "note_mode": "append", "note": "批量备注",
                },
                headers={
                    "X-CSRF-Token": app_module._WRITE_TOKEN,
                    "Accept": "application/json",
                })
            first_row = app_module.filestore.get_question(first)
            second_row = app_module.filestore.get_question(second)
        finally:
            app_module.filestore.clear_selected()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.get_json()["ids"]), {first, second})
        self.assertEqual(first_row["type"], "解答题")
        self.assertEqual(first_row["difficulty"], "4")
        self.assertTrue(first_row["starred"])
        self.assertEqual(first_row["source"], "统一题源")
        self.assertEqual(first_row["note"], "原备注\n\n批量备注")
        self.assertEqual(second_row["note"], "批量备注")

    def test_question_card_shows_source_and_always_exposes_note_toggle(self):
        folder = app_module.filestore.get_or_create_collection("题卡属性展示", "")
        app_module.filestore.create_question(
            "无备注题", qtype="填空题", source="灰色题源", folder=folder)
        html = app_module.app.test_client().get(
            "/", query_string={"collection": folder}).get_data(as_text=True)
        self.assertIn('class="card-star-source"', html)
        self.assertIn('class="card-head-source"', html)
        self.assertIn('title="题源：灰色题源">灰色题源</span>', html)
        self.assertIn('class="q-note is-empty"', html)
        self.assertIn('<summary>备注<span>未填写</span></summary>', html)
        self.assertIn('data-inline-focus="note"', html)

    def test_selected_export_collects_targeted_ids_without_global_scan(self):
        folder = app_module.filestore.get_or_create_collection("定向导出测试", "")
        first = app_module.filestore.create_question(
            "定向导出第一题", qtype="填空题", folder=folder, number=1)
        second = app_module.filestore.create_question(
            "定向导出第二题", qtype="填空题", folder=folder, number=2)
        app_module.filestore.clear_selected()
        app_module.filestore.select_ids([second, first])
        try:
            with (mock.patch.object(
                    app_module.filestore, "_all_records",
                    side_effect=AssertionError("选题篮导出不应扫描整座题库")),
                  app_module.app.test_request_context(
                      "/export", method="POST", data={"scope": "selected"})):
                questions = app_module._collect_questions("selected")
        finally:
            app_module.filestore.clear_selected()

        self.assertEqual([question["id"] for question in questions], [first, second])

    def test_select_all_in_collection_scans_only_that_subtree(self):
        folder = app_module.filestore.get_or_create_collection("局部全选测试", "")
        qid = app_module.filestore.create_question(
            "局部全选题", qtype="填空题", folder=folder)
        app_module.filestore.clear_selected()
        try:
            with mock.patch.object(
                    app_module.filestore, "_all_records",
                    side_effect=AssertionError("单卷全选不应扫描全库")):
                response = app_module.app.test_client().post(
                    "/select_all", data={"collection": folder},
                    headers={
                        "X-CSRF-Token": app_module._WRITE_TOKEN,
                        "Accept": "application/json",
                    })
        finally:
            app_module.filestore.clear_selected()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["matched"], 1)
        self.assertEqual(response.get_json()["count"], 1)

    def test_select_all_requires_explicit_scope_for_full_library(self):
        qid = app_module.filestore.create_question(
            "全库范围保护题", qtype="填空题")
        app_module.filestore.clear_selected()
        headers = {
            "X-CSRF-Token": app_module._WRITE_TOKEN,
            "Accept": "application/json",
        }
        try:
            unsafe = app_module.app.test_client().post(
                "/select_all", data={}, headers=headers)
            self.assertEqual(unsafe.status_code, 400)
            self.assertEqual(app_module.filestore.count_selected(), 0)

            explicit = app_module.app.test_client().post(
                "/select_all", data={"all": "1"}, headers=headers)
            self.assertEqual(explicit.status_code, 200)
            self.assertIn(qid, app_module.filestore.selected_ids())
        finally:
            app_module.filestore.clear_selected()

    def test_folder_mutations_return_json_for_local_tree_refresh(self):
        client = app_module.app.test_client()
        headers = {
            "X-CSRF-Token": app_module._WRITE_TOKEN,
            "Accept": "application/json",
        }
        created = client.post(
            "/collections/create", data={"name": "局部目录父"}, headers=headers)
        self.assertEqual(created.status_code, 200)
        parent_id = created.get_json()["id"]

        child = client.post(
            "/collections/create",
            data={"name": "局部目录子", "parent_id": parent_id}, headers=headers)
        child_id = child.get_json()["id"]
        renamed = client.post(
            f"/collections/{child_id}/rename", data={"name": "局部目录新"},
            headers=headers)
        renamed_id = renamed.get_json()["id"]
        moved = client.post(
            f"/collections/{renamed_id}/move", json={"parent_id": None},
            headers=headers)
        moved_id = moved.get_json()["id"]
        deleted = client.post(
            f"/collections/{moved_id}/delete", headers=headers)

        self.assertEqual(child.status_code, 200)
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed_id, f"{parent_id}/局部目录新")
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved_id, "局部目录新")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["parent_id"], "")

    def test_folder_tree_supports_initial_lazy_and_root_drag_targets(self):
        folder_id = app_module.filestore.get_or_create_collection("拖动目录", "")
        html = app_module.app.test_client().get("/?all=1").get_data(as_text=True)
        folder_markup = re.search(
            rf'data-folder-id="{re.escape(folder_id)}".*?'
            r'<div class="folder-row" draggable="true">(.*?)</div>',
            html, re.S)
        self.assertIsNotNone(folder_markup)
        self.assertNotIn('▰', folder_markup.group(1))
        self.assertNotIn('class="folder-item folder-root', html)
        self.assertNotIn('>全部题目</a>', html)

        template = (config.BASE_DIR / "templates" / "index.html").read_text(
            encoding="utf-8")
        lazy_renderer = re.search(
            r"function renderLazyFolder\(node\) \{(.*?)"
            r"function insertFolderNodeSorted",
            template, re.S)
        self.assertIsNotNone(lazy_renderer)
        self.assertNotIn("▰", lazy_renderer.group(1))
        self.assertIn("row.draggable = true", template)
        self.assertIn("const row = event.target.closest('.folder-row')", template)
        self.assertIn(
            "row.querySelector(':scope > a.folder-link[data-ajax=\"1\"]')",
            template)
        self.assertIn("application/x-quizforge-folder", template)
        self.assertIn("isInvalidFolderDrop(sourceId, cid)", template)
        self.assertIn("remapActiveCollection(sourceId, data.id, true)", template)
        self.assertIn("loadFolderFragment(location.href, false, true)", template)

    def test_question_page_tabs_replace_current_page_and_add_only_on_plus(self):
        folder_id = app_module.filestore.get_or_create_collection("多标签目录", "")
        html = app_module.app.test_client().get(
            "/", query_string={"collection": folder_id}).get_data(as_text=True)
        self.assertIn('id="collection-tabs"', html)
        self.assertIn('role="tablist" aria-label="已打开的题集"', html)
        self.assertIn('class="collection-tab-label">多标签目录</span>', html)
        self.assertIn('data-collection-tab-add', html)

        template = (config.BASE_DIR / "templates" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn("quizforge:collection-tabs:v2", template)
        self.assertIn("function addBlankCollectionTab()", template)
        self.assertIn("let tab = activeCollectionTab()", template)
        self.assertIn("tab.id = id", template)
        self.assertNotIn("collectionTabsState.tabs.find(row => row.id === id)", template)
        self.assertIn("captureCollectionTabPosition", template)
        self.assertIn("restoreCollectionTabPosition", template)
        self.assertIn("await loadNextQuestionPage()", template)
        self.assertIn("void openCollectionTab(link.href", template)
        self.assertIn("closeCollectionTab", template)
        self.assertIn("remapCollectionTabs(fid, data.id, true)", template)
        self.assertIn("removeCollectionTabs(fid)", template)
        self.assertIn("const next = deletedCollectionFallback(fid, data.parent_id || '')", template)
        self.assertIn("history.replaceState({qfFragment: true}, '', next.url)", template)
        deleted_fallback = re.search(
            r"function deletedCollectionFallback\(.*?\n\}", template, re.S)
        self.assertIsNotNone(deleted_fallback)
        self.assertNotIn("searchParams.set('all', '1')", deleted_fallback.group(0))
        self.assertIn("removeFolderTreeItem(li)", template)
        self.assertIn("function remapFolderTreeItem(", template)
        self.assertIn("function moveFolderTreeItem(", template)
        self.assertIn("remapFolderTreeItem(li, fid, data.id", template)
        self.assertIn("moveFolderTreeItem(", template)
        self.assertIn("sourceId, data.id, cid", template)
        self.assertNotIn("await loadFolderFragment(next.url, next.changed, true);\n    flashToast(data.message || '题集已移入回收站')", template)
        self.assertIn("insertCreatedFolder(data)", template)
        self.assertNotIn("await loadFolderFragment(location.href, false, true);\n    flashToast(data.message || '文件夹已新建')", template)
        self.assertIn('id="bulk-folder-tree" role="tree"', template)
        self.assertIn("loadBulkFolderChildren", template)
        self.assertIn("function positionBulkFolderPopover()", template)
        self.assertIn("bulkFolderTrigger.getBoundingClientRect()", template)
        self.assertIn("window.addEventListener('resize', scheduleBulkFolderPopoverPosition)", template)
        self.assertIn("bulk-folder-twist${node.has_children ? '' : ' is-empty'}", template)
        self.assertNotIn("bulk-folder-twist${node.has_children ? '' : ' empty'}", template)
        self.assertIn("class=\"bulk-selected-list\"", html)
        self.assertIn('id="bulk-drawer-trigger"', html)
        self.assertIn('<dialog id="bulkbar"', html)
        self.assertNotIn('class="question-bulk-resizer"', html)
        self.assertIn("body.className = 'bulk-selected-body'", template)
        self.assertIn("bulkbar.showModal()", template)
        self.assertIn("body.classList.add('bulk-drawer-open')", template)
        self.assertNotIn("classList.toggle('bulk-active'", template)
        self.assertIn("syncBulkCollectionAction(activeCollectionTab()?.id || '')", template)
        self.assertIn(
            "loadFolderFragment(tab.url, true, false, false, ownsTargetTab)",
            template)
        self.assertIn("if (commitGuard && !commitGuard()) return false", template)
        self.assertIn("event.submitter?.matches('button[type=\"submit\"]')", template)

        stylesheet = (config.BASE_DIR / "static" / "style.css").read_text(
            encoding="utf-8")
        popover_rule = re.search(
            r"\.bulk-folder-popover\s*\{([^}]*)\}", stylesheet, re.S)
        self.assertIsNotNone(popover_rule)
        self.assertIn("position: fixed", popover_rule.group(1))
        self.assertIn("width: min(380px, calc(100vw - 24px))", popover_rule.group(1))
        self.assertIn("overflow: auto", popover_rule.group(1))
        self.assertNotIn(".bulkbar.show", stylesheet)
        self.assertRegex(
            stylesheet,
            r"\.bulkbar\s*\{[^}]*position:\s*fixed;[^}]*"
            r"width:\s*min\(520px,\s*calc\(100vw\s*-\s*24px\)\);",
        )
        self.assertIn(".bulkbar[open] { display: flex; }", stylesheet)
        self.assertIn(
            "main:has(> .bulk-drawer-trigger) .layout { padding-right: 48px; }",
            stylesheet)
        self.assertRegex(
            stylesheet,
            r"\.bulk-drawer-trigger\s*\{[^}]*width:\s*40px;[^}]*"
            r"border-radius:\s*8px 0 0 8px;",
        )
        self.assertIn(".bulk-drawer-feedback .toast", stylesheet)
        self.assertIn(
            "body:has(#export-panel:not(.hidden)) .new-question-fab,",
            stylesheet)
        self.assertIn(
            "body:has(.card.inline-editing) .new-question-fab { display: none; }",
            stylesheet)

        tabs_rules = re.findall(
            r"(?:^|\n)\.collection-tabs\s*\{([^}]*)\}", stylesheet, re.S)
        self.assertEqual(len(tabs_rules), 1)
        self.assertIn("position: sticky", tabs_rules[0])
        self.assertIn("top: 12px", tabs_rules[0])
        self.assertRegex(
            stylesheet,
            r"html\.desktop-host \.collection-tabs,\s*"
            r"html\.desktop-host\.embedded-view \.collection-tabs\s*"
            r"\{\s*top:\s*10px;")
        self.assertRegex(
            stylesheet,
            r"html\.desktop-host \.toolbar\s*\{\s*top:\s*54px;")
        self.assertRegex(
            stylesheet,
            r"html\.desktop-host\.embedded-view \.toolbar\s*"
            r"\{\s*top:\s*54px;")

    def test_question_drag_has_viewport_edge_auto_scroll(self):
        template = (config.BASE_DIR / "templates" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn("stepQuestionDragAutoScroll", template)
        self.assertIn("window.scrollBy(0, delta)", template)
        self.assertIn("if (dragQuestionId) updateQuestionDragAutoScroll", template)
        self.assertIn("stopQuestionDragAutoScroll();", template)

    def test_selection_action_tabs_share_compact_rail(self):
        template = (config.BASE_DIR / "templates" / "index.html").read_text(
            encoding="utf-8")
        stylesheet = (config.BASE_DIR / "static" / "style.css").read_text(
            encoding="utf-8")
        self.assertIn('class="bulk-action-stack" id="bulk-action-stack"', template)
        self.assertIn('role="group"', template)
        self.assertIn('id="bulk-drawer-trigger"', template)
        self.assertIn('id="bulk-action-rail"', template)
        self.assertIn("function closeBulkActionSections()", template)
        self.assertIn("bulkActionStack?.classList.toggle('has-open-panel'", template)
        self.assertIn("main:has(.bulk-action-stack:not([hidden])) .layout { padding-right: 48px; }", stylesheet)
        self.assertIn(".bulk-action-panel:not([hidden])", stylesheet)
        self.assertIn(".bulk-action-panel .bulk-action-body form > .btn", stylesheet)
        self.assertIn("width: auto;", stylesheet)

    def test_selection_only_changes_state_and_never_reorders_cards(self):
        template = (config.BASE_DIR / "templates" / "index.html").read_text(
            encoding="utf-8")
        handler = re.search(
            r"listEl\?\.addEventListener\('change', (?:async )?event => \{"
            r"(.*?)\n\}\);",
            template, re.S)
        self.assertIsNotNone(handler)
        self.assertIn("card?.classList.toggle('selected'", handler.group(1))
        self.assertNotIn("after(card)", handler.group(1))
        self.assertNotIn("prepend(card)", handler.group(1))

    def test_selection_mutations_are_serialized_and_clear_all_visible_cards(self):
        template = (config.BASE_DIR / "templates" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn("let selectionMutationTail = Promise.resolve()", template)
        self.assertIn("const run = selectionMutationTail.then(task, task)", template)

        clear_start = template.index(
            "document.addEventListener('submit', async event => {")
        clear_end = template.index("function syncBulkPropertyInputs()", clear_start)
        clear_handler = template[clear_start:clear_end]
        self.assertIn("selectionClearInFlight = true", clear_handler)
        self.assertIn("setVisibleCardsSelected(false)", clear_handler)
        self.assertIn("enqueueSelectionMutation(performRequest)",
                      clear_handler)
        self.assertNotIn("querySelectorAll('.card.selected')",
                         clear_handler)

    def test_single_question_toggle_reports_json_success(self):
        qid = app_module.filestore.create_question("单题勾选响应测试")
        app_module.filestore.clear_selected()
        try:
            response = app_module.app.test_client().post(
                f"/question/{qid}/toggle",
                headers={"X-CSRF-Token": app_module._WRITE_TOKEN})
        finally:
            app_module.filestore.clear_selected()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.get_json()["selected"], 1)

    def test_collection_add_exposes_copy_and_move_semantics(self):
        source = app_module.filestore.get_or_create_collection("移动复制来源", "")
        target = app_module.filestore.get_or_create_collection("移动复制目标", "")
        first = app_module.filestore.create_question(
            "保留原题", solution="保留解析", note="保留备注", folder=source,
            number=1)
        second = app_module.filestore.create_question(
            "移动原题", folder=source, number=2)
        client = app_module.app.test_client()
        headers = {
            "X-CSRF-Token": app_module._WRITE_TOKEN,
            "Accept": "application/json",
        }
        app_module.filestore.clear_selected()
        try:
            app_module.filestore.select_ids([first])
            with mock.patch.object(
                    app_module.filestore, "_all_records",
                    side_effect=AssertionError("复制路由不应扫描整座题库")):
                copied = client.post(
                    f"/collections/{target}/add", data={"mode": "copy"},
                    headers=headers)
            app_module.filestore.clear_selected()
            app_module.filestore.select_ids([second])
            with mock.patch.object(
                    app_module.filestore, "_all_records",
                    side_effect=AssertionError("移动路由不应扫描整座题库")):
                moved = client.post(
                    f"/collections/{target}/add", data={"mode": "move"},
                    headers=headers)
            source_rows = app_module.filestore.collection_records_snapshot(
                source, recursive=False)
            target_rows = app_module.filestore.collection_records_snapshot(
                target, recursive=False)
        finally:
            app_module.filestore.clear_selected()

        self.assertEqual(copied.status_code, 200)
        self.assertEqual(copied.get_json()["mode"], "copy")
        self.assertEqual(len(copied.get_json()["created"]), 1)
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.get_json()["moved"], [second])
        self.assertEqual([row["id"] for row in source_rows], [first])
        self.assertEqual(len(target_rows), 2)
        self.assertEqual(target_rows[0]["body"], "保留原题")
        self.assertEqual(target_rows[1]["id"], second)

    def test_image_toolbar_explains_export_width_and_offers_presets(self):
        card = (config.BASE_DIR / "templates" / "_question_card.html").read_text(
            encoding="utf-8")
        script = (config.BASE_DIR / "static" / "js" / "image-layout.js").read_text(
            encoding="utf-8")
        self.assertIn("导出宽度", card)
        self.assertIn("窗口变化只改变屏幕预览，最终 PDF 比例不变", card)
        self.assertIn('class="img-width-chip"', card)
        self.assertIn("bar.querySelectorAll('.img-width-chip')", script)

    def test_folder_move_rejects_escape_missing_and_reserved_targets(self):
        source = app_module.filestore.get_or_create_collection("移动边界源", "")
        client = app_module.app.test_client()
        headers = {"X-CSRF-Token": app_module._WRITE_TOKEN}

        for target in ("../outside", "不存在", "_handouts"):
            response = client.post(
                f"/collections/{source}/move", json={"parent_id": target},
                headers=headers)
            self.assertEqual(response.status_code, 400, target)
            self.assertTrue((config.BANK_DIR / source).is_dir(), target)

    def test_tag_rename_returns_json_for_local_card_refresh(self):
        qid = app_module.filestore.create_question(
            "标签局部刷新题", qtype="填空题", tags=["局部旧标签"])
        response = app_module.app.test_client().post(
            "/tags/局部旧标签/rename", data={"name": "局部新标签"},
            headers={
                "X-CSRF-Token": app_module._WRITE_TOKEN,
                "Accept": "application/json",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["new_name"], "局部新标签")
        self.assertIn("局部新标签", app_module.filestore.get_question(qid)["tags"])

    def test_tikz_coordinate_math_is_wrapped_before_compile(self):
        source = r"""\begin{tikzpicture}
\coordinate (A) at (0,0);
\fill (2*sqrt(6), 0) circle (1.5pt);
\draw (0,cos(30)*2) -- ($(A)!0.5!(B)$);
\end{tikzpicture}"""
        fixed = tikz_render.normalize_coordinate_math(source)
        self.assertIn(r"\fill ({2*sqrt(6)}, 0)", fixed)
        self.assertIn(r"\draw (0,{cos(30)*2})", fixed)
        self.assertIn(r"\coordinate (A) at (0,0)", fixed)
        self.assertIn(r"($(A)!0.5!(B)$)", fixed)
        self.assertEqual(tikz_render.normalize_coordinate_math(fixed), fixed)

    def test_image_redraw_uses_embedded_dialog_instead_of_native_prompt(self):
        script = (config.BASE_DIR / "static" / "js" / "image-redraw.js").read_text(
            encoding="utf-8")
        # 注释需要保留根因说明；这里只禁止把它重新写回可执行代码行。
        executable = "\n".join(
            line for line in script.splitlines()
            if not line.lstrip().startswith("//"))
        self.assertNotIn("window.prompt(", executable)
        self.assertIn("redraw-request-dialog", script)
        self.assertIn("开始重绘", script)

    def test_import_defaults_to_no_ai(self):
        self.assertEqual(app_module._parse_block_mode(""), "no_ai")
        self.assertEqual(app_module._parse_block_mode("unknown"), "no_ai")
        html = app_module.app.test_client().get("/import").get_data(as_text=True)
        self.assertIn(
            '<option value="no_ai" selected>全部不送入 AI，机械渲染（默认）</option>',
            html,
        )
        self.assertNotIn('<option value="all_ai" selected>', html)

    def test_import_boundary_mode_defaults_to_auto_and_exposes_whitelist(self):
        self.assertEqual("auto", app_module._parse_boundary_mode(""))
        self.assertEqual("auto", app_module._parse_boundary_mode("unknown"))
        self.assertEqual("whitelist", app_module._parse_boundary_mode("whitelist"))
        html = app_module.app.test_client().get("/import").get_data(as_text=True)
        self.assertIn('id="batch-boundary-mode"', html)
        self.assertIn('<option value="auto" selected>智能识别</option>', html)
        self.assertIn('<option value="whitelist">强制白名单</option>', html)

    def test_legacy_convert_start_persists_and_passes_whitelist_mode(self):
        image = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(image, format="PNG")
        image.seek(0)
        try:
            with mock.patch.object(
                    app_module, "_history_record_for_sources",
                    return_value="history-legacy"), \
                    mock.patch.object(app_module, "_persist_job"), \
                    mock.patch.object(app_module.threading, "Thread") as thread:
                response = app_module.app.test_client().post(
                    "/convert/start",
                    data={
                        "file": (image, "单页.png"),
                        "engine": "whole",
                        "boundary_mode": "whitelist",
                    }, content_type="multipart/form-data",
                    headers={"X-CSRF-Token": app_module._WRITE_TOKEN})

            self.assertEqual(200, response.status_code,
                             response.get_data(as_text=True))
            job_id = response.get_json()["job_id"]
            job = app_module._jobs[job_id]
            self.assertEqual("whitelist", job["boundary_mode"])
            self.assertEqual(1, job["image_page_count"])
            args = thread.call_args.kwargs["args"]
            self.assertIs(app_module._convert_worker,
                          thread.call_args.kwargs["target"])
            self.assertEqual("block", args[7])
            self.assertEqual("whitelist", args[10])
            self.assertEqual((1, 0), args[11:13])
        finally:
            job_id = locals().get("job_id")
            job = app_module._jobs.pop(job_id, None) if job_id else None
            if job and job.get("path"):
                Path(job["path"]).unlink(missing_ok=True)

    def test_health_exposes_identity_for_safe_plugin_restart(self):
        data = app_module.app.test_client().get("/healthz").get_json()
        self.assertEqual(data["app"], "quizforge")
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(Path(data["project"]), config.BASE_DIR)

    def test_home_starts_blank_without_scanning_global_questions(self):
        app_module.filestore.create_question(
            "首页默认题卡回归", qtype="填空题", folder="首页回归")
        client = app_module.app.test_client()
        with (mock.patch.object(
                app_module.filestore, "list_question_paths",
                side_effect=AssertionError("空白首页不应枚举全库路径")),
              mock.patch.object(
                  app_module.filestore, "all_records_snapshot",
                  side_effect=AssertionError("空白首页不应解析全库题目"))):
            page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("未选择题集".encode("utf-8"), page.data)
        self.assertIn(b'data-total="0"', page.data)
        self.assertIn(b'data-blank="1"', page.data)
        self.assertIn(b'class="search-bar hidden"', page.data)
        self.assertIn(b'class="toolbar hidden"', page.data)
        self.assertIn(b'<option value="practice">', page.data)
        self.assertIn(b'<select name="paper_tone">', page.data)
        self.assertIn('米黄护眼'.encode("utf-8"), page.data)
        self.assertIn('页眉与页脚'.encode("utf-8"), page.data)

    def test_home_renders_only_one_folder_move_select(self):
        tree = [{
            "id": "2026", "name": "2026", "parent_id": "", "cnt": 0,
            "depth": 0, "children": [{
                "id": "2026/全国卷", "name": "全国卷", "parent_id": "2026",
                "cnt": 0, "depth": 1, "children": [{
                    "id": "2026/全国卷/分卷", "name": "分卷",
                    "parent_id": "2026/全国卷", "cnt": 0, "depth": 2,
                    "children": [],
                }],
                "children_loaded": False, "has_children": True,
            }], "children_loaded": True, "has_children": True,
        }]
        flat = [tree[0], tree[0]["children"][0],
                tree[0]["children"][0]["children"][0]]
        records = []
        with (mock.patch.object(app_module.filestore, "all_records_snapshot",
                                return_value=records) as snapshot,
              mock.patch.object(app_module.filestore, "all_tags",
                                return_value=[]) as all_tags,
              mock.patch.object(app_module.filestore, "list_navigation_tree",
                                return_value=tree) as build_tree,
              mock.patch.object(app_module.filestore, "count_selected",
                                return_value=0)):
            response = app_module.app.test_client().get("/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count('id="folder-move-select"'), 1)
        self.assertEqual(html.count('class="folder-move-select hidden"'), 1)
        self.assertIn('data-folder-id="2026"', html)
        self.assertIn('data-parent-id="2026" data-loaded="1"', html)
        self.assertIn('data-folder-id="2026/全国卷"', html)
        self.assertNotIn('data-folder-id="2026/全国卷/分卷"', html)
        snapshot.assert_not_called()
        all_tags.assert_not_called()
        build_tree.assert_called_once_with("")

    def test_collection_options_are_loaded_by_separate_endpoint(self):
        tree = [{
            "id": "年份", "name": "年份", "parent_id": "", "cnt": 0,
            "depth": 0, "children": [],
        }]
        with (mock.patch.object(app_module.filestore, "list_collections_tree",
                                return_value=tree) as build_tree,
              mock.patch.object(app_module.filestore, "all_collections",
                                return_value=tree) as flatten):
            response = app_module.app.test_client().get("/collections/options")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["collections"], [{
            "id": "年份", "name": "年份", "depth": 0,
        }])
        build_tree.assert_called_once_with([])
        flatten.assert_called_once_with(tree)

    def test_tags_api_loads_global_tags_on_demand(self):
        with mock.patch.object(app_module.filestore, "all_tags",
                               return_value=["高考", "函数"]) as all_tags:
            response = app_module.app.test_client().get("/api/tags")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["tags"], ["高考", "函数"])
        all_tags.assert_called_once_with()

    def test_parent_collection_defaults_to_recursive_questions_and_papers(self):
        parent = app_module.filestore.get_or_create_collection("性能年份", "")
        child = app_module.filestore.get_or_create_collection("试卷甲", parent)
        app_module.filestore.create_question(
            "父文件夹应显示的后代题目", qtype="解答题", folder=child)
        paper_path = config.BANK_DIR / child / "试卷甲.pdf"
        paper_path.write_bytes(b"%PDF-1.4\n%%EOF")

        with mock.patch.object(
                app_module.filestore, "collection_records_snapshot",
                side_effect=AssertionError("父文件夹默认路径流不应解析全部后代题目")) as snapshot:
            response = app_module.app.test_client().get(
                "/", query_string={"collection": parent})

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-overview="0"', html)
        self.assertIn("父文件夹应显示的后代题目", html)
        self.assertIn("试卷甲.pdf", html)
        self.assertIn("来自 试卷甲", html)
        self.assertNotIn("汇总显示全部题目（较慢）", html)
        snapshot.assert_not_called()

    def test_recursive_collection_loads_questions_in_30_item_pages(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            app_module.filestore._cache.clear()
            app_module.filestore.invalidate_scan_cache()
            app_module._question_pages.clear()
            ids = app_module.filestore.create_questions_batch(
                [{"body": f"无限滚动题目 {number}", "number": number}
                 for number in range(1, 66)],
                "高考卷/分页测试/测试卷",
            )
            app_module.filestore.clear_selected()
            client = app_module.app.test_client()
            first = client.get(
                "/", query_string={"collection": "高考卷/分页测试"})
            html = first.get_data(as_text=True)
            token_match = re.search(r'data-page-token="([0-9a-f]{32})"', html)

            self.assertEqual(first.status_code, 200)
            self.assertEqual(html.count('<article class="card '), 30)
            self.assertIn('data-total="65"', html)
            self.assertIn('data-loaded="30"', html)
            self.assertIsNotNone(token_match)
            token = token_match.group(1)

            try:
                # 快照建立后才改变勾选状态，后续页必须读取服务端最新状态，不能
                # 沿用建快照时的旧记录。
                app_module.filestore.select_ids(ids)
                second = client.get(
                    "/questions/page", query_string={"token": token, "offset": 30})
                second_data = second.get_json()
                self.assertEqual(second.status_code, 200)
                self.assertEqual(second_data["html"].count('<article class="card '), 30)
                self.assertEqual(
                    second_data["html"].count('<article class="card selected'), 30)
                self.assertEqual(len(re.findall(
                    r'class="sel-toggle"[^>]*\bchecked\b', second_data["html"], re.S)), 30)
                self.assertEqual(second_data["next_offset"], 60)
                self.assertFalse(second_data["done"])

                app_module.filestore.clear_selected()
                third = client.get(
                    "/questions/page", query_string={"token": token, "offset": 60})
                third_data = third.get_json()
                self.assertEqual(third.status_code, 200)
                self.assertEqual(third_data["html"].count('<article class="card '), 5)
                self.assertNotIn('<article class="card selected', third_data["html"])
                self.assertNotRegex(
                    third_data["html"], r'class="sel-toggle"[^>]*\bchecked\b')
                self.assertEqual(third_data["next_offset"], 65)
                self.assertTrue(third_data["done"])
            finally:
                app_module.filestore.clear_selected()

        app_module.filestore._cache.clear()
        app_module.filestore.invalidate_scan_cache()
        app_module._question_pages.clear()

    def test_question_page_rejects_unknown_snapshot(self):
        response = app_module.app.test_client().get(
            "/questions/page", query_string={"token": "不存在", "offset": 0})
        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.get_json()["ok"])

    def test_question_snapshot_failure_offers_explicit_reload_button(self):
        template = (config.BASE_DIR / "templates" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('id="question-scroll-retry" hidden', template)
        self.assertIn("加载失败：${error.message}`, false, true", template)
        self.assertIn("await loadFolderFragment(location.href, false)", template)
        self.assertNotIn("（点击重试）", template)

    def test_export_paper_tone_accepts_only_white_or_cream(self):
        with app_module.app.test_request_context(
                "/export", method="POST", data={"paper_tone": "cream"}):
            self.assertEqual(app_module._read_export_params()["paper_tone"],
                             "cream")
        with app_module.app.test_request_context(
                "/export", method="POST", data={"paper_tone": "red"}):
            self.assertEqual(app_module._read_export_params()["paper_tone"],
                             "white")

    def test_export_wimath_checkbox_parameter_and_service_port_forwarding(self):
        page = app_module.app.test_client().get("/?all=1")
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            b'<input type="checkbox" name="wimath_logo" value="1">',
            page.data,
        )
        with app_module.app.test_request_context(
                "/export", method="POST", data={"wimath_logo": "1"}):
            self.assertTrue(app_module._read_export_params()["wimath_logo"])
        with app_module.app.test_request_context(
                "/export", method="POST", data={}):
            self.assertFalse(app_module._read_export_params()["wimath_logo"])

        produced = config.OUTPUT_DIR / "mock-wimath.pdf"
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_bytes(b"%PDF-1.4\n")
        question = {
            "id": "wimath-export", "body": "WIMath 参数题", "solution": "",
            "type": "填空题", "source": "", "difficulty": "", "tags": [],
            "img_align": "", "img_width": None, "img_split": None,
            "img_layouts": [], "sol_img_split": None, "sol_img_layouts": [],
        }
        with (mock.patch.object(app_module, "_collect_questions",
                                return_value=[question]),
              mock.patch.object(app_module.service_ports, "export_document",
                                return_value=produced) as export_mock):
            preview = app_module.app.test_client().post(
                "/preview", data={"wimath_logo": "1"},
                headers={"X-CSRF-Token": app_module._WRITE_TOKEN})
            exported = app_module.app.test_client().post(
                "/export", data={"wimath_logo": "1", "fmt": "pdf"},
                headers={"X-CSRF-Token": app_module._WRITE_TOKEN})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(export_mock.call_count, 2)
        self.assertTrue(all(
            call.kwargs.get("wimath_logo") is True
            for call in export_mock.call_args_list
        ))

    def test_html_preview_returns_registered_inline_document(self):
        question = {
            "id": "html-preview", "body": "近似预览题 $x^2$", "solution": "答案",
            "type": "填空题", "difficulty": "3", "img_align": "",
            "img_width": None, "img_split": None, "img_layouts": [],
            "sol_img_split": None, "sol_img_layouts": [],
        }
        with (mock.patch.object(app_module, "_collect_questions",
                                return_value=[question]),
              mock.patch.object(
                  app_module.service_ports, "export_document",
                  side_effect=AssertionError("HTML 近似预览不应启动 TeX"))):
            response = app_module.app.test_client().post(
                "/preview", data={"preview_kind": "html", "title": "近似预览"},
                headers={"X-CSRF-Token": app_module._WRITE_TOKEN})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        preview = app_module.app.test_client().get(payload["url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.mimetype, "text/html")
        self.assertIn("近似预览题", preview.get_data(as_text=True))
        self.assertIn("katex.min.js", preview.get_data(as_text=True))
        preview.close()

    def test_single_question_tex_zip_uses_one_record_and_download_name(self):
        qid = app_module.filestore.create_question(
            "单题导出正文", solution="单题导出解析", note="内部备注不应导出",
            qtype="解答题", number=6)
        produced = config.OUTPUT_DIR / "single-question.zip"
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

        with (mock.patch.object(
                app_module.filestore, "list_questions",
                side_effect=AssertionError("单题导出不应扫描整座题库")),
              mock.patch.object(
                  app_module.service_ports, "export_document",
                  return_value=produced) as export_mock):
            response = app_module.app.test_client().post(
                f"/question/{qid}/export-tex-zip",
                headers={"X-CSRF-Token": app_module._WRITE_TOKEN})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["filename"], "第6题.tex.zip")
        self.assertEqual(export_mock.call_args.kwargs["fmt"], "zip")
        self.assertEqual(export_mock.call_args.kwargs["solution_mode"], "inline")
        exported_question = export_mock.call_args.args[0][0]
        self.assertEqual(exported_question["body"], "单题导出正文")
        self.assertNotIn("note", exported_question)
        download = app_module.app.test_client().get(payload["url"])
        download_status = download.status_code
        disposition = unquote(download.headers["Content-Disposition"])
        download.close()
        self.assertEqual(download_status, 200)
        self.assertIn("第6题.tex.zip", disposition)

    def test_homepage_exposes_docx_and_marks_pdf_only_controls(self):
        page = app_module.app.test_client().get("/?all=1")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b'<option value="docx">Word', page.data)
        self.assertIn(b'id="export-format"', page.data)
        self.assertIn(b'id="paper-tone-field"', page.data)
        self.assertIn(b'id="wimath-logo-field"', page.data)
        self.assertIn(b'id="word-export-hint"', page.data)

    def test_docx_export_registers_office_filename_and_mime(self):
        produced = config.OUTPUT_DIR / "mock.docx"
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_bytes(b"docx-route-fixture")
        question = {
            "id": "q1", "body": "题干", "type": "填空题",
            "difficulty": "3", "solution": "", "img_align": "",
            "img_width": None, "img_split": None, "img_layouts": [],
            "sol_img_split": None, "sol_img_layouts": [],
        }
        with (mock.patch.object(app_module, "_collect_questions",
                                return_value=[question]),
              mock.patch.object(app_module.service_ports, "export_document",
                                return_value=produced) as export_mock):
            response = app_module.app.test_client().post(
                "/export", data={"fmt": "docx", "title": "月考"},
                headers={"X-CSRF-Token": app_module._WRITE_TOKEN})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["filename"], "月考.docx")
        self.assertEqual(export_mock.call_args.kwargs["fmt"], "docx")
        download = app_module.app.test_client().get(payload["url"])
        self.assertEqual(
            download.mimetype,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertIn(".docx", download.headers["Content-Disposition"])
        download.close()

    def test_export_rejects_unknown_format_before_service_call(self):
        with (mock.patch.object(app_module, "_collect_questions",
                                return_value=[{"id": "q1"}]),
              mock.patch.object(
                  app_module.service_ports, "export_document") as export_mock):
            response = app_module.app.test_client().post(
                "/export", data={"fmt": "exe"},
                headers={"X-CSRF-Token": app_module._WRITE_TOKEN})

        self.assertEqual(response.status_code, 400)
        self.assertIn("导出格式", response.get_json()["error"])
        self.assertFalse(export_mock.called)

    def test_export_collection_passes_question_difficulty(self):
        qid = app_module.filestore.create_question(
            "（1）第一问\n（2）第二问", qtype="解答题", difficulty="5")
        with app_module.app.test_request_context(
                "/export", method="POST", data={"scope": "all"}):
            questions = app_module._collect_questions("all")

        row = next(q for q in questions if q["id"] == qid)
        self.assertEqual(row["difficulty"], "5")

    def test_paper_link_uses_obsidian_bridge_with_browser_fallback(self):
        folder = app_module.filestore.get_or_create_collection("原卷桥测试", "")
        source = config.UPLOAD_DIR / "bridge.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"%PDF-1.7\n")
        app_module.filestore.store_paper(str(source), folder, "bridge.pdf", "exam")
        response = app_module.app.test_client().get(
            "/", query_string={"collection": folder})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="paper-open-obsidian"', response.data)
        self.assertIn(b"paper-open-library", response.data)
        self.assertIn(b"paper-open-question", response.data)
        self.assertIn(b"open-question-file", response.data)
        self.assertIn(b"post('open-file'", response.data)
        self.assertIn(b"post('location'", response.data)

    def test_batch_dashboard_exposes_group_controls(self):
        app_module._batch_jobs["page-batch"] = {
            "status": "converting", "running": 0, "cancelled": False,
            "created_at": time.time(), "groups": [{
                "gid": 0, "job_id": "page-job", "filename": "sample.pdf",
                "status": "pending", "reviewed": None, "cancelled": False,
                "md": None, "error": None, "imported_count": 0,
            }],
        }
        response = app_module.app.test_client().get("/batch/page-batch")
        self.assertEqual(response.status_code, 200)
        self.assertIn("中止该组".encode("utf-8"), response.data)
        self.assertIn("删除记录".encode("utf-8"), response.data)

    @unittest.skipUnless(shutil.which("node"), "未安装 Node，跳过内联 JS 语法检查")
    def test_rendered_inline_javascript_parses(self):
        app_module._batch_jobs.setdefault("js-batch", {
            "status": "converting", "running": 0, "cancelled": False,
            "created_at": time.time(), "groups": [{
                "gid": 0, "job_id": "js-job", "filename": "sample.pdf",
                "status": "pending", "reviewed": None, "cancelled": False,
                "md": None, "error": None, "imported_count": 0,
            }],
        })
        client = app_module.app.test_client()
        pages = [client.get("/").get_data(as_text=True),
                 client.get("/batch/js-batch").get_data(as_text=True)]
        for page in pages:
            scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                 page, flags=re.S | re.I)
            for script in scripts:
                result = subprocess.run(
                    [shutil.which("node"), "--check", "-"],
                    input=script.encode("utf-8"), capture_output=True, check=False)
                self.assertEqual(
                    result.returncode, 0,
                    result.stderr.decode("utf-8", errors="replace"))


class InlineEditorAndLibraryTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.headers = {
            "X-CSRF-Token": app_module._WRITE_TOKEN,
            "Content-Type": "application/json",
        }

    def test_inline_preview_is_read_only_and_save_replaces_one_card(self):
        qid = app_module.filestore.create_question(
            "原正文 $x$", solution="【解析】原解析", qtype="单选题",
            source="原题源", difficulty="2", tags=["旧标签"], note="原备注")
        rec = app_module.filestore.get_question(qid)
        path = config.BANK_DIR / rec["path"]
        before = path.read_bytes()
        payload = {
            "body": "新正文 $x^2$\n\nA. 1\n\nB. 2",
            "solution": "新解析 $2$",
            "note": "新备注：注意定义域 $x>0$",
            "type": "单选题",
            "source": "新题源",
            "difficulty": "4",
            "tags": "代数, 校内",
            "card_sort": "browse",
        }

        preview = self.client.post(
            f"/question/{qid}/preview", json=payload, headers=self.headers)
        self.assertEqual(preview.status_code, 200)
        self.assertIn("新正文", preview.get_json()["body_html"])
        self.assertIn("注意定义域", preview.get_json()["note_html"])
        self.assertEqual(path.read_bytes(), before)

        saved = self.client.post(
            f"/question/{qid}/inline", json=payload, headers=self.headers)
        self.assertEqual(saved.status_code, 200)
        card_html = saved.get_json()["card_html"]
        self.assertIn("inline-editor", card_html)
        self.assertIn("源码模式", card_html)
        self.assertIn("实时编译", card_html)
        self.assertIn("阅读模式", card_html)
        self.assertIn('<details class="q-note">', card_html)
        self.assertIn("注意定义域", card_html)
        updated = app_module.filestore.get_question(qid)
        self.assertEqual(updated["body"], payload["body"])
        self.assertEqual(updated["solution"], payload["solution"])
        self.assertEqual(updated["note"], payload["note"])
        self.assertEqual(updated["difficulty"], "4")
        self.assertEqual(updated["tags"], ["代数", "校内"])

    def test_inline_save_preserves_current_server_selection_in_replacement_card(self):
        qid = app_module.filestore.create_question("局部编辑勾选状态")
        app_module.filestore.clear_selected()
        try:
            app_module.filestore.select_ids([qid])
            response = self.client.post(
                f"/question/{qid}/inline",
                json={
                    "body": "局部编辑后的正文", "solution": "", "note": "",
                    "type": "填空题", "source": "", "difficulty": "",
                    "tags": "", "card_sort": "browse",
                },
                headers=self.headers,
            )
        finally:
            app_module.filestore.clear_selected()

        self.assertEqual(response.status_code, 200)
        card_html = response.get_json()["card_html"]
        self.assertIn('<article class="card selected', card_html)
        self.assertRegex(card_html, r'class="sel-toggle"[^>]*\bchecked\b')

    def test_optional_editor_fields_default_from_content_on_both_edit_surfaces(self):
        qid = app_module.filestore.create_question(
            "编辑器折叠状态", solution="已有解析", note="已有备注")
        record = app_module.filestore.get_question(qid)
        with app_module.app.test_request_context("/"):
            inline_html = app_module.render_template(
                "_inline_question_editor.html", q=record,
                types=config.QUESTION_TYPES,
            )
        standalone_html = self.client.get(f"/question/{qid}/edit").get_data(as_text=True)
        empty_html = self.client.get("/question/new").get_data(as_text=True)

        def optional_block(html_text, field):
            match = re.search(
                rf'<details class="inline-optional-field"[^>]*'
                rf'data-inline-optional="{field}".*?</details>',
                html_text,
                re.S,
            )
            self.assertIsNotNone(match)
            return match.group(0)

        for html_text in (inline_html, standalone_html):
            for field in ("solution", "note"):
                block = optional_block(html_text, field)
                opening = block.split(">", 1)[0]
                self.assertRegex(opening, r"\bopen\b")
                self.assertRegex(
                    block,
                    rf'data-preview-field="{field}"[^>]*\bchecked\b',
                )

        for field in ("solution", "note"):
            block = optional_block(empty_html, field)
            opening = block.split(">", 1)[0]
            self.assertNotRegex(opening, r"\bopen\b")
            self.assertNotRegex(
                block,
                rf'data-preview-field="{field}"[^>]*\bchecked\b',
            )

    def test_card_and_inline_preview_hide_solution_leading_label_only(self):
        qid = app_module.filestore.create_question(
            "解析标签展示回归", solution="【解析】正文保留解析二字", qtype="填空题")
        rec = app_module.filestore.get_question(qid)
        with app_module.app.test_request_context("/"):
            card_html = app_module.render_template(
                "_question_card.html", q=rec, types=config.QUESTION_TYPES,
                question_card_sort="browse")
        reader_html = card_html.split('<div class="card-reader">', 1)[1].split(
            '<section class="inline-editor"', 1)[0]
        self.assertIn("正文保留解析二字", reader_html)
        self.assertNotIn("【解析】", reader_html)
        self.assertIn('<details class="q-solution">', reader_html)
        self.assertIn("<summary>解析</summary>", reader_html)
        self.assertNotIn('<details class="q-solution" open', reader_html)

        payload = {
            "body": rec["body"], "solution": "解析：实时预览正文",
            "type": rec["type"], "source": "", "difficulty": "", "tags": "",
        }
        preview = self.client.post(
            f"/question/{qid}/preview", json=payload, headers=self.headers)
        self.assertEqual(preview.status_code, 200)
        solution_html = preview.get_json()["solution_html"]
        self.assertIn("实时预览正文", solution_html)
        self.assertNotIn("解析：", solution_html)

    def test_solution_split_route_saves_and_rerenders_solution_as_wrap(self):
        qid = app_module.filestore.create_question(
            "解析图文分栏题", solution="【解析】左侧文字\n\n![[solution-split.png]]",
            qtype="填空题")
        response = self.client.post(
            f"/question/{qid}/img_split",
            json={"mode": "full", "field": "solution"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["field"], "solution")
        self.assertEqual(data["mode"], "full")
        self.assertIn('class="q-solution-flow q-solution-flow-right"',
                      data["body_html"])
        self.assertIn('class="q-solution-flow-img"', data["body_html"])
        self.assertNotIn('class="q-split"', data["body_html"])
        self.assertIn("左侧文字", data["body_html"])
        self.assertNotIn("【解析】", data["body_html"])
        self.assertEqual(
            app_module.filestore.get_question(qid)["sol_img_split"], "full")

        disabled = self.client.post(
            f"/question/{qid}/img_split",
            json={"mode": "", "field": "solution"}, headers=self.headers)
        self.assertEqual(disabled.status_code, 200)
        self.assertNotIn('class="q-split"', disabled.get_json()["body_html"])
        self.assertEqual(
            app_module.filestore.get_question(qid)["sol_img_split"], "off")

    def test_solution_wrap_keeps_legacy_full_value_and_card_wording(self):
        qid = app_module.filestore.create_questions_batch([{
            "body": "解析混排兼容题",
            "solution": "长解析\n\n![[solution-wrap.png]]",
            "type": "填空题", "sol_img_split": "full",
            "sol_img_layouts": [{"i": 0, "w": 40, "align": "left"}],
        }])[0]
        rec = app_module.filestore.get_question(qid)
        with app_module.app.test_request_context("/"):
            card_html = app_module.render_template(
                "_question_card.html", q=rec, types=config.QUESTION_TYPES,
                question_card_sort="browse")

        self.assertEqual(rec["sol_img_split"], "full")
        self.assertIn("图文混排", card_html)
        self.assertIn("q-solution-flow-left", card_html)
        self.assertIn("width:40.0%", card_html)

    def test_inline_editor_rejects_empty_body(self):
        qid = app_module.filestore.create_question("不会被清空")
        response = self.client.post(
            f"/question/{qid}/inline", json={"body": ""}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(app_module.filestore.get_question(qid)["body"], "不会被清空")

    def test_inline_create_appends_to_current_folder_and_returns_regular_card(self):
        folder = app_module.filestore.get_or_create_collection("原地新增测试", "")
        old_id = app_module.filestore.create_question("已有题目", folder=folder)
        old_order = app_module.filestore.get_question(old_id)["order"]
        files_before = len(list((config.BANK_DIR / folder).glob("*.md")))

        draft = self.client.get(
            "/question/inline-draft", query_string={"collection": folder})
        self.assertEqual(draft.status_code, 200)
        draft_html = draft.get_json()["card_html"]
        self.assertIn('data-new="1"', draft_html)
        self.assertIn("原地新增测试", draft_html)
        self.assertEqual(len(list((config.BANK_DIR / folder).glob("*.md"))), files_before)

        payload = {
            "body": "末尾新题 $x+1$", "solution": "答案 $2$",
            "type": "填空题", "source": "手动录入", "difficulty": "3",
            "tags": "原地, 新增", "collection": folder, "card_sort": "custom",
        }
        preview = self.client.post(
            "/question/inline-preview", json=payload, headers=self.headers)
        self.assertEqual(preview.status_code, 200)
        self.assertIn("末尾新题", preview.get_json()["body_html"])
        self.assertEqual(len(list((config.BANK_DIR / folder).glob("*.md"))), files_before)

        created = self.client.post(
            "/question/inline-create", json=payload, headers=self.headers)
        self.assertEqual(created.status_code, 200)
        data = created.get_json()
        self.assertIn("inline-edit-trigger", data["card_html"])
        rec = app_module.filestore.get_question(data["id"])
        self.assertEqual(rec["folder"], folder)
        self.assertGreater(rec["order"], old_order)
        self.assertEqual(rec["tags"], ["原地", "新增"])

    def test_inline_create_rejects_missing_folder_and_nav_uses_context_menu(self):
        payload = {"body": "不得落盘", "collection": "不存在的文件夹"}
        rejected = self.client.post(
            "/question/inline-create", json=payload, headers=self.headers)
        self.assertEqual(rejected.status_code, 404)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-act="new-question"', page)
        self.assertIn('data-sidebar-tab="files"', page)
        self.assertIn("QOpenNewQuestionCard", page)
        self.assertNotIn('href="/question/new"', page)

    def test_question_rename_returns_updated_title_and_path(self):
        folder = app_module.filestore.get_or_create_collection("题卡改名路由", "")
        qid = app_module.filestore.create_question(
            "改名题干", folder=folder, title="旧题卡名")
        old_path = app_module.filestore.get_question(qid)["path"]

        response = self.client.post(
            f"/question/{qid}/rename",
            json={"title": "新题卡名"}, headers=self.headers)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        renamed = app_module.filestore.get_question(qid)
        self.assertTrue(data["ok"])
        self.assertEqual(data["id"], qid)
        self.assertEqual(data["title"], "新题卡名")
        self.assertEqual(data["path"], renamed["path"])
        self.assertNotEqual(data["path"], old_path)

    def test_library_lists_and_reads_only_supported_visible_files(self):
        folder = config.BANK_DIR / "资料阅读测试"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "笔记.md").write_text("# 标题\n\n$x^2$", encoding="utf-8")
        (folder / "试卷.pdf").write_bytes(b"%PDF-1.7\n%%EOF")
        (folder / "图.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        (folder / "忽略.txt").write_text("no", encoding="utf-8")
        (folder / ".隐藏.md").write_text("secret", encoding="utf-8")

        page = self.client.get("/library")
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.headers["Location"].endswith("/"))
        question_page = self.client.get(
            "/", query_string={"show_general_md": "1", "show_pdf": "1"})
        self.assertEqual(question_page.status_code, 200)
        self.assertIn('name="show_pdf"', question_page.get_data(as_text=True))
        self.assertIn('name="show_general_md"', question_page.get_data(as_text=True))
        library_script = (config.BASE_DIR / "static" / "js" / "library-tabs.js").read_text(
            encoding="utf-8")
        self.assertIn("quizforge:library-workspace:v3", library_script)
        self.assertIn("function addBlankDocumentTab(", library_script)
        self.assertIn("let tab = tabs.get(pane.active)", library_script)
        self.assertIn("replaceTabDocument(tab,", library_script)
        self.assertIn("add.dataset.libraryTabAdd = pane.id", library_script)
        self.assertNotIn("if (tabs.has(path))", library_script)
        listing = self.client.get(
            "/api/library/children", query_string={"path": "资料阅读测试"})
        self.assertEqual(listing.status_code, 200)
        entries = listing.get_json()["entries"]
        self.assertEqual(
            {item["name"] for item in entries}, {"笔记.md", "试卷.pdf", "图.png"})
        self.assertEqual(
            {item["kind"] for item in entries}, {"markdown", "pdf", "image"})

        note = self.client.get(
            "/api/library/read", query_string={"path": "资料阅读测试/笔记.md"})
        self.assertEqual(note.status_code, 200)
        self.assertEqual(note.get_json()["text"], "# 标题\n\n$x^2$")
        original_mtime = note.get_json()["mtime"]
        self.assertIsInstance(original_mtime, str)
        self.assertGreater(int(original_mtime), 2 ** 53 - 1)
        saved = self.client.post(
            "/api/library/write",
            json={
                "path": "资料阅读测试/笔记.md",
                "text": "# 已修改\r\n\r\n正文",
                "mtime": original_mtime,
            },
            headers=self.headers,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            (folder / "笔记.md").read_text(encoding="utf-8", newline=""),
            "# 已修改\n\n正文",
        )
        saved_mtime = saved.get_json()["mtime"]
        self.assertIsInstance(saved_mtime, str)
        # 旧版页面传来的整数仍可保存；新页面始终用字符串，避免 JS 精度丢失。
        legacy_saved = self.client.post(
            "/api/library/write",
            json={
                "path": "资料阅读测试/笔记.md",
                "text": "# 旧请求兼容\n",
                "mtime": int(saved_mtime),
            },
            headers=self.headers,
        )
        self.assertEqual(legacy_saved.status_code, 200)
        saved_mtime = legacy_saved.get_json()["mtime"]
        (folder / "笔记.md").write_text("外部修改", encoding="utf-8")
        stat = (folder / "笔记.md").stat()
        os.utime(
            folder / "笔记.md",
            ns=(stat.st_atime_ns, int(saved_mtime) + 1_000_000),
        )
        conflict = self.client.post(
            "/api/library/write",
            json={
                "path": "资料阅读测试/笔记.md",
                "text": "不得覆盖外部修改",
                "mtime": saved_mtime,
            },
            headers=self.headers,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual((folder / "笔记.md").read_text(encoding="utf-8"), "外部修改")
        pdf = self.client.get(
            "/library/raw", query_string={"path": "资料阅读测试/试卷.pdf"})
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.headers["X-Content-Type-Options"], "nosniff")
        # Windows 下 send_file 的响应未关闭会持续占用文件，导致临时题库无法清理。
        pdf.close()
        self.assertEqual(self.client.get(
            "/library/raw", query_string={"path": "资料阅读测试/忽略.txt"}
        ).status_code, 404)

    def test_library_lists_reads_and_edits_history_markdown(self):
        shutil.rmtree(config.HISTORY_DIR, ignore_errors=True)
        source = config.UPLOAD_DIR / "library-history.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"%PDF-1.7\n%%EOF")
        record = app_module.history_store.create_record(
            "资料库历史", [source], source_names=["历史原卷.pdf"])
        app_module.history_store.attach_markdown(
            record["id"], "# 原始识别\n")

        root_entries = self.client.get(
            "/api/library/children").get_json()["entries"]
        history_root = next(
            item for item in root_entries if item["name"] == "历史记录")
        records = self.client.get(
            "/api/library/children",
            query_string={"path": history_root["path"]}).get_json()["entries"]
        self.assertEqual(len(records), 1)
        files = self.client.get(
            "/api/library/children",
            query_string={"path": records[0]["path"]}).get_json()["entries"]
        by_name = {item["name"]: item for item in files}
        self.assertEqual(set(by_name), {"历史原卷.pdf", "result.md"})

        note = self.client.get(
            "/api/library/read",
            query_string={"path": by_name["result.md"]["path"]})
        self.assertEqual(note.status_code, 200)
        self.assertEqual(note.get_json()["text"], "# 原始识别\n")
        saved = self.client.post(
            "/api/library/write",
            json={"path": by_name["result.md"]["path"],
                  "text": "# 人工修订\n", "mtime": note.get_json()["mtime"]},
            headers=self.headers)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            app_module.history_store.read_markdown(record["id"]),
            "# 人工修订\n")
        pdf = self.client.get(
            "/library/raw",
            query_string={"path": by_name["历史原卷.pdf"]["path"]})
        self.assertEqual(pdf.status_code, 200)
        pdf.close()

    def test_library_rejects_traversal_hidden_paths_and_symlink_escape(self):
        self.assertEqual(self.client.get(
            "/api/library/read", query_string={"path": "../outside.md"}
        ).status_code, 404)
        self.assertEqual(self.client.get(
            "/api/library/read", query_string={"path": ".trash/secret.md"}
        ).status_code, 404)
        self.assertEqual(self.client.post(
            "/api/library/write",
            json={"path": "../outside.md", "text": "bad", "mtime": 1},
            headers=self.headers,
        ).status_code, 404)

        outside = Path(_tmp.name) / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        link = config.BANK_DIR / "escape.md"
        try:
            link.symlink_to(outside)
        except OSError:
            return
        self.assertEqual(self.client.get(
            "/api/library/read", query_string={"path": "escape.md"}
        ).status_code, 404)
        root_listing = self.client.get("/api/library/children").get_json()["entries"]
        self.assertNotIn("escape.md", {item["name"] for item in root_listing})

    def test_desktop_workspace_shell_only_accepts_local_business_path(self):
        page = self.client.get("/workspace", query_string={"path": "/library"})
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('id="workspace-library-frame"', html)
        self.assertIn('data-initial-path="/library"', html)
        rejected = self.client.get(
            "/workspace", query_string={"path": "//example.com/steal"})
        self.assertIn('data-initial-path="/"', rejected.get_data(as_text=True))


class HandoutStorageTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.headers = {
            "X-CSRF-Token": app_module._WRITE_TOKEN,
            "Content-Type": "application/json",
        }
        app_module.filestore.clear_selected()

    def test_create_roundtrip_autosave_and_external_conflict(self):
        created = self.client.post(
            "/api/handouts",
            json={"title": "函数讲义", "page_format": "a4", "columns": 2},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201)
        payload = created.get_json()
        self.assertTrue(payload["path"].startswith("_handouts/"))
        self.assertEqual(payload["metadata"]["columns"], 2)
        self.assertIsInstance(payload["mtime"], str)

        loaded = self.client.get(
            "/api/handouts/read", query_string={"path": payload["path"]})
        self.assertEqual(loaded.status_code, 200)
        doc = loaded.get_json()
        doc["metadata"]["unknown_user_field"] = {"keep": True}
        saved = self.client.post(
            "/api/handouts/write",
            json={
                "path": payload["path"], "metadata": doc["metadata"],
                "body": "# 新标题\r\n\r\n正文 $x^2$", "mtime": doc["mtime"],
            },
            headers=self.headers,
        )
        self.assertEqual(saved.status_code, 200)
        saved_data = saved.get_json()
        path = config.BANK_DIR / payload["path"]
        self.assertNotIn("\r", path.read_text(encoding="utf-8", newline=""))
        reread = self.client.get(
            "/api/handouts/read", query_string={"path": payload["path"]}).get_json()
        self.assertEqual(reread["metadata"]["unknown_user_field"], {"keep": True})

        path.write_text(path.read_text(encoding="utf-8") + "\n外部修改", encoding="utf-8")
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, int(saved_data["mtime"]) + 1_000_000))
        conflict = self.client.post(
            "/api/handouts/write",
            json={
                "path": payload["path"], "metadata": reread["metadata"],
                "body": "不得覆盖", "mtime": saved_data["mtime"],
            },
            headers=self.headers,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("外部修改", path.read_text(encoding="utf-8"))

    def test_question_markers_roundtrip_and_malformed_content_is_preserved(self):
        block_id = app_module.handouts.new_block_id()
        marker = app_module.handouts.question_marker(
            block_id, "题干 $x$", "解析 $1$")
        meta = app_module.handouts._default_metadata("标记测试")
        meta["question_blocks"][block_id] = {
            "source_id": "q-source", "number_override": "例1",
            "solution_placement": "inline",
            "render_confirmed": True,
        }
        meta["wimath_logo"] = True
        text = app_module.handouts.serialize_document(meta, "前文\n\n" + marker + "\n\n后文")
        loaded_meta, body, warnings = app_module.handouts.split_document(text)
        blocks, parse_warnings = app_module.handouts.parse_content(
            body, loaded_meta["question_blocks"])
        self.assertEqual(warnings + parse_warnings, [])
        question = next(block for block in blocks if block["kind"] == "question")
        self.assertEqual(question["body"], "题干 $x$")
        self.assertEqual(question["solution"], "解析 $1$")
        self.assertEqual(question["number_override"], "例1")
        self.assertTrue(loaded_meta["wimath_logo"])
        self.assertTrue(question["render_confirmed"])

        malformed = f"正文\n\n<!-- quizforge:question {block_id} -->\n没有结束"
        blocks, warnings = app_module.handouts.parse_content(malformed, {})
        self.assertTrue(warnings)
        self.assertEqual("".join(block["text"] for block in blocks), malformed)

    def test_handout_directory_never_becomes_question_collection(self):
        created = app_module.handouts.create_document("不得进题库")
        app_module.filestore.invalidate_scan_cache(folder_structure=True)
        paths = app_module.filestore.list_question_paths("")
        self.assertNotIn(created["path"], paths)
        tree = app_module.filestore.list_collections_tree(records=[])
        self.assertNotIn("_handouts", {node["id"] for node in tree})
        listing = self.client.get("/api/library/children").get_json()["entries"]
        self.assertIn("_handouts", {entry["name"] for entry in listing})
        handout_listing = self.client.get(
            "/api/library/children", query_string={"path": "_handouts"}
        ).get_json()["entries"]
        row = next(item for item in handout_listing if item["path"] == created["path"])
        self.assertEqual(row["kind"], "handout")

    def test_handout_api_rejects_paths_outside_reserved_directory(self):
        for path in ("../outside.md", "普通目录/a.md", "_handouts/../outside.md"):
            response = self.client.get(
                "/api/handouts/read", query_string={"path": path})
            self.assertIn(response.status_code, (400, 404))

    def test_handout_delete_success_conflict_boundary_and_future_schema(self):
        created = app_module.handouts.create_document("待删除讲义")
        path = config.BANK_DIR / created["path"]
        response = self.client.post(
            "/api/handouts/delete",
            json={"path": created["path"], "mtime": created["mtime"]},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertFalse(path.exists())

        conflict = app_module.handouts.create_document("冲突删除讲义")
        conflict_path = config.BANK_DIR / conflict["path"]
        os.utime(conflict_path, ns=(conflict_path.stat().st_atime_ns,
                                   int(conflict["mtime"]) + 1_000_000))
        response = self.client.post(
            "/api/handouts/delete",
            json={"path": conflict["path"], "mtime": conflict["mtime"]},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(conflict_path.is_file())

        outside = config.BANK_DIR / "outside.md"
        outside.write_text("不得删除", encoding="utf-8")
        response = self.client.post(
            "/api/handouts/delete",
            json={"path": "_handouts/../outside.md", "mtime": "0"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(outside.is_file())

        future = app_module.handouts.create_document("未来讲义")
        future_path = config.BANK_DIR / future["path"]
        future_meta = future["metadata"]
        future_meta["quizforge_schema"] = 99
        future_path.write_text(
            app_module.handouts.serialize_document(future_meta, "未来内容"),
            encoding="utf-8",
        )
        future_mtime = str(future_path.stat().st_mtime_ns)
        response = self.client.post(
            "/api/handouts/delete",
            json={"path": future["path"], "mtime": future_mtime},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(future_path.is_file())

        link = config.HANDOUTS_DIR / "讲义链接.md"
        try:
            link.symlink_to(outside)
        except OSError:
            pass
        else:
            response = self.client.post(
                "/api/handouts/delete",
                json={"path": "_handouts/讲义链接.md",
                      "mtime": str(outside.stat().st_mtime_ns)},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 400)
            self.assertTrue(outside.is_file())

    def test_selected_question_snapshot_contains_source_version_and_layout(self):
        qid = app_module.filestore.create_question(
            "快照题 $x$", solution="快照解析\n\n![[snapshot-solution.png]]", qtype="填空题",
            source="校本")
        app_module.filestore.set_img_width(qid, 66)
        app_module.filestore.set_img_split(qid, "full", field="solution")
        app_module.filestore.toggle_selected(qid)
        # 模拟桌面冷启动：没有题卡缓存时也只能扫描 frontmatter 头部，不得解析全库。
        with app_module.filestore._scan_lock:
            app_module.filestore._cache.clear()
        app_module.filestore.invalidate_scan_cache()
        with mock.patch.object(
                app_module.filestore, "_scan",
                side_effect=AssertionError("读取选题篮不应扫描全库")):
            selected = self.client.get("/api/handouts/selected")
        self.assertEqual(selected.status_code, 200)
        selected_rows = selected.get_json()["questions"]
        self.assertIn(qid, {row["id"] for row in selected_rows})
        selected_row = next(row for row in selected_rows if row["id"] == qid)
        self.assertIn('class="q-stem"', selected_row["body_html"])
        self.assertIn("$x$", selected_row["body_html"])
        snapshot = self.client.get(f"/api/handouts/question/{qid}")
        self.assertEqual(snapshot.status_code, 200)
        data = snapshot.get_json()
        self.assertEqual(data["body"], "快照题 $x$")
        self.assertIn("快照解析", data["solution"])
        self.assertEqual(data["metadata"]["img_width"], 66)
        self.assertEqual(data["metadata"]["sol_img_split"], "full")
        self.assertRegex(data["metadata"]["source_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(data["metadata"]["source_mtime_ns"], r"^\d+$")

    def test_selection_details_preserve_order_fields_safe_html_and_root_values(self):
        parent = app_module.filestore.get_or_create_collection("抽屉详情父级", "")
        folder = app_module.filestore.get_or_create_collection("抽屉详情测试", parent)
        first = app_module.filestore.create_question(
            '第一题 <script>alert("body")</script> $x$',
            solution='<script>alert("solution")</script> 解析 $x=1$',
            qtype="解答题", source="校本题源", difficulty="3",
            tags=["函数", "重点"], folder=folder, number=1,
            note='<script>alert("note")</script> 只读备注', title="抽屉第一题")
        second = app_module.filestore.create_question(
            "第二题", folder=folder, number=2, title="抽屉第二题")
        root = app_module.filestore.create_question(
            "根目录空值题", title="抽屉根目录题")
        empty = app_module.filestore.create_question(
            "", title="抽屉空题干")
        app_module.filestore.toggle_starred(first)
        app_module.filestore.select_ids([second, first, root, empty])

        response = self.client.get("/api/selection")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 4)
        rows = payload["questions"]
        folder_rows = [row for row in rows if row["collection"] == folder]
        self.assertEqual([row["id"] for row in folder_rows], [first, second])
        first_row = next(row for row in rows if row["id"] == first)
        self.assertEqual(set(first_row), {
            "id", "type", "source", "number", "folder", "excerpt", "path",
            "body_html", "title", "difficulty", "starred", "tags",
            "collection", "solution_html", "note_html",
        })
        self.assertEqual(first_row["title"], "抽屉第一题")
        self.assertEqual(first_row["difficulty"], "3")
        self.assertTrue(first_row["starred"])
        self.assertEqual(first_row["tags"], ["函数", "重点"])
        self.assertEqual(first_row["folder"], "抽屉详情测试")
        self.assertEqual(first_row["collection"], folder)
        for field in ("body_html", "solution_html", "note_html"):
            self.assertNotIn("<script", first_row[field])
            self.assertIn("&lt;script&gt;", first_row[field])
        root_row = next(row for row in rows if row["id"] == root)
        self.assertEqual(root_row["folder"], "题库根目录")
        self.assertEqual(root_row["collection"], "")
        self.assertEqual(root_row["difficulty"], "")
        self.assertFalse(root_row["starred"])
        self.assertEqual(root_row["tags"], [])
        self.assertEqual(root_row["solution_html"], "")
        self.assertEqual(root_row["note_html"], "")
        empty_row = next(row for row in rows if row["id"] == empty)
        self.assertEqual(empty_row["body_html"], "")

    def test_selection_count_ignores_stale_selected_ids(self):
        qid = app_module.filestore.create_question("仍存在的选题")
        app_module.filestore.toggle_selected(qid)
        with mock.patch.object(
                app_module.filestore, "selected_ids",
                return_value=[qid, "missing-question-id"]):
            response = self.client.get("/api/selection")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual([row["id"] for row in payload["questions"]], [qid])

    def test_selection_details_use_targeted_reads_on_cold_cache(self):
        qid = app_module.filestore.create_question(
            "冷缓存抽屉题", solution="冷缓存解析", note="冷缓存备注",
            title="冷缓存抽屉题")
        app_module.filestore.toggle_selected(qid)
        with app_module.filestore._scan_lock:
            app_module.filestore._cache.clear()
        app_module.filestore.invalidate_scan_cache()

        with mock.patch.object(
                app_module.filestore, "_scan",
                side_effect=AssertionError("读取选题抽屉不应扫描全库")):
            response = self.client.get("/api/selection")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 1)
        self.assertEqual(response.get_json()["questions"][0]["id"], qid)

    def test_handout_selected_endpoint_stays_lightweight(self):
        qid = app_module.filestore.create_question(
            "讲义轻量题", solution="不应渲染的解析", note="不应渲染的备注",
            title="讲义轻量题")
        app_module.filestore.toggle_selected(qid)
        render_body = app_module.handouts.qrender.render_body
        with (mock.patch.object(
                app_module.handouts.qrender, "render_body",
                wraps=render_body) as render_body_mock,
              mock.patch.object(
                app_module.handouts.qrender, "render_solution") as render_solution):
            response = self.client.get("/api/handouts/selected")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertNotIn("count", payload)
        self.assertEqual(len(payload["questions"]), 1)
        self.assertEqual(set(payload["questions"][0]), {
            "id", "type", "source", "number", "folder", "excerpt", "path",
            "body_html",
        })
        render_body_mock.assert_called_once()
        self.assertEqual(render_body_mock.call_args.args[0], "讲义轻量题")
        render_solution.assert_not_called()

    def test_same_title_creates_collision_safe_file_and_future_schema_is_read_only(self):
        first = app_module.handouts.create_document("同名讲义")
        second = app_module.handouts.create_document("同名讲义")
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(second["path"].endswith("_2.md"))

        path = config.BANK_DIR / first["path"]
        meta = first["metadata"]
        meta["quizforge_schema"] = 99
        path.write_text(app_module.handouts.serialize_document(meta, "未来内容"),
                        encoding="utf-8")
        loaded = self.client.get(
            "/api/handouts/read", query_string={"path": first["path"]}).get_json()
        self.assertTrue(loaded["read_only"])
        refused = self.client.post(
            "/api/handouts/write",
            json={"path": first["path"], "metadata": loaded["metadata"],
                  "body": "不能覆盖", "mtime": loaded["mtime"]},
            headers=self.headers,
        )
        self.assertEqual(refused.status_code, 400)
        self.assertIn("未来内容", path.read_text(encoding="utf-8"))

    def test_export_markdown_numbering_solution_modes_and_double_columns(self):
        ids = [app_module.handouts.new_block_id() for _ in range(3)]
        meta = app_module.handouts._default_metadata("导出讲义", columns=2)
        meta["solution_default"] = "appendix"
        for index, block_id in enumerate(ids):
            meta["question_blocks"][block_id] = {
                "question_type": "解答题", "number_override": "例1" if index == 1 else None,
                "solution_placement": "inline" if index == 0 else "inherit",
            }
        body = "\n\n".join([
            "# 导出讲义",
            app_module.handouts.question_marker(ids[0], "第一题 $x$", "第一解"),
            app_module.handouts.question_marker(ids[1], "第二题", "第二解"),
            app_module.handouts.PAGE_BREAK_MARKER,
            app_module.handouts.question_marker(ids[2], "第三题", "第三解"),
        ])
        markdown, warnings = app_module.service_ports.handout_exporter.build_markdown(meta, body)
        self.assertEqual(warnings, [])
        self.assertIn(r"\qopen{1.}", markdown)
        self.assertIn(r"\qopen{例1}", markdown)
        self.assertIn(r"\qopen{3.}", markdown)
        self.assertNotIn("【解析】", markdown)
        self.assertIn("参考解析", markdown)
        self.assertIn("第一解", markdown)
        self.assertIn("第二解", markdown)
        self.assertIn("第三解", markdown)
        self.assertGreaterEqual(markdown.count(r"\qpracticebegin"), 2)
        self.assertGreaterEqual(markdown.count(r"\qpracticeend"), 2)
        self.assertEqual(markdown.count(r"\begin{qpracticesolve}"), 3)
        self.assertEqual(markdown.count(r"\end{qpracticesolve}"), 3)
        # 第一、二题之间换栏；第三题前已有显式分页，不应再多跳到右栏。
        self.assertEqual(markdown.count(r"\columnbreak"), 1)
        self.assertIn(r"\clearpage", markdown)

    def test_portable_markdown_export_has_no_internal_or_tex_markers(self):
        first = app_module.handouts.new_block_id()
        second = app_module.handouts.new_block_id()
        meta = app_module.handouts._default_metadata("可编辑讲义")
        meta["solution_default"] = "appendix"
        meta["question_blocks"][first] = {
            "question_type": "填空题", "solution_placement": "inline",
        }
        meta["question_blocks"][second] = {
            "question_type": "解答题", "number_override": "例1",
            "solution_placement": "inherit",
        }
        body = "\n\n".join([
            "## 知识点",
            app_module.handouts.question_marker(
                first, "第一题 $x$ ![[figure.png]]", "【解析】第一解"),
            app_module.handouts.PAGE_BREAK_MARKER,
            app_module.handouts.question_marker(second, "第二题", "第二解"),
        ])

        markdown, warnings = (
            app_module.service_ports.handout_exporter.build_portable_markdown(
                meta, body))
        self.assertEqual(warnings, [])
        self.assertTrue(markdown.startswith("# 可编辑讲义\n"))
        self.assertIn("**1.**", markdown)
        self.assertIn("**例1**", markdown)
        self.assertIn("**解析**\n\n第一解", markdown)
        self.assertIn("## 参考解析", markdown)
        self.assertIn("![[figure.png]]", markdown)
        self.assertIn("\n---\n", markdown)
        self.assertNotIn("quizforge:", markdown)
        self.assertNotIn(r"\qopen", markdown)
        self.assertNotIn(r"\begin{samepage}", markdown)

        exported = app_module.service_ports.handout_exporter.export(
            meta, body, fmt="md")
        self.assertEqual(exported.suffix, ".md")
        self.assertEqual(exported.read_text(encoding="utf-8"), markdown)

    def test_handout_solution_prefix_is_stripped_and_slides_use_left_70_percent(self):
        block_id = app_module.handouts.new_block_id()
        meta = app_module.handouts._default_metadata(
            "横版解析", page_format="slides")
        meta["solution_default"] = "inline"
        meta["question_blocks"][block_id] = {
            "question_type": "填空题", "solution_placement": "inherit",
            "_img_files": ["body.png"], "_sol_img_files": ["solution.png"],
            "sol_img_split": "full",
        }
        body = app_module.handouts.question_marker(
            block_id, "横版题干 $x$\nQFIGSLOT0",
            "【解析】只显示这一段解析\nQFIGSLOT0")
        markdown, warnings = (
            app_module.service_ports.handout_exporter.build_markdown(meta, body))
        self.assertEqual(warnings, [])
        self.assertIn(r"\begin{minipage}[t]{0.7\linewidth}", markdown)
        self.assertIn("只显示这一段解析", markdown)
        self.assertNotIn("【解析】", markdown)
        self.assertIn(r"\qwrapclear", markdown)
        self.assertLess(markdown.index("body.png"), markdown.index("只显示这一段解析"))
        self.assertLess(markdown.index("只显示这一段解析"), markdown.index("solution.png"))
        self.assertLess(markdown.index(r"\qclose\end{samepage}"),
                        markdown.index(r"\begin{wrapfigure}"))
        self.assertLess(markdown.index(r"\begin{wrapfigure}"),
                        markdown.index(r"\end{minipage}\par"))

    def test_handout_solution_wrap_avoids_double_column_wrappers_and_appendix_fence_is_block(self):
        inline_id = app_module.handouts.new_block_id()
        appendix_id = app_module.handouts.new_block_id()
        meta = app_module.handouts._default_metadata("解析混排", columns=2)
        meta["solution_default"] = "appendix"
        meta["question_blocks"][inline_id] = {
            "question_type": "解答题", "solution_placement": "inline",
            "_sol_img_files": ["inline.png"], "sol_img_split": "full",
            "sol_img_layouts": [{"i": 0, "w": 35}],
        }
        meta["question_blocks"][appendix_id] = {
            "question_type": "填空题", "solution_placement": "inherit",
            "_sol_img_files": ["appendix.png"], "sol_img_split": "full",
            "sol_img_layouts": [{"i": 0, "w": 35}],
        }
        body = "\n\n".join([
            app_module.handouts.question_marker(
                inline_id, "内联题干", "内联解析\nQFIGSLOT0"),
            app_module.handouts.question_marker(
                appendix_id, "文末题干", "文末解析\nQFIGSLOT0"),
        ])

        markdown, warnings = (
            app_module.service_ports.handout_exporter.build_markdown(meta, body))
        self.assertEqual(warnings, [])
        inline_wrap = markdown.index(r"\begin{wrapfigure}")
        self.assertLess(markdown.index(r"\end{qpracticesolve}"), inline_wrap)
        self.assertLess(markdown.index(r"\qclose\end{samepage}"), inline_wrap)
        self.assertIn(
            "**2.**\n\n文末解析\n\n```{=latex}\n\\begin{wrapfigure}", markdown)
        self.assertNotIn("**2.** ```{=latex}", markdown)

    def test_handout_preview_and_export_routes_use_service_port(self):
        produced = config.OUTPUT_DIR / "mock-handout.pdf"
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_bytes(b"%PDF-1.4\n")
        payload = {
            "metadata": app_module.handouts._default_metadata("接口讲义"),
            "body": "# 接口讲义\n",
        }
        with mock.patch.object(
            app_module.service_ports, "export_handout_document", return_value=produced,
        ) as export_mock:
            preview = self.client.post(
                "/api/handouts/preview", json=payload, headers=self.headers)
            exported = self.client.post(
                "/api/handouts/export", json={**payload, "fmt": "pdf"},
                headers=self.headers,
            )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(exported.status_code, 200)
        self.assertIn("/outfile/", preview.get_json()["url"])
        self.assertEqual(exported.get_json()["filename"], "接口讲义.pdf")
        self.assertEqual(export_mock.call_count, 2)

    def test_handout_question_render_route_returns_sandboxed_svg(self):
        digest = "a" * 64
        produced = config.OUTPUT_DIR / "handout_card_cache" / f"{digest}.svg"
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>',
            encoding="utf-8",
        )
        payload = {
            "metadata": app_module.handouts._default_metadata("局部编译"),
            "question": {
                "block_id": "q_render_test", "body": "题干 $x$", "solution": "",
                "number_override": None, "solution_placement": "inherit",
            },
            "position": 2,
        }
        with mock.patch.object(
            app_module.service_ports, "render_handout_question", return_value=produced,
        ) as render_mock:
            response = self.client.post(
                "/api/handouts/render-question", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["url"],
            f"/api/handouts/rendered-card/{digest}",
        )
        inline = self.client.get(response.get_json()["url"])
        self.assertEqual(inline.status_code, 200)
        self.assertEqual(inline.mimetype, "image/svg+xml")
        self.assertEqual(inline.headers.get("Content-Security-Policy"), "sandbox")
        inline.close()
        render_mock.assert_called_once()

        # 普通 outfile 的 64 token 淘汰不影响稳定题卡 URL。
        for index in range(app_module._MAX_OUT_FILES + 5):
            app_module._register_out_file(config.OUTPUT_DIR / f"other-{index}.pdf")
        stable = self.client.get(response.get_json()["url"])
        self.assertEqual(stable.status_code, 200)
        self.assertEqual(stable.headers.get("X-Content-Type-Options"), "nosniff")
        stable.close()
        self.assertEqual(self.client.get(
            "/api/handouts/rendered-card/../../outside.md").status_code, 404)

    def test_image_staging_rejects_escape_absolute_and_symlink_chain(self):
        config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        safe = config.IMAGES_DIR / "safe.png"
        safe.write_bytes(b"safe")
        outside = config.IMAGES_DIR.parent / "outside.png"
        outside.write_bytes(b"outside")
        absolute = outside.resolve().as_posix()

        self.assertEqual(app_module.exporter._resolve_image_source("safe.png"),
                         safe.resolve())
        for malicious in ("../outside.png", absolute, "C:/Windows/win.ini"):
            self.assertIsNone(app_module.exporter._resolve_image_source(malicious))

        work = config.OUTPUT_DIR / "image-stage-safe"
        work.mkdir(exist_ok=True)
        rendered = app_module.service_ports.handout_exporter._stage_markdown_images(
            f"![[safe.png]] ![[../outside.png]] ![[{absolute}]]", "safe", work)
        self.assertIn("safe_img_body_0.png", rendered)
        self.assertNotIn("outside", rendered)
        staged = app_module.exporter._stage_images(
            [{"body": "![[safe.png]] ![[../outside.png]]", "solution": ""}],
            "safe", work,
        )[0]
        self.assertIn("QFIGSLOT", staged["body"])
        self.assertNotIn("outside", staged["body"])

        first_fingerprint = app_module.service_ports.handout_exporter._question_image_fingerprints(
            "![[safe.png]]")
        safe.write_bytes(b"safe image replaced")
        second_fingerprint = app_module.service_ports.handout_exporter._question_image_fingerprints(
            "![[safe.png]]")
        self.assertNotEqual(first_fingerprint, second_fingerprint)

        link = config.IMAGES_DIR / "linked"
        try:
            link.symlink_to(config.IMAGES_DIR.parent, target_is_directory=True)
        except OSError:
            self.skipTest("当前 Windows 环境不允许创建目录符号链接")
        self.assertIsNone(
            app_module.exporter._resolve_image_source("linked/outside.png"))

    def test_wimath_logo_is_staged_with_zip_compatible_name(self):
        logo = config.OUTPUT_DIR / "brand-source.pdf"
        logo.parent.mkdir(parents=True, exist_ok=True)
        logo.write_bytes(b"%PDF-1.4\n")
        work = config.OUTPUT_DIR / "brand-stage"
        work.mkdir(exist_ok=True)
        with mock.patch.object(config, "WIMATH_LOGO_PDF", logo):
            name = app_module.service_ports.handout_exporter._stage_wimath_logo(
                {"wimath_logo": True}, "handout_test", work)
        self.assertEqual(name, "handout_test_img_wimath_logo.pdf")
        self.assertEqual((work / name).read_bytes(), logo.read_bytes())

    def test_a4_wimath_logo_is_lowered_inside_page(self):
        template = config.TEX_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(
            r"\AtPageUpperLeft{\raisebox{-1.25cm}[0pt][0pt]{%",
            template,
        )

    def test_release_scanner_requires_wimath_logo(self):
        from tools import verify_desktop_bundle

        dist = config.OUTPUT_DIR / "dist-without-logo"
        # 用例必须可独立运行，不能依赖同一测试类的其它用例先创建 OUTPUT_DIR。
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "QuizForge.exe").write_bytes(b"MZ")
        problems = verify_desktop_bundle.scan(dist, Path(__file__).parent.parent)
        self.assertTrue(any("wimath-logo-latex-black.pdf" in item
                            for item in problems))
        self.assertTrue(any("word-reference.docx" in item
                            for item in problems))


if __name__ == "__main__":
    unittest.main()
