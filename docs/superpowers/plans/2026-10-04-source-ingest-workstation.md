# 好题好卷好资料导入工作站实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 为 QuizForge 增加三个可持续监听的内容导入 Profile，把稳定的好题、好卷、好资料文件转换为永久保留、可预览和可组卷的独立题卡版本。

**架构：** 在现有 `converter.py`、切块链路、`filestore.py` 和 `task_store.py` 之上增加统一监听器、Profile 适配器、版本登记和 Windows 系统回收站事务层。监听器只负责发现稳定文件和调度；Profile 负责内容语义；版本层负责递增命名和幂等提交；Flask、桌面 GUI、Agent 与交互式 CLI 共用这些无 Flask 业务服务。

**技术栈：** Python 3、标准库 `pathlib`/`threading`/`hashlib`/`ctypes`、现有 Flask/Jinja、现有 MinerU/Doc2X 转换链路、现有 unittest 测试体系；不新增运行时依赖。

**规范：** `docs/superpowers/specs/2026-10-04-source-ingest-workstation-design.md`

## 全局约束

- 默认目录为当前题库根目录下的 `好题`、`好卷`、`好资料`，每个 Profile 默认输入目录等于输出目录。
- 六个目录可自定义到其他本地磁盘普通目录；拒绝网络 UNC 路径、不可验证路径和符号链接越界。
- 监听默认关闭；只有用户显式打开对应 Profile 开关后才调用 OCR。
- 文件至少连续两个检查周期大小和 `mtime_ns` 不变且可共享读取后才入队；转换前再次计算 SHA-256。
- 版本成功后永久保留；基础名之后使用 `_2`、`_3`……，不覆盖旧结果，失败重试不占版本号。
- 输出完整校验成功后才将源文件发送到 Windows 系统回收站；不直接 `unlink`，回收失败进入 `recycle_pending`。
- 同一输入路径、Profile 和内容哈希不重复转换；内容变化生成新的独立版本。
- 好题识别主题号并追加 `第N题`，只删除正文开头主题号，小题号和选项号原样保留。
- 好卷按源文件名建立版本目录，目录内每题独立成卡并清理卷首语、页眉、页脚和页码噪声。
- 好资料每个正文块、题目块、解析块独立成卡，保留顺序；页码只放 frontmatter 和预览侧边。
- 不把外部目录自动纳入普通题库扫描；不把 API Key、Token 或完整凭据写入任务快照、版本登记或日志。
- 不执行 `update_installed.ps1 -DirectBundle`、安装包构建、版本 tag 或发布，除非用户另行明确授权。

## Review Focus

- 输入目录与输出目录重叠时，新生成的 Markdown、目录和临时文件不能再次被监听器当成输入；由 Task 4 的重叠目录扫描测试固定。
- Windows 文件仍被写入或占用时不能提前调用 OCR；由 Task 4 的稳定性和共享读取测试固定。
- 输出已经提交但系统回收站调用失败时不能重复生成版本；由 Task 2 的 `recycle_pending` 幂等测试固定。
- 好题正文包含 `(1)`、`（2）`、A-D 选项或正文数字时只能删除主题号；由 Task 3 的命名与正文保留测试固定。
- 进程在外部 OCR 调用期间退出后不能自动重放付费调用；由 Task 1/4 的中断快照恢复测试固定。

## 文件结构与职责

- `source_settings.py`：三个 Profile 的目录与开关配置、默认目录、普通本地路径验证和原子 JSON 存储。
- `source_versions.py`：输入指纹、版本注册表、递增序号、输出清单校验、状态提交和并发锁。
- `source_recycle.py`：Windows Shell 回收站调用；非 Windows 环境提供可测试的明确错误。
- `source_profiles.py`：好题、好卷、好资料的转换适配器、命名和来源 frontmatter。
- `source_ingest.py`：统一扫描、稳定性检查、队列、工作线程、暂停／恢复／重试和任务快照接线。
- `task_store.py`：扩展新的 `source` 任务命名空间，不改变现有 `job`、`batch`、`library` 快照结构。
- `config.py`：新增题库状态级的自动导入设置、版本登记和临时工作区路径常量。
- `app.py`：注册服务生命周期、只读状态与受保护写 API；不实现 Profile 细节。
- `templates/settings.html`、`static/js/settings-page.js`、必要的 CSS：设置页的三个 Profile 配置与任务状态 UI。
- `agent_tools.py`、`agent_actions.py`、`agent_services.py`：为 GUI/CLI/Agent 暴露统一的读取、配置、控制、重试和版本预览能力。
- `tests/test_source_settings.py`、`tests/test_source_versions.py`、`tests/test_source_profiles.py`、`tests/test_source_ingest.py`、`tests/test_source_routes.py`：按边界拆分单元和集成回归。
- `docs/PRODUCT.md`、`CHANGELOG.md`、`docs/STATUS.md`：源码验证完成后同步用户可感知能力和实际验证证据。

