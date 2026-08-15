# QuizForge 0.17.0-beta Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可信源码基线、固化完整验证、完成两处最小模块拆分，并生成及隔离验证 `0.17.0-beta` 安装包。

**Architecture:** 保持 Flask 单机应用、Markdown 题库和既有导出链不变。重构只增加 `import_defaults.py` 与 `export_tables.py` 两个纯逻辑模块，`app.py`/`exporter.py` 保留兼容入口；发布继续使用现有 PyInstaller/Inno Setup 和原位更新边界。

**Tech Stack:** Python 3.13、Flask、unittest、Node.js test runner、PyInstaller、Inno Setup 6、PowerShell 5.1。

## Global Constraints

- 不修改真实题库、图片或 `%LOCALAPPDATA%\QuizForge` 用户数据。
- 不更换许可证公钥、签发私钥或加密根密钥。
- 不新增第三方依赖，不重写路由或导出行为。
- 旧版升级验证只使用临时安装目录和隔离应用数据目录。
- 每个阶段必须在 fresh verification 后单独向用户报告。

---

### Task 1: 建立可信 Git 基线

**Files:**
- Modify: `.gitignore`
- Delete: `$out/`
- Delete: `_pre_sync_backup/`
- Delete: `.folder-row)})`
- Delete: `x.name.includes('undefined')).length})`
- Stage: 当前全部产品源码、测试、前端、桌面构建与文档

**Interfaces:**
- Consumes: 当前未提交的 `0.17.0-beta` 工作区
- Produces: 不含临时产物和敏感数据的 Git 基线提交

- [ ] **Step 1: 核对待删除项和仓库边界**

运行 `Get-ChildItem` 与 `git status --short`，确认四类目标分别是字体探测产物、同步前副本和 Playwright 错误输出，且目标绝对路径都位于仓库根目录内。

- [ ] **Step 2: 增加临时目录忽略规则**

在 `.gitignore` 增加 `/$out/` 与 `/_pre_sync_backup/`；异常输出文件不使用宽泛通配符，以免掩盖新的终端误写。

- [ ] **Step 3: 删除已确认临时产物**

使用经过根目录边界校验的 PowerShell `Remove-Item -LiteralPath` 删除上述目标，再次运行 `git status --short` 确认它们消失。

- [ ] **Step 4: 扫描敏感数据和发行内容**

运行：

```powershell
.venv\Scripts\python.exe tools\verify_desktop_bundle.py build\desktop\QuizForge
rg -n -i "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|api[_-]?key\s*[:=]|password\s*[:=]" `
  -g '!data/**' -g '!build/**' -g '!node_modules/**' -g '!runtime/pandoc/**'
