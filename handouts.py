"""讲义 Markdown 的文件模型、题目快照与安全存取。

讲义仍是普通 Markdown：文档级设置和题目来源元数据放在 YAML frontmatter，正文
只用不可见 HTML 注释标出题目/解析边界。自动分页属于编辑器派生状态，不写入文件；
只有 ``<!-- quizforge:page-break -->`` 是用户明确插入、需要长期保存的分页符。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import uuid

from ruamel.yaml import YAML

import config
import filestore
import qrender


SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
KIND = "handout"

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096

_FRONTMATTER_RE = re.compile(
    r"(?s)\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z")
_QUESTION_OPEN_RE = re.compile(
    r"^[ \t]*<!--\s*quizforge:question\s+([A-Za-z0-9_-]{6,80})\s*-->[ \t]*$")
_QUESTION_SOLUTION_RE = re.compile(
    r"^[ \t]*<!--\s*quizforge:solution\s+([A-Za-z0-9_-]{6,80})\s*-->[ \t]*$")
_QUESTION_END_RE = re.compile(
    r"^[ \t]*<!--\s*quizforge:end\s+([A-Za-z0-9_-]{6,80})\s*-->[ \t]*$")
PAGE_BREAK_MARKER = "<!-- quizforge:page-break -->"

_PAGE_FORMATS = frozenset({"a4", "slides"})
_PAPER_TONES = frozenset({"white", "cream"})
_SOLUTION_MODES = frozenset({"hidden", "inline", "appendix"})
_BLOCK_SOLUTION_MODES = frozenset({"inherit", "hidden", "inline", "appendix"})
_HEADER_FOOTER_KEYS = (
    "header_left", "header_center", "header_right",
    "footer_left", "footer_center", "footer_right",
)


class HandoutError(ValueError):
    """讲义请求可向用户展示的校验错误。"""


def _plain(value):
    """ruamel 的映射/序列转成可 jsonify 的普通 Python 容器。"""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _default_metadata(title: str = "新建讲义", page_format: str = "a4",
                      columns: int = 1) -> dict:
    page_format = page_format if page_format in _PAGE_FORMATS else "a4"
    columns = 2 if page_format == "a4" and int(columns or 1) == 2 else 1
    return {
        "quizforge_kind": KIND,
        "quizforge_schema": SCHEMA_VERSION,
        "title": str(title or "新建讲义").strip() or "新建讲义",
        "page_format": page_format,
        "columns": columns,
        "paper_tone": "white",
        "wimath_logo": False,
        "solution_default": "hidden",
        "header_footer": {
            "header_left": "", "header_center": "", "header_right": "",
            "footer_left": "", "footer_center": "第 {页码} / {总页数} 页",
            "footer_right": "",
        },
        "question_blocks": {},
    }


def normalize_metadata(raw, *, fallback_title: str = "新建讲义") -> dict:
    """宽松读取、严格收敛已知字段，同时保留未知 frontmatter 字段。"""
    meta = _plain(raw) if isinstance(raw, dict) else {}
    defaults = _default_metadata(fallback_title)
    out = deepcopy(meta)
    out["quizforge_kind"] = KIND

    schema = meta.get("quizforge_schema", SCHEMA_VERSION)
    try:
        schema = int(schema)
    except (TypeError, ValueError):
        schema = SCHEMA_VERSION
    out["quizforge_schema"] = max(1, schema)

    title = str(meta.get("title") or fallback_title).strip()
    out["title"] = title[:200] or defaults["title"]
    page_format = str(meta.get("page_format") or "a4").strip().lower()
    out["page_format"] = page_format if page_format in _PAGE_FORMATS else "a4"
    try:
        columns = int(meta.get("columns", 1))
    except (TypeError, ValueError):
        columns = 1
    out["columns"] = 2 if out["page_format"] == "a4" and columns == 2 else 1
    tone = str(meta.get("paper_tone") or "white")
    out["paper_tone"] = tone if tone in _PAPER_TONES else "white"
    logo = meta.get("wimath_logo", False)
    out["wimath_logo"] = (
        logo is True or (isinstance(logo, str) and logo.strip().lower() in {
            "1", "true", "yes", "on",
        })
    )
    solution = str(meta.get("solution_default") or "hidden")
    out["solution_default"] = (
        solution if solution in _SOLUTION_MODES else "hidden")

    incoming_hf = meta.get("header_footer")
    incoming_hf = incoming_hf if isinstance(incoming_hf, dict) else {}
    out["header_footer"] = {
        key: str(incoming_hf.get(key, defaults["header_footer"][key]) or "")[:500]
        for key in _HEADER_FOOTER_KEYS
    }

    blocks = meta.get("question_blocks")
    normalized_blocks = {}
    if isinstance(blocks, dict):
        for raw_id, raw_block in blocks.items():
            block_id = str(raw_id)
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", block_id):
                continue
            if not isinstance(raw_block, dict):
                raw_block = {}
            block = _plain(raw_block)
            placement = str(block.get("solution_placement") or "inherit")
            block["solution_placement"] = (
                placement if placement in _BLOCK_SOLUTION_MODES else "inherit")
            override = block.get("number_override")
            block["number_override"] = (
                str(override)[:80] if override not in (None, "") else None)
            block["render_confirmed"] = bool(block.get("render_confirmed", False))
            for key in ("source_id", "source_path", "source_mtime_ns",
                        "source_hash", "question_type", "source"):
                value = block.get(key)
                block[key] = str(value) if value not in (None, "") else ""
            normalized_blocks[block_id] = block
    out["question_blocks"] = normalized_blocks
    return out


def split_document(text: str, *, fallback_title: str = "新建讲义") -> tuple[dict, str, list[str]]:
    """完整 Markdown → (frontmatter, 正文, 告警)。裸 Markdown 也可安全打开。"""
    text = filestore.normalize_newlines(str(text or ""))
    warnings = []
    match = _FRONTMATTER_RE.match(text)
    if not match:
        meta = _default_metadata(fallback_title)
        warnings.append("文件没有 QuizForge 讲义 frontmatter；保存后会补齐 schema 1 元数据")
        body = text
    else:
        try:
            loaded = _yaml.load(match.group(1)) or {}
        except Exception as exc:
            # YAML 已坏时不能假装成功并在下次保存覆盖；把全文作为正文只读返回。
            meta = _default_metadata(fallback_title)
            meta["quizforge_schema"] = SCHEMA_VERSION + 1
            warnings.append(f"frontmatter 无法解析：{exc}")
            return meta, text, warnings
        meta = normalize_metadata(loaded, fallback_title=fallback_title)
        body = match.group(2)
        if loaded.get("quizforge_kind") not in (None, "", KIND):
            warnings.append("文件类型不是 QuizForge 讲义，将按只读 Markdown 打开")
            meta["quizforge_schema"] = SCHEMA_VERSION + 1
    warnings.extend(validate_markers(body, meta.get("question_blocks") or {}))
    return meta, body, warnings


def serialize_document(metadata: dict, body: str) -> str:
    """frontmatter + 正文 → 规范化 Markdown；未知字段原样保留。"""
    meta = normalize_metadata(metadata, fallback_title=str(metadata.get("title") or "新建讲义"))
    buffer = io.StringIO()
    _yaml.dump(meta, buffer)
    normalized_body = filestore.normalize_newlines(str(body or "")).lstrip("\n")
    return "---\n" + buffer.getvalue() + "---\n\n" + normalized_body


def _resolve_path(raw: str, *, must_exist: bool = True) -> tuple[Path, str]:
    """只允许访问当前题库 ``_handouts`` 内的 Markdown。"""
    value = str(raw or "").strip().replace("\\", "/")
    rel = PurePosixPath(value)
    parts = tuple(part for part in rel.parts if part not in ("", "."))
    if (rel.is_absolute() or not parts or parts[0] != config.HANDOUTS_DIR.name
            or any(part == ".." or part.startswith(".") for part in parts)
            or PurePosixPath(*parts).suffix.lower() != ".md"):
        raise HandoutError("讲义路径无效")
    root_path = Path(config.HANDOUTS_DIR).absolute()
    if root_path.is_symlink():
        raise HandoutError("讲义目录不能是符号链接")
    root = root_path.resolve()
    relative_parts = parts[1:]
    candidate = root.joinpath(*relative_parts)
    cursor = root
    for part in relative_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise HandoutError("讲义路径不能包含符号链接")
    target = candidate.resolve()
    if target == root or root not in target.parents:
        raise HandoutError("讲义路径越界")
    if must_exist and not target.is_file():
        raise FileNotFoundError("讲义不存在")
    if not must_exist:
        parent = target.parent.resolve()
        if parent != root and root not in parent.parents:
            raise HandoutError("讲义目录越界")
    return target, PurePosixPath(*parts).as_posix()


def list_documents() -> list[dict]:
    root = config.HANDOUTS_DIR
    if not root.is_dir():
        return []
    rows = []
    for path in root.rglob("*.md"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(config.BANK_DIR).as_posix()
            resolved, _ = _resolve_path(rel)
            if resolved != path.resolve():
                continue
            text = path.read_text(encoding="utf-8", newline="")
            meta, _body, warnings = split_document(
                text, fallback_title=path.stem)
            stat = path.stat()
            rows.append({
                "path": rel,
                "name": path.name,
                "title": meta.get("title") or path.stem,
                "page_format": meta.get("page_format", "a4"),
                "columns": meta.get("columns", 1),
                "mtime": str(stat.st_mtime_ns),
                "updated_at": stat.st_mtime,
                "read_only": int(meta.get("quizforge_schema", 1)) > SCHEMA_VERSION,
                "warning_count": len(warnings),
            })
        except (OSError, UnicodeError, HandoutError):
            continue
    rows.sort(key=lambda row: (-row["updated_at"], row["name"].casefold()))
    return rows


def _unique_path(title: str) -> tuple[Path, str]:
    root = config.HANDOUTS_DIR
    stem = filestore.safe_folder_name(str(title or "新建讲义")) or "新建讲义"
    for index in range(0, 1000):
        name = f"{stem}.md" if index == 0 else f"{stem}_{index + 1}.md"
        rel = f"{root.name}/{name}"
        path, normalized = _resolve_path(rel, must_exist=False)
        if not path.exists():
            return path, normalized
    raise HandoutError("同名讲义过多，请换一个标题")


def create_document(title: str, *, page_format: str = "a4", columns: int = 1,
                    body: str | None = None, metadata: dict | None = None) -> dict:
    title = str(title or "新建讲义").strip()[:200] or "新建讲义"
    path, rel = _unique_path(title)
    meta = normalize_metadata(metadata or _default_metadata(title, page_format, columns),
                              fallback_title=title)
    meta["title"] = title
    if metadata is None:
        meta["page_format"] = page_format if page_format in _PAGE_FORMATS else "a4"
        meta["columns"] = 2 if meta["page_format"] == "a4" and int(columns or 1) == 2 else 1
    if body is None:
        body = f"# {title}\n\n"
    text = serialize_document(meta, body)
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise HandoutError("讲义超过 8 MiB 上限")
    mtime = filestore.create_markdown_text(path, text)
    return {"path": rel, "mtime": str(mtime), "metadata": meta, "body": body}


def read_document(raw_path: str) -> dict:
    path, rel = _resolve_path(raw_path)
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise HandoutError("讲义超过 8 MiB 上限")
    text = path.read_text(encoding="utf-8", newline="")
    meta, body, warnings = split_document(text, fallback_title=path.stem)
    return {
        "path": rel,
        "mtime": str(path.stat().st_mtime_ns),
        "metadata": meta,
        "body": body,
        "warnings": warnings,
        "read_only": int(meta.get("quizforge_schema", 1)) > SCHEMA_VERSION,
    }


def write_document(raw_path: str, metadata: dict, body: str,
                   expected_mtime) -> dict:
    path, rel = _resolve_path(raw_path)
    try:
        expected = int(str(expected_mtime))
    except (TypeError, ValueError):
        raise HandoutError("文件版本无效") from None
    current = read_document(rel)
    if current["read_only"]:
        raise HandoutError("讲义 schema 高于当前程序版本，只能只读打开")
    text = serialize_document(metadata or {}, body)
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise HandoutError("讲义超过 8 MiB 上限")
    saved, mtime = filestore.write_markdown_text(path, text, expected)
    if not saved:
        return {"ok": False, "conflict": True, "path": rel, "mtime": str(mtime)}
    normalized, normalized_body, warnings = split_document(
        text, fallback_title=path.stem)
    return {
        "ok": True, "path": rel, "mtime": str(mtime),
        "metadata": normalized, "body": normalized_body, "warnings": warnings,
    }


def delete_document(raw_path: str, expected_mtime) -> dict:
    """删除整份讲义；复用写锁并以 mtime 防止误删外部刚修改的文件。"""
    path, rel = _resolve_path(raw_path)
    try:
        expected = int(str(expected_mtime))
    except (TypeError, ValueError):
        raise HandoutError("文件版本无效") from None
    with filestore._write_lock:
        # 进锁后重新拒绝替换、符号链接与版本变化，避免校验和 unlink 之间的竞态。
        checked, _ = _resolve_path(rel)
        latest = read_document(rel)
        if latest["read_only"]:
            raise HandoutError("讲义 schema 高于当前程序版本，只能只读打开")
        stat = checked.stat(follow_symlinks=False)
        if stat.st_mtime_ns != expected:
            return {"ok": False, "conflict": True, "path": rel,
                    "mtime": str(stat.st_mtime_ns)}
        checked.unlink()
        # 持久化目录项，断电后不会出现“接口已成功但文件重新出现”。
        try:
            directory_fd = os.open(checked.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Windows 通常不允许 fsync 目录；unlink 本身仍是唯一原子操作。
            pass
        filestore.invalidate_scan_cache(folder_structure=True)
    return {"ok": True, "path": rel}


def _snapshot_payload(record: dict) -> dict:
    fields = {
        "body": record.get("body") or "",
        "solution": record.get("solution") or "",
        "question_type": record.get("type") or "",
        "source": record.get("source") or "",
        "img_align": record.get("img_align") or "",
        "img_width": record.get("img_width"),
        "img_split": record.get("img_split"),
        "img_layouts": record.get("img_layouts") or [],
        "sol_img_split": record.get("sol_img_split"),
        "sol_img_layouts": record.get("sol_img_layouts") or [],
    }
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    path = config.BANK_DIR / record["path"]
    return {
        **fields,
        "source_id": str(record.get("id") or ""),
        "source_path": str(record.get("path") or ""),
        "source_mtime_ns": str(path.stat().st_mtime_ns) if path.is_file() else "",
        "source_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def question_snapshot(qid: str) -> dict:
    record = filestore.get_question(str(qid or ""))
    if not record:
        raise KeyError("原题不存在或已删除")
    return _snapshot_payload(record)


def _selected_question_records() -> list[dict]:
    # 选题篮通常只有少量题：热缓存直接取，冷启动只扫 frontmatter 头部定位路径，
    # 不能为了十几道题解析上万份完整 Markdown。
    records = filestore.records_from_ids(filestore.selected_ids())
    return filestore.list_questions(
        selected_only=True, sort="custom", records=records)


def _selected_question_summary(record: dict) -> dict:
    plain = re.sub(r"!\[\[[^\]]+\]\]", "[图片]", record.get("body") or "")
    plain = re.sub(r"\s+", " ", plain).strip()
    return {
        "id": str(record.get("id") or ""),
        "type": record.get("type") or "未分类",
        "source": record.get("source") or "未记录题源",
        "number": record.get("number"),
        "folder": record.get("folder") or "题库根目录",
        "excerpt": plain[:180],
        "path": record.get("path") or "",
        # qrender 与题库正式题卡、PDF 共用选项和图片布局规则；它会先转义外来
        # 文本，再只放行自己生成的结构标签，因此可作为受信任 HTML 交给前端。
        "body_html": str(qrender.render_body(
            record.get("body") or "", record.get("type"),
            img_layouts=record.get("img_layouts"),
            img_width=record.get("img_width"),
            img_align=record.get("img_align"),
            img_split=record.get("img_split"),
        )),
    }


def selected_question_summaries() -> list[dict]:
    """讲义选择器使用的轻量摘要，不渲染解析或备注。"""
    return [_selected_question_summary(record)
            for record in _selected_question_records()]


def selected_question_details() -> list[dict]:
    """题库选题抽屉使用的完整只读题卡数据。"""
    rows = []
    for record in _selected_question_records():
        tags = record.get("tags")
        collection = str(record.get("folder") or "")
        row = _selected_question_summary(record)
        row.update({
            "title": str(record.get("title") or ""),
            "difficulty": str(record.get("difficulty") or ""),
            "starred": bool(record.get("starred")),
            "tags": list(tags) if isinstance(tags, (list, tuple)) else [],
            # folder 是人类可读的末级名称；collection 保留完整相对路径供定位。
            "folder": PurePosixPath(collection).name if collection else "题库根目录",
            "collection": collection,
            "solution_html": str(qrender.render_solution(
                record.get("solution") or "",
                sol_img_layouts=record.get("sol_img_layouts"),
                sol_img_split=record.get("sol_img_split"),
            )),
            "note_html": str(qrender.render_body(record.get("note") or "")),
        })
        rows.append(row)
    return rows


def new_block_id() -> str:
    return "q_" + uuid.uuid4().hex


def validate_markers(body: str, question_meta: dict) -> list[str]:
    """只报告结构问题；任何异常都不改写原始正文。"""
    _blocks, warnings = parse_content(body, question_meta)
    return warnings


def parse_content(body: str, question_meta: dict | None = None) -> tuple[list[dict], list[str]]:
    """正文拆成普通 Markdown 与题目块，供导出和结构校验共用。"""
    question_meta = question_meta if isinstance(question_meta, dict) else {}
    lines = filestore.normalize_newlines(str(body or "")).splitlines(keepends=True)
    blocks = []
    warnings = []
    plain = []

    def flush_plain():
        if plain:
            blocks.append({"kind": "markdown", "text": "".join(plain)})
            plain.clear()

    index = 0
    seen = set()
    while index < len(lines):
        opening = _QUESTION_OPEN_RE.match(lines[index].rstrip("\n"))
        if not opening:
            plain.append(lines[index])
            index += 1
            continue
        block_id = opening.group(1)
        solution_index = end_index = None
        cursor = index + 1
        while cursor < len(lines):
            solution = _QUESTION_SOLUTION_RE.match(lines[cursor].rstrip("\n"))
            ending = _QUESTION_END_RE.match(lines[cursor].rstrip("\n"))
            if solution and solution.group(1) == block_id and solution_index is None:
                solution_index = cursor
            elif ending and ending.group(1) == block_id:
                end_index = cursor
                break
            elif _QUESTION_OPEN_RE.match(lines[cursor].rstrip("\n")):
                break
            cursor += 1
        if end_index is None:
            warnings.append(f"题目块 {block_id} 缺少结束标记，已按普通 Markdown 保留")
            plain.append(lines[index])
            index += 1
            continue
        flush_plain()
        body_end = solution_index if solution_index is not None else end_index
        question_body = "".join(lines[index + 1:body_end]).strip("\n")
        solution_body = (
            "".join(lines[solution_index + 1:end_index]).strip("\n")
            if solution_index is not None else "")
        if block_id in seen:
            warnings.append(f"题目块 id {block_id} 重复；保存前请重新插入其中一块")
        seen.add(block_id)
        meta = deepcopy(question_meta.get(block_id) or {})
        # frontmatter 允许保留未来未知字段，但不能让它覆盖正文里解析出的结构字段。
        blocks.append({
            **meta, "kind": "question", "block_id": block_id,
            "body": question_body, "solution": solution_body,
        })
        index = end_index + 1
    flush_plain()
    orphaned = sorted(set(question_meta) - seen)
    if orphaned:
        warnings.append(f"frontmatter 中有 {len(orphaned)} 个未被正文引用的题目块元数据")
    return blocks, warnings


def question_marker(block_id: str, body: str, solution: str = "") -> str:
    """生成可直接插入正文的题目快照标记。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", str(block_id or "")):
        raise HandoutError("题目块 id 无效")
    parts = [f"<!-- quizforge:question {block_id} -->", str(body or "").strip()]
    if solution:
        parts += [f"<!-- quizforge:solution {block_id} -->", str(solution).strip()]
    parts.append(f"<!-- quizforge:end {block_id} -->")
    return "\n\n".join(parts)