### Task 1: 配置、任务快照与版本登记基础

**文件：**
- 创建：`source_settings.py`
- 创建：`source_versions.py`
- 修改：`config.py`
- 修改：`task_store.py`
- 测试：`tests/test_source_settings.py`、`tests/test_source_versions.py`、`tests/test_task_store.py`

**接口：**
- `source_settings.load_profiles() -> dict[str, dict]`
- `source_settings.validate_local_directory(raw: str | Path, *, create: bool = True) -> Path`
- `source_settings.save_profile(name: str, *, input_dir: str | Path, output_dir: str | Path, enabled: bool) -> dict`
- `source_settings.reset_profile(name: str) -> dict`
- `source_versions.sha256_file(path: Path) -> str`
- `source_versions.version_key(profile: str, source_path: Path, source_hash: str) -> str`
- `source_versions.reserve_version(output_dir: Path, base_name: str, *, is_directory: bool, source_key: str) -> dict`
- `source_versions.commit_version(record: dict, manifest: dict) -> dict`
- `source_versions.mark_recycle_pending(version_id: str, error: str) -> dict`
- `source_versions.list_versions(profile: str | None = None) -> list[dict]`

- [ ] **步骤 1：先写配置与版本的失败测试**
  - 断言默认目录为 `BANK_DIR/好题`、`BANK_DIR/好卷`、`BANK_DIR/好资料`，默认输出等于输入且 `enabled=False`。
  - 断言自定义本地盘路径可创建并持久化；UNC、文件路径、越界符号链接被拒绝。
  - 断言同一输入哈希只能命中一次；内容哈希变化可创建新版本。
  - 断言并发保留序号得到 `名称`、`名称_2`、`名称_3`，不覆盖既有路径。
  - 断言 `task_store.load("source")` 与现有命名空间并存，旧快照读取不变。
- [ ] **步骤 2：运行失败测试确认边界未实现**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_settings tests.test_source_versions -v`
  - 预期：因模块／接口尚不存在而失败。
- [ ] **步骤 3：实现配置与版本基础**
  - 在 `config.py` 增加题库状态级 `SOURCE_SETTINGS_PATH`、`SOURCE_VERSIONS_PATH`、`SOURCE_WORKSPACE_DIR`。
  - 用临时文件 + `os.replace` 原子保存配置与版本 JSON；所有路径先 `expanduser`、绝对化、检查普通目录和符号链接边界。
  - 版本序号和 source key 在同一进程锁内分配；只在 `commit_version` 后把记录标记为已提交。
  - 在 `task_store.KINDS` 增加 `source`，保持旧 JSON 缺字段时自动补空命名空间。
- [ ] **步骤 4：运行测试确认通过**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_settings tests.test_source_versions tests.test_task_store -v`
  - 预期：新增测试和既有 task store 测试全部通过。
- [ ] **步骤 5：提交基础层**
  - `git add config.py task_store.py source_settings.py source_versions.py tests/test_source_settings.py tests/test_source_versions.py tests/test_task_store.py`
  - `git commit -m "feat: add source ingest settings and version registry"`

### Task 2: Windows 系统回收站与输出清单事务

**文件：**
- 创建：`source_recycle.py`
- 修改：`source_versions.py`
- 测试：`tests/test_source_recycle.py`、`tests/test_source_versions.py`

**接口：**
- `source_recycle.send_to_recycle_bin(path: Path) -> None`
- `source_recycle.can_recycle(path: Path) -> bool`
- `source_versions.validate_manifest(manifest: dict) -> dict`
- `source_versions.commit_outputs(record: dict, staged_output: Path, manifest: dict) -> dict`
- `source_versions.retry_recycle(version_id: str) -> dict`

- [ ] **步骤 1：写失败测试**
  - mock Windows Shell API，断言只接受存在的普通源文件、使用回收标记而非直接删除。
  - 断言非 Windows 或 Shell 返回错误时保留源文件并进入 `recycle_pending`。
  - 断言输出清单中的 Markdown、图片和哈希全部有效才能提交；缺失引用拒绝提交。
  - 断言 `retry_recycle` 可重复调用且不会重新生成版本。
