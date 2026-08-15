# QuizForge 软件版

单机版数学／物理题库管理工具：本地文件存储、无需数据库、无需账号，可作为独立 Windows 桌面程序运行，也可继续在浏览器或 Obsidian 中使用。

一套完整的「PDF/图片试卷 → OCR 识别 → AI 规范化 → 题库管理 → 组卷导出」工具链，专为个人/教师本地整理数学题库设计。所有数据以 Markdown 文件形式存储在本地磁盘，兼容 Obsidian 直接打开浏览。

> English version: see [README.en.md](README.en.md)

## 独立桌面初版

`build/installer/QuizForge-0.17.0-beta-Setup.exe` 是当前 Windows 封闭内测安装包；支持中文安装向导、当前用户安装、开始菜单/可选桌面快捷方式和标准卸载，不需要安装 Python，也不依赖 Obsidian。目录版保留在 `build/desktop/QuizForge/QuizForge.exe`，供开发验收和不生成安装包的本机直接更新使用。

当前源码、安装包与日常安装版均为 `0.17.0-beta`（文件版本 `0.17.0.0`）。安装包为 61,622,643 字节，SHA-256 为 `4DE8D3D9D038C6C6F7A3E5429472015C8818CF2850FEE0388DE34BA1CA2FFB1F`；已用隔离数据完成 `0.15.3-beta → 0.17.0-beta` 原位覆盖升级，卸载器与用户数据均保留。

更新时不需要先卸载：正式分发可运行新版安装包并沿用原安装目录；只更新本机时可执行 `.\update_installed.ps1 -DirectBundle`，只重建桌面目录并覆盖程序文件，不生成安装包。题库登记、转换任务、加密密钥、OCR／LLM 配置、设备身份和许可证独立保存在 `%LOCALAPPDATA%\QuizForge` 或用户选择的题库目录，更新器会在覆盖与启动前后核对这些受保护数据。当前内测阶段不启用联网自动更新；只有明确需要对外发布时才重新构建安装包。

桌面版使用无边框原生窗口和自绘标题栏，题库、导入、任务、设置等栏目采用紧凑的应用式导航；浏览器与 Obsidian 模式仍保留原有外壳。桌面窗口内部使用常驻工作区，顶部切换栏目时资料库只隐藏、不销毁，已打开的 PDF 和分栏状态会在后台保留。

程序会在本机随机端口启动既有 Flask 后端，再用系统 WebView2 打开桌面窗口。窗口采用无黑边的自绘标题栏，并支持从四边和四角自由拖动缩放。首次启动选择题库文件夹；若该目录完全没有 Markdown，程序会创建一个独立的“QuizForge 示例题库”，放入 3 道原创示例题，已有题库绝不注入。之后可从顶部“题库”打开类似 Obsidian 的题库列表：登记已有 Obsidian vault 或普通文件夹、创建空题库、切换当前题库，或仅从列表移除记录；移除记录永远不会删除磁盘文件。旧版单一 `bank_dir` 配置会自动加入列表，不搬迁题目。欢迎/关于页会检查当前题库与数据目录、Pandoc、XeLaTeX、磁盘空间和离线服务状态，并提供打开当前题库、日志和数据目录的入口。程序配置、日志和临时产物默认放在 `%LOCALAPPDATA%\QuizForge`，题目仍是各自题库目录里的普通 Markdown 文件。

当前内测版不接授权服务器、更新服务器或云端导出服务器，题库管理、手动导入、查重、回收站、组卷和导出逻辑均在本机完成。独立桌面包使用离线签名的 `.qflicense`：设置页会显示由本机随机身份摘要生成的设备请求码，发布者按该请求码签发，默认测试期为 7 个自然日（签发当天计入）。许可证只用随包公钥在本机验签，不采集硬件或题库信息；未授权时仍可阅读和整理题库，预览与导出需要导入有效且匹配本机的许可证。源码开发和 Obsidian 托管模式默认不强制授权。MinerU、Doc2X、云端 LLM 等原有可选能力仍保留，但只有用户主动配置凭据并发起识别时才会访问对应第三方；完全离线使用时不配置即可。

