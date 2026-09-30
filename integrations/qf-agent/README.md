# QuizForge Agent（`qf`）

> 基于 [pi](https://pi.dev) 二次开发、但**独立安装**的 QuizForge 终端 agent。
> pi 提供运行时骨架（终端界面、会话、模型接入、脚本执行、权限管线），
> QuizForge 提供全部业务能力（38 个题库工具，经 MCP 暴露）。
> **对外只有一个身份：QuizForge。**

## 与通用 pi 完全隔离

`qf` 使用**自己的 agent 目录**（pi 的 `PI_CODING_AGENT_DIR`）：

```
%LOCALAPPDATA%\QuizForge\agent\        ← qf 专用，与 ~/.pi/agent 无任何共用
  settings.json     工具集（禁 bash/edit/write）/ 会话目录
  mcp.json          quizforge server（本机 venv 绝对路径 + 题库信息）
  SYSTEM.md         身份与行动准则（完整替换系统提示）
  extensions/       qf-brand（品牌）、qf-permissions（权限门）
  skills/quizforge/ 题库领域知识
  prompts/          5 个专属命令
  models.json       ← 安装时从通用 pi 一次性拷入，之后各自独立
  auth.json         ← 同上
  sessions/         只属于 qf 的会话历史
```

| | 通用 `pi` | `qf` |
|---|---|---|
| agent 目录 | `~/.pi/agent` | `%LOCALAPPDATA%\QuizForge\agent` |
| MCP | `virtuoso`、`mailbox` | 只有 `quizforge` |
| 扩展 / skill / 命令 | 你自己的 | 只有 QuizForge 的 |
| 会话历史 | 共用一套 | 独立一套 |
| 身份 | 通用编码助手 | QuizForge 题库助手 |

两者互不影响：改 qf 的东西不会动 pi，反之亦然。

## 安装

```powershell
cd <QuizForge 软件版>

# 1) 装命令（一次）：qf / qf-tui / qf-mcp / qf-bank
pwsh -File tools\install_qf_alias.ps1

# 2) 装独立 agent（会复制资源、生成 mcp.json、导入初始模型配置、清理旧的全局安装）
pwsh -File integrations\qf-agent\install.ps1

# 3) 新开终端
qf
```

重装/改题库/卸载：

```powershell
pwsh -File integrations\qf-agent\install.ps1 -SkipImport   # 不导入模型配置
pwsh -File integrations\qf-agent\install.ps1 -KeepGlobal   # 不清理 ~/.pi/agent
pwsh -File integrations\qf-agent\install.ps1 -Uninstall    # 删除独立 agent 目录（含会话）
```

## 四条命令

| 命令 | 作用 |
|---|---|
| `qf` | 进入 QuizForge agent（日常入口） |
| `qf-bank [名称\|路径]` | 查看/切换题库，然后直接进入 `qf` |
| `qf-tui` | QuizForge 自研终端界面（零外部依赖，pi 不在也能用） |
| `qf-mcp` | 只启动 MCP server（给别的 MCP 客户端用） |

## 功能清单（45 个工具）

**只读浏览（7）**：`list_folders` 目录树 / `browse_quizforge` 浏览题库·资料库·回收站·图片·讲义·识别历史 /
`search_questions` 关键词搜索 / `read_question` 读原题 / `check_duplicates` 查重 / `filter_questions` 组合筛选（题型·难度·标签·来源·星标·关键词，分页） /
`list_snapshots` 快照列表

**题库写入（10）**：`create_question` 新建 / `update_question` 改题 / `rename_question` 改名 /
`move_questions` 移动 / `create_folder` 建目录 / `tag_questions` 打标签 /
`delete_questions` 移入回收站 / `restore_question` 从回收站恢复 / `copy_questions` 复制成新题 /
`bulk_update_questions` 批量改属性

**识别导入（6）**：`start_conversion` 启动 OCR（MinerU/Doc2X × 整篇/逐块 × 机械/LLM/人工审核）/
`inspect_conversion` 看识别结果与查重 / `import_conversion` 入库 /
`diagnose_markdown` 混乱 markdown 结构诊断 / `split_preview` 切题预览 / `apply_review_fixes` 逐题修正

**导出（1）**：`export_questions` 导出 PDF / TeX / ZIP（可指定模板、解析模式、页眉页脚、分值区）

**模板（4）**：`list_templates` / `validate_template` / `preview_template`（真实编译预览）/ `enable_template`

**讲义（7）**：`list_handouts` 列出讲义 / `read_handout` 看结构与题目块 /
`create_handout` **指定题目新建讲义**（ids 点名或 query/type/difficulty/tags 筛选；
可选 A4 单栏・双栏・16:9、解析 hidden/inline/appendix、页眉页脚）/ `add_handout_questions` 追加 /
`update_handout_meta` 改版式 / `export_handout` 导出 PDF・TeX・ZIP / `delete_handout` 删除

> 讲义存在 `_handouts/` 下，题目以**快照**写入：之后改原题不影响已生成的讲义。
> 桌面版已不再提供讲义编辑器（只保留“组卷按讲义版式导出”那个导出模式），
> 讲义全部在 agent 里做。

**API 配置（8）**：`list_api_configs` / `get_api_config` / `upsert_api_config` / `set_active_api_config` /
`delete_api_config` / `test_api_config` / `list_remote_models` / `probe_magpie`

**其他（2）**：`rollback_snapshot` 回滚 / `execute_command` 受白名单限制的本机命令

## 怎么指定题库

题库信息写在**独立 agent 目录的 `mcp.json`** 里（自包含，不依赖环境变量）：

```jsonc
"env": {
  "QUIZFORGE_BANK": "D:\\path\\to\\vault",
  "QUIZFORGE_SUBJECT": "math",                          // math / physics
  "QUIZFORGE_ASSETS_DIR": "D:\\...\\QuizForge\\_assets" // 共享图片目录（与桌面版一致）
}
```

三种改法，任选其一：

```
# ① 对话里直接切（上下键选择，推荐）
/bank

# ② 命令行切换（会自动带 subject 与共享图片目录）
qf-bank                 # 列出所有题库与当前值
qf-bank 物理            # 切换并进入 qf
qf-bank 数学 -NoStart   # 只切换，下次 qf 生效
qf-bank D:\some\vault   # 直接用没登记过的路径
```

```powershell
# ③ 安装时指定
pwsh -File integrations\qf-agent\install.ps1 -Bank "D:\path\to\vault"
# 或直接编辑 %LOCALAPPDATA%\QuizForge\agent\mcp.json 的 env
```

`/bank` 与 `qf-bank` 写的是同一份 `mcp.json`，选哪个都行。
**`/bank` 切完后，当前会话要输入 `/mcp reconnect quizforge` 才生效**
（MCP server 是长驻子进程，题库是它的启动环境）；下次启动 `qf` 自动生效。

题库清单来自桌面版登记（`%LOCALAPPDATA%\QuizForge\desktop.json`），所以桌面版加过的库
`qf-bank` 直接就能选。**改完不用重启任何东西，下次 `qf` 即生效。**

> 注意 `QUIZFORGE_SUBJECT=physics` 时题库里的"填空题"在界面与导出标题上显示为"实验题"——
> 这是 QuizForge 的既定语义，切物理库时别漏了它（`qf-bank` 会自动带上）。

## 专属命令（`qf` 里输入 `/`）

| 命令 | 作用 |
|---|---|
| `/bank` | **切换题库**：上下键选择、回车确认；同时也直接写在 `mcp.json` |
| `/api` | **查看 API 配置**：列出五类用途的条目（脱敏），选中一条直接做连通性测试 |
| `/api:list` | 直接打印全部 API 配置（不弹选择器） |
| `/qf-overview` | 题库概览：目录、题数、题型/难度分布、结构异常 |
| `/qf-doctor` | 题库体检：缺解析、选项不足、空壳题、正文混解析、图片引用 |
| `/qf-check` | 查重：完全重复与高度相似，给处理建议（不擅自删） |
| `/qf-batch-edit` | 批量改属性，先列影响清单再改 |
| `/qf-export` | 按条件导出 PDF/TeX/ZIP |
| `/qf:status` | 题库 / 工作目录 / 权限档 / 模型 / 会话 |
| `/qf:brand`、`/qf:plain` | 切换品牌外观 / 临时还原默认外观 |

> 需要明文 Key/Token 的 API 配置**不在对话里做**（凭据不进上下文）：`/api` 只负责
> 查看与测试，新增/改 Key 请到桌面版设置页；本机端点（magpie / Ollama / LM Studio）
> 可以直接让 agent 用 `upsert_api_config` 建。

## 权限档（`QF_PERMISSION`）

| 档位 | 行为 |
|---|---|
| `standard`（默认） | 写操作逐次确认；可对单个工具选「本会话内全部允许此工具」 |
| `full` | 完全放开，不确认 |
| `read-only` | 任何非只读调用直接拒绝 |

非交互模式（`qf -p "..."`）**默认拒绝写操作**，要放行加 `QF_PERMISSION_NONINTERACTIVE=1`。

## 安全网

- **写前快照**：改/标签/删除/批量执行前自动快照，`rollback_snapshot` 可回滚（回滚前还建反向快照）。
- **边界校验**：题库写入只能经 QuizForge 工具；qf 没有 `bash`/`edit`/`write`，绕不过去。
  只有 `read`/`grep`/`ls` 可看文件。
- **凭据不进对话**：需要明文 API Key/Token 的配置引导去桌面版设置页；本机端点
  （magpie / Ollama / LM Studio）可用 `upsert_api_config` 直接建。

## 故障排查

| 症状 | 处理 |
|---|---|
| `qf` 说 agent 未安装 | `pwsh -File integrations\qf-agent\install.ps1` |
| `qf` 说找不到 `pi` | `npm install -g --ignore-scripts @earendil-works/pi-coding-agent` |
| `qf` 提示 `QF_ROOT is not set` | 新开终端（环境变量刚写入，老终端看不到） |
| 它自称 pi / 看不到 QF 工具 | 检查 `%LOCALAPPDATA%\QuizForge\agent\SYSTEM.md` 是否存在，`qf mcp list` 是否只有 `quizforge` |
| 题库是空的 | `qf-bank` 看当前值，切到真的有题的库 |
| 图片全部找不到 | `QUIZFORGE_ASSETS_DIR` 没设对（应与桌面版共享图片目录一致） |
| 忘了自己在哪儿 | `/qf:status` |

### 一个踩过的坑（改启动器时注意）

`qf.cmd` **不能包在 `setlocal` 里**。npm 的 `pi.cmd` shim 自己用 `SETLOCAL/ENDLOCAL`，
会把外层 `setlocal` 作用域一起弹掉，导致 `PI_CODING_AGENT_DIR` 在 node 启动前丢失，
`qf` 会静默退回通用 pi 配置（表现为"它又自称 pi 了"）。现在的做法是：
设置变量 → `call pi` → 自行恢复变量。

## 环境变量

| 变量 | 读取方 | 默认 | 说明 |
|---|---|---|---|
| `QUIZFORGE_AGENT_DIR` | `qf.cmd` | `%LOCALAPPDATA%\QuizForge\agent` | 独立 agent 目录 |
| `QF_PERMISSION` | 扩展 | `standard` | `standard` / `full` / `read-only` |
| `QF_PERMISSION_NONINTERACTIVE` | 扩展 | 未设 | `1` 允许非交互模式执行写操作 |
| `QF_ROOT` | 全局转发脚本 | — | 仓库目录，由 `install_qf_alias.ps1` 写入 |
| `QF_WORKDIR` | MCP server | 空（根） | 题库内工作目录 |
| `QF_MODE` | MCP server | `danger` | QuizForge 侧权限档（一般不用改） |

题库相关的 `QUIZFORGE_BANK` / `QUIZFORGE_SUBJECT` / `QUIZFORGE_ASSETS_DIR` 写在
`mcp.json` 的 `env` 里，不需要设成系统环境变量。