- [ ] **步骤 2：运行失败测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_recycle -v`
  - 预期：模块或接口未实现导致失败。
- [ ] **步骤 3：实现回收站适配与事务提交**
  - Windows 使用 `ctypes` 调用 Shell 回收操作并设置 `FOF_ALLOWUNDO`；不引入 `send2trash` 依赖。
  - `commit_outputs` 先校验 staged manifest，再将完整目录／文件原子移动到已保留的递增目标路径，最后调用回收站；异常时保留源文件并记录状态。
  - 输出已提交而回收失败只更新状态，不再次分配序号。
- [ ] **步骤 4：运行测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_recycle tests.test_source_versions -v`
  - 预期：全部通过。
- [ ] **步骤 5：提交事务层**
  - `git add source_recycle.py source_versions.py tests/test_source_recycle.py tests/test_source_versions.py`
  - `git commit -m "feat: commit source outputs with recycle-bin lifecycle"`

### Task 3: 三种 Profile、命名和资料来源元数据

**文件：**
- 创建：`source_profiles.py`
- 修改：`converter.py`（只增加可复用的受控适配入口，避免改变旧调用契约）
- 测试：`tests/test_source_profiles.py`

**接口：**
- `source_profiles.convert_good_question(source: Path, workspace: Path, *, ocr_backend: str, engine: str) -> dict`
- `source_profiles.convert_good_paper(source: Path, workspace: Path, *, ocr_backend: str, engine: str) -> dict`
- `source_profiles.convert_good_material(source: Path, workspace: Path, *, ocr_backend: str, engine: str) -> dict`
- `source_profiles.build_output_manifest(profile: str, source: Path, staged_root: Path) -> dict`
- `source_profiles.extract_question_number(text: str) -> tuple[int | None, str]`
- `source_profiles.sanitize_output_name(raw: str, suffix: str = "") -> str`

- [ ] **步骤 1：写失败测试**
  - 好题：`圆梦杯.png` 识别正文 `11. 已知函数\n（1）求...\nA. ...` 后生成基础名 `圆梦杯第11题`，正文只移除 `11.`，保留 `（1）` 和 `A.`。
  - 好题：无法确定主题号时保留 `圆梦杯` 并生成待校对告警。
  - 好卷：多题生成 `圆梦杯/圆梦杯第1题.md`、`圆梦杯第2题.md`，并写入来源清单。
  - 好资料：正文、题目、解析各为独立卡，顺序、`source_block_type`、页码和 `source_version_id` 完整。
  - Windows 非法字符、保留名和超长文件名可安全收敛。
- [ ] **步骤 2：运行失败测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_profiles -v`
  - 预期：模块或接口未实现导致失败。
- [ ] **步骤 3：实现 Profile 适配器**
  - 复用 `converter` 的统一 OCR 接口和现有切块／规范化逻辑；Profile 只负责清噪、块语义、命名、frontmatter 和 manifest。
  - 好题只接受单题结果，多题或质量门失败进入人工校对告警，不猜测拆分。
  - 好卷写入版本目录，显式处理卷首语、页眉、页脚和页码噪声。
  - 好资料为每个块写 `qf_source_kind`、`source_document_id`、`source_version_id`、`source_block_id`、`source_block_index`、`source_block_type`、`source_page_start`、`source_page_end` 和 `source_order`。
- [ ] **步骤 4：运行测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_profiles tests.test_converter_boundaries -v`
  - 预期：Profile 测试和现有 converter 边界测试全部通过。
- [ ] **步骤 5：提交 Profile 层**
  - `git add source_profiles.py converter.py tests/test_source_profiles.py`
  - `git commit -m "feat: add good question paper and material profiles"`

### Task 4: 统一监听器、稳定文件和可恢复任务

**文件：**
- 创建：`source_ingest.py`
- 修改：`task_store.py`（仅增加 source payload 辅助字段／清理兼容）
- 测试：`tests/test_source_ingest.py`

**接口：**
- `source_ingest.SourceIngestService.start() -> None`
- `source_ingest.SourceIngestService.stop() -> None`
- `source_ingest.SourceIngestService.scan_once() -> list[dict]`
- `source_ingest.SourceIngestService.pause(profile: str) -> None`
- `source_ingest.SourceIngestService.resume(profile: str) -> None`
- `source_ingest.SourceIngestService.retry(task_id: str) -> dict`
- `source_ingest.is_stable_file(path: Path, previous: dict | None, *, now: float) -> tuple[bool, dict]`