安装包已经内置 Pandoc 3.9.0.2，所以新电脑可直接导出 `.tex` 和含图片的 `tex.zip`。当前预览版暂不捆绑完整 TeX 发行版：直接生成 PDF 仍需另装 MiKTeX/TeX Live；不想安装时，把 `tex.zip` 上传 Overleaf 即可编译。随包同时附带 Pandoc 的 GPL 文本、版权声明和对应源码归档，第三方说明见 `installer/THIRD_PARTY_NOTICES-preview.md`。

开发环境重新构建：

```powershell
.venv\Scripts\pip.exe install -r requirements-desktop.txt
npm.cmd install
npm.cmd run build:handouts
.\build_desktop.ps1
.\build_installer.ps1
```

提交或发布前可统一运行完整源码验证；生成桌面目录版后去掉 `-SkipBundleScan`，会继续检查发行文件中是否混入源码或私密数据：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\verify_release.ps1 -SkipBundleScan
```

桌面构建脚本优先选择可用的 Nuitka；当前 Python 3.13 且没有 MSVC 时自动回落到 PyInstaller。安装器使用 Inno Setup 6。正式收费发布前还需购买/确认 Inno Setup 商业使用许可、替换内测许可文本、做 Windows 代码签名，并考虑用 MSVC + Nuitka 加强逆向门槛；当前内测包不应宣称“无法逆向”。

封闭内测许可证由发布者在开发机离线签发，私钥不进入项目发行目录。建议使用本地台账工具统一管理；以下命令把 SQLite 台账、签发文件和当前 beta 密钥副本放在仓库外的“文档/QuizForgePublisher”，与已经内置该公钥的 `0.15.3-beta` 安装包直接配套：

```powershell
# 仅为当前封闭 beta 执行一次；接管已在客户端使用的内部测试密钥，不生成新钥
.venv\Scripts\python.exe tools\license_admin.py adopt-key `
  --private-key-source data\publisher\beta_signing_private.pem `
  --public-key-source assets\license_public_key.pem --no-password

# 客户把“设置 → 软件授权”中的设备请求码发来；不写时默认 7 天
.venv\Scripts\python.exe tools\license_admin.py issue `
  --licensee '<测试者>' --device-id '<QFD1-...>' --no-password

# 查台账；续期可按天数或固定日期，换机会生成绑定新设备的新证
.venv\Scripts\python.exe tools\license_admin.py list
.venv\Scripts\python.exe tools\license_admin.py renew <license-id> --valid-days 30 --no-password
.venv\Scripts\python.exe tools\license_admin.py replace <license-id> --device-id '<新 QFD1-...>' --no-password
.venv\Scripts\python.exe tools\license_admin.py revoke <license-id>
```

`renew`、`replace` 和 `revoke` 都会保留历史记录；但纯离线客户端不会收到即时作废通知，已经发出的证只能自然用到到期日，因此短期内测默认 7 天。仓库内 `data/publisher/` 的无密码 beta 私钥只供这轮封闭测试，已被 `.gitignore` 排除，不能用于正式收费。正式发布时先把私钥密码放入当前会话的 `QUIZFORGE_SIGNING_PASSWORD` 环境变量，再运行 `license_admin.py --publisher-dir <仓库外正式目录> init` 生成有密码密钥，并至少保留两份离线备份；核对后再有意地用新公钥替换 `assets/license_public_key.pem` 并重建客户端。在此之前，新密钥不能给现有客户端签证。不要覆盖仍在使用的密钥，否则旧许可证会全部失效。以后普通功能更新只需正常改源码、测试和重打包，不需要重做授权模块或换钥。

## 功能

