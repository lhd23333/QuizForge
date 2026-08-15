"""MinerU 精准解析 API（v4）封装。

本地文件解析流程：
    申请上传链接 (POST /file-urls/batch)
 -> PUT 上传 PDF（不设 Content-Type）
 -> 轮询 batch (GET /extract-results/batch/{batch_id}) 直到 state=done
 -> 下载 full_zip_url 并解压，取出 markdown
"""

import hashlib
import json
import logging
import os
import re
import ssl
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import requests

from .exceptions import MinerUAPIError

logger = logging.getLogger(__name__)

_DOWNLOAD_ATTEMPTS = 6
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_RANGE_FALLBACK_CHUNK_SIZE = 2 * 1024 * 1024
_DOWNLOAD_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_DOWNLOAD_RETRY_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)
_CONTENT_RANGE_RE = re.compile(
    r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)
_RESUME_STATE_VERSION = 1


class _RetryableDownloadError(Exception):
    """同一个结果文件可安全重试的下载/ZIP 完整性错误。"""

    def __init__(self, message: str, *, use_bounded_ranges: bool = False):
        super().__init__(message)
        self.use_bounded_ranges = bool(use_bounded_ranges)


def _is_unexpected_ssl_eof(error: BaseException) -> bool:
    """只识别 TLS 被代理提前截断；证书错误等其它 SSL 问题不得绕过代理。"""
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if (isinstance(current, ssl.SSLEOFError)
                or "UNEXPECTED_EOF_WHILE_READING" in str(current).upper()):
            return True
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)
        pending.extend(
            arg for arg in getattr(current, "args", ())
            if isinstance(arg, BaseException)
        )
    return False


