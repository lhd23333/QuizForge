#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 MinerU（doclib HTTP API）客户端 —— 接口对齐 MineruClient.parse_pdf。

本地 MinerU 4.x 走 doclib 协议（默认 ``http://127.0.0.1:15980/api/v1``），与
mineru.net v4 的「申请上传 → 轮询 batch → 下载 zip」完全不同；本模块把本地协议
包装成 converter 依赖的同形状接口：

    parse_pdf(pdf_path, extract_dir=..., poll_timeout=..., force_ocr=...,
              resume_dir=..., resume_key=...) -> (markdown, md_name)

流程：
    1. POST /parses               提交解析（服务端按路径读文件；命中缓存时
       status=done，不重复占用 GPU）
    2. GET  /parses?ids=...       轮询直到 done / failed
    3. GET  /docs/{sha}/content   分页拉取 Markdown（truncated + next_request）
    4. GET  /content?locator=...  把 ``doc:.../page:N/block:M`` 图片定位符导出为
       真实 PNG 落到 ``extract_dir/images/``，正文引用替换为相对路径——
       形状与 mineru.net 解压树的 ``images/xxx.jpg`` 引用一致，下游拆题、
       图片登记逻辑不需要知道后端差异。

与 mineru.net 客户端的有意差异：
- **无凭证**：本地服务无鉴权，``token`` 参数仅为接口兼容而保留。
- **resume_dir/resume_key 接受但忽略**：doclib 按文档哈希缓存解析结果，重跑
  同一文件自然幂等，无需 batch 断点文件。
- **force_ocr 接受但忽略**（2026-10-03 起）：本地管线没有 mineru.net 的
  「文本层 / 强制 OCR」双模式可供切换，converter 的强制 OCR 重试意图在这里
  天然满足；照 force 重跑只会白烧一遍 GPU（同 PDF 同管线的结果一致）。
- **串行化**：本地解码吃 GPU（8GB 显存下 10 页密集公式卷峰值约 7 GB），
  模块级锁保证同一进程内一次只跑一个本地解析；跨进程（桌面版 + 导入脚本）
  请遵守「先关桌面版、脚本用 --jobs 1」的约束。
- 不产出 ``*_content_list.json``：依赖坐标的增强修复（imgorder 多图选择题
  归属）在本地模式下自动降级为原文，不报错。

自测：
    python mineru_local.py <pdf>            # 解析并打印摘要（图片落临时目录）
    python mineru_local.py <pdf> -o 目录 --force
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:15980/api/v1"
DEFAULT_TIER = "standard"
# 单次 content 拉取的字符上限；分页由 next_request 驱动。
_CONTENT_LIMIT = 30000
# 兜底页数上限：正常 286 组文档最多几十次分页，超过说明服务端行为异常。
_MAX_CONTENT_PAGES = 300

# 本地 MinerU 主目录（doclib 解析产物在 <home>/doclib/parsed/…）。
_MINERU_HOME = Path(os.environ.get("MINERU_HOME") or (Path.home() / ".mineru"))
# docvortex.middle 的块类型 → mineru content_list 的块类型（imgorder 只消费
# text/title/list 作锚点；页脚、页码不参与，避免干扰 A-D 标签坐标）。
_CONTENT_LIST_TEXT_TYPES = {
    "text": "text",
    "paragraph_title": "title",
    "title": "title",
    "list": "list",
}

# doclib 输出的页界注释（<!-- page 3 of 14 -->）。QuizForge 用自己
# 的 <!-- quizforge:source-page-break --> 做白名单页界，本地页注释在拆题前
# 剥掉，避免混进题目正文。
_PAGE_MARKER_RE = re.compile(r"(?m)^<!-- page \d+ of \d+ -->[ \t]*\n?")
# 图片定位符：doc:<short_id>/tier:<tier>/page:<n>[/<kind>:<n>]
_LOCATOR_RE = re.compile(
    r"doc:[0-9a-fA-F]{6,}/tier:[^/)\s]+/page:\d+(?:/[A-Za-z_]+:\d+)?")