- **题目导入**：支持粘贴 Markdown、上传 PDF/Word/图片（AI 识别）、批量导入多份试卷；文件可拖放上传，也能从文件夹一键导入并按文件名自动配对「题干 + 答案」。对于“一本题干合集 PDF + 一本答案合集 PDF”，在任务卡勾选“多份试卷合集”后，有试卷首页书签时会在 OCR 前快速拆卷；没有书签时不裁切 PDF，而是把题干册和解析册各整本 OCR 一次。程序优先用可靠的结构标题和后续题号确认边界，但标题不是必需条件：可靠标题不足 2 个时，可根据多组题号回到 1 自动分组，例如 `1…19 → 1…19` 会在 `19｜1` 之间拆卷。为避免把小问或题型切换误当新卷，每组必须从 1 开始、至少有 5 个不同题号、题号覆盖率至少 85%，相邻组题号集合重合至少 80%；解析侧还必须得到相同组数并逐组满足题号重合。“第×份参考答案”这类普通标题可在序号和题号覆盖都一致时配对；个别题解析缺失会记录题号并留空，其余题仍可正常入库，不按位置错配。若双方已经出现明确但互相冲突的标题、标题重名、组数不等或专题对应不上，仍会停止，不按页序猜答案。合集内少量 A–D 选项文字被右侧插图挤掉时，只有在原题壳唯一、完整候选唯一且 MinerU 坐标证明文字列位置的情况下，才对对应单页做有界补识别并把选项文字合回原题干和原图；任何歧义都保留待复核。Doc2X 合集会在初次识别和缓存重试时按页面坐标恢复跨题图片及唯一四图选项，重复执行不会继续搬图。校对页可给每道题额外加入最多 20 张图片，立即预览、拖动排序并选择位置与上下／左右排列；确认后才保存进 `_assets`。带图选择题默认仅选项分栏，解答题默认仅小问分栏，填空题默认题干分栏，多图默认上下排列。目标文件夹在打开选择器后逐层加载，进入导入页不再递归扫描整棵题库。OCR 可逐批选择 MinerU 或 Doc2X v3，后续再独立选择整篇 AI、逐块 AI 或纯机械拆题。机械拆题会展开带“题号／答案”首列的标准答案表，并在整批题号唯一、连续且完整时按题号归序；重复或缺号时不猜测重排。导入只检查本次内容内部的重复块，不再为每次导入扫描整个历史题库；跨批次或历史重复请按需打开「查重」。选择题标签会在边界明确时统一为 A.–D.，概率事件 A/B 等歧义保留待复核；解答题连续小问自动分段；OCR 结构异常只在强证据成立时机械修复，否则进入校对提示
- **图片与批量识别防丢**：同一题干或解析框中的多张图片按选择顺序逐页合成 PDF，任何一张损坏都会让该组明确失败，不生成缺页半成品；同组多份 PDF／Word 或图片与文档混选会直接拒绝，不再只取第一份。纯图片 `.docx` 会按文档内图片顺序逐图一页，避免 Pandoc 把整页扫描图压缩挤页。MinerU 强制 OCR 重试与首轮附件各自隔离，只采用正文、题号和图片覆盖更完整的结果；高置信正文缺失、题号缺口、模型删掉原图引用或 OCR 附件损坏时会标记 `【必须人工校对】` 并暂停自动入库。最终图片按内容摘要存入 `_assets`，重复转换可复用相同文件，内容不同的同名图不会覆盖旧题资产
- **MinerU 下载可恢复**：服务端识别完成后的结果 ZIP 以流式临时文件下载；连接中断时最多 6 次从已收字节继续，并校验 Range、ZIP 成员 CRC 与解压结果。任务工作区只保存 batch_id、源文件摘要、解析参数和 Token 指纹，不保存 Token 明文或带签名下载地址；点击“重新转换”会优先用原 Token 查询同一服务端任务并续传，不会重新上传整本或重复 OCR。由旧版本创建、且失败前没有留下 batch_id 的任务不具备这一恢复能力
- **可靠的多批转换**：一次排多组任务，后台并发识别（默认同时 3 组），支持单组中止、批次翻页和重启后继续审核已完成结果
- **拆题人工审核**：逐题识别默认“全部不送入 AI”并机械渲染（不花额度）；也可主动改为切块后送 AI，或先人工调整块的合并/拆分/顺序再送 AI
- **AI 识别**：PDF/Word/图片 → MinerU 或 Doc2X OCR → DeepSeek（或任意 OpenAI 兼容模型）规范化，也可跳过 LLM 直接机械拆题
- **题库管理与原地编辑**：文件夹分类、标签、难度、题型筛选，去重检测，回收站（软删除可恢复）；独立桌面端侧栏使用“文件/筛选”双页签，优先把完整文件树留在最小窗口首屏，有筛选生效时会显示状态点并在本机记住最后页签。文件夹可直接拖到另一文件夹下成为子级，也可拖到“全部题目”恢复为顶层；自身及后代落点会被拒绝。右下角加号会在当前文件夹末尾直接展开一张新题卡，填写并保存后才创建 Markdown。新题与现有题的“原地编辑”共用源码模式、实时编译和阅读模式，Markdown/KaTeX 预览复用正式题卡渲染规则，保存后只替换当前题卡。桌面题卡将题型、难度收成可点击摘要，题源与删除收入更多菜单；浏览器和 Obsidian 仍保留原控件布局。打开父文件夹会汇总显示所有后代原卷和题目，题目首屏只加载 30 道，接近列表底部再按 30 道连续追加，不分页也不一次创建全部题卡。拖动题目接近窗口上下边缘时页面会自动滚动。首次进入题库时侧栏目录全部折叠，展开时按需读取子目录；深链接只展开当前路径。刷新会补回已浏览批次并恢复到原题卡的原视口位置。全部标签与“加入/移动到”目录按需加载，进入单卷只扫描对应子树。题型、难度、标星、图片位置、删除、转移及勾选批量操作只更新当前题卡或题目列表，不整页重载；选择题可选仅选项／整题分栏、题干选项之间或题后，解答题把小问作为对应内容区，填空题只提供题干分栏或题后。任意多图可上下／左右排列并拖动换序，题卡与导出效果同源；“导出宽度”以页面百分比保存，可选 25%／35%／50%／70% 快捷档，窗口大小只影响屏幕预览，不改变 PDF 排版比例。解析图片另有“图文混排”，文字先环绕图片并在图片下方恢复整行宽度，不挤占题干的图片布局设置。题卡默认折叠解析，点击“解析”按需展开或收起；展开后只展示字段正文，不再自动加“【解析】”前缀。题目与解析页面可预览 MinerU HTML 表格和 Markdown 管道表格，宽表在卡片内横向滚动
- **图片生命周期**：题目软删除只移动 Markdown，图片继续保留以便恢复。彻底删除或清空回收站时，仅在全部登记题库、讲义与安全备份都不再引用后清理候选图。查重页的“共享图片库体检”可全量扫描；任一题库不可访问就拒绝删除，扫描结果还需二次确认并重新核验，删除永久且不可恢复
- **多题库、科目与共享图片**：桌面端记住最多 100 个本机题库路径，打开或新建时可选数学／物理。所有题库使用 `desktop.json` 中唯一的共享图片目录；在题库管理器切换目录时，程序会先把各题库旧 `_assets` 无损复制到目标，同内容复用、同名异内容拒绝，源目录不删除。题库都在一个 Obsidian vault 时应选择公共 vault 的 `_assets`。物理题库沿用相同识别与存储逻辑，仅把“填空题”显示和导出为“实验题”，标准试卷科目默认改为“物理”。当前题库固定显示在列表首位；可打开已有目录或在指定父目录中新建空题库，切换后自动重启本地后端。断开的移动盘会标记“不可用”，仍可从列表移除；当前题库不能直接移除。列表、科目、当前路径和共享图片目录只存在本机 `desktop.json`，不上传。除用户明确切换图片目录时的安全复制外，题库管理器不复制、移动或删除题库内容
- **软件内资料库**：按当前题库目录惰性展开文件树，在同一工作区用常驻标签阅读任意 UTF-8 Markdown、PDF 和常见图片；PDF 使用 WebView2 内置阅读器，切换标签或顶部栏目不会重建。支持左右／上下双分栏、比例拖动和标签跨栏移动，布局与标签会在当前窗口会话中恢复。Markdown 可在源码模式直接修改和保存，阅读模式即时渲染当前草稿及 KaTeX；若 Obsidian 已在外部改动同一文件，保存会拒绝覆盖并保留草稿。文件接口只允许题库根目录内的可见白名单文件，PDF 与图片只读
- **讲义编排**：独立“讲义”栏目提供已选试题栏、固定分页纸张和 Markdown 工具栏；支持 H1–H6、列表、引用、粗斜体、行内／块公式、显式分页与 A4 单栏、A4 双栏、16:9 横版。题目拖入后先在右栏编辑题干、解析和任意题号，点击“确定并编译”即通过正式 Pandoc/XeLaTeX 模板生成蓝框成品卡；画布只保留成品，再点题卡才重开右栏。解析可隐藏、题后或文末，正文直接从解析字段开始，不自动显示“【解析】”；解析图片可独立启用文字绕图混排，图片下方继续排正文。WIMath 标志可选为 A4 左上／横版左下，A4 标志与纸张顶边保留安全间距。16:9 题目区按页面宽度 70% 靠左，逐图大小设置在该题目区内继续生效。讲义以 `_handouts/*.md` 为唯一真源，支持安全删除整份讲义，1 秒防抖保存并以 mtime 保护外部修改；自动分页与 SVG 都不写盘，最终仍以整页 PDF 为准
- **组卷导出**：按条件筛选题目，可导出普通/标准试卷、A4 双栏刷题册或 16:9 横版课堂课件；所有模式均可选择纯白或米黄护眼纸色、设置横跨整页的六位置页眉页脚，并可选 WIMath 标志（A4 左上、横版左下）。16:9 题目区按页面宽度 70% 靠左，题卡设置的图片大小会按比例反映到 PDF。双栏模板按单选、多选、填空、解答分区连续流排；小题先连续填栏，第一道解答题只有在当前栏真实剩余高度足够容纳整题和作答区时才跟在后面，否则换栏，此后每道解答题独占一列。双栏中的图文分栏会自动改为文字环绕右图，图片下方恢复完整栏宽，其余模式继续使用原图文分栏与插图自动布局。导出的逐题解析同样不添加“【解析】”前缀，文末“参考解析”等分区标题仍保留
- **纯本地存储**：每题一个 `.md` 文件（YAML frontmatter + 正文），文件夹即真实目录，可直接用 Obsidian 打开题库目录当 vault 浏览编辑
  - 导入时能认出原卷题号的题，文件就按题号命名（`第3题.md`），在文件夹里一眼可寻；认不出的（手工新增、校对页拆出来的）用随机 id 命名。**文件名不参与身份认定**——身份取 frontmatter 里的 `id`，随手改名不会断任何引用
