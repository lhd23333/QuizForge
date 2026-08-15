"""共享图片目录与跨题库孤儿清理的安全边界。"""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import config
import filestore


def _write_md(path: Path, body: str, frontmatter: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\n{frontmatter}---\n\n{body}" if frontmatter else body
    path.write_text(text, encoding="utf-8", newline="\n")


def _old_file(path: Path, data: bytes = b"image") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    old = time.time_ns() - 10 * 60 * 1_000_000_000
    os.utime(path, ns=(old, old))


class SharedAssetGcTests(unittest.TestCase):
    def _patch_config(self, data: Path, bank: Path, assets: Path):
        return mock.patch.multiple(
            config, DATA_DIR=data, BANK_DIR=bank, ASSETS_DIR=assets,
            TRASH_DIR=bank / ".trash", IMAGES_DIR=assets,
        )

    def test_scan_counts_refs_across_banks_trash_handouts_and_redraw_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            math = root / "math"
            physics = root / "physics"
            assets = root / "shared-assets"
            for path in (data, math, physics, assets):
                path.mkdir()
            (data / "desktop.json").write_text(json.dumps({
                "bank_dir": str(math),
                "assets_dir": str(assets),
                "banks": [{"path": str(math)}, {"path": str(physics)}],
            }), encoding="utf-8")
            _write_md(math / "q1.md", "题干 ![[math.png]]")
            _write_md(physics / "q2.md", "题干 ![[physics.png]]")
            _write_md(math / ".trash" / "deleted.md", "题干 ![[trash.png]]")
            _write_md(
                math / "_handouts" / "讲义.md", "插图 ![[tikz.svg]]",
                "img_originals:\n- orig: original.png\n",
            )
            for name in ("math.png", "physics.png", "trash.png", "original.png",
                         "tikz.svg", "tikz.pdf", "orphan.png"):
                _old_file(assets / name, name.encode("ascii"))

            with self._patch_config(data, math, assets):
                scan = filestore.scan_orphan_assets()

            self.assertEqual(scan["bank_count"], 2)
            self.assertEqual(scan["markdown_files"], 4)
            self.assertEqual(scan["asset_files"], 7)
            self.assertEqual(scan["referenced_files"], 6)
            self.assertEqual(scan["missing_references"], 0)
            self.assertEqual([item["name"] for item in scan["orphans"]],
                             ["orphan.png"])

    def test_delete_rescans_and_preserves_candidate_that_gained_a_reference(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            bank = root / "bank"
            assets = root / "assets"
            for path in (data, bank, assets):
                path.mkdir()
            (data / "desktop.json").write_text(json.dumps({
                "bank_dir": str(bank), "banks": [{"path": str(bank)}],
            }), encoding="utf-8")
            _old_file(assets / "candidate.png")

            with self._patch_config(data, bank, assets):
                scan = filestore.scan_orphan_assets()
                _write_md(bank / "new.md", "新增引用 ![[candidate.png]]")
                result = filestore.delete_scanned_orphan_assets(scan)

            self.assertEqual(result["removed"], 0)
            self.assertEqual(result["changed_or_skipped"], 1)
            self.assertTrue((assets / "candidate.png").is_file())

    def test_delete_removes_only_old_unchanged_orphans(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            bank = root / "bank"
            assets = root / "assets"
            for path in (data, bank, assets):
                path.mkdir()
            (data / "desktop.json").write_text(json.dumps({
                "bank_dir": str(bank), "banks": [{"path": str(bank)}],
            }), encoding="utf-8")
            _old_file(assets / "old.png", b"old")
            (assets / "recent.png").write_bytes(b"recent")

            with self._patch_config(data, bank, assets):
                scan = filestore.scan_orphan_assets()
                result = filestore.delete_scanned_orphan_assets(scan)

            self.assertEqual(scan["orphan_count"], 1)
            self.assertEqual(scan["recent_unreferenced"], 1)
            self.assertEqual(result["removed"], 1)
            self.assertFalse((assets / "old.png").exists())
            self.assertTrue((assets / "recent.png").is_file())

    def test_reference_matching_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            bank = root / "bank"
            assets = root / "assets"
            for path in (data, bank, assets):
                path.mkdir()
            (data / "desktop.json").write_text(json.dumps({
                "bank_dir": str(bank), "banks": [{"path": str(bank)}],
            }), encoding="utf-8")
            _write_md(bank / "q.md", "题干 ![[FIGURE.PNG]]")
            _old_file(assets / "figure.png")

            with self._patch_config(data, bank, assets):
                scan = filestore.scan_orphan_assets()

            self.assertEqual(scan["orphan_count"], 0)
            self.assertEqual(scan["referenced_files"], 1)

    def test_unavailable_registered_bank_blocks_scan_and_delete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            bank = root / "bank"
            assets = root / "assets"
            for path in (data, bank, assets):
                path.mkdir()
            missing = root / "disconnected"
            (data / "desktop.json").write_text(json.dumps({
                "bank_dir": str(bank),
                "banks": [{"path": str(bank)}, {"path": str(missing)}],
            }), encoding="utf-8")
            _old_file(assets / "orphan.png")

            with self._patch_config(data, bank, assets):
                with self.assertRaises(filestore.AssetAuditError):
                    filestore.scan_orphan_assets()

            self.assertTrue((assets / "orphan.png").is_file())

    def test_permanent_question_cleanup_keeps_image_used_by_other_bank(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            math = root / "math"
            physics = root / "physics"
            assets = root / "assets"
            for path in (data, math, physics, assets):
                path.mkdir()
            (data / "desktop.json").write_text(json.dumps({
                "bank_dir": str(math),
                "banks": [{"path": str(math)}, {"path": str(physics)}],
            }), encoding="utf-8")
            _write_md(physics / "q.md", "物理题 ![[shared.png]]")
            _old_file(assets / "shared.png")

            with self._patch_config(data, math, assets):
                removed = filestore.purge_orphan_images({"shared.png"})

            self.assertEqual(removed, 0)
            self.assertTrue((assets / "shared.png").is_file())


if __name__ == "__main__":
    unittest.main()
