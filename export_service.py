"""导出任务的后台执行、实时进度与历史。

用户点「导出」后不再阻塞在请求里等 XeLaTeX：路由只登记任务并立即把焦点还给
题库，真正的取材 → 渲染 → 编译 → 检查在独立 worker 线程里跑，面板轮询服务
进程内的任务镜像。设计取舍：

- **实时状态放内存**（``_tasks``），JSON 快照只在状态/阶段变化时落盘。整卷
  导出动辄十几分钟、xelatex 逐页输出，进度若逐条写盘会把
  ``conversion_tasks.json`` 变成每秒重写一次的日志文件；面板与徽标读内存，
  只有重启恢复才读快照。
- **重启时在途任务一律标「中断」且绝不自动重放**：编译本身不计费，但用户
  可能已经改过配置或题集，自动补跑只会产出一份没人要的 PDF。与资料库任务
  同一口径（ADR-068）。
- **终止是「用户不要了」**：正在编译的杀进程树（exporter/tex_sandbox 的
  cancel 管线），排队中的直接出队；exporter 会把本次导出工作目录一并清掉。
- **删历史不删产物文件**：产物由 cleanup_output 在 24h 后回收，但只要任务
  记录还在（快照保留 7 天），cleanup_output 就跳过它引用的产物——「打开
  PDF」在记录有效期内必须一直可用。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import config
import exporter
import task_store

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})

# 阶段 → 展示文本与基准百分比。关键收尾阶段（compiling/verifying）由
# _stage_view 按遍数细分，不走这张表。
_STAGE_TEXT = {
    "queued": "排队中",
    "collecting": "汇总题目中",
    "staging": "整理素材中",
    "rendering": "排版渲染中",
    "compiling": "编译中",
    "verifying": "检查产物中",
}
_STAGE_PERCENT = {
    "queued": 2,
    "collecting": 6,
    "staging": 20,
    "rendering": 40,
}


def _stage_view(stage: str, detail: dict | None) -> tuple[str, int]:
    """把（阶段, 细节）换算成（展示文本, 百分比）。

    只有编译阶段带真实细节：第几遍、xelatex 已排出多少页。'已排出 N 页' 是
    整条链路上唯一来自 TeX 真实断页的进度信号，比字数估算可靠。
    """
    stage = str(stage or "queued")
    if stage == "compiling":
        d = detail if isinstance(detail, dict) else {}
        try:
            passes = max(1, int(d.get("passes") or 1))
        except (TypeError, ValueError):
            passes = 1
        try:
            idx = min(passes, max(1, int(d.get("pass") or 1)))
        except (TypeError, ValueError):
            idx = 1
        text = "编译中" if passes <= 1 else f"编译中（第 {idx}/{passes} 遍）"
        try:
            pages = max(0, int(d.get("pages") or 0))
        except (TypeError, ValueError):
            pages = 0
        if pages:
            text += f" · 已排出 {pages} 页"
        percent = 50 + int(38 * (idx - 1) / passes)
        return text, min(88, percent)
    if stage == "verifying":
        return "检查产物中", 95
    if stage == "done":
        return "已完成", 100
    return _STAGE_TEXT.get(stage, "处理中"), _STAGE_PERCENT.get(stage, 5)


@dataclass
class _Task:
    id: str
    title: str = "试卷"
    fmt: str = "pdf"
    bank: str = ""
    bank_path: str = ""
    payload: dict = field(default_factory=dict)
    # collect/run/deliver 是 app.py 提交时构造的闭包，只在本次进程内有效；
    # 重启恢复出来的记录没有它们，重试由路由按 payload 重建。
    collect: Callable | None = None
    run: Callable | None = None
    deliver: Callable | None = None
    status: str = "queued"
    stage: str = "queued"
    stage_text: str = "排队中"
    percent: int = 2
    question_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str = ""
    artifact: dict | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @classmethod
    def from_row(cls, task_id: str, row: dict) -> "_Task":
        """从持久化快照恢复展示信息（不恢复执行闭包）。"""
        task = cls(id=task_id)
        task.title = str(row.get("title") or "试卷")
        task.fmt = str(row.get("fmt") or "pdf")
        task.bank = str(row.get("bank") or "")
        task.bank_path = str(row.get("bank_path") or "")
        task.payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        task.status = str(row.get("status") or "interrupted")
        task.stage = str(row.get("stage") or task.status)
        task.stage_text = str(row.get("stage_text") or "")
        try:
            task.percent = int(row.get("percent") or 0)
        except (TypeError, ValueError):
            task.percent = 0
        try:
            task.question_count = int(row.get("question_count") or 0)
        except (TypeError, ValueError):
            task.question_count = 0
        for name in ("created_at", "started_at", "finished_at"):
            try:
                value = row.get(name)
                setattr(task, name, float(value) if value else None)
            except (TypeError, ValueError):
                setattr(task, name, None)
        task.error = str(row.get("error") or "")
        artifact = row.get("artifact")
        task.artifact = artifact if isinstance(artifact, dict) else None
        return task


class ExportService:
    """导出任务的提交、执行、取消与历史。

    persist=False 用于测试：不落快照、不与其他用例共享 conversion_tasks.json。
    autostart=False 同样给测试用：submit 后由调用方用 run_task_sync 在本线程
    执行，避免 worker 线程与断言抢同一条任务。生产保持默认（首次提交时懒启动
    EXPORT_CONCURRENCY 个 worker——启动即开线程没必要，而且 config 在测试里
    可能被替换后才生效）。
    """

    def __init__(self, *, persist: bool = True, autostart: bool = True):
        self._lock = threading.RLock()
        self._tasks: dict[str, _Task] = {}
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._persist = persist
        self._autostart = autostart

    # ------------------------------------------------------------------
    # 提交与执行
    # ------------------------------------------------------------------

    def submit(self, *, title: str, fmt: str, bank: str, bank_path: str,
               payload: dict, collect: Callable, run: Callable,
               deliver: Callable | None = None) -> str:
        """登记一个导出任务并返回 task_id。

        collect() 与 run(questions, progress, cancel) 都在 worker 线程里执行：
        取材、OCR 后处理、pandoc、xelatex 全都不属于请求线程，用户点完导出
        立刻能继续操作题库。
        """
        task = _Task(
            id=uuid.uuid4().hex[:12],
            title=title, fmt=fmt, bank=bank, bank_path=bank_path,
            payload=payload, collect=collect, run=run, deliver=deliver,
        )
        with self._lock:
            self._tasks[task.id] = task
            self._persist_locked(task)
        if self._autostart:
            self._ensure_workers()
        self._queue.put(task.id)
        return task.id

    def run_task_sync(self, task_id: str) -> bool:
        """测试用入口：不经过队列，在本线程直接执行一个排队中的任务。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "queued":
                return False
        self._run_task(task_id)
        return True

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            want = max(1, int(getattr(config, "EXPORT_CONCURRENCY", 2)))
            alive = [t for t in self._workers if t.is_alive()]
            self._workers = alive
            for _ in range(want - len(alive)):
                thread = threading.Thread(target=self._worker,
                                          name="qf-export-task", daemon=True)
                self._workers.append(thread)
                thread.start()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                task_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_task(task_id)
            except Exception:
                # worker 崩溃会吞掉之后所有导出；只记日志保住循环。
                logger.exception("导出任务执行器异常")
            finally:
                self._queue.task_done()

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            # 只认排队中：取消已把它置成终态，或恢复记录根本不是活任务。
            if task is None or task.status != "queued":
                return
            if task.cancel.is_set():
                self._finish_locked(task, "cancelled", "")
                return
            task.status = "running"
            task.started_at = time.time()
            self._set_stage_locked(task, "collecting", None)
            self._persist_locked(task)

        try:
            questions = task.collect() if task.collect is not None else []
            with self._lock:
                if task.status != "running":
                    return
                task.question_count = len(questions)
                if task.cancel.is_set():
                    self._finish_locked(task, "cancelled", "")
                    return
                # 取材完成即进入素材整理；后续阶段由 exporter 的 progress
                # 回调推进（staging/rendering/compiling/verifying）。
                self._set_stage_locked(task, "staging", None)
                self._persist_locked(task)
            path = task.run(questions, self._progress(task_id), task.cancel)
            with self._lock:
                if task.status != "running":
                    return
                task.artifact = self._deliver_locked(task, path)
                self._persist_locked(task)
                self._finish_locked(task, "done", "")
                self._persist_locked(task)
        except exporter.ExportCancelled:
            with self._lock:
                if task.status == "running":
                    self._finish_locked(task, "cancelled", "")
                    self._persist_locked(task)
        except Exception as exc:
            logger.exception("导出任务失败：%s", task_id)
            with self._lock:
                if task.status == "running":
                    self._finish_locked(task, "failed", str(exc)[:2000] or "导出失败")
                    self._persist_locked(task)
        finally:
            # 闭包可能捕获整批题目 dict；任务结束后及时释放，长会话不积内存。
            with self._lock:
                task.collect = None
                task.run = None

    def _deliver_locked(self, task: _Task, path) -> dict:
        """登记产物取件号，得到面板可直接用的 artifact 描述。"""
        resolved = Path(path)
        artifact = {"path": str(resolved), "name": resolved.name, "token": ""}
        if task.deliver is not None:
            try:
                extra = task.deliver(resolved)
                if isinstance(extra, dict):
                    artifact.update(extra)
            except Exception:
                logger.exception("导出产物登记失败：%s", task.id)
        return artifact

    def _progress(self, task_id: str) -> Callable:
        """返回给 exporter 的进度回调：改内存 + 只在阶段变化时落盘。"""

        def report(stage: str, detail: dict | None = None) -> None:
            with self._lock:
                task = self._tasks.get(task_id)
                if task is None or task.status != "running":
                    return
                changed = str(stage or "") != task.stage
                self._set_stage_locked(task, stage, detail)
                if changed:
                    self._persist_locked(task)

        return report

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------

    def cancel(self, task_id: str) -> bool:
        """终止任务。排队中的立即出队；运行中的置取消标记杀编译进程。

        运行中的任务这里就直接把状态置成「已终止」（乐观更新）：xelatex 被杀
        还要一两秒，面板不该等进程收尸才变色；worker 的收尾逻辑看到状态已不是
        running 也不会把它覆写回完成。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status not in ACTIVE_STATUSES:
                return False
            task.cancel.set()
            if task.status == "queued":
                self._finish_locked(task, "cancelled", "")
            else:
                task.status = "cancelled"
                task.error = ""
                task.finished_at = time.time()
                task.stage_text = (f"{task.stage_text} · 已终止"
                                   if task.stage_text else "已终止")
            self._persist_locked(task)
        return True

    def remove(self, task_id: str) -> bool:
        """删除任务历史（仅终态）。只删记录，不动磁盘上的产物文件。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status not in TERMINAL_STATUSES:
                return False
            self._tasks.pop(task_id, None)
        if self._persist:
            try:
                task_store.delete("export", task_id)
            except Exception:
                logger.exception("导出任务快照删除失败：%s", task_id)
        return True

    def pending_count(self) -> int:
        """导航徽标计数：排队中 + 进行中的导出任务。"""
        with self._lock:
            return sum(1 for task in self._tasks.values()
                       if task.status in ACTIVE_STATUSES)

    # ------------------------------------------------------------------
    # 查询与恢复
    # ------------------------------------------------------------------

    def overview(self) -> list[dict]:
        """面板数据：未完成在前（先来先做），终态按结束时间倒序。"""
        with self._lock:
            tasks = list(self._tasks.values())
        active = sorted((t for t in tasks if t.status in ACTIVE_STATUSES),
                        key=lambda t: t.created_at)
        finished = sorted((t for t in tasks if t.status in TERMINAL_STATUSES),
                          key=lambda t: t.finished_at or t.created_at,
                          reverse=True)
        with self._lock:
            return [self._view_locked(t) for t in active + finished]

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return self._view_locked(task) if task is not None else None

    def restore(self, rows) -> None:
        """载入重启前的快照（调用前应已由 task_store.mark_interrupted 标中断）。"""
        with self._lock:
            for task_id, row in rows:
                if task_id in self._tasks or not isinstance(row, dict):
                    continue
                self._tasks[str(task_id)] = _Task.from_row(str(task_id), row)

    def shutdown(self) -> None:
        """停掉 worker（测试与退出用）；不影响队列里已登记的任务记录。"""
        self._stop.set()
        for thread in list(self._workers):
            thread.join(timeout=1)
        self._workers = []

    # ------------------------------------------------------------------
    # 内部：状态与序列化
    # ------------------------------------------------------------------

    def _set_stage_locked(self, task: _Task, stage: str, detail) -> None:
        task.stage = str(stage or task.stage)
        task.stage_text, task.percent = _stage_view(task.stage, detail)

    def _finish_locked(self, task: _Task, status: str, error: str) -> None:
        task.status = status
        task.error = error or ""
        task.finished_at = time.time()
        if status == "done":
            task.stage = "done"
            task.stage_text, task.percent = "已完成", 100
        elif status == "cancelled":
            task.stage_text = "已终止"
        elif status == "failed":
            task.stage_text = "失败"
        # 失败/终止保留最后进度百分比：进度条停在哪，用户就知道卡在哪一步。

    def _row_locked(self, task: _Task) -> dict:
        """持久化行：只留基础类型与展示字段，逐页进度细节刻意不进快照。"""
        return {
            "title": task.title, "fmt": task.fmt, "bank": task.bank,
            "bank_path": task.bank_path, "payload": task.payload,
            "status": task.status, "stage": task.stage,
            "stage_text": task.stage_text, "percent": task.percent,
            "question_count": task.question_count,
            "created_at": task.created_at, "started_at": task.started_at,
            "finished_at": task.finished_at, "error": task.error,
            "artifact": task.artifact,
        }

    def _view_locked(self, task: _Task) -> dict:
        row = self._row_locked(task)
        row["id"] = task.id
        return row

    def _persist_locked(self, task: _Task) -> None:
        if not self._persist:
            return
        try:
            task_store.save("export", task.id, self._row_locked(task))
        except Exception:
            # 快照写失败不能杀死导出：任务的真实状态在内存与产物文件里，
            # 丢的只是重启恢复能力。
            logger.exception("导出任务快照保存失败：%s", task.id)
