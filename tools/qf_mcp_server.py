"""QuizForge MCP server —— 把 QuizForge 的操作能力挂到 MCP 客户端（pi）上。

设计要点
--------
* **只做转发**：工具清单直接来自 ``agent_tools.TOOLS``（含 JSON Schema），
  调用统一走 ``agent_tools.dispatch``；本文件不重复实现任何业务逻辑，
  也不引入第二套状态。
* **传输是 stdio**：stdout 属于 JSON-RPC 协议，任何 ``print`` 都会破坏它。
  因此启动阶段的 import 与初始化都被重定向到 stderr，诊断统一走
  ``logging``（stderr）。
* **审批归客户端**：本进程以完全放开（``danger``）运行 QuizForge 侧，写操作
  直接执行；工具按 ``agent_tools.is_write_tool`` 带上 ``destructiveHint``，
  由 pi 的 permission extension 负责向用户确认。QuizForge 的写前快照挂在
  ``agent_actions.execute_action`` 上，不受审批位置影响，照常生成。
* **单会话**：MCP 没有 QuizForge 的会话概念，本进程固定一个题库会话；
  工作目录取环境变量 ``QF_WORKDIR``（题库内相对路径，默认题库根）。

环境变量
--------
* ``QF_WORKDIR``   题库内相对目录，默认空（题库根）
* ``QF_MODE``      QuizForge 侧权限档，默认 ``danger``（= 不自行审批）
"""
from __future__ import annotations

import sys

# --------------------------------------------------------------------------
# stdout 保护：从这一刻起，直到 heavy import 结束，任何 print 都会去 stderr。
# MCP 的 stdio 传输只允许协议帧占用 stdout。
# --------------------------------------------------------------------------
_PRISTINE_STDOUT = sys.stdout
sys.stdout = sys.stderr

import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from pydantic import ConfigDict  # noqa: E402

from mcp import types  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

import agent_approvals  # noqa: E402
import agent_core  # noqa: E402
import agent_services  # noqa: E402
import agent_tools  # noqa: E402
import config  # noqa: E402
import filestore  # noqa: E402

sys.stdout = _PRISTINE_STDOUT  # heavy import 结束，把 stdout 还给协议

logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="[qf-mcp] %(levelname)s %(message)s",
)
logger = logging.getLogger("qf-mcp")

SERVER_NAME = "quizforge"

INSTRUCTIONS = """\
QuizForge 是本地单机的数学/物理题库工具链：题库是 Obsidian vault，每题一个 Markdown
文件（YAML frontmatter + 正文），图片用 `![[文件名]]` 双链引用，图片统一放在全局
assets 目录。所有操作都在本机进行，没有账号与网络服务。

工具分组：
- 只读浏览：list_folders / browse_quizforge / search_questions / read_question /
  check_duplicates / filter_questions / list_snapshots
- 题库写入：create_question / update_question / rename_question / move_questions /
  create_folder / tag_questions / delete_questions / restore_question /
  copy_questions / bulk_update_questions（写入前自动快照，可 rollback_snapshot 回滚）
- 识别导入：inspect_conversion / start_conversion / import_conversion /
  diagnose_markdown / split_preview / apply_review_fixes
- 导出：export_questions（PDF / TeX / ZIP）
- 模板：list_templates / validate_template / preview_template / enable_template
- API 配置：list_api_configs / get_api_config / upsert_api_config /
  set_active_api_config / delete_api_config / test_api_config / list_remote_models /
  probe_magpie
- 其他：execute_command（受工作目录白名单限制）

使用约定：
1. 先只读再写入。写操作会让用户确认，因此一次调用尽量把要改的东西说清楚。
2. 需要明文 API Key/Token 的配置无法在这里完成，引导用户到桌面版设置页填写；
   本机端点（如 magpie）可以直接创建。
3. 题目身份取 frontmatter 的 `id`，文件名不参与，改名不会改变身份。
4. 识别结果（OCR）先 inspect_conversion 看预览，再 import_conversion 入库。
"""


