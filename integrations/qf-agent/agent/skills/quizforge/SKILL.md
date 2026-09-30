---
name: quizforge
description: 用 QuizForge 的题库工具处理数学/物理试卷：浏览与筛选题目、改写题卡、OCR 识别导入、导出 PDF/TeX、管理导出模板与 API 配置。当用户提到题库、题卡、试卷、组卷、识别导入、导出 LaTeX/PDF，或要求批量改题、查重、打标签时使用。
---

# QuizForge 题库

QuizForge 是本地单机的数学/物理题库工具链，通过 MCP server `quizforge` 暴露工具，
工具名形如 `mcp__quizforge__<tool>`（codemode 脚本里用 `<tool>`）。

## 数据模型（必须理解，否则会做错）

- 题库就是一个 Obsidian vault 目录，**每题一个 `.md` 文件**：YAML frontmatter + 正文。
- **题目身份取 frontmatter 的 `id`，文件名不参与**。用户随手改名不会改变身份，
  所以永远用 `id` 引用题目，不要用文件名或路径。
- 图片是 Obsidian 双链 `![[文件名]]`，图片文件统一放在全局 assets 目录（扁平存放），
  不是和 `.md` 同目录。
- `_assets` / `_handouts` / `_backups` / `.trash` 是保留目录，不参与题目扫描。
- 软删除只把 `.md` 移进 `.trash`，图片保留以支持恢复。

## 常用流程

1. **找题**：`filter_questions`（题型/难度/标签/来源/星标/关键词，支持分页）或
   `search_questions`。不要自己遍历目录树。
2. **看题**：`read_question`（要 `id`）。`browse_quizforge` 可以只读浏览题库、回收站、
   图片附件、讲义和识别历史。
3. **改题**：`update_question` / `tag_questions` / `bulk_update_questions` /
   `move_questions` / `copy_questions`。批量操作一次说清全部目标。
4. **OCR 导入**：`start_conversion` 启动识别（**必须先确认识别后端、导入方式、
   规范化方式**，缺参数时它会返回选项让用户挑）→ `inspect_conversion` 看预览与
   查重 → `import_conversion` 入库。
5. **清洗混乱 markdown**：`diagnose_markdown` 诊断结构（题号断档、粘连、图片引用缺失），
   `split_preview` 预览切题结果，`apply_review_fixes` 逐题修正识别结果。
6. **导出**：`export_questions`（PDF / TeX / ZIP）。可先 `list_templates` 看可用模板。
7. **回滚**：写操作执行前自动建快照；`list_snapshots` 查看，`rollback_snapshot` 恢复。

## 边界与安全

- 所有写入都会由 pi 的权限门在执行前向用户确认（`read-only` / `standard` / `full`
  三档，见 `QF_PERMISSION`）。被拒绝时不要重试同一调用，先问用户。
- **绝不要求用户把 API Key / Token 贴进对话**。需要明文凭据的配置必须引导用户到
  桌面版设置页填写；本机端点（magpie / Ollama / LM Studio）可以直接创建。
- `execute_command` 只能在工作目录白名单内执行，且同样需要确认；能用专用工具
  完成的事不要用它。
- 引用题目时给出 `id` 和所在目录；不要编造题库里没有的内容。
