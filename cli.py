#!/usr/bin/env python
"""QuizForge CLI —— 交互式 Agent 会话。

默认启动全屏终端界面（`cli_tui.py`，基于 textual）；`--plain` 回退到本文件里
零依赖的行式 REPL（未安装 textual 时也自动回退）。两种形态共享同一核心层、
同一套工具与 `data/agent_sessions.json`，因此可与桌面版互相接续会话。

只做交互式会话，**不提供一次性命令模式**（用户已明确否决）。

启动（推荐用根目录的 `qf.cmd`）：

    qf.cmd
    qf.cmd --resume
    qf.cmd --permissions full      # 完全放开写操作
    qf.cmd --permissions read-only # 只读，拒绝一切写操作
    qf.cmd --plain                 # 行式 REPL

等价于 `.venv\\Scripts\\python.exe cli.py [选项]`。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import agent_approvals  # noqa: E402
import agent_core  # noqa: E402
import agent_orchestrator  # noqa: E402
import agent_provider  # noqa: E402
import agent_services  # noqa: E402
import agent_tools  # noqa: E402
import api_config  # noqa: E402
import config  # noqa: E402
import filestore  # noqa: E402

try:  # 老版本 Windows 控制台缺少某些汉字字形时不要因编码直接崩掉
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:  # pragma: no cover - 仅在非标准流上出现
    pass

_COLOR = sys.stdout.isatty()
RESET = "\033[0m" if _COLOR else ""
DIM = "\033[2m" if _COLOR else ""
CYAN = "\033[36m" if _COLOR else ""
YELLOW = "\033[33m" if _COLOR else ""
RED = "\033[31m" if _COLOR else ""
GREEN = "\033[32m" if _COLOR else ""

HELP_TEXT = """\
可用命令（行首以 / 开头）：
  /help                    显示本帮助
  /config                  显示当前会话配置（会话、目录、模型、权限）
  /clear                   清空对话区
  /new                     新建会话
  /sessions                列出全部会话；/sessions <序号|会话id> 切换
  /model                   列出 Agent Provider；/model <序号|id> 切换
  /provider magpie         一键创建并启用本机 magpie 网关 Provider
  /permissions [档位]      查看或切换权限档位（见下）
  /mode [standard|full]    /permissions 的简写
  /quit                    退出

权限档位（只在本次进程内有效，不写入会话文件）：
  read-only   只读：拒绝一切写操作（含导出落盘与本机命令）
  standard    标准：写操作逐次确认（默认）
  full        完全放开：写操作直接执行，不再逐次确认