- **用户内容零篡改**：LaTeX 公式、自定义分区（`## 标题`）原样保留，不做任何静默清洗

## 快速开始

### 1. 环境准备

需要 Python 3.11+（开发环境用 3.13）。

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

从源码运行时，组卷导出还需要可探测到：

- **Pandoc**（Markdown → LaTeX）：https://pandoc.org/installing.html
- **XeLaTeX**（LaTeX → PDF，推荐 MiKTeX 或 TeX Live，需支持中文）：https://miktex.org/

当前 Windows 安装包已自带 Pandoc，目标电脑只需为“直接生成 PDF”补 XeLaTeX；仅导出 `.tex` / `tex.zip` 不需要另装组件。

安装后 `config.py` 会依次从环境变量、随包 `runtime/`、系统 `PATH` 和 Windows 常见安装目录探测 `pandoc`/`xelatex`；开发时也可用 `QUIZFORGE_PANDOC`、`QUIZFORGE_XELATEX`、`QUIZFORGE_DVISVGM` 指定绝对路径。

### 2. 启动

```powershell
python app.py
```

浏览器打开 http://127.0.0.1:5000 即可使用。

### 3.（可选）配置 AI 识别

不配置也能用全部核心功能（手动粘贴导入、题库管理、组卷导出）。上传 PDF/图片自动识别至少需要配置一种 OCR；只有选择 AI 规范化时才还需要 LLM：

