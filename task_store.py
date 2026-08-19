"""后台转换任务的本地 JSON 快照。

插件退出时会主动终止 Python 后端，所以任务状态不能只放在 ``app.py`` 的内存
字典里。这里仅保存可恢复的业务状态，不负责执行线程；重启后由 ``app.py`` 把
尚在 pending/converting 的 OCR 任务，以及资料库工具的在途任务标成中断，绝不
自动重放外部调用。

文件采用“临时文件 + os.replace”原子覆盖，避免 Obsidian/系统退出时只写下一半
JSON。所有入口共用一把进程锁，后台并发转换不会互相覆盖。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# 任务类型只决定快照顶层的命名空间，不改变 payload 的业务结构。资料库转换
# 任务沿用同一份原子账本，避免再引入第二个 conversion_tasks.json。
KINDS = ("job", "batch", "library")
_KINDS = KINDS


def _empty() -> dict:
    return {kind: {} for kind in _KINDS}


def _load_unlocked() -> dict:
    path = config.TASKS_PATH
    if not path.exists():
        return _empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # 损坏文件可能是唯一还带着已付费结果的副本，先改名留证再启用新账本；
        # 不能让下一次 save 直接把它覆盖掉。
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, backup)
            logger.error("转换任务快照损坏，已保留为 %s：%s", backup, exc)
        except OSError:
            logger.error("转换任务快照无法读取且无法留副本：%s", exc)
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    out = _empty()
    for kind in _KINDS:
        rows = raw.get(kind)
        if isinstance(rows, dict):
            out[kind] = rows
    return out


def _write_unlocked(data: dict) -> None:
    path = config.TASKS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    # 固定的 ``conversion_tasks.json.tmp`` 在 Windows 上会被并发任务或短暂的
    # 文件索引占用，曾让 38 卷批次中的单卷仅因保存状态失败而被标成转换失败。
    # 每次写入使用同目录唯一临时文件，并对 replace 的短暂拒绝访问做有限重试；
    # 同目录仍保证 os.replace 的原子性，最终失败时也会清掉自己的临时文件。
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.warning("转换任务临时快照清理失败：%s", tmp)


def save(kind: str, task_id: str, payload: dict) -> None:
    if kind not in _KINDS:
        raise ValueError(f"未知任务类型：{kind}")
    with _lock:
        data = _load_unlocked()
        data[kind][task_id] = {
            "updated_at": time.time(),
            "payload": payload,
        }
        _write_unlocked(data)


def delete(kind: str, task_id: str) -> None:
    if kind not in _KINDS:
        raise ValueError(f"未知任务类型：{kind}")
    with _lock:
        data = _load_unlocked()
        if data[kind].pop(task_id, None) is not None:
            _write_unlocked(data)


def load(kind: str) -> list[tuple[str, dict]]:
    if kind not in _KINDS:
        raise ValueError(f"未知任务类型：{kind}")
    with _lock:
        rows = dict(_load_unlocked()[kind])
    out = []
    for task_id, item in rows.items():
        payload = item.get("payload") if isinstance(item, dict) else None
        if isinstance(payload, dict):
            out.append((str(task_id), payload))
    return out


def mark_interrupted(
        kind: str,
        statuses,
        error: str,
        *,
        interrupted_status: str = "interrupted") -> list[tuple[str, dict]]:
    """把指定类型中仍在执行的任务标为中断并返回全部任务。

    资料库的 PDF/DOCX 操作可能在任意阶段被桌面进程终止；进程重启后不能猜测
    外部工具是否已经写出结果，更不能自动再次调用。该函数在同一把锁内完成
    “读取、修改、原子发布”，因此调用方拿到的列表对应同一份快照。除状态外，
    已存在的 ``running``/``in_flight`` 控制位也会复位，方便后续 UI 明确提供
    手动重试。``groups``/``tasks``/``items``/``operations`` 中的活动子任务
    同样会被标记，兼容资料库批量工具而不要求 task_store 了解其业务字段。
    """
    if kind not in _KINDS:
        raise ValueError(f"未知任务类型：{kind}")
    active = {
        str(value).strip().lower()
        for value in (statuses or ())
        if str(value).strip()
    }
    if not isinstance(error, str) or not error.strip():
        raise ValueError("中断原因不能为空")
    terminal_status = str(interrupted_status or "interrupted").strip()
    if not terminal_status:
        raise ValueError("中断状态不能为空")

    def interrupt_node(node: dict, now: float) -> bool:
        changed = False
        # 资料库批量操作的容器名称在不同阶段可能不同；只遍历明确的列表
        # 字段，避免误把 metadata 中的 status 当成执行状态。
        for child_key in ("groups", "tasks", "items", "operations"):
            children = node.get(child_key)
            if not isinstance(children, list):
                continue
            for child in children:
                if isinstance(child, dict) and interrupt_node(child, now):
                    changed = True

        status = str(node.get("status") or "").strip().lower()
        active_node = status in active or bool(node.get("running")) \
            or bool(node.get("in_flight"))
        if not active_node and not changed:
            return False
        node["status"] = terminal_status
        node["error"] = error
        node["interrupted_at"] = now
        if "running" in node:
            node["running"] = 0
        if "in_flight" in node:
            node["in_flight"] = False
        return True

    with _lock:
        data = _load_unlocked()
        now = time.time()
        changed = False
        for item in data[kind].values():
            payload = item.get("payload") if isinstance(item, dict) else None
            if isinstance(payload, dict) and interrupt_node(payload, now):
                changed = True
        if changed:
            _write_unlocked(data)

        out = []
        for task_id, item in data[kind].items():
            payload = item.get("payload") if isinstance(item, dict) else None
            if isinstance(payload, dict):
                # JSON 快照只包含基础类型；浅拷贝足以隔离顶层，嵌套结构仍由
                # 调用方按业务读取，避免在锁外意外修改 store 内的临时对象。
                out.append((str(task_id), dict(payload)))
        return out


def purge_expired(days: int = 7) -> list[dict]:
    """删除超龄快照并返回 payload，供调用方继续清理上传件。"""
    cutoff = time.time() - max(1, int(days)) * 86400
    removed: list[dict] = []
    changed = False
    with _lock:
        data = _load_unlocked()
        for kind in _KINDS:
            for task_id, item in list(data[kind].items()):
                updated = item.get("updated_at", 0) if isinstance(item, dict) else 0
                try:
                    expired = float(updated) < cutoff
                except (TypeError, ValueError):
                    expired = True
                if not expired:
                    continue
                payload = item.get("payload") if isinstance(item, dict) else None
                if isinstance(payload, dict):
                    removed.append(payload)
                del data[kind][task_id]
                changed = True
        if changed:
            _write_unlocked(data)
    return removed
