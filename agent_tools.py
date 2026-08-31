"""Agent 可调用的题库工具。

这里是业务工具边界：模型只能通过这些函数读取题库，不能直接执行本机命令。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import config
import filestore
import agent_actions
import dedup

_RESERVED_DIRS = {"_assets", "_handouts", "_backups", ".trash"}


class ToolError(ValueError):
    pass


TOOLS = [
    {"name": "list_folders", "description": "列出当前题库目录树", "parameters": {"type": "object", "properties": {}}},
    {"name": "browse_quizforge", "description": "只读浏览 QuizForge 的题库、资料库、回收站、图片附件、讲义和识别历史；可读取文本文件摘要", "parameters": {"type": "object", "properties": {"area": {"type": "string", "enum": ["bank", "library", "trash", "assets", "handouts", "history"]}, "path": {"type": "string"}, "read_text": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "search_questions", "description": "按关键词搜索题目", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "folder": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "read_question", "description": "读取一道题目的完整内容", "parameters": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    {"name": "check_duplicates", "description": "检查当前工作目录内的完全重复和相似题目", "parameters": {"type": "object", "properties": {"folder": {"type": "string"}, "query": {"type": "string"}, "threshold": {"type": "number", "minimum": 0.5, "maximum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}}},
    {"name": "create_question", "description": "新建一道题（标准模式需要确认）", "parameters": {"type": "object", "required": ["body"], "properties": {"body": {"type": "string"}, "solution": {"type": "string"}, "type": {"type": "string"}, "folder": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}, "title": {"type": "string"}, "number": {"type": "integer"}}}},
    {"name": "update_question", "description": "修改题目（标准模式需要确认）", "parameters": {"type": "object", "required": ["id", "body"], "properties": {"id": {"type": "string"}, "body": {"type": "string"}, "solution": {"type": "string"}, "type": {"type": "string"}, "difficulty": {"type": "string"}, "source": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}, "note": {"type": "string"}}}},
    {"name": "rename_question", "description": "重命名题目（标准模式需要确认）", "parameters": {"type": "object", "required": ["id", "new_title"], "properties": {"id": {"type": "string"}, "new_title": {"type": "string"}}}},
    {"name": "move_questions", "description": "移动题目到题库目录（标准模式需要确认）", "parameters": {"type": "object", "required": ["ids", "folder"], "properties": {"ids": {"type": "array", "items": {"type": "string"}}, "folder": {"type": "string"}}}},
    {"name": "create_folder", "description": "新建题库目录（标准模式需要确认）", "parameters": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "parent": {"type": "string"}}}},
    {"name": "tag_questions", "description": "批量追加题目标签（标准模式需要确认）", "parameters": {"type": "object", "required": ["ids", "tags"], "properties": {"ids": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "delete_questions", "description": "将题目移入回收站（标准模式需要确认）", "parameters": {"type": "object", "required": ["ids"], "properties": {"ids": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "restore_question", "description": "从回收站恢复题目（标准模式需要确认）", "parameters": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    {"name": "inspect_conversion", "description": "查看识别任务状态和可导入题目预览", "parameters": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}}},
    {"name": "start_conversion", "description": "在用户明确选择识别后端、导入方式和规范化方式后，启动已安全暂存文件的 OCR 识别任务；缺少选择时必须先询问用户", "parameters": {"type": "object", "required": ["stage_id"], "properties": {"stage_id": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}, "solution": {"type": "string"}, "ocr_backend": {"type": "string", "enum": ["mineru", "doc2x"]}, "engine": {"type": "string", "enum": ["block", "whole"]}, "normalization_mode": {"type": "string", "enum": ["mechanical", "llm", "review"]}, "include_solution": {"type": "boolean"}, "folder": {"type": "string"}}}},
    {"name": "import_conversion", "description": "把识别结果导入题库（标准模式需要确认）", "parameters": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}, "folder": {"type": "string"}, "include_solution": {"type": "boolean"}}}},
    {"name": "export_questions", "description": "筛选并导出 PDF、TeX 或 ZIP（默认直接执行；output_dir 为空时输出到当前工作目录）", "parameters": {"type": "object", "properties": {"folder": {"type": "string"}, "output_dir": {"type": "string"}, "query": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}, "format": {"type": "string", "enum": ["pdf", "tex", "zip"]}, "title": {"type": "string"}, "template_id": {"type": "string"}}}},
    {"name": "execute_command", "description": "在当前 Agent 选定的输入目录和题库目录范围内执行 PowerShell、CMD 或 Python 命令。可读写的文件路径必须位于允许目录内；标准模式下每条命令都需要确认，只有当前页面显式武装的危险模式可直接执行。禁止访问目录范围外的路径。", "parameters": {"type": "object", "required": ["command"], "properties": {"command": {"type": "string", "minLength": 1}, "cwd": {"type": "string", "description": "允许目录内的相对路径，默认使用题库联动目录"}, "language": {"type": "string", "enum": ["powershell", "cmd", "python"]}, "timeout": {"type": "integer", "minimum": 1, "maximum": 300}}}},
]

# 需要依赖 Flask 任务表或导出器的动作由 app 在完成初始化后注册；模块本身仍可
# 独立做离线单元测试。回调只接收规范化后的参数和公开会话快照。
_services: dict[str, Any] = {}


def register_service(name: str, callback) -> None:
    if not str(name or "").strip() or not callable(callback):
        raise ValueError("Agent service 注册参数无效")
    _services[str(name).strip()] = callback


def execute_service(name: str, args: dict | None, *, session: dict,
                    approved_execute: bool = False) -> dict:
    """供审批层执行已批准的任务服务。

    ``approved_execute`` 是进程内控制位，不能由 HTTP 参数伪造。普通工具调用
    会主动丢弃同名字段，只有审批路由显式传入该关键字时才交给服务回调。
    """
    clean = dict(args) if isinstance(args, dict) else {}
    clean.pop("_approved_execute", None)
    if approved_execute:
        clean["_approved_execute"] = True
    return _service_call(str(name), clean, session)


def _json_value(value: Any, *, limit: int = 12000):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_value(v, limit=limit) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(v, limit=limit) for v in value]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "\n…（内容已截断）"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _check_folder(folder: str) -> str:
    raw = str(folder or "").strip()
    # 工具参数始终是题库根下的相对 id；绝对路径即使拼接后仍落在根下，
    # 也不应该被当作合法目录，避免模型把本机路径误当成题库目录。
    if (raw.startswith(("/", "\\"))
            or (len(raw) >= 2 and raw[1] == ":")):
        raise ToolError("目录参数必须是题库内的相对路径")
    folder = raw.replace("\\", "/").strip("/")
    if any(part in {"", ".", ".."} or part.startswith(".") or part in _RESERVED_DIRS
               for part in folder.split("/") if part):
        raise ToolError("目录参数无效")
    root = config.BANK_DIR.resolve()
    path = (root / folder).resolve()
    if path != root and not path.is_relative_to(root):
        raise ToolError("目录必须位于当前题库内")
    return folder


def _session_folder(session: dict | None) -> str:
    """读取并校验会话绑定目录；空串表示题库根目录。"""
    if not session:
        return ""
    return _check_folder(str(session.get("workdir_id", "") or ""))


def _folder_in_scope(folder: str, bound: str) -> bool:
    return not bound or folder == bound or folder.startswith(bound + "/")


def _restrict_tree(nodes: list[dict], bound: str) -> list[dict]:
    """裁剪目录树，只保留绑定目录及其祖先/后代，不泄露同级目录。"""
    if not bound:
        return nodes
    result = []
    for node in nodes or []:
        node_id = str(node.get("id", "")).strip("/")
        children = _restrict_tree(node.get("children") or [], bound)
        if node_id == bound or bound.startswith(node_id + "/"):
            clone = dict(node)
            clone["children"] = children if node_id != bound else node.get("children") or []
            result.append(clone)
    return result


def _summary(record: dict) -> dict:
    keys = ("id", "name", "title", "folder", "type", "difficulty", "tags", "starred", "path")
    return {key: _json_value(record.get(key)) for key in keys if key in record}


def _public_question(record: dict) -> dict:
    """去掉内部绝对路径、缓存和未知对象后再交给模型。"""
    allowed = {
        "id", "title", "name", "folder", "type", "difficulty", "source",
        "tags", "starred", "number", "body", "solution", "note",
        "extra_sections", "img_split", "img_layouts", "sol_img_split",
        "sol_img_layouts",
    }
    return {key: _json_value(value) for key, value in record.items()
            if key in allowed}


_BROWSE_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".tex", ".sty", ".cls"}

_WINDOWS_ABSOLUTE_RE = re.compile(r"(?i)(?:[a-z]:[\\/][^\s'\"`;,|&]+|\\\\[^\s'\"`;,|&]+)")
_DANGEROUS_COMMAND_RE = re.compile(
    r"(?i)(?:\b(remove-item|del|erase|rd|rmdir|format|move-item|move|rename-item|ren|"
    r"set-content|add-content|out-file|clear-content|copy-item|copy|mkdir|md|new-item|"
    r"touch|truncate|chmod|python(?:\.exe)?\s+-c)\b|(?:>>?|\|\s*set-content))"
)


def _command_roots(session: dict | None) -> list[Path]:
    """返回会话允许访问的目录，所有路径均解析到题库根目录下。"""
    root = Path(config.BANK_DIR).expanduser().resolve()
    values = [session.get("input_dir_id"), session.get("workdir_id"),
              session.get("output_dir_id")] if session else [""]
    roots: list[Path] = []
    for value in values:
        folder = _check_folder(str(value or ""))
        candidate = (root / folder).resolve()
        if candidate != root and root not in candidate.parents:
            raise ToolError("Agent 工作目录必须位于当前题库根目录内")
        if candidate not in roots:
            roots.append(candidate)
    return roots


def _path_allowed(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _validate_command_paths(command: str, roots: list[Path]) -> None:
    if "\x00" in command:
        raise ToolError("命令包含无效字符")
    if re.search(r"(?i)(?:^|[\\/\s'\"])(?:\.\.[\\/]|\.\.$)", command):
        raise ToolError("命令不能使用目录穿越路径 ..")
    for match in _WINDOWS_ABSOLUTE_RE.findall(command):
        raw = match.rstrip(".,)")
        candidate = Path(raw).expanduser().resolve()
        if not _path_allowed(candidate, roots):
            raise ToolError("命令引用了允许工作目录之外的路径")


def _command_plan(args: dict, session: dict | None) -> dict:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ToolError("命令不能为空")
    if len(command) > 20000:
        raise ToolError("命令长度不能超过 20000 个字符")
    language = str(args.get("language") or "powershell").strip().lower()
    if language not in {"powershell", "cmd", "python"}:
        raise ToolError("命令语言必须是 powershell、cmd 或 python")
    try:
        timeout = max(1, min(300, int(args.get("timeout", 120))))
    except (TypeError, ValueError):
        timeout = 120
    roots = _command_roots(session)
    root = Path(config.BANK_DIR).expanduser().resolve()
    requested_cwd = str(args.get("cwd") or (session or {}).get("workdir_id") or "").strip()
    cwd_folder = _check_folder(requested_cwd)
    cwd = (root / cwd_folder).resolve()
    if not _path_allowed(cwd, roots):
        raise ToolError("命令工作目录必须位于当前 Agent 允许目录内")
    if cwd.is_symlink() or not cwd.exists() or not cwd.is_dir():
        if cwd.is_symlink():
            raise ToolError("命令工作目录不能是符号链接")
        raise ToolError("命令工作目录不存在")
    _validate_command_paths(command, roots)
    destructive = bool(_DANGEROUS_COMMAND_RE.search(command))
    return {"command": command, "language": language, "timeout": timeout,
            "cwd": cwd, "cwd_folder": cwd_folder, "destructive": destructive}


def _execute_command(args: dict, session: dict | None, *, approved_execute: bool = False) -> dict:
    plan = _command_plan(args, session)
    if plan["destructive"] and not approved_execute:
        raise ToolError("破坏性命令必须经过 Agent 审批")
    command = plan["command"]
    if plan["language"] == "powershell":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    elif plan["language"] == "cmd":
        argv = ["cmd.exe", "/d", "/s", "/c", command]
    else:
        argv = [sys.executable, "-c", command]
    env = os.environ.copy()
    env["QUIZFORGE_AGENT_WORKDIR"] = str(plan["cwd"])
    try:
        completed = subprocess.run(
            argv, cwd=str(plan["cwd"]), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=plan["timeout"], check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "timeout": True, "returncode": None,
                "stdout": str(exc.stdout or "")[-12000:],
                "stderr": str(exc.stderr or "")[-12000:],
                "message": f"命令超过 {plan['timeout']} 秒，已终止等待"}
    return {"ok": completed.returncode == 0, "returncode": completed.returncode,
            "stdout": completed.stdout[-12000:], "stderr": completed.stderr[-12000:],
            "cwd": plan["cwd_folder"], "language": plan["language"],
            "destructive": plan["destructive"]}


def _execute_command_service(args: dict, session: dict) -> dict:
    approved = bool(args.pop("_approved_execute", False))
    return _execute_command(args, session, approved_execute=approved)


register_service("execute_command", _execute_command_service)


def _browse_root(area: str) -> Path:
    roots = {
        "bank": config.BANK_DIR,
        "library": config.BANK_DIR,
        "trash": config.TRASH_DIR,
        "assets": config.ASSETS_DIR,
        "handouts": config.HANDOUTS_DIR,
        "history": config.HISTORY_DIR,
    }
    try:
        return Path(roots[area]).expanduser().resolve()
    except KeyError as exc:
        raise ToolError("浏览范围必须是 bank、library、trash、assets、handouts 或 history") from exc


def _browse_path(area: str, raw: object) -> tuple[Path, str]:
    root = _browse_root(area)
    value = str(raw or "").strip().replace("\\", "/").strip("/")
    if value and (value.startswith("/") or (len(value) > 1 and value[1] == ":")):
        raise ToolError("浏览路径必须是相对路径")
    parts = tuple(part for part in value.split("/") if part)
    if any(part in {".", ".."} or part.startswith(".") for part in parts):
        raise ToolError("浏览路径包含无效或隐藏目录")
    lexical = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ToolError("不支持浏览符号链接")
    target = lexical.resolve()
    if target != root and root not in target.parents:
        raise ToolError("浏览路径越界")
    if target.is_symlink():
        raise ToolError("不支持浏览符号链接")
    return target, "/".join(parts)


def _browse_quizforge(args: dict, *, session: dict | None) -> dict:
    area = str(args.get("area") or "bank").strip().lower()
    target, rel = _browse_path(area, args.get("path"))
    if not target.exists():
        raise ToolError("浏览目标不存在")
    if area == "bank" and session:
        bound = _session_folder(session)
        if bound and not _folder_in_scope(rel, bound) and rel != bound:
            raise ToolError("浏览路径必须位于当前 Agent 工作目录内")
    limit = max(1, min(200, int(args.get("limit", 100) or 100)))
    read_text = bool(args.get("read_text", False))
    if target.is_file():
        entries = [target]
        truncated = False
    else:
        try:
            all_entries = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
            truncated = len(all_entries) > limit
            entries = all_entries[:limit]
        except OSError as exc:
            raise ToolError(f"读取目录失败：{exc}") from exc
    result = []
    for item in entries:
        try:
            stat = item.stat()
        except OSError:
            continue
        item_rel = "/".join(part for part in (rel, item.name) if part)
        row = {"path": item_rel, "name": item.name,
               "kind": "folder" if item.is_dir() else "file",
               "size": stat.st_size if item.is_file() else None,
               "modified": stat.st_mtime}
        if read_text and item.is_file() and item.suffix.casefold() in _BROWSE_TEXT_SUFFIXES:
            try:
                row["content"] = item.read_text(encoding="utf-8", errors="replace")[:12000]
            except OSError:
                row["content_error"] = "文件无法读取"
        result.append(row)
    return {"area": area, "path": rel, "entries": result, "truncated": truncated}


def _check_duplicates(args: dict, *, bound_folder: str) -> dict:
    """在当前 Agent 范围内查找完全重复和相似题。

    查重结果只返回题目摘要，不把记录中的绝对路径、缓存字段或完整正文
    交给模型。``dedup.find_duplicates`` 本身是纯函数，目录边界和结果裁剪
    留在工具层，避免其它调用方意外扩大扫描范围。
    """
    requested = str(args.get("folder", "") or "").strip()
    folder = _check_folder(requested if requested else bound_folder)
    if not _folder_in_scope(folder, bound_folder):
        raise ToolError("查重目录必须位于当前 Agent 工作目录内")

    query = str(args.get("query", "") or "").strip()
    try:
        threshold = float(args.get("threshold", 0.85))
    except (TypeError, ValueError):
        raise ToolError("相似度阈值必须是 0.5 到 1 之间的数字")
    if threshold < 0.5 or threshold > 1:
        raise ToolError("相似度阈值必须是 0.5 到 1 之间的数字")
    try:
        limit = int(args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    if limit < 1 or limit > 100:
        raise ToolError("查重结果数量必须在 1 到 100 之间")

    try:
        records = (filestore.collection_records_snapshot(folder)
                   if folder else filestore.all_records_snapshot())
        if query:
            records = filestore.list_questions(
                records=list(records), search=query, sort="custom")
    except Exception as exc:
        raise ToolError(f"读取查重范围失败：{exc}") from exc

    # 对单次 Agent 工具调用设置硬上限，避免误把超大题库交给 O(n²) 相似度
    # 算法。完全重复仍可以由调用方按目录或关键词分批检查。
    max_items = 5000
    scanned_records = list(records)
    truncated = len(scanned_records) > max_items
    if truncated:
        scanned_records = scanned_records[:max_items]
    items = []
    for row in scanned_records:
        if not isinstance(row, dict):
            continue
        qid = str(row.get("id") or "").strip()
        body = row.get("body")
        if not qid or not isinstance(body, str) or not body.strip():
            continue
        # 只传 dedup 所需的字段，避免算法结果对象携带完整 Markdown 记录。
        items.append({"id": qid, "body": body,
                      "fingerprint": row.get("fingerprint")})
    try:
        groups = dedup.find_duplicates(items, threshold=threshold)
    except Exception as exc:
        raise ToolError(f"查重失败：{exc}") from exc

    public_groups = []
    for group in groups[:limit]:
        members = []
        for member in group.get("members") or []:
            qid = str(member.get("id") or "").strip()
            if not qid:
                continue
            # 从已扫描记录中取摘要；不信任 dedup 回传对象中的额外字段。
            source = next((row for row in scanned_records
                           if str(row.get("id") or "") == qid), None)
            member_summary = _summary(source or {"id": qid})
            # 查重结果只需要题库内的目录信息；即使测试替身或旧缓存带入
            # 绝对路径，也不能把本机文件系统位置交给模型。
            member_summary.pop("path", None)
            members.append(member_summary)
        if len(members) >= 2:
            public_groups.append({
                "kind": str(group.get("kind") or "similar"),
                "score": round(float(group.get("score") or 0), 3),
                "members": members,
            })
    return {
        "folder": folder,
        "query": query,
        "threshold": threshold,
        "scanned": len(items),
        "total_groups": len(groups),
        "shown_groups": len(public_groups),
        "truncated": truncated or len(groups) > limit,
        "groups": public_groups,
    }


def _service_call(name: str, args: dict, session: dict) -> dict:
    callback = _services.get(name)
    if callback is None:
        raise ToolError(f"Agent 服务暂不可用：{name}")
    try:
        result = callback(args, session)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"{name} 失败：{str(exc)[:320]}") from exc
    if not isinstance(result, dict):
        raise ToolError(f"{name} 返回了无效结果")
    return result


def dispatch(name: str, args: dict | None = None, *, session: dict | None = None,
             approval_store=None) -> dict:
    args = args or {}
    if session and session.get("scope") == "chat":
        raise ToolError("当前会话为仅聊天模式，不能访问题库")
    bound_folder = _session_folder(session)
    if name == "list_folders":
        tree = filestore.list_navigation_tree(active_id=bound_folder)
        return {"folders": _json_value(_restrict_tree(tree, bound_folder))}
    if name == "browse_quizforge":
        return _browse_quizforge(args, session=session)
    if name == "search_questions":
        query = str(args.get("query", "")).strip()
        if not query:
            raise ToolError("搜索关键词不能为空")
        requested = str(args.get("folder", "") or "").strip()
        folder = _check_folder(requested if requested else bound_folder)
        if not _folder_in_scope(folder, bound_folder):
            raise ToolError("搜索目录必须位于当前 Agent 工作目录内")
        try:
            records = filestore.collection_records_snapshot(folder) if folder else filestore.all_records_snapshot()
            rows = filestore.list_questions(records=records, search=query, sort="custom")
        except Exception as exc:
            raise ToolError(f"搜索失败：{exc}") from exc
        try:
            limit = max(1, min(100, int(args.get("limit", 20))))
        except (TypeError, ValueError):
            limit = 20
        return {"query": query, "total": len(rows), "questions": [_summary(row) for row in rows[:limit]]}
    if name == "read_question":
        qid = str(args.get("id", "")).strip()
        if not qid:
            raise ToolError("题目 id 不能为空")
        row = filestore.get_question(qid)
        if row is None:
            raise ToolError("未找到这道题")
        if bound_folder:
            row_folder = _check_folder(str(row.get("folder", "") or ""))
            if not _folder_in_scope(row_folder, bound_folder):
                raise ToolError("这道题不在当前 Agent 工作目录内")
        return {"question": _public_question(row)}

    if name == "check_duplicates":
        return _check_duplicates(args, bound_folder=bound_folder)

    if name == "execute_command":
        plan = _command_plan(args, session or {})
        # 本机命令即使被静态判断为只读，也可能通过别名、脚本或子进程产生
        # 副作用。标准模式统一逐次确认；只有当前页面显式武装的危险模式跳过。
        if str((session or {}).get("mode") or "standard") != "danger":
            if approval_store is not None:
                approval = approval_store.create(
                    session, "execute_command",
                    f"在“{plan['cwd_folder'] or '题库根目录'}”执行 {plan['language']} 命令",
                    {"command": plan["command"], "cwd": plan["cwd_folder"],
                     "language": plan["language"], "timeout": plan["timeout"]})
                return {"ok": True, "pending_confirmation": True,
                        "approval": approval,
                        "message": "本机命令需要逐次确认，已生成审批卡片。"}
            return {"ok": True, "pending_confirmation": True,
                    "plan": {"action": "execute_command",
                             "summary": "执行本机命令",
                             "arguments": {"command": plan["command"],
                                           "cwd": plan["cwd_folder"],
                                           "language": plan["language"],
                                           "timeout": plan["timeout"]}},
                    "message": "本机命令需要逐次确认。"}
        return _execute_command(args, session, approved_execute=True)

    # 这些动作都先在 agent_actions 中重新校验，不能因为模型传入了额外字段
    # 就绕过当前目录边界。标准模式返回确认卡片；危险模式才落盘。
    if agent_actions.is_write_action(name):
        try:
            plan = agent_actions.plan_action(name, args, session=session or {})
            if str((session or {}).get("mode") or "standard") == "danger":
                result = agent_actions.execute_action(
                    plan["name"], plan["arguments"], session=session or {})
                return {"ok": True, "executed": True, "result": _json_value(result)}
            if approval_store is not None:
                approval = approval_store.create(
                    session, plan["name"], plan["summary"], plan["arguments"])
                return {"ok": True, "pending_confirmation": True,
                        "approval": approval, "preview": _json_value(plan["preview"]),
                        "message": "写入操作已生成预览，请确认后执行。"}
            return {"ok": True, "pending_confirmation": True,
                    "plan": {"action": plan["name"], "summary": plan["summary"],
                             "preview": _json_value(plan["preview"]),
                             "arguments": _json_value(plan["arguments"])},
                    "message": "写入操作已生成预览，请确认后执行。"}
        except agent_actions.AgentActionError as exc:
            raise ToolError(str(exc)) from exc

    if name in {"inspect_conversion", "start_conversion", "import_conversion",
                "export_questions"}:
        if session and session.get("scope") == "chat":
            raise ToolError("当前会话为仅聊天模式，不能操作题库任务")
        # 客户端即使传入内部审批标记，也必须在这里被剥掉。
        safe_args = dict(args)
        safe_args.pop("_approved_execute", None)
        result = _service_call(name, safe_args, session or {})
        # 导入属于写入服务：服务回调可以返回 pending_confirmation；导出则直接返回
        # 取件地址。这样模型工具循环不需要知道 Flask 任务表的内部结构。
        return _json_value(result)

    raise ToolError(f"未注册的 Agent 工具：{name}")