1. **MinerU 或 Doc2X**（OCR）：MinerU Token 在 https://mineru.net 创建；Doc2X API Key 在 Doc2X 控制台创建。导入任务中可逐批切换，默认仍为 MinerU
2. **DeepSeek 或任意 OpenAI 兼容模型**（可选规范化）：在对应服务创建 API Key；选择“全部不送入 AI”时不需要它

配置方式二选一：

- **在设置页填写**（推荐）：启动后打开“设置”页，分别维护 MinerU Token、Doc2X Key 和 LLM 配置（支持 DeepSeek / 阿里云百炼 / 硅基流动 / 中转站 / 自建等任意 OpenAI 兼容接口），凭据都会加密存储在本地
- **或**：复制 `vendor/project_alpha/.env.example` 为 `vendor/project_alpha/.env`，填入 `MINERU_API_TOKEN` 和 `DEEPSEEK_API_KEY`

MinerU Token、Doc2X Key 与 LLM 配置都可在设置页维护；LLM 配置支持新增、编辑、按“导入识别/配图重绘”分别启停，编辑时 API Key 留空会保留原值。勾选“配图重绘”的视觉配置会出现在设置页快速切换区，点模型按钮即可切换，不影响导入识别所用模型。

规范化那一步也可以接**本机的本地推理服务**（Ollama、LM Studio、vLLM 等），Base URL 直接填 `http://127.0.0.1:11434/v1` 这类回环地址即可——单机版对 loopback 放行。非回环的内网地址（如 `http://192.168.x.x`）和非 HTTPS 的公网地址仍会被拦下。

