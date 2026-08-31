"""QuizForge 本地提示词库。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
MAX_PROMPTS = 500
MAX_TITLE_LENGTH = 120
MAX_CATEGORY_LENGTH = 40
MAX_CONTENT_BYTES = 256 * 1024

_lock = threading.RLock()


class PromptStoreError(ValueError):
    """提示词数据无效或无法安全保存。"""


_OFFICIAL_SPECS = (
    {
        "id": "official-question-markdown",
        "title": "PDF/图片题目转 QuizForge Markdown",
        "category": "题目导入",
        "filename": "questions_to_quizforge.md",
    },
    {
        "id": "official-tex-template",
        "title": "PDF/TeX 样式转 QuizForge TeX",
        "category": "模板制作",
        "filename": "template_to_quizforge_tex.md",
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _clean_text(value, field: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise PromptStoreError(f"{field}必须是文本")
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if required and not cleaned:
        raise PromptStoreError(f"{field}不能为空")
    if len(cleaned) > limit:
        raise PromptStoreError(f"{field}过长（最多 {limit} 个字符）")
    return cleaned


def _clean_content(value) -> str:
    if not isinstance(value, str):
        raise PromptStoreError("提示词正文必须是文本")
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        raise PromptStoreError("提示词正文不能为空")
    if len(cleaned.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise PromptStoreError(
            f"提示词正文过大（最多 {MAX_CONTENT_BYTES // 1024}KB）")
    return cleaned


def _read_user_document(path: Path) -> dict:
    if not path.is_file():
        return {"schema": SCHEMA_VERSION, "prompts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromptStoreError(f"用户提示词文件无法读取：{exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        raise PromptStoreError("用户提示词文件版本不受支持")
    rows = data.get("prompts")
    if not isinstance(rows, list):
        raise PromptStoreError("用户提示词文件结构无效")
    cleaned = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise PromptStoreError("用户提示词文件包含无效记录")
        prompt_id = str(raw.get("id") or "")
        if (not prompt_id.startswith("user-") or len(prompt_id) > 80
                or prompt_id in seen):
            raise PromptStoreError("用户提示词文件包含无效或重复 ID")
        seen.add(prompt_id)
        cleaned.append({
            "id": prompt_id,
            "title": _clean_text(raw.get("title"), "标题", MAX_TITLE_LENGTH),
            "category": _clean_text(
                raw.get("category", "自定义"), "分类", MAX_CATEGORY_LENGTH),
            "content": _clean_content(raw.get("content")),
            "created": str(raw.get("created") or ""),
            "updated": str(raw.get("updated") or ""),
        })
    if len(cleaned) > MAX_PROMPTS:
        raise PromptStoreError(f"用户提示词数量超过上限 {MAX_PROMPTS}")
    return {"schema": SCHEMA_VERSION, "prompts": cleaned}


def _write_user_document(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise PromptStoreError(f"用户提示词保存失败：{exc}") from exc


def _official_prompts(official_dir: Path) -> list[dict]:
    result = []
    for spec in _OFFICIAL_SPECS:
        path = official_dir / spec["filename"]
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise PromptStoreError(f"官方提示词缺失：{spec['filename']}") from exc
        result.append({
            "id": spec["id"],
            "title": spec["title"],
            "category": spec["category"],
            "content": content,
            "readonly": True,
            "official": True,
            "created": "",
            "updated": "",
        })
    return result


def list_prompts(path: Path, official_dir: Path) -> list[dict]:
    with _lock:
        users = _read_user_document(path)["prompts"]
        return _official_prompts(official_dir) + [
            {**row, "readonly": False, "official": False} for row in users
        ]


def create_prompt(path: Path, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise PromptStoreError("请求内容必须是 JSON 对象")
    with _lock:
        document = _read_user_document(path)
        if len(document["prompts"]) >= MAX_PROMPTS:
            raise PromptStoreError(f"用户提示词最多保存 {MAX_PROMPTS} 条")
        now = _now()
        row = {
            "id": f"user-{uuid.uuid4().hex}",
            "title": _clean_text(payload.get("title"), "标题", MAX_TITLE_LENGTH),
            "category": _clean_text(
                payload.get("category", "自定义"), "分类", MAX_CATEGORY_LENGTH),
            "content": _clean_content(payload.get("content")),
            "created": now,
            "updated": now,
        }
        document["prompts"].append(row)
        _write_user_document(path, document)
        return {**row, "readonly": False, "official": False}


def update_prompt(path: Path, prompt_id: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise PromptStoreError("请求内容必须是 JSON 对象")
    if not str(prompt_id).startswith("user-"):
        raise PromptStoreError("官方提示词为只读，不能修改")
    allowed = {"title", "category", "content"}
    if not (allowed & set(payload)):
        raise PromptStoreError("没有可更新的字段")
    with _lock:
        document = _read_user_document(path)
        row = next((item for item in document["prompts"]
                    if item["id"] == prompt_id), None)
        if row is None:
            raise KeyError(prompt_id)
        if "title" in payload:
            row["title"] = _clean_text(
                payload["title"], "标题", MAX_TITLE_LENGTH)
        if "category" in payload:
            row["category"] = _clean_text(
                payload["category"], "分类", MAX_CATEGORY_LENGTH)
        if "content" in payload:
            row["content"] = _clean_content(payload["content"])
        row["updated"] = _now()
        _write_user_document(path, document)
        return {**row, "readonly": False, "official": False}


def delete_prompt(path: Path, prompt_id: str) -> None:
    if not str(prompt_id).startswith("user-"):
        raise PromptStoreError("官方提示词为只读，不能删除")
    with _lock:
        document = _read_user_document(path)
        remaining = [row for row in document["prompts"]
                     if row["id"] != prompt_id]
        if len(remaining) == len(document["prompts"]):
            raise KeyError(prompt_id)
        document["prompts"] = remaining
        _write_user_document(path, document)
