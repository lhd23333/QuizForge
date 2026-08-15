"""路径与常量配置。

所有外部工具路径集中在此，方便换机器时统一修改。
"""

import os
import shutil
from pathlib import Path
import hashlib

# 程序资源目录（源码运行时就是项目根；Nuitka standalone 中是 exe 所在目录）。
# 模板、静态资源和 vendor 代码只读，始终从这里取。
BASE_DIR = Path(__file__).resolve().parent

# 运行数据目录。直接 `python app.py` 开发时沿用仓库内 data/，独立桌面程序则由
# desktop.py 传入 `%LOCALAPPDATA%\QuizForge`。安装目录以后可能位于 Program Files，
# 普通用户无写权限，密钥、设置与任务快照绝不能继续写在程序旁边。
_DATA_DIR_ENV = os.environ.get("QUIZFORGE_DATA_DIR", "").strip()
DATA_DIR = Path(_DATA_DIR_ENV) if _DATA_DIR_ENV else BASE_DIR / "data"

# 题库根目录：每题一个 .md 文件（YAML frontmatter + 正文），文件夹即真实目录。
#
# 这个目录直接就是一个 Obsidian vault（QuizForge 仓库），题目在 Obsidian 里
# 是普通笔记，可实时预览和编辑；_assets 也在 vault 内，正文的 ![[图片]] 双链
# 能被 Obsidian 原生解析。
#
# Obsidian 插件启动本应用时会用 QUIZFORGE_BANK 传入它所在 vault 的根目录；
# 手动 python app.py 时走下面的默认值。两者指向同一处，避免「怎么启动决定看到
# 哪个题库」的分歧。
BANK_DIR = Path(os.environ.get("QUIZFORGE_BANK")
                or (Path.home() / "Documents" / "QuizForge"))

# 科目是题库级显示属性，识别与存储仍统一使用既有题型值。这样物理题库只把
# “填空题”显示为“实验题”，不会为同一套切题逻辑再维护一份分叉。
BANK_SUBJECT = os.environ.get("QUIZFORGE_SUBJECT", "math").strip().lower()
if BANK_SUBJECT not in {"math", "physics"}:
    BANK_SUBJECT = "math"
BANK_SUBJECT_LABEL = {"math": "数学", "physics": "物理"}[BANK_SUBJECT]

# 多题库可同时由多个桌面进程打开。凭据、许可证和外观仍放全局 DATA_DIR；会包含
# 题目 id、上传件或 OCR 中间产物的运行状态必须按题库隔离，避免两个窗口串任务。
_BANK_STATE_ENV = os.environ.get("QUIZFORGE_BANK_STATE_DIR", "").strip()
if _BANK_STATE_ENV:
    BANK_STATE_DIR = Path(_BANK_STATE_ENV)
elif _DATA_DIR_ENV and os.environ.get("QUIZFORGE_DESKTOP", "") == "1":
    _bank_digest = hashlib.sha256(
        os.path.normcase(str(BANK_DIR.resolve())).encode("utf-8")
    ).hexdigest()[:16]
    BANK_STATE_DIR = DATA_DIR / "banks" / _bank_digest
else:
    BANK_STATE_DIR = DATA_DIR

# 回收站：软删除的题目/文件夹移到这里（相对 BANK_DIR 的原路径记在 frontmatter 里，
# 供精确恢复），仿 Obsidian 自己的 .trash 目录约定。
TRASH_DIR = BANK_DIR / ".trash"

# 图片资产目录：桌面版允许多题库显式共用唯一目录，避免把一个旧 vault 拆成数学／
# 物理子题库后，每个进程只认自己的 ``BANK_DIR/_assets``，让原图明明仍在父级却全部
# 404。未配置时仍退回当前题库的 ``_assets``，保持源码运行和 Obsidian 插件兼容。
# 正文只保存文件名，因此共享目录仍必须是扁平目录；桌面端负责在切换目录前合并旧图。
_ASSETS_DIR_ENV = os.environ.get("QUIZFORGE_ASSETS_DIR", "").strip()
ASSETS_DIR = (Path(_ASSETS_DIR_ENV).expanduser().resolve()
              if _ASSETS_DIR_ENV else BANK_DIR / "_assets")

# 讲义工作台的普通 Markdown 文档。目录位于题库内，随多题库切换自然隔离；但它
# 不是题目文件夹，filestore 的题目扫描和目录树必须显式跳过。目录只在首次新建
# 讲义时创建，避免打开一个旧题库就无端修改其磁盘结构。
HANDOUTS_DIR = BANK_DIR / "_handouts"

