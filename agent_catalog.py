"""Agent 可复用资产目录。

Skill、导出模板和用户偏好是 Agent 的配置资产，不属于题库 Markdown，因此各自
使用独立的 JSON 元数据文件。上传内容只会写入 ``config.DATA_DIR`` 下的专用目录，
不会根据客户端传来的路径覆盖任意本地文件。

本模块刻意不执行 Skill 中的代码，也不负责 TeX 编译。它只做登记、解析、预览
状态和启用状态管理；真正的题库写入及导出仍由 Agent 工具层和 exporter 负责。
"""

from __future__ import annotations

import io
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from flask import Blueprint, jsonify, request, send_file

import config
import template_pipeline
import tex_sandbox

logger = logging.getLogger(__name__)


class CatalogError(ValueError):
    """目录参数、上传内容或状态转换无效。"""

    def __init__(self, message: str, *, code: str = "invalid_catalog",
                 status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


_lock = threading.RLock()
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_MAX_NAME = 160
_MAX_DESCRIPTION = 4000
_MAX_STEPS = 80
_MAX_STEP_LENGTH = 1000
_MAX_SKILL_FILES = 120
_MAX_SKILL_FILE_BYTES = 8 * 1024 * 1024
_MAX_SKILL_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_TEMPLATE_BYTES = 64 * 1024 * 1024
_MAX_TEMPLATE_ZIP_MEMBERS = 300
_MAX_TEMPLATE_ZIP_UNPACKED = 160 * 1024 * 1024
_MAX_TEMPLATE_ZIP_RATIO = 200
_MAX_PREFERENCE_BYTES = 128 * 1024

_EXECUTABLE_SUFFIXES = {
    ".ade", ".app", ".apk", ".bat", ".bin", ".cmd", ".com", ".cpl",
    ".dll", ".dylib", ".exe", ".gadget", ".hta", ".jar", ".js", ".jse",
    ".lnk", ".msi", ".msp", ".ocx", ".ps1", ".py", ".pyc", ".pyo",
    ".rb", ".scr", ".sh", ".so", ".sys", ".vb", ".vbe", ".vbs", ".wsf",
}
_SKILL_ALLOWED_SUFFIXES = {
    ".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv",
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif",
}
_TEMPLATE_RESOURCE_SUFFIXES = {
    ".tex", ".sty", ".cls", ".bbx", ".cbx", ".def", ".cfg", ".bib",
    ".png", ".jpg", ".jpeg", ".webp", ".pdf", ".svg", ".eps", ".ttf",
    ".otf", ".txt", ".md", ".json", ".yaml", ".yml",
}
_SECRET_FIELD_MARKERS = ("api_key", "apikey", "token", "password", "secret",
                         "credential", "private_key")
_DENIED_SKILL_KEYS = {
    "command", "commands", "exec", "execute", "executable", "shell",
    "shell_command", "python", "script", "scripts", "subprocess",
}
_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                           *(f"LPT{i}" for i in range(1, 10))}
_DEFAULT_SKILL_TOOLS = {
    "list_folders", "search_questions", "read_question", "create_question",
    "update_question", "move_question", "create_folder", "import_questions",
    "bulk_tag", "select_questions", "export_exam", "create_skill",
    "update_preferences", "register_template",
}

_DEFAULT_PREFERENCES: dict[str, Any] = {
    "ocr_backend": "mineru",
    "default_bank": "",
    "default_workdir": "",
    "naming_rule": "",
    "question_type_mapping": {},
    "template_id": None,
    "export_defaults": {},
}
# 只读副本供设置页展示默认值；调用方不应直接修改内部字典。
DEFAULT_PREFERENCES = dict(_DEFAULT_PREFERENCES)


def _path(value: Any) -> Path:
    return Path(value).expanduser()


def _metadata_path(kind: str) -> Path:
    if kind == "skills":
        return _path(config.AGENT_SKILLS_PATH)
    if kind == "templates":
        return _path(config.AGENT_TEMPLATES_PATH)
    if kind == "preferences":
        return _path(config.AGENT_PREFERENCES_PATH)
    raise CatalogError("未知的 Agent 目录类型", code="invalid_kind")


def _asset_root(kind: str) -> Path:
    if kind == "skills":
        return _path(config.AGENT_SKILLS_DIR)
    if kind == "templates":
        return _path(config.AGENT_TEMPLATES_DIR)
    raise CatalogError("未知的 Agent 资产类型", code="invalid_kind")


def _empty(kind: str) -> dict[str, Any]:
    if kind == "preferences":
        return {"version": 1, "preferences": {}}
    version = template_pipeline.CATALOG_SCHEMA if kind == "templates" else 1
    return {"version": version, kind: [], "active_id": None}


def _load(kind: str) -> dict[str, Any]:
    path = _metadata_path(kind)
    data = _empty(kind)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return data
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if kind == "templates":
            raise CatalogError(
                "模板目录无法读取，请修复或恢复 agent_templates.json 后重试",
                code="catalog_corrupt", status=500) from exc
        logger.warning("Agent %s 元数据无法读取，按空目录处理", kind)
        return data
    if not isinstance(raw, dict):
        if kind == "templates":
            raise CatalogError("模板目录格式无效", code="catalog_corrupt", status=500)
        return data
    if kind == "templates" and (
            not isinstance(raw.get("templates"), list)
            or any(not isinstance(row, dict) for row in raw["templates"])):
        raise CatalogError("模板目录格式无效", code="catalog_corrupt", status=500)
    data.update(raw)
    try:
        data["version"] = max(1, int(raw.get("version", 1) or 1))
    except (TypeError, ValueError):
        data["version"] = 1
    if kind == "preferences":
        data["preferences"] = (dict(raw.get("preferences") or {})
                                if isinstance(raw.get("preferences"), dict)
                                else {})
    else:
        data[kind] = ([row for row in (raw.get(kind) or []) if isinstance(row, dict)]
                      if isinstance(raw.get(kind), list) else [])
        data.setdefault("active_id", None)
    if kind == "templates":
        data, changed = template_pipeline.migrate_catalog(data)
        if changed:
            try:
                _save("templates", data)
            except OSError:
                logger.warning("Agent 模板目录 schema v2 迁移暂时无法写回", exc_info=True)
    return data


def _save(kind: str, data: dict[str, Any]) -> None:
    """以同目录临时文件 + fsync + replace 写入，避免半个 JSON 文件。"""
    path = _metadata_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _new_id() -> str:
    return secrets.token_urlsafe(16).replace("=", "")


def _id(value: Any) -> str:
    result = str(value or "").strip()
    if not _ID_RE.fullmatch(result):
        raise CatalogError("Agent 资产编号无效", code="invalid_id", status=404)
    return result


