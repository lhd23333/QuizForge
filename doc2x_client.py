"""Doc2X v2 PDF API 客户端。

流程：申请预上传链接 -> PUT PDF -> 轮询 v3-2026 解析 -> 触发 Markdown 导出
-> 轮询导出 -> 安全解包。解析 JSON 与页级质量分一并落盘，供后续回归定位。
"""

from __future__ import annotations

import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://v2.doc2x.noedgeai.com"
MODEL = "v3-2026"
MAX_ZIP_BYTES = 500 * 1024 * 1024
MAX_ZIP_FILES = 5000

_ERROR_HINTS = {
    "parse_task_limit_exceeded": "当前处理任务数已达上限，请等前面的任务完成后重试",
    "parse_concurrency_limit": "当前处理页数已达上限，请等前面的任务完成后重试",
    "parse_quota_limit": "Doc2X 可用解析页数额度不足",
    "parse_file_too_large": "文件超过 Doc2X 大小上限，请拆分后重试",
    "parse_page_limit_exceeded": "文件页数超过 Doc2X 上限，请拆分后重试",
    "parse_file_lock": "该文件因重复解析失败被临时锁定，请重新打印 PDF 后重试",
    "parse_file_not_pdf": "Doc2X 判定上传内容不是 PDF",
    "parse_file_invalid": "PDF 格式不规范，Doc2X 无法解析；请重新打印 PDF 后重试",
    "parse_timeout": "Doc2X 解析超过 15 分钟，请拆分 PDF 后重试",
}


class Doc2XError(Exception):
    """Doc2X 请求、解析或导出失败。"""

    def __init__(self, message: str, *, code=""):
        super().__init__(message)
        self.code = str(code or "")


@dataclass(frozen=True)
class Doc2XResult:
    markdown: str
    markdown_name: str
    uid: str
    model: str
    page_scores: tuple[int | None, ...]


