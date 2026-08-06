# QuizForge 软件版

单机版数学题库管理工具：本地文件存储、无需数据库、无需账号，浏览器打开即用。

一套完整的「PDF/图片试卷 → OCR 识别 → AI 规范化 → 题库管理 → 组卷导出」工具链，专为个人/教师本地整理数学题库设计。所有数据以 Markdown 文件形式存储在本地磁盘，兼容 Obsidian 直接打开浏览。

> English version: see [README.en.md](README.en.md)

## 功能

- **题目导入**：支持粘贴 Markdown、上传 PDF/Word/图片（AI 识别）、批量导入多份试卷
- **AI 识别**：PDF/图片 → MinerU OCR → DeepSeek（或任意 OpenAI 兼容模型）规范化为标准格式题目
- **题库管理**：文件夹分类、标签、难度、题型筛选，去重检测，回收站（软删除可恢复）
- **组卷导出**：按条件筛选题目，导出为 PDF 试卷（支持选择题分栏排版、插图自动布局）
- **纯本地存储**：每题一个 `.md` 文件（YAML frontmatter + 正文），文件夹即真实目录，可直接用 Obsidian 打开题库目录当 vault 浏览编辑
- **用户内容零篡改**：LaTeX 公式、自定义分区（`## 标题`）原样保留，不做任何静默清洗

## 快速开始

### 1. 环境准备

需要 Python 3.11+（开发环境用 3.13）。

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

组卷导出（PDF）还需要本机安装：

- **Pandoc**（Markdown → LaTeX）：https://pandoc.org/installing.html
- **XeLaTeX**（LaTeX → PDF，推荐 MiKTeX 或 TeX Live，需支持中文）：https://miktex.org/

安装后 `config.py` 会自动从系统 `PATH` 里探测 `pandoc`/`xelatex`；探测不到时可以直接改 `config.py` 里的 `PANDOC`/`XELATEX` 常量指向可执行文件的绝对路径。

### 2. 启动

```powershell
python app.py
```

浏览器打开 http://127.0.0.1:5000 即可使用。

### 3.（可选）配置 AI 识别

不配置也能用全部核心功能（手动粘贴导入、题库管理、组卷导出），只是"上传 PDF/图片自动识别"这一项需要两个云端服务的密钥：

1. **MinerU**（OCR）：在 https://mineru.net 的"API 管理"页面创建 API Token（付费服务）
2. **DeepSeek**（规范化，或任意 OpenAI 兼容模型）：在 https://platform.deepseek.com 创建 API Key（付费服务）

配置方式二选一：

- **在设置页填写**（推荐）：启动后打开"设置"页，添加一套 LLM 配置（支持 DeepSeek / 阿里云百炼 / 硅基流动 / 中转站 / 自建等任意 OpenAI 兼容接口），Key 会加密存储在本地
- **或**：复制 `vendor/project_alpha/.env.example` 为 `vendor/project_alpha/.env`，填入 `MINERU_API_TOKEN` 和 `DEEPSEEK_API_KEY`

两者中 MinerU token 目前只能通过 `.env` 配置（OCR 环节固定用 MinerU，无法切换），DeepSeek/LLM 那一步可以在设置页换成任意其他模型。

## 安全说明

**本工具只监听 `127.0.0.1`，没有任何用户认证机制，设计上仅供本机单人使用。请勿修改代码把它暴露到公网或局域网，也不要在多用户共享的机器上运行——任何能访问这台机器网络端口的人都能读写你的题库。**

API Key 使用 Fernet 对称加密后存储在 `data/.enc_key`（密钥文件）+ `data/providers.json`（加密后的配置）。**这个密钥文件如果被删除或更换，已保存的所有 Key 会永久无法解密**，只能在设置页重新填写。该文件已在 `.gitignore` 中排除，切勿手动提交到 git 或加入任何备份/同步工具。

## 目录结构

```
软件版/
  app.py                 Flask 应用与路由
  config.py               路径与常量配置
  filestore.py             文件式题库存储层（每题一个 .md）
  importer.py              Markdown 题目切分逻辑
  exporter.py               PDF 导出（pandoc + xelatex）
  converter.py             AI 识别转换层（调用 vendor/project_alpha）
  dedup.py                 题目去重检测
  crypto_utils.py           API Key 加密存储
  llm_client.py             通用 OpenAI 兼容 LLM 客户端
  providers.py              多套 LLM 配置管理（cc-switch 风格）
  blockpipe.py / blocksplit.py / blocknorm.py / mechfix.py
                             另一套「机械切块」识别引擎（可选）
  templates/                 Jinja2 页面模板
  static/                    CSS
  vendor/project_alpha/       内置 PDF/图片规范化引擎（MinerU + DeepSeek）
  data/                       题库数据（.gitignore 排除，本机私有）
  output/                    导出产物（.gitignore 排除）
```

## 关于 `vendor/project_alpha`

这是内置的 PDF/图片规范化引擎（PDF/Word/图片 → MinerU OCR → DeepSeek → 规范化 Markdown），已作为内部包 vendor 进本项目，不依赖任何外部路径。它是一个纯 Python 库，不是独立服务，由 `converter.py` 直接调用其函数。

它依赖的两个云端 API（MinerU、DeepSeek）都是付费服务，无本地/离线替代方案——这是"AI 自动识别"这一项功能本身的硬性限制，不影响其余核心功能（题库管理、手动导入、组卷导出）在无网络/无密钥情况下正常工作。

## License

本项目仅供个人学习与本地使用，未附带开源许可证。如需二次分发或商用，请先与作者确认。