_LOCATOR_PARTS_RE = re.compile(
    r"doc:[0-9a-fA-F]+/tier:[^/]+/page:(?P<page>\d+)"
    r"(?:/[A-Za-z_]+:(?P<block>\d+))?")
_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg",
             "image/webp": ".webp", "image/gif": ".gif"}

# 页标记（doclib 渲染的 <!-- page N of M -->）——结构化预处理在剥除之前使用
_PAGE_BLOCK_RE = re.compile(r"(<!-- page \d+ of \d+ -->)")
_PAGE_MARKER_NUM_RE = re.compile(r"<!-- page (\d+) of \d+ -->")
_WS_STRIP_RE = re.compile(r"\s+")
# 「伪标题」：MinerU 会把解答题的题号行（`18.（17 分）`）也标成 paragraph_title。
# 这类文本不能强化为 ##、也不能进分区清单，否则切题器把它当分区标题吞掉题号行。
# `第N题` 同理：AGMC 2026.2/2026.8 junior 等卷题号即写作「第N题」，被强化成
# `## 第N题` 后题号行整批被当分区标题吞掉（2026-10-03 全库对账实测：25 题只剩 2）。
_FAKE_TITLE_RE = re.compile(r"^(?:\d{1,3}\s*[.．、)）]|第\s*\d+\s*题)")


def _is_real_title(text: str) -> bool:
    return bool(text) and not _FAKE_TITLE_RE.match(text.strip())


def _norm_line(text: str) -> str:
    """去空白后的行/块文本，用于 md 行与 json 块文本的容错匹配（OCR 空格差异）。"""
    return _WS_STRIP_RE.sub("", text or "")


def _content_text(item: dict) -> str:
    """取出 content[] 项的文本：content 可能是字符串，也可能是嵌套子块列表
    （实测 image_caption 形如 [{"type": "text", "content": "图 1: …"}]）。"""
    value = item.get("content")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        pieces: list[str] = []
        for sub in value:
            if isinstance(sub, dict):
                text = sub.get("content")
                if isinstance(text, str) and text.strip():
                    pieces.append(text.strip())
            elif isinstance(sub, str) and sub.strip():
                pieces.append(sub.strip())
        return " ".join(pieces).strip()
    return ""


def _block_plain_text(block: dict) -> str:
    """拼出中间 json 块的纯文本（text/equation_inline 等子块按序拼接）。"""
    pieces: list[str] = []
    for item in block.get("content") or []:
        if isinstance(item, dict):
            text = _content_text(item)
            if text:
                pieces.append(text)
    return " ".join(pieces).strip()


def _process_blocks_page(text: str, blocks: list) -> str:
    """对 md 的单页文本按该页 json 块做三项增强（找不到就逐项跳过）。"""
    junk: set[str] = set()
    title_keys: set[str] = set()
    continue_heads: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind in ("header", "footer", "page_number"):
            key = _norm_line(_block_plain_text(block))
            if key:
                junk.add(key)
        elif kind in ("paragraph_title", "doc_title"):
            title_text = _block_plain_text(block)
            if _is_real_title(title_text):
                key = _norm_line(title_text)
                if key:
                    title_keys.add(key)
        elif block.get("continues_prev"):
            head = _block_plain_text(block)
            if len(head) >= 8:
                continue_heads.append(head[:16])
    lines = text.splitlines()
    if junk:
        lines = [ln for ln in lines if _norm_line(ln) not in junk]
    if title_keys:
        for i, ln in enumerate(lines):
            if _norm_line(ln) in title_keys and not ln.lstrip().startswith("#"):
                lines[i] = "## " + ln.strip()
    text = "\n".join(lines)
    for head in continue_heads:
        idx = text.find(head)
        if idx > 0 and text[:idx].endswith("\n\n"):
            text = text[: idx - 1] + text[idx:]
    return text

