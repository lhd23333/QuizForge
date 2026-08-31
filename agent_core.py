"""QuizForge Agent 基础运行时：会话、工作目录和受控工具注册。"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


logger = logging.getLogger(__name__)


# 会话是工作上下文，不是长期知识库。只保留有限的最近消息，避免把完整题目
# 正文或无限增长的聊天记录写入用户数据目录。
_SESSION_STORE_VERSION = 1
_MAX_PERSISTED_SESSIONS = 100
_MAX_PERSISTED_MESSAGES = 48
_MAX_PERSISTED_MESSAGE_CHARS = 12000
_MAX_PERSISTED_STORE_BYTES = 2 * 1024 * 1024
_MAX_SESSION_ID_CHARS = 128
_MAX_PROVIDER_ID_CHARS = 160
_SESSION_SCOPES = frozenset({"bank", "chat"})
_SESSION_MODES = frozenset({"standard", "danger"})
_MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})
_MESSAGE_STATUSES = frozenset({"complete", "stopped", "error"})


class AgentError(ValueError):
    pass


class AgentBusyError(AgentError):
    """当前会话已有一轮消息在处理，调用方应稍后重试。"""


@dataclass
class AgentTurnControl:
    """一轮 Agent 请求的进程内控制句柄。

    句柄不会持久化。取消既设置协作式事件，也会立即关闭当前上游流，避免
    浏览器停止等待后模型请求仍在后台继续消耗额度。
    """

    id: str
    session_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _closer: Callable[[], None] | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _turn_lock: threading.Lock | None = field(default=None, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def bind_closer(self, closer: Callable[[], None] | None) -> None:
        close_now = False
        with self._lock:
            self._closer = closer
            close_now = self.cancel_event.is_set() and closer is not None
        if close_now:
            try:
                closer()
            except Exception:
                pass

    def cancel(self) -> bool:
        """幂等请求取消；返回本次是否首次发出取消。"""
        with self._lock:
            first = not self.cancel_event.is_set()
            self.cancel_event.set()
            closer = self._closer if first else None
            if self.status == "running":
                self.status = "cancelling"
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
        return first

    def finish(self, status: str) -> bool:
        with self._lock:
            if self.finished_at is not None:
                return False
            self.status = status
            self.finished_at = time.time()
            self._closer = None
            return True

    def public_state(self) -> dict:
        with self._lock:
            return {
                "turn_id": self.id,
                "session_id": self.session_id,
                "status": self.status,
                "cancelled": self.cancel_event.is_set(),
                "created_at": self.created_at,
                "finished_at": self.finished_at,
            }


_UNSET = object()


class AgentRuntime:
    def __init__(self, root: Path, sessions_path: Path | None = None):
        self.root = Path(root).expanduser().resolve()
        # sessions_path 显式传入时启用轻量持久化；省略时保持原有的纯内存
        # 运行时，方便离线测试和调用方创建短生命周期的临时 Agent。
        self.sessions_path = (
            Path(sessions_path).expanduser().resolve()
            if sessions_path is not None else None
        )
        self._lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self._turn_locks: dict[str, threading.Lock] = {}
        self._turns: dict[str, AgentTurnControl] = {}
        with self._lock:
            self._load_persisted_locked()

    @staticmethod
    def _canonical_root(value) -> str | None:
        """返回用于跨平台比较的题库根路径；非法值不参与会话恢复。"""
        try:
            path = Path(value).expanduser().resolve()
        except (OSError, TypeError, ValueError):
            return None
        return os.path.normcase(str(path))

    def _same_root(self, value) -> bool:
        return self._canonical_root(value) == self._canonical_root(self.root)

    @staticmethod
    def _safe_timestamp(value, fallback: float) -> float:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return fallback
        if not math.isfinite(timestamp) or timestamp < 0:
            return fallback
        return timestamp

    @classmethod
    def _normalise_messages(cls, raw) -> list[dict]:
        if not isinstance(raw, list):
            return []
        messages = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in _MESSAGE_ROLES:
                continue
            content = item.get("content")
            if not isinstance(content, str):
                continue
            content = content.replace("\x00", "")
            if len(content) > _MAX_PERSISTED_MESSAGE_CHARS:
                content = content[-_MAX_PERSISTED_MESSAGE_CHARS:]
            status = str(item.get("status") or "complete").strip().lower()
            if status not in _MESSAGE_STATUSES:
                status = "complete"
            message = {"role": role, "content": content, "status": status}
            turn_id = str(item.get("turn_id") or "").strip()
            if turn_id and len(turn_id) <= _MAX_SESSION_ID_CHARS:
                message["turn_id"] = turn_id
            messages.append(message)
        if len(messages) <= _MAX_PERSISTED_MESSAGES:
            return messages
        return messages[-_MAX_PERSISTED_MESSAGES:]

    @classmethod
    def _normalise_persisted_row(cls, raw: dict, *, fallback_root,
                                 runtime: "AgentRuntime") -> dict | None:
        """校验磁盘记录并还原成运行时行；任何一项不可信就丢弃该行。"""
        sid = str(raw.get("id") or "").strip()
        if not sid or len(sid) > _MAX_SESSION_ID_CHARS:
            return None
        row_root = raw.get("bank_root") or fallback_root
        if not runtime._same_root(row_root):
            return None

        scope = str(raw.get("scope") or "bank").strip().lower()
        if scope not in _SESSION_SCOPES:
            return None
        stored_mode = str(raw.get("mode") or "standard").strip().lower()
        if stored_mode not in _SESSION_MODES:
            return None
        # 危险授权只能由当前页面显式武装，旧版落盘的 danger 一律降级。
        mode = "standard"

        provider_id = raw.get("provider_id")
        if provider_id is not None:
            if not isinstance(provider_id, (str, int)):
                return None
            provider_id = str(provider_id).strip()
            if not provider_id or len(provider_id) > _MAX_PROVIDER_ID_CHARS:
                return None

        workdir_id = raw.get("workdir_id") or ""
        if not isinstance(workdir_id, str):
            return None
        workdir_id = workdir_id.replace("\\", "/").strip()
        output_dir_id = raw.get("output_dir_id") or ""
        if not isinstance(output_dir_id, str):
            return None
        output_dir_id = output_dir_id.replace("\\", "/").strip()
        input_dir_id = raw.get("input_dir_id") or ""
        if not isinstance(input_dir_id, str):
            return None
        input_dir_id = input_dir_id.replace("\\", "/").strip()
        now = time.time()
        created_at = cls._safe_timestamp(raw.get("created_at"), now)
        updated_at = cls._safe_timestamp(raw.get("updated_at"), created_at)

        if scope == "chat":
            path = None
            workdir_id = ""
            input_dir_id = ""
        else:
            try:
                path = runtime.resolve_path(workdir_id or ".")
            except (AgentError, TypeError, ValueError):
                return None
            # 目录被用户删除时保留对话，但把工作范围安全地退回题库根；
            # 文件路径则不是合法的 Agent 工作目录，直接丢弃记录。
            if path.exists() and not path.is_dir():
                return None
            if not path.exists():
                path = runtime.root
                workdir_id = ""
            else:
                try:
                    workdir_id = runtime.relative_id(path)
                except AgentError:
                    return None
            try:
                output_path = runtime.resolve_path(output_dir_id or workdir_id or ".")
                if not output_path.is_dir():
                    raise AgentError("导出目录不存在")
                output_dir_id = runtime.relative_id(output_path)
                if workdir_id and not (output_dir_id == workdir_id
                                       or output_dir_id.startswith(workdir_id + "/")):
                    raise AgentError("导出目录不在工作目录内")
            except (AgentError, TypeError, ValueError):
                output_dir_id = workdir_id
            try:
                input_path = runtime.resolve_path(input_dir_id or workdir_id or ".")
                if not input_path.is_dir():
                    raise AgentError("材料目录不存在")
                input_dir_id = runtime.relative_id(input_path)
            except (AgentError, TypeError, ValueError):
                input_dir_id = workdir_id

        return {
            "id": sid,
            "scope": scope,
            "workdir": str(path) if path else None,
            "workdir_id": workdir_id,
            "input_dir_id": input_dir_id if scope == "bank" else "",
            "output_dir_id": output_dir_id if scope == "bank" else "",
            "mode": mode,
            "provider_id": provider_id,
            "messages": cls._normalise_messages(raw.get("messages")),
            "created_at": created_at,
            "updated_at": updated_at,
        }

    def _read_store_locked(self) -> tuple[list[dict], str | None]:
        """读取磁盘中的全部记录；损坏文件按空存储处理。"""
        if self.sessions_path is None:
            return [], None
        try:
            raw = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], None
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("agent_sessions.json 无法读取，按空存储处理")
            return [], None
        if not isinstance(raw, dict):
            return [], None
        try:
            version = int(raw.get("version", _SESSION_STORE_VERSION) or 0)
        except (TypeError, ValueError):
            return [], None
        if version != _SESSION_STORE_VERSION:
            logger.warning("agent_sessions.json 版本不受支持，按空存储处理")
            return [], None
        rows = raw.get("sessions")
        if not isinstance(rows, list):
            return [], raw.get("bank_root")
        return [row for row in rows if isinstance(row, dict)], raw.get("bank_root")

    def _load_persisted_locked(self) -> None:
        rows, fallback_root = self._read_store_locked()
        seen: set[str] = set()
        for raw in rows:
            row = self._normalise_persisted_row(
                raw, fallback_root=fallback_root, runtime=self)
            if row is None or row["id"] in seen:
                continue
            seen.add(row["id"])
            self.sessions[row["id"]] = row
            self._turn_locks[row["id"]] = threading.Lock()
        if len(self.sessions) > _MAX_PERSISTED_SESSIONS:
            ordered = sorted(self.sessions.values(),
                             key=lambda row: row.get("updated_at", 0),
                             reverse=True)[:_MAX_PERSISTED_SESSIONS]
            self.sessions = {row["id"]: row for row in ordered}
            self._turn_locks = {
                row["id"]: self._turn_locks[row["id"]] for row in ordered
            }

    @classmethod
    def _serialise_row(cls, row: dict, bank_root: str) -> dict:
        messages = cls._normalise_messages(row.get("messages"))
        return {
            "id": str(row.get("id") or ""),
            "bank_root": bank_root,
            "scope": str(row.get("scope") or "bank"),
            "workdir_id": str(row.get("workdir_id") or ""),
            "output_dir_id": str(row.get("output_dir_id") or ""),
            "input_dir_id": str(row.get("input_dir_id") or row.get("workdir_id") or ""),
            # 危险模式是进程内短期授权，绝不进入会话文件。
            "mode": "standard",
            "provider_id": row.get("provider_id"),
            "messages": messages,
            "created_at": cls._safe_timestamp(row.get("created_at"), time.time()),
            "updated_at": cls._safe_timestamp(row.get("updated_at"), time.time()),
        }

    def _persist_locked(self) -> None:
        """原子保存会话；失败时保留内存状态并记录日志。"""
        if self.sessions_path is None:
            return
        path = self.sessions_path
        try:
            disk_rows, disk_root = self._read_store_locked()
            current_root = str(self.root)
            merged = []
            for raw in disk_rows:
                row_root = raw.get("bank_root") or disk_root
                if self._same_root(row_root):
                    continue
                merged.append(raw)
            merged.extend(self._serialise_row(row, current_root)
                          for row in self.sessions.values())
            merged.sort(key=lambda row: self._safe_timestamp(
                row.get("updated_at"), 0), reverse=True)
            merged = merged[:_MAX_PERSISTED_SESSIONS]
            payload = {
                "version": _SESSION_STORE_VERSION,
                # 供单题库旧格式/人工检查使用；每条记录仍带 bank_root，
                # 因为同一份全局文件可以同时保存多个题库的会话。
                "bank_root": current_root,
                "sessions": merged,
            }
            encoded = json.dumps(payload, ensure_ascii=False, indent=2)
            # 理论上单条消息已受限，但仍给整个文件设硬上限，避免异常历史
            # 记录占满用户数据目录。优先丢弃最旧的记录。
            while len(encoded.encode("utf-8")) > _MAX_PERSISTED_STORE_BYTES \
                    and len(payload["sessions"]) > 1:
                payload["sessions"].pop()
                encoded = json.dumps(payload, ensure_ascii=False, indent=2)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(
                f".{path.name}.{secrets.token_hex(8)}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Agent 会话持久化失败：%s", exc)

    def new_session(self, workdir: str | None = None, scope: str = "bank",
                    output_dir: str | None = None,
                    input_dir: str | None = None) -> dict:
        scope = str(scope or "bank").lower()
        if scope not in {"bank", "chat"}:
            raise AgentError("未知的 Agent 工作范围")
        path = self._resolve_workdir(workdir or ".") if scope == "bank" else None
        workdir_id = self.relative_id(path) if path else ""
        output_path = (self._resolve_workdir(output_dir or workdir or ".")
                       if scope == "bank" else None)
        output_dir_id = self.relative_id(output_path) if output_path else ""
        input_path = (self._resolve_workdir(input_dir or workdir or ".")
                      if scope == "bank" else None)
        input_dir_id = self.relative_id(input_path) if input_path else ""
        sid = secrets.token_urlsafe(18)
        row = {"id": sid, "scope": scope, "workdir": str(path) if path else None,
               "workdir_id": workdir_id,
               "input_dir_id": input_dir_id,
               "output_dir_id": output_dir_id,
               "mode": "standard", "provider_id": None, "messages": [],
               "created_at": time.time(), "updated_at": time.time()}
        with self._lock:
            self.sessions[sid] = row
            self._turn_locks[sid] = threading.Lock()
            self._persist_locked()
        return self.public_session(row)

    def get_session(self, sid: str) -> dict:
        with self._lock:
            try:
                return self.sessions[sid]
            except KeyError as exc:
                raise AgentError("Agent 会话不存在") from exc

    def list_sessions(self) -> list[dict]:
        """返回稳定的会话快照，避免请求线程遍历可变字典。"""
        with self._lock:
            rows = sorted(self.sessions.values(),
                          key=lambda row: row.get("updated_at", 0), reverse=True)
            return [self.public_session(row) for row in rows]

    def delete_session(self, sid: str) -> None:
        with self._lock:
            if self.sessions.pop(sid, None) is None:
                raise AgentError("Agent 会话不存在")
            self._turn_locks.pop(sid, None)
            self._persist_locked()

    def resolve_path(self, value: str) -> Path:
        candidate = (self.root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise AgentError("路径必须位于当前题库目录内") from exc
        reserved = {"_assets", "_handouts", "_backups", ".trash"}
        if any(part.startswith(".") or part in reserved for part in relative.parts):
            raise AgentError("不能把隐藏或系统目录作为 Agent 工作目录")
        return candidate

    def _resolve_workdir(self, value: str) -> Path:
        """解析一个实际可用的工作目录，拒绝普通文件和尚不存在的路径。"""
        candidate = self.resolve_path(value)
        if not candidate.is_dir():
            raise AgentError("Agent 工作目录不存在或不是文件夹")
        return candidate

    def relative_id(self, path: Path) -> str:
        """把目录统一成题库根下的 POSIX id，避免会话暴露不稳定绝对路径。"""
        try:
            relative = path.resolve().relative_to(self.root).as_posix()
            return "" if relative == "." else relative
        except ValueError as exc:
            raise AgentError("路径必须位于当前题库目录内") from exc

    @staticmethod
    def public_session(row: dict) -> dict:
        # messages 不能直接暴露内部 list；否则 jsonify/前端合并期间的并发追加
        # 可能观察到半条消息，也会让调用方意外修改运行时状态。
        return {
            "id": row["id"],
            "scope": row["scope"],
            "workdir": row["workdir"],
            "workdir_id": row["workdir_id"],
            "input_dir_id": row.get("input_dir_id", row.get("workdir_id", "")),
            "output_dir_id": row.get("output_dir_id", row.get("workdir_id", "")),
            "mode": row["mode"],
            "provider_id": row.get("provider_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "messages": [dict(message) for message in row.get("messages", [])],
        }

    def set_workdir(self, sid: str, workdir: str | None, scope: str | None = None) -> dict:
        return self.update_session(sid, workdir=workdir, scope=scope)

    def update_session(self, sid: str, *, mode=_UNSET, provider_id=_UNSET,
                       workdir=_UNSET, output_dir=_UNSET, input_dir=_UNSET, scope=_UNSET,
                       provider_validator=None) -> dict:
        """一次性校验并更新会话上下文，避免 PATCH 半成功。

        ``provider_validator`` 由 Flask 层注入，负责确认 Provider 仍存在且处于
        启用状态；运行时本身不依赖 Provider 模块，便于离线测试和本地快捷模式。
        """
        with self._lock:
            try:
                row = self.sessions[sid]
            except KeyError as exc:
                raise AgentError("Agent 会话不存在") from exc
            next_mode = row.get("mode", "standard") if mode is _UNSET else mode
            if next_mode != "standard":
                raise AgentError("危险模式必须在当前页面中单独确认并临时开启")
            next_scope = (row.get("scope") or "bank") if scope is _UNSET else scope
            next_scope = str(next_scope or "bank").lower()
            if next_scope not in {"bank", "chat"}:
                raise AgentError("未知的 Agent 工作范围")
            if provider_id is _UNSET:
                next_provider = row.get("provider_id")
            else:
                next_provider = (str(provider_id).strip()
                                 if provider_id else None)
                if next_provider and provider_validator is not None \
                        and not provider_validator(next_provider):
                    raise AgentError("Agent Provider 不存在或已停用")
            if next_scope == "bank":
                raw_workdir = ((row.get("workdir_id") or ".")
                               if workdir is _UNSET else (workdir or "."))
                path = self._resolve_workdir(raw_workdir)
                current_workdir_id = self.relative_id(path)
                raw_output = ((row.get("output_dir_id") or current_workdir_id)
                              if output_dir is _UNSET else (output_dir or current_workdir_id))
                output_path = self._resolve_workdir(raw_output)
                output_dir_id = self.relative_id(output_path)
                if current_workdir_id and not (output_dir_id == current_workdir_id
                                               or output_dir_id.startswith(current_workdir_id + "/")):
                    if output_dir is not _UNSET:
                        raise AgentError("导出目录必须位于当前工作目录内")
                    output_dir_id = current_workdir_id
            else:
                path = None
                output_dir_id = ""
                input_dir_id = ""
            workdir_id = self.relative_id(path) if path else ""
            if next_scope == "bank":
                raw_input = ((row.get("input_dir_id") or workdir_id)
                             if input_dir is _UNSET else (input_dir or workdir_id))
                input_path = self._resolve_workdir(raw_input)
                input_dir_id = self.relative_id(input_path)
            # 到这里所有字段都已验证；最后才改变内部字典，调用方不会观察到
            # “模式已切换但目录仍旧”的半状态。
            row.update(mode=next_mode, provider_id=next_provider,
                       scope=next_scope, workdir_id=workdir_id,
                       output_dir_id=output_dir_id,
                       input_dir_id=input_dir_id,
                       workdir=str(path) if path else None)
            row["updated_at"] = time.time()
            self._persist_locked()
            return self.public_session(row)

    def set_mode(self, sid: str, mode: str) -> dict:
        return self.update_session(sid, mode=mode)

    def set_provider(self, sid: str, provider_id: str | None) -> dict:
        """绑定当前对话使用的 Agent Provider；空值表示跟随全局活动配置。"""
        return self.update_session(sid, provider_id=provider_id)

    def append(self, sid: str, role: str, content: str, *,
               status: str = "complete", turn_id: str | None = None) -> dict:
        with self._lock:
            try:
                row = self.sessions[sid]
            except KeyError as exc:
                raise AgentError("Agent 会话不存在") from exc
            role = str(role or "").strip().lower()
            if role not in _MESSAGE_ROLES:
                raise AgentError("未知的 Agent 消息角色")
            status = str(status or "complete").strip().lower()
            if status not in _MESSAGE_STATUSES:
                raise AgentError("未知的 Agent 消息状态")
            message = {"role": role, "content": str(content or ""),
                       "status": status}
            if turn_id:
                message["turn_id"] = str(turn_id)
            row["messages"].append(message)
            row["updated_at"] = time.time()
            self._persist_locked()
            return self.public_session(row)

    @contextmanager
    def turn(self, sid: str):
        """为一轮模型/工具调用加锁，拒绝同一会话的交叉响应。"""
        control = self.start_turn(sid)
        try:
            yield self.get_session(sid)
        finally:
            self.finish_turn(control, "stopped" if control.cancelled else "complete")

    def start_turn(self, sid: str) -> AgentTurnControl:
        """非阻塞领取会话执行权，并返回可由其他请求取消的句柄。"""
        with self._lock:
            if sid not in self.sessions:
                raise AgentError("Agent 会话不存在")
            lock = self._turn_locks.setdefault(sid, threading.Lock())
        if not lock.acquire(blocking=False):
            raise AgentBusyError("当前对话正在处理上一条消息，请稍候")
        control = AgentTurnControl(
            id=secrets.token_urlsafe(18), session_id=sid, _turn_lock=lock)
        with self._lock:
            self._turns[control.id] = control
            # 只保留最近的有限状态供重复取消请求查询。
            if len(self._turns) > 200:
                finished = sorted(
                    (item for item in self._turns.values()
                     if item.finished_at is not None),
                    key=lambda item: item.finished_at or 0,
                )
                for item in finished[:len(self._turns) - 200]:
                    self._turns.pop(item.id, None)
        return control

    def finish_turn(self, control: AgentTurnControl, status: str) -> dict:
        """只终结一次 turn，并释放会话锁。"""
        if status not in {"complete", "stopped", "error"}:
            status = "error"
        if control.finish(status):
            lock, control._turn_lock = control._turn_lock, None
            if lock is not None:
                lock.release()
        return control.public_state()

    def cancel_turn(self, turn_id: str, sid: str | None = None) -> dict:
        """幂等取消指定 turn；可选 sid 用于阻止跨会话误取消。"""
        with self._lock:
            control = self._turns.get(str(turn_id or ""))
        if control is None:
            raise AgentError("Agent 回合不存在")
        if sid is not None and str(sid) != control.session_id:
            raise AgentError("Agent 回合不属于当前会话")
        if control.finished_at is None:
            control.cancel()
        return control.public_state()


def load_declarative_skill(path: Path) -> dict:
    """读取 JSON/YAML 之外的可执行文件一律拒绝；YAML 由调用方按需解析。"""
    if path.suffix.lower() not in {".md", ".json", ".yaml", ".yml"}:
        raise AgentError("Skill 只允许 Markdown、JSON 或 YAML 文件")
    return {"name": path.stem, "path": str(path), "status": "draft"}
