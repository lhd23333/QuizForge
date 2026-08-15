# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

QuizForge **软件版**——单机文件式数学／物理题库管理工具。本地文件存储、无需数据库、无需账号，浏览器打开即用。完整链路：PDF/图片试卷 → MinerU 或 Doc2X OCR → LLM/机械规范化 → 题库管理 → 组卷导出 PDF。

**只监听 `127.0.0.1:5000`，无鉴权，设计上仅供本机单人使用。请勿修改代码暴露到公网。**

题库根目录直接就是一个 Obsidian vault，题目是普通 `.md` 笔记，图片用 `![[文件名]]` 双链引用。有一个配套的 Obsidian 插件，把本应用的页面嵌在 Obsidian 侧边窗格（iframe）里。

## 启动与开发命令

```powershell
# 启动应用
python app.py
# 浏览器打开 http://127.0.0.1:5000

# 编译检查 + 标准库回归测试（不新增 pytest 依赖）
.venv\Scripts\python.exe -m py_compile app.py
.venv\Scripts\python.exe -m py_compile filestore.py exporter.py export_tables.py import_defaults.py converter.py pdf_collection.py collection_structure.py ocr_pool.py mineru_store.py doc2x_client.py doc2x_store.py imgorder.py blockpipe.py blocksplit.py blocknorm.py mechfix.py importer.py dedup.py llm_client.py providers.py qrender.py task_store.py cleanup_output.py corpus.py tools\eval_doc2x.py
.venv\Scripts\python.exe -m unittest discover -s tests -v

# 一键运行完整源码验证；构建目录版后去掉 -SkipBundleScan 可同时检查发行文件
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\verify_release.ps1 -SkipBundleScan

# 模板语法检查
.venv\Scripts\python.exe -c "from app import app; [app.jinja_env.get_template(t) for t in app.jinja_env.list_templates()]"

# JS 语法检查（如安装了 Node）
node --check static/js/import-upload.js
node --check static/js/text-preview.js
```

**重启注意**：`QUIZFORGE_DEBUG=0`（默认）时 Flask 会缓存模板，改模板/CSS/Python 后要点 Obsidian 插件里的 ⏻「重启后端」才生效；JS 还要 Ctrl+Shift+R 强刷。

## 核心架构

### 存储模型（与服务器版根本不同）

| | 软件版 | 服务器版（quizbank-web） |
|---|---|---|
| 题目存储 | 每题一个 `.md`（YAML frontmatter + 正文） | SQLite |
| 文件夹 | 真实目录（相对 `BANK_DIR` 的路径） | 数据库行 |
| 图片引用 | Obsidian 双链 `![[文件名]]` | `![alt](/qimages/<scope>/<file>)` |
| 图片磁盘 | 全局 `ASSETS_DIR` 扁平存放；桌面端由 `desktop.json.assets_dir` 唯一指定 | `IMAGES_DIR/<scope>/` 按用户分子目录 |
| 鉴权 | 无 | Flask-Login + 角色 |

**身份认定**：题目的身份取 frontmatter 的 `id` 字段，**文件名不参与**。`path.stem` 只是没有 frontmatter 时的兜底。所以用户可以随手改名。

**图片引用正则**：`!\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]`——单捕获组（文件名），`|` 之后是 Obsidian 显示宽度后缀而非 alt。这个正则在 `app.py`、`qrender.py`、`exporter.py`、`tikz_redraw.py` 四处出现，必须保持一致。

### 模块职责

