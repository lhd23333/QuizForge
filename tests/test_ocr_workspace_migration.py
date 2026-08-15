"""桌面 OCR 工作区隔离与旧安装目录缓存迁移回归。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import config
import converter
import desktop


class OcrWorkspaceConfigTests(unittest.TestCase):
    def test_converter_uses_the_configured_workspace_root(self):
        self.assertEqual(converter._RAW_MD_ROOT, config.OCR_WORKSPACE_ROOT)
        self.assertTrue(Path(config.OCR_WORKSPACE_ROOT).is_absolute())

    def test_desktop_workspace_root_is_derived_from_bank_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            bank_dir = root / "题库"
            state_dir = data_dir / "banks" / "bank-one"
            env = os.environ.copy()
            env.update({
                "QUIZFORGE_DATA_DIR": str(data_dir),
                "QUIZFORGE_BANK": str(bank_dir),
                "QUIZFORGE_BANK_STATE_DIR": str(state_dir),
                "QUIZFORGE_DESKTOP": "1",
            })
            output = subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import config; print(config.OCR_WORKSPACE_ROOT)",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                encoding="utf-8",
            ).strip()

            self.assertEqual(Path(output), state_dir / "raw_md")


class LegacyOcrWorkspaceMigrationTests(unittest.TestCase):
    @staticmethod
    def _make_workspace(root: Path, name: str, *, resume: bool = True) -> Path:
        workspace = root / name
        workspace.mkdir(parents=True)
        (workspace / "_collection_raw.md").write_text(
            f"# {name}\n", encoding="utf-8"
        )
        if resume:
            (workspace / ".mineru_task_text-layer.json").write_text(
                json.dumps({"batch_id": f"batch-{name}"}), encoding="utf-8"
            )
        images = workspace / "images"
        images.mkdir()
        (images / "figure.png").write_bytes(b"image")
        return workspace

    @staticmethod
    def _write_tasks(state_dir: Path, payload: dict) -> Path:
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "conversion_tasks.json"
        path.write_text(
            json.dumps({
                "job": {},
                "batch": {
                    "batch-1": {
                        "updated_at": 1,
                        "payload": payload,
                    }
                },
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _load_payload(tasks_path: Path) -> dict:
        return json.loads(tasks_path.read_text(encoding="utf-8"))["batch"][
            "batch-1"
        ]["payload"]

    def test_migrates_referenced_direct_workspaces_and_atomically_rewrites_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "installed" / "raw_md"
            legacy_root.mkdir(parents=True)
            exam = self._make_workspace(
                legacy_root, "collection_unit_cache_token_exam"
            )
            solution = self._make_workspace(
                legacy_root, "collection_unit_cache_token_solution"
            )
            materialized = self._make_workspace(
                legacy_root, "collection_unit_token_01", resume=False
            )
            unrelated = root / "uploads" / "source.pdf"
            payload = {
                "collection_cache_dirs": [str(exam), str(solution)],
                "cleanup_dirs": [str(exam), str(unrelated)],
                "groups": [{
                    "cleanup_dirs": [str(materialized), str(solution)],
                    # 同值出现在非约定字段时不能被全局字符串替换。
                    "diagnostic_path": str(exam),
                }],
            }
            state_dir = root / "data" / "banks" / "physics"
            tasks_path = self._write_tasks(state_dir, payload)
            real_replace = os.replace

            with mock.patch.object(
                desktop.os, "replace", wraps=real_replace
            ) as replace:
                migrated = desktop._migrate_legacy_ocr_workspaces(
                    legacy_root, state_dir
                )

            self.assertEqual(migrated, 3)
            destination = state_dir / "raw_md"
            for source in (exam, solution, materialized):
                target = destination / source.name
                self.assertEqual(
                    (target / "_collection_raw.md").read_text(encoding="utf-8"),
                    f"# {source.name}\n",
                )
                self.assertEqual((target / "images" / "figure.png").read_bytes(), b"image")
            self.assertEqual(
                json.loads(
                    (destination / exam.name / ".mineru_task_text-layer.json").read_text(
                        encoding="utf-8"
                    )
                )["batch_id"],
                f"batch-{exam.name}",
            )

            rewritten = self._load_payload(tasks_path)
            expected_exam = str(destination / exam.name)
            expected_solution = str(destination / solution.name)
            expected_unit = str(destination / materialized.name)
            self.assertEqual(
                rewritten["collection_cache_dirs"],
                [expected_exam, expected_solution],
            )
            self.assertEqual(
                rewritten["cleanup_dirs"], [expected_exam, str(unrelated)]
            )
            self.assertEqual(
                rewritten["groups"][0]["cleanup_dirs"],
                [expected_unit, expected_solution],
            )
            self.assertEqual(rewritten["groups"][0]["diagnostic_path"], str(exam))
            self.assertTrue(any(
                len(call.args) >= 2 and Path(call.args[1]) == tasks_path
                for call in replace.call_args_list
            ), "conversion_tasks.json 必须通过同目录 os.replace 原子覆盖")
            self.assertEqual(
                [
                    path for path in tasks_path.parent.iterdir()
                    if path.name.endswith(".tmp")
                ],
                [],
            )

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            workspace = self._make_workspace(
                legacy_root, "collection_unit_cache_once_exam"
            )
            state_dir = root / "state"
            tasks_path = self._write_tasks(state_dir, {
                "collection_cache_dirs": [str(workspace)],
                "cleanup_dirs": [str(workspace)],
            })

            self.assertEqual(
                desktop._migrate_legacy_ocr_workspaces(legacy_root, state_dir), 1
            )
            first_snapshot = tasks_path.read_bytes()
            self.assertEqual(
                desktop._migrate_legacy_ocr_workspaces(legacy_root, state_dir), 0
            )

            self.assertEqual(tasks_path.read_bytes(), first_snapshot)
            self.assertEqual(
                [path.name for path in (state_dir / "raw_md").iterdir()],
                [workspace.name],
            )

    def test_rejects_unreferenced_nested_outside_and_non_directory_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            valid = self._make_workspace(
                legacy_root, "collection_unit_cache_valid_exam"
            )
            unreferenced = self._make_workspace(
                legacy_root, "collection_unit_cache_unreferenced_exam"
            )
            nested_parent = legacy_root / "nested"
            nested = self._make_workspace(
                nested_parent, "collection_unit_cache_nested_exam"
            )
            outside = self._make_workspace(
                root, "collection_unit_cache_outside_exam"
            )
            ordinary_file = legacy_root / "collection_unit_cache_file_exam"
            ordinary_file.write_text("not a directory", encoding="utf-8")
            # 普通逐题审核任务的目录并不以 collection_unit_ 开头；只要任务明确
            # 引用旧根目录的直接子工作区，也必须保住，不能只救结构合集。
            ordinary_workspace = self._make_workspace(
                legacy_root, "ordinary_cache_exam"
            )
            state_dir = root / "state"
            original_paths = [
                str(valid), str(nested), str(outside), str(ordinary_file),
                str(ordinary_workspace),
            ]
            tasks_path = self._write_tasks(state_dir, {
                "cleanup_dirs": original_paths,
            })

            migrated = desktop._migrate_legacy_ocr_workspaces(
                legacy_root, state_dir
            )

            self.assertEqual(migrated, 2)
            destination = state_dir / "raw_md"
            self.assertTrue((destination / valid.name).is_dir())
            self.assertTrue((destination / ordinary_workspace.name).is_dir())
            for rejected in (unreferenced, nested, outside, ordinary_file):
                self.assertFalse((destination / rejected.name).exists())
            rewritten = self._load_payload(tasks_path)["cleanup_dirs"]
            self.assertEqual(rewritten[0], str(destination / valid.name))
            self.assertEqual(rewritten[1:4], original_paths[1:4])
            self.assertEqual(
                rewritten[4], str(destination / ordinary_workspace.name)
            )

    def test_migrates_review_and_collection_file_path_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            workspace = self._make_workspace(legacy_root, "ordinary_review_scope")
            raw_file = workspace / "paper_raw.md"
            raw_file.write_text("# raw\n", encoding="utf-8")
            state_dir = root / "state"
            tasks_path = self._write_tasks(state_dir, {
                "collection_raw_path": str(raw_file),
                "pending": {
                    "extract_dirs": [{"dir": str(workspace), "stem": "paper"}],
                },
            })

            migrated = desktop._migrate_legacy_ocr_workspaces(
                legacy_root, state_dir, strict=True
            )

            self.assertEqual(migrated, 1)
            target = state_dir / "raw_md" / workspace.name
            rewritten = self._load_payload(tasks_path)
            self.assertEqual(rewritten["collection_raw_path"], str(target / raw_file.name))
            self.assertEqual(rewritten["pending"]["extract_dirs"][0]["dir"], str(target))

    def test_strict_migration_rejects_a_nested_directory_reference(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            nested = self._make_workspace(
                legacy_root / "nested", "collection_unit_cache_nested_exam"
            )
            state_dir = root / "state"
            tasks_path = self._write_tasks(
                state_dir, {"cleanup_dirs": [str(nested)]}
            )

            with self.assertRaisesRegex(RuntimeError, "不是直接工作区"):
                desktop._migrate_legacy_ocr_workspaces(
                    legacy_root, state_dir, strict=True
                )

            self.assertEqual(
                self._load_payload(tasks_path)["cleanup_dirs"], [str(nested)]
            )

    def test_existing_target_must_match_the_complete_source_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            workspace = self._make_workspace(legacy_root, "review_scope")
            state_dir = root / "state"
            tasks_path = self._write_tasks(
                state_dir, {"cleanup_dirs": [str(workspace)]}
            )
            target = state_dir / "raw_md" / workspace.name
            target.parent.mkdir(parents=True)
            shutil.copytree(workspace, target)
            (target / "images" / "figure.png").write_bytes(b"different")

            with self.assertRaisesRegex(RuntimeError, "内容不一致"):
                desktop._migrate_legacy_ocr_workspaces(
                    legacy_root, state_dir, strict=True
                )

            self.assertEqual(
                self._load_payload(tasks_path)["cleanup_dirs"], [str(workspace)]
            )
            self.assertEqual((target / "images" / "figure.png").read_bytes(), b"different")

    def test_strict_migration_rejects_a_link_as_existing_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            workspace = self._make_workspace(legacy_root, "review_scope")
            state_dir = root / "state"
            tasks_path = self._write_tasks(
                state_dir, {"cleanup_dirs": [str(workspace)]}
            )
            outside = self._make_workspace(root / "outside", "target")
            target = state_dir / "raw_md" / workspace.name
            target.parent.mkdir(parents=True)
            try:
                target.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"当前系统不允许创建测试符号链接：{exc}")

            with self.assertRaisesRegex(RuntimeError, "链接或联接点"):
                desktop._migrate_legacy_ocr_workspaces(
                    legacy_root, state_dir, strict=True
                )

            self.assertEqual(
                self._load_payload(tasks_path)["cleanup_dirs"], [str(workspace)]
            )

    def test_update_helper_rejects_active_task_state_before_rewriting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            workspace = self._make_workspace(legacy_root, "active_scope")
            app_data = root / "appdata"
            state_dir = app_data / "banks" / "physics"
            tasks_path = self._write_tasks(state_dir, {
                "status": "converting",
                "groups": [{
                    "status": "pending",
                    "in_flight": True,
                    "cleanup_dirs": [str(workspace)],
                }],
            })

            with self.assertRaisesRegex(RuntimeError, "未完成转换批次"):
                desktop.migrate_all_legacy_ocr_workspaces(
                    legacy_root, app_data
                )

            self.assertFalse((state_dir / "raw_md").exists())
            self.assertEqual(
                self._load_payload(tasks_path)["groups"][0]["cleanup_dirs"],
                [str(workspace)],
            )

    def test_does_not_follow_a_direct_symbolic_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            target = self._make_workspace(root / "outside", "real_workspace")
            link = legacy_root / "collection_unit_cache_link_exam"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"当前系统不允许创建测试符号链接：{exc}")
            state_dir = root / "state"
            tasks_path = self._write_tasks(state_dir, {
                "collection_cache_dirs": [str(link)],
                "cleanup_dirs": [str(link)],
            })

            migrated = desktop._migrate_legacy_ocr_workspaces(
                legacy_root, state_dir
            )

            self.assertEqual(migrated, 0)
            self.assertFalse((state_dir / "raw_md" / link.name).exists())
            rewritten = self._load_payload(tasks_path)
            self.assertEqual(rewritten["collection_cache_dirs"], [str(link)])
            self.assertEqual(rewritten["cleanup_dirs"], [str(link)])

    def test_two_banks_only_migrate_their_own_referenced_workspaces(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            math_workspace = self._make_workspace(
                legacy_root, "collection_unit_cache_math_exam"
            )
            physics_workspace = self._make_workspace(
                legacy_root, "collection_unit_cache_physics_exam"
            )
            math_state = root / "banks" / "math"
            physics_state = root / "banks" / "physics"
            math_tasks = self._write_tasks(math_state, {
                "cleanup_dirs": [str(math_workspace)],
            })
            physics_tasks = self._write_tasks(physics_state, {
                "cleanup_dirs": [str(physics_workspace)],
            })

            self.assertEqual(
                desktop._migrate_legacy_ocr_workspaces(legacy_root, math_state), 1
            )
            self.assertTrue((math_state / "raw_md" / math_workspace.name).is_dir())
            self.assertFalse((math_state / "raw_md" / physics_workspace.name).exists())

            self.assertEqual(
                desktop._migrate_legacy_ocr_workspaces(legacy_root, physics_state), 1
            )
            self.assertTrue(
                (physics_state / "raw_md" / physics_workspace.name).is_dir()
            )
            self.assertFalse(
                (physics_state / "raw_md" / math_workspace.name).exists()
            )
            self.assertEqual(
                self._load_payload(math_tasks)["cleanup_dirs"],
                [str(math_state / "raw_md" / math_workspace.name)],
            )
            self.assertEqual(
                self._load_payload(physics_tasks)["cleanup_dirs"],
                [str(physics_state / "raw_md" / physics_workspace.name)],
            )

    def test_update_helper_migrates_each_direct_bank_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy_root = root / "installed" / "raw_md"
            legacy_root.mkdir(parents=True)
            first = self._make_workspace(
                legacy_root, "collection_unit_cache_first_exam"
            )
            second = self._make_workspace(
                legacy_root, "collection_unit_cache_second_exam"
            )
            app_data = root / "appdata"
            first_state = app_data / "banks" / "first"
            second_state = app_data / "banks" / "second"
            self._write_tasks(first_state, {"cleanup_dirs": [str(first)]})
            self._write_tasks(second_state, {"cleanup_dirs": [str(second)]})
            # 非目录与非 banks 直接子目录都不能被更新器顺手扫描。
            (app_data / "banks" / "ordinary.txt").write_text(
                "ignore", encoding="utf-8"
            )

            migrated = desktop.migrate_all_legacy_ocr_workspaces(
                legacy_root, app_data
            )

            self.assertEqual(migrated, 2)
            self.assertTrue((first_state / "raw_md" / first.name).is_dir())
            self.assertTrue((second_state / "raw_md" / second.name).is_dir())


if __name__ == "__main__":
    unittest.main()