# Unicode 数学字母数字区段（𝑏、𝑖、𝑀、𝛼…）。PP-FormulaNet 在部分字体/排版下会
# 把公式输出成这些字符而不是 LaTeX，且落在 $ 之外——渲染时与周边 LaTeX 不一致
# （用户反馈的「字母没有用美元符号包裹」）。统一 NFKC 回 ASCII/希腊并按需补 $。
_UNI_MATH_RUN_RE = re.compile(r"[\U0001D400-\U0001D7FF]+")
# 公式段切分：$$...$$ 优先于 $...$
_FORMULA_SPAN_RE = re.compile(r"(\$\$[^$]*\$\$|\$[^$]*\$)")


# 个别卷（实测 AGMC 2025.2 problems-junior）把整题输出成 `**加粗**`，题号前的
# `**` 让切题器认不出题号（全卷 25 题只切出 3 道未加粗的）。检测到 `**N.` 特征
# 时剥离公式段之外的 ** 记号——加粗纯属排版，剥离不改内容。
_BOLD_NUM_RE = re.compile(r"(?m)^\*\*\s*\d{1,3}\s*[.．、]")


def _strip_bold_noise(md: str) -> str:
    """剥离「整题加粗」卷的 ** 记号（仅当出现 **N. 题号特征时触发）。"""
    if not _BOLD_NUM_RE.search(md):
        return md
    parts = _FORMULA_SPAN_RE.split(md)
    for index in range(0, len(parts), 2):  # 偶数索引为公式段之外
        parts[index] = parts[index].replace("**", "")
    return "".join(parts)


def _normalize_unicode_math(md: str) -> str:
    """归一化裸 Unicode 数学字符，使其与 LaTeX 一致。

    - $ 内：Unicode 数学字母做 NFKC（𝑏→b，KaTeX 不认这些码点）；U+2212 减号
      换成 ASCII '-'（KaTeX 对 U+2212 支持不稳）。
    - $ 外：连续的 Unicode 数学字母段整体 NFKC 后包成 $...$，保证字母由美元
      符号包裹。
    只做字符级归一，不改变任何内容语义；失败不影响主链路。
    """
    try:
        parts = _FORMULA_SPAN_RE.split(md)
        out: list[str] = []
        for index, part in enumerate(parts):
            if index % 2 == 1:  # 公式段
                part = _UNI_MATH_RUN_RE.sub(
                    lambda m: unicodedata.normalize("NFKC", m.group(0)), part)
                part = part.replace("−", "-")
            else:
                part = _UNI_MATH_RUN_RE.sub(
                    lambda m: "$" + unicodedata.normalize("NFKC", m.group(0)) + "$",
                    part)
            out.append(part)
        return "".join(out)
    except Exception:  # noqa: BLE001 —— 增强项只降级，不失败
        return md

# 本地解析吃 GPU 显存；同进程内串行，跨进程靠脚本侧 --jobs 1 与桌面版关闭约束。
_PARSE_LOCK = threading.Lock()


class MineruLocalError(RuntimeError):
    """本地 MinerU 服务或解析错误。消息面向用户，可直接进任务错误栏。"""


def probe_server(base_url: str | None = None, timeout: float = 1.5) -> dict:
    """探测本地 MinerU 服务可用性——不抛异常，供 UI 动态选项与状态提示。

    与 ``_ensure_server`` 同源（GET /server/status），但失败路径全部收敛成
    返回值：UI 探测不该因为服务没启动就抛错。返回::

        {"available": bool, "running": bool, "version": str, "error": str}

    服务未启动时一般毫秒级返回（连接被拒），最坏在 ``timeout`` 秒内返回。
    """
    url = (base_url
           or os.environ.get("QUIZFORGE_MINERU_LOCAL_URL")
           or DEFAULT_BASE_URL).rstrip("/")
    try:
        with MineruLocalClient._session() as session:
            resp = session.get(f"{url}/server/status", timeout=(1.0, timeout))
        body = resp.json() if resp.status_code == 200 else {}
    except (ValueError, OSError) as exc:
        return {"available": False, "running": False, "version": "",
                "error": f"本地 MinerU 服务未响应（{type(exc).__name__}）"}
    if not bool(body.get("running")):
        return {"available": False, "running": False, "version": "",
                "error": "本地 MinerU 服务未在运行"}
    return {"available": True, "running": True,
            "version": str(body.get("version") or ""), "error": ""}


