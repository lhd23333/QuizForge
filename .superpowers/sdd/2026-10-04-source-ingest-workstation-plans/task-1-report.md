# Task 1 实施报告

状态：DONE；审查发现已在后续修复，详见下方「审查修复报告」。

## 改动文件

- `config.py`：增加题库状态级 `SOURCE_SETTINGS_PATH`、`SOURCE_VERSIONS_PATH`、`SOURCE_WORKSPACE_DIR`。
- `task_store.py`：增加 `source` 命名空间，保留旧快照兼容行为。
- `source_settings.py`：默认配置、原子 JSON 保存、本地目录验证、配置重置与持久化。
- `source_versions.py`：SHA-256、版本键、原子预留/提交、回收待处理标记和已提交版本查询。
- `tests/test_source_settings.py`、`tests/test_source_versions.py`、`tests/test_task_store.py`：相关回归测试。

## 验证

RED（按要求先写测试后确认接口缺失）：

命令：`.venv\Scripts\python.exe -m unittest tests.test_source_settings tests.test_source_versions -v`

结果：`FAILED (errors=2)`；两个测试模块均因 `ModuleNotFoundError` 找不到尚未实现的 `source_settings` / `source_versions` 失败。

GREEN：

命令：`.venv\Scripts\python.exe -m unittest tests.test_source_settings tests.test_source_versions tests.test_task_store -v`

结果：`Ran 13 tests in 0.118s`，`OK`，退出码 `0`。

语法检查：

命令：`.venv\Scripts\python.exe -m py_compile config.py task_store.py source_settings.py source_versions.py tests\test_source_settings.py tests\test_source_versions.py tests\test_task_store.py`

结果：退出码 `0`，无输出。

差异检查：`git diff --check` 退出码 `0`。Git 对工作区中若干文件提示 LF 将在后续 Git 操作时转换为 CRLF；未改写无关文件。

## 提交

功能提交：`fbd6b9b`；初始报告提交：`94c1a67`。

## 审查修复报告

状态：修复完成，提交 `b3cd9b1`。

### 追加改动

- 配置与版本账本遇到无效 JSON/schema 时，复制原文件到 `.corrupt-<time_ns>`，记录固定原因码并抛出专用异常；若复制失败也阻止后续写入。配置读取 I/O 错误同样 fail closed。
- 版本预留要求显式 `profile` 并按真实 profile 查询；新增 `cancel_reservation()`，只删除目标不存在的 `reserved` 记录。已提交或目标已出现时拒绝释放；`recycle_pending` 仍占用目标序号，且不能被重新提交。
- 目录验证检查工作区本身是否经符号链接／junction 重定向、词法路径与解析目标边界，以及解析后 UNC 目标。配置读取逐条重新验证路径；不安全条目跳过，并在旁路审计 JSONL 中仅保存时间、profile 名的短哈希和固定原因码。
- 回收失败只存受控错误码并省略外部错误详情，避免把路径或凭据写入登记 JSON。

### 修复验证

RED：

命令：`.venv\Scripts\python.exe -m unittest tests.test_source_settings tests.test_source_versions -v`

结果：`Ran 13 tests`，`FAILED (failures=2, errors=6)`。新增断言按预期暴露缺失的损坏账本异常、profile 参数、预留释放接口、profile 审计和工作区链接检查。

GREEN：

命令：`.venv\Scripts\python.exe -m unittest tests.test_source_settings tests.test_source_versions tests.test_task_store -v`

结果：`Ran 20 tests in 0.180s`，`OK`，退出码 `0`。输出中的 corrupt/evidence/recycle 日志是测试触发的预期固定消息，不含路径或凭据。

语法检查：`.venv\Scripts\python.exe -m py_compile config.py task_store.py source_settings.py source_versions.py tests\test_source_settings.py tests\test_source_versions.py tests\test_task_store.py`，退出码 `0`，无输出。

差异检查：`git diff --check` 退出码 `0`。Git 报告现有工作区文件的 LF/CRLF 转换提示，不影响检查结果。

### 尚存注意事项

- 版本序号分配依简报约定由单进程锁保护；多个 QuizForge 进程同时写同一版本账本不属于当前实现保证范围。
- 配置非法 profile 的审计日志记录于 `source_settings.audit.jsonl`，只写 profile 名哈希，不包含路径或错误原文。

修复提交：`b3cd9b1`。

## 二次复审修复报告

状态：DONE。

### 修复内容

- `save_profile()` 与 `reset_profile()` 现在只更新被明确编辑的配置档案，并保留其他非法档案的原始 JSON 值；读取时仍跳过非法值、写入固定原因码审计，不会无声删掉损坏证据。
- `reserve_version()` 恢复任务简报中的旧调用签名：`profile` 改为可选 keyword。旧调用会按输出目录匹配已配置 profile；没有唯一匹配时回退到输出目录名，多个匹配则要求显式传入 profile。显式传入 profile 时仍按该值登记。
- 新增两个回归测试：非法条目在保存其他档案后仍原样存在；不带 profile 的旧 API 调用可以从来源配置解析真实 profile。

### 验证证据

RED：

命令：`.venv\Scripts\python.exe -m unittest tests.test_source_settings.SourceSettingsTests.test_saving_another_profile_preserves_raw_invalid_entry tests.test_source_versions.SourceVersionsTests.test_legacy_reserve_signature_derives_profile_from_settings -v`

结果：`Ran 2 tests`，`FAILED (errors=2)`。第一项因非法配置记录在另一个档案保存时丢失而触发 `KeyError`；第二项因旧 API 调用缺少必需的 `profile` 参数而触发 `TypeError`。

GREEN：

命令：`.venv\Scripts\python.exe -m unittest tests.test_source_settings tests.test_source_versions tests.test_task_store -v`

结果：`Ran 22 tests in 0.183s`，`OK`，退出码 `0`。测试中出现的固定 corrupt/evidence/recycle 日志为预期诊断，不包含敏感路径或凭据。

语法检查：`.venv\Scripts\python.exe -m py_compile config.py task_store.py source_settings.py source_versions.py tests\test_source_settings.py tests\test_source_versions.py tests\test_task_store.py`，退出码 `0`，无输出。

差异检查：`git diff --check` 退出码 `0`；Git 对部分文件报告 LF/CRLF 自动转换提示。

修复提交：`ba1476f`。
