"""QuizForge 终端界面（textual 全屏 TUI）。

与行式 `cli.py` 共享同一核心层与会话存储，只是把渲染换成全屏面板：
顶部状态栏、中部对话滚动区、底部输入框，审批以内联弹窗完成。

启动由 `cli.py` 分发（默认走本界面，`--plain` 退回零依赖行式 REPL）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import agent_approvals  # noqa: E402
import agent_core  # noqa: E402
import agent_orchestrator  # noqa: E402
import agent_provider  # noqa: E402
import agent_services  # noqa: E402
import api_config  # noqa: E402
import cli as cli_common  # noqa: E402
import config  # noqa: E402
import filestore  # noqa: E402

HELP_TEXT = cli_common.HELP_TEXT
PERMISSION_LEVELS = cli_common.PERMISSION_LEVELS
PERMISSION_DESCRIPTIONS = cli_common.PERMISSION_DESCRIPTIONS

TOOL_ICON = {"running": "…", "done": "✓", "error": "✗",
             "awaiting_confirmation": "⏸"}


class ChoiceScreen(ModalScreen[str]):
    """通用三选一弹窗；返回所选 key（取消返回 None）。"""

    BINDINGS = [Binding("escape", "dismiss('no')", "取消", show=False)]

    def __init__(self, title: str, body: str,
                 choices: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._title, id="dialog-title")
            yield Static(self._body, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                for key, label, variant in self._choices:
                    yield Button(label, id=f"choice-{key}", variant=variant)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        choice = str(event.button.id or "").removeprefix("choice-")
        self.dismiss(choice or "no")

    def on_mount(self) -> None:
        buttons = self.query(Button)
        if buttons:
            buttons.first().focus()


class QuizForgeTui(App):
    """QuizForge 全屏终端 Agent 会话。"""

    CSS = """
    Screen { layout: vertical; }
    #status {
        height: 1; background: $panel; padding: 0 1;
        text-overflow: ellipsis;
    }
    #status.-readonly { color: $text-muted; }
    #status.-standard { color: $text; }
    #status.-full { color: $warning; text-style: bold; }
    #log { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    #live {
        height: auto; max-height: 8; padding: 0 1; color: $text-muted;
        border-top: solid $panel;
    }
    #prompt { border: none; padding: 0 1; }
    ChoiceScreen { align: center middle; }
    #dialog {
        width: 96; max-width: 95%; height: auto; max-height: 80%;
        background: $surface; border: thick $primary; padding: 1 2;
    }
    #dialog-title { text-style: bold; padding-bottom: 1; }
    #dialog-body { height: auto; max-height: 18; overflow-y: auto; }
    #dialog-buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #dialog-buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("ctrl+n", "new_session", "新会话"),
        Binding("ctrl+m", "toggle_mode", "权限循环"),
        Binding("ctrl+y", "permission_panel", "权限面板"),
        Binding("ctrl+l", "clear_log", "清屏"),
        Binding("ctrl+c", "quit", "退出", show=False),
    ]

    TITLE = "QuizForge"
    SUB_TITLE = "终端 Agent 会话"

    def __init__(self, startup) -> None:
        super().__init__()
        self.startup = startup
        self.runtime = None
        self.approvals = None
        self.sid = ""
        # 会话级权限档位，只活在本次进程内；绝不写回会话文件。
        self.permissions: dict[str, str] = {}
        self._busy = False
        self._live_text = ""
        self._pending_approvals: list[dict] = []
        self._busy_hint = ""
        self.status_text = ""

    # ---------------------------------------------------------------- 生命周期
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status")
        yield RichLog(id="log", markup=False, wrap=True, highlight=False)
        yield Static("", id="live")
        yield Input(placeholder="输入消息，或 /help 查看命令；Ctrl+Q 退出", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        filestore.init_store()
        self.runtime = agent_core.AgentRuntime(
            config.BANK_DIR, config.AGENT_SESSIONS_PATH)
        self.approvals = agent_approvals.ApprovalStore()
        agent_services.configure(runtime=self.runtime, approvals=self.approvals)
        agent_services.register_all()
        self._write(Text("QuizForge 终端会话已就绪。", style="bold"))
        self._open_session()
        self._update_status()
        self._provider_banner()
        self.query_one("#prompt", Input).focus()
        if getattr(self.startup, "danger", False):
            self._confirm_startup_danger()
        else:
            level = cli_common.parse_permission_level(
                getattr(self.startup, "permissions", None))
            if level == "read-only":
                self._set_permission("read-only")
                self._log("启动权限：只读（写操作会被拒绝）。", "dim")
            elif level == "full":
                self._confirm_startup_danger()

    # ------------------------------------------------------------------ 渲染
    def _write(self, renderable) -> None:
        self.query_one("#log", RichLog).write(renderable)

    def _log(self, message: str, style: str = "") -> None:
        self._write(Text(str(message), style=style))

    def _permission_level(self) -> str:
        return self.permissions.get(self.sid, "standard")

    def _set_permission(self, level: str) -> None:
        self.permissions[self.sid] = level
        self._update_status()

    def _update_status(self) -> None:
        if self.runtime is None or not self.sid:
            return
        provider = self._effective_provider()
        model = (f"{provider['name']}/{provider['model']}" if provider
                 else "未配置模型")
        level = self._permission_level()
        mode = {"read-only": "READ-ONLY", "standard": "standard",
                "full": "FULL ACCESS"}[level]
        try:
            session = self.runtime.get_session(self.sid)
            workdir = session.get("workdir_id") or "（题库根）"
        except agent_core.AgentError:
            workdir = "?"
        hint = f" │ {self._busy_hint}" if self._busy_hint else ""
        self.status_text = (
            f" 模型 {model} │ 权限 {mode} │ 目录 {workdir} │ 会话 {self.sid[:10]}{hint}")
        widget = self.query_one("#status", Static)
        widget.update(self.status_text)
        for name in PERMISSION_LEVELS:
            widget.remove_class(f"-{name}")
        widget.add_class(f"-{level}")

    def _set_busy_hint(self, hint: str) -> None:
        self._busy_hint = hint
        self._update_status()

    # ------------------------------------------------------------------ 会话
    def _open_session(self) -> None:
        args = self.startup
        if args.session:
            try:
                row = self.runtime.get_session(args.session)
                self.sid = row["id"]
                self._log(f"已接续会话 {self.sid}", "dim")
            except agent_core.AgentError as exc:
                self._log(f"找不到会话 {args.session}：{exc}", "red")
        elif args.resume:
            rows = [r for r in self.runtime.list_sessions() if r["scope"] == "bank"]
            if rows:
                self.sid = rows[0]["id"]
                self._log(f"已接续最近会话 {self.sid}", "dim")
        if not self.sid:
            workdir = args.workdir
            try:
                row = self.runtime.new_session(workdir=workdir, scope="bank")
            except agent_core.AgentError as exc:
                self._log(f"工作目录不可用（{exc}），改用题库根目录。", "yellow")
                row = self.runtime.new_session(workdir=None, scope="bank")
            self.sid = row["id"]
            self._log(f"已新建会话 {self.sid}｜工作目录：{row['workdir_id'] or '（题库根）'}",
                      "green")
        if args.provider:
            self._bind_provider(args.provider)

    def _effective_provider(self) -> dict | None:
        if self.runtime is None or not self.sid:
            return None
        bound = self.runtime.get_session(self.sid).get("provider_id")
        if bound:
            cfg = agent_provider.get(bound)
            if cfg is not None:
                return {"id": cfg.id, "name": cfg.name, "base_url": cfg.base_url,
                        "model": cfg.model}
        return agent_provider.describe_active()

    def _bind_provider(self, token: str) -> None:
        rows = agent_provider.list_public()
        chosen = cli_common._pick(rows, token)
        if chosen is None:
            self._log(f"没有匹配的 Provider：{token}", "red")
            return
        try:
            self.runtime.update_session(
                self.sid, provider_id=chosen["id"],
                provider_validator=cli_common._provider_enabled)
        except agent_core.AgentError as exc:
            self._log(f"切换失败：{exc}", "red")
            return
        self._log(f"当前会话模型已切换为 {chosen['name']} / {chosen['model']}", "green")
        self._update_status()

    @work(thread=True)
    def _provider_banner(self) -> None:
        if agent_provider.active() is not None:
            return
        probe = api_config.probe_magpie()
        if probe.get("available"):
            models = list(probe.get("models") or [])
            self.call_from_thread(
                self._log,
                f"检测到本机 magpie 网关可用（{probe.get('base_url')}，"
                f"{len(models)} 个模型）。输入 /provider magpie 一键创建并启用。",
                "yellow")
        else:
            self.call_from_thread(
                self._log,
                "尚未配置 Agent Provider：请在桌面版设置页添加，"
                "或先启动本机 magpie 网关后输入 /provider magpie。",
                "yellow")

    @work(exclusive=True)
    async def _confirm_startup_danger(self) -> None:
        answer = await self.push_screen_wait(ChoiceScreen(
            "完全放开权限？",
            "完全放开会跳过写操作的逐次确认，只应在你完全信任本轮任务时使用。\n"
            "删除/覆盖类操作仍会提示。",
            [("yes", "完全放开", "warning"), ("no", "取消", "default")]))
        if answer == "yes":
            self._set_permission("full")
            self._log("已完全放开权限：写操作不再逐次确认。", "yellow")

    @work(thread=True)
    def _create_magpie(self) -> None:
        probe = api_config.probe_magpie()
        if not probe.get("available"):
            self.call_from_thread(self._log, "magpie 网关不可达。", "red")
            return
        models = list(probe.get("models") or [])
        model = models[0] if models else "deepseek/deepseek-flash"
        try:
            pid = agent_provider.create(
                name="Magpie 网关", base_url=str(probe.get("base_url")),
                api_key="magpie", model=model, max_tokens=32768)
            agent_provider.set_active(pid)
        except Exception as exc:  # noqa: BLE001 - 配置失败要给用户可读原因
            self.call_from_thread(self._log, f"创建 Provider 失败：{exc}", "red")
            return
        self.call_from_thread(
            self._log,
            f"已创建并启用 Provider：Magpie 网关 / {model}"
            f"（可 /model 切换，共 {len(models)} 个模型）", "green")
        self.call_from_thread(self._update_status)

    # ------------------------------------------------------------------ 回合
    @on(Input.Submitted, "#prompt")
    def _submitted(self, event: Input.Submitted) -> None:
        text = str(event.value or "").strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._command(text)
        else:
            self._start_turn(text)

    def _start_turn(self, text: str) -> None:
        if self._busy:
            self._log("上一轮还在进行中，请稍候。", "yellow")
            return
        self._busy = True
        self._live_text = ""
        self._pending_approvals = []
        self.query_one("#live", Static).update("")
        self._write(Text(f"\n你 › {text}", style="bold cyan"))
        self._set_busy_hint("思考中…")
        self._run_turn(text)

    @work(thread=True, exclusive=True)
    def _run_turn(self, text: str) -> None:
        control = None
        status = "error"
        try:
            control = self.runtime.start_turn(self.sid)
            mode = cli_common.mode_for_level(self._permission_level())
            agent_orchestrator.run_turn(
                self.runtime, self.sid, text,
                provider_id=self.runtime.get_session(self.sid).get("provider_id"),
                approval_store=self.approvals,
                on_event=self._on_event,
                turn_control=control, stream=True,
                session_override={"mode": mode})
            status = "complete"
        except agent_core.AgentBusyError as exc:
            self.call_from_thread(self._log, str(exc), "yellow")
        except agent_orchestrator.AgentTurnCancelled:
            status = "cancelled"
            self.call_from_thread(self._log, "（已取消）", "yellow")
        except Exception as exc:  # noqa: BLE001 - 工具/编排异常要显示而不是崩界面
            self.call_from_thread(self._log, f"处理失败：{exc}", "red")
        finally:
            if control is not None:
                try:
                    self.runtime.finish_turn(control, status)
                except agent_core.AgentError:
                    pass
        self.call_from_thread(self._finish_turn)

    def _on_event(self, event: dict) -> None:
        """在回合线程里被调用；所有 UI 操作转到主线程。"""
        self.call_from_thread(self._apply_event, dict(event))

    def _apply_event(self, event: dict) -> None:
        etype = str(event.get("type") or "")
        if etype == "assistant_delta":
            delta = str(event.get("delta") or "")
            if delta:
                self._live_text += delta
                self.query_one("#live", Static).update(self._live_text[-4000:])
                self._set_busy_hint("回复中…")
        elif etype == "tool_state":
            icon = TOOL_ICON.get(str(event.get("status") or ""), "·")
            self._log(f"  {icon} {event.get('name')} ({event.get('status')})", "dim")
        elif etype == "tool":
            self._log(f"  · {event.get('name')}", "dim")
        elif etype == "approval":
            self._pending_approvals.append(event)

    def _finish_turn(self) -> None:
        self._busy = False
        self._set_busy_hint("")
        text = self._live_text.strip()
        self._live_text = ""
        self.query_one("#live", Static).update("")
        if text:
            self._write(RichMarkdown(text))
        pending = self._pending_approvals
        self._pending_approvals = []
        if pending:
            self.run_worker(self._run_approvals(pending), exclusive=False)
        self.query_one("#prompt", Input).focus()
        self._update_status()

    # ------------------------------------------------------------------ 审批
    async def _run_approvals(self, events: list[dict]) -> None:
        for event in events:
            approval = event.get("approval") or {}
            approval_id = str(approval.get("id") or "")
            if not approval_id:
                continue
            summary = str(approval.get("summary") or event.get("name") or "写入操作")
            arguments = approval.get("arguments")
            body = (json.dumps(arguments, ensure_ascii=False, indent=2)
                    if arguments else "（无参数明细）")
            answer = await self.push_screen_wait(ChoiceScreen(
                f"需要确认：{summary}", body,
                [("yes", "执行", "success"), ("no", "取消", "error"),
                 ("always", "本会话始终执行", "warning")]))
            if answer in ("yes", "always"):
                if answer == "always":
                    self._set_permission("full")
                    self._log("已完全放开权限（后续写操作不再逐次确认）。",
                              "yellow")
                outcome = await asyncio.to_thread(self._execute_approval, approval_id)
                self._log(outcome, "green" if outcome.startswith("已执行") else "red")
            else:
                await asyncio.to_thread(self._cancel_approval, approval_id)
                self._log("已取消。", "dim")

    def _execute_approval(self, approval_id: str) -> str:
        session = self.runtime.get_session(self.sid)
        try:
            self.approvals.approve(session, approval_id)
            _row, executed, result = agent_services.execute_approval(session, approval_id)
        except agent_approvals.ApprovalError as exc:
            return f"审批执行失败：{exc}"
        if executed:
            return f"已执行。{cli_common._summarize(result)}"
        return f"未执行：{result}"

    def _cancel_approval(self, approval_id: str) -> None:
        try:
            self.approvals.cancel(self.runtime.get_session(self.sid), approval_id,
                                  "用户在终端界面取消")
        except agent_approvals.ApprovalError:
            pass

    # ------------------------------------------------------------------ 命令
    def _command(self, line: str) -> None:
        command, _, arg = line[1:].partition(" ")
        command = command.strip().lower()
        arg = arg.strip()
        if command in {"quit", "exit", "q"}:
            self.exit()
        elif command == "help":
            self._log(HELP_TEXT)
        elif command == "clear":
            self.query_one("#log", RichLog).clear()
        elif command == "config":
            self._cmd_config()
        elif command == "permissions":
            self._cmd_permissions(arg)
        elif command == "mode":
            self._cmd_permissions(arg)
        elif command == "sessions":
            self._cmd_sessions(arg)
        elif command == "model":
            self._cmd_model(arg)
        elif command == "provider":
            if arg.lower() in {"magpie", ""}:
                self._log("正在探测本机 magpie 网关…", "dim")
                self._create_magpie()
            else:
                self._log("用法：/provider magpie", "yellow")
        else:
            self._log(f"未知命令 /{command}，输入 /help 查看帮助。", "yellow")

    def _cmd_config(self) -> None:
        try:
            session = self.runtime.get_session(self.sid)
        except agent_core.AgentError as exc:
            self._log(str(exc), "red")
            return
        provider = self._effective_provider()
        level = self._permission_level()
        lines = [
            f"会话 id   ：{self.sid}",
            f"工作范围  ：{session.get('scope')}",
            f"工作目录  ：{session.get('workdir_id') or '（题库根）'}",
            f"题库根    ：{self.runtime.root}",
            f"权限档位  ：{level}（{PERMISSION_DESCRIPTIONS[level]}）",
            ("模型      ：" + (f"{provider['name']} / {provider['model']}"
                               if provider else "未配置（仅本地快捷回复）")),
            f"会话存储  ：{config.AGENT_SESSIONS_PATH}",
        ]
        self._log("\n".join(lines))

    def _cmd_permissions(self, arg: str) -> None:
        arg = arg.strip()
        if not arg:
            self._permission_panel()
            return
        level = cli_common.parse_permission_level(arg)
        if level is None:
            self._log(f"未知权限档位：{arg}；可选 read-only / standard / full", "yellow")
            return
        self._apply_permission(level)

    def _apply_permission(self, level: str) -> None:
        self._set_permission(level)
        style = "yellow" if level == "full" else "green"
        self._log(f"权限档位已切换为 {level}（{PERMISSION_DESCRIPTIONS[level]}）", style)

    @work(exclusive=True)
    async def _permission_panel(self) -> None:
        """Ctrl+P / 无参 /permissions：弹出三档选择面板。"""
        current = self._permission_level()
        choices = []
        variants = {"read-only": "default", "standard": "primary",
                    "full": "warning"}
        for level in PERMISSION_LEVELS:
            label = level + ("（当前）" if level == current else "")
            choices.append((level, label, variants[level]))
        answer = await self.push_screen_wait(ChoiceScreen(
            "选择权限档位",
            "\n".join(f"{lvl}：{PERMISSION_DESCRIPTIONS[lvl]}" for lvl in PERMISSION_LEVELS),
            choices))
        if answer:
            self._apply_permission(answer)

    def _cmd_sessions(self, arg: str) -> None:
        rows = self.runtime.list_sessions()
        if not rows:
            self._log("（暂无会话）")
            return
        if arg:
            chosen = cli_common._pick(rows, arg)
            if chosen is None:
                self._log(f"没有匹配的会话：{arg}", "red")
                return
            self.sid = chosen["id"]
            self._log(f"已切换到会话 {self.sid}", "green")
            self._update_status()
            return
        lines = []
        for idx, row in enumerate(rows, 1):
            mark = "*" if row["id"] == self.sid else " "
            last = ""
            for message in reversed(row.get("messages") or []):
                if message.get("role") == "user":
                    last = str(message.get("content") or "")[:40]
                    break
            lines.append(f"{mark} {idx:2d}. {row['id']}  [{row.get('scope')}] "
                         f"{row.get('workdir_id') or '（题库根）'}  {last}")
        self._log("\n".join(lines))

    def _cmd_model(self, arg: str) -> None:
        rows = agent_provider.list_public()
        if not rows:
            self._log("（未配置 Provider；可用 /provider magpie 一键创建）")
            return
        if arg:
            self._bind_provider(arg)
            return
        provider = self._effective_provider() or {}
        lines = []
        for idx, row in enumerate(rows, 1):
            mark = "*" if row["id"] == provider.get("id") else " "
            state = "" if row.get("enabled") else "[已停用]"
            lines.append(f"{mark} {idx:2d}. {row['id']}  {row['name']} / "
                         f"{row['model']} {state}")
        self._log("\n".join(lines))

    # ------------------------------------------------------------------ 快捷键
    def action_new_session(self) -> None:
        try:
            row = self.runtime.new_session(workdir=None, scope="bank")
        except agent_core.AgentError as exc:
            self._log(str(exc), "red")
            return
        self.sid = row["id"]
        self._log(f"\n已新建会话 {self.sid}｜工作目录：（题库根）", "green")
        self._update_status()

    def action_toggle_mode(self) -> None:
        """Ctrl+M：在 read-only → standard → full 之间循环。"""
        order = list(PERMISSION_LEVELS)
        current = self._permission_level()
        nxt = order[(order.index(current) + 1) % len(order)]
        self._apply_permission(nxt)

    def action_permission_panel(self) -> None:
        """Ctrl+Y：弹出权限档位选择面板（Ctrl+P 被命令面板占用）。"""
        self._permission_panel()

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_quit(self) -> None:
        self.exit()


def run(startup) -> int:
    """由 cli.py 调用；返回进程退出码。"""
    app = QuizForgeTui(startup)
    app.run()
    return 0
