# 参与贡献

感谢参与 QuizForge。提交改动前，请先搜索现有 Issue，较大的产品或架构变化先开 Issue 说明目标和兼容影响。

## 开发环境

QuizForge 主要在 Windows 和 Python 3.13 环境开发：

```powershell
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
npm.cmd install
npm.cmd run test:handouts
```

运行 `python app.py` 后访问 `http://127.0.0.1:5000`。该本地服务没有用户鉴权，不得暴露到公网或局域网。

## 提交要求

- 每个提交聚焦一个可独立审查和回退的主题，不提交构建产物、题库、日志或真实 API Key。
- 保持现有文件式题库格式和用户数据兼容；涉及不可逆迁移时必须先在 Issue 中讨论。
- 修改用户可感知行为时同步 `CHANGELOG.md`；改变长期产品边界时同步 `docs/PRODUCT.md`。
- Python 改动至少通过语法检查和完整 `unittest`；前端改动同时运行相关 Node 测试并检查窄屏布局。
- 新增依赖前说明必要性、许可证和安装体积影响。

提交 Pull Request 即表示你同意按项目的 `GPL-3.0-or-later` 许可贡献相应代码，并确认你有权提交这些内容。