class MineruClient:
    """MinerU 精准解析 API 客户端。"""

    BASE_URL = "https://mineru.net/api/v4"

    def __init__(self, token: str, model_version: str = "vlm"):
        self.token = token
        self.model_version = model_version
        self._auth_headers = {"Authorization": f"Bearer {token}"}

    def parse_pdf(
        self,
        pdf_path: str | Path,
        extract_dir: str | Path | None = None,
        poll_timeout: int = 600,
        poll_interval: int = 3,
        force_ocr: bool = False,
        resume_dir: str | Path | None = None,
        resume_key: str | None = None,
    ) -> tuple[str, str]:
        """解析本地 PDF，返回 (markdown 文本, md 文件名)。

        若给定 extract_dir，会把 zip 解压到该目录（保留原始结果供调试）。
        若给定 resume_dir，会保存不含凭证明文/签名 URL 的 batch_id 和下载断点；
        后续用同一 Token 调用时会复用服务端任务，避免重复提交识别。
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise MinerUAPIError(f"PDF 文件不存在: {pdf_path}")

        state_path = None
        part_path = None
        source_identity = None
        batch_id = ""
        if resume_dir is not None:
            state_path, part_path = self._resume_paths(resume_dir, resume_key)
            source_identity = self._source_identity(pdf_path)
            state = self._read_resume_state(state_path)
            if state:
                if self._resume_state_matches(
                        state, source_identity, force_ocr=force_ocr):
                    # MinerU batch 可能按账号隔离。同一输入的未完成任务绝不允许
                    # 另一份 Token 覆盖或误查，以免静默重做并重复计费。
                    if (state.get("token_fingerprint")
                            != self._token_fingerprint()):
                        raise MinerUAPIError(
                            "发现该文件未完成的 MinerU 任务，但当前 Token 与原任务不一致；"
                            "请切回原 Token 后重试，以免重复计费",
                            code="resume_token_mismatch",
                        )
                    batch_id = str(state["batch_id"])
                    logger.info("发现未完成的 MinerU 服务端任务，继续查询结果")
                else:
                    # 同一 Token 下源文件或解析参数已经改变，旧断点不能拼接到新结果。
                    self._remove_resume_file(state_path)
                    self._remove_resume_file(part_path)

        if not batch_id:
            logger.info("申请上传链接: %s", pdf_path.name)
            batch_id, upload_url = self._apply_upload_link(
                pdf_path.name, force_ocr=force_ocr)

            logger.info("上传文件中...")
            self._upload_file(pdf_path, upload_url)
            if state_path is not None:
                self._write_resume_state(
                    state_path,
                    batch_id=batch_id,
                    source_identity=source_identity,
                    force_ocr=force_ocr,
                )

        logger.info("解析中，轮询进度...")
        try:
            zip_url = self._poll_batch(batch_id, poll_timeout, poll_interval)
        except MinerUAPIError as exc:
            # 只有服务端明确判定 batch failed 才作废恢复信息。轮询超时、网络中断
            # 和结果下载失败都保留，下一次可继续查询/续传。
            if getattr(exc, "_mineru_batch_failed", False):
                self._remove_resume_file(state_path)
                self._remove_resume_file(part_path)
            raise

        logger.info("下载结果并解压...")
        return self._download_and_extract(
            zip_url, pdf_path.stem, extract_dir, partial_path=part_path)

    # ---------- 内部步骤 ----------

    def _apply_upload_link(self, filename: str, *, force_ocr: bool = False) -> tuple[str, str]:
        url = f"{self.BASE_URL}/file-urls/batch"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        payload = {
            "files": [{"name": filename, "data_id": uuid.uuid4().hex,
                       "is_ocr": force_ocr}],
            "model_version": self.model_version,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        data = self._parse(resp)
        return data["batch_id"], data["file_urls"][0]

    @staticmethod
    def _upload_file(pdf_path: Path, upload_url: str) -> None:
        # MinerU 要求上传时不设置 Content-Type，故不传 headers
        with open(pdf_path, "rb") as f:
            resp = requests.put(upload_url, data=f, timeout=600)
        if resp.status_code not in (200, 201):
            raise MinerUAPIError(
                f"文件上传失败 HTTP {resp.status_code}: {resp.text[:200]}"
            )

    def _poll_batch(self, batch_id: str, timeout: int, interval: int) -> str:
        url = f"{self.BASE_URL}/extract-results/batch/{batch_id}"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        start = time.time()
        while time.time() - start < timeout:
            resp = requests.get(url, headers=headers, timeout=60)
            data = self._parse(resp)
            results = data.get("extract_result", [])
            if not results:
                time.sleep(interval)
                continue
            item = results[0]
            state = item.get("state")
            if state == "done":
                zip_url = item.get("full_zip_url")
                if not zip_url:
                    raise MinerUAPIError("解析完成但未返回 full_zip_url")
                return zip_url
            if state == "failed":
                code = item.get("err_code") or ""
                error = MinerUAPIError(
                    f"解析失败: {item.get('err_msg', '未知错误')}", code=code)
                # 保留原始 err_code 给上层凭证池判断额度/并发，仅用私有标记告诉
                # parse_pdf 该 batch 已不可恢复。
                setattr(error, "_mineru_batch_failed", True)
                raise error
            progress = item.get("extract_progress")
            if progress:
                logger.info(
                    "  已解析 %s/%s 页",
                    progress.get("extracted_pages", "?"),
                    progress.get("total_pages", "?"),
                )
            time.sleep(interval)
        raise MinerUAPIError(
            f"轮询超时（{timeout}s），可稍后用 batch_id 查询: {batch_id}"
        )

    @staticmethod
    def _download_and_extract(
        zip_url: str,
        stem: str,
        extract_dir: str | Path | None,
        partial_path: str | Path | None = None,
    ) -> tuple[str, str]:
        """流式下载并解压结果；给定 partial_path 时跨调用保留下载断点。"""
        temporary_dir = None
        persistent = partial_path is not None
        if partial_path is None:
            temporary_dir = tempfile.TemporaryDirectory(prefix="mineru-result-")
            partial_path = Path(temporary_dir.name) / "result.zip.part"
        else:
            partial_path = Path(partial_path)
            partial_path.parent.mkdir(parents=True, exist_ok=True)

        last_error: BaseException | None = None
        last_reason = ""
        direct_session = None
        use_direct = False
        use_bounded_ranges = False
        network_failures = 0
        range_work_path = partial_path.with_name(
            f".{partial_path.name}.{uuid.uuid4().hex}.range.tmp")
        try:
            for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
                response = None
                switched_to_direct = False
                try:
                    request_get = (direct_session.get
                                   if use_direct else requests.get)
                    if use_bounded_ranges:
                        MineruClient._download_with_bounded_ranges(
                            zip_url,
                            partial_path,
                            range_work_path,
                            request_get,
                        )
                        result = MineruClient._read_and_extract_zip(
                            partial_path, stem, extract_dir)
                        MineruClient._remove_resume_file(partial_path)
                        return result

                    offset = (partial_path.stat().st_size
                              if partial_path.is_file() else 0)
                    headers = {"Accept-Encoding": "identity"}
                    if offset:
                        headers["Range"] = f"bytes={offset}-"
                    response = request_get(
                        zip_url,
                        headers=headers,
                        stream=True,
                        timeout=(30, 300),
                    )
                    status = int(response.status_code)

                    if status == 416 and offset:
                        # 上次可能恰好收完全部字节但在校验前中断。先验证本地文件；
                        # 若仍损坏，清空后从零下载，避免永远卡在 Range 末尾。
                        try:
                            result = MineruClient._read_and_extract_zip(
                                partial_path, stem, extract_dir)
                        except MinerUAPIError:
                            raise
                        except (zipfile.BadZipFile, EOFError, OSError,
                                RuntimeError) as exc:
                            raise _RetryableDownloadError(
                                "本地断点 ZIP 不完整",
                                use_bounded_ranges=True,
                            ) from exc
                        MineruClient._remove_resume_file(partial_path)
                        return result

                    if status not in (200, 206):
                        error = MinerUAPIError(
                            f"下载 zip 失败 HTTP {status}",
                            code=f"http_{status}",
                        )
                        if status not in _DOWNLOAD_RETRY_STATUS:
                            raise error
                        raise _RetryableDownloadError(
                            f"CDN 返回 HTTP {status}") from error

                    mode = "wb"
                    range_length = None
                    range_total = None
                    if status == 206:
                        content_range = str(
                            getattr(response, "headers", {}).get(
                                "Content-Range", ""))
                        match = _CONTENT_RANGE_RE.match(content_range.strip())
                        if (not match or int(match.group(1)) != offset
                                or int(match.group(2)) < int(match.group(1))):
                            # 起点不一致时不能把两段不同位置的数据拼在一起。
                            MineruClient._remove_resume_file(partial_path)
                            raise _RetryableDownloadError(
                                "CDN 返回的断点位置不一致")
                        range_length = (
                            int(match.group(2)) - int(match.group(1)) + 1)
                        if match.group(3) != "*":
                            range_total = int(match.group(3))
                        mode = "ab" if offset else "wb"
                    # CDN 忽略 Range 并返回 200 时，必须覆盖旧前缀。

                    content_length = str(
                        getattr(response, "headers", {}).get(
                            "Content-Length", "")).strip()
                    expected_length = (
                        int(content_length) if content_length.isdigit() else None)
                    if (range_length is not None
                            and expected_length is not None
                            and expected_length != range_length):
                        MineruClient._remove_resume_file(partial_path)
                        raise _RetryableDownloadError(
                            "CDN 返回的分段长度声明不一致")
                    received = 0
                    with open(partial_path, mode) as output:
                        for chunk in response.iter_content(
                                chunk_size=_DOWNLOAD_CHUNK_SIZE):
                            if not chunk:
                                continue
                            output.write(chunk)
                            received += len(chunk)
                    if (expected_length is not None
                            and received != expected_length):
                        raise requests.exceptions.ChunkedEncodingError(
                            f"结果分段长度不完整: {received}/{expected_length}")
                    if range_length is not None and received != range_length:
                        raise requests.exceptions.ChunkedEncodingError(
                            f"结果分段长度不完整: {received}/{range_length}")
                    if (range_total is not None
                            and partial_path.stat().st_size != range_total):
                        # bytes=<offset>- 请求应返回到对象末尾。若 CDN 只给了更短
                        # 的一段，保留现有前缀，下次继续从新的长度请求。
                        raise requests.exceptions.ChunkedEncodingError(
                            "结果分段尚未到达文件末尾")

                    try:
                        result = MineruClient._read_and_extract_zip(
                            partial_path, stem, extract_dir)
                    except MinerUAPIError:
                        raise
                    except (zipfile.BadZipFile, EOFError, OSError,
                            RuntimeError) as exc:
                        # 先保留现有字节。若只是 CDN 提前断流，下一次可从当前长度
                        # 续传；若其实已收满，随后 416 分支会校验并安全重置。
                        raise _RetryableDownloadError(
                            "下载到的 ZIP 不完整或校验失败",
                            use_bounded_ranges=True,
                        ) from exc

                    MineruClient._remove_resume_file(partial_path)
                    return result
                except _DOWNLOAD_RETRY_ERRORS as exc:
                    last_error = exc
                    network_failures += 1
                    # 偶发断流先沿用既有开放尾段续传；连续两次网络中断才改用
                    # 小块 Range，兼顾普通路径开销与异常链路的可恢复性。
                    if network_failures >= 2:
                        use_bounded_ranges = True
                    # 只记异常类，不记可能含签名 URL 的 message。
                    last_reason = f"网络连接中断（{type(exc).__name__}）"
                    # 本机系统代理可能能访问 MinerU API，却会在 CDN CONNECT/TLS
                    # 握手阶段提前 EOF。仅对这个明确错误让结果下载改用不读取环境
                    # 代理的独立 Session；提交、上传和 batch 查询仍走原网络配置。
                    if not use_direct and _is_unexpected_ssl_eof(exc):
                        direct_session = requests.Session()
                        direct_session.trust_env = False
                        use_direct = True
                        switched_to_direct = True
                        last_reason = "系统代理中断 TLS，已切换直连"
                except _RetryableDownloadError as exc:
                    last_error = exc
                    last_reason = str(exc)
                    if exc.use_bounded_ranges:
                        use_bounded_ranges = True
                finally:
                    if response is not None:
                        close = getattr(response, "close", None)
                        if callable(close):
                            close()

                if attempt < _DOWNLOAD_ATTEMPTS:
                    saved = (partial_path.stat().st_size
                             if partial_path.is_file() else 0)
                    if switched_to_direct:
                        logger.warning(
                            "MinerU 结果下载经系统代理失败（已保存 %s 字节），"
                            "立即改用直连重试（%s/%s）",
                            saved, attempt + 1, _DOWNLOAD_ATTEMPTS,
                        )
                        continue
                    delay = 2 ** (attempt - 1)
                    logger.warning(
                        "MinerU 结果下载失败（%s，已保存 %s 字节），"
                        "%s 秒后重试（%s/%s）",
                        last_reason, saved, delay,
                        attempt + 1, _DOWNLOAD_ATTEMPTS,
                    )
                    time.sleep(delay)

            if persistent:
                saved = (partial_path.stat().st_size
                         if partial_path.is_file() else 0)
                if saved:
                    hint = (
                        f"已保留服务端任务和 {saved / 1024 / 1024:.1f} MiB "
                        "下载进度；请稍后重试，将继续下载且不会重新提交识别"
                    )
                else:
                    hint = (
                        "已保留服务端任务；当前连接尚未收到可保存的结果字节；"
                        "请稍后重试，将重新查询结果地址且不会重新提交识别"
                    )
            else:
                hint = "请稍后重试"
            raise MinerUAPIError(
                f"下载 MinerU 结果失败：{last_reason or '未知下载错误'}，"
                f"已尝试下载 {_DOWNLOAD_ATTEMPTS} 次；{hint}"
            ) from last_error
        finally:
            if direct_session is not None:
                direct_session.close()
            MineruClient._remove_resume_file(range_work_path)
            if temporary_dir is not None:
                temporary_dir.cleanup()

    @staticmethod
    def _download_with_bounded_ranges(
        zip_url: str,
        destination: Path,
        work_path: Path,
        request_get,
    ) -> None:
        """用显式有界 Range 重建 ZIP，校验完成后再原子替换下载断点。

        普通 GET 在部分 CDN／代理组合下可能字节数正确但中间内容损坏。这里每段
        都核对 Content-Range 与实际长度；单段失败不会写入工作文件，后续重试可
        从最后一个已验证边界继续。只有整包 CRC 通过后才替换 destination。
        """
        work_path.parent.mkdir(parents=True, exist_ok=True)

        # 上一轮可能在收完最后一段后、原子替换前中断。先验证即可避免多发一次
        # 已越过对象末尾的 Range 请求。
        if work_path.is_file() and work_path.stat().st_size:
            try:
                MineruClient._validate_zip(work_path)
            except (zipfile.BadZipFile, EOFError, OSError, RuntimeError):
                pass
            else:
                try:
                    os.replace(work_path, destination)
                except OSError as exc:
                    raise MinerUAPIError("保存 MinerU 分段下载结果失败") from exc
                return

        offset = work_path.stat().st_size if work_path.is_file() else 0
        total_size = None
        while total_size is None or offset < total_size:
            requested_end = offset + _RANGE_FALLBACK_CHUNK_SIZE - 1
            response = None
            try:
                response = request_get(
                    zip_url,
                    headers={
                        "Accept-Encoding": "identity",
                        "Range": f"bytes={offset}-{requested_end}",
                    },
                    stream=True,
                    timeout=(30, 300),
                )
                status = int(response.status_code)
                if status != 206:
                    error = MinerUAPIError(
                        f"下载 zip 分段失败 HTTP {status}",
                        code=f"http_{status}",
                    )
                    if status in _DOWNLOAD_RETRY_STATUS or status in (200, 416):
                        raise _RetryableDownloadError(
                            "CDN 未返回有效的 Range 分段",
                            use_bounded_ranges=True,
                        ) from error
                    raise error

                content_range = str(
                    getattr(response, "headers", {}).get(
                        "Content-Range", "")).strip()
                match = _CONTENT_RANGE_RE.match(content_range)
                if not match or match.group(3) == "*":
                    raise _RetryableDownloadError(
                        "CDN 返回的分段范围声明无效",
                        use_bounded_ranges=True,
                    )
                range_start = int(match.group(1))
                range_end = int(match.group(2))
                response_total = int(match.group(3))
                expected_end = min(requested_end, response_total - 1)
                if (response_total <= 0
                        or range_start != offset
                        or range_end != expected_end
                        or range_end < range_start):
                    raise _RetryableDownloadError(
                        "CDN 返回的分段位置不一致",
                        use_bounded_ranges=True,
                    )
                if total_size is not None and response_total != total_size:
                    MineruClient._remove_resume_file(work_path)
                    raise _RetryableDownloadError(
                        "CDN 结果文件大小在分段下载期间发生变化",
                        use_bounded_ranges=True,
                    )
                total_size = response_total
                range_length = range_end - range_start + 1
                content_length = str(
                    getattr(response, "headers", {}).get(
                        "Content-Length", "")).strip()
                if (content_length.isdigit()
                        and int(content_length) != range_length):
                    raise _RetryableDownloadError(
                        "CDN 返回的分段长度声明不一致",
                        use_bounded_ranges=True,
                    )

                payload = bytearray()
                for chunk in response.iter_content(
                        chunk_size=_DOWNLOAD_CHUNK_SIZE):
                    if not chunk:
                        continue
                    payload.extend(chunk)
                    if len(payload) > range_length:
                        raise _RetryableDownloadError(
                            "CDN 返回的分段数据超出声明长度",
                            use_bounded_ranges=True,
                        )
                if len(payload) != range_length:
                    raise requests.exceptions.ChunkedEncodingError(
                        f"结果分段长度不完整: {len(payload)}/{range_length}")

                with open(work_path, "ab") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                offset = range_end + 1
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()

        try:
            MineruClient._validate_zip(work_path)
        except (zipfile.BadZipFile, EOFError, OSError, RuntimeError) as exc:
            # 每段长度正确仍不代表内容正确；CRC 失败时整包重建，不能把损坏的
            # 已完成文件当成下一轮可靠前缀。
            MineruClient._remove_resume_file(work_path)
            raise _RetryableDownloadError(
                "分段下载后的 ZIP 仍未通过 CRC 校验",
                use_bounded_ranges=True,
            ) from exc
        try:
            os.replace(work_path, destination)
        except OSError as exc:
            raise MinerUAPIError("保存 MinerU 分段下载结果失败") from exc

    @staticmethod
    def _validate_zip(path: Path) -> None:
        """校验 ZIP 目录与每个成员的 CRC，不解压也不改动目标目录。"""
        with zipfile.ZipFile(path) as archive:
            archive.infolist()
            bad_member = archive.testzip()
            if bad_member:
                raise zipfile.BadZipFile(f"ZIP CRC 校验失败: {bad_member}")

    @staticmethod
    def _read_and_extract_zip(
        partial_path: Path,
        stem: str,
        extract_dir: str | Path | None,
    ) -> tuple[str, str]:
        """完整读取 ZIP（含 CRC）后再解压，任何包损坏都会回到下载重试。"""
        with zipfile.ZipFile(partial_path) as archive:
            archive.infolist()
            bad_member = archive.testzip()
            if bad_member:
                raise zipfile.BadZipFile(f"ZIP CRC 校验失败: {bad_member}")
            names = [
                name for name in archive.namelist()
                if name.lower().endswith(".md")
            ]
            if not names:
                raise MinerUAPIError("zip 中未找到 markdown 文件")

            # 优先取与源文件同名 / full.md / 体量最大的 md。
            target = (
                next((name for name in names if Path(name).stem == stem), None)
                or next((name for name in names
                         if Path(name).stem.lower() == "full"), None)
                or max(names, key=lambda name: archive.getinfo(name).file_size)
            )
            md_text = archive.read(target).decode("utf-8", errors="replace")
            if extract_dir:
                target_dir = Path(extract_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
                archive.extractall(target_dir)
        return md_text, Path(target).name

    @staticmethod
    def _resume_paths(
        resume_dir: str | Path, resume_key: str | None
    ) -> tuple[Path, Path]:
        directory = Path(resume_dir)
        directory.mkdir(parents=True, exist_ok=True)
        safe_key = re.sub(r"[^0-9A-Za-z_-]+", "_", resume_key or "default")
        safe_key = safe_key.strip("_") or "default"
        return (
            directory / f".mineru_task_{safe_key}.json",
            directory / f".mineru_result_{safe_key}.zip.part",
        )

    @staticmethod
    def _source_identity(pdf_path: Path) -> dict:
        stat = pdf_path.stat()
        digest = hashlib.sha256()
        with open(pdf_path, "rb") as source:
            while chunk := source.read(_DOWNLOAD_CHUNK_SIZE):
                digest.update(chunk)
        return {
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_sha256": digest.hexdigest(),
        }

    def _token_fingerprint(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()

    @staticmethod
    def _read_resume_state(state_path: Path) -> dict | None:
        if not state_path.is_file():
            return None
        try:
            with open(state_path, "r", encoding="utf-8") as source:
                state = json.load(source)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            logger.warning("MinerU 恢复状态损坏，将清除后重新提交")
            MineruClient._remove_resume_file(state_path)
            return None
        except OSError as exc:
            raise MinerUAPIError("读取 MinerU 恢复状态失败") from exc
        if not isinstance(state, dict) or not state.get("batch_id"):
            logger.warning("MinerU 恢复状态缺少 batch_id，将清除后重新提交")
            MineruClient._remove_resume_file(state_path)
            return None
        return state

    def _resume_state_matches(
        self, state: dict, source_identity: dict, *, force_ocr: bool
    ) -> bool:
        return all((
            state.get("version") == _RESUME_STATE_VERSION,
            state.get("source_size") == source_identity["source_size"],
            # mtime 只用于诊断；DOCX/图片重建出的同内容 PDF 时间戳会变化，
            # 真正判定同一输入使用 size + SHA-256。
            state.get("source_sha256") == source_identity["source_sha256"],
            state.get("model_version") == self.model_version,
            state.get("force_ocr") is bool(force_ocr),
        ))

    def _write_resume_state(
        self,
        state_path: Path,
        *,
        batch_id: str,
        source_identity: dict,
        force_ocr: bool,
    ) -> None:
        state = {
            "version": _RESUME_STATE_VERSION,
            "batch_id": str(batch_id),
            **source_identity,
            "model_version": self.model_version,
            "force_ocr": bool(force_ocr),
            "token_fingerprint": self._token_fingerprint(),
        }
        temporary = state_path.with_name(
            f".{state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "x", encoding="utf-8") as output:
                json.dump(state, output, ensure_ascii=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, state_path)
        except OSError as exc:
            raise MinerUAPIError("保存 MinerU 恢复状态失败") from exc
        finally:
            self._remove_resume_file(temporary)

    @staticmethod
    def _remove_resume_file(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("清理 MinerU 临时恢复文件失败: %s", type(exc).__name__)

    @staticmethod
    def _parse(resp: requests.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            raise MinerUAPIError(
                f"MinerU 返回非 JSON (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        if resp.status_code != 200 or body.get("code") != 0:
            raise MinerUAPIError(
                f"MinerU 接口异常 code={body.get('code')} "
                f"msg={body.get('msg')} (HTTP {resp.status_code})",
                code=body.get("code") or f"http_{resp.status_code}",
            )
        return body["data"]