```
app.py              Flask 应用与全部路由（~59KB，单体路由文件）
config.py           路径、常量、上传边界配置
filestore.py        文件式题库存储层（替代服务器版的 db.py）
                    ——每题一个 .md，YAML frontmatter 往返解析
                    ——内存索引缓存（按 mtime 刷新）
                    ——勾选篮以题目 id 原子写入 data/selections.json
task_store.py       转换任务 JSON 快照；重启后保留完成/待审核结果，不自动重放付费调用
cleanup_output.py   启动清理：过期任务 7 天、上传件/导出产物 24 小时
converter.py        OCR 后端与拆题引擎的统一转换层
                    ——OCR 为 MinerU/Doc2X 二选一；下游 whole/block/no_ai 独立选择
                    ——多组并发转换用 ThreadPoolExecutor
                    ——中间产物路径全部绝对化（CWD 是进程级的，不能切）
pdf_collection.py   有书签多试卷合集的 OCR 前预拆分
                    ——按试卷首页书签切成单卷，按规范化标题配对答案
collection_structure.py
                    无书签合集的 OCR 后结构分组
                    ——通用大写中文序号标题 + 后续题号从 1 开始且连续确认边界
                    ——题干/解析专题不能严格一一对应时拒绝，不按页序猜配
doc2x_client.py     Doc2X v2/v3 API 客户端
                    ——预上传、解析轮询、Markdown ZIP 导出、图片归一与安全解压
                    ——保留 v3 布局 JSON，按 FigureGroup 坐标修复高置信 A-D 图片顺序
doc2x_store.py      多份 Doc2X Key 加密存储；明文只在调用期间存在
imgorder.py         多图片选择题归属恢复
                    ——读取 MinerU content_list 的 page_idx + bbox
                    ——区分题干辅助图与 A-D 四张选项图，低置信度时不改原文
exporter.py         PDF 导出：题目列表 → Markdown → pandoc → xelatex → PDF
                    ——分页、选项分列、图片布局、分值区与 WIMath 标志
                    ——双栏大题按 TeX 实际高度占列，16:9 题目区保持 70% 左对齐
export_tables.py    OCR HTML／Markdown 管道表格的共享纯文本行列解析
                    ——页面与 PDF 分别安全渲染，不把外来 HTML 直接带回 DOM
qrender.py          页面侧题目正文结构化渲染
                    ——选项分列/图片位置/表格解析与 exporter 共享规则
                    ——题干与解析各自消费图片分栏设置；公式不碰（留给前端 KaTeX）
import_defaults.py  新导入题目的图片位置、方向与逐图布局默认规则
                    ——只接收科目和配对判定，不读取 Flask 请求或题库
handouts.py         讲义 schema、题目快照、Markdown 往返与安全原子存储
handout_exporter.py 讲义题卡局部编译及 PDF/TeX/ZIP 导出适配
importer.py         Markdown 题目切分与题号提取
dedup.py            题目去重检测（基于 SequenceMatcher）
providers.py        LLM 配置管理（cc-switch 风格，多套可切换）
                    ——两条用途：md（导入识别） / redraw（配图重绘）
                    ——supports_vision 标记控制重绘快速切换按钮
                    ——redraw 解析不到时回落 md，反方向不回落
llm_client.py       OpenAI 兼容 LLM 客户端
                    ——SSRF 防护（拒绝内网地址，但放行 loopback——单机版需要本地推理）
                    ——假完成检测（模型在长文档上提前 stop 导致漏题）
crypto_utils.py     Fernet 对称加密——API Key / MinerU Token 的加密存储
                    ——密钥文件 data/.enc_key，删掉则已存 key 永久解不开
blockpipe.py        逐块识别路径的总编排（五层串起来）
blocksplit.py       机械切块（三档降级：严格 → 放宽 → 结构标记）
blocknorm.py        LLM 逐块判类型 + 规范化
mechfix.py          安全子集的机械排版修正（跑在 LLM 之前）
qualcheck.py        识别结果质量检查
optcheck.py         选项完整性检查
corpus.py           识别语料留档（OCR 中间产物存 data/corpus/）
mineru_store.py     MinerU Token 加密存储（多份）
ocr_pool.py         MinerU/Doc2X 统一请求池——多凭证轮转、跨窗口限流与退避
tools/eval_doc2x.py Doc2X 真实 PDF 回归；按内容哈希复用付费结果，再跑最新本地逻辑
tikz_render.py      TikZ 代码 → xelatex → PDF + dvisvgm → SVG（带沙箱三道闸）
tikz_redraw.py      AI 重绘配图（多模态模型看图 → TikZ → 矢量图）
ui_prefs.py         界面外观偏好（深浅色/主题色/壁纸）
static/js/text-preview.js
                    编辑页、导入校对和拆题审核的轻量实时表格/图片预览
```

### 导入链路的两个正交维度

OCR 后端由 `ocr_backend` 选择：`mineru`（默认，兼容旧任务）或 `doc2x`。拆题引擎由 `engine` / `block_mode` 独立选择：

1. **`whole`**（project-alpha 整篇规范化）：老路径，一份卷子整个送 LLM
2. **`block`**（逐块识别，**默认**）：机械切块 → LLM 逐块判类型 → 程序化配对。块数在切块那一步定死，LLM 不能增减
3. **`no_ai`**：纯机械渲染，完全不送 LLM（不花额度）

`block` 模式下还可选「先人工审核拆题结果」（`manual`），在送 AI 之前让人调整块的合并/拆分/顺序。

不要把 OCR 后端与拆题方式合并成一个枚举：Doc2X 和 MinerU 都必须能接三种下游。任务快照要写 `ocr_backend`，但不得写 OCR 明文凭据；旧快照缺字段时回落 MinerU。

