"""Agent 写入操作的确认状态机。

模型编排层只负责提出操作计划；真正写入题库前由这里记录一个绑定到会话上下文
的待确认项。当前模块不执行任何工具，确认接口仅把状态推进为 ``approved``，
后续写入编排器可以安全地消费该状态并自行调用白名单工具。
"""

from __future__ import annotations

import secrets
import threading
import time
import copy
import json
import os
from typing import Any

import config


class ApprovalError(ValueError):
    """确认项不存在、归属不匹配或状态不允许转换。"""

    def __init__(self, message: str, *, code: str = "invalid_approval",
                 status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


_ID_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
_MAX_PENDING = 200
_TTL_SECONDS = 24 * 60 * 60
_SECRET_KEYS = {"api_key", "apikey", "token", "password", "secret", "credential"}


def _redact(value: Any):
    """只保存可序列化且不含常见凭据字段的确认参数。"""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            if name.casefold() in _SECRET_KEYS or any(
                    marker in name.casefold()
                    for marker in ("api_key", "token", "password", "secret")):
                result[name] = "[已隐藏]"
            else:
                result[name] = _redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _copy(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "action": row["action"],
        "summary": row["summary"],
        "arguments": _redact(row.get("arguments") or {}),
        "status": row["status"],
        "mode": row.get("mode", "standard"),
        "scope": row.get("scope", "bank"),
        "workdir_id": row.get("workdir_id", ""),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "result": _redact(row.get("result")),
        "error": row.get("error"),
        "execution_status": row.get("execution_status"),
    }


class ApprovalStore:
    """进程内确认项存储；单机应用重启后待确认操作自然失效。"""

    def __init__(self, *, max_items: int = _MAX_PENDING,
                 ttl_seconds: int = _TTL_SECONDS):
        self.max_items = max(1, int(max_items))
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._lock = threading.RLock()
        self._rows: dict[str, dict[str, Any]] = {}

    def _purge_unlocked(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        expired = [key for key, row in self._rows.items()
                   if now - float(row.get("created_at", now)) > self.ttl_seconds]
        for key in expired:
            self._rows.pop(key, None)

    @staticmethod
    def _audit(event: str, row: dict[str, Any], *, detail: str = "") -> None:
        """写入不含原始参数的审计日志；日志失败不影响题库操作。"""
        try:
            path = getattr(config, "AGENT_AUDIT_PATH", None)
            if path is None:
                path = config.DATA_DIR / "agent_audit.jsonl"
            path = path if hasattr(path, "parent") else str(path)
            path = __import__("pathlib").Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "event": str(event), "approval_id": str(row.get("id")),
                "session_id": str(row.get("session_id")),
                "action": str(row.get("action")),
                "status": str(row.get("status")), "mode": str(row.get("mode")),
                "scope": str(row.get("scope")), "workdir_id": str(row.get("workdir_id")),
                "timestamp": time.time(),
            }
            if detail:
                record["detail"] = str(detail)[:500]
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            return

    def create(self, session: dict[str, Any], action: str, summary: str,
               arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        action = str(action or "").strip()
        summary = str(summary or "").strip()
        if not action or len(action) > 120:
            raise ApprovalError("确认操作名称无效", code="invalid_action")
        if not summary or len(summary) > 4000:
            raise ApprovalError("确认说明不能为空且不能超过 4000 个字符",
                                code="invalid_summary")
        sid = str(session.get("id") or "").strip()
        if not sid:
            raise ApprovalError("Agent 会话无效", code="invalid_session")
        now = time.time()
        row = {
            "id": secrets.token_urlsafe(18),
            "session_id": sid,
            "action": action,
            "summary": summary,
            "arguments": _redact(arguments or {}),
            "status": "pending",
            "mode": str(session.get("mode") or "standard"),
            "scope": str(session.get("scope") or "bank"),
            "workdir_id": str(session.get("workdir_id") or ""),
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            # 原始参数只在进程内供审批后执行；公开快照和审计日志永远使用
            # 上面的 _redact 结果，避免密钥或完整正文进入响应。
            "_arguments": copy.deepcopy(arguments or {}),
            "execution_status": "pending",
        }
        with self._lock:
            self._purge_unlocked(now)
            self._rows[row["id"]] = row
            self._audit("created", row)
            if len(self._rows) > self.max_items:
                # 终态优先清理，随后按创建时间淘汰最旧项，避免无限堆积。
                order = sorted(
                    self._rows.items(),
                    key=lambda item: (
                        item[1].get("status") == "pending",
                        item[1].get("created_at", 0),
                    ),
                )
                for key, _row in order[:len(self._rows) - self.max_items]:
                    self._rows.pop(key, None)
            return _copy(row)

    def _get_unlocked(self, approval_id: str) -> dict[str, Any]:
        row = self._rows.get(str(approval_id or ""))
        if row is None:
            raise ApprovalError("确认项不存在或已过期", code="not_found", status=404)
        return row

    def get(self, session_id: str, approval_id: str) -> dict[str, Any]:
        with self._lock:
            self._purge_unlocked()
            row = self._get_unlocked(approval_id)
            if row["session_id"] != str(session_id):
                raise ApprovalError("确认项不属于当前会话", code="forbidden", status=403)
            return _copy(row)

    def list(self, session_id: str, *, include_terminal: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            self._purge_unlocked()
            rows = [row for row in self._rows.values()
                    if row["session_id"] == str(session_id)
                    and (include_terminal or row["status"] == "pending")]
            return [_copy(row) for row in sorted(
                rows, key=lambda row: row.get("created_at", 0), reverse=True)]

    def _transition(self, session: dict[str, Any], approval_id: str,
                    target: str, *, result: Any = None) -> dict[str, Any]:
        sid = str(session.get("id") or "")
        with self._lock:
            self._purge_unlocked()
            row = self._get_unlocked(approval_id)
            if row["session_id"] != sid:
                raise ApprovalError("确认项不属于当前会话", code="forbidden", status=403)
            current = row["status"]
            if current == target:
                return _copy(row)
            if current != "pending":
                raise ApprovalError(
                    f"确认项已经处于“{current}”状态，不能再次操作",
                    code="already_terminal")
            # 会话的工作范围/目录变化后，旧计划不能悄悄作用到新目录。
            if (row.get("scope") != str(session.get("scope") or "bank")
                    or row.get("workdir_id", "") != str(session.get("workdir_id") or "")):
                raise ApprovalError(
                    "会话工作目录已变化，请重新生成确认计划",
                    code="context_changed", status=409)
            row["status"] = target
            row["execution_status"] = "approved" if target == "approved" else "cancelled"
            row["updated_at"] = time.time()
            if result is not None:
                row["result"] = _redact(result)
            self._audit(target, row)
            return _copy(row)

    def approve(self, session: dict[str, Any], approval_id: str,
                result: Any = None) -> dict[str, Any]:
        return self._transition(session, approval_id, "approved", result=result)

    def cancel(self, session: dict[str, Any], approval_id: str,
               reason: str = "") -> dict[str, Any]:
        row = self._transition(session, approval_id, "cancelled")
        if reason and row["status"] == "cancelled":
            with self._lock:
                current = self._rows.get(str(approval_id))
                if current:
                    current["error"] = str(reason)[:1000]
                    current["updated_at"] = time.time()
                    row = _copy(current)
        return row

    def execution_payload(self, session: dict[str, Any], approval_id: str) -> tuple[str, dict[str, Any]]:
        """读取已批准动作的私有参数；只允许绑定会话且不能重复执行。"""
        sid = str(session.get("id") or "")
        with self._lock:
            self._purge_unlocked()
            row = self._get_unlocked(approval_id)
            if row["session_id"] != sid:
                raise ApprovalError("确认项不属于当前会话", code="forbidden", status=403)
            if row.get("status") != "approved":
                raise ApprovalError("确认项尚未批准或已经执行", code="not_approved", status=409)
            return str(row["action"]), copy.deepcopy(row.get("_arguments") or {})

    def claim_execution(self, session: dict[str, Any], approval_id: str):
        """原子领取一个已批准动作，防止重复点击造成重复写入。

        返回 ``(action, arguments)`` 表示本次请求取得执行权；若该项已经被另一
        个请求领取或执行，返回 ``None``。调用方仍需在完成后调用
        :meth:`mark_executed` 或 :meth:`mark_failed`。
        """
        sid = str(session.get("id") or "")
        with self._lock:
            self._purge_unlocked()
            row = self._get_unlocked(approval_id)
            if row["session_id"] != sid:
                raise ApprovalError("确认项不属于当前会话", code="forbidden", status=403)
            if row.get("status") != "approved":
                raise ApprovalError("确认项尚未批准或已经执行", code="not_approved", status=409)
            if (row.get("scope") != str(session.get("scope") or "bank")
                    or row.get("workdir_id", "") != str(session.get("workdir_id") or "")):
                raise ApprovalError(
                    "会话工作目录已变化，请重新生成确认计划",
                    code="context_changed", status=409)
            execution_status = str(row.get("execution_status") or "approved")
            if execution_status in {"executing", "executed", "failed", "cancelled"}:
                return None
            row["execution_status"] = "executing"
            row["updated_at"] = time.time()
            self._audit("executing", row)
            return str(row["action"]), copy.deepcopy(row.get("_arguments") or {})

    def mark_executed(self, session: dict[str, Any], approval_id: str,
                      result: Any = None) -> dict[str, Any]:
        sid = str(session.get("id") or "")
        with self._lock:
            self._purge_unlocked()
            row = self._get_unlocked(approval_id)
            if row["session_id"] != sid:
                raise ApprovalError("确认项不属于当前会话", code="forbidden", status=403)
            if row.get("status") == "executed":
                return _copy(row)
            if row.get("status") != "approved":
                raise ApprovalError("确认项未处于可执行状态", code="not_approved", status=409)
            if row.get("execution_status") not in {"approved", "executing", "pending"}:
                raise ApprovalError("确认项已经执行或失效", code="already_terminal", status=409)
            row["status"] = "executed"
            row["execution_status"] = "executed"
            row["updated_at"] = time.time()
            row["result"] = _redact(result)
            self._audit("executed", row)
            return _copy(row)

    def mark_failed(self, session: dict[str, Any], approval_id: str,
                    error: str) -> dict[str, Any]:
        sid = str(session.get("id") or "")
        with self._lock:
            self._purge_unlocked()
            row = self._get_unlocked(approval_id)
            if row["session_id"] != sid:
                raise ApprovalError("确认项不属于当前会话", code="forbidden", status=403)
            if row.get("status") == "failed":
                return _copy(row)
            if row.get("status") != "approved":
                raise ApprovalError("确认项未处于可执行状态", code="not_approved", status=409)
            if row.get("execution_status") not in {"approved", "executing", "pending"}:
                raise ApprovalError("确认项已经执行或失效", code="already_terminal", status=409)
            row["status"] = "failed"
            row["execution_status"] = "failed"
            row["error"] = str(error)[:1000]
            row["updated_at"] = time.time()
            self._audit("failed", row, detail=str(error))
            return _copy(row)