# LLM 识别模型配置（cc-switch 风格，多套可切换）；API Key 用 crypto_utils 加密存储。
PROVIDERS_PATH = DATA_DIR / "providers.json"

# MinerU API Token（OCR）：同样只存 Fernet 密文，与 LLM 的 API Key 用同一把
# ENC_KEY_PATH。单独一个文件而不是塞进 providers.json —— MinerU 只有一份 token、
# 没有「多套切换」的概念，混进那份列表结构里会让 providers.py 的 active 语义变形。
MINERU_TOKEN_PATH = DATA_DIR / "mineru.json"

# Doc2X API Key（第二条 OCR 链路）：与 MinerU、LLM 共用本机 Fernet 密钥，
# 但单独落文件，避免把不同服务的凭证与启停语义混在一起。
DOC2X_KEY_PATH = DATA_DIR / "doc2x.json"

# 界面外观偏好（深浅色 / 主题色 / 壁纸文件名）。单用户，一个 JSON 就够；
# 服务器版存在 users 表的 theme_mode / theme_color / wallpaper 三列。
UI_PREFS_PATH = DATA_DIR / "ui_prefs.json"

# 转换任务快照。插件退出会停止后端，已完成结果与待审核状态必须跨进程保留。
TASKS_PATH = BANK_STATE_DIR / "conversion_tasks.json"

# 组卷篮选题状态。题目本身仍在 vault，只在这里保存被选中的题目 id。
SELECTIONS_PATH = BANK_STATE_DIR / "selections.json"

# 离线许可证是用户数据，不随安装包覆盖；发行包只携带公钥，签发私钥永远不被
# 应用读取。源码/Obsidian 模式默认不强制，独立桌面入口会显式开启校验。
LICENSE_PATH = DATA_DIR / "license.qflicense"
LICENSE_PUBLIC_KEY_PATH = BASE_DIR / "assets" / "license_public_key.pem"
# 设备身份只包含随机秘密的 Windows DPAPI 密文，不采集主板、硬盘或题库信息。
# 与许可证一样放在用户数据目录，升级/卸载应用都不能把它当安装资源覆盖。
DEVICE_IDENTITY_PATH = DATA_DIR / "device_identity.dat"

# 壁纸文件存放目录（用户上传的图片/视频）。放 BASE_DIR 而不是 BANK_DIR：
# 它是应用外观配置，不是题库内容，落进 vault 会被 Obsidian 当成笔记附件。
WALLPAPER_DIR = DATA_DIR / "wallpaper"

# 壁纸大小上限，与服务器版一致（图片含动图 25MB / 视频 100MB）
WALLPAPER_MAX_IMAGE = 25 * 1024 * 1024
WALLPAPER_MAX_VIDEO = 100 * 1024 * 1024

# API key 加密密钥：不进 git、不进任何备份。删掉它=已存的 key 永久解不开。
ENC_KEY_PATH = DATA_DIR / ".enc_key"

# 导出产物目录。源码开发继续落在仓库 output/；桌面产品落在用户数据目录，避免
# 安装目录只读。两条路径都由 cleanup_output.py 做 24 小时保守清理。
OUTPUT_DIR = (BANK_STATE_DIR / "output") if _DATA_DIR_ENV else (BASE_DIR / "output")

# LaTeX 模板（复用自 project-alpha，pandoc 用）
TEX_TEMPLATE = BASE_DIR / "exam_template.tex"
# 正式黑色 LaTeX 横向标志随软件发行。源码工作区也允许从同级品牌项目读取，
# 这样品牌资产仍只有一份权威源；桌面构建会把它复制进发行包的 assets/。
_WIMATH_PACKAGED_LOGO = BASE_DIR / "assets" / "wimath-logo-latex-black.pdf"
_WIMATH_BRAND_LOGO = BASE_DIR.parent / "WIMath品牌" / "assets" / "pdf" / "wimath-logo-latex-black.pdf"
WIMATH_LOGO_PDF = (
    _WIMATH_PACKAGED_LOGO if _WIMATH_PACKAGED_LOGO.is_file() else _WIMATH_BRAND_LOGO
)

