"""QuizForge Agent 的写入计划与执行边界。

模型不能直接拿到文件系统函数。所有写入先经过这里做参数收敛、题库范围
检查和可读预览；标准模式由上层创建确认项，危险模式才允许立即执行。
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

import config
import filestore


class AgentActionError(ValueError):
    """Agent 写入参数或执行结果无效。"""


_RESERVED = {"_assets", "_handouts", "_backups", ".trash"}
_MAX_BODY = 2 * 1024 * 1024
_MAX_TAG_LENGTH = 80
_ID_RE = re.compile(r"^[^\\/\x00]{1,160}$")


def _root() -> Path:
    return config.BANK_DIR.resolve()


def _safe_parts(raw: object, *, allow_empty: bool = True) -> tuple[str, ...]:
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        if allow_empty:
            return ()
        raise AgentActionError("目录不能为空")
    rel = PurePosixPath(value)
    if rel.is_absolute() or (len(value) > 1 and value[1] == ":"):
        raise AgentActionError("目录必须是题库内的相对路径")
    parts = tuple(part for part in rel.parts if part not in ("", "."))
    if (not parts or any(part == ".." or part.startswith(".") or
                         part in _RESERVED for part in parts)):
        raise AgentActionError("目录路径无效或属于系统目录")
    return parts


def _folder(raw: object, session: dict, *, must_exist: bool = True,
            default_bound: bool = True) -> tuple[str, Path]:
    """返回规范化 folder id 和路径，并限制在会话工作目录内。"""
    bound = str(session.get("workdir_id") or "").strip("/")
    requested = str(raw or "").strip().replace("\\", "/").strip("/")
    if not requested and default_bound:
        requested = bound
    parts = _safe_parts(requested)
    folder_id = PurePosixPath(*parts).as_posix() if parts else ""
    if bound and not (folder_id == bound or folder_id.startswith(bound + "/")):
        raise AgentActionError("目标目录必须位于当前 Agent 工作目录内")
    root = _root()
    path = (root / PurePosixPath(folder_id)).resolve() if folder_id else root
    if path != root and root not in path.parents:
        raise AgentActionError("目标目录越过当前题库边界")
    # 逐段拒绝符号链接，避免 resolve 后把一个看似题库内的目录指向外部。
    current = root
    for part in PurePosixPath(folder_id).parts:
        current = current / part
        if current.is_symlink():
            raise AgentActionError("目标目录不能是符号链接")
    if must_exist and not path.is_dir():
        raise AgentActionError("目标目录不存在")
    return folder_id, path


def _session_question(qid: object, session: dict) -> dict:
    value = str(qid or "").strip()
    if not value or not _ID_RE.fullmatch(value):
        raise AgentActionError("题目 id 无效")
    row = filestore.get_question(value)
    if not row:
        raise AgentActionError(f"未找到题目：{value}")
    bound = str(session.get("workdir_id") or "").strip("/")
    folder = str(row.get("folder") or "").strip("/")
    if bound and not (folder == bound or folder.startswith(bound + "/")):
        raise AgentActionError("题目不在当前 Agent 工作目录内")
    return row


def _ids(raw: object, session: dict) -> list[str]:
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(item).strip() for item in raw]
    else:
        values = []
    values = list(dict.fromkeys(item for item in values if item))
    if not values:
        raise AgentActionError("至少选择一道题")
    for qid in values:
        _session_question(qid, session)
    return values


def _text(value: object, label: str, *, required: bool = False,
          limit: int = _MAX_BODY) -> str:
    text = str(value or "")
    if required and not text.strip():
        raise AgentActionError(f"{label}不能为空")
    if len(text) > limit:
        raise AgentActionError(f"{label}过长（上限 {limit} 字符）")
    return text


def _tags(raw: object) -> list[str]:
    values = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(values, (list, tuple, set)):
        values = []
    result = []
    for item in values:
        tag = str(item or "").strip()
        if not tag:
            continue
        if len(tag) > _MAX_TAG_LENGTH or "\n" in tag or "\r" in tag:
            raise AgentActionError("标签包含无效或过长内容")
        result.append(tag)
    result = list(dict.fromkeys(result))
    return result


def _question_preview(row: dict) -> dict:
    body = str(row.get("body") or "")
    return {
        "id": row.get("id"),
        "title": row.get("title") or row.get("name"),
        "folder": row.get("folder") or "",
        "type": row.get("type") or "",
        "body": body[:320] + ("…" if len(body) > 320 else ""),
    }


def plan_action(name: str, args: dict | None, *, session: dict) -> dict:
    """校验并规范化一个写入动作，返回可展示的计划。"""
    args = args if isinstance(args, dict) else {}
    action = str(name or "").strip()
    if session.get("scope") == "chat":
        raise AgentActionError("仅聊天模式不能执行题库写入")

    if action == "create_question":
        folder, _ = _folder(args.get("folder"), session)
        body = _text(args.get("body"), "题干", required=True)
        solution = _text(args.get("solution"), "解析")
        qtype = _text(args.get("type", args.get("qtype")), "题型", limit=80)
        difficulty = _text(args.get("difficulty"), "难度", limit=20)
        source = _text(args.get("source"), "题源", limit=300)
        title = _text(args.get("title"), "题目名称", limit=180)
        number = args.get("number")
        if number not in (None, ""):
            try:
                number = int(number)
            except (TypeError, ValueError) as exc:
                raise AgentActionError("题号必须是整数") from exc
            if number < 0 or number > 100000:
                raise AgentActionError("题号超出范围")
        tags = _tags(args.get("tags"))
        normalized = {"body": body, "solution": solution, "qtype": qtype,
                      "source": source, "difficulty": difficulty, "tags": tags,
                      "folder": folder, "number": number, "title": title}
        return {"name": action, "arguments": normalized,
                "summary": f"在“{folder or '题库根目录'}”新建 1 道题",
                "preview": {"folder": folder, "type": qtype,
                            "title": title or "自动命名", "body": body[:500]}}

    if action == "update_question":
        row = _session_question(args.get("id"), session)
        body = _text(args.get("body", row.get("body", "")), "题干", required=True)
        solution = _text(args.get("solution", row.get("solution", "")), "解析")
        qtype = _text(args.get("type", args.get("qtype", row.get("type", ""))), "题型", limit=80)
        difficulty = _text(args.get("difficulty", row.get("difficulty", "")), "难度", limit=20)
        source = _text(args.get("source", row.get("source", "")), "题源", limit=300)
        tags = _tags(args.get("tags", row.get("tags", [])))
        note = args.get("note", None)
        if note is not None:
            note = _text(note, "备注", limit=10000)
        normalized = {"id": str(row["id"]), "body": body, "solution": solution,
                      "qtype": qtype, "source": source, "difficulty": difficulty,
                      "tags": tags, "note": note}
        return {"name": action, "arguments": normalized,
                "summary": f"修改题目“{row.get('title') or row['id']}”",
                "preview": {"before": _question_preview(row),
                            "after": {"type": qtype, "body": body[:500]}}}

    if action == "rename_question":
        row = _session_question(args.get("id"), session)
        title = _text(args.get("new_title", args.get("title")), "新名称",
                      required=True, limit=180)
        normalized = {"id": str(row["id"]), "new_title": title}
        return {"name": action, "arguments": normalized,
                "summary": f"重命名题目“{row.get('title') or row['id']}”",
                "preview": {"id": row["id"], "from": row.get("title"), "to": title}}

    if action in {"move_questions", "move_question"}:
        values = args.get("ids", args.get("id"))
        ids = _ids(values, session)
        folder, _ = _folder(args.get("folder", args.get("target_folder")), session)
        normalized = {"ids": ids, "folder": folder}
        return {"name": "move_questions", "arguments": normalized,
                "summary": f"移动 {len(ids)} 道题到“{folder or '题库根目录'}”",
                "preview": {"ids": ids, "folder": folder}}

    if action in {"create_folder", "create_collection"}:
        parent, parent_path = _folder(args.get("parent", args.get("folder")), session)
        name = _text(args.get("name"), "文件夹名称", required=True, limit=100).strip()
        if ("/" in name or "\\" in name or name in {".", ".."}
                or name.startswith(".") or name in _RESERVED):
            raise AgentActionError("文件夹名称必须是普通单段名称")
        target = parent_path / name
        if target.exists():
            raise AgentActionError("同名文件夹已存在")
        normalized = {"name": name, "parent": parent}
        return {"name": "create_folder", "arguments": normalized,
                "summary": f"在“{parent or '题库根目录'}”新建文件夹“{name}”",
                "preview": {"path": (f"{parent}/{name}" if parent else name)}}

    if action in {"tag_questions", "add_tags"}:
        ids = _ids(args.get("ids", args.get("id")), session)
        tags = _tags(args.get("tags"))
        if not tags:
            raise AgentActionError("至少提供一个标签")
        normalized = {"ids": ids, "tags": tags}
        return {"name": "tag_questions", "arguments": normalized,
                "summary": f"给 {len(ids)} 道题追加 {len(tags)} 个标签",
                "preview": {"ids": ids, "tags": tags}}

    if action in {"delete_questions", "delete_question"}:
        ids = _ids(args.get("ids", args.get("id")), session)
        normalized = {"ids": ids}
        return {"name": "delete_questions", "arguments": normalized,
                "summary": f"将 {len(ids)} 道题移入回收站",
                "preview": {"ids": ids, "count": len(ids)}}

    if action == "restore_question":
        qid = str(args.get("id") or "").strip()
        if not qid or not _ID_RE.fullmatch(qid):
            raise AgentActionError("题目 id 无效")
        deleted = next((row for row in filestore.list_deleted_questions()
                        if str(row.get("id")) == qid), None)
        if not deleted:
            raise AgentActionError("回收站中没有这道题")
        bound = str(session.get("workdir_id") or "").strip("/")
        original = str(deleted.get("original_path") or "").replace("\\", "/")
        if bound and not (original == bound or original.startswith(bound + "/")):
            raise AgentActionError("题目原目录不在当前 Agent 工作目录内")
        normalized = {"id": qid}
        return {"name": action, "arguments": normalized,
                "summary": f"恢复回收站中的题目“{deleted.get('title') or qid}”",
                "preview": {"id": qid, "original_path": original}}

    # 导入/导出由 app 注册的服务提供额外任务状态和参数校验。
    if action in {"import_conversion", "export_questions"}:
        raise AgentActionError("该任务必须通过 Agent 编排接口提交")
    raise AgentActionError(f"未注册的 Agent 写入操作：{action}")


def execute_action(name: str, args: dict, *, session: dict) -> dict:
    """执行已由 :func:`plan_action` 校验过的动作；执行前再次校验。"""
    plan = plan_action(name, args, session=session)
    action = plan["name"]
    values = plan["arguments"]
    try:
        if action == "create_question":
            qids = filestore.create_questions_batch([{
                "body": values["body"], "solution": values["solution"],
                "type": values["qtype"], "source": values["source"],
                "difficulty": values["difficulty"], "tags": values["tags"],
                "number": values["number"], "title": values["title"],
            }], values["folder"])
            return {"action": action, "created_ids": qids, "count": len(qids)}
        if action == "update_question":
            filestore.update_question(
                values["id"], values["body"], values["solution"], values["qtype"],
                values["source"], values["difficulty"], values["tags"],
                values.get("note"))
            row = filestore.get_question(values["id"])
            return {"action": action, "question": _question_preview(row or {})}
        if action == "rename_question":
            row = filestore.rename_question(values["id"], values["new_title"])
            return {"action": action, "question": _question_preview(row)}
        if action == "move_questions":
            moved = filestore.move_to_collection(values["ids"], values["folder"])
            return {"action": action, "moved_ids": moved, "count": len(moved)}
        if action == "create_folder":
            folder = filestore.create_collection(values["name"], values["parent"])
            return {"action": action, "folder": folder}
        if action == "tag_questions":
            filestore.add_tags_to(values["ids"], values["tags"])
            return {"action": action, "ids": values["ids"], "tags": values["tags"],
                    "count": len(values["ids"])}
        if action == "delete_questions":
            deleted = []
            for qid in values["ids"]:
                if filestore.delete_question(qid):
                    deleted.append(qid)
            return {"action": action, "deleted_ids": deleted, "count": len(deleted)}
        if action == "restore_question":
            filestore.restore_question(values["id"])
            return {"action": action, "restored_id": values["id"]}
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise AgentActionError(f"{action} 执行失败：{exc}") from exc
    raise AgentActionError(f"未注册的 Agent 写入操作：{action}")


def is_write_action(name: str) -> bool:
    return str(name or "").strip() in {
        "create_question", "update_question", "rename_question",
        "move_questions", "move_question", "create_folder", "create_collection",
        "tag_questions", "add_tags", "delete_questions", "delete_question",
        "restore_question",
    }


def normalize_folder(value: object, *, session: dict,
                     must_exist: bool = True) -> tuple[str, Path]:
    """供任务服务复用的公开目录校验入口。"""
    return _folder(value, session, must_exist=must_exist)