```

预期：发行扫描成功；源码中没有真实私钥或明文凭据，测试占位符和字段名人工复核后可提交。

- [ ] **Step 5: 运行基线语法检查**

运行 `python -m py_compile` 覆盖 `CLAUDE.md` 列出的 Python 模块，并运行 Jinja 全模板加载。预期退出码均为 0。

- [ ] **Step 6: 分组暂存并审计**

先用 `git add -u` 暂存已跟踪修改，再显式暂存 `assets/ frontend/ installer/ prompts/ static/js/ templates/ tests/ tools/ vendor/` 与根目录新增产品文件；不使用 `git add -A`。运行 `git diff --cached --check`、`git status --short` 与 `git diff --cached --stat`。

- [ ] **Step 7: 提交基线**

提交信息：`重整：建立 QuizForge 0.17.0 开发基线`。

### Task 2: 固化可重复完整验证

**Files:**
- Create: `tools/verify_release.ps1`
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Test: `tests/test_desktop_product.py`

**Interfaces:**
- Consumes: Python unittest、Node test、Jinja、语法检查及 `verify_desktop_bundle.py`
- Produces: `tools/verify_release.ps1 -SkipBundleScan` 与默认完整发行验证入口，任一步失败返回非零退出码

- [ ] **Step 1: 写失败的验证入口契约测试**

在 `tests/test_desktop_product.py` 增加测试：断言 `tools/verify_release.ps1` 存在，包含 Python unittest、`npm.cmd run test:handouts`、Jinja 加载、JavaScript `node --check` 和发行扫描调用。首次运行应因脚本不存在而失败。

- [ ] **Step 2: 验证 RED**

运行新增单测，确认失败原因是缺少 `tools/verify_release.ps1`。

- [ ] **Step 3: 实现统一验证脚本**

脚本使用 `$ErrorActionPreference = 'Stop'` 和 `Assert-LastExitCode`；依次执行 Python 编译、完整 unittest、Jinja、所有一方 `static/js/*.js` 的 `node --check`、`npm.cmd run test:handouts`，默认最后扫描 `build/desktop/QuizForge`，`-SkipBundleScan` 仅用于源码阶段。

- [ ] **Step 4: 验证 GREEN 并更新说明**

运行新增单测，再在源码阶段执行 `powershell -ExecutionPolicy Bypass -File tools/verify_release.ps1 -SkipBundleScan`。把统一命令写入 `CLAUDE.md` 与 `README.md`。

- [ ] **Step 5: 提交验证入口**

提交信息：`重整：统一软件版完整验证入口`。

### Task 3: 拆分导入图片默认规则

**Files:**
- Create: `import_defaults.py`
- Modify: `app.py`
- Modify: `tests/test_core_reliability.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `qtype: str`、`body: str`、`subject: str`、`pair_applies: Callable[[str, str], bool]`、人工请求的布局值
- Produces: `import_image_defaults(...) -> tuple[str | None, list[dict], str]` 与 `import_solution_image_defaults(...) -> tuple[str | None, list[dict]]`

- [ ] **Step 1: 写失败的模块边界测试**

增加测试导入 `import_defaults`，对数学/物理、单图/多图、四图配对及人工覆盖调用新 API；同时断言 `app._import_image_defaults` 仍返回既有结果。首次运行应因模块不存在而失败。

- [ ] **Step 2: 验证 RED**

运行对应测试类，确认失败原因是 `ModuleNotFoundError: import_defaults`。

- [ ] **Step 3: 提取纯逻辑并保留兼容包装**

将规则移入 `import_defaults.py`。`app.py` 包装函数只注入 `config.BANK_SUBJECT`、`qrender.pair_applies` 和 `_QIMG_RE`，路由调用不改名。

- [ ] **Step 4: 验证 GREEN 与完整回归**

运行定向测试，再运行 `tools/verify_release.ps1 -SkipBundleScan`。预期全部通过。

- [ ] **Step 5: 更新模块职责并提交**

在 `CLAUDE.md` 模块职责中加入 `import_defaults.py`。提交信息：`重构：拆分导入图片默认规则`。

### Task 4: 拆分导出表格转换

**Files:**
- Create: `export_tables.py`
- Modify: `exporter.py`
- Modify: `qrender.py`
- Modify: `tests/test_qrender_tables.py`
- Modify: `tests/test_exporter_slides.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: HTML 表格内部文本或 Markdown 管道表格文本
- Produces: `html_table_rows`、`pipe_text_cells`、`stash_tables`、`expand_tables`；`exporter` 继续重新导出原 `_TABLE_RE`、`_PIPE_SEP_RE`、`_html_table_rows`、`_pipe_text_cells`、`_table_tex`、`_stash_tables`、`_expand_tables`

- [ ] **Step 1: 写失败的模块边界测试**

增加测试导入 `export_tables`，断言 HTML/管道表格解析和 LaTeX 令牌往返结果与现有 `exporter` API 一致，并断言兼容别名仍存在。首次运行应因模块不存在而失败。

- [ ] **Step 2: 验证 RED**

运行两个表格测试文件，确认唯一新增失败来自缺少 `export_tables`。

- [ ] **Step 3: 提取表格纯逻辑**

移动表格正则、清洗、LaTeX 渲染、令牌存取与管道表格解析到 `export_tables.py`；通过回调或模块内小函数避免反向导入 `exporter`。`exporter.py` 导入并绑定旧私有名称，`qrender.py` 改为直接读取共享模块的公开函数。

- [ ] **Step 4: 验证 GREEN 与完整回归**

运行定向测试、`tools/verify_release.ps1 -SkipBundleScan`，并比较拆分前后的代表性表格输出字节。预期一致。

- [ ] **Step 5: 更新模块职责并提交**

在 `CLAUDE.md` 模块职责中加入 `export_tables.py`。提交信息：`重构：拆分导出表格转换`。

### Task 5: 构建安装包并隔离验证旧版覆盖

**Files:**
- Modify: `README.md`
- Modify: `README.en.md`
- Modify: `../docs/STATUS.md`
- Modify: `../docs/CHANGELOG.md`
- Generate, not commit: `build/installer/QuizForge-0.17.0-beta-Setup.exe`

**Interfaces:**
- Consumes: `build_installer.ps1`、旧 `0.15.3-beta` 安装包、固定 Inno Setup `AppId`
- Produces: 新安装包、SHA-256、隔离升级验证记录、annotated tag `v0.17.0`

- [ ] **Step 1: 运行最终源码验证**

执行 `tools/verify_release.ps1 -SkipBundleScan`，预期 Python、前端、语法与模板全部通过。

- [ ] **Step 2: 构建目录版与安装包**

执行 `build_installer.ps1 -Version 0.17.0-beta -FileVersion 0.17.0.0`。预期生成目标安装包，随后执行默认 `tools/verify_release.ps1`，发行扫描通过。

- [ ] **Step 3: 检查产物**

核对安装包版本资源为 `0.17.0.0`，记录文件大小与 SHA-256；核对 `QuizForge.iss` 的固定 `AppId`、`UsePreviousAppDir=yes` 与 `ignoreversion` 覆盖语义未变化。

- [ ] **Step 4: 隔离安装旧版**

创建位于仓库 `tmp/` 下的唯一临时根，设置任务专用 `LOCALAPPDATA`，用 `0.15.3-beta` 安装包 `/VERYSILENT /DIR=<临时程序目录>` 安装；确认旧版 `QuizForge.exe` 与 `unins000.exe` 存在，并写入隔离用户数据哨兵。

- [ ] **Step 5: 用新版覆盖并验证**

用新安装包对同一临时程序目录静默覆盖。确认可执行文件版本为 `0.17.0-beta / 0.17.0.0`、卸载器仍存在、哨兵哈希不变；启动隔离程序并验证 `/healthz`，然后正常关闭。完成后删除任务创建的临时目录。

- [ ] **Step 6: 更新发行文档**

把“未生成 0.17.0-beta 安装包”更新为实际文件大小、SHA-256 与隔离升级结果；不宣称 Authenticode 或联网更新已启用。

- [ ] **Step 7: 提交发布记录并最终验证**

提交信息按项目规范使用 `QuizForge v0.17.0：稳定化与安装包发布`，再运行 `git status --short`、完整验证和安装包哈希复核。

- [ ] **Step 8: 创建 annotated tag**

运行 `git tag -a v0.17.0 -m "QuizForge v0.17.0"`，核对 tag 类型为 `tag` 且指向发布提交。不推送远端。