def _loose_arg_model(sample: type) -> type:
    """构造一个「照单全收」的参数模型。

    SDK 的 ``add_tool`` 按函数签名推断参数 schema，而我们的 handler 是
    ``**kwargs``，推断结果既不对也无法直接替换（``arg_model`` 会参与校验）。
    这里保留 SDK 的 ``ArgModelBase``（它提供 ``model_dump_one_level``），
    只把字段放开为 extra=allow，并让 dump 原样返回，于是 dispatch 收到的
    就是 MCP 客户端传来的原始参数。
    """
    owner = next(c for c in sample.__mro__ if "model_dump_one_level" in c.__dict__)

    def _dump_one_level(self):  # noqa: ANN001 - 与 SDK 内部签名保持一致
        return self.model_dump()

    return type(
        "QfLooseArguments",
        (owner,),
        {
            "model_config": ConfigDict(extra="allow"),
            "model_dump_one_level": _dump_one_level,
        },
    )


def _build_session() -> dict:
    workdir = str(os.environ.get("QF_WORKDIR") or "").strip().strip("/\\")
    mode = str(os.environ.get("QF_MODE") or "danger").strip().lower() or "danger"
    return {
        "id": "mcp",
        "scope": "bank",
        "workdir_id": workdir,
        "input_dir_id": workdir,
        "output_dir_id": workdir,
        "mode": mode,
        "provider_id": None,
    }


SESSION: dict = {}


def _init_runtime() -> None:
    """初始化 QuizForge 运行时；整个过程 stdout 保持重定向，避免污染协议。"""
    global SESSION
    sys.stdout = sys.stderr
    try:
        filestore.init_store()
        runtime = agent_core.AgentRuntime(config.BANK_DIR, config.AGENT_SESSIONS_PATH)
        approvals = agent_approvals.ApprovalStore()
        agent_services.configure(runtime=runtime, approvals=approvals)
        agent_services.register_all()
        SESSION = _build_session()
        logger.warning(
            "ready: bank=%s workdir=%r mode=%s",
            config.BANK_DIR, SESSION["workdir_id"], SESSION["mode"])
    finally:
        sys.stdout = _PRISTINE_STDOUT


def _call(name: str, arguments: dict) -> str:
    try:
        result = agent_tools.dispatch(name, arguments, session=SESSION)
    except agent_tools.ToolError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - 任何崩溃都要变成可读结果，不能断开连接
        logger.exception("tool %s crashed", name)
        return json.dumps(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False)
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        logger.exception("tool %s returned un-encodable result", name)
        return json.dumps(
            {"ok": False, "error": f"结果无法序列化：{exc}"}, ensure_ascii=False)


def _make_handler(name: str):
    def handler(**kwargs):
        return _call(name, dict(kwargs))

    handler.__name__ = f"qf_{name}"
    handler.__doc__ = f"调用 QuizForge 工具 {name}"
    return handler


def build_server() -> MCPServer:
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)
    manager = server._tool_manager
    for spec in agent_tools.TOOLS:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        write = agent_tools.is_write_tool(name)
        tool = manager.add_tool(
            _make_handler(name),
            name=name,
            description=str(spec.get("description") or name),
            annotations=types.ToolAnnotations(
                read_only_hint=not write,
                destructive_hint=write,
                open_world_hint=False,
            ),
            structured_output=False,
        )
        # 用 QuizForge 自己的 JSON Schema 覆盖按签名推断出来的假 schema。
        schema = spec.get("parameters") or {"type": "object", "properties": {}}
        tool.parameters = json.loads(json.dumps(schema))
        tool.fn_metadata.arg_model = _loose_arg_model(tool.fn_metadata.arg_model)
    return server


def main() -> None:
    try:
        _init_runtime()
        server = build_server()
    except Exception:  # noqa: BLE001 - 启动失败要让客户端看到原因
        logger.exception("QuizForge MCP server 启动失败")
        raise
    logger.warning("serving %d tools over stdio", len(agent_tools.TOOLS))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
