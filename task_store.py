"""后台转换任务的本地 JSON 快照。

插件退出时会主动终止 Python 后端，所以任务状态不能只放在 ``app.py`` 的内存
字典里。这里仅保存可恢复的业务状态，不负责执行线程；重启后由 ``app.py`` 把
尚在 pending/converting 的任务标成中断，绝不自动重放付费 API 调用。

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
_KINDS = ("job", "batch")


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
