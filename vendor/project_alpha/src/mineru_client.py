"""MinerU 精准解析 API（v4）封装。

本地文件解析流程：
    申请上传链接 (POST /file-urls/batch)
 -> PUT 上传 PDF（不设 Content-Type）
 -> 轮询 batch (GET /extract-results/batch/{batch_id}) 直到 state=done
 -> 下载 full_zip_url 并解压，取出 markdown
"""

import io
import logging
import time
import uuid
import zipfile
from pathlib import Path

import requests

from .exceptions import MinerUAPIError

logger = logging.getLogger(__name__)


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
    ) -> tuple[str, str]:
        """解析本地 PDF，返回 (markdown 文本, md 文件名)。

        若给定 extract_dir，会把 zip 解压到该目录（保留原始结果供调试）。
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise MinerUAPIError(f"PDF 文件不存在: {pdf_path}")

        logger.info("申请上传链接: %s", pdf_path.name)
        batch_id, upload_url = self._apply_upload_link(pdf_path.name)

        logger.info("上传文件中...")
        self._upload_file(pdf_path, upload_url)

        logger.info("解析中，轮询进度...")
        zip_url = self._poll_batch(batch_id, poll_timeout, poll_interval)

        logger.info("下载结果并解压...")
        return self._download_and_extract(zip_url, pdf_path.stem, extract_dir)

    # ---------- 内部步骤 ----------

    def _apply_upload_link(self, filename: str) -> tuple[str, str]:
        url = f"{self.BASE_URL}/file-urls/batch"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        payload = {
            "files": [{"name": filename, "data_id": uuid.uuid4().hex}],
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
                raise MinerUAPIError(f"解析失败: {item.get('err_msg', '未知错误')}")
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
        zip_url: str, stem: str, extract_dir: str | Path | None
    ) -> tuple[str, str]:
        resp = requests.get(zip_url, timeout=300)
        if resp.status_code != 200:
            raise MinerUAPIError(f"下载 zip 失败 HTTP {resp.status_code}")

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = [n for n in zf.namelist() if n.lower().endswith(".md")]
        if extract_dir:
            extract_dir = Path(extract_dir)
            extract_dir.mkdir(parents=True, exist_ok=True)
            zf.extractall(extract_dir)
        if not names:
            raise MinerUAPIError("zip 中未找到 markdown 文件")

        # 优先取与源文件同名 / full.md / 体量最大的 md
        target = (
            next((n for n in names if Path(n).stem == stem), None)
            or next((n for n in names if Path(n).stem.lower() == "full"), None)
            or max(names, key=lambda n: zf.getinfo(n).file_size)
        )
        md_text = zf.read(target).decode("utf-8", errors="replace")
        return md_text, Path(target).name

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
                f"msg={body.get('msg')} (HTTP {resp.status_code})"
            )
        return body["data"]