def _tool_path(env_name: str, command: str, bundled: list[Path],
               installed: list[Path]) -> str:
    """按“显式覆盖 → 随软件附带 → PATH → 常见安装目录”寻找外部工具。"""
    def _is_file(path: Path) -> bool:
        try:
            return path.is_file()
        except OSError:
            # Windows Store/受保护目录可能连 stat 都拒绝；它只是一个候选位置，
            # 无权访问时继续找下一项，不能让可选排版工具拖垮应用启动。
            return False

    override = os.environ.get(env_name, "").strip()
    if override:
        return override
    for path in bundled:
        if _is_file(path):
            return str(path)
    found = shutil.which(command)
    if found:
        return found
    for path in installed:
        if _is_file(path):
            return str(path)
    # 保留命令名，让调用点给出统一的“找不到可执行文件”错误，不回落到开发者私有路径。
    return command


_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", ""))
_PROGRAM_FILES = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
_TEX_BINS = [
    BASE_DIR / "runtime" / "tex" / "bin" / "windows",
    BASE_DIR / "runtime" / "tex" / "miktex" / "bin" / "x64",
]

# runtime/ 是未来离线排版组件的固定挂载点；初版构建可以不带它，此时继续使用
# 用户电脑已安装的 Pandoc/MiKTeX，所有题库与导出业务逻辑保持不变。
PANDOC = _tool_path(
    "QUIZFORGE_PANDOC", "pandoc",
    [BASE_DIR / "runtime" / "pandoc" / "pandoc.exe"],
    [_LOCAL_APP_DATA / "Pandoc" / "pandoc.exe",
     _PROGRAM_FILES / "Pandoc" / "pandoc.exe"],
)
XELATEX = _tool_path(
    "QUIZFORGE_XELATEX", "xelatex",
    [path / "xelatex.exe" for path in _TEX_BINS],
    [_LOCAL_APP_DATA / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "xelatex.exe",
     _PROGRAM_FILES / "MiKTeX" / "miktex" / "bin" / "x64" / "xelatex.exe"],
)
DVISVGM = _tool_path(
    "QUIZFORGE_DVISVGM", "dvisvgm",
    [path / "dvisvgm.exe" for path in _TEX_BINS],
    [_LOCAL_APP_DATA / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "dvisvgm.exe",
     _PROGRAM_FILES / "MiKTeX" / "miktex" / "bin" / "x64" / "dvisvgm.exe"],
)

# project-alpha（PDF/图片 → MinerU OCR → DeepSeek 规范化）已整体 vendor 进本项目，
# 不再依赖外部路径；converter.py 以此为根注入 sys.path，并以此为 CWD 读取 .env。
PROJECT_ALPHA = str(BASE_DIR / "vendor" / "project_alpha")

# MinerU 解压结果、续传快照和合集缓存都会被转换任务长期引用。桌面版必须随题库
# 状态目录隔离，避免同时打开数学、物理题库时读写同一份安装目录缓存；源码开发
# 没有独立用户数据目录时保留旧位置，兼容已有调试语料与离线回归夹具。
OCR_WORKSPACE_ROOT = (
    (BANK_STATE_DIR / "raw_md") if _DATA_DIR_ENV
    else (Path(PROJECT_ALPHA) / "output" / "raw_md")
).resolve()

# 上传文件暂存目录
UPLOAD_DIR = BANK_STATE_DIR / "uploads"

# 方式四「多组 PDF 批量导入」的上传子目录：与方式一分开存放，避免方式一
# 每次转换前的 _clean_uploads 误删方式四正在校对中的在途文件。
BATCH_UPLOAD_DIR = UPLOAD_DIR / "batch"

# 识别语料留档目录：OCR 中间产物（_raw.md / _blocks.json / _normalized.md）。
# 刻意放在 BASE_DIR 而不是 BANK_DIR —— 题库根就是 Obsidian vault，几百份中间
# 产物落进去会被 Obsidian 自己的搜索和图谱吃到（filestore 的 _skip_rel 只挡住
# 本应用的扫描，管不了 Obsidian）。
CORPUS_DIR = BANK_STATE_DIR / "corpus"

# 未来授权、自动更新和远程导出的配置孔位。初版文件不存在时全部走离线默认值，
# service_ports.py 不会建立任何网络连接。
SERVICE_PORTS_PATH = DATA_DIR / "service_ports.json"

# 图片资产目录的别名：从服务器版移植来的模块用 IMAGES_DIR 这个名字。单机版图片
# 扁平存在 _assets 下（无 scope 子目录），正文用 ![[文件名]] 双链引用。
IMAGES_DIR = ASSETS_DIR