def _text(value: Any, field: str, *, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", "").strip()
    if required and not value:
        raise CatalogError(f"{field}不能为空", code="missing_field")
    if len(value) > limit:
        raise CatalogError(f"{field}过长", code="field_too_long")
    return value


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise CatalogError("配置嵌套层级过深", code="value_too_deep")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or abs(value) == float("inf")):
            raise CatalogError("配置包含无效数字", code="invalid_value")
        return value
    if isinstance(value, dict):
        if len(value) > 200:
            raise CatalogError("配置字段过多", code="value_too_large")
        return {str(k)[:100]: _json_safe(v, depth=depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > 500:
            raise CatalogError("配置列表过长", code="value_too_large")
        return [_json_safe(item, depth=depth + 1) for item in value]
    raise CatalogError("配置包含不可序列化值", code="invalid_value")


def _reject_secret_keys(value: Any) -> None:
    """递归拒绝配置对象中的凭据字段，避免把密钥藏在嵌套参数里。"""
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).casefold()
            if any(marker in name for marker in _SECRET_FIELD_MARKERS):
                raise CatalogError("Agent 资产中不能保存密钥或凭据", code="secret_field")
            _reject_secret_keys(item)


def _reject_executable_keys(value: Any) -> None:
    """声明式 Skill 的任意嵌套层都不能声明命令、脚本或解释器。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _DENIED_SKILL_KEYS:
                raise CatalogError(f"Skill 声明禁止执行字段：{key}", code="executable_field")
            _reject_executable_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_executable_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_keys(item)


def _redact_public(value: Any) -> Any:
    """即使用户手工编辑了 JSON，也不把疑似凭据字段回传到前端。"""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            if any(marker in name.casefold() for marker in _SECRET_FIELD_MARKERS):
                result[name] = "[已隐藏]"
            else:
                result[name] = _redact_public(item)
        return result
    if isinstance(value, list):
        return [_redact_public(item) for item in value]
    return value


def _public(row: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """过滤内部绝对路径和原始二进制，只返回前端需要的元数据。"""
    result = {key: value for key, value in row.items()
              if key not in {"storage_dir", "source_path", "source_bytes"}}
    if kind == "skills":
        result["files"] = list(row.get("files") or [])
        result["tools"] = list(row.get("tools") or [])
        result["steps"] = list(row.get("steps") or [])
    if kind == "templates":
        result["fields"] = list(row.get("fields") or [])
        result["supported_modes"] = list(row.get("supported_modes") or [])
        result["manifest"] = dict(row.get("manifest") or {})
        result["validation"] = dict(row.get("validation") or {})
        defaults = row.get("default_params")
        result["default_params"] = dict(defaults) if isinstance(defaults, dict) else {}
        preview = row.get("preview")
        result["preview"] = dict(preview) if isinstance(preview, dict) else {}
        if result["preview"].get("rendered"):
            result["preview_url"] = f"/api/templates/{row.get('id')}/preview/file"
        if "draft_source" in result:
            result["draft_source"] = str(result["draft_source"])[:30000]
    return _redact_public(result)


def _find(rows: list[dict[str, Any]], item_id: str, *, kind: str) -> dict[str, Any]:
    item_id = _id(item_id)
    for row in rows:
        if str(row.get("id")) == item_id:
            return row
    raise CatalogError("Agent 资产不存在", code="not_found", status=404)


def _now() -> float:
    return round(time.time(), 3)


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


def _safe_member_name(raw: Any, *, allow_root: bool = False) -> str | None:
    name = str(raw or "").replace("\\", "/")
    if not name or "\x00" in name:
        return None
    # Windows 驱动器路径和 UNC 路径即使经过 PurePosixPath 也可能在落盘时
    # 被解释成绝对路径，不能把它们当作普通 ZIP/上传成员名。
    if re.match(r"^[A-Za-z]:", name) or name.startswith("//"):
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    # 目录版主要运行在 Windows；这些字符会让落盘路径失真或触发保留设备名。
    if any(re.search(r'[<>:"|?*]', part) for part in path.parts):
        return None
    if any(part.endswith((".", " ")) for part in path.parts):
        return None
    if any(Path(part).stem.upper() in _WINDOWS_RESERVED_NAMES for part in path.parts):
        return None
    if not allow_root and any(part.startswith(".") for part in path.parts):
        return None
    return path.as_posix()


def _is_executable_name(name: str) -> bool:
    lower = name.lower()
    suffix = Path(lower).suffix
    if suffix in _EXECUTABLE_SUFFIXES:
        return True
    # 无扩展名的脚本通常以 shebang 开头；内容检查在读取后继续进行。
    return False


def _read_entry(entry: Any) -> tuple[str, bytes]:
    """读取 Flask FileStorage、(name, bytes) 或 (name, stream) 形式的条目。"""
    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        name, value = entry
    else:
        name = getattr(entry, "filename", "")
        value = getattr(entry, "stream", entry)
    safe = _safe_member_name(name)
    if not safe:
        raise CatalogError("Skill 文件名包含非法路径", code="path_traversal")
    if _is_executable_name(safe):
        raise CatalogError(f"Skill 禁止包含可执行文件：{safe}", code="executable_file")
    suffix = Path(safe).suffix.lower()
    base = Path(safe).name.lower()
    if suffix not in _SKILL_ALLOWED_SUFFIXES and base not in {"skill", "readme"}:
        raise CatalogError(f"Skill 文件类型不受支持：{safe}", code="unsupported_file")
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, bytearray):
        data = bytes(value)
    elif hasattr(value, "read"):
        try:
            data = value.read(_MAX_SKILL_FILE_BYTES + 1)
        except OSError as exc:
            raise CatalogError("读取 Skill 上传文件失败", code="read_failed") from exc
        if isinstance(data, str):
            data = data.encode("utf-8")
    else:
        raise CatalogError("Skill 上传内容无效", code="invalid_upload")
    if len(data) > _MAX_SKILL_FILE_BYTES:
        raise CatalogError(f"Skill 文件过大：{safe}", code="file_too_large")
    # 文本声明文件不应携带 shell shebang 或 NUL 二进制内容。
    if b"\x00" in data or data.startswith((b"#!", b"MZ")):
        raise CatalogError(f"Skill 文件疑似可执行内容：{safe}", code="executable_file")
    return safe, data


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("\n", 1)
    if len(parts) != 2:
        return {}
    rest = parts[1]
    end = re.search(r"\n---\s*(?:\n|$)", rest)
    if not end:
        return {}
    block = rest[:end.start()]
    try:
        from ruamel.yaml import YAML
        parsed = YAML(typ="safe").load(block)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        # 声明文件解析失败时仍可作为草稿导入，避免把用户资料当成执行内容。
        result: dict[str, Any] = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                result[key.strip()] = value.strip().strip("\"'")
        return result


def _as_list(value: Any, *, limit: int, field: str) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in values[:limit]:
        text = _text(item, field, limit=_MAX_STEP_LENGTH)
        if text:
            result.append(text)
    return result


def _parse_skill_text(text: str, *, fallback_name: str) -> dict[str, Any]:
    meta = _frontmatter(text)
    _reject_executable_keys(meta)
    headings = re.findall(r"^#{1,3}\s+(.+?)\s*$", text, flags=re.MULTILINE)
    bullets = re.findall(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$", text,
                        flags=re.MULTILINE)
    name = _text(meta.get("name") or (headings[0] if headings else fallback_name),
                 "Skill 名称", limit=_MAX_NAME, required=True)
    description = _text(meta.get("description") or meta.get("summary") or "",
                         "Skill 描述", limit=_MAX_DESCRIPTION)
    trigger = _text(meta.get("trigger") or meta.get("triggers") or "",
                     "触发条件", limit=1000)
    steps = _as_list(meta.get("steps") or bullets, limit=_MAX_STEPS, field="步骤")
    tools = _as_list(meta.get("tools") or meta.get("permissions"),
                     limit=60, field="工具权限")
    tools = _validate_tools(tools)
    if not steps:
        steps = ["读取用户输入并确认参数", "调用声明的题库工具", "返回处理结果"]
    parameters = meta.get("parameters") or meta.get("params") or {}
    if not isinstance(parameters, dict):
        raise CatalogError("Skill 参数必须是对象", code="invalid_parameters")
    _reject_secret_keys(parameters)
    parameters = _json_safe(parameters)
    return {"name": name, "description": description, "trigger": trigger,
            "steps": steps, "tools": tools, "parameters": parameters,
            "examples": _as_list(meta.get("examples"), limit=20, field="示例")}


def _parse_skill_mapping(meta: dict[str, Any], *, fallback_name: str) -> dict[str, Any]:
    """把 JSON/YAML 声明转换为与 Markdown 草稿相同的结构。"""
    _reject_executable_keys(meta)
    name = _text(meta.get("name") or meta.get("title") or fallback_name,
                 "Skill 名称", limit=_MAX_NAME, required=True)
    description = _text(meta.get("description") or meta.get("summary") or "",
                         "Skill 描述", limit=_MAX_DESCRIPTION)
    trigger = _text(meta.get("trigger") or meta.get("triggers") or "",
                     "触发条件", limit=1000)
    steps = _as_list(meta.get("steps"), limit=_MAX_STEPS, field="步骤")
    tools = _validate_tools(_as_list(meta.get("tools") or meta.get("permissions"),
                                    limit=60, field="工具权限"))
    parameters = meta.get("parameters") or meta.get("params") or {}
    if not isinstance(parameters, dict):
        raise CatalogError("Skill 参数必须是对象", code="invalid_parameters")
    _reject_secret_keys(parameters)
    examples = _as_list(meta.get("examples"), limit=20, field="示例")
    return {"name": name, "description": description, "trigger": trigger,
            "steps": steps or ["读取用户输入并确认参数", "调用声明的题库工具", "返回处理结果"],
            "tools": tools, "parameters": _json_safe(parameters), "examples": examples}


def _validate_tools(tools: Iterable[Any]) -> list[str]:
    result = []
    for raw in tools:
        value = _text(raw, "工具权限", limit=100)
        if not value:
            continue
        if value.casefold() in {"shell", "python", "exec", "execute", "subprocess"}:
            raise CatalogError(f"Skill 不允许使用执行工具：{value}", code="executable_tool")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value):
            raise CatalogError(f"工具权限名称无效：{value}", code="invalid_tool")
        if value not in result:
            result.append(value)
    return result


def _skill_row(parsed: dict[str, Any], *, files: list[str], source: str,
               skill_id: str | None = None) -> dict[str, Any]:
    now = _now()
    return {
        "id": skill_id or _new_id(),
        "name": parsed["name"],
        "description": parsed.get("description", ""),
        "trigger": parsed.get("trigger", ""),
        "steps": list(parsed.get("steps") or []),
        "tools": list(parsed.get("tools") or []),
        "parameters": dict(parsed.get("parameters") or {}),
        "examples": list(parsed.get("examples") or []),
        "files": files,
        "source": source,
        "status": "draft",
        "version": "1.0.0",
        "created_at": now,
        "updated_at": now,
    }


def _infer_skill(prompt: str, *, name: str | None = None) -> dict[str, Any]:
    prompt = _text(prompt, "Skill 描述", limit=_MAX_DESCRIPTION, required=True)
    lower = prompt.casefold()
    tools: list[str] = []
    mapping = (("目录", "list_folders"), ("搜索", "search_questions"),
               ("查题", "search_questions"), ("读取", "read_question"),
               ("题目", "read_question"), ("导出", "export_exam"),
               ("模板", "register_template"), ("偏好", "update_preferences"),
               ("导入", "import_questions"))
    for marker, tool in mapping:
        if marker in prompt or marker.casefold() in lower:
            if tool not in tools:
                tools.append(tool)
    if not tools:
        tools = ["search_questions", "read_question"]
    sentences = [part.strip(" 。；;\n\t") for part in
                 re.split(r"[。；;.!！?？\n]+", prompt) if part.strip()]
    steps = sentences[:_MAX_STEPS] or [prompt]
    title = _text(name or (sentences[0] if sentences else "自定义题库工作流"),
                  "Skill 名称", limit=_MAX_NAME)
    if len(title) > 80:
        title = title[:80].rstrip()
    return {"name": title, "description": prompt,
            "trigger": f"当用户提出与“{title}”相关的请求时",
            "steps": steps, "tools": tools, "parameters": {}, "examples": []}


def _store_skill(row: dict[str, Any], files_data: list[tuple[str, bytes]] | None = None) -> dict[str, Any]:
    root = _asset_root("skills")
    directory = (root / row["id"]).resolve()
    if root.resolve() not in directory.parents:
        raise CatalogError("Skill 存储路径无效", code="path_invalid", status=500)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        if files_data:
            for rel, data in files_data:
                target = (directory / PurePosixPath(rel)).resolve()
                if target != directory and directory not in target.parents:
                    raise CatalogError("Skill 文件路径越界", code="path_traversal")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(data)
        with _lock:
            data = _load("skills")
            data["skills"].append(row)
            _save("skills", data)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return _public(row, kind="skills")


def generate_skill_draft(description: str, *, name: str | None = None) -> dict[str, Any]:
    """根据自然语言生成结构化草稿；生成结果永远是 ``draft``。"""
    parsed = _infer_skill(description, name=name)
    row = _skill_row(parsed, files=[], source="natural_language")
    return _store_skill(row)


create_skill_draft = generate_skill_draft


def import_skill_folder(source: Any, *, name: str | None = None,
                        description: str | None = None) -> dict[str, Any]:
    """导入上传的 Skill 文件夹或 ZIP。

    ``source`` 可为 Flask 文件条目列表、``Path`` 目录/文件或 ``(filename,
    bytes)``。这让路由和离线测试共享同一套安全校验。
    """
    entries: list[tuple[str, bytes]] = []
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file() and not path.is_dir():
            raise CatalogError("Skill 来源不存在", code="source_not_found", status=404)
        if path.is_file() and path.suffix.lower() == ".zip":
            entries = _zip_entries(path.read_bytes(), kind="skill")
        elif path.is_file():
            entries = [_read_entry((path.name, path.read_bytes()))]
        else:
            for item in sorted(path.rglob("*")):
                if item.is_symlink():
                    raise CatalogError("Skill 文件夹不能包含符号链接", code="symlink_file")
                if item.is_file():
                    try:
                        if item.stat().st_size > _MAX_SKILL_FILE_BYTES:
                            raise CatalogError(f"Skill 文件过大：{item.name}", code="file_too_large")
                    except OSError as exc:
                        raise CatalogError("读取 Skill 文件失败", code="read_failed") from exc
                    rel = item.relative_to(path).as_posix()
                    entries.append(_read_entry((rel, item.read_bytes())))
    elif isinstance(source, (bytes, bytearray)):
        entries = _zip_entries(bytes(source), kind="skill")
    else:
        raw_entries = source if isinstance(source, (list, tuple, set)) else [source]
        entries = [_read_entry(item) for item in raw_entries]
    if not entries:
        raise CatalogError("Skill 文件夹为空", code="empty_skill")
    if len(entries) > _MAX_SKILL_FILES:
        raise CatalogError("Skill 文件数量过多", code="too_many_files")
    total = sum(len(data) for _name, data in entries)
    if total > _MAX_SKILL_TOTAL_BYTES:
        raise CatalogError("Skill 文件总大小超出限制", code="total_too_large")
    names = [name for name, _data in entries]
    main = next((item for item in entries
                 if Path(item[0]).name.casefold() in {
                     "skill.md", "skill.markdown", "skill.json", "skill.yaml", "skill.yml",
                 }), None)
    if main is None:
        main = next((item for item in entries
                     if Path(item[0]).suffix.lower() in {".md", ".markdown", ".json", ".yaml", ".yml"}), None)
    if main is None:
        raise CatalogError("Skill 文件夹需要 SKILL.md、SKILL.json 或 YAML 声明文件",
                           code="missing_manifest")
    suffix = Path(main[0]).suffix.casefold()
    try:
        text = main[1].decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CatalogError("Skill 声明文件必须是 UTF-8 文本", code="invalid_encoding") from exc
    if suffix == ".json":
        try:
            raw_meta = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CatalogError("Skill JSON 声明格式无效", code="invalid_manifest") from exc
        if not isinstance(raw_meta, dict):
            raise CatalogError("Skill JSON 声明必须是对象", code="invalid_manifest")
        parsed = _parse_skill_mapping(raw_meta, fallback_name=Path(main[0]).stem)
    elif suffix in {".yaml", ".yml"}:
        try:
            from ruamel.yaml import YAML
            raw_meta = YAML(typ="safe").load(text)
        except Exception as exc:
            raise CatalogError("Skill YAML 声明格式无效", code="invalid_manifest") from exc
        if not isinstance(raw_meta, dict):
            raise CatalogError("Skill YAML 声明必须是对象", code="invalid_manifest")
        parsed = _parse_skill_mapping(dict(raw_meta), fallback_name=Path(main[0]).stem)
    else:
        parsed = _parse_skill_text(text, fallback_name=Path(main[0]).stem)
    if name:
        parsed["name"] = _text(name, "Skill 名称", limit=_MAX_NAME, required=True)
    if description is not None:
        parsed["description"] = _text(description, "Skill 描述", limit=_MAX_DESCRIPTION)
    row = _skill_row(parsed, files=names, source="upload")
    return _store_skill(row, entries)


def list_skills(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    with _lock:
        rows = list(_load("skills")["skills"])
    if not include_disabled:
        rows = [row for row in rows if row.get("status") == "enabled"]
    return [_public(row, kind="skills") for row in
            sorted(rows, key=lambda item: item.get("updated_at", 0), reverse=True)]


def get_skill(skill_id: str) -> dict[str, Any]:
    with _lock:
        row = _find(_load("skills")["skills"], skill_id, kind="skills")
        return _public(row, kind="skills")


def update_skill(skill_id: str, **changes: Any) -> dict[str, Any]:
    allowed = {"name", "description", "trigger", "steps", "tools", "parameters", "examples"}
    unknown = set(changes) - allowed
    if unknown:
        raise CatalogError("Skill 包含未知字段", code="unknown_field")
    with _lock:
        data = _load("skills")
        row = _find(data["skills"], skill_id, kind="skills")
        if "name" in changes:
            row["name"] = _text(changes["name"], "Skill 名称", limit=_MAX_NAME, required=True)
        if "description" in changes:
            row["description"] = _text(changes["description"], "Skill 描述", limit=_MAX_DESCRIPTION)
        if "trigger" in changes:
            row["trigger"] = _text(changes["trigger"], "触发条件", limit=1000)
        if "steps" in changes:
            row["steps"] = _as_list(changes["steps"], limit=_MAX_STEPS, field="步骤")
        if "tools" in changes:
            row["tools"] = _validate_tools(changes["tools"] or [])
        if "parameters" in changes:
            if not isinstance(changes["parameters"], dict):
                raise CatalogError("Skill 参数必须是对象", code="invalid_parameters")
            _reject_secret_keys(changes["parameters"])
            row["parameters"] = _json_safe(changes["parameters"])
        if "examples" in changes:
            row["examples"] = _as_list(changes["examples"], limit=20, field="示例")
        row["status"] = "draft" if row.get("status") == "enabled" else row.get("status", "draft")
        row["updated_at"] = _now()
        _save("skills", data)
        return _public(row, kind="skills")


def _set_skill_status(skill_id: str, status: str, *, confirm: bool = False) -> dict[str, Any]:
    if status == "enabled" and not confirm:
        raise CatalogError("启用 Skill 前必须明确确认草稿内容", code="confirmation_required", status=409)
    with _lock:
        data = _load("skills")
        row = _find(data["skills"], skill_id, kind="skills")
        row["status"] = status
        row["updated_at"] = _now()
        _save("skills", data)
        return _public(row, kind="skills")


def enable_skill(skill_id: str, *, confirm: bool = False) -> dict[str, Any]:
    return _set_skill_status(skill_id, "enabled", confirm=confirm)


def disable_skill(skill_id: str) -> dict[str, Any]:
    return _set_skill_status(skill_id, "disabled", confirm=True)


def delete_skill(skill_id: str) -> bool:
    skill_id = _id(skill_id)
    with _lock:
        data = _load("skills")
        before = len(data["skills"])
        data["skills"] = [row for row in data["skills"] if str(row.get("id")) != skill_id]
        if len(data["skills"]) == before:
            raise CatalogError("Agent 资产不存在", code="not_found", status=404)
        _save("skills", data)
    shutil.rmtree((_asset_root("skills") / skill_id).resolve(), ignore_errors=True)
    return True


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------


def _zip_entries(raw: bytes, *, kind: str) -> list[tuple[str, bytes]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile) as exc:
        raise CatalogError("ZIP 文件损坏", code="invalid_archive") from exc
    entries: list[tuple[str, bytes]] = []
    total = 0
    try:
        infos = archive.infolist()
        max_members = _MAX_TEMPLATE_ZIP_MEMBERS if kind == "template" else _MAX_SKILL_FILES
        if not infos:
            raise CatalogError("ZIP 文件为空", code="empty_archive")
        if len(infos) > max_members:
            raise CatalogError("ZIP 内文件数量过多", code="too_many_files")
        for info in infos:
            rel = _safe_member_name(info.filename)
            if rel is None:
                raise CatalogError("ZIP 成员路径无效", code="path_traversal")
            if any(existing == rel for existing, _data in entries):
                raise CatalogError(f"ZIP 内存在重复文件名：{rel}", code="duplicate_member")
            mode = (int(info.external_attr) >> 16) & 0o170000
            if mode == 0o120000:
                raise CatalogError("ZIP 不能包含符号链接", code="symlink_file")
            if info.is_dir():
                continue
            suffix = Path(rel).suffix.lower()
            if kind == "skill":
                if _is_executable_name(rel):
                    raise CatalogError(f"Skill 禁止包含可执行文件：{rel}", code="executable_file")
                if suffix not in _SKILL_ALLOWED_SUFFIXES and Path(rel).name.lower() not in {"skill", "readme"}:
                    raise CatalogError(f"Skill 文件类型不受支持：{rel}", code="unsupported_file")
                max_size = _MAX_SKILL_FILE_BYTES
            else:
                if _is_executable_name(rel) or suffix not in _TEMPLATE_RESOURCE_SUFFIXES:
                    raise CatalogError(f"模板资源类型不受支持：{rel}", code="unsupported_file")
                max_size = _MAX_TEMPLATE_ZIP_UNPACKED
            declared = int(info.file_size)
            compressed = max(1, int(info.compress_size))
            if declared < 0 or declared > max_size or declared > compressed * _MAX_TEMPLATE_ZIP_RATIO:
                raise CatalogError(f"ZIP 成员大小或压缩比异常：{rel}", code="member_too_large")
            total += declared
            if total > (_MAX_SKILL_TOTAL_BYTES if kind == "skill" else _MAX_TEMPLATE_ZIP_UNPACKED):
                raise CatalogError("ZIP 解压总量超出限制", code="archive_too_large")
            try:
                data = archive.read(info)
            except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
                raise CatalogError(f"ZIP 成员读取失败：{rel}", code="member_read_failed") from exc
            if len(data) != declared:
                raise CatalogError(f"ZIP 成员读取失败：{rel}", code="member_size_mismatch")
            if kind == "skill" and (b"\x00" in data or data.startswith((b"#!", b"MZ"))):
                raise CatalogError(f"Skill 文件疑似可执行内容：{rel}", code="executable_file")
            entries.append((rel, data))
    finally:
        archive.close()
    if not entries:
        raise CatalogError("ZIP 没有可用文件", code="empty_archive")
    return entries


def _read_template_source(source: Any) -> tuple[str, bytes]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            raise CatalogError("模板来源不存在", code="source_not_found", status=404)
        name, data = path.name, path.read_bytes()
    elif isinstance(source, (tuple, list)) and len(source) == 2:
        name, value = source
        if isinstance(value, bytes):
            data = value
        elif isinstance(value, bytearray):
            data = bytes(value)
        elif hasattr(value, "read"):
            try:
                data = value.read(_MAX_TEMPLATE_BYTES + 1)
            except OSError as exc:
                raise CatalogError("读取模板上传文件失败", code="read_failed") from exc
        else:
            raise CatalogError("模板上传内容无效", code="invalid_upload")
    else:
        name = getattr(source, "filename", "")
        stream = getattr(source, "stream", source)
        if not name or not hasattr(stream, "read"):
            raise CatalogError("模板上传内容无效", code="invalid_upload")
        try:
            data = stream.read(_MAX_TEMPLATE_BYTES + 1)
        except OSError as exc:
            raise CatalogError("读取模板上传文件失败", code="read_failed") from exc
    safe = _safe_member_name(name, allow_root=True)
    if not safe:
        raise CatalogError("模板文件名无效", code="path_traversal")
    # 单文件上传允许路径中带目录，但只登记 basename，避免把客户端目录写入元数据。
    safe = Path(safe).name
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif not isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data)
        except (TypeError, ValueError) as exc:
            raise CatalogError("模板上传内容无效", code="invalid_upload") from exc
    data = bytes(data)
    if not data or len(data) > _MAX_TEMPLATE_BYTES:
        raise CatalogError("模板文件为空或过大", code="file_too_large")
    return safe, data


def _extract_fields(tex: str) -> list[str]:
    found: set[str] = set()
    patterns = (
        r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}",
        r"\$\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\$?",
        r"\$([A-Za-z_][A-Za-z0-9_.-]*)\$",
        r"\$if\(\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\)\$",
        r"\$for\(\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\)\$",
        r"\{%\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*%\}",
    )
    for pattern in patterns:
        found.update(re.findall(pattern, tex))
    # Pandoc 的控制标记不是可供用户填写的字段。
    found.difference_update({"if", "endif", "for", "endfor", "else", "sep"})
    # 常用 Pandoc/QuizForge 字段即使只在条件分支中出现，也展示给模板编辑器。
    for field in ("title", "questions", "question", "answer", "answers",
                  "solution", "solutions", "score", "header", "footer",
                  "number", "date", "subject"):
        if re.search(rf"\b{re.escape(field)}\b", tex, flags=re.IGNORECASE):
            found.add(field)
    return sorted(found)


def _pdf_metadata(raw: bytes) -> dict[str, Any]:
    if b"%PDF-" not in raw[:1024]:
        raise CatalogError("文件内容不是有效 PDF", code="invalid_pdf")
    result: dict[str, Any] = {"pages": 0, "page_sizes": [], "text_excerpt": ""}
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=False)
        result["pages"] = len(reader.pages)
        excerpts = []
        for page in reader.pages[:3]:
            try:
                box = page.mediabox
                result["page_sizes"].append({"width": float(box.width), "height": float(box.height)})
                text = page.extract_text() or ""
                if text:
                    excerpts.append(text[:1200])
            except Exception:
                continue
        result["text_excerpt"] = "\n".join(excerpts)[:3000]
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError("PDF 无法解析", code="invalid_pdf") from exc
    return result


def _generated_pdf_tex(meta: dict[str, Any]) -> str:
    title = "从 PDF 样例生成的试卷模板"
    return ("% Agent 根据 PDF 版式样例生成的可编辑草稿\n"
            "% 复杂视觉效果（字体、精确分页、装饰线）需要人工调整。\n"
            "\\documentclass{ctexart}\n\\begin{document}\n"
            f"\\section*{{{title}}}\n"
            "$if(title)$\\textbf{$title$}\\par$endif$\n"
            "$for(questions)$\\textbf{$number$.} $question$\\par$endfor$\n"
            "\\end{document}\n")


def _template_row(name: str, fmt: str, source_file: str, *, description: str = "",
                  fields: list[str] | None = None, default_params: dict | None = None,
                  source_data: bytes = b"", version: str = "1.0.0",
                  pdf_meta: dict | None = None, draft_source: str = "",
                  package_info: dict[str, Any] | None = None,
                  reference_only: bool = False) -> dict[str, Any]:
    _reject_secret_keys(default_params or {})
    now = _now()
    package = dict(package_info or {})
    source_hash = str(package.get("source_hash") or (
        hashlib.sha256(source_data).hexdigest() if source_data else ""))
    validation_status = "reference_only" if reference_only else "pending"
    return {
        "id": _new_id(), "name": _text(name, "模板名称", limit=_MAX_NAME, required=True),
        "description": _text(description, "模板描述", limit=_MAX_DESCRIPTION),
        "format": fmt, "source_file": source_file,
        "schema_version": template_pipeline.CATALOG_SCHEMA,
        "reference_only": reference_only,
        "executable": not reference_only and bool(source_file),
        "manifest": dict(package.get("manifest") or {}),
        "entrypoint": str(package.get("entrypoint") or ""),
        "supported_modes": list(package.get("supported_modes") or []),
        "source_hash": source_hash, "validation_hash": "",
        "fields": list(package.get("fields") or fields or []),
        "default_params": _json_safe(default_params or {}),
        "version": _text(version, "模板版本", limit=40, required=True),
        "status": validation_status, "enabled": False, "selected": False,
        "validation": {
            "status": validation_status,
            "message": ("PDF 仅作为版式参考，不能进入导出编译。"
                        if reference_only else "等待真实编译验证。"),
            "modes": {},
        },
        "preview": {"status": validation_status, "generated_at": now,
                     "rendered": reference_only,
                     "needs_manual_adjustment": reference_only,
                     "sample": pdf_meta or {"bytes": len(source_data)}},
        "pdf_metadata": pdf_meta or {}, "draft_source": draft_source,
        "created_at": now, "updated_at": now,
    }


def _store_template(row: dict[str, Any], files: list[tuple[str, bytes]]) -> dict[str, Any]:
    root = _asset_root("templates").resolve()
    directory = (root / row["id"]).resolve()
    if root not in directory.parents:
        raise CatalogError("模板存储路径无效", code="path_invalid", status=500)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        for rel, data in files:
            target = (directory / PurePosixPath(rel)).resolve()
            if target != directory and directory not in target.parents:
                raise CatalogError("模板资源路径越界", code="path_traversal")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
        row["storage_dir"] = str(directory)
        with _lock:
            data = _load("templates")
            data["templates"].append(row)
            _save("templates", data)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return _public(row, kind="templates")


def register_template_upload(source: Any, *, name: str | None = None,
                             description: str = "", version: str = "1.0.0",
                             default_params: dict | None = None) -> dict[str, Any]:
    filename, raw = _read_template_source(source)
    lower = filename.casefold()
    files: list[tuple[str, bytes]]
    if lower.endswith(".tex.zip"):
        files = _zip_entries(raw, kind="template")
        try:
            package_info = template_pipeline.inspect_files(files)
        except template_pipeline.TemplatePipelineError as exc:
            raise CatalogError(str(exc), code=exc.code, status=exc.status) from exc
        fmt = "tex.zip"
        fields = package_info["fields"]
        pdf_meta, draft_source = None, ""
    elif lower.endswith(".tex"):
        if b"\x00" in raw:
            raise CatalogError("TeX 模板包含二进制控制字符", code="invalid_encoding")
        files = template_pipeline.single_tex_package(filename, raw)
        try:
            package_info = template_pipeline.inspect_files(files)
        except template_pipeline.TemplatePipelineError as exc:
            raise CatalogError(str(exc), code=exc.code, status=exc.status) from exc
        fmt, fields, pdf_meta, draft_source = (
            "tex", package_info["fields"], None, "")
    elif lower.endswith(".pdf"):
        pdf_meta = _pdf_metadata(raw)
        fields, draft_source, package_info = [], "", {}
        files = [(filename, raw)]
        fmt = "pdf"
    else:
        raise CatalogError("模板只支持 .tex、.tex.zip 或 .pdf", code="unsupported_file")
    title = name or Path(filename).name
    if title.casefold().endswith(".tex.zip"):
        title = title[:-8]
    else:
        title = Path(title).stem
    source_file = (str(package_info.get("entrypoint") or filename)
                   if fmt != "pdf" else filename)
    row = _template_row(title, fmt, source_file, description=description,
                        fields=fields, default_params=default_params,
                        source_data=raw, version=version, pdf_meta=pdf_meta,
                        draft_source=draft_source, package_info=package_info,
                        reference_only=fmt == "pdf")
    return _store_template(row, files)


register_template = register_template_upload


def register_template_metadata(*, name: str, fmt: str = "tex", description: str = "",
                               fields: Iterable[Any] = (), default_params: dict | None = None,
                               version: str = "1.0.0") -> dict[str, Any]:
    """无文件时登记一个待编辑模板草稿，供 Agent 先生成后再上传资源。"""
    fmt = _text(fmt, "模板格式", limit=20, required=True).lower()
    if fmt not in {"tex", "tex.zip", "pdf"}:
        raise CatalogError("模板格式无效", code="invalid_format")
    row = _template_row(name, fmt, "", description=description,
                        fields=_as_list(fields, limit=100, field="模板字段"),
                        default_params=default_params, version=version,
                        reference_only=fmt == "pdf")
    return _store_template(row, [])


def list_templates(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    with _lock:
        data = _load("templates")
        changed = False
        for row in data["templates"]:
            if row.get("reference_only"):
                continue
            expected = str(row.get("validation_hash") or "")
            validation_status = str((row.get("validation") or {}).get("status") or "")
            requires_valid_hash = bool(expected) or bool(row.get("enabled")) \
                or bool(row.get("selected")) or row.get("status") in {"enabled", "validated"} \
                or validation_status == "valid"
            if not requires_valid_hash:
                continue
            current_hash = ""
            try:
                directory = _template_directory(str(row.get("id") or ""))
                current_hash = template_pipeline.inspect_directory(directory)["source_hash"]
            except (CatalogError, OSError, template_pipeline.TemplatePipelineError):
                current_hash = ""
            if expected and current_hash == expected:
                continue
            validation = dict(row.get("validation") or {})
            validation.update({
                "status": "stale",
                "message": "模板源码已变化或无法读取，需要重新验证。",
            })
            row["validation"] = validation
            row["validation_hash"] = ""
            row["status"] = "stale"
            row["enabled"] = False
            row["selected"] = False
            row["updated_at"] = _now()
            if data.get("active_id") == row.get("id"):
                data["active_id"] = None
            changed = True
        if changed:
            _save("templates", data)
        rows = list(data["templates"])
    if not include_disabled:
        rows = [row for row in rows
                if row.get("status") == "enabled" and row.get("enabled")
                and row.get("validation_hash") == row.get("source_hash")]
    return [_public(row, kind="templates") for row in
            sorted(rows, key=lambda item: item.get("updated_at", 0), reverse=True)]


def get_template(template_id: str) -> dict[str, Any]:
    with _lock:
        row = _find(_load("templates")["templates"], template_id, kind="templates")
        return _public(row, kind="templates")


def _template_directory(template_id: str) -> Path:
    template_id = _id(template_id)
    root = _asset_root("templates").resolve()
    directory = (root / template_id).resolve()
    if root not in directory.parents or directory.is_symlink() or not directory.is_dir():
        raise CatalogError("模板资源不存在", code="source_not_found", status=404)
    return directory


def _template_validation_result(template_id: str, *, status: str, message: str,
                                modes: dict[str, Any] | None = None,
                                source_hash: str = "",
                                preview_rendered: bool = False,
                                preview_mode: str = "") -> dict[str, Any]:
    """提交一次验证终态；调用方必须已退出外部编译过程。"""
    with _lock:
        data = _load("templates")
        row = _find(data["templates"], template_id, kind="templates")
        row["validation"] = {
            "status": status,
            "message": message,
            "modes": dict(modes or {}),
            "validated_at": _now(),
        }
        row["validation_hash"] = source_hash if status == "valid" else ""
        row["status"] = "validated" if status == "valid" else status
        row["enabled"] = False
        row["selected"] = False
        if data.get("active_id") == row.get("id"):
            data["active_id"] = None
        preview = dict(row.get("preview") or {})
        preview.update({
            "status": "ready" if preview_rendered else status,
            "rendered": preview_rendered,
            "generated_at": _now(),
            "mode": preview_mode,
        })
        row["preview"] = preview
        row["updated_at"] = _now()
        _save("templates", data)
        return _public(row, kind="templates")


def preview_template(template_id: str, sample: dict | None = None) -> dict[str, Any]:
    """使用产品固定样例真实编译；客户端不能替换样例来缩小验证覆盖面。"""
    template_id = _id(template_id)
    _reject_secret_keys(sample or {})
    with _lock:
        data = _load("templates")
        row = _find(data["templates"], template_id, kind="templates")
        if row.get("reference_only"):
            return _public(row, kind="templates")
        if not row.get("source_file"):
            raise CatalogError("请先上传可执行的 TeX 模板文件",
                               code="missing_file", status=409)
        row["validation"] = {
            "status": "validating", "message": "正在执行固定样例真实编译。",
            "modes": {},
        }
        row["status"], row["enabled"], row["selected"] = (
            "validating", False, False)
        if data.get("active_id") == template_id:
            data["active_id"] = None
        row["updated_at"] = _now()
        _save("templates", data)

    directory = _template_directory(template_id)
    try:
        template_pipeline.ensure_legacy_manifest(directory, row)
        info = template_pipeline.inspect_directory(directory)
        compiled = template_pipeline.compile_preview(directory)
        # 编译结束后重新计算，防止验证期间模板文件被外部程序替换。
        current = template_pipeline.inspect_directory(directory)
        if current["source_hash"] != compiled["source_hash"]:
            raise template_pipeline.TemplatePipelineError(
                "模板在验证过程中发生变化，请重新验证。", code="source_changed",
                status=409)
        template_pipeline.write_preview(directory, compiled)
    except tex_sandbox.TexToolUnavailable as exc:
        return _template_validation_result(
            template_id, status="pending", message=str(exc))
    except template_pipeline.TemplatePipelineError as exc:
        return _template_validation_result(
            template_id, status="failed", message=str(exc),
            modes=(exc.details or {}).get("modes"))
    except tex_sandbox.TexSandboxError as exc:
        return _template_validation_result(
            template_id, status="failed", message=str(exc))
    except (OSError, RuntimeError) as exc:
        logger.warning("模板真实预览失败", exc_info=True)
        return _template_validation_result(
            template_id, status="failed", message=f"模板预览文件处理失败：{exc}")

    with _lock:
        data = _load("templates")
        stored = _find(data["templates"], template_id, kind="templates")
        stored["manifest"] = dict(info["manifest"])
        stored["entrypoint"] = info["entrypoint"]
        stored["source_file"] = info["entrypoint"]
        stored["supported_modes"] = list(info["supported_modes"])
        stored["source_hash"] = info["source_hash"]
        stored["fields"] = list(info["fields"])
        stored["executable"] = True
        stored["schema_version"] = template_pipeline.CATALOG_SCHEMA
        stored["updated_at"] = _now()
        _save("templates", data)
    return _template_validation_result(
        template_id, status="valid", message="全部声明模式已通过固定样例真实编译。",
        modes=compiled["modes"], source_hash=compiled["source_hash"],
        preview_rendered=True, preview_mode=compiled["preview_mode"])


def confirm_template(template_id: str, *, confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise CatalogError("启用模板前必须确认预览结果", code="confirmation_required", status=409)
    with _lock:
        data = _load("templates")
        row = _find(data["templates"], template_id, kind="templates")
        if row.get("reference_only"):
            raise CatalogError("PDF 只能作为版式参考，不能启用为导出模板",
                               code="reference_only", status=409)
        if ((row.get("validation") or {}).get("status") != "valid"
                or not (row.get("preview") or {}).get("rendered")):
            raise CatalogError("模板尚未通过真实编译验证", code="validation_required",
                               status=409)
        expected_hash = str(row.get("validation_hash") or "")
    try:
        info = template_pipeline.inspect_directory(_template_directory(template_id))
    except template_pipeline.TemplatePipelineError as exc:
        raise CatalogError(str(exc), code=exc.code, status=exc.status) from exc
    if not expected_hash or info["source_hash"] != expected_hash:
        _template_validation_result(
            template_id, status="stale", message="模板源码已变化，需要重新验证。")
        raise CatalogError("模板源码已变化，需要重新验证", code="template_stale",
                           status=409)
    with _lock:
        data = _load("templates")
        row = _find(data["templates"], template_id, kind="templates")
        row["status"] = "enabled"
        row["enabled"] = True
        row["updated_at"] = _now()
        _save("templates", data)
        return _public(row, kind="templates")


enable_template = confirm_template


def disable_template(template_id: str) -> dict[str, Any]:
    with _lock:
        data = _load("templates")
        row = _find(data["templates"], template_id, kind="templates")
        row["status"], row["enabled"], row["selected"] = "disabled", False, False
        if data.get("active_id") == row["id"]:
            data["active_id"] = None
        row["updated_at"] = _now()
        _save("templates", data)
        return _public(row, kind="templates")


def select_template(template_id: str) -> dict[str, Any]:
    # 选择前重新核对落盘内容及验证哈希；仅看 JSON 状态会放过外部手改文件。
    template_source_path(template_id, require_enabled=True)
    with _lock:
        data = _load("templates")
        target = _find(data["templates"], template_id, kind="templates")
        if target.get("status") != "enabled":
            raise CatalogError("只能选择已确认启用的模板", code="template_not_enabled", status=409)
        for row in data["templates"]:
            row["selected"] = row.get("id") == target["id"]
            if row.get("id") == target["id"]:
                row["updated_at"] = _now()
        data["active_id"] = target["id"]
        _save("templates", data)
        return _public(target, kind="templates")


def selected_template() -> dict[str, Any] | None:
    with _lock:
        data = _load("templates")
        active = data.get("active_id")
        if not active:
            return None
        row = next((item for item in data["templates"] if item.get("id") == active), None)
        if not row or row.get("status") != "enabled" or not row.get("enabled"):
            return None
        result = _public(row, kind="templates")
    try:
        template_source_path(str(active), require_enabled=True)
    except CatalogError:
        return None
    return result


def template_source_path(template_id: str, *, relative: str | None = None,
                         require_enabled: bool = True,
                         mode: str | None = None) -> Path:
    """供导出服务内部读取模板资源；绝不把该路径直接暴露给 HTTP 客户端。"""
    template_id = _id(template_id)
    with _lock:
        row = _find(_load("templates")["templates"], template_id, kind="templates")
    if row.get("reference_only"):
        raise CatalogError("PDF 只能作为版式参考，不能用于导出编译",
                           code="reference_only", status=409)
    if require_enabled and (row.get("status") != "enabled" or not row.get("enabled")):
        raise CatalogError("模板尚未确认启用", code="template_not_enabled", status=409)
    directory = _template_directory(template_id)
    try:
        template_pipeline.ensure_legacy_manifest(directory, row)
        info = template_pipeline.inspect_directory(directory)
        if mode:
            template_pipeline.ensure_mode(info, mode)
    except template_pipeline.TemplatePipelineError as exc:
        raise CatalogError(str(exc), code=exc.code, status=exc.status) from exc
    expected = str(row.get("validation_hash") or "")
    if require_enabled and (not expected or info["source_hash"] != expected):
        _template_validation_result(
            template_id, status="stale", message="模板源码已变化，需要重新验证。")
        raise CatalogError("模板源码已变化，需要重新验证", code="template_stale",
                           status=409)
    if relative:
        rel = _safe_member_name(relative)
        if not rel:
            raise CatalogError("模板资源路径无效", code="path_traversal")
        target = (directory / PurePosixPath(rel)).resolve()
    else:
        target = Path(info["entrypoint_path"])
    if target != directory and directory not in target.parents:
        raise CatalogError("模板资源路径越界", code="path_traversal")
    if target.is_symlink() or not target.is_file():
        raise CatalogError("模板资源不存在", code="source_not_found", status=404)
    return target


def update_template(template_id: str, **changes: Any) -> dict[str, Any]:
    allowed = {"name", "description", "fields", "default_params", "version"}
    if set(changes) - allowed:
        raise CatalogError("模板包含未知字段", code="unknown_field")
    with _lock:
        data = _load("templates")
        row = _find(data["templates"], template_id, kind="templates")
        if "name" in changes:
            row["name"] = _text(changes["name"], "模板名称", limit=_MAX_NAME, required=True)
        if "description" in changes:
            row["description"] = _text(changes["description"], "模板描述", limit=_MAX_DESCRIPTION)
        if "fields" in changes:
            row["fields"] = _as_list(changes["fields"], limit=100, field="模板字段")
        if "default_params" in changes:
            if not isinstance(changes["default_params"], dict):
                raise CatalogError("模板默认参数必须是对象", code="invalid_parameters")
            _reject_secret_keys(changes["default_params"])
            row["default_params"] = _json_safe(changes["default_params"])
        if "version" in changes:
            row["version"] = _text(changes["version"], "模板版本", limit=40, required=True)
        # schema v2 的有效性绑定整个模板包内容哈希。名称、描述和展示字段只是目录
        # 元数据，不改变已编译内容，因此不应让一次安全验证无故失效。
        row["updated_at"] = _now()
        _save("templates", data)
        return _public(row, kind="templates")


def delete_template(template_id: str) -> bool:
    template_id = _id(template_id)
    with _lock:
        data = _load("templates")
        before = len(data["templates"])
        data["templates"] = [row for row in data["templates"] if str(row.get("id")) != template_id]
        if len(data["templates"]) == before:
            raise CatalogError("Agent 资产不存在", code="not_found", status=404)
        if data.get("active_id") == template_id:
            data["active_id"] = None
        _save("templates", data)
    shutil.rmtree((_asset_root("templates") / template_id).resolve(), ignore_errors=True)
    return True


def create_template_version(template_id: str, source: Any, *, version: str,
                             name: str | None = None,
                             description: str | None = None) -> dict[str, Any]:
    """从已有模板派生一个新版本；新版本仍需单独预览确认。"""
    base = get_template(template_id)
    row = register_template_upload(
        source, name=name or base.get("name"),
        description=base.get("description", "") if description is None else description,
        version=version, default_params=base.get("default_params") or {})
    with _lock:
        data = _load("templates")
        created = _find(data["templates"], row["id"], kind="templates")
        created["parent_id"] = base["id"]
        created["updated_at"] = _now()
        _save("templates", data)
        return _public(created, kind="templates")


def list_template_versions(template_id: str) -> list[dict[str, Any]]:
    template_id = _id(template_id)
    with _lock:
        rows = list(_load("templates")["templates"])
    base = _find(rows, template_id, kind="templates")
    root_id = str(base.get("parent_id") or base.get("id"))
    result = [row for row in rows if str(row.get("id")) == root_id
              or str(row.get("parent_id")) == root_id]
    return [_public(row, kind="templates") for row in
            sorted(result, key=lambda item: item.get("version", ""))]


# ---------------------------------------------------------------------------
# 偏好
# ---------------------------------------------------------------------------


def _validate_workdir(value: Any) -> str:
    raw = _text(value, "默认工作目录", limit=1000)
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute() or (len(raw) > 1 and raw[1] == ":"):
        raise CatalogError("默认工作目录必须是题库内相对路径", code="path_traversal")
    normalized = raw.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} or part.startswith(".")
           or part in {"_assets", "_handouts", "_backups", ".trash"} for part in parts):
        raise CatalogError("默认工作目录无效", code="path_traversal")
    root = Path(config.BANK_DIR).resolve()
    candidate = (root / normalized).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CatalogError("默认工作目录必须位于当前题库内", code="path_traversal") from exc
    return normalized


def _validate_preference(key: Any, value: Any) -> tuple[str, Any]:
    name = _text(key, "偏好名称", limit=80, required=True)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", name):
        raise CatalogError("偏好名称无效", code="invalid_key")
    if any(marker in name.casefold() for marker in _SECRET_FIELD_MARKERS):
        raise CatalogError("偏好中不能保存密钥或凭据", code="secret_field")
    if name in {"default_workdir", "workdir", "working_directory"}:
        return name, _validate_workdir(value)
    if name == "ocr_backend":
        backend = _text(value, "OCR 后端", limit=40, required=True).casefold()
        if backend not in {"mineru", "doc2x", "local", "none"}:
            raise CatalogError("OCR 后端必须是 mineru、doc2x、local 或 none", code="invalid_value")
        return name, backend
    if name in {"template_id", "default_template_id"}:
        if value in (None, ""):
            return name, None
        template_id = _id(value)
        template_source_path(template_id, require_enabled=True)
        return name, template_id
    _reject_secret_keys(value)
    safe = _json_safe(value)
    encoded = json.dumps(safe, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > _MAX_PREFERENCE_BYTES:
        raise CatalogError("偏好内容过大", code="value_too_large")
    return name, safe


def get_preferences() -> dict[str, Any]:
    with _lock:
        stored = _load("preferences")["preferences"]
    result = dict(_DEFAULT_PREFERENCES)
    result.update(stored)
    # 返回深拷贝，调用方修改结果不会污染内存中的缓存（当前虽无缓存，保持契约）。
    return json.loads(json.dumps(result, ensure_ascii=False))


load_preferences = get_preferences


def update_preferences(values: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    if values is not None and not isinstance(values, dict):
        raise CatalogError("偏好必须是对象", code="invalid_preferences")
    patch = dict(values or {})
    patch.update(kwargs)
    if not isinstance(patch, dict):
        raise CatalogError("偏好必须是对象", code="invalid_preferences")
    validated = dict(_validate_preference(key, value) for key, value in patch.items())
    with _lock:
        data = _load("preferences")
        data["preferences"].update(validated)
        _save("preferences", data)
    return get_preferences()


def delete_preferences(keys: Iterable[Any] | Any | None = None) -> dict[str, Any]:
    if keys is None:
        target: list[Any] = list(_DEFAULT_PREFERENCES)
    elif isinstance(keys, str):
        target = [keys]
    else:
        target = list(keys)
    names = [_text(key, "偏好名称", limit=80, required=True) for key in target]
    with _lock:
        data = _load("preferences")
        for name in names:
            data["preferences"].pop(name, None)
        _save("preferences", data)
    return get_preferences()


reset_preferences = delete_preferences


# ---------------------------------------------------------------------------
# Flask API
# ---------------------------------------------------------------------------


bp = Blueprint("agent_catalog", __name__)


def _error(exc: CatalogError):
    return jsonify(ok=False, error=str(exc), code=exc.code), exc.status


def _json_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on", "confirm"}


@bp.route("/api/agent/skills", methods=["GET", "POST"])
def skills_collection():
    if request.method == "GET":
        rows = list_skills()
        return jsonify(ok=True, skills=rows, count=len(rows))
    try:
        files = request.files.getlist("files")
        single = request.files.get("file")
        if single is not None and single not in files:
            files.append(single)
        if files:
            row = import_skill_folder(files, name=request.form.get("name") or None,
                                      description=request.form.get("description"))
        else:
            payload = _json_payload()
            prompt = payload.get("description") or payload.get("prompt")
            if prompt:
                row = generate_skill_draft(prompt, name=payload.get("name"))
            else:
                row = import_skill_folder([], name=payload.get("name"))
        return jsonify(ok=True, skill=row), 201
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/agent/skills/<skill_id>", methods=["GET", "PATCH", "DELETE"])
def skill_detail(skill_id: str):
    try:
        if request.method == "GET":
            return jsonify(ok=True, skill=get_skill(skill_id))
        if request.method == "DELETE":
            delete_skill(skill_id)
            return jsonify(ok=True, deleted=True)
        payload = _json_payload()
        status = str(payload.pop("status", "") or "").casefold()
        if status == "enabled":
            return jsonify(ok=True, skill=enable_skill(skill_id, confirm=_as_bool(payload.get("confirm"))))
        if status == "disabled":
            return jsonify(ok=True, skill=disable_skill(skill_id))
        payload.pop("confirm", None)
        return jsonify(ok=True, skill=update_skill(skill_id, **payload))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/agent/skills/<skill_id>/enable", methods=["POST"])
@bp.route("/api/agent/skills/<skill_id>/confirm", methods=["POST"])
def skill_enable(skill_id: str):
    try:
        payload = _json_payload()
        return jsonify(ok=True, skill=enable_skill(skill_id,
                         confirm=_as_bool(payload.get("confirm"))))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/agent/skills/<skill_id>/disable", methods=["POST"])
def skill_disable(skill_id: str):
    try:
        return jsonify(ok=True, skill=disable_skill(skill_id))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/agent/skills/draft", methods=["POST"])
def skill_draft_alias():
    """自然语言生成草稿的显式别名，兼容设置页和旧版客户端。"""
    try:
        payload = _json_payload()
        return jsonify(ok=True, skill=generate_skill_draft(
            payload.get("description") or payload.get("prompt") or "",
            name=payload.get("name"))), 201
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/agent/skills/import", methods=["POST"])
def skill_import_alias():
    try:
        files = request.files.getlist("files") or request.files.getlist("file")
        return jsonify(ok=True, skill=import_skill_folder(
            files, name=request.form.get("name") or None,
            description=request.form.get("description"))), 201
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates", methods=["GET", "POST"])
@bp.route("/api/agent/templates", methods=["GET", "POST"])
def templates_collection():
    try:
        if request.method == "GET":
            rows = list_templates()
            return jsonify(ok=True, templates=rows, selected=selected_template(), count=len(rows))
        uploaded = request.files.get("file") or request.files.get("template")
        if uploaded is not None:
            row = register_template_upload(
                uploaded, name=request.form.get("name") or None,
                description=request.form.get("description", ""),
                version=request.form.get("version", "1.0.0"),
                default_params={})
        else:
            payload = _json_payload()
            row = register_template_metadata(
                name=payload.get("name", "未命名模板"), fmt=payload.get("format", "tex"),
                description=payload.get("description", ""), fields=payload.get("fields", []),
                default_params=payload.get("default_params") or {},
                version=payload.get("version", "1.0.0"))
        return jsonify(ok=True, template=row), 201
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates/<template_id>", methods=["GET", "PATCH", "DELETE"])
@bp.route("/api/agent/templates/<template_id>", methods=["GET", "PATCH", "DELETE"])
def template_detail(template_id: str):
    try:
        if request.method == "GET":
            return jsonify(ok=True, template=get_template(template_id))
        if request.method == "DELETE":
            delete_template(template_id)
            return jsonify(ok=True, deleted=True)
        payload = _json_payload()
        status = str(payload.pop("status", "") or "").casefold()
        if status == "enabled":
            return jsonify(ok=True, template=confirm_template(template_id, confirm=_as_bool(payload.get("confirm"))))
        if status == "disabled":
            return jsonify(ok=True, template=disable_template(template_id))
        payload.pop("confirm", None)
        return jsonify(ok=True, template=update_template(template_id, **payload))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates/<template_id>/validate", methods=["POST"])
@bp.route("/api/agent/templates/<template_id>/validate", methods=["POST"])
@bp.route("/api/templates/<template_id>/preview", methods=["GET", "POST"])
@bp.route("/api/agent/templates/<template_id>/preview", methods=["GET", "POST"])
def template_preview(template_id: str):
    try:
        if request.method == "GET":
            return jsonify(ok=True, template=get_template(template_id))
        payload = _json_payload()
        return jsonify(ok=True, template=preview_template(template_id, payload.get("sample")))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates/<template_id>/preview/file", methods=["GET"])
@bp.route("/api/agent/templates/<template_id>/preview/file", methods=["GET"])
def template_preview_file(template_id: str):
    try:
        template_id = _id(template_id)
        with _lock:
            row = _find(_load("templates")["templates"], template_id,
                        kind="templates")
        directory = _template_directory(template_id)
        if row.get("reference_only"):
            rel = _safe_member_name(Path(str(row.get("source_file") or "")).name,
                                    allow_root=True)
            target = (directory / str(rel or "")).resolve()
            if (not rel or target.parent != directory or target.is_symlink()
                    or not target.is_file() or target.suffix.casefold() != ".pdf"):
                raise CatalogError("PDF 参考文件不存在", code="source_not_found",
                                   status=404)
        else:
            try:
                target = template_pipeline.preview_path(directory)
            except template_pipeline.TemplatePipelineError as exc:
                raise CatalogError(str(exc), code=exc.code, status=exc.status) from exc
        return send_file(target, mimetype="application/pdf", conditional=True,
                         download_name=f"{template_id}-preview.pdf")
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates/<template_id>/versions", methods=["GET", "POST"])
@bp.route("/api/agent/templates/<template_id>/versions", methods=["GET", "POST"])
def template_versions(template_id: str):
    try:
        if request.method == "GET":
            return jsonify(ok=True, templates=list_template_versions(template_id))
        uploaded = request.files.get("file") or request.files.get("template")
        if uploaded is None:
            raise CatalogError("请选择新版本模板文件", code="missing_file")
        payload = _json_payload()
        # multipart 字段优先，JSON 仅用于测试客户端或无文件扩展场景。
        version = request.form.get("version") or payload.get("version") or "1.0.0"
        row = create_template_version(
            template_id, uploaded, version=version,
            name=request.form.get("name") or None,
            description=request.form.get("description")
            if "description" in request.form else None)
        return jsonify(ok=True, template=row), 201
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates/<template_id>/confirm", methods=["POST"])
@bp.route("/api/templates/<template_id>/enable", methods=["POST"])
@bp.route("/api/agent/templates/<template_id>/confirm", methods=["POST"])
@bp.route("/api/agent/templates/<template_id>/enable", methods=["POST"])
def template_confirm(template_id: str):
    try:
        return jsonify(ok=True, template=confirm_template(
            template_id, confirm=_as_bool(_json_payload().get("confirm"))))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates/<template_id>/disable", methods=["POST"])
@bp.route("/api/agent/templates/<template_id>/disable", methods=["POST"])
def template_disable(template_id: str):
    try:
        return jsonify(ok=True, template=disable_template(template_id))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates/<template_id>/select", methods=["POST"])
@bp.route("/api/agent/templates/<template_id>/select", methods=["POST"])
def template_select(template_id: str):
    try:
        return jsonify(ok=True, template=select_template(template_id))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates/select", methods=["POST"])
@bp.route("/api/agent/templates/select", methods=["POST"])
def template_select_collection():
    try:
        template_id = _json_payload().get("template_id")
        return jsonify(ok=True, template=select_template(template_id))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/templates/upload", methods=["POST"])
@bp.route("/api/agent/templates/upload", methods=["POST"])
def template_upload_alias():
    try:
        uploaded = request.files.get("file") or request.files.get("template")
        if uploaded is None:
            raise CatalogError("请选择模板文件", code="missing_file")
        row = register_template_upload(
            uploaded, name=request.form.get("name") or None,
            description=request.form.get("description", ""),
            version=request.form.get("version", "1.0.0"), default_params={})
        return jsonify(ok=True, template=row), 201
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/agent/preferences", methods=["GET", "PUT", "PATCH", "DELETE"])
def preferences_endpoint():
    try:
        if request.method == "GET":
            return jsonify(ok=True, preferences=get_preferences())
        if request.method == "DELETE":
            payload = _json_payload()
            keys = payload.get("keys")
            if keys is None:
                raw = request.args.get("keys")
                keys = [part for part in raw.split(",") if part] if raw else None
            return jsonify(ok=True, preferences=delete_preferences(keys))
        payload = _json_payload()
        if request.method == "PUT":
            # PUT 仍采用合并语义，避免设置页只提交一项时意外清空其它偏好。
            payload = payload.get("preferences", payload)
        return jsonify(ok=True, preferences=update_preferences(payload))
    except CatalogError as exc:
        return _error(exc)


@bp.route("/api/agent/preferences/<key>", methods=["GET", "PUT", "PATCH", "DELETE"])
def preference_detail(key: str):
    """单项偏好 REST 入口，便于设置页做局部编辑。"""
    try:
        name = _text(key, "偏好名称", limit=80, required=True)
        if request.method == "GET":
            values = get_preferences()
            if name not in values:
                raise CatalogError("偏好不存在", code="not_found", status=404)
            return jsonify(ok=True, key=name, value=values[name])
        if request.method == "DELETE":
            return jsonify(ok=True, preferences=delete_preferences([name]))
        payload = _json_payload()
        value = payload.get("value", payload.get(name))
        return jsonify(ok=True, preferences=update_preferences({name: value}))
    except CatalogError as exc:
        return _error(exc)


def register_agent_catalog(app) -> None:
    """将 API 注册到现有 Flask 应用；供 app.py 在创建 app 后调用。"""
    if "agent_catalog" not in app.blueprints:
        app.register_blueprint(bp)


__all__ = [
    "CatalogError", "bp", "register_agent_catalog",
    "generate_skill_draft", "create_skill_draft", "import_skill_folder",
    "list_skills", "get_skill", "update_skill", "enable_skill", "disable_skill",
    "delete_skill", "register_template_upload", "register_template",
    "register_template_metadata", "list_templates", "get_template",
    "preview_template", "confirm_template", "enable_template", "disable_template",
    "select_template", "selected_template", "template_source_path",
    "update_template", "delete_template",
    "create_template_version", "list_template_versions",
    "get_preferences", "load_preferences", "update_preferences", "delete_preferences",
    "reset_preferences", "DEFAULT_PREFERENCES",
]
