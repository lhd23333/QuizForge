"""好题、好卷、好资料自动导入 Profile。

Profile 层只编排识别结果与题卡元数据；OCR 本身由调用方注入，便于监听器和
离线测试复用现有 converter 而不在这里读取凭据。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

import importer


_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_QUESTION_NUMBER = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__|\*)?\s*第?\s*(\d{1,3})\s*(?:\*\*|__|\*)?\s*[.．、题]"
)


def sanitize_output_name(raw: str, suffix: str = "") -> str:
    """收敛为 Windows 可用且不超过 120 字符的普通文件名片段。"""
    name = Path(str(raw or "")).stem.strip().rstrip(" .")
    name = _INVALID_NAME.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "未命名"
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(?:COM|LPT)[1-9]", name, re.I):
        name = f"_{name}"
    return (name + str(suffix))[:120].rstrip(" .") or "未命名"


def extract_question_number(text: str) -> tuple[int | None, str]:
    """返回首行主题号及移除该主题号后的题干，保留小题号和选项。"""
    body = str(text or "").lstrip()
    match = _QUESTION_NUMBER.match(body)
    if not match:
        return None, body
    number = int(match.group(1))
    stripped = body[match.end():].lstrip()
    return (number if number > 0 else None), stripped


def _source_id(source: Path) -> str:
    return hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]


def _write_card(path: Path, body: str, *, source: Path, profile: str,
                meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "quizforge_kind": "question",
        "id": uuid.uuid4().hex,
        "source": source.name,
        "qf_source_kind": profile,
    }
    data.update(meta or {})
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}"
                        for k, v in data.items()] + ["---", "", body.strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _result_markdown(result) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("markdown", "md", "text", "body"):
            if isinstance(result.get(key), str):
                return result[key]
    raise ValueError("OCR 结果缺少 Markdown 文本")


def _ocr(source: Path, workspace: Path, *, ocr_backend: str, engine: str) -> str:
    """调用可选的注入 OCR；真实监听器通过 monkeypatch/injector 提供它。"""
    import converter
    runner = getattr(converter, "source_ocr", None)
    if callable(runner):
        return _result_markdown(runner(source, workspace,
                                       ocr_backend=ocr_backend, engine=engine))
    import converter
    import mineru_store
    return converter.convert_file(
        source, mineru_store.resolve(), is_image=source.suffix.lower() in
        {".png", ".jpg", ".jpeg", ".webp", ".bmp"}, include_solution=True,
        engine=engine, ocr_backend=ocr_backend)


def convert_good_question(source: Path, workspace: Path, *, ocr_backend: str,
                          engine: str) -> dict:
    source = Path(source).resolve()
    markdown = _ocr(source, Path(workspace), ocr_backend=ocr_backend, engine=engine)
    number, body = extract_question_number(markdown)
    blocks = importer.split_questions(markdown)
    warnings = []
    if len(blocks) != 1:
        warnings.append("好题输入识别到多道题，需人工校对")
    base = sanitize_output_name(source.name)
    if number is not None:
        base = sanitize_output_name(source.name, f"第{number}题")
    out = Path(workspace) / f"{base}.md"
    _write_card(out, body, source=source, profile="good_question",
                meta={"number": number, "qf_review_required": bool(warnings)})
    return {"profile": "good_question", "source": str(source),
            "cards": [str(out.relative_to(workspace))], "warnings": warnings,
            "staged_root": str(workspace)}


def _split_blocks(markdown: str) -> list[tuple[str, str, int | None, int | None]]:
    blocks = importer.split_questions(markdown)
    if len(blocks) == 1 and blocks[0].strip() == markdown.strip():
        return [(markdown.strip(), "body", None, None)]
    out = []
    for index, block in enumerate(blocks, 1):
        number, body = extract_question_number(block)
        stem, solution = importer.split_solution(body, scan_markers=True)
        if solution:
            out.append((stem, "question", None, None))
            out.append((solution, "solution", None, None))
        else:
            out.append((body, "question", None, None))
    return out


def _convert_multi(source: Path, workspace: Path, *, profile: str,
                   ocr_backend: str, engine: str) -> dict:
    markdown = _ocr(source, workspace, ocr_backend=ocr_backend, engine=engine)
    document_id = _source_id(source)
    version_id = uuid.uuid4().hex
    root = Path(workspace) / sanitize_output_name(source.name)
    cards = []
    for index, (body, kind, page_start, page_end) in enumerate(
            _split_blocks(markdown), 1):
        if kind == "question":
            number, clean = extract_question_number(body)
            stem = sanitize_output_name(source.name,
                                        f"第{number}题" if number else f"第{index}题")
        else:
            clean = body
            stem = sanitize_output_name(source.name, f"{kind}第{index}块")
        relative = Path(f"{stem}.md")
        path = root / relative
        _write_card(path, clean, source=source, profile=profile, meta={
            "source_document_id": document_id,
            "source_document_name": source.name,
            "source_version_id": version_id,
            "source_block_id": uuid.uuid4().hex,
            "source_block_index": index,
            "source_block_type": {"question": "question", "solution": "solution"}.get(kind, "body"),
            "source_page_start": page_start,
            "source_page_end": page_end,
            "source_order": index,
        })
        cards.append(str(root.name + "/" + relative.as_posix()))
    return {"profile": profile, "source": str(source), "cards": cards,
            "source_document_id": document_id, "source_version_id": version_id,
            "staged_root": str(workspace)}


def convert_good_paper(source: Path, workspace: Path, *, ocr_backend: str,
                       engine: str) -> dict:
    return _convert_multi(Path(source).resolve(), Path(workspace), profile="good_paper",
                          ocr_backend=ocr_backend, engine=engine)


def convert_good_material(source: Path, workspace: Path, *, ocr_backend: str,
                          engine: str) -> dict:
    return _convert_multi(Path(source).resolve(), Path(workspace), profile="good_material",
                          ocr_backend=ocr_backend, engine=engine)


def build_output_manifest(profile: str, source: Path, staged_root: Path) -> dict:
    import hashlib
    root = Path(staged_root)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files.append({"path": path.relative_to(root).as_posix(), "sha256": digest})
    if not files:
        raise ValueError("输出没有文件")
    return {"profile": profile, "source_path": str(Path(source).resolve()),
            "files": files}