## 安全说明

**本工具只监听 `127.0.0.1`，没有任何用户认证机制，设计上仅供本机单人使用。请勿修改代码把它暴露到公网或局域网，也不要在多用户共享的机器上运行——任何能访问这台机器网络端口的人都能读写你的题库。**

API Key 使用 Fernet 对称加密后存储在 `data/.enc_key`（密钥文件）以及 `data/providers.json`、`data/mineru.json`、`data/doc2x.json`（加密后的配置）。**这个密钥文件如果被删除或更换，已保存的所有 Key 会永久无法解密**，只能在设置页重新填写。上述文件已在 `.gitignore` 中排除，切勿手动提交到 git 或加入公开备份/同步工具。

软件许可证使用另一套 Ed25519 非对称签名：应用只携带公钥，发布者私钥只用于签发 `.qflicense`。设备身份是 32 字节本机随机秘密，经 Windows DPAPI 当前用户保护后写入运行数据目录；签名许可证只记录其不可逆摘要。删除设备身份会生成新请求码并使旧许可证失配。公钥本身可以公开；私钥一旦泄露，任何人都能伪造许可证，必须立即换钥并重新构建应用。纯离线方案也无法即时撤销、阻止系统时钟回拨或抵抗有能力修改本机程序的人；它用于控制正常内测分发，不应宣称绝对防破解。当前 PyInstaller 包能避免直接分发 `.py` 业务源码，但不能承诺无法逆向或无法绕过本机校验。

所有本地写请求都需要页面随机令牌，试卷上传会检查 PDF/DOCX/图片的真实内容；这用于阻止普通网页借浏览器向 `127.0.0.1` 伪造写请求。后端重启后若旧页面提示安全令牌失效，刷新页面即可。

## 目录结构