class Doc2XClient:
    def __init__(self, api_key: str, *, session=None):
        api_key = (api_key or "").strip()
        if not api_key:
            raise Doc2XError("尚未配置 Doc2X API Key，请在「设置」页填入")
        self._session = session or requests.Session()
        # 本机环境代理可能把 Authorization 和预签名 URL 送给未知中间层；OCR
        # 接口按官方建议直连，显式忽略 HTTP(S)_PROXY。
        if session is None:
            self._session.trust_env = False
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def parse_pdf(self, pdf_path, *, extract_dir, poll_timeout=900,
                  poll_interval=3) -> Doc2XResult:
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise Doc2XError(f"PDF 文件不存在: {pdf_path}")
        if pdf_path.suffix.lower() != ".pdf":
            raise Doc2XError("Doc2X PDF 链路只接受 PDF；图片和 Word 需先在本地转为 PDF")

        extract_dir = Path(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Doc2X 申请上传链接: %s", pdf_path.name)
        pre = self._json("POST", "/api/v2/parse/preupload",
                         json_body={"model": MODEL}, timeout=60)
        uid = str(pre.get("uid") or "").strip()
        upload_url = str(pre.get("url") or "").strip()
        if not uid or not upload_url:
            raise Doc2XError("Doc2X 预上传响应缺少 uid 或上传地址")

        logger.info("Doc2X 上传文件: %s", pdf_path.name)
        with pdf_path.open("rb") as stream:
            response = self._session.put(upload_url, data=stream, timeout=600)
        if response.status_code not in (200, 201, 204):
            raise Doc2XError(f"Doc2X 文件上传失败（HTTP {response.status_code}）")

        parse_result = self._poll_parse(uid, poll_timeout, poll_interval)
        meta_path = extract_dir / f"{pdf_path.stem}_doc2x.json"
        meta_path.write_text(
            json.dumps(parse_result, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info("Doc2X 导出 Markdown: %s", pdf_path.name)
        started = self._json(
            "POST", "/api/v2/convert/parse",
            json_body={
                "uid": uid,
                "to": "md",
                "formula_mode": "dollar",
                "filename": pdf_path.stem,
                "merge_cross_page_forms": True,
                "formula_level": 0,
            }, timeout=60)
        download_url = str(started.get("url") or "").strip()
        if started.get("status") != "success" or not download_url:
            download_url = self._poll_export(uid, poll_timeout, poll_interval)

        markdown, md_name = self._download_export(
            download_url, extract_dir, pdf_path.stem)
        markdown, moved = _repair_figure_question_owners(
            markdown, parse_result, extract_dir / "images")
        markdown, repaired = _repair_figure_choice_order(
            markdown, parse_result, extract_dir / "images")
        if repaired:
            parse_result["quizforge_repaired_figure_choices"] = repaired
        if moved:
            parse_result["quizforge_reassigned_figures"] = moved
        if repaired or moved:
            meta_path.write_text(
                json.dumps(parse_result, ensure_ascii=False, indent=2),
                encoding="utf-8")
        raw_path = extract_dir / f"{pdf_path.stem}_raw.md"
        raw_path.write_text(markdown, encoding="utf-8")
        pages = parse_result.get("pages") or []
        scores = tuple(p.get("score") if isinstance(p, dict) else None for p in pages)
        return Doc2XResult(markdown, md_name, uid, MODEL, scores)

    def _poll_parse(self, uid: str, timeout: int, interval: int) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self._json("GET", "/api/v2/parse/status",
                              params={"uid": uid}, timeout=60)
            status = data.get("status")
            if status == "success":
                result = data.get("result")
                if not isinstance(result, dict):
                    raise Doc2XError("Doc2X 解析完成但未返回结果")
                return result
            if status == "failed":
                detail = str(data.get("detail") or "未知原因")
                code = next((value for value in _ERROR_HINTS if value in detail), "")
                raise Doc2XError(f"Doc2X 解析失败：{detail}", code=code)
            logger.info("Doc2X 解析进度: %s%%", data.get("progress", "?"))
            time.sleep(interval)
        raise Doc2XError(f"Doc2X 解析超时（{timeout} 秒）")

    def _poll_export(self, uid: str, timeout: int, interval: int) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self._json("GET", "/api/v2/convert/parse/result",
                              params={"uid": uid}, timeout=60)
            status = data.get("status")
            url = str(data.get("url") or "").strip()
            if status == "success" and url:
                return url.replace("\\u0026", "&")
            if status == "failed":
                raise Doc2XError(
                    f"Doc2X Markdown 导出失败：{data.get('detail') or '未知原因'}")
            time.sleep(interval)
        raise Doc2XError(f"Doc2X Markdown 导出超时（{timeout} 秒）")

    def _json(self, method: str, path: str, *, json_body=None, params=None,
              timeout=60) -> dict:
        kwargs = {"headers": self._headers, "timeout": timeout}
        if json_body is not None:
            kwargs["json"] = json_body
        if params is not None:
            kwargs["params"] = params
        try:
            response = self._session.request(method, BASE_URL + path, **kwargs)
        except requests.RequestException as exc:
            raise Doc2XError(f"Doc2X 网络请求失败：{type(exc).__name__}") from exc
        if response.status_code == 429:
            raise Doc2XError(
                "Doc2X 并发已满，请等前面的任务完成后重试", code="http_429")
        try:
            body = response.json()
        except ValueError as exc:
            raise Doc2XError(
                f"Doc2X 返回了非 JSON 响应（HTTP {response.status_code}）") from exc
        if response.status_code != 200 or body.get("code") != "success":
            code = str(body.get("code") or f"HTTP {response.status_code}")
            hint = _ERROR_HINTS.get(code) or str(body.get("msg") or "接口异常")
            raise Doc2XError(f"Doc2X 接口错误 {code}：{hint}", code=code)
        data = body.get("data")
        if not isinstance(data, dict):
            raise Doc2XError("Doc2X 响应缺少 data")
        return data

    def _download_export(self, url: str, extract_dir: Path,
                         stem: str) -> tuple[str, str]:
        zip_path = extract_dir / f".{stem}_doc2x.zip"
        try:
            response = self._session.get(url.replace("\\u0026", "&"),
                                         timeout=300, stream=True)
            if response.status_code != 200:
                raise Doc2XError(
                    f"Doc2X 导出包下载失败（HTTP {response.status_code}）")
            size = 0
            with zip_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_ZIP_BYTES:
                        raise Doc2XError("Doc2X 导出包超过 500MB 安全上限")
                    output.write(chunk)
            return _safe_extract_markdown(zip_path, extract_dir, stem)
        except requests.RequestException as exc:
            raise Doc2XError(f"Doc2X 导出包下载失败：{type(exc).__name__}") from exc
        finally:
            try:
                zip_path.unlink()
            except OSError:
                pass


_IMAGE_REF_RE = re.compile(
    r"(?P<head>!\[[^\]]*\]\(\s*<?)(?P<path>(?:\./)?images/[^)>\s]+)(?P<tail>>?\s*\))",
    re.IGNORECASE)


def _safe_name(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise Doc2XError("Doc2X 导出包包含不安全路径")
    return path


def _safe_extract_markdown(zip_path: Path, extract_dir: Path,
                           stem: str) -> tuple[str, str]:
    """只提取 Markdown 与 images/，不使用 extractall，杜绝 ZIP 路径穿越。"""
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise Doc2XError("Doc2X 导出结果不是有效 ZIP") from exc
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > MAX_ZIP_FILES:
            raise Doc2XError("Doc2X 导出包文件数异常")
        if sum(info.file_size for info in infos) > MAX_ZIP_BYTES:
            raise Doc2XError("Doc2X 导出包解压后超过 500MB 安全上限")
        paths = {info.filename: _safe_name(info.filename) for info in infos}
        md_infos = [info for info in infos if paths[info.filename].suffix.lower() == ".md"]
        if not md_infos:
            raise Doc2XError("Doc2X 导出包中没有 Markdown 文件")
        target = (
            next((i for i in md_infos if paths[i.filename].stem == stem), None)
            or next((i for i in md_infos if paths[i.filename].stem.lower() == "full"), None)
            or max(md_infos, key=lambda item: item.file_size)
        )
        markdown = archive.read(target).decode("utf-8", errors="replace")

        image_dir = extract_dir / "images"
        mapping: dict[str, str] = {}
        used: set[str] = set()
        for info in infos:
            path = paths[info.filename]
            lower = [part.lower() for part in path.parts]
            if "images" not in lower:
                continue
            index = lower.index("images")
            if index == len(path.parts) - 1:
                continue
            image_dir.mkdir(parents=True, exist_ok=True)
            base = Path(path.name).name
            candidate = base
            count = 2
            while candidate.lower() in used:
                candidate = f"{Path(base).stem}_{count}{Path(base).suffix}"
                count += 1
            used.add(candidate.lower())
            (image_dir / candidate).write_bytes(archive.read(info))
            rel = "/".join(path.parts[index:])
            mapping[rel.lower()] = candidate

        def _rewrite(match: re.Match) -> str:
            old = match.group("path").lstrip("./")
            new = mapping.get(old.lower())
            if not new:
                return match.group(0)
            return f'{match.group("head")}images/{new}{match.group("tail")}'

        markdown = _IMAGE_REF_RE.sub(_rewrite, markdown)
        return markdown, Path(target.filename).name


_FIGURE_CAPTION_RE = re.compile(r"^[（(]?\s*([A-DＡ-Ｄ])\s*[.．、)）]?$")


def _figure_filename(page_idx: int, figure: dict, image_dir: Path) -> str | None:
    """由 v3 Figure 的裁切参数反查导出包里的本地图片名。"""
    src = str(figure.get("src") or "")
    query = parse_qs(urlsplit(src).query)
    keys = ("x", "y", "w", "h", "r")
    values = [query.get(key, [None])[0] for key in keys]
    if any(value is None for value in values):
        bbox = figure.get("bbox") or []
        if len(bbox) != 4:
            return None
        values = [bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1], 0]
    prefix = f"{page_idx}_{'_'.join(str(value) for value in values)}"
    exact_matches = []
    if image_dir.is_dir():
        exact_matches = sorted(set(image_dir.glob(prefix + ".*")))
        if len(exact_matches) == 1:
            return exact_matches[0].name
        # 题干/解析缓存合并后会加一层命名空间。布局 JSON 仍保存原裁图参数，
        # 重试时必须能认回已经改名的同一张图，且只剥这一层固定前缀。
        namespaced = sorted(set(
            list(image_dir.glob("exam_" + prefix + ".*"))
            + list(image_dir.glob("solution_" + prefix + ".*"))))
        if not exact_matches and len(namespaced) == 1:
            return namespaced[0].name
    return None


def _image_name_variants(filename: str) -> tuple[str, ...]:
    """返回布局原名及合集缓存可能添加的固定命名空间。"""
    if filename.startswith(("exam_", "solution_")):
        return (filename, filename.split("_", 1)[1])
    return (filename, "exam_" + filename, "solution_" + filename)


def _figure_choice_clusters(parse_result: dict, image_dir: Path) -> list[dict[str, str]]:
    clusters: list[dict[str, str]] = []
    for page in parse_result.get("pages") or []:
        if not isinstance(page, dict):
            continue
        blocks = ((page.get("layout") or {}).get("blocks") or [])
        children: dict[str, list[dict]] = {}
        for block in blocks:
            if isinstance(block, dict) and block.get("parent_id"):
                children.setdefault(str(block["parent_id"]), []).append(block)
        pairs = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "FigureGroup":
                continue
            group_children = children.get(str(block.get("id") or ""), [])
            caption = next((child for child in group_children
                            if child.get("type") == "Caption"
                            and _FIGURE_CAPTION_RE.match(
                                str(child.get("text") or "").strip())), None)
            figure = next((child for child in group_children
                           if child.get("type") == "Figure"), None)
            if caption is None or figure is None:
                continue
            match = _FIGURE_CAPTION_RE.match(str(caption.get("text") or "").strip())
            letter = match.group(1).translate(str.maketrans("ＡＢＣＤ", "ABCD"))
            filename = _figure_filename(
                int(page.get("page_idx") or 0), figure, image_dir)
            bbox = caption.get("bbox") or block.get("bbox") or [0, 0, 0, 0]
            if filename:
                pairs.append((float(bbox[1]), float(bbox[0]), letter, filename))

        # 同一组选项的标题基线接近；遇到重复字母或纵向相距过远即开新组。
        current: dict[str, str] = {}
        first_y = None
        for y, _x, letter, filename in sorted(pairs):
            if current and (letter in current or (first_y is not None and y - first_y > 300)):
                if set(current) == set("ABCD"):
                    clusters.append(current)
                current = {}
                first_y = None
            if first_y is None:
                first_y = y
            current[letter] = filename
        if set(current) == set("ABCD"):
            clusters.append(current)

        # Doc2X 有时只把 A 识成 FigureGroup、B 识成普通 Text，C/D 标签完全漏掉，
        # 但四幅选项图仍处于同一规则横排。仅在 A、B 两个前缀锚点都与前两图贴邻、
        # 四图近似同尺寸且候选四元组唯一时补齐，页面上的其它题干图不会被凑进来。
        figures = []
        anchors = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bbox = block.get("bbox") or []
            if len(bbox) != 4:
                continue
            if block.get("type") == "Figure":
                filename = _figure_filename(
                    int(page.get("page_idx") or 0), block, image_dir)
                if filename:
                    figures.append((filename, tuple(float(v) for v in bbox)))
            elif block.get("type") in {"Text", "Caption"}:
                match = _FIGURE_CAPTION_RE.fullmatch(
                    str(block.get("text") or "").strip())
                if match:
                    anchors.append((
                        match.group(1).translate(str.maketrans("ＡＢＣＤ", "ABCD")),
                        tuple(float(v) for v in bbox),
                    ))

        inferred = []
        if len(figures) >= 4:
            import itertools
            import statistics

            for chosen in itertools.combinations(figures, 4):
                ordered = sorted(chosen, key=lambda item: item[1][0])
                widths = [box[2] - box[0] for _name, box in ordered]
                heights = [box[3] - box[1] for _name, box in ordered]
                centers_y = [(box[1] + box[3]) / 2 for _name, box in ordered]
                if (min(widths) <= 0 or min(heights) <= 0
                        or max(widths) / min(widths) > 1.35
                        or max(heights) / min(heights) > 1.35
                        or max(centers_y) - min(centers_y)
                        > statistics.median(heights) * 0.35):
                    continue
                aligned = set()
                for letter, anchor in anchors:
                    idx = ord(letter) - ord("A")
                    image = ordered[idx][1]
                    ax = (anchor[0] + anchor[2]) / 2
                    ay = (anchor[1] + anchor[3]) / 2
                    if (image[1] <= ay <= image[3]
                            and 0 <= image[0] - ax <= 100):
                        aligned.add(letter)
                if not {"A", "B"}.issubset(aligned):
                    continue
                inferred.append({letter: ordered[index][0]
                                 for index, letter in enumerate("ABCD")})
        if len(inferred) == 1:
            existing = {frozenset(cluster.values()) for cluster in clusters}
            if frozenset(inferred[0].values()) not in existing:
                clusters.append(inferred[0])
    return clusters


_QUESTION_HEAD_RE = re.compile(r"^\s*(\d{1,3})\s*[.．、]\s*")


def _figure_owner_map(parse_result: dict, image_dir: Path) -> dict[str, int]:
    owners: dict[str, int] = {}
    for page in parse_result.get("pages") or []:
        if not isinstance(page, dict):
            continue
        blocks = ((page.get("layout") or {}).get("blocks") or [])
        questions = []
        figures = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bbox = block.get("bbox") or []
            if len(bbox) != 4:
                continue
            if block.get("type") in {"Text", "Title"}:
                match = _QUESTION_HEAD_RE.match(str(block.get("text") or ""))
                if match:
                    questions.append((float(bbox[1]), int(match.group(1))))
            elif block.get("type") == "Figure":
                filename = _figure_filename(
                    int(page.get("page_idx") or 0), block, image_dir)
                if filename:
                    # 用图片顶边而不是中心归属：大图可能向下跨过下一题标题，但只要
                    # 它在下一题标题之前开始，就仍属于上一题；真正放在下一题右侧的
                    # 图，其顶边也会位于下一题标题之后。
                    figures.append((filename, float(bbox[1])))
        questions.sort()
        for filename, top_y in figures:
            candidates = [number for top, number in questions if top <= top_y]
            if candidates:
                owners[filename] = candidates[-1]
    return owners


def _repair_figure_question_owners(markdown: str, parse_result: dict,
                                   image_dir: Path) -> tuple[str, int]:
    """按 Doc2X 题号/图片纵坐标，把抢到上一题末尾的图移回所属题。"""
    owners = _figure_owner_map(parse_result, image_dir)
    if not owners:
        return markdown, 0
    import blocksplit
    import collection_structure

    try:
        units = collection_structure.split_markdown_units(
            markdown, label="Doc2X 图片归属恢复")
        chunks = [unit.markdown for unit in units]
    except collection_structure.CollectionStructureError:
        chunks = [markdown]

    output = markdown
    output_cursor = 0
    moved = 0
    for chunk in chunks:
        blocks = [block for block in blocksplit.split_blocks(chunk)
                  if block.zone == "stem"]
        by_number = {}
        for block in blocks:
            by_number.setdefault(block.number, []).append(block)
        replacements = {block.index: block.text for block in blocks}
        additions: dict[int, list[str]] = {}
        for block in blocks:
            for match in list(_IMAGE_REF_RE.finditer(block.text)):
                filename = Path(match.group("path")).name
                owner = next((owners[name] for name in _image_name_variants(filename)
                              if name in owners), None)
                targets = by_number.get(owner) or []
                if (not isinstance(block.number, int) or owner == block.number
                        or len(targets) != 1):
                    continue
                replacements[block.index] = replacements[block.index].replace(
                    match.group(0), "", 1)
                additions.setdefault(targets[0].index, []).append(match.group(0))
                moved += 1
        if not additions:
            continue
        for block in blocks:
            text = replacements[block.index]
            refs = additions.get(block.index) or []
            if refs:
                insertion = "\n\n".join(refs) + "\n\n"
                option = re.search(r"(?m)^\s*A\s*[.．、)）]", text)
                if option:
                    text = text[:option.start()] + insertion + text[option.start():]
                else:
                    text = text.rstrip() + "\n\n" + insertion.rstrip()
            replacements[block.index] = re.sub(r"\n{3,}", "\n\n", text).strip()

        rebuilt = chunk
        positions = []
        cursor = 0
        for block in blocks:
            pos = rebuilt.find(block.text, cursor)
            if pos < 0:
                positions = []
                break
            positions.append((pos, pos + len(block.text), block.index))
            cursor = pos + len(block.text)
        for start, end, index in reversed(positions):
            rebuilt = rebuilt[:start] + replacements[index] + rebuilt[end:]
        pos = output.find(chunk, output_cursor)
        if pos >= 0:
            output = output[:pos] + rebuilt + output[pos + len(chunk):]
            output_cursor = pos + len(rebuilt)
    return output, moved


def repair_markdown_from_layout(markdown: str, parse_result: dict,
                                image_dir: Path) -> tuple[str, int, int]:
    """对新下载或已缓存的 Doc2X 原文重放幂等布局修复。"""
    markdown, moved = _repair_figure_question_owners(
        markdown, parse_result, image_dir)
    markdown, choices = _repair_figure_choice_order(
        markdown, parse_result, image_dir)
    return markdown, moved, choices


def repair_cached_markdown(markdown: str, extract_dir: Path
                           ) -> tuple[str, int, int]:
    candidates = sorted(Path(extract_dir).glob("*_doc2x.json"))
    if len(candidates) != 1:
        return markdown, 0, 0
    try:
        payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return markdown, 0, 0
    if not isinstance(payload, dict):
        return markdown, 0, 0
    return repair_markdown_from_layout(markdown, payload,
                                       Path(extract_dir) / "images")


def _repair_figure_choice_order(markdown: str, parse_result: dict,
                                image_dir: Path) -> tuple[str, int]:
    """用 v3 父子关系把导出时打乱/漏标签的 A–D 图片选项重排为标准顺序。"""
    repaired = 0
    text = markdown
    for cluster in _figure_choice_clusters(parse_result, image_dir):
        lines = text.splitlines()
        hits = {}
        resolved = {}
        for index, line in enumerate(lines):
            for letter, filename in cluster.items():
                actual = next((name for name in _image_name_variants(filename)
                               if f"images/{name}" in line), None)
                if actual:
                    hits[letter] = index
                    resolved[letter] = actual
        if set(hits) != set("ABCD") or len(set(hits.values())) != 4:
            continue
        start, end = min(hits.values()), max(hits.values())
        while start > 0 and (not lines[start - 1].strip()
                             or _FIGURE_CAPTION_RE.match(lines[start - 1].strip())):
            start -= 1
        while end + 1 < len(lines) and (not lines[end + 1].strip()
                                        or _FIGURE_CAPTION_RE.match(lines[end + 1].strip())):
            end += 1
        # 重排会替换整段，因此必须先证明这一段只有标签、空行和这四张图。
        # 一旦夹有选项文字、公式或其它图片就保持原文，不能为修顺序静默删内容。
        markers = tuple(f"images/{filename}" for filename in resolved.values())
        safe_region = True
        for line in lines[start:end + 1]:
            stripped = line.strip()
            if not stripped or _FIGURE_CAPTION_RE.fullmatch(stripped):
                continue
            if (_IMAGE_REF_RE.fullmatch(stripped)
                    and any(marker in stripped for marker in markers)):
                continue
            safe_region = False
            break
        if not safe_region:
            continue
        replacement = []
        for letter in "ABCD":
            replacement.extend([f"{letter}.", "", f"![](images/{resolved[letter]})", ""])
        replacement = replacement[:-1]
        if lines[start:end + 1] != replacement:
            lines[start:end + 1] = replacement
            text = "\n".join(lines)
            repaired += 1
    if repaired:
        logger.info("Doc2X 按 v3 版面关系修复 %d 组图片选项", repaired)
    return text, repaired
