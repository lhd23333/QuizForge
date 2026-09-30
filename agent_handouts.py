"""Agent 的讲义动作：把指定题目做成讲义。

这是桌面版讲义板块的 agent 化替代：不拖拽、不开编辑器，直接按题目 id 生成，
版式（A4 单栏／双栏、16:9、解析模式、页眉页脚）作为参数给出。

设计要点
--------
* 讲义文件放在题库的 ``_handouts/`` 下，仍是普通 Markdown；正文由
  ``<!-- quizforge:question <block_id> -->`` 块组成，题目内容走 **快照**，
  原题之后被改动不会影响已生成的讲义。
* 路径校验全部交给 ``handouts`` 模块（题库根内 + 保留目录白名单），
  本模块只负责参数收敛、工作目录边界与快照。
* 写操作由 ``agent_tools.dispatch`` 转成审批卡片，执行仍走这里的
  ``execute_handout_action``；与题库写操作同一套审批与快照链路。
"""
from __future__ import annotations

import logging
from typing import Any

import handouts
import handout_exporter
import filestore
import config
from agent_actions import AgentActionError

logger = logging.getLogger(__name__)

#: 需要审批 + 快照的讲义写动作（在 agent_actions 之外，单独一组成员）。
HANDOUT_WRITE_ACTIONS = frozenset({
    "create_handout", "add_handout_questions", "delete_handout",
    "update_handout_meta", "export_handout",
})

_MAX_QUESTIONS = 200
_FMTS = frozenset({"pdf", "tex", "zip"})
_PAGE_FORMATS = frozenset({"a4", "slides"})
_SOLUTION_MODES = frozenset({"hidden", "inline", "appendix"})
_HEADER_FOOTER_KEYS = (
    "header_left", "header_center", "header_right",
    "footer_left", "footer_center", "footer_right",
)


def is_handout_action(name: str) -> bool:
    return str(name or "").strip() in HANDOUT_WRITE_ACTIONS


# --------------------------------------------------------------------------- 工具


def _require_bank(session: dict) -> str:
    if str((session or {}).get("scope") or "bank") != "bank":
        raise AgentActionError("仅聊天模式不能操作讲义")
    return str((session or {}).get("workdir_id") or "").strip("/")