# ---------------------------------------------------------------------------
# 上传边界：与服务器版 upload_guard.py 的同名常量逐条对齐
#
# 软件版此前只有一条 200MB 的原卷限额和一个写死在 app.py 里的「20 组」，中间那几
# 层（每侧文件数、单批文件数、整次请求总量）根本没有，于是一次只能排 20 组，而
# 服务器版早就放到 500 组了。这里把整套搬过来，值一处定义、前后端共用。
#
# **这些不是安全校验**（扩展名与 Content-Type 都由客户端提供，能伪造）。软件版只
# 监听 127.0.0.1、单人本机使用，没有服务器版 upload_guard 的多用户配额与内容验真
# 那一层需求；这些数只用来挡住明显的误操作，以及给「前端脚本出 bug 反复 append
# 同一批文件」这类异常留一个尽头。
# ---------------------------------------------------------------------------

_MB = 1024 * 1024

# 单批任务组数上限。服务器版是 500，这里按需求放到 1000。
#
# 放宽是安全的：**组数只决定队列长度，不决定同时在跑几组**——并发始终由下面的
# BATCH_CONVERT_CONCURRENCY 单独控制，多出来的组在线程池里排队等。所以 1000 组
# 只是让队列变长（「攒一整晚的卷子一次丢进去跑」），不会同时占用更多内存，也不会
# 同时打更多 MinerU 并发。
MAX_BATCH_GROUPS = 1000

# 单批文件总数，以及每组题干/解析各自的文件数（多图合成一份卷子时会用满）
MAX_BATCH_FILES = 2000
MAX_FILES_PER_GROUP_SIDE = 200

# 单文件大小。这两条才是真正防「单个超大文件打爆内存」的墙，跟着服务器版没动。
# 图片那条更小是因为 MinerU 对图片直传另有硬限制（见 converter._IMAGE_DIRECT_LIMIT_BYTES）。
MAX_EXAM_DOCUMENT_BYTES = 200 * _MB
MAX_EXAM_IMAGE_BYTES = 25 * _MB

# 整次请求的总量天花板（Flask 的 MAX_CONTENT_LENGTH）。服务器版是 2048MB，因为
# 那个值必须与 deploy/nginx.conf 的 client_max_body_size 一致；软件版没有反向代理，
# 而 1000 组带图的卷子轻易就过 2GB，卡在那儿等于让上面放宽的组数白放。
MAX_REQUEST_BYTES = 8192 * _MB

# 方式二（批量上传 md）的限额，同样照抄服务器版
MAX_MD_FILES = 50
MAX_MD_FILE_BYTES = 5 * _MB
MAX_MD_BATCH_BYTES = 20 * _MB

# 允许的扩展名。EXAM_* 三条对应服务器版 upload_guard 的同名集合；`.doc` 刻意
# **不在**里面——pandoc 读不了旧版二进制格式（见 converter._docx_to_pdf），收下它
# 只会让用户白等一趟转换再看到报错。前端仍把 .doc 放进 accept，但在提交前就拦下
# 并给出「另存为 .docx」的提示，比在文件选择器里直接看不见它更好懂。
EXAM_DOCUMENT_EXTS = {".pdf", ".docx"}
EXAM_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
EXAM_EXTS = EXAM_DOCUMENT_EXTS | EXAM_IMAGE_EXTS
MD_EXTS = {".md", ".markdown", ".txt"}

# 批次工作线程只负责让任务尽快进入统一 OCR 队列；真正的云端并发由下面三层
# 进程级槽位控制。多个批次会共享同一组槽，不会再把每批并发简单相加。
BATCH_CONVERT_CONCURRENCY = 12

# 官方文档口径：Doc2X 默认 10 个 PDF 并发，先留 2 个余量；MinerU 未承诺解析
# 并发，只公开提交频控和服务端 pending 队列，保守从 6 起。全局 12 防止两个后端
# 同时满载时把上传带宽和本机预处理一起压满。
OCR_TOTAL_CONCURRENCY = 12
MINERU_CONCURRENCY = 6
DOC2X_CONCURRENCY = 8
OCR_LIMIT_COOLDOWN_SECONDS = 15

# 同时进行的 xelatex 编译数。单机单人，1 足够，避免多个 xelatex 抢 CPU。
EXPORT_CONCURRENCY = 1

# 题目类型选项
QUESTION_TYPES = ["单选题", "多选题", "填空题", "解答题"]


def question_type_label(qtype: str) -> str:
    """返回当前题库的题型显示名；底层值始终保持兼容的规范名称。"""
    if BANK_SUBJECT == "physics" and qtype == "填空题":
        return "实验题"
    return qtype

# 难度选项：1-5（1 最易，5 最难）；空表示未设
DIFFICULTIES = ["1", "2", "3", "4", "5"]