### vendor/project_alpha

MinerU 路径的 PDF/图片规范化引擎已整体 vendor 进 `vendor/project_alpha/`，不依赖任何外部路径。Doc2X 不进入 vendor，而由 `doc2x_client.py` 调官方 API，再把相同的 raw Markdown 交给现有下游。云端 OCR/LLM 都可能计费；不配置密钥时其余功能（手动导入、题库管理、组卷导出）正常工作。

### 数据目录

```
data/
  .enc_key           Fernet 密钥（不进 git，删掉=已存 key 永久解不开）
  providers.json     LLM 配置（API Key 存密文）
  mineru.json        MinerU Token（密文）
  doc2x.json         Doc2X API Key（密文）
  ui_prefs.json      外观偏好
  corpus/            识别语料留档（_raw.md / _blocks.json / _normalized.md）
  uploads/           上传文件暂存
  wallpaper/         壁纸文件
```

题库本身在 `BANK_DIR`（默认 `D:\data\笔记本\Obsidian\QuizForge`），由环境变量 `QUIZFORGE_BANK` 覆盖。Obsidian 插件启动时自动传入它所在 vault 的根目录。图片统一写入 `ASSETS_DIR`；桌面版从全局 `desktop.json.assets_dir` 读取并在导入应用前设置 `QUIZFORGE_ASSETS_DIR`，所有题库窗口共用同一路径。源码或 Obsidian 未显式设置时才兼容回落 `BANK_DIR/_assets`。

桌面题库记录另带 `subject=math|physics`，由 `desktop.py` 在导入应用前写入 `QUIZFORGE_SUBJECT`。题目文件、识别结果和表单值仍统一存“填空题”；物理题库只在模板与导出标题显示为“实验题”，不得为它复制一套题型或识别分支。`_backups/`、`_handouts/` 与各级历史 `_assets/` 都不能进入题目扫描和目录树；图片读取只认当前全局 `ASSETS_DIR`。

### 关键安全边界

- **只监听 127.0.0.1**，无鉴权——绝不能暴露到公网或局域网
- 所有写请求必须带当前进程随机令牌（`static/js/csrf.js` 自动补）；后端重启后旧页需刷新
- 试卷上传除大小/扩展名外还验 PDF/DOCX/图片真实内容，伪装格式不得落盘
- API Key / MinerU Token / Doc2X Key 用 Fernet 加密存盘，明文只在内存中存在、永不回显；任务快照和语料元数据只记后端名，不记凭据
- 独立桌面包通过 `QUIZFORGE_LICENSE_ENFORCED=1` 启用 Ed25519 离线许可证；只在 `service_ports.export_document()` 门控预览/导出，不限制本地题库阅读和整理。源码与 Obsidian 托管模式默认不强制，避免开发调试依赖发行许可证
- `assets/license_public_key.pem` 可随包公开；签发私钥只允许由 `tools/license_signer.py` 在发行者侧读取，绝不能复制进项目资源、安装包、日志或测试夹具。正式收费前必须废弃当前无密码 beta 私钥并生成有密码、离线备份的新密钥
- `llm_client.py` 的 SSRF 防护放行 loopback（本地推理 Ollama/LM Studio），但拒绝局域网和非 HTTPS 公网地址
- TikZ 编译有三道沙箱闸：黑名单（`\write18`/`\input`/`\directlua` 等）、`-no-shell-escape`、`openin_any=p`/`openout_any=p`
- 图片服务 `/assets/<name>` 只从全局 `ASSETS_DIR` 走 `send_from_directory`（内建 `safe_join`）；永久清理还必须跨全部登记题库 fail-closed 扫引用
- 所有「外来文本 → 落盘」必须过 `filestore.normalize_newlines`，写盘 `newline="\n"`、读盘 `newline=""`——Windows 的 `\r\n` 翻译会让换行每存一轮翻一倍

### 与服务器版（quizbank-web）的关系

流水线模块（`exporter.py`、`blockpipe.py`、`blocksplit.py`、`blocknorm.py`、`mechfix.py`、`importer.py`、`dedup.py`、`qrender.py`、`llm_client.py`、`qualcheck.py`、`optcheck.py`、`corpus.py`）与服务器版同源，移植时适配了图片寻址和存储模型。**这些模块里没有 `owner_id`/`current_user`/`user_id` 等多用户残留，也没有 import `db`/`auth`/`models`。**

## 开发注意事项

