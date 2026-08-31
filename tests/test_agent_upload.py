"""Agent ZIP 暂存与写入确认状态机的离线回归。"""

from __future__ import annotations

import io
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import config
import agent_upload
import agent_approvals
from agent_core import AgentError, AgentRuntime


def _zip_bytes(entries: list[tuple[str, bytes, int | None]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if mode is not None:
                info.external_attr = (mode & 0xFFFF) << 16
            archive.writestr(info, data)
    return output.getvalue()


class _Storage:
    def __init__(self, data: bytes, filename: str = "上传.zip"):
        self.filename = filename
        self.stream = io.BytesIO(data)


class AgentUploadTests(unittest.TestCase):
    def test_single_document_is_staged_without_starting_a_job(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                config, "UPLOAD_DIR", Path(td) / "uploads"):
            manifest = agent_upload.stage_file(
                _Storage(b"%PDF-1.7\nexam", filename="试卷.pdf"))
            self.assertEqual("staged", manifest["status"])
            self.assertIsNone(manifest["job_id"])
            self.assertEqual("pdf", manifest["files"][0]["kind"])
            self.assertTrue(agent_upload.resolve_stage_file(
                manifest["id"], "试卷.pdf").is_file())
            agent_upload.discard_stage(manifest["id"])

    def test_valid_archive_returns_roles_without_absolute_paths(self):
        data = _zip_bytes([
            ("2026/期末卷.pdf", b"%PDF-1.7\nexam", None),
            ("2026/期末卷答案.pdf", b"%PDF-1.7\nsolution", None),
        ])
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                config, "UPLOAD_DIR", Path(td) / "uploads"):
            manifest = agent_upload.stage_zip(_Storage(data))
            self.assertEqual("staged", manifest["status"])
            self.assertEqual({"exam", "solution"},
                             {item["role"] for item in manifest["files"]})
            self.assertNotIn(str(Path(td)), str(manifest))
            for item in manifest["files"]:
                target = agent_upload.resolve_stage_file(
                    manifest["id"], item["path"])
                self.assertTrue(target.is_file())
            self.assertTrue(agent_upload.discard_stage(manifest["id"]))

    def test_traversal_and_symlink_members_are_rejected(self):
        traversal = _zip_bytes([("../逃逸.pdf", b"%PDF-1.7", None)])
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                config, "UPLOAD_DIR", Path(td) / "uploads"):
            with self.assertRaisesRegex(agent_upload.AgentUploadError, "越界"):
                agent_upload.stage_zip(_Storage(traversal))
            self.assertFalse((Path(td) / "逃逸.pdf").exists())

            symlink = _zip_bytes([("link.pdf", b"target", stat.S_IFLNK)])
            with self.assertRaisesRegex(agent_upload.AgentUploadError, "符号链接"):
                agent_upload.stage_zip(_Storage(symlink))

    def test_nested_archive_and_unsupported_members_are_rejected(self):
        nested = _zip_bytes([("nested.zip", b"PK\x03\x04", None)])
        unsupported = _zip_bytes([("说明.txt", b"readme", None)])
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                config, "UPLOAD_DIR", Path(td) / "uploads"):
            with self.assertRaisesRegex(agent_upload.AgentUploadError, "嵌套"):
                agent_upload.stage_zip(_Storage(nested))
            with self.assertRaisesRegex(agent_upload.AgentUploadError, "不支持"):
                agent_upload.stage_zip(_Storage(unsupported))

    def test_compression_bomb_is_rejected_before_extraction(self):
        bomb = _zip_bytes([("bomb.pdf", b"0" * (2 * 1024 * 1024), None)])
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                config, "UPLOAD_DIR", Path(td) / "uploads"):
            with self.assertRaisesRegex(agent_upload.AgentUploadError, "压缩比"):
                agent_upload.stage_zip(_Storage(bomb))
            stage_root = Path(td) / "uploads" / "agent"
            self.assertFalse(any(stage_root.glob("*/bomb.pdf")))


class AgentApprovalTests(unittest.TestCase):
    def test_context_change_invalidates_pending_approval_and_redacts(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = AgentRuntime(Path(td))
            session = runtime.new_session()
            store = agent_approvals.ApprovalStore()
            approval = store.create(
                session, "update_question", "修改题目",
                {"api_key": "do-not-store", "id": "q1"})
            self.assertEqual("[已隐藏]", approval["arguments"]["api_key"])
            runtime.set_workdir(session["id"], ".")
            # 切到仅聊天后，原先针对题库根的计划不能继续执行。
            runtime.update_session(session["id"], scope="chat")
            with self.assertRaisesRegex(agent_approvals.ApprovalError, "工作目录已变化"):
                store.approve(runtime.get_session(session["id"]), approval["id"])

    def test_approval_is_bound_to_session_and_transitions_once(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = AgentRuntime(Path(td))
            first = runtime.new_session()
            second = runtime.new_session()
            store = agent_approvals.ApprovalStore()
            approval = store.create(first, "create_folder", "新建目录")
            with self.assertRaisesRegex(agent_approvals.ApprovalError, "不属于"):
                store.approve(second, approval["id"])
            approved = store.approve(first, approval["id"], result={"id": "x"})
            self.assertEqual("approved", approved["status"])
            with self.assertRaisesRegex(agent_approvals.ApprovalError, "不能再次"):
                store.cancel(first, approval["id"])

    def test_runtime_update_is_atomic_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = AgentRuntime(Path(td))
            session = runtime.new_session()
            with self.assertRaises(AgentError):
                runtime.update_session(session["id"], mode="danger",
                                       workdir="../outside")
            current = runtime.public_session(runtime.get_session(session["id"]))
            self.assertEqual("standard", current["mode"])
            self.assertEqual("", current["workdir_id"])


if __name__ == "__main__":
    unittest.main()