- [ ] **步骤 1：写失败测试**
  - 文件第一次出现为 `pending`；连续两个周期大小和 `mtime_ns` 不变、可共享读取后才 `queued`。
  - 文件正在写入、被占用、扩展名不支持或位于输出／隐藏／临时目录时不入队。
  - 同一 `profile + path + sha256` 只产生一个活动任务；内容修改后产生新任务。
  - 失败任务可重试；`converting` 进程中断后恢复为 `interrupted`，不自动再次调用 OCR。
  - 并发任务按输出序号锁提交且不重复。
- [ ] **步骤 2：运行失败测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_ingest -v`
  - 预期：模块或接口未实现导致失败。
- [ ] **步骤 3：实现扫描与工作线程**
  - 使用周期扫描作为可靠底线；每个 Profile 共享统一队列但保留独立暂停开关。
  - 工作线程为每个任务创建 `raw/`、`assets/`、`output/`、`manifest.json`、`report.json` 工作区，调用 Task 3 Profile，再调用 Task 2 提交事务。
  - 转换前重新 hash；异常只标记失败并保留源文件；重启恢复只标记中断，不自动重放 OCR。
  - 排除规范化后的输出树，防止默认原地目录回扫产物。
- [ ] **步骤 4：运行测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_ingest tests.test_task_store -v`
  - 预期：全部通过。
- [ ] **步骤 5：提交监听层**
  - `git add source_ingest.py task_store.py tests/test_source_ingest.py`
  - `git commit -m "feat: add stable source directory watcher"`

### Task 5：Flask 设置、状态 API 和设置页

**文件：**
- 修改：`app.py`
- 修改：`templates/settings.html`
- 修改：`static/js/settings-page.js`
- 必要时修改：`static/style.css`、`base.html`
- 测试：`tests/test_source_routes.py`

**接口：**
- `GET /api/source-ingest/config`
- `POST /api/source-ingest/config`
- `POST /api/source-ingest/control`
- `GET /api/source-ingest/tasks`
- `GET /api/source-ingest/versions`
- `POST /api/source-ingest/retry/<task_id>`
- `POST /api/source-ingest/recycle/<version_id>`

- [ ] **步骤 1：写失败路由和页面测试**
  - 断言 GET 返回三个 Profile 的输入／输出目录、开关、队列和任务计数，但不返回任何凭据。
  - 断言目录写入、监听启停、暂停／恢复、转换重试和回收重试都要求现有 CSRF 写令牌。
  - 断言非法目录、越界路径和未知 Profile 返回结构化错误。
  - 断言设置页包含三个 Profile 的目录控件、开关、状态、重试和打开目录入口。
- [ ] **步骤 2：运行失败测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_routes -v`
  - 预期：路由或页面控件未实现导致失败。
- [ ] **步骤 3：实现路由与页面接线**
  - app 层只调用 `source_settings` 和 `SourceIngestService`，不复制版本／Profile 逻辑。
  - 应用启动时创建服务对象但不启动任何默认监听；显式打开开关后启动，关闭时安全停止。
  - 页面按现有设置分类、CSRF、静态版本号和响应式控件约定接入。
- [ ] **步骤 4：运行测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_routes tests.test_core_reliability -v`
  - 预期：新增路由与核心设置回归全部通过。
- [ ] **步骤 5：提交 UI/API 层**
  - `git add app.py templates/settings.html static/js/settings-page.js static/style.css base.html tests/test_source_routes.py`
  - `git commit -m "feat: add source ingest settings and task controls"`

### Task 6：Agent、交互式 CLI 和应用生命周期

**文件：**
- 修改：`agent_tools.py`
- 修改：`agent_actions.py`
- 修改：`agent_services.py`
- 修改：`cli.py`、`cli_tui.py`（仅复用已有 Agent 工具展示，不复制业务逻辑）
- 修改：`app.py`、`desktop.py`（安全启动／停止生命周期）
- 测试：`tests/test_source_agent_tools.py`、`tests/test_cli.py`

**接口：**
- `list_source_profiles() -> dict`
- `update_source_profile(profile: str, input_dir: str, output_dir: str, enabled: bool) -> dict`
- `control_source_ingest(action: str, profile: str | None = None) -> dict`
- `list_source_tasks(profile: str | None = None, status: str | None = None) -> list[dict]`
- `list_source_versions(profile: str | None = None) -> list[dict]`
- `retry_source_task(task_id: str) -> dict`
- `retry_source_recycle(version_id: str) -> dict`

