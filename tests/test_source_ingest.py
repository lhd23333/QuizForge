from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import source_ingest


class SourceIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.in_dir = self.root / "好题"
        self.out_dir = self.root / "输出"
        self.in_dir.mkdir()
        self.out_dir.mkdir()
        self.settings = mock.patch.object(config, "SOURCE_SETTINGS_PATH",
                                          self.root / "settings.json")
        self.settings.start()
        self.tasks = mock.patch.object(config, "TASKS_PATH", self.root / "tasks.json")
        self.tasks.start()
        self.workspace = mock.patch.object(config, "SOURCE_WORKSPACE_DIR",
                                           self.root / "workspace")
        self.workspace.start()

    def tearDown(self):
        self.workspace.stop()
        self.tasks.stop()
        self.settings.stop()
        self.tmp.cleanup()

    def test_stability_requires_two_unchanged_observations(self):
        path = self.in_dir / "q.png"
        path.write_bytes(b"x")
        stable, state = source_ingest.is_stable_file(path, None, now=1.0)
        self.assertFalse(stable)
        stable, state = source_ingest.is_stable_file(path, state, now=2.0)
        self.assertTrue(stable)

    def test_scan_deduplicates_content_and_ignores_output_tree(self):
        source = self.in_dir / "q.png"
        source.write_bytes(b"x")
        service = source_ingest.SourceIngestService(
            profiles={"good_question": {"input_dir": str(self.in_dir),
                                        "output_dir": str(self.out_dir),
                                        "enabled": True}},
            interval=999)
        with mock.patch.object(source_ingest, "_now", side_effect=[1.0, 2.0, 3.0]):
            service.scan_once()
            result = service.scan_once()
        self.assertEqual(len(result), 1)
        (self.out_dir / "generated.png").write_bytes(b"out")
        service.profiles["good_question"]["input_dir"] = str(self.out_dir)
        service.profiles["good_question"]["output_dir"] = str(self.out_dir)
        self.assertEqual(service.scan_once(), [])

    def test_pause_and_resume_control_scanning(self):
        source = self.in_dir / "q.png"
        source.write_bytes(b"x")
        service = source_ingest.SourceIngestService(
            profiles={"good_question": {"input_dir": str(self.in_dir),
                                        "output_dir": str(self.out_dir),
                                        "enabled": True}},
            interval=999)
        service.pause("good_question")
        self.assertEqual(service.scan_once(), [])
        service.resume("good_question")
        self.assertIsInstance(service.scan_once(), list)


if __name__ == "__main__":
    unittest.main()
