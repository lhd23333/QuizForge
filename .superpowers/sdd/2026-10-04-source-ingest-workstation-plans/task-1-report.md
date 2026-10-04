# Task 1 实施报告

状态：DONE_WITH_CONCERNS

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

## 担忧

- `reserve_version()` 的简报签名没有 `profile` 参数，但 `list_versions(profile)` 要按 profile 过滤。当前登记使用输出目录名作为 profile；调用者若输出目录名与 profile 名不同，筛选结果不会符合预期。后续调用约定需明确或扩展 API。
- 配置或版本 JSON 若损坏，读取目前退回默认/空记录；需要更强的数据保全策略时，应仿照 `task_store` 保留损坏文件副本。

## 提交

待提交。
