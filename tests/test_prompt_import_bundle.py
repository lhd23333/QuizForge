"""提示词库与带图 Markdown 资源包回归。"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

import config
import import_bundle
import prompt_store


def _png(color=(20, 80, 160)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), color).save(stream, format="PNG")
    return stream.getvalue()


def _bundle(markdown: str, assets: dict[str, bytes] | None = None,
            *, manifest: dict | None = None,
            extras: list[tuple[zipfile.ZipInfo | str, bytes]] | None = None) -> bytes:
    stream = io.BytesIO()
    document = manifest or {
        "schema": 1,
        "contract": "quizforge-markdown-v1",
        "entrypoint": "questions.md",
    }
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("quizforge-import.json", json.dumps(document))
        archive.writestr("questions.md", markdown)
        for name, data in (assets or {}).items():
            archive.writestr(name, data)
        for name, data in extras or []:
            archive.writestr(name, data)
    return stream.getvalue()


def _stage(data: bytes, root: Path) -> dict:
    return import_bundle.stage_bundle(
        data, "questions.qfimport.zip",
        stage_root=root / "stages", assets_dir=root / "assets",
        max_bundle_bytes=10 * 1024 * 1024, max_files=50,
        max_uncompressed_bytes=20 * 1024 * 1024,
        max_image_bytes=2 * 1024 * 1024,
        max_markdown_bytes=2 * 1024 * 1024,
        max_compression_ratio=200,
    )


class PromptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.official = self.root / "official"
        self.official.mkdir()
        (self.official / "questions_to_quizforge.md").write_text(
            "题目提示词", encoding="utf-8")
        (self.official / "template_to_quizforge_tex.md").write_text(
            "模板提示词", encoding="utf-8")
        self.path = self.root / "data" / "prompts.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_official_readonly_and_user_crud_are_kept_separate(self):
        initial = prompt_store.list_prompts(self.path, self.official)
        self.assertEqual(len(initial), 2)
        self.assertTrue(all(item["readonly"] for item in initial))
        self.assertEqual(
            [item["title"] for item in initial],
            ["PDF/图片题目转 QuizForge Markdown", "PDF/TeX 样式转 QuizForge TeX"],
        )

        created = prompt_store.create_prompt(self.path, {
            "title": "我的提示词", "category": "整理", "content": "正文",
        })
        self.assertTrue(created["id"].startswith("user-"))
        updated = prompt_store.update_prompt(
            self.path, created["id"], {"content": "新正文"})
        self.assertEqual(updated["content"], "新正文")
        prompt_store.delete_prompt(self.path, created["id"])
        self.assertEqual(len(prompt_store.list_prompts(self.path, self.official)), 2)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"schema": 1, "prompts": []})

    def test_corrupt_store_is_not_silently_overwritten(self):
        self.path.parent.mkdir()
        self.path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(prompt_store.PromptStoreError):
            prompt_store.create_prompt(self.path, {
                "title": "标题", "category": "分类", "content": "正文",
            })
        self.assertEqual(self.path.read_text(encoding="utf-8"), "not-json")

    def test_official_prompt_cannot_be_modified_or_deleted(self):
        with self.assertRaises(prompt_store.PromptStoreError):
            prompt_store.update_prompt(
                self.path, "official-question-markdown", {"content": "x"})
        with self.assertRaises(prompt_store.PromptStoreError):
            prompt_store.delete_prompt(self.path, "official-question-markdown")


class ImportBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_bundle_rewrites_relative_images_and_tracks_cleanup(self):
        data = _bundle(
            "- [解答] 观察图像\n\n  ![图像](assets/graph.png)\n\n"
            "  【解析】\n  略\n\n  ## 备注\n  待校对：坐标",
            {"assets/graph.png": _png()},
        )
        staged = _stage(data, self.root)
        self.assertRegex(staged["markdown"], r"!\[\[qfimport_[0-9a-f]{32}_[0-9a-f]{20}\.png\]\]")
        self.assertNotIn("assets/graph.png", staged["markdown"])
        asset = self.root / "assets" / staged["asset_names"][0]
        self.assertTrue(asset.is_file())
        state = import_bundle.get_stage(self.root / "stages", staged["id"])
        self.assertEqual(state["status"], "ready")
        candidates = import_bundle.discard_stage(
            self.root / "stages", self.root / "assets", staged["id"])
        self.assertEqual(candidates, set(staged["asset_names"]))
        self.assertFalse((self.root / "stages" / staged["id"]).exists())

    def test_rejects_traversal_symlink_and_external_image(self):
        traversal = _bundle("- [解答] 题目", extras=[("../outside.png", _png())])
        with self.assertRaisesRegex(import_bundle.ImportBundleError, "不安全路径"):
            _stage(traversal, self.root)

        link = zipfile.ZipInfo("assets/link.png")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        symlink = _bundle("- [解答] 题目", extras=[(link, b"target")])
        with self.assertRaisesRegex(import_bundle.ImportBundleError, "符号链接"):
            _stage(symlink, self.root)

        external = _bundle("- [解答] ![图](https://example.com/a.png)")
        with self.assertRaisesRegex(import_bundle.ImportBundleError, "相对路径"):
            _stage(external, self.root)

    def test_rejects_missing_or_disguised_image_and_zip_bomb_ratio(self):
        missing = _bundle("- [解答] ![图](assets/missing.png)")
        with self.assertRaisesRegex(import_bundle.ImportBundleError, "不存在"):
            _stage(missing, self.root)

        disguised = _bundle(
            "- [解答] ![图](assets/fake.png)", {"assets/fake.png": b"not-png"})
        with self.assertRaisesRegex(import_bundle.ImportBundleError, "损坏|伪装"):
            _stage(disguised, self.root)

        bomb = _bundle("- [解答] " + "A" * (1024 * 1024))
        with self.assertRaisesRegex(import_bundle.ImportBundleError, "压缩比"):
            import_bundle.stage_bundle(
                bomb, "bomb.zip", stage_root=self.root / "stages",
                assets_dir=self.root / "assets", max_bundle_bytes=10 * 1024 * 1024,
                max_files=50, max_uncompressed_bytes=10 * 1024 * 1024,
                max_image_bytes=2 * 1024 * 1024,
                max_markdown_bytes=2 * 1024 * 1024, max_compression_ratio=10)

    def test_partial_asset_write_rolls_back_stage_and_files(self):
        data = _bundle(
            "- [解答] ![一](assets/a.png) ![二](assets/b.png)",
            {"assets/a.png": _png((255, 0, 0)),
             "assets/b.png": _png((0, 255, 0))},
        )
        original = import_bundle._write_asset_atomic
        calls = 0

        def flaky(target, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return original(target, payload)

        with mock.patch.object(import_bundle, "_write_asset_atomic", side_effect=flaky):
            with self.assertRaises(OSError):
                _stage(data, self.root)
        self.assertFalse(any((self.root / "assets").glob("qfimport_*")))
        self.assertFalse(any((self.root / "stages").iterdir()))

    def test_finalize_deduplicates_assets_by_full_content_digest(self):
        payload = _bundle(
            "- [解答] ![图](assets/a.png)",
            {"assets/a.png": _png()},
        )
        first = _stage(payload, self.root)
        second = _stage(payload, self.root)
        with import_bundle.finalize_stage(
                self.root / "stages", self.root / "assets", first["id"]) as result:
            first_name = next(iter(result["mapping"].values()))
        with import_bundle.finalize_stage(
                self.root / "stages", self.root / "assets", second["id"]) as result:
            second_name = next(iter(result["mapping"].values()))

        self.assertEqual(first_name, second_name)
        self.assertRegex(first_name, r"^qfimport_[0-9a-f]{64}\.png$")
        self.assertTrue((self.root / "assets" / first_name).is_file())

    def test_finalize_rolls_back_new_digest_asset_when_commit_fails(self):
        staged = _stage(_bundle(
            "- [解答] ![图](assets/a.png)",
            {"assets/a.png": _png((180, 20, 40))},
        ), self.root)
        final_name = ""
        with self.assertRaisesRegex(RuntimeError, "create failed"):
            with import_bundle.finalize_stage(
                    self.root / "stages", self.root / "assets", staged["id"]) as result:
                final_name = next(iter(result["mapping"].values()))
                raise RuntimeError("create failed")
        self.assertFalse((self.root / "assets" / final_name).exists())
        self.assertTrue((self.root / "assets" / staged["asset_names"][0]).is_file())

    def test_stale_cleanup_returns_only_owned_asset_candidates(self):
        staged = _stage(_bundle(
            "- [解答] ![图](assets/a.png)", {"assets/a.png": _png()}), self.root)
        stage_dir = self.root / "stages" / staged["id"]
        old = time.time() - 1000
        os.utime(stage_dir, (old, old))
        candidates = import_bundle.cleanup_stale_stages(
            self.root / "stages", self.root / "assets",
            max_age_seconds=100, now=time.time())
        self.assertEqual(candidates, set(staged["asset_names"]))
        self.assertFalse(stage_dir.exists())


class PromptAndImportRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app_module = app_module

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = self.app_module.app.test_client()
        self.headers = {"X-CSRF-Token": self.app_module._WRITE_TOKEN}
        self.patches = [
            mock.patch.object(config, "PROMPTS_PATH", self.root / "prompts.json"),
            mock.patch.object(config, "IMPORT_BUNDLE_STAGE_DIR", self.root / "stages"),
            mock.patch.object(config, "ASSETS_DIR", self.root / "assets"),
        ]
        for patcher in self.patches:
            patcher.start()
        self.app_module._md_queues.clear()

    def tearDown(self):
        self.app_module._md_queues.clear()
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def test_prompt_api_crud_and_official_readonly(self):
        listing = self.client.get("/api/prompts").get_json()
        self.assertTrue(listing["ok"])
        self.assertEqual(len(listing["prompts"]), 2)
        response = self.client.post("/api/prompts", headers=self.headers, json={
            "title": "自定义", "category": "题目", "content": "正文",
        })
        self.assertEqual(response.status_code, 201)
        prompt_id = response.get_json()["prompt"]["id"]
        updated = self.client.patch(
            f"/api/prompts/{prompt_id}", headers=self.headers, json={"content": "修改"})
        self.assertEqual(updated.get_json()["prompt"]["content"], "修改")
        readonly = self.client.delete(
            "/api/prompts/official-question-markdown", headers=self.headers)
        self.assertEqual(readonly.status_code, 400)
        deleted = self.client.delete(f"/api/prompts/{prompt_id}", headers=self.headers)
        self.assertTrue(deleted.get_json()["ok"])

    def test_import_preview_splits_solution_and_note_sections(self):
        preview, _collections, _missing = self.app_module._build_import_preview(
            "- [解答] 求 $x+1=2$ 的解。\n\n"
            "  【解析】\n  移项得 $x=1$。\n\n"
            "  ## 备注\n  待校对：原图右下角字迹模糊。",
            all_cols=[],
        )
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["solution"], "【解析】 移项得 $x=1$。")
        self.assertEqual(preview[0]["note"], "待校对：原图右下角字迹模糊。")
        self.assertNotIn("## 备注", preview[0]["body"])
        self.assertNotIn("## 备注", preview[0]["solution"])

    def test_bundle_route_queues_preview_and_skip_reclaims_assets(self):
        payload = _bundle(
            "- [解答] 看图\n\n  ![图](assets/a.png)",
            {"assets/a.png": _png()},
        )
        response = self.client.post(
            "/import/batch", headers=self.headers,
            data={"md_files": (io.BytesIO(payload), "one.qfimport.zip")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        queue_id = response.headers["Location"].rstrip("/").split("/")[-1]
        queued = self.app_module._md_queues[queue_id]["files"][0]
        self.assertTrue(queued["bundle_stage_id"])
        self.assertIn("![[qfimport_", queued["text"])
        assets = list((self.root / "assets").glob("qfimport_*"))
        self.assertEqual(len(assets), 1)

        def purge(names):
            for name in names:
                (self.root / "assets" / name).unlink(missing_ok=True)
            return len(names)

        with mock.patch.object(self.app_module.filestore, "purge_orphan_images",
                               side_effect=purge):
            skipped = self.client.post(
                f"/import/queue/{queue_id}/skip", headers=self.headers)
        self.assertEqual(skipped.status_code, 302)
        self.assertFalse(assets[0].exists())

    def test_bundle_confirm_rewrites_stage_image_to_digest_asset(self):
        staged = _stage(_bundle(
            "- [解答] 看图\n\n  ![图](assets/a.png)",
            {"assets/a.png": _png()},
        ), self.root)
        queue_id = "a" * 32
        self.app_module._md_queues[queue_id] = {
            "files": [{
                "name": "one.qfimport.zip",
                "text": staged["markdown"],
                "bundle_stage_id": staged["id"],
            }],
            "pos": 0,
        }
        captured = {}

        def create(items, folder, **kwargs):
            captured["items"] = items
            return ["qid-1"]

        stage_name = staged["asset_names"][0]
        with mock.patch.object(self.app_module.filestore, "create_questions_batch",
                               side_effect=create), \
                mock.patch.object(self.app_module.filestore, "purge_orphan_images",
                                  return_value=0):
            response = self.client.post("/import", headers=self.headers, data={
                "action": "confirm", "keep": "0",
                "body_0": f"看图\n\n![[{stage_name}]]",
                "solution_0": "", "note_0": "", "type_0": "解答题",
                "batch_source": "测试卷", "queue_id": queue_id,
                "bundle_stage_id": staged["id"],
            })

        self.assertEqual(response.status_code, 302)
        body = captured["items"][0]["body"]
        self.assertNotIn(stage_name, body)
        match = re.search(r"!\[\[(qfimport_[0-9a-f]{64}\.png)\]\]", body)
        self.assertIsNotNone(match)
        self.assertTrue((self.root / "assets" / match.group(1)).is_file())
        self.assertFalse((self.root / "stages" / staged["id"]).exists())

    def test_pasted_images_roll_back_when_later_image_save_fails(self):
        purged: list[set[str]] = []

        def save_image(token, index, data, ext):
            name = f"{token}_{index}.{ext}"
            target = self.root / "assets" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if index == 2:
                raise OSError("second image failed")
            return f"![[{name}]]"

        def purge_images(names):
            current = set(names)
            purged.append(current)
            for name in current:
                (self.root / "assets" / name).unlink(missing_ok=True)
            return len(current)

        with mock.patch.object(self.app_module.filestore, "save_image",
                               side_effect=save_image), \
                mock.patch.object(self.app_module.filestore, "purge_orphan_images",
                                  side_effect=purge_images):
            with self.assertRaisesRegex(OSError, "second image failed"):
                self.app_module._save_import_images([
                    (_png(), "png"), (_png((180, 30, 30)), "png"),
                ])

        self.assertEqual(len(purged), 1)
        self.assertEqual(len(purged[0]), 2)
        self.assertFalse(any((self.root / "assets").iterdir()))

    def test_later_card_image_failure_rolls_back_earlier_card_images(self):
        calls = 0

        def save_image(token, index, data, ext):
            nonlocal calls
            calls += 1
            name = f"{token}_{index}.{ext}"
            target = self.root / "assets" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if calls == 2:
                raise OSError("later card failed")
            return f"![[{name}]]"

        def purge_images(names):
            current = set(names)
            for name in current:
                (self.root / "assets" / name).unlink(missing_ok=True)
            return len(current)

        form = {
            "action": "confirm", "keep": ["0", "1"],
            "body_0": "first", "body_1": "second",
            "type_0": "解答题", "type_1": "解答题",
            "batch_source": "unit-test",
        }
        with self.app_module.app.test_request_context(
                "/import", method="POST", data=form), \
                mock.patch.object(self.app_module, "_read_import_images",
                                  side_effect=[[(_png(), "png")],
                                               [(_png(), "png")]]), \
                mock.patch.object(self.app_module.filestore, "save_image",
                                  side_effect=save_image), \
                mock.patch.object(self.app_module.filestore, "purge_orphan_images",
                                  side_effect=purge_images):
            with self.assertRaisesRegex(OSError, "later card failed"):
                self.app_module.import_md()

        self.assertFalse(any((self.root / "assets").iterdir()))

    def test_confirm_passes_note_to_existing_question_section_api(self):
        captured = {}

        def create(items, folder, **kwargs):
            captured["items"] = items
            return ["qid-1"]

        with mock.patch.object(self.app_module.filestore, "create_questions_batch",
                               side_effect=create):
            response = self.client.post("/import", headers=self.headers, data={
                "action": "confirm", "keep": "0", "body_0": "题目",
                "solution_0": "【解析】答案", "note_0": "待校对：图像",
                "type_0": "解答题", "batch_source": "测试卷",
                "bundle_stage_id": "",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(captured["items"][0]["note"], "待校对：图像")


class PromptImportBundleReleaseTests(unittest.TestCase):
    def test_release_scanner_requires_prompt_and_import_resources(self):
        from tools import verify_desktop_bundle

        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary)
            (dist / "QuizForge.exe").write_bytes(b"MZ")
            problems = verify_desktop_bundle.scan(
                dist, Path(__file__).resolve().parent.parent)
        self.assertTrue(any("questions_to_quizforge.md" in item
                            for item in problems))
        self.assertTrue(any("template_to_quizforge_tex.md" in item
                            for item in problems))
        self.assertTrue(any("import-preview-images.js" in item
                            for item in problems))
        self.assertTrue(any("agent-markdown.bundle.js" in item
                            for item in problems))
        self.assertTrue(any("template-manager.js" in item
                            for item in problems))


if __name__ == "__main__":
    unittest.main()
