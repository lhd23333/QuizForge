# Vendor 来源记录

本目录（`vendor/project_alpha/`）的代码整体拷自外部项目 project-alpha，
不是本仓库原创。记录来源，方便以后判断"上游更新了多少、要不要重新同步"。

## 当前基线

| 项目 | 值 |
|------|----|
| 来源路径 | `D:\data\ai-coding\project-alpha` |
| 来源 commit | `9033184c898cb9a2fc321bfb02dae690fa13ffff` |
| 来源 commit 日期 | 2026-07-17 |
| 拷贝日期 | 2026-08-07（见 `../../迁移说明.md` 首条记录） |

## 拷贝了哪些文件

`src/__init__.py`、`src/config.py`、`src/deepseek_client.py`、
`src/exceptions.py`、`src/mineru_client.py`、`src/normalizer.py`、
`src/pipeline.py`、`src/validator.py`、`templates/normalize_prompt.md`、
`.env.example`。

**排除**：`main.py`（CLI 入口，本项目不用）、真实 `.env`、
`templates/exam_template.tex`（本项目有自己更完善的版本）、以及
project-alpha 自己的工具配置/文档（`.claude/`、`.obsidian/`、`README.md` 等）。

排除的具体理由见 `../../迁移说明.md` 第一条记录的第 3 节。

## 如何判断是否需要重新同步

```powershell
cd D:\data\ai-coding\project-alpha
git log --oneline 9033184c898cb9a2fc321bfb02dae690fa13ffff..HEAD
```

如果这条命令有输出，说明上游有新 commit，需要人工比对
`src/pipeline.py::run_parse()` 和 `src/normalizer.py::normalize()` 的函数
签名是否变化（`converter.py` 依赖这两个函数的具体参数），确认兼容后再
决定是否重新拷贝，并在本文件更新"当前基线"表格，同时在
`../../迁移说明.md` 追加一条新记录。