### 模板与静态资源
- 跨蓝图 `url_for` 不适用（单文件 app，没有蓝图），直接用 `url_for('端点名')`
- 静态资源用 `static_v(filename)` 追加 `?v=<mtime>` 防缓存——插件内嵌 webview 没有 Ctrl+Shift+R
- CSS 已补全 `.q-*` 结构化渲染类族、`.dropzone`/`.dz-*` 拖放上传、`.bo-*`/`.br-*` 批量操作面板、`.folder-ctx` 右键菜单、`.toast` 系列
- 前端公式渲染从 MathJax CDN 换成了自托管 KaTeX（`static/js/katex/`），`base.html` 里的三个脚本顺序即依赖
- **首屏防闪**：`<html>` 加 `.math-pending` 类，KaTeX 排完摘掉，另有 3s 兜底定时器

### 并发
- 批量线程池默认 12 个 worker，只负责把组送入统一 OCR 队列；多个批次共享进程级槽位
- `ocr_pool` 按每个 OCR 文档任务调度：总并发 12、MinerU 6、Doc2X 8；题干+解析双文件也分别取凭证和槽位
- 服务端并发/队列限制触发后端冷却并用原凭证重试；只有凭证失效或额度不足才换另一份凭证一次
- 导出并发由 `config.EXPORT_CONCURRENCY`（默认 1）控制，用 `BoundedSemaphore`
- `converter.py` 里中间产物路径全都绝对化了——`os.chdir` 是进程级的，并发时会串台
- `filestore._write_lock` 保护「取名 + 算 order + 落盘」的读-改-写原子性
- 已加载题卡的单题操作必须优先命中 `filestore._cache` 并只重读该 Markdown；`invalidate_scan_cache(folder_structure=True)` 只用于真实目录结构变化，frontmatter 更新不得清空完整目录树缓存
- `providers._lock` 保护 LLM 配置的 JSON 读写
- 转换任务的稳定状态同时写入 `data/conversion_tasks.json`；在途调用重启后标中断，绝不自动重放

### 图片三处路径不可混用
- 磁盘 `ASSETS_DIR`（桌面端唯一共享目录，扁平；通常选择公共 Obsidian vault 的 `_assets/`）
- 网页 `/assets/<name>`（`asset_serve` 路由）
- 导出暂存 `output/quiz_<时间>_<uuid>/quiz_*_img_*`（24h 后清理）

软删除题目只把 Markdown 移入 `.trash`，图片必须保留以支持恢复。彻底删除／清空回收站只把该题引用过的图片列为候选，再检查全部已登记题库；查重页的全量图片体检同时把 `.trash`、`_handouts`、`_backups` 计为活引用。任一题库不可访问或 Markdown 读取失败就拒绝删除；确认删除前再扫描一次，只删两次都未引用且大小、mtime 未变化的旧普通图片。不得改回“只扫当前 `BANK_DIR`”。

### 排版模式的三态（`img_split`）
`None`（从未设过→给默认值）、`"off"`（明确关掉→不给默认）、`"opts"/"full"/"sub"/"pair"`（显式模式）。空字符串不能压成 `""`——会把前两种合并。同一套语义在 `_to_record`/`set_img_layout`/`_KNOWN_DEFAULTS` 三处，改一处必须改三处。

解析图片使用独立字段 `sol_img_split`，当前只接受 `None`／`"off"`／`"full"`；页面按钮请求必须携带 `field="solution"`，题目快照、局部预览、整份导出也要分别透传，不能借用题干的 `img_split`。题卡和导出只在显示层剥掉解析字段开头的结构性“【解析】”或“解析：”，不得改写原题 Markdown，也不得误删“解析如下”等正文；“参考解析”和“第 N 题解析”仍是分区／页面标题。

### 讲义与横版／双栏导出版式

- WIMath 标志由导出表单显式传入，普通组卷与讲义共用本地 PDF 资源：A4 左上并保留顶边间距，16:9 左下。不得在运行时下载资源。
- 16:9 的 70% 是题目内容外层宽度；单图的 `img_width` 再相对该容器计算，并以横版专用最大高度封顶。若只缩外层、不调整高度上限，大图会持续撞同一高度限制，看起来像“改图片大小无效”。
- 双栏刷题的解答题由 `qpracticesolve` 在 TeX 侧以 `\ht + \dp` 测完整题目和作答区：第一道只有当前栏放得下才接在小题后，此后每题先换栏；超出单栏自身高度时允许自然展开。不要用 Python 字数估算替代真实排版高度。

### 连续图片分组
`exporter.plan_figs()` 把连续上下图分为一个视觉单元。若拆成两个单图行，`.q-fig`/LaTeX 行级外边距会重新制造白缝。系统只消除结构间距，不自动裁原图白边。

