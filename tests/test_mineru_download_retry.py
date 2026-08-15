"""MinerU 结果下载续传与 batch 恢复：只模拟网络，不访问真实服务。"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
ALPHA_ROOT = ROOT / "vendor" / "project_alpha"
if str(ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(ALPHA_ROOT))

from src.exceptions import MinerUAPIError
from src.mineru_client import MineruClient


class _Response:
    def __init__(
        self,
        status_code=200,
        content=b"",
        *,
        chunks=None,
        headers=None,
        json_body=None,
    ):
        self.status_code = status_code
        self.content = content
        self._chunks = [content] if chunks is None else list(chunks)
        self.headers = dict(headers or {})
        self._json_body = json_body
        self.text = ""
        self.closed = False
        self.chunk_sizes = []

    def iter_content(self, chunk_size):
        self.chunk_sizes.append(chunk_size)
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    def json(self):
        if self._json_body is None:
            raise ValueError("没有 JSON")
        return self._json_body

    def close(self):
        self.closed = True


def _zip_bytes(markdown="1. 测试题"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("full.md", markdown)
    return output.getvalue()


class MinerUDownloadRetryTests(unittest.TestCase):
    def test_ordinary_ssl_error_retries_same_result_url(self):
        from src import mineru_client

        success = _Response(content=_zip_bytes())
        responses = [
            mineru_client.requests.exceptions.SSLError("模拟 TLS EOF"),
            success,
        ]
        with (mock.patch.object(mineru_client.requests, "get",
                                side_effect=responses) as request_get,
              mock.patch.object(mineru_client.time, "sleep") as sleep):
            markdown, name = MineruClient._download_and_extract(
                "https://cdn.example/result.zip", "paper", None)

        self.assertEqual("1. 测试题", markdown)
        self.assertEqual("full.md", name)
        self.assertEqual(2, request_get.call_count)
        for call in request_get.call_args_list:
            self.assertEqual("https://cdn.example/result.zip", call.args[0])
            self.assertEqual("identity", call.kwargs["headers"]["Accept-Encoding"])
            self.assertTrue(call.kwargs["stream"])
            self.assertEqual((30, 300), call.kwargs["timeout"])
        self.assertEqual([1024 * 1024], success.chunk_sizes)
        sleep.assert_called_once_with(1)

    def test_proxy_unexpected_eof_switches_result_download_to_direct(self):
        from src import mineru_client

        eof = mineru_client.requests.exceptions.SSLError(
            mineru_client.ssl.SSLEOFError(
                8, "[SSL: UNEXPECTED_EOF_WHILE_READING] 提前断开"))
        session = mock.Mock()
        session.get.return_value = _Response(
            content=_zip_bytes("绕过异常代理后成功"))
        with (mock.patch.object(mineru_client.requests, "get",
                                side_effect=eof) as request_get,
              mock.patch.object(mineru_client.requests, "Session",
                                return_value=session) as session_factory,
              mock.patch.object(mineru_client.time, "sleep") as sleep):
            markdown, _ = MineruClient._download_and_extract(
                "https://cdn.example/result.zip", "paper", None)

        self.assertEqual("绕过异常代理后成功", markdown)
        request_get.assert_called_once()
        session_factory.assert_called_once_with()
        self.assertIs(session.trust_env, False)
        session.get.assert_called_once()
        self.assertEqual("https://cdn.example/result.zip",
                         session.get.call_args.args[0])
        session.close.assert_called_once_with()
        sleep.assert_not_called()

    def test_stream_eof_keeps_prefix_and_direct_session_uses_range(self):
        from src import mineru_client

        data = _zip_bytes("代理中途断开后直连续传")
        split = 29
        eof = mineru_client.requests.exceptions.SSLError(
            mineru_client.ssl.SSLEOFError(
                8, "[SSL: UNEXPECTED_EOF_WHILE_READING] 提前断开"))
        proxied = _Response(
            chunks=[data[:split], eof],
            headers={"Content-Length": str(len(data))},
        )
        session = mock.Mock()
        session.get.return_value = _Response(
            status_code=206,
            content=data[split:],
            headers={
                "Content-Length": str(len(data) - split),
                "Content-Range": f"bytes {split}-{len(data)-1}/{len(data)}",
            },
        )
        with (mock.patch.object(mineru_client.requests, "get",
                                return_value=proxied),
              mock.patch.object(mineru_client.requests, "Session",
                                return_value=session),
              mock.patch.object(mineru_client.time, "sleep")):
            markdown, _ = MineruClient._download_and_extract(
                "https://cdn.example/result.zip", "paper", None)

        self.assertEqual("代理中途断开后直连续传", markdown)
        self.assertEqual(f"bytes={split}-",
                         session.get.call_args.kwargs["headers"]["Range"])

    def test_non_eof_ssl_error_does_not_bypass_proxy(self):
        from src import mineru_client

        cert_error = mineru_client.requests.exceptions.SSLError(
            "certificate verify failed")
        responses = [cert_error, _Response(content=_zip_bytes("代理重试成功"))]
        with (mock.patch.object(mineru_client.requests, "get",
                                side_effect=responses) as request_get,
              mock.patch.object(mineru_client.requests, "Session") as session,
              mock.patch.object(mineru_client.time, "sleep") as sleep):
            markdown, _ = MineruClient._download_and_extract(
                "https://cdn.example/result.zip", "paper", None)

        self.assertEqual("代理重试成功", markdown)
        self.assertEqual(2, request_get.call_count)
        session.assert_not_called()
        sleep.assert_called_once_with(1)

    def test_direct_fallback_failure_keeps_total_attempt_bound(self):
        from src import mineru_client

        eof = mineru_client.requests.exceptions.SSLError(
            mineru_client.ssl.SSLEOFError(
                8, "[SSL: UNEXPECTED_EOF_WHILE_READING] 提前断开"))
        direct_error = mineru_client.requests.exceptions.ConnectionError("断开")
        session = mock.Mock()
        session.get.side_effect = [
            direct_error for _ in range(mineru_client._DOWNLOAD_ATTEMPTS - 1)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "result.zip.part"
            with (mock.patch.object(mineru_client.requests, "get",
                                    side_effect=eof) as request_get,
                  mock.patch.object(mineru_client.requests, "Session",
                                    return_value=session),
                  mock.patch.object(mineru_client.time, "sleep"),
                  self.assertRaisesRegex(
                      MinerUAPIError,
                      rf"尝试下载 {mineru_client._DOWNLOAD_ATTEMPTS} 次")):
                MineruClient._download_and_extract(
                    "https://cdn.example/result.zip", "paper", None, part)

            request_get.assert_called_once()
            self.assertEqual(mineru_client._DOWNLOAD_ATTEMPTS - 1,
                             session.get.call_count)
            session.close.assert_called_once_with()
            self.assertFalse(part.exists())

    def test_interrupted_stream_resumes_with_range(self):
        from src import mineru_client

        data = _zip_bytes("2. 断点续传成功")
        split = 23
        interrupted = mineru_client.requests.exceptions.ConnectionError("断开")
        responses = [
            _Response(
                chunks=[data[:split], interrupted],
                headers={"Content-Length": str(len(data))},
            ),
            _Response(
                status_code=206,
                content=data[split:],
                headers={
                    "Content-Length": str(len(data) - split),
                    "Content-Range": (
                        f"bytes {split}-{len(data) - 1}/{len(data)}"),
                },
            ),
        ]
        with (mock.patch.object(mineru_client.requests, "get",
                                side_effect=responses) as request_get,
              mock.patch.object(mineru_client.time, "sleep")):
            markdown, _ = MineruClient._download_and_extract(
                "https://cdn.example/result.zip", "paper", None)

        self.assertEqual("2. 断点续传成功", markdown)
        first_headers = request_get.call_args_list[0].kwargs["headers"]
        second_headers = request_get.call_args_list[1].kwargs["headers"]
        self.assertNotIn("Range", first_headers)
        self.assertEqual(f"bytes={split}-", second_headers["Range"])

    def test_range_ignored_200_overwrites_old_partial(self):
        from src import mineru_client

        data = _zip_bytes("3. 已覆盖旧前缀")
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "result.zip.part"
            part.write_bytes(b"stale-prefix")
            with mock.patch.object(
                    mineru_client.requests, "get",
                    return_value=_Response(content=data)) as request_get:
                markdown, _ = MineruClient._download_and_extract(
                    "https://cdn.example/result.zip", "paper", None, part)

            self.assertEqual("3. 已覆盖旧前缀", markdown)
            self.assertEqual(
                f"bytes={len(b'stale-prefix')}-",
                request_get.call_args.kwargs["headers"]["Range"],
            )
            self.assertFalse(part.exists())

    def test_wrong_content_range_is_reset_before_retry(self):
        from src import mineru_client

        data = _zip_bytes("4. 起点校验成功")
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "result.zip.part"
            part.write_bytes(b"old")
            responses = [
                _Response(
                    status_code=206,
                    content=data,
                    headers={"Content-Range": f"bytes 0-{len(data)-1}/{len(data)}"},
                ),
                _Response(content=data),
            ]
            with (mock.patch.object(mineru_client.requests, "get",
                                    side_effect=responses) as request_get,
                  mock.patch.object(mineru_client.time, "sleep")):
                markdown, _ = MineruClient._download_and_extract(
                    "https://cdn.example/result.zip", "paper", None, part)

            self.assertEqual("4. 起点校验成功", markdown)
            self.assertEqual("bytes=3-", request_get.call_args_list[0].kwargs[
                "headers"]["Range"])
            self.assertNotIn("Range", request_get.call_args_list[1].kwargs[
                "headers"])

    def test_content_range_length_mismatch_is_not_appended(self):
        from src import mineru_client

        data = _zip_bytes("4b. 分段长度校验成功")
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "result.zip.part"
            part.write_bytes(b"old")
            responses = [
                _Response(
                    status_code=206,
                    content=b"x",
                    headers={
                        "Content-Range": "bytes 3-8/9",
                        "Content-Length": "1",
                    },
                ),
                _Response(content=data),
            ]
            with (mock.patch.object(mineru_client.requests, "get",
                                    side_effect=responses) as request_get,
                  mock.patch.object(mineru_client.time, "sleep")):
                markdown, _ = MineruClient._download_and_extract(
                    "https://cdn.example/result.zip", "paper", None, part)

            self.assertEqual("4b. 分段长度校验成功", markdown)
            self.assertEqual("bytes=3-", request_get.call_args_list[0].kwargs[
                "headers"]["Range"])
            self.assertNotIn("Range", request_get.call_args_list[1].kwargs[
                "headers"])

    def test_416_uses_already_complete_partial(self):
        from src import mineru_client

        data = _zip_bytes("5. 本地文件已经完整")
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "result.zip.part"
            part.write_bytes(data)
            response = _Response(status_code=416)
            with mock.patch.object(
                    mineru_client.requests, "get", return_value=response):
                markdown, _ = MineruClient._download_and_extract(
                    "https://cdn.example/result.zip", "paper", None, part)

            self.assertEqual("5. 本地文件已经完整", markdown)
            self.assertFalse(part.exists())
            self.assertTrue(response.closed)

    def test_crc_failure_switches_to_bounded_range_download(self):
        from src import mineru_client

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("full.md", "6. CRC 恢复")
        valid = output.getvalue()
        corrupt = bytearray(valid)
        marker = "6. CRC 恢复".encode("utf-8")
        marker_at = corrupt.find(marker)
        self.assertGreater(marker_at, 0)
        corrupt[marker_at] ^= 0x01
        def fake_get(_url, *, headers, **_kwargs):
            range_header = headers.get("Range")
            if not range_header:
                return _Response(content=bytes(corrupt))
            match = mineru_client.re.fullmatch(r"bytes=(\d+)-(\d+)", range_header)
            self.assertIsNotNone(match)
            start, requested_end = map(int, match.groups())
            end = min(requested_end, len(valid) - 1)
            return _Response(
                status_code=206,
                content=valid[start:end + 1],
                headers={
                    "Content-Length": str(end - start + 1),
                    "Content-Range": f"bytes {start}-{end}/{len(valid)}",
                },
            )

        with (mock.patch.object(mineru_client, "_RANGE_FALLBACK_CHUNK_SIZE", 31),
              mock.patch.object(mineru_client.requests, "get",
                                side_effect=fake_get) as request_get,
              mock.patch.object(mineru_client.time, "sleep") as sleep):
            markdown, _ = MineruClient._download_and_extract(
                "https://cdn.example/result.zip", "paper", None)

        self.assertEqual("6. CRC 恢复", markdown)
        self.assertGreater(request_get.call_count, 2)
        self.assertNotIn(
            "Range", request_get.call_args_list[0].kwargs["headers"])
        for call in request_get.call_args_list[1:]:
            self.assertRegex(
                call.kwargs["headers"]["Range"], r"^bytes=\d+-\d+$")
        sleep.assert_called_once_with(1)

    def test_bounded_range_keeps_destination_atomic_and_resumes_by_chunk(self):
        from src import mineru_client

        data = _zip_bytes("6b. 分段断点原子替换")
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "result.zip.part"
            work = Path(tmp) / ".result.range.tmp"
            destination.write_bytes(b"stable-old-result")
            calls = []

            def interrupted_get(_url, *, headers, **_kwargs):
                calls.append(headers["Range"])
                start, requested_end = map(
                    int, headers["Range"][len("bytes="):].split("-"))
                end = min(requested_end, len(data) - 1)
                if start:
                    return _Response(
                        status_code=206,
                        chunks=[data[start:start + 2],
                                mineru_client.requests.exceptions.ConnectionError(
                                    "模拟第二段中断")],
                        headers={
                            "Content-Length": str(end - start + 1),
                            "Content-Range": (
                                f"bytes {start}-{end}/{len(data)}"),
                        },
                    )
                return _Response(
                    status_code=206,
                    content=data[start:end + 1],
                    headers={
                        "Content-Length": str(end - start + 1),
                        "Content-Range": f"bytes {start}-{end}/{len(data)}",
                    },
                )

            with (mock.patch.object(
                    mineru_client, "_RANGE_FALLBACK_CHUNK_SIZE", 37),
                  self.assertRaises(
                      mineru_client.requests.exceptions.ConnectionError)):
                MineruClient._download_with_bounded_ranges(
                    "https://cdn.example/result.zip",
                    destination,
                    work,
                    interrupted_get,
                )

            self.assertEqual(b"stable-old-result", destination.read_bytes())
            self.assertEqual(data[:37], work.read_bytes())

            def resume_get(_url, *, headers, **_kwargs):
                start, requested_end = map(
                    int, headers["Range"][len("bytes="):].split("-"))
                end = min(requested_end, len(data) - 1)
                return _Response(
                    status_code=206,
                    content=data[start:end + 1],
                    headers={
                        "Content-Length": str(end - start + 1),
                        "Content-Range": f"bytes {start}-{end}/{len(data)}",
                    },
                )

            with mock.patch.object(
                    mineru_client, "_RANGE_FALLBACK_CHUNK_SIZE", 37):
                MineruClient._download_with_bounded_ranges(
                    "https://cdn.example/result.zip",
                    destination,
                    work,
                    resume_get,
                )

            self.assertEqual(data, destination.read_bytes())
            self.assertFalse(work.exists())
            self.assertEqual("bytes=0-36", calls[0])
            self.assertEqual("bytes=37-73", calls[1])

    def test_retryable_http_status_then_success(self):
        from src import mineru_client

        responses = [
            _Response(status_code=503),
            _Response(content=_zip_bytes("7. 服务恢复")),
        ]
        with (mock.patch.object(mineru_client.requests, "get",
                                side_effect=responses) as request_get,
              mock.patch.object(mineru_client.time, "sleep") as sleep):
            markdown, _ = MineruClient._download_and_extract(
                "https://cdn.example/result.zip", "paper", None)

        self.assertEqual("7. 服务恢复", markdown)
        self.assertEqual(2, request_get.call_count)
        sleep.assert_called_once_with(1)

    def test_non_transient_http_error_is_not_retried(self):
        from src import mineru_client

        with (mock.patch.object(mineru_client.requests, "get",
                                return_value=_Response(status_code=404)) as request_get,
              mock.patch.object(mineru_client.time, "sleep") as sleep,
              self.assertRaisesRegex(MinerUAPIError, "HTTP 404")):
            MineruClient._download_and_extract(
                "https://cdn.example/missing.zip", "paper", None)

        request_get.assert_called_once()
        sleep.assert_not_called()

    def test_repeated_connection_failure_preserves_partial(self):
        from src import mineru_client

        error = mineru_client.requests.exceptions.ConnectionError("断开")
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "result.zip.part"
            responses = [
                _Response(chunks=[b"partial", error]),
            ] + [error] * (mineru_client._DOWNLOAD_ATTEMPTS - 1)
            with (mock.patch.object(mineru_client.requests, "get",
                                    side_effect=responses) as request_get,
                  mock.patch.object(mineru_client.time, "sleep"),
                  self.assertRaisesRegex(
                      MinerUAPIError,
                      rf"网络连接中断.*尝试下载 "
                      rf"{mineru_client._DOWNLOAD_ATTEMPTS} 次.*"
                      "不会重新提交识别")):
                MineruClient._download_and_extract(
                    "https://cdn.example/result.zip", "paper", None, part)

            self.assertEqual(mineru_client._DOWNLOAD_ATTEMPTS,
                             request_get.call_count)
            self.assertEqual(b"partial", part.read_bytes())
            self.assertEqual("bytes=7-", request_get.call_args_list[1].kwargs[
                "headers"]["Range"])
            for call in request_get.call_args_list[2:]:
                self.assertEqual(
                    f"bytes=0-{mineru_client._RANGE_FALLBACK_CHUNK_SIZE - 1}",
                    call.kwargs["headers"]["Range"],
                )
            self.assertFalse(list(Path(tmp).glob(".*.range.tmp")))

    def test_failure_without_received_bytes_reports_server_state_only(self):
        from src import mineru_client

        error = mineru_client.requests.exceptions.SSLError("断开")
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "result.zip.part"
            with (mock.patch.object(
                      mineru_client.requests, "get",
                      side_effect=[error] * mineru_client._DOWNLOAD_ATTEMPTS),
                  mock.patch.object(mineru_client.time, "sleep"),
                  self.assertRaisesRegex(
                      MinerUAPIError, "尚未收到可保存的结果字节")):
                MineruClient._download_and_extract(
                    "https://cdn.example/result.zip", "paper", None, part)

            self.assertFalse(part.exists())


class MinerUResumeStateTests(unittest.TestCase):
    def _pdf(self, directory, content=b"%PDF-1.4\nresume-test"):
        path = Path(directory) / "paper.pdf"
        path.write_bytes(content)
        return path

    def test_retry_reuses_batch_and_state_contains_no_secret_or_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._pdf(tmp)
            resume_dir = Path(tmp) / "resume"
            client = MineruClient("token-secret", model_version="vlm")
            with (mock.patch.object(client, "_apply_upload_link",
                                    return_value=("batch-1", "https://upload/signed")),
                  mock.patch.object(client, "_upload_file"),
                  mock.patch.object(client, "_poll_batch",
                                    return_value="https://cdn/old-signed"),
                  mock.patch.object(client, "_download_and_extract",
                                    side_effect=MinerUAPIError("下载中断"))):
                with self.assertRaisesRegex(MinerUAPIError, "下载中断"):
                    client.parse_pdf(
                        pdf, resume_dir=resume_dir, resume_key="text-layer")

            state_path, part_path = client._resume_paths(
                resume_dir, "text-layer")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            serialized = state_path.read_text(encoding="utf-8")
            self.assertEqual("batch-1", state["batch_id"])
            self.assertEqual(pdf.stat().st_size, state["source_size"])
            self.assertEqual(pdf.stat().st_mtime_ns, state["source_mtime_ns"])
            self.assertEqual(64, len(state["source_sha256"]))
            self.assertEqual(64, len(state["token_fingerprint"]))
            self.assertNotIn("token-secret", serialized)
            self.assertNotIn("https://", serialized)
            self.assertFalse(any("url" in key.lower() for key in state))

            with (mock.patch.object(client, "_apply_upload_link") as apply_link,
                  mock.patch.object(client, "_upload_file") as upload,
                  mock.patch.object(client, "_poll_batch",
                                    return_value="https://cdn/new-signed") as poll,
                  mock.patch.object(client, "_download_and_extract",
                                    return_value=("完成", "full.md")) as download):
                result = client.parse_pdf(
                    pdf, resume_dir=resume_dir, resume_key="text-layer")

            self.assertEqual(("完成", "full.md"), result)
            apply_link.assert_not_called()
            upload.assert_not_called()
            poll.assert_called_once_with("batch-1", 600, 3)
            download.assert_called_once()
            self.assertEqual(
                ("https://cdn/new-signed", "paper", None),
                download.call_args.args,
            )
            self.assertEqual(part_path,
                             download.call_args.kwargs["partial_path"])
            # 成功下载只清 part；状态由外层发布最终结果时随 resume_dir 一起替换。
            self.assertTrue(state_path.exists())

    def test_same_content_with_changed_mtime_still_reuses_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._pdf(tmp)
            resume_dir = Path(tmp) / "resume"
            client = MineruClient("token-a")
            state_path, _ = client._resume_paths(resume_dir, "text-layer")
            client._write_resume_state(
                state_path,
                batch_id="batch-same-content",
                source_identity=client._source_identity(pdf),
                force_ocr=False,
            )
            stat = pdf.stat()
            os.utime(pdf, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

            with (mock.patch.object(client, "_apply_upload_link") as apply_link,
                  mock.patch.object(client, "_poll_batch",
                                    return_value="https://cdn/result"),
                  mock.patch.object(client, "_download_and_extract",
                                    return_value=("ok", "full.md"))):
                client.parse_pdf(
                    pdf, resume_dir=resume_dir, resume_key="text-layer")

            apply_link.assert_not_called()

    def test_different_token_does_not_query_or_overwrite_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._pdf(tmp)
            resume_dir = Path(tmp) / "resume"
            original = MineruClient("token-a")
            state_path, part_path = original._resume_paths(
                resume_dir, "text-layer")
            original._write_resume_state(
                state_path,
                batch_id="batch-a",
                source_identity=original._source_identity(pdf),
                force_ocr=False,
            )
            part_path.write_bytes(b"partial-a")
            before = state_path.read_bytes()

            other = MineruClient("token-b")
            with (mock.patch.object(other, "_apply_upload_link") as apply_link,
                  mock.patch.object(other, "_poll_batch") as poll):
                with self.assertRaises(MinerUAPIError) as caught:
                    other.parse_pdf(
                        pdf, resume_dir=resume_dir, resume_key="text-layer")

            self.assertEqual("resume_token_mismatch", caught.exception.code)
            self.assertRegex(str(caught.exception), "原 Token.*重复计费")
            apply_link.assert_not_called()
            poll.assert_not_called()
            self.assertEqual(before, state_path.read_bytes())
            self.assertEqual(b"partial-a", part_path.read_bytes())

    def test_new_source_may_replace_stale_state_from_another_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._pdf(tmp, b"%PDF-1.4\nold-source")
            resume_dir = Path(tmp) / "resume"
            original = MineruClient("token-a")
            state_path, part_path = original._resume_paths(
                resume_dir, "text-layer")
            original._write_resume_state(
                state_path,
                batch_id="batch-old",
                source_identity=original._source_identity(pdf),
                force_ocr=False,
            )
            part_path.write_bytes(b"old-part")
            pdf.write_bytes(b"%PDF-1.4\nnew-source-and-size")

            other = MineruClient("token-b")
            with (mock.patch.object(other, "_apply_upload_link",
                                    return_value=("batch-new", "upload-new")) as apply_link,
                  mock.patch.object(other, "_upload_file"),
                  mock.patch.object(other, "_poll_batch",
                                    return_value="https://cdn/new"),
                  mock.patch.object(other, "_download_and_extract",
                                    return_value=("ok", "full.md"))):
                other.parse_pdf(
                    pdf, resume_dir=resume_dir, resume_key="text-layer")

            apply_link.assert_called_once()
            new_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("batch-new", new_state["batch_id"])
            self.assertEqual(other._token_fingerprint(),
                             new_state["token_fingerprint"])
            self.assertFalse(part_path.exists())

    def test_server_batch_failed_clears_state_and_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._pdf(tmp)
            resume_dir = Path(tmp) / "resume"
            client = MineruClient("token-a")
            state_path, part_path = client._resume_paths(
                resume_dir, "text-layer")
            client._write_resume_state(
                state_path,
                batch_id="batch-failed",
                source_identity=client._source_identity(pdf),
                force_ocr=False,
            )
            part_path.write_bytes(b"partial")
            error = MinerUAPIError("服务端判定失败", code="parse_quota_limit")
            setattr(error, "_mineru_batch_failed", True)

            with (mock.patch.object(client, "_apply_upload_link") as apply_link,
                  mock.patch.object(client, "_poll_batch", side_effect=error)):
                with self.assertRaisesRegex(MinerUAPIError, "服务端判定失败"):
                    client.parse_pdf(
                        pdf, resume_dir=resume_dir, resume_key="text-layer")

            apply_link.assert_not_called()
            self.assertFalse(state_path.exists())
            self.assertFalse(part_path.exists())

    def test_poll_marks_only_explicit_server_failure(self):
        from src import mineru_client

        response = _Response(json_body={
            "code": 0,
            "data": {
                "extract_result": [{
                    "state": "failed",
                    "err_code": "parse_quota_limit",
                    "err_msg": "额度不足",
                }],
            },
        })
        client = MineruClient("token-a")
        with mock.patch.object(mineru_client.requests, "get",
                               return_value=response):
            with self.assertRaises(MinerUAPIError) as caught:
                client._poll_batch("batch-failed", timeout=1, interval=0)

        self.assertEqual("parse_quota_limit", caught.exception.code)
        self.assertTrue(getattr(caught.exception, "_mineru_batch_failed", False))


if __name__ == "__main__":
    unittest.main()