def _text(value: Any, label: str, *, limit: int = 200, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise AgentActionError(f"{label}不能为空")
    if len(text) > limit:
        raise AgentActionError(f"{label}过长（上限 {limit} 字）")
    return text


def _in_scope(folder: str, bound: str) -> bool:
    folder = str(folder or "").strip("/")
    if not bound:
        return True
    return folder == bound or folder.startswith(bound + "/")


def _resolve_questions(args: dict, session: dict) -> list[str]:
    """把 ids / 关键词筛选收敛成「工作目录内、存在的题目 id」列表。"""
    bound = _require_bank(session)
    raw_ids = args.get("ids")
    values: list[str] = []
    if raw_ids:
        if isinstance(raw_ids, str):
            values = [item.strip() for item in raw_ids.replace("，", ",").split(",")]
        elif isinstance(raw_ids, (list, tuple)):
            values = [str(item).strip() for item in raw_ids]
        else:
            raise AgentActionError("ids 必须是题目 id 列表")
        values = [item for item in values if item]
        if not values:
            raise AgentActionError("ids 不能为空")
    else:
        query = _text(args.get("query"), "关键词", limit=200)
        qtype = _text(args.get("type"), "题型", limit=80)
        difficulty = _text(args.get("difficulty"), "难度", limit=20)
        tags_raw = args.get("tags")
        tags = [str(item).strip() for item in (tags_raw or [])
                if str(item).strip()] if isinstance(tags_raw, (list, tuple)) else []
        if not any((query, qtype, difficulty, tags)):
            raise AgentActionError("请提供 ids，或用 query / type / difficulty / tags 指定题目")
        try:
            records = (filestore.collection_records_snapshot(bound)
                       if bound else filestore.all_records_snapshot())
            rows = filestore.list_questions(
                records=records, search=query, tags=tags, match="and",
                qtype=qtype, difficulty=difficulty, sort="custom")
        except Exception as exc:  # noqa: BLE001 - 筛选失败要给可读原因
            raise AgentActionError(f"筛选题目失败：{exc}") from exc
        values = [str(row.get("id") or "") for row in rows if row.get("id")]

    if not values:
        raise AgentActionError("没有匹配到任何题目")
    if len(values) > _MAX_QUESTIONS:
        raise AgentActionError(f"单次最多 {_MAX_QUESTIONS} 道题（当前 {len(values)}）")

    ordered: list[str] = []
    for qid in dict.fromkeys(values):
        row = filestore.get_question(qid)
        if row is None:
            raise AgentActionError(f"未找到题目：{qid}")
        if not _in_scope(str(row.get("folder") or ""), bound):
            raise AgentActionError(f"题目 {qid} 不在当前 Agent 工作目录内")
        ordered.append(qid)
    return ordered


def _layout(args: dict, metadata: dict | None = None) -> dict:
    """把版式参数收敛成 handouts 元数据（未给的沿用原值/默认值）。"""
    base = dict(metadata or {})
    page_format = str(args.get("page_format") or base.get("page_format") or "a4").strip().lower()
    if page_format not in _PAGE_FORMATS:
        raise AgentActionError("版式只支持 a4 或 slides")
    columns_raw = args.get("columns", base.get("columns", 1))
    try:
        columns = int(columns_raw or 1)
    except (TypeError, ValueError) as exc:
        raise AgentActionError("columns 必须是 1 或 2") from exc
    if columns not in (1, 2):
        raise AgentActionError("columns 必须是 1 或 2")
    solution = str(args.get("solution_mode") or base.get("solution_default") or "hidden").strip().lower()
    if solution not in _SOLUTION_MODES:
        raise AgentActionError("solution_mode 只支持 hidden / inline / appendix")
    header_footer = dict(base.get("header_footer") or {})
    raw_hf = args.get("header_footer")
    if isinstance(raw_hf, dict):
        for key in _HEADER_FOOTER_KEYS:
            if raw_hf.get(key) is not None:
                header_footer[key] = str(raw_hf[key])[:500]
    meta = {
        **base,
        "page_format": page_format,
        "columns": 2 if page_format == "a4" and columns == 2 else 1,
        "solution_default": solution,
        "paper_tone": "cream" if args.get("paper_tone") == "cream" else base.get("paper_tone", "white"),
        "wimath_logo": bool(args.get("wimath_logo", base.get("wimath_logo", False))),
        "header_footer": header_footer,
    }
    return meta


def _blocks_for(qids: list[str]) -> tuple[str, dict, list[dict]]:
    """把题目快照拼成讲义正文与 question_blocks 元数据。"""
    parts: list[str] = []
    question_meta: dict[str, dict] = {}
    summary: list[dict] = []
    for qid in qids:
        try:
            snapshot = handouts.question_snapshot(qid)
        except KeyError as exc:
            raise AgentActionError(f"原题不存在或已删除：{qid}") from exc
        block_id = handouts.new_block_id()
        body = str(snapshot.get("body") or "")
        solution = str(snapshot.get("solution") or "")
        parts.append(handouts.question_marker(block_id, body, solution))
        question_meta[block_id] = {k: v for k, v in snapshot.items()}
        summary.append({
            "block_id": block_id, "id": qid,
            "type": snapshot.get("question_type") or "",
            "body": body[:120],
        })
    return "\n\n".join(parts) + "\n", question_meta, summary


def _handout_path(raw: Any) -> str:
    path = _text(raw, "讲义路径", limit=400, required=True)
    return path.replace("\\", "/")


# ---------------------------------------------------------------------- 计划


def plan_handout_action(name: str, args: dict | None, *, session: dict) -> dict:
    """校验并规范化讲义写动作，返回可展示的计划（无副作用）。"""
    args = args if isinstance(args, dict) else {}
    action = str(name or "").strip()
    _require_bank(session)

    if action == "create_handout":
        title = _text(args.get("title"), "讲义标题", limit=200, required=True)
        qids = _resolve_questions(args, session)
        meta = _layout(args)
        body, question_meta, summary = _blocks_for(qids)
        meta["question_blocks"] = question_meta
        return {
            "name": action,
            "arguments": {"title": title, "metadata": meta, "body": body},
            "summary": f"新建讲义「{title}」（{len(qids)} 道题）",
            "preview": {"title": title, "count": len(qids),
                        "page_format": meta["page_format"], "columns": meta["columns"],
                        "solution_mode": meta["solution_default"], "questions": summary[:30]},
        }

    if action == "add_handout_questions":
        path = _handout_path(args.get("path"))
        qids = _resolve_questions(args, session)
        body, question_meta, summary = _blocks_for(qids)
        position = str(args.get("position") or "end").strip().lower()
        if position not in {"end", "start"}:
            raise AgentActionError("position 只支持 end 或 start")
        return {
            "name": action,
            "arguments": {"path": path, "body": body,
                          "question_blocks": question_meta, "position": position},
            "summary": f"向讲义 {path} 追加 {len(qids)} 道题（{'开头' if position == 'start' else '末尾'}）",
            "preview": {"path": path, "count": len(qids), "questions": summary[:30]},
        }

    if action == "update_handout_meta":
        path = _handout_path(args.get("path"))
        try:
            current = handouts.read_document(path)
        except (handouts.HandoutError, OSError, UnicodeError) as exc:
            raise AgentActionError(f"读不到讲义：{exc}") from exc
        # 以现有元数据打底：只改显式给出的版式字段，标题与页眉页脚等都保留。
        meta = _layout(args, current["metadata"])
        return {
            "name": action,
            "arguments": {"path": path, "metadata": meta},
            "summary": f"修改讲义 {path} 的版式",
            "preview": {"path": path, "page_format": meta["page_format"],
                        "columns": meta["columns"],
                        "solution_mode": meta["solution_default"]},
        }

    if action == "export_handout":
        path = _handout_path(args.get("path"))
        fmt = str(args.get("format") or "pdf").strip().lower()
        if fmt not in _FMTS:
            raise AgentActionError("导出格式只支持 pdf / tex / zip")
        return {
            "name": action,
            "arguments": {"path": path, "format": fmt},
            "summary": f"导出讲义 {path}（{fmt.upper()}）",
            "preview": {"path": path, "format": fmt},
        }

    if action == "delete_handout":
        path = _handout_path(args.get("path"))
        return {
            "name": action,
            "arguments": {"path": path},
            "summary": f"删除讲义 {path}",
            "preview": {"path": path},
        }

    raise AgentActionError(f"未注册的讲义写操作：{action}")


# ---------------------------------------------------------------------- 执行


def _snapshot(path: str, action: str, session: dict) -> None:
    """讲义是内容类文件：改/删之前先快照，回滚语义与题卡一致。"""
    import agent_snapshots  # 延迟导入，避免与 agent_tools 形成环

    try:
        agent_snapshots.create_snapshot(
            [path], action=action, session_id=str((session or {}).get("id") or ""))
    except agent_snapshots.SnapshotError as exc:
        logger.warning("讲义快照失败（%s %s）：%s", action, path, exc)


def execute_handout_action(name: str, args: dict, *, session: dict) -> dict:
    """执行已批准的讲义动作。

    ``args`` 必须是 :func:`plan_handout_action` 规范化后的参数（审批卡片里存的就是
    这一份）。题目内容在规划阶段已固化成快照，所以这里不再重新解析 ids / 筛选条件，
    只处理讲义文件本身（读写前靠 mtime 做外部修改冲突检测）。
    """
    action = str(name or "").strip()
    values = args if isinstance(args, dict) else {}
    if not is_handout_action(action):
        raise AgentActionError(f"未注册的讲义写操作：{action}")

    if action == "create_handout":
        try:
            result = handouts.create_document(
                values["title"], metadata=values["metadata"], body=values["body"])
        except (handouts.HandoutError, OSError, UnicodeError) as exc:
            raise AgentActionError(f"新建讲义失败：{exc}") from exc
        return {"action": action, "path": result["path"], "title": values["title"],
                "count": len((values.get("metadata") or {}).get("question_blocks") or {})}

    if action == "add_handout_questions":
        path = values["path"]
        _snapshot(path, action, session)
        try:
            current = handouts.read_document(path)
            meta = dict(current["metadata"])
            blocks = dict(meta.get("question_blocks") or {})
            blocks.update(values["question_blocks"])
            meta["question_blocks"] = blocks
            addition = values["body"]
            body = current["body"]
            body = (addition + "\n" + body) if values["position"] == "start" else (
                body.rstrip("\n") + "\n\n" + addition)
            saved = handouts.write_document(path, meta, body, current["mtime"])
        except (handouts.HandoutError, OSError, UnicodeError) as exc:
            raise AgentActionError(f"追加题目失败：{exc}") from exc
        if not saved.get("ok"):
            raise AgentActionError("讲义在外部被修改，请重新读取后再追加")
        return {"action": action, "path": path, "mtime": saved.get("mtime"),
                "added": len(values["question_blocks"])}

    if action == "update_handout_meta":
        path = values["path"]
        _snapshot(path, action, session)
        try:
            current = handouts.read_document(path)
            meta = dict(values["metadata"])
            # 题目块的元数据以磁盘上的为准，免得规划阶段的旧快照覆盖正文新块。
            meta["question_blocks"] = current["metadata"].get("question_blocks") or {}
            saved = handouts.write_document(path, meta, current["body"], current["mtime"])
        except (handouts.HandoutError, OSError, UnicodeError) as exc:
            raise AgentActionError(f"修改讲义版式失败：{exc}") from exc
        if not saved.get("ok"):
            raise AgentActionError("讲义在外部被修改，请重新读取后再改")
        return {"action": action, "path": path, "mtime": saved.get("mtime")}

    if action == "export_handout":
        path = values["path"]
        try:
            document = handouts.read_document(path)
            out_path = handout_exporter.export(
                document["metadata"], document["body"], fmt=values["format"])
        except (handouts.HandoutError, handout_exporter.HandoutExportError,
                OSError, RuntimeError) as exc:
            raise AgentActionError(f"导出讲义失败：{exc}") from exc
        relative = str(out_path)
        try:
            relative = out_path.relative_to(config.BANK_DIR).as_posix()
        except ValueError:
            pass
        return {"action": action, "path": path, "format": values["format"],
                "output": relative, "filename": out_path.name}

    if action == "delete_handout":
        path = values["path"]
        _snapshot(path, action, session)
        try:
            document = handouts.read_document(path)
            handouts.delete_document(path, document["mtime"])
        except (handouts.HandoutError, OSError, UnicodeError) as exc:
            raise AgentActionError(f"删除讲义失败：{exc}") from exc
        return {"action": action, "path": path, "deleted": True}

    raise AgentActionError(f"未注册的讲义写操作：{action}")