### 新增功能模块注意
- 新增 Python 模块要同时在 `app.py` 顶部 import
- 新增 JS 文件要同步更新 `base.html` 的 `<script>` 标签（带 `static_v` + `defer`）
- 新增路由的图片/文件访问必须验路径（`safe_join` + `resolve()` 验祖先目录）
- 任何「外来文本 → 落盘」必须先过 `filestore.normalize_newlines`

### 桌面缩放与题库内新增

- 独立桌面窗口固定打开 `/workspace`，由 `workspace.html` 同时保留普通业务 iframe 和资料库 iframe。顶部导航只隐藏／显示，不得销毁资料库；子页面用 `_embedded=1` 隐藏重复外壳，并通过现有 location 消息同步路径。浏览器与 Obsidian 仍直接打开业务路由。
- `library-tabs.js` 的标签必须对应持续存在的 document panel，PDF iframe 只创建一次；切换标签只能设 `hidden`。双分栏最多两栏，布局、比例、各栏标签及活动项保存在 `sessionStorage`，正文仍从磁盘读取。
- 资料库 Markdown 保存走独立的 `/api/library/write`，请求必须带 `/api/library/read` 以十进制字符串返回的 `st_mtime_ns`。路由先过题库根路径与扩展白名单，`filestore.write_markdown_text()` 再与题卡写入共享锁、核对版本并原子替换；前端禁止把它转为会丢精度的 JavaScript `Number`；冲突返回 409 且客户端保留草稿，不能静默覆盖 Obsidian 的外部修改。
- `/import` 首屏不得调用完整目录树。目标父文件夹选择器在打开后复用 `/collections/children` 按层加载，最终仍提交 `target_parent`。
- Windows 无边框窗口的缩放由 `base.html` 八个 `.desktop-resize-*` 命中区和 `DesktopApi.window_resize()` 共同实现；页面传目标尺寸与固定对侧角，Python 用 `FixPoint` 调原生 `Window.resize()`。最小宽高必须与 `create_window(min_size=(1024, 680))` 保持一致，最大化时禁止缩放。
- 新题不再从顶部独立栏目进入。`index.html` 的悬浮加号请求 `/question/inline-draft`，`_new_question_card.html` 与普通题卡共用 `_inline_question_editor.html`；预览走 `/question/inline-preview`，保存走 `/question/inline-create`。目标文件夹必须后端验证，取消草稿不落盘。
- 无限滚动快照不含本轮新题，禁止直接递增 `data-total/data-loaded`；使用 `data-inline-created` 另计，并把后续快照卡插到本轮新题之前。文件夹片段替换时清零该计数。

### 离线许可证与发行扫描

- `license_manager.py` 只信任内置 Ed25519 公钥，对 `.qflicense` 的规范化 payload、签名、产品、日期和功能项做本地校验；无效导入不得覆盖已有有效许可证。许可证落在 `DATA_DIR/license.qflicense`，属于用户数据，不得进入发行包。
- `service_ports.py` 的授权缺省值是 `offline_signed`，未来联网授权只能新增 `remote` 适配，不得把 HTTP 调用散进题库路由或 `exporter.py`。`updates_until` 当前是为未来更新授权预留的签名字段，现阶段没有自动更新器，也不据此联网。
- `tools/license_signer.py` 和私钥不进入桌面包；每次构建由 `tools/verify_desktop_bundle.py` 扫描业务 `.py`、私钥标记、`.qflicense` 与运行数据。源码匹配必须按发行目录相对路径判断，不能只按 basename：pywebview 等第三方包内部也有 `app.py`，全局同名匹配会误报。
- 修改普通功能不需要换密钥。只有私钥泄露或主动轮换时才替换公钥并重建应用；一旦换公钥，旧许可证全部失效，必须明确安排迁移。
- Inno 覆盖中文或含空格目录时不能把裸 `/DIR=...` 交给 `Start-Process -ArgumentList`，它会重拼并可能截断到首个空格。应使用 `ProcessStartInfo.Arguments` 传入带内层双引号的 `/DIR="完整路径"`，并在安装后同时核对最新 Setup Log 的 `Dest filename`、EXE ProductVersion 和快捷方式目标；返回码 0 本身不能证明装到了预期目录。

## 相关文档

- `README.md` / `README.en.md`：功能说明与快速开始
- `迁移说明.md`：从服务器版移植模块的完整变更记录（2026-08-07 ~ 2026-08-08，七轮）
- `vendor/project_alpha/VENDORED_FROM.md`：project-alpha 的上游 commit 记录