- [ ] **步骤 1：写失败工具测试**
  - 断言所有工具暴露 read/write 和 destructive 元数据，目录写入、启停、重试经过现有审批。
  - 断言 Agent、CLI 和桌面调用得到同一份任务／版本状态，不各自维护缓存。
  - 断言应用停止时监听线程退出，正在 OCR 的任务按既有规则标记中断。
- [ ] **步骤 2：运行失败测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_agent_tools tests.test_cli -v`
  - 预期：工具尚不存在导致失败。
- [ ] **步骤 3：实现共享工具与生命周期接线**
  - 所有工具通过 `agent_services` 调用 `source_*` 模块，保留写前快照、审批和审计。
  - 桌面进程启动服务对象，服务只在 Profile enabled 时启动；进程结束先 stop，再标记活动任务中断。
  - CLI 只增加交互式命令或工具展示，不引入一次性命令模式。
- [ ] **步骤 4：运行测试**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_agent_tools tests.test_cli tests.test_agent_services tests.test_agent_permissions -v`
  - 预期：全部通过。
- [ ] **步骤 5：提交 Agent 生命周期层**
  - `git add agent_tools.py agent_actions.py agent_services.py cli.py cli_tui.py app.py desktop.py tests/test_source_agent_tools.py tests/test_cli.py`
  - `git commit -m "feat: expose source ingest through agent and cli"`

### Task 7：全量验证、文档和交付证据

**文件：**
- 修改：`docs/PRODUCT.md`
- 修改：`CHANGELOG.md`
- 修改：`docs/STATUS.md`
- 必要时创建：`tools/verify_source_ingest.ps1`
- 测试：现有全量测试与新增测试

- [ ] **步骤 1：运行定向验证**
  - 运行：`.venv\Scripts\python.exe -m unittest tests.test_source_settings tests.test_source_versions tests.test_source_recycle tests.test_source_profiles tests.test_source_ingest tests.test_source_routes tests.test_source_agent_tools -v`
  - 保存完整命令、解释器版本、Git revision、临时输入／输出路径、任务 JSON、manifest、report 和 SHA-256。
- [ ] **步骤 2：运行源码级回归**
  - 运行：`.venv\Scripts\python.exe -m py_compile source_settings.py source_versions.py source_recycle.py source_profiles.py source_ingest.py app.py agent_tools.py agent_actions.py agent_services.py`
  - 运行：`.venv\Scripts\python.exe -m unittest discover -s tests -v`
  - 运行模板检查和必要的 Node 语法检查；任何未执行项在交付说明中明确列出。
- [ ] **步骤 3：做一次离线端到端样本验证**
  - 使用临时本地目录和 mock OCR/本地 MinerU 结果验证三种 Profile、重复版本、原地输出、失败保留源文件和回收站状态；不使用真实付费凭据。
- [ ] **步骤 4：同步长期文档**
  - `docs/PRODUCT.md` 增加“内容导入工作站”定位、目录和成熟度。
  - `CHANGELOG.md` 在 `[Unreleased]` 记录三个 Profile、持续监听、版本保留和系统回收站语义。
  - `docs/STATUS.md` 只记录实际执行过的验证、未执行项和已知限制。
  - 确认 `D:\data\笔记本\Obsidian\Original\项目\项目索引.md` 的 QuizForge 条目仍存在且入口和更新时间正确，不删除他人条目。
- [ ] **步骤 5：最终检查与提交**
  - 运行：`git diff --check`、`git status --short`，检查无凭据和无临时输出进入提交。
  - 提交文档和验证证据：`git add docs/PRODUCT.md CHANGELOG.md docs/STATUS.md tools/verify_source_ingest.ps1 && git commit -m "docs: document source ingest workstation validation"`
  - 不执行 DirectBundle、Setup 构建或发布。

## 计划自审

- 规范第 2 节的目录、原地替换、系统回收站和永久版本分别由 Task 1、Task 2、Task 3/4 覆盖。
- 规范第 3 节的模块边界在“文件结构与职责”和各 Task 的 Files/Interfaces 中固定；Flask 不承载 Profile 细节。
- 规范第 4 节的稳定性、去重、重启中断和输出树排除由 Task 4 覆盖。
- 规范第 5 节的好题／好卷／好资料语义由 Task 3 覆盖，并由 Task 7 离线端到端样本复核。
- 规范第 6 节的 manifest、事务顺序、凭据边界由 Task 2 和 Task 4 覆盖。
- 规范第 7 节的设置页、Agent、CLI 和生命周期由 Task 5/6 覆盖。
- 规范第 8 节的可复现证据和长期文档由 Task 7 覆盖。
- 没有引入额外运行时依赖或正式发布动作；计划可按任务逐步回滚和验证。
