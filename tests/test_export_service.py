"""导出任务服务的确定性回归。

不启动 worker 线程、不碰网络与 TeX：`ExportService(autostart=False)` 让每个
用例用 run_task_sync 在断言线程里同步执行，`persist=False` 避免多个用例共享
真实 conversion_tasks.json。快照往返单开一个类，显式 patch config.TASKS_PATH
到临时目录。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import exporter
import export_service
import task_store


def default_run(questions, progress, cancel):
    return str(Path("out") / "result.pdf")


def default_deliver(path):
    return {"name": "测试卷.pdf", "token": "tok-1"}


def submit_task(service, *, run=None, title="测试卷", **overrides):
    kwargs = dict(
        title=title, fmt="pdf", bank="数学题库", bank_path="D:/bank",
        payload={"fmt": "pdf", "scope": "all"},
        collect=lambda: [{"id": "q1"}, {"id": "q2"}],
        run=run or default_run,
        deliver=default_deliver,
    )
    kwargs.update(overrides)
    return service.submit(**kwargs)


class StageViewTest(unittest.TestCase):
    """阶段换算表是进度条的单一真相，改百分比先改这里。"""

    def test_known_stages_map_to_text_and_percent(self):
        self.assertEqual(export_service._stage_view("queued", None), ("排队中", 2))
        self.assertEqual(export_service._stage_view("staging", None)[1], 20)
        self.assertEqual(export_service._stage_view("verifying", None),
                         ("检查产物中", 95))
        self.assertEqual(export_service._stage_view("done", None), ("已完成", 100))

    def test_compiling_detail_reports_pass_pages_and_percent(self):
        text, percent = export_service._stage_view(
            "compiling", {"pass": 2, "passes": 2, "pages": 12})
        self.assertIn("第 2/2 遍", text)
        self.assertIn("已排出 12 页", text)
        self.assertEqual(percent, 69)

    def test_compiling_percent_stays_below_verifying(self):
        # 无论第几遍，编译阶段的百分比都不能越过 verifying(95) 与 done(100)，
        # 否则进度条会先满再退。单遍编译取基准 50。
        _text, first = export_service._stage_view(
            "compiling", {"pass": 1, "passes": 1})
        _text, last = export_service._stage_view(
            "compiling", {"pass": 8, "passes": 8})
        self.assertEqual(first, 50)
        self.assertLessEqual(last, 88)


class ExportServiceTest(unittest.TestCase):
    def make_service(self, **kwargs):
        kwargs.setdefault("persist", False)
        kwargs.setdefault("autostart", False)
        service = export_service.ExportService(**kwargs)
        self.addCleanup(service.shutdown)
        return service

    def test_sync_run_reaches_done_with_artifact_and_question_count(self):
        service = self.make_service()
        task_id = submit_task(service)
        self.assertTrue(service.run_task_sync(task_id))
        row = service.get(task_id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["percent"], 100)
        self.assertEqual(row["question_count"], 2)
        self.assertEqual(row["artifact"]["name"], "测试卷.pdf")
        self.assertEqual(row["artifact"]["token"], "tok-1")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(service.pending_count(), 0)

    def test_progress_callback_advances_stage_text_inside_run(self):
        service = self.make_service()
        holder = {}
        seen = {}

        def run(questions, progress, cancel):
            progress("compiling", {"pass": 2, "passes": 2, "pages": 7})
            # 运行中的镜像必须在回调后立刻可见（面板轮询读的就是它）
            seen.update(service.get(holder["task_id"]))
            return str(Path("out") / "result.pdf")

        holder["task_id"] = submit_task(service, run=run)
        service.run_task_sync(holder["task_id"])
        self.assertIn("第 2/2 遍", seen["stage_text"])
        self.assertIn("已排出 7 页", seen["stage_text"])
        self.assertEqual(seen["percent"], 69)
        self.assertEqual(service.get(holder["task_id"])["status"], "done")

    def test_cancel_queued_task_closes_without_running(self):
        service = self.make_service()
        ran = []
        task_id = submit_task(service, run=lambda q, p, c: ran.append(True))
        self.assertEqual(service.pending_count(), 1)
        self.assertTrue(service.cancel(task_id))
        row = service.get(task_id)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["finished_at"])
        # 已出队：同步执行入口拒绝再跑，取材/编译一次都没发生
        self.assertFalse(service.run_task_sync(task_id))
        self.assertEqual(ran, [])
        # 终态任务不能被二次取消
        self.assertFalse(service.cancel(task_id))
        self.assertEqual(service.pending_count(), 0)

    def test_cancel_while_running_wins_over_late_success(self):
        service = self.make_service()
        holder = {}

        def run(questions, progress, cancel):
            # 模拟用户在编译中点「终止」：服务乐观置终态并 set 取消位，
            # exporter 收尸后再抛 ExportCancelled。收尾逻辑不得把状态改回 done。
            self.assertTrue(service.cancel(holder["task_id"]))
            raise exporter.ExportCancelled("导出已终止")

        holder["task_id"] = submit_task(service, run=run)
        service.run_task_sync(holder["task_id"])
        row = service.get(holder["task_id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["artifact"])
        self.assertNotEqual(row["error"], "导出已终止")

    def test_run_failure_records_error_and_keeps_stage_percent(self):
        service = self.make_service()

        def run(questions, progress, cancel):
            progress("compiling", {"pass": 1, "passes": 2})
            raise RuntimeError("xelatex 返回码 1")

        task_id = submit_task(service, run=run)
        service.run_task_sync(task_id)
        row = service.get(task_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn("xelatex 返回码 1", row["error"])
        # 失败保留最后进度：进度条停在哪，用户就知道卡在哪一步
        self.assertEqual(row["percent"], 50)
        self.assertIsNone(row["artifact"])

    def test_deliver_registration_failure_keeps_done_with_raw_path(self):
        service = self.make_service()

        def deliver(path):
            raise RuntimeError("取件登记失败")

        task_id = submit_task(service, deliver=deliver)
        service.run_task_sync(task_id)
        row = service.get(task_id)
        # 产物已经真实落盘：登记失败只影响下载按钮，任务仍是完成态，
        # 面板还能用路径打开文件夹。
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["artifact"]["path"],
                         str(Path("out") / "result.pdf"))
        self.assertEqual(row["artifact"]["token"], "")

    def test_remove_only_accepts_terminal_tasks(self):
        service = self.make_service()
        holder = {}

        def run(questions, progress, cancel):
            self.assertFalse(service.remove(holder["task_id"]))
            return str(Path("out") / "result.pdf")

        holder["task_id"] = submit_task(service, run=run)
        service.run_task_sync(holder["task_id"])
        self.assertTrue(service.remove(holder["task_id"]))
        self.assertIsNone(service.get(holder["task_id"]))
        self.assertFalse(service.remove("nonexistent"))

    def test_restored_interrupted_row_is_never_replayed(self):
        service = self.make_service()
        row = {
            "title": "昨天的卷子", "fmt": "pdf", "bank": "数学题库",
            "bank_path": "D:/bank", "payload": {"fmt": "pdf", "scope": "all"},
            "status": "interrupted", "stage": "compiling",
            "stage_text": "编译中", "percent": 50, "question_count": 0,
            "created_at": 1.0, "started_at": 2.0, "finished_at": 3.0,
            "error": "", "artifact": None,
        }
        service.restore([("restored-1", row)])
        got = service.get("restored-1")
        self.assertEqual(got["status"], "interrupted")
        self.assertEqual(got["created_at"], 1.0)
        # 恢复记录没有执行闭包：不能重新排队、不能取消、不计入徽标
        self.assertEqual(service.pending_count(), 0)
        self.assertFalse(service.cancel("restored-1"))
        self.assertFalse(service.run_task_sync("restored-1"))

    def test_overview_lists_active_first_then_terminal_desc(self):
        service = self.make_service()
        submit_task(service, title="先提交的")          # 保持排队
        done_id = submit_task(service, title="会完成的")
        service.run_task_sync(done_id)
        submit_task(service, title="后提交的")          # 保持排队

        rows = service.overview()
        self.assertEqual([r["title"] for r in rows],
                         ["先提交的", "后提交的", "会完成的"])
        self.assertEqual(rows[0]["status"], "queued")
        self.assertEqual(rows[-1]["status"], "done")
        # view 行带 id，面板按 id diff
        self.assertTrue(all(r.get("id") for r in rows))


class ExportServicePersistenceTest(unittest.TestCase):
    """快照往返与重启中断口径（对齐 task_store.mark_interrupted 的调用序列）。"""

    def test_snapshot_roundtrip_marks_only_active_rows_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_path = Path(tmp) / "tasks.json"
            with mock.patch.object(config, "TASKS_PATH", tasks_path):
                service = export_service.ExportService(
                    persist=True, autostart=False)
                self.addCleanup(service.shutdown)
                done_id = submit_task(service, title="已完成的")
                service.run_task_sync(done_id)
                # 模拟被杀进程留下的在途快照行（真实场景是 running 行）
                task_store.save("export", "in-flight", {
                    "title": "被杀时的任务", "fmt": "pdf", "bank": "数学题库",
                    "bank_path": "D:/bank",
                    "payload": {"fmt": "pdf", "scope": "all"},
                    "status": "running", "stage": "compiling",
                    "stage_text": "编译中", "percent": 50,
                    "question_count": 3, "created_at": 1.0,
                    "started_at": 2.0, "finished_at": None,
                    "error": "", "artifact": None,
                })

                # 重启序列：先标中断再恢复
                rows = task_store.mark_interrupted(
                    "export", export_service.ACTIVE_STATUSES,
                    "进程重启，任务未完成")
                service2 = export_service.ExportService(
                    persist=True, autostart=False)
                self.addCleanup(service2.shutdown)
                service2.restore(rows)

                self.assertEqual(service2.get(done_id)["status"], "done")
                self.assertEqual(service2.get("in-flight")["status"],
                                 "interrupted")
                self.assertEqual(service2.pending_count(), 0)

                # 删除历史会同步清快照
                self.assertTrue(service2.remove(done_id))
                remaining = dict(task_store.load("export"))
                self.assertNotIn(done_id, remaining)
                self.assertIn("in-flight", remaining)


if __name__ == "__main__":
    unittest.main()