```
软件版/
  app.py                 Flask 应用、题目原地编辑与资料库读取路由
  desktop.py             独立 Windows 桌面壳（pywebview + 本地随机端口）
  desktop_product.py     首次启动示例、环境诊断与产品版本信息
  device_identity.py     DPAPI 保护的随机设备身份与请求码
  license_manager.py     Ed25519 离线许可证验签与安全导入
  service_ports.py       离线授权、更新、远程导出的统一预留边界
  config.py               路径与常量配置
  filestore.py             文件式题库存储层（每题一个 .md）
  importer.py              Markdown 题目切分逻辑
  import_defaults.py       导入图片默认位置与排列规则
  exporter.py               PDF 导出（pandoc + xelatex）
  export_tables.py         页面与导出共用的表格行列解析
  handouts.py               讲义 schema、快照、标记与安全原子存储
  handout_exporter.py       讲义 PDF/TeX/ZIP 导出适配层
  converter.py             OCR 后端与下游拆题方式的统一转换层
  pdf_collection.py        有书签合集的 OCR 前拆卷与题干/答案配对
  collection_structure.py  无书签合集的 OCR 后结构分组与严格题解配对
  doc2x_client.py          Doc2X v2/v3 API、ZIP 导出与布局修复
  doc2x_store.py           Doc2X 多 Key 加密存储
  ocr_pool.py              MinerU/Doc2X 统一凭证轮转与进程级并发控制
  imgorder.py              用 MinerU 页面坐标恢复题干图与 A-D 选项图归属
  qrender.py                页面正文渲染（选项分列/图片落位与 PDF 同源）
  dedup.py                 题目去重检测
  crypto_utils.py           API Key 加密存储
  llm_client.py             通用 OpenAI 兼容 LLM 客户端
  providers.py              多套 LLM 配置管理（cc-switch 风格）
  task_store.py             转换任务跨重启快照
  cleanup_output.py         过期任务、上传件与导出产物清理
  blockpipe.py / blocksplit.py / blocknorm.py / mechfix.py
                             另一套「机械切块」识别引擎（可选）
  qualcheck.py / optcheck.py  识别结果质量检查、选项完整性检查
  corpus.py                 识别语料留档（OCR 中间产物存 data/corpus）
  tests/                    核心可靠性回归（标准库 unittest）
  tools/eval_split.py       离线切题语料回归与基线比较
  tools/eval_doc2x.py       Doc2X 真实 PDF 回归（哈希缓存，避免重复计费）
  tools/smoke_desktop_release.py 捆绑 Pandoc 与 tex.zip 发行烟测
  tools/smoke_device_license.py 隔离启动成品并验证设备绑定授权
  tools/license_signer.py 发布者私钥生成与许可证签发（不进发行包）
  tools/license_admin.py 仓库外本地台账、签发、续期、换机与作废（不进发行包）
  tools/verify_desktop_bundle.py 发行包源码、私钥与运行数据扫描
  assets/                  应用 PNG/ICO 图标
  installer/               Inno Setup 脚本、预览许可与第三方声明
  build_desktop.ps1        Windows 桌面目录版构建入口
  build_installer.ps1      Windows 安装包构建入口
  requirements-desktop.txt 桌面构建依赖
  runtime/                 随包 Pandoc 与可选 TeX 运行时孔位
  templates/                 Jinja2 页面模板
  frontend/                  讲义编辑器源码与 Node 单测（仅开发期）
  static/                    CSS 与已编译的本地讲义编辑器
    js/text-preview.js       编辑/导入/拆题审核及资料库的安全 Markdown 预览
    js/inline-editor.js      题卡源码/实时编译/阅读三模式
    js/image-layout.js       题卡图片位置、方向、缩放与拖动换序
    js/import-preview-images.js 导入题卡图片添加、预览与排序
    js/library-tabs.js       Markdown/PDF/图片多标签阅读器
  vendor/project_alpha/       MinerU OCR 与整篇 LLM 规范化的内置实现
  data/                       题库数据（.gitignore 排除，本机私有）
  output/                    导出产物（.gitignore 排除）
  <题库>/_handouts/          普通 Markdown 讲义（按题库懒创建）
```

## 关于 `vendor/project_alpha`

这是 MinerU 路径使用的内置 PDF/图片规范化引擎（PDF/Word/图片 → MinerU OCR → DeepSeek → 规范化 Markdown），已作为内部包 vendor 进本项目，不依赖任何外部路径。Doc2X 路径由 `doc2x_client.py` 调官方 API，再把相同的 Markdown 中间格式交给现有拆题流水线。

MinerU、Doc2X 和云端 LLM 都可能产生调用费用；Doc2X 与 MinerU 互为可选 OCR，不会在同一任务中自动双跑。该限制不影响题库管理、手动导入和组卷导出在无网络/无密钥情况下正常工作。

## License

本项目仅供个人学习与本地使用，未附带开源许可证。如需二次分发或商用，请先与作者确认。