其余输入都会作为消息交给 Agent；标准档下写操作会先给预览并要求 y/n/always。
"""

PERMISSION_LEVELS = ("read-only", "standard", "full")
PERMISSION_DESCRIPTIONS = {
    "read-only": "只读：拒绝一切写操作（含导出落盘与本机命令）",
    "standard": "标准：写操作逐次确认（默认）",
    "full": "完全放开：写操作直接执行，不再逐次确认",
}
_LEVEL_TO_MODE = {"read-only": "readonly", "readonly": "readonly",
                  "standard": "standard", "full": "danger", "danger": "danger"}
_MODE_TO_LEVEL = {"readonly": "read-only", "standard": "standard", "danger": "full"}


def parse_permission_level(value: object) -> str | None:
    """接受 read-only / readonly / standard / full / danger，返回规范档位名。"""
    raw = str(value or "").strip().lower()
    if raw in PERMISSION_LEVELS:
        return raw
    if raw == "readonly":
        return "read-only"
    if raw == "danger":
        return "full"
    return None


def mode_for_level(level: str) -> str:
    return _LEVEL_TO_MODE.get(str(level), "standard")


def level_for_mode(mode: str) -> str:
    return _MODE_TO_LEVEL.get(str(mode), "standard")


class _TurnRenderer:
    """把流式事件渲染成简洁的终端输出。"""

    def __init__(self) -> None:
        self.started = False    # 是否已经打印过 assistant 前缀
        self.mid_line = False   # 当前光标是否停在一行中间
        self.approvals: list[dict] = []

    def _ensure_newline(self) -> None:
        if self.mid_line:
            print()
            self.mid_line = False

    def handle(self, event: dict) -> None:
        etype = str(event.get("type") or "")
        if etype == "assistant_delta":
            delta = str(event.get("delta") or "")
            if not delta:
                return
            if not self.started:
                print(f"{CYAN}助手 › {RESET}", end="", flush=True)
                self.started = True
            print(delta, end="", flush=True)
            self.mid_line = True
        elif etype == "tool_state":
            self._ensure_newline()
            name = str(event.get("name") or "工具")
            status = str(event.get("status") or "")
            icon = {"running": "…", "done": "✓", "error": "✗",
                    "awaiting_confirmation": "⏸"}.get(status, "·")
            print(f"{DIM}  {icon} {name} ({status}){RESET}")
        elif etype == "tool":
            self._ensure_newline()
            print(f"{DIM}  · {event.get('name') or '工具'}{RESET}")
        elif etype == "approval":
            self._ensure_newline()
            self.approvals.append(event)
        elif etype == "error":
            self._ensure_newline()
            print(f"{RED}  ! {event.get('message') or event}{RESET}")

    def finish(self) -> None:
        self._ensure_newline()


class QuizForgeCli:
    def __init__(self, args: argparse.Namespace) -> None:
        self.runtime: agent_core.AgentRuntime | None = None
        self.approvals: agent_approvals.ApprovalStore | None = None
        self.sid = ""
        # 会话级权限档位，只活在本次 CLI 进程内；绝不写回会话文件。
        self.permissions: dict[str, str] = {}
        self.args = args

    # ------------------------------------------------------------------ 启动
    def setup(self) -> None:
        filestore.init_store()
        self.runtime = agent_core.AgentRuntime(
            config.BANK_DIR, config.AGENT_SESSIONS_PATH)
        self.approvals = agent_approvals.ApprovalStore()
        # 与 app.py 相同的注入契约；CLI 不注入 url_builder（无 Flask 请求上下文）。
        agent_services.configure(runtime=self.runtime, approvals=self.approvals)
        agent_services.register_all()
        self._open_session()

    def _open_session(self) -> None:
        assert self.runtime is not None
        args = self.args
        if args.session:
            try:
                row = self.runtime.get_session(args.session)
            except agent_core.AgentError as exc:
                print(f"{RED}找不到会话 {args.session}：{exc}{RESET}")
                raise SystemExit(2)
            self.sid = row["id"]
            print(f"{DIM}已接续会话 {self.sid}{RESET}")
        elif args.resume:
            rows = [r for r in self.runtime.list_sessions() if r["scope"] == "bank"]
            if rows:
                self.sid = rows[0]["id"]
                print(f"{DIM}已接续最近会话 {self.sid}{RESET}")
            else:
                self.sid = self._create_session()
        else:
            self.sid = self._create_session()

        if self.args.provider:
            self._bind_provider(self.args.provider)
        level = parse_permission_level(getattr(self.args, "permissions", None))
        if getattr(self.args, "danger", False):
            level = "full"
        if level == "read-only":
            self.set_permission("read-only")
            print(f"{DIM}当前会话权限：只读（写操作会被拒绝）{RESET}")
        elif level == "full":
            warning = ("完全放开会跳过写操作的逐次确认，只应在你完全信任本轮任务时使用；"
                       "删除/覆盖类操作仍会提示。")
            print(f"{YELLOW}{warning}{RESET}")
            if sys.stdin.isatty():
                if input("确认放开权限？输入 yes 继续 › ").strip().lower() == "yes":
                    self.set_permission("full")
                    print(f"{YELLOW}已放开权限：写操作不再逐次确认。{RESET}")
                else:
                    print("未放开权限，保持标准档。")
            else:
                self.set_permission("full")

    def _create_session(self) -> str:
        assert self.runtime is not None
        workdir = self.args.workdir
        try:
            row = self.runtime.new_session(workdir=workdir, scope="bank")
        except agent_core.AgentError as exc:
            print(f"{YELLOW}工作目录不可用（{exc}），改用题库根目录。{RESET}")
            row = self.runtime.new_session(workdir=None, scope="bank")
        print(f"{GREEN}已新建会话 {row['id']}｜工作目录：{row['workdir_id'] or '（题库根）'}{RESET}")
        return row["id"]

    def _ensure_provider(self) -> None:
        """首启引导：没有 Provider 时提示 GUI 配置，或一键创建本机 magpie。"""
        if agent_provider.active() is not None:
            return
        print(f"{YELLOW}尚未配置任何 Agent Provider。{RESET}")
        probe = api_config.probe_magpie()
        if probe.get("available"):
            models = list(probe.get("models") or [])
            default = models[0] if models else "deepseek/deepseek-flash"
            print(f"检测到本机 magpie 网关可用（{probe.get('base_url')}，{len(models)} 个模型）。")
            answer = _ask("是否创建并启用「Magpie 网关」Provider？[Y/n] ", default="y")
            if answer.strip().lower() in {"", "y", "yes"}:
                chosen = default
                if len(models) > 1:
                    print("可选模型：" + "，".join(models))
                    entered = input(f"模型（回车用 {default}）› ").strip()
                    if entered:
                        chosen = entered
                pid = agent_provider.create(
                    name="Magpie 网关", base_url=str(probe.get("base_url")),
                    api_key="magpie", model=chosen, max_tokens=32768)
                agent_provider.set_active(pid)
                print(f"{GREEN}已创建并启用 Provider：Magpie 网关 / {chosen}{RESET}")
                return
        print("请先在桌面版「设置 → Agent 模型」里配置一个 Provider，或用 /model 查看现有配置。")
        print(f"{DIM}提示：本机 magpie 网关地址 http://127.0.0.1:3425/v1 无需密钥。{RESET}")

    # ------------------------------------------------------------------ 命令
    def _bind_provider(self, token: str) -> None:
        assert self.runtime is not None
        rows = agent_provider.list_public()
        chosen = _pick(rows, token)
        if chosen is None:
            print(f"{RED}没有匹配的 Provider：{token}{RESET}")
            return
        try:
            self.runtime.update_session(self.sid, provider_id=chosen["id"],
                                        provider_validator=_provider_enabled)
        except agent_core.AgentError as exc:
            print(f"{RED}切换失败：{exc}{RESET}")
            return
        print(f"{GREEN}当前会话模型已切换为 {chosen['name']} / {chosen['model']}{RESET}")

    def _effective_provider(self) -> dict | None:
        assert self.runtime is not None
        session = self.runtime.get_session(self.sid)
        bound = session.get("provider_id")
        if bound:
            cfg = agent_provider.get(bound)
            if cfg is not None:
                return {"id": cfg.id, "name": cfg.name, "base_url": cfg.base_url,
                        "model": cfg.model}
        return agent_provider.describe_active()

    def _permission_level(self) -> str:
        return self.permissions.get(self.sid, "standard")

    def _permission_mode(self) -> str:
        return mode_for_level(self._permission_level())

    def set_permission(self, level: str) -> None:
        if level not in PERMISSION_LEVELS:
            raise ValueError(f"未知权限档位：{level}")
        self.permissions[self.sid] = level

    def cmd_help(self, _arg: str) -> None:
        print(HELP_TEXT.rstrip())

    def cmd_config(self, _arg: str) -> None:
        assert self.runtime is not None
        session = self.runtime.get_session(self.sid)
        provider = self._effective_provider()
        print(f"会话 id     ：{self.sid}")
        print(f"工作范围    ：{session.get('scope')}")
        print(f"工作目录    ：{session.get('workdir_id') or '（题库根）'}")
        print(f"题库根      ：{self.runtime.root}")
        level = self._permission_level()
        print(f"权限档位    ：{level}（{PERMISSION_DESCRIPTIONS[level]}）")
        if provider:
            print(f"模型        ：{provider['name']} / {provider['model']}")
            print(f"接口地址    ：{provider['base_url']}")
        else:
            print(f"模型        ：{YELLOW}未配置（仅本地快捷回复）{RESET}")
        print(f"会话存储    ：{config.AGENT_SESSIONS_PATH}")

    def cmd_permissions(self, arg: str) -> None:
        arg = arg.strip()
        if not arg:
            current = self._permission_level()
            print(f"当前权限档位：{current}")
            for level in PERMISSION_LEVELS:
                mark = "*" if level == current else " "
                print(f"{mark} {level:<9} {PERMISSION_DESCRIPTIONS[level]}")
            print(f"{DIM}用法：/permissions read-only|standard|full{RESET}")
            return
        level = parse_permission_level(arg)
        if level is None:
            print(f"{YELLOW}未知权限档位：{arg}；可选 read-only / standard / full{RESET}")
            return
        if level == "full" and self._permission_level() != "full":
            print(f"{YELLOW}完全放开：写操作将不再逐次确认。{RESET}")
        self.set_permission(level)
        print(f"{GREEN}权限档位已切换为 {level}{RESET}")

    def cmd_mode(self, arg: str) -> None:
        """兼容旧命令：standard / danger（= full）。"""
        arg = arg.strip()
        if not arg:
            print(f"当前权限档位：{self._permission_level()}")
            return
        self.cmd_permissions(arg)

    def cmd_new(self, _arg: str) -> None:
        self.sid = self._create_session()
        print(f"{DIM}当前权限档位：{self._permission_level()}{RESET}")

    def cmd_clear(self, _arg: str) -> None:
        print("\033[2J\033[H", end="", flush=True)

    def cmd_provider(self, arg: str) -> None:
        arg = arg.strip().lower()
        if arg != "magpie":
            print("用法：/provider magpie（一键创建并启用本机 magpie 网关）")
            return
        probe = api_config.probe_magpie()
        if not probe.get("available"):
            print(f"{RED}magpie 网关不可达。{RESET}")
            return
        models = list(probe.get("models") or [])
        model = models[0] if models else "deepseek/deepseek-flash"
        try:
            pid = agent_provider.create(
                name="Magpie 网关", base_url=str(probe.get("base_url")),
                api_key="magpie", model=model, max_tokens=32768)
            agent_provider.set_active(pid)
        except Exception as exc:  # noqa: BLE001 - 配置失败要给人可读原因
            print(f"{RED}创建 Provider 失败：{exc}{RESET}")
            return
        print(f"{GREEN}已创建并启用 Provider：Magpie 网关 / {model}{RESET}")

    def cmd_sessions(self, arg: str) -> None:
        assert self.runtime is not None
        rows = self.runtime.list_sessions()
        if not rows:
            print("（暂无会话）")
            return
        arg = arg.strip()
        if arg:
            chosen = _pick(rows, arg)
            if chosen is None:
                print(f"{RED}没有匹配的会话：{arg}{RESET}")
                return
            self.sid = chosen["id"]
            print(f"{GREEN}已切换到会话 {self.sid}{RESET}")
            return
        for idx, row in enumerate(rows, 1):
            mark = "*" if row["id"] == self.sid else " "
            last = ""
            for message in reversed(row.get("messages") or []):
                if message.get("role") == "user":
                    last = str(message.get("content") or "")[:40]
                    break
            scope = row.get("scope")
            print(f"{mark} {idx:2d}. {row['id']}  [{scope}] "
                  f"{row.get('workdir_id') or '（题库根）'}  {DIM}{last}{RESET}")

    def cmd_model(self, arg: str) -> None:
        rows = agent_provider.list_public()
        if not rows:
            print("（未配置 Provider；可运行 /config 查看，或在桌面版设置页添加）")
            return
        arg = arg.strip()
        if arg:
            self._bind_provider(arg)
            return
        provider = self._effective_provider() or {}
        for idx, row in enumerate(rows, 1):
            mark = "*" if row["id"] == provider.get("id") else " "
            state = "" if row.get("enabled") else f"{YELLOW}[已停用]{RESET}"
            print(f"{mark} {idx:2d}. {row['id']}  {row['name']} / {row['model']} {state}")
        print(f"{DIM}用 /model <序号|id> 切换当前会话模型{RESET}")

    # ------------------------------------------------------------------ 轮次
    def run_turn(self, text: str) -> None:
        assert self.runtime is not None and self.approvals is not None
        control = None
        status = "error"
        renderer = _TurnRenderer()
        try:
            control = self.runtime.start_turn(self.sid)
            session_override = {"mode": self._permission_mode()}
            agent_orchestrator.run_turn(
                self.runtime, self.sid, text,
                provider_id=self.runtime.get_session(self.sid).get("provider_id"),
                approval_store=self.approvals,
                on_event=renderer.handle,
                turn_control=control, stream=True,
                session_override=session_override)
            status = "complete"
        except agent_core.AgentBusyError as exc:
            renderer.finish()
            print(f"{YELLOW}{exc}{RESET}")
            return
        except agent_orchestrator.AgentTurnCancelled as exc:
            renderer.finish()
            if exc.partial:
                print(f"{YELLOW}（已取消）{RESET}")
            status = "cancelled"
            return
        except (agent_core.AgentError, agent_tools.ToolError,
                agent_orchestrator.AgentTurnError) as exc:
            renderer.finish()
            print(f"{RED}处理失败：{exc}{RESET}")
            status = "error"
            return
        except KeyboardInterrupt:
            renderer.finish()
            print(f"{YELLOW}（已中断）{RESET}")
            status = "cancelled"
            return
        finally:
            renderer.finish()
            if control is not None:
                try:
                    self.runtime.finish_turn(control, status)
                except agent_core.AgentError:
                    pass
        self._handle_approvals(renderer.approvals)

    def _handle_approvals(self, events: list[dict]) -> None:
        assert self.runtime is not None and self.approvals is not None
        for event in events:
            approval = event.get("approval") or {}
            approval_id = str(approval.get("id") or "")
            if not approval_id:
                continue
            summary = str(approval.get("summary") or event.get("name") or "写入操作")
            print(f"\n{YELLOW}需要确认：{summary}{RESET}")
            arguments = approval.get("arguments")
            if arguments:
                print(f"{DIM}参数：{json.dumps(arguments, ensure_ascii=False)[:800]}{RESET}")
            answer = input("执行？[y=执行 / n=取消 / a=本会话后续自动执行] › ").strip().lower()
            if answer in {"a", "always"}:
                self.set_permission("full")
            if answer in {"y", "yes", "a", "always", ""}:
                self._approve_and_execute(approval_id)
            else:
                self._cancel_approval(approval_id)

    def _approve_and_execute(self, approval_id: str) -> None:
        assert self.runtime is not None and self.approvals is not None
        session = self.runtime.get_session(self.sid)
        try:
            self.approvals.approve(session, approval_id)
            _row, executed, result = agent_services.execute_approval(session, approval_id)
        except agent_approvals.ApprovalError as exc:
            print(f"{RED}审批执行失败：{exc}{RESET}")
            return
        if executed:
            print(f"{GREEN}已执行。{_summarize(result)}{RESET}")
        else:
            print(f"{RED}未执行：{result}{RESET}")

    def _cancel_approval(self, approval_id: str) -> None:
        assert self.runtime is not None and self.approvals is not None
        try:
            self.approvals.cancel(self.runtime.get_session(self.sid), approval_id,
                                  "用户在 CLI 中取消")
            print(f"{DIM}已取消。{RESET}")
        except agent_approvals.ApprovalError as exc:
            print(f"{RED}取消失败：{exc}{RESET}")

    # ------------------------------------------------------------------ REPL
    def run(self) -> None:
        assert self.runtime is not None
        print(f"\n{CYAN}QuizForge CLI{RESET} — 交互式 Agent 会话（输入 /help 查看命令，/quit 退出）")
        session = self.runtime.get_session(self.sid)
        print(f"{DIM}会话 {self.sid}｜工作目录 {session.get('workdir_id') or '（题库根）'}"
              f"｜权限 {self._permission_level()}{RESET}")
        self._ensure_provider()
        provider = self._effective_provider()
        if provider:
            print(f"{DIM}模型 {provider['name']} / {provider['model']}{RESET}")
        while True:
            try:
                line = input(f"\n{CYAN}你 › {RESET}")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                command, _, arg = text[1:].partition(" ")
                if command in {"quit", "exit", "q"}:
                    break
                handler = {
                    "help": self.cmd_help, "config": self.cmd_config,
                    "clear": self.cmd_clear, "new": self.cmd_new,
                    "mode": self.cmd_mode, "permissions": self.cmd_permissions,
                    "sessions": self.cmd_sessions, "model": self.cmd_model,
                    "provider": self.cmd_provider,
                }.get(command)
                if handler is None:
                    print(f"{YELLOW}未知命令 /{command}，输入 /help 查看帮助。{RESET}")
                else:
                    handler(arg)
                continue
            self.run_turn(text)
        print(f"{DIM}再见。{RESET}")


def _provider_enabled(provider_id: str) -> bool:
    for row in agent_provider.list_public():
        if row["id"] == str(provider_id):
            return bool(row.get("enabled"))
    return False


def _pick(rows: list[dict], token: str) -> dict | None:
    token = str(token or "").strip()
    if not token:
        return None
    if token.isdigit():
        idx = int(token)
        if 1 <= idx <= len(rows):
            return rows[idx - 1]
    for row in rows:
        if row["id"] == token:
            return row
    for row in rows:
        if str(row.get("name") or "").lower() == token.lower():
            return row
    return None


def _ask(prompt: str, *, default: str = "") -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def _summarize(result) -> str:
    if not isinstance(result, dict):
        return ""
    parts = []
    for key in ("count", "imported_ids", "folder", "filename", "job_id", "message"):
        if key in result and result[key] not in (None, "", []):
            value = result[key]
            parts.append(f"{key}={value if not isinstance(value, list) else len(value)}")
    return (" " + " ".join(parts)) if parts else ""


def _interactive_terminal() -> bool:
    """只有真正接在交互终端上才启动全屏界面；管道/重定向时回退行式 REPL。"""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:  # pragma: no cover - 某些环境无 isatty
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qf", description="QuizForge 交互式 Agent 会话（默认全屏终端界面）")
    parser.add_argument("--session", help="接续指定会话 id")
    parser.add_argument("--resume", action="store_true",
                        help="接续最近一次题库会话")
    parser.add_argument("--workdir", help="新会话的工作目录（题库内相对路径）")
    parser.add_argument("--provider", help="为新会话绑定 Agent Provider")
    parser.add_argument("--danger", action="store_true",
                        help="等价于 --permissions full：写操作不再逐次确认")
    parser.add_argument("--permissions",
                        choices=list(PERMISSION_LEVELS),
                        help="初始权限档位：read-only / standard / full")
    parser.add_argument("--plain", action="store_true",
                        help="使用零依赖的行式 REPL（不使用全屏界面）")
    args = parser.parse_args(argv)

    # 默认走全屏 TUI；装了 textual 且处在交互终端就用，否则回退行式 REPL。
    if not args.plain:
        try:
            import importlib.util
            has_textual = importlib.util.find_spec("textual") is not None
        except Exception:  # pragma: no cover - find_spec 极少失败
            has_textual = False
        if has_textual and _interactive_terminal():
            import cli_tui
            return cli_tui.run(args)
        if not has_textual:
            print("未安装 textual，使用行式界面（可装 textual 体验全屏界面）。")
        else:
            print("当前不是交互终端，使用行式界面（可在终端里直接运行以使用全屏界面）。")

    cli = QuizForgeCli(args)
    try:
        cli.setup()
        cli.run()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