class MineruLocalClient:
    """把本地 doclib 协议包装成 MineruClient 同形状客户端（见模块文档）。"""

    def __init__(self, token: str = "", model_version: str = "",
                 tier: str | None = None, base_url: str | None = None):
        # token / model_version 仅接口兼容：本地服务无鉴权，档位由 tier 决定。
        self.token = token
        self.model_version = model_version
        self.tier = (tier
                     or os.environ.get("QUIZFORGE_MINERU_LOCAL_TIER")
                     or DEFAULT_TIER).strip().lower()
        self.base_url = (base_url
                         or os.environ.get("QUIZFORGE_MINERU_LOCAL_URL")
                         or DEFAULT_BASE_URL).rstrip("/")

    # ---------- 对外主入口 ----------

    def parse_pdf(
        self,
        pdf_path,
        extract_dir=None,
        poll_timeout: int = 1800,
        poll_interval: float = 2.0,
        force_ocr: bool = False,
        resume_dir=None,
        resume_key=None,
    ) -> tuple[str, str]:
        """解析本地文件，返回 (markdown 文本, md 文件名)。

        若给定 extract_dir，图片导出到其 images/ 子目录，同时把成品 Markdown
        写为 ``<stem>.md`` 留在该目录（形状与 mineru.net 解压树一致）。
        resume_dir / resume_key 接受但忽略（doclib 自带缓存）。
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise MineruLocalError(f"PDF 文件不存在: {pdf_path}")

        with _PARSE_LOCK:
            self._ensure_server()
            # force_ocr 接受但**不**触发重解析：本地链路只有 OCR 一条路（没有
            # mineru.net 的「文本层」模式可供绕过），converter 的「强制 OCR
            # 重试」意图在这里天然满足；若照 force 重跑，每次都白烧一遍 GPU
            #（实测 19 页卷 ~127s，全库约 30 卷触发）。doclib 缓存命中即复用
            #（同 PDF 同管线的解析结果一致）。需真正强制重跑时用 `mineru`
            # 命令行或直接 POST /parses 手工 force。
            sha = self._submit(pdf_path, force=False,
                               poll_timeout=poll_timeout,
                               poll_interval=poll_interval)
            md = self._fetch_markdown(sha,
                                      wait_timeout=min(300.0, float(poll_timeout)),
                                      poll_interval=poll_interval)
            md = self._structural_preprocess(md, sha)
            md = _PAGE_MARKER_RE.sub("", md)
            md = _strip_bold_noise(md)
            md = _normalize_unicode_math(md)
            md, image_map = self._materialize_images(md, extract_dir)
            self._write_content_list(sha, extract_dir, image_map,
                                     pdf_path.stem)
            name = f"{pdf_path.stem}.md"
            if extract_dir is not None:
                try:
                    target = Path(extract_dir)
                    target.mkdir(parents=True, exist_ok=True)
                    (target / name).write_text(md, encoding="utf-8")
                except OSError as exc:
                    # 落盘失败只影响调试留档，不影响转换本身。
                    print(f"[warn] 本地 MinerU 原文落盘失败（不影响转换）: {exc}",
                          file=sys.stderr)
            return md, name

    # ---------- 内部步骤 ----------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _session() -> requests.Session:
        session = requests.Session()
        # 本地回环绝不能走系统代理：Clash 等对 127.0.0.1 的劫持会失败得莫名其妙。
        session.trust_env = False
        return session

    def _request(self, method: str, path: str, *, params=None, json=None):
        try:
            with self._session() as session:
                resp = session.request(method, self._url(path), params=params,
                                       json=json, timeout=(5, 120))
        except requests.RequestException as exc:
            raise MineruLocalError(
                f"本地 MinerU 请求失败（{method} {path}）：{exc}") from exc
        if resp.status_code != 200:
            detail = resp.text[:200]
            try:
                body = resp.json()
                if isinstance(body, dict) and isinstance(body.get("error"), dict):
                    error = body["error"]
                    detail = f"{error.get('code', '')} {error.get('message', '')}".strip()
            except ValueError:
                pass
            raise MineruLocalError(
                f"本地 MinerU 接口异常 HTTP {resp.status_code}（{method} {path}）：{detail}")
        try:
            return resp.json()
        except ValueError as exc:
            raise MineruLocalError(
                f"本地 MinerU 返回非 JSON（{method} {path}）") from exc

    def _ensure_server(self) -> None:
        """服务不可用时给出可执行的启动命令，而不是裸 ConnectionError。"""
        try:
            with self._session() as session:
                resp = session.get(self._url("/server/status"), timeout=(3, 10))
            body = resp.json() if resp.status_code == 200 else {}
        except (requests.RequestException, ValueError) as exc:
            raise MineruLocalError(
                "本地 MinerU 服务未响应（%s）。请先启动：\n"
                '  & "D:\\Miniconda\\envs\\mineru\\Scripts\\mineru.exe" server start'
                % self.base_url) from exc
        if not body.get("running"):
            raise MineruLocalError(
                f"本地 MinerU 服务未在运行（{self.base_url}）")

    def _submit(self, pdf_path: Path, *, force: bool,
                poll_timeout: float, poll_interval: float) -> str:
        # 必须显式 page_range="all"：省略时 doclib 按“默认前 10 页”解析（ingest
        # 的 default_parse_range），超过 10 页的文档会被静默截断。
        data = self._request("POST", "/parses", json={
            "path": str(pdf_path), "tier": self.tier,
            "page_range": "all", "force": force, "remote": False,
        })
        sha = str(data.get("sha256") or "")
        if not sha:
            raise MineruLocalError(f"本地 MinerU 未返回文档哈希：{data}")
        if data.get("status") == "done":
            return sha

        ids: list[int] = []
        for key in ("wait_parse_ids", "created_parse_ids", "reused_parse_ids"):
            for value in data.get(key) or []:
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed not in ids:
                    ids.append(parsed)
        if not ids:
            raise MineruLocalError(
                f"本地 MinerU 未返回待解析任务（status={data.get('status')}）：{data}")

        deadline = time.time() + max(30.0, float(poll_timeout))
        id_set = set(ids)
        while True:
            # 注意：doclib 的 GET /parses 会忽略 ids 参数（返回全表窗口）——必须用
            # doc_ref 过滤，并在本地按 id 核对；只有明确看到每个待等 id 都完成才返回，
            # 缺任何一个都继续等待。否则窗口「恰好全是 done」会被误判为完成，
            # 拉内容时得到 404 not_cached（实测踩坑）。
            info = self._request("GET", "/parses", params={
                "doc_ref": sha, "include_superseded": "true", "limit": 200,
            })
            mine = [p for p in (info.get("parses") or [])
                    if p.get("id") in id_set]
            failed = [p for p in mine if p.get("status") == "failed"]
            if failed:
                p = failed[0]
                raise MineruLocalError(
                    "本地 MinerU 解析失败：%s %s"
                    % (p.get("error_code") or "", p.get("error_msg") or ""))
            statuses = {int(p["id"]): str(p.get("status") or "")
                        for p in mine if p.get("id") is not None}
            if all(statuses.get(i) in ("done", "superseded") for i in ids):
                return sha
            if time.time() > deadline:
                raise MineruLocalError(
                    f"等待本地 MinerU 解析超时（{int(poll_timeout)}s，任务 {ids}）。"
                    "服务可能仍在处理；稍后重跑同一命令即可，已完成页段不会重复解析")
            time.sleep(max(0.5, float(poll_interval)))

    def _fetch_markdown(self, sha: str, *, wait_timeout: float = 0.0,
                        poll_interval: float = 2.0) -> str:
        """分页拉全 Markdown。next_request 按页段推进，不做字符级游标。

        ``wait_timeout>0`` 时，首请求遇到 ``not_cached``（解析刚完成、页缓存
        尚未可见的边缘时序）会在该时限内重试等待而不是立即失败。
        """
        chunks: list[str] = []
        params: dict = {"tier": self.tier, "limit": _CONTENT_LIMIT}
        deadline = time.time() + max(0.0, float(wait_timeout))
        seen_cursors: set[tuple] = set()
        for _ in range(_MAX_CONTENT_PAGES):
            while True:
                try:
                    data = self._request(
                        "GET", f"/docs/{sha}/content", params=params)
                    break
                except MineruLocalError as exc:
                    if "not_cached" in str(exc) and time.time() < deadline:
                        time.sleep(max(0.5, float(poll_interval)))
                        continue
                    raise
            chunks.append(str(data.get("content") or ""))
            # 完成条件以 next_request 为准：实测服务端在「还有后续页段」时也会
            # 返回 truncated=False（team 卷 page 1-10 后带 page_range=11-14），
            # 只看 truncated 会丢掉整个尾段（曾致 14 页卷只导入前 10 页）。
            # 无 next_request、或 next_request 与上一轮重复（不推进）即视为取完。
            nxt = data.get("next_request") or {}
            cursor = tuple(str(nxt.get(k) or "") for k in
                           ("page_range", "after", "locator"))
            if not any(cursor) or cursor in seen_cursors:
                return "".join(chunks)
            seen_cursors.add(cursor)
            params = {"tier": self.tier, "limit": _CONTENT_LIMIT}
            for key in ("page_range", "after", "locator"):
                value = nxt.get(key)
                if value:
                    params[key] = value
        raise MineruLocalError(
            f"本地 MinerU 内容分页异常：超过 {_MAX_CONTENT_PAGES} 次仍未取完")

    def _materialize_images(self, md: str, extract_dir):
        """把 doc: 定位符导出成 images/ 下的真实文件，并替换正文引用。

        返回 ``(md, image_map)``；image_map 键为 ``(page, block)``（均 1 起，
        无 block 的页级定位符 block 为 None），值为文件名，供 content_list
        生成时与 doclib 中间产物的块编号配对。
        """
        locators: list[str] = []
        for locator in _LOCATOR_RE.findall(md):
            if locator not in locators:
                locators.append(locator)
        image_map: dict[tuple[int, int | None], str] = {}
        if not locators:
            return md, image_map
        if extract_dir is None:
            raise MineruLocalError(
                f"正文含 {len(locators)} 个本地图片定位符，但未提供 extract_dir")
        images_dir = Path(extract_dir) / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        by_locator: dict[str, str] = {}
        for locator in locators:
            parts = _LOCATOR_PARTS_RE.match(locator)
            page = parts.group("page") if parts else "0"
            block = parts.group("block") if parts else None
            stem = f"p{page}_b{block}" if block else f"p{page}"
            existing = sorted(images_dir.glob(f"{stem}.*"))
            if existing:
                target = existing[0]
            else:
                data = self._request("GET", "/content", params={
                    "locator": locator, "format": "image", "image_format": "png",
                })
                asset = data.get("asset") or {}
                source = asset.get("path")
                if not source or not Path(str(source)).is_file():
                    raise MineruLocalError(
                        f"本地 MinerU 图片导出失败：{locator}（asset.path={source}）")
                ext = _MIME_EXT.get(str(asset.get("mime_type") or ""), ".png")
                target = images_dir / f"{stem}{ext}"
                try:
                    shutil.copy2(str(source), target)
                except OSError as exc:
                    raise MineruLocalError(
                        f"本地 MinerU 图片落地失败：{locator} -> {target}（{exc}）") from exc
            by_locator[locator] = target.name
            try:
                key = (int(page), int(block) if block is not None else None)
            except (TypeError, ValueError):
                key = None
            if key is not None:
                image_map[key] = target.name

        # 必须整词替换：str.replace 的子串语义会把 `block:1` 前缀命中
        # `block:13`，产出 `images/p3_b1.png3` 这类坏引用（图已导出但引用
        # 断裂，下游报「图片缺失」且该图丢失），凡同页 block 号互为前缀都会中招。
        def _replace(match: "re.Match") -> str:
            name = by_locator.get(match.group(0))
            return f"images/{name}" if name else match.group(0)

        md = _LOCATOR_RE.sub(_replace, md)
        return md, image_map

    def _load_middle_json(self, sha: str):
        """加载本文档在本档位下的最新中间产物 json（docvortex.middle）。

        可能为 None（从未解析 / 缺失 / 损坏）——所有调用方一律降级处理。
        """
        root = _MINERU_HOME / "doclib" / "parsed" / sha[:2] / sha / self.tier
        candidates = sorted(
            (p for p in root.glob("*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime_ns, reverse=True)
        if not candidates:
            return None
        try:
            return json.loads(candidates[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _structural_preprocess(self, md: str, sha: str) -> str:
        """用中间 json 的结构信号增强 md（设计见 MinerU_json导入优化分析.md）。

        三件事，全部按「json 块文本 → md 行」容错匹配，找不到就跳过：
          ① header/footer/page_number 块整行剔除（页脚不再混入题目）；
          ② 分区标题清单注入：paragraph_title/doc_title 的文本写入 md 顶部
             `<!-- qf:section-titles: ... -->` 注释——切题器据此精确分区，
             覆盖「证明轮」「题组1」这类不可枚举的标题；
          ③ 页内延续段合并：continues_prev 块与前一段粘连（空行→单换行）。
        任何一步失败都返回原 md（纯文本降级路径行为不变）。
        """
        try:
            data = self._load_middle_json(sha)
            if not isinstance(data, dict):
                return md
            pages = data.get("pages")
            if not isinstance(pages, list) or not pages:
                return md

            parts = _PAGE_BLOCK_RE.split(md)
            out: list[str] = []
            page_no: int | None = None
            for part in parts:
                marker = _PAGE_BLOCK_RE.fullmatch(part.strip())
                if marker:
                    num = _PAGE_MARKER_NUM_RE.search(part)
                    page_no = int(num.group(1)) if num else None
                    out.append(part)
                    continue
                blocks = None
                if page_no is not None and 0 <= page_no - 1 < len(pages):
                    page = pages[page_no - 1]
                    if isinstance(page, dict):
                        blocks = page.get("blocks")
                if page_no is None or not isinstance(blocks, list) \
                        or not part.strip():
                    out.append(part)
                    continue
                out.append(_process_blocks_page(part, blocks))
            result = "".join(out)

            titles: list[str] = []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                for block in page.get("blocks") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in ("paragraph_title", "doc_title"):
                        text = _block_plain_text(block)
                        if _is_real_title(text) and text not in titles:
                            titles.append(text)
            if titles:
                result = ("<!-- qf:section-titles: "
                          + " || ".join(titles[:200]) + " -->\n\n" + result)

            # 非题目文档预判（成绩/结果类）：几乎没有任何题号样式的文本块 +
            # 表格/图表块占比高。converter 读到该标记会给出明确错误而不是
            # 让整份文档走完切题后报含糊的"切不出题"（实测 AGMC results-*）。
            total_blocks = sum(
                len(p.get("blocks") or []) for p in pages if isinstance(p, dict))
            table_like = 0
            has_number_lines = False
            for page in pages:
                if not isinstance(page, dict):
                    continue
                for block in page.get("blocks") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in ("table", "chart", "index"):
                        table_like += 1
                    elif not has_number_lines and re.search(
                            r"(?m)^\s*\d{1,3}\s*[.．、]", _block_plain_text(block)):
                        has_number_lines = True
            if (total_blocks >= 5 and not has_number_lines
                    and table_like * 3 >= total_blocks):
                result = "<!-- qf:no-questions -->\n\n" + result
            return result
        except Exception as exc:  # noqa: BLE001 —— 增强项失败只降级
            print(f"[warn] 结构化预处理失败（不影响转换）: {exc}",
                  file=sys.stderr)
            return md

    def _write_content_list(self, sha: str, extract_dir, image_map,
                            stem: str) -> None:
        """把 doclib 中间产物转换为 imgorder 兼容的 ``*_content_list.json``。

        doclib 的解析产物 ``<MINERU_HOME>/doclib/parsed/<sha[:2]>/<sha>/<tier>/
        <page_range>_<done_ms>.json``（docvortex.middle v2.0）每块带 0-1 归一化
        bbox；imgorder 与白名单页界注入读的是 mineru.net 风格的
        ``*_content_list.json``。这里做一层转换，让本地后端也能用上「多图选择题
        坐标归属」修复。块编号对应关系（已实测验证）：markdown 定位符
        ``page:P/block:N`` ↔ 中间产物 ``page_idx=P-1`` 的第 ``index=N-1`` 块。

        任何一步失败都只告警并降级（下游按"无坐标"处理），绝不拖垮主链路。
        """
        try:
            if extract_dir is None or not image_map:
                return
            middle = self._load_middle_json(sha)
            if not isinstance(middle, dict):
                return
            pages = middle.get("pages")
            if not isinstance(pages, list):
                return
            rows: list[dict] = []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                try:
                    page_no = int(page.get("page_idx", -1)) + 1
                except (TypeError, ValueError):
                    continue
                if page_no < 1:
                    continue
                for block in page.get("blocks") or []:
                    if not isinstance(block, dict):
                        continue
                    bbox = block.get("bbox")
                    if not isinstance(bbox, list) or len(bbox) != 4:
                        continue
                    kind = str(block.get("type") or "")
                    if kind in ("image", "chart"):
                        try:
                            block_no = int(block.get("index")) + 1
                        except (TypeError, ValueError):
                            continue
                        name = image_map.get((page_no, block_no))
                        if name:
                            row = {
                                "type": "image",
                                "img_path": f"images/{name}",
                                "page_idx": page_no - 1,
                                "bbox": bbox,
                            }
                            for item in block.get("content") or []:
                                if (isinstance(item, dict)
                                        and item.get("type") in (
                                            "image_caption", "image_footnote",
                                            "chart_caption")):
                                    text = _content_text(item)
                                    if text:
                                        row["img_caption"] = text
                                        break
                            rows.append(row)
                    elif kind in _CONTENT_LIST_TEXT_TYPES:
                        text = "\n".join(
                            str(item.get("content") or "")
                            for item in (block.get("content") or [])
                            if isinstance(item, dict))
                        if text.strip():
                            rows.append({
                                "type": _CONTENT_LIST_TEXT_TYPES[kind],
                                "text": text,
                                "page_idx": page_no - 1,
                                "bbox": bbox,
                            })
            if not rows:
                return
            target = Path(extract_dir) / f"{stem}_content_list.json"
            target.write_text(json.dumps(rows, ensure_ascii=False),
                              encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 —— 增强项只降级，不失败
            print(f"[warn] 本地 MinerU content_list 生成失败（不影响转换）: {exc}",
                  file=sys.stderr)


# ---------- 命令行自测 ----------

def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本地 MinerU 单文件解析（自测/调试）")
    parser.add_argument("pdf", help="待解析的 PDF 路径")
    parser.add_argument("-o", "--out", default="",
                        help="输出目录（默认系统临时目录 mineru_local_cli）")
    parser.add_argument("--force", action="store_true",
                        help="强制重新解析（忽略 doclib 缓存）")
    parser.add_argument("--tier", default="",
                        help="解析档位（默认 standard，也可 flash/basic/advanced）")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "mineru_local_cli"
    out.mkdir(parents=True, exist_ok=True)
    client = MineruLocalClient(tier=args.tier or None)
    started = time.time()
    md, name = client.parse_pdf(args.pdf, extract_dir=out, force_ocr=args.force)
    elapsed = time.time() - started
    images_dir = out / "images"
    images = sorted(images_dir.glob("*")) if images_dir.is_dir() else []
    print(f"[OK] {name}  {len(md)} chars  {len(images)} images  "
          f"{elapsed:.1f}s  -> {out}")
    print("-" * 60)
    print(md[:600])
    return 0


if __name__ == "__main__":
    sys.exit(_main())
