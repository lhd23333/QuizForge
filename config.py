"""路径与常量配置。

所有外部工具路径集中在此，方便换机器时统一修改。
"""

import shutil
from pathlib import Path

# 项目根目录（本文件所在目录）
BASE_DIR = Path(__file__).resolve().parent

# 题库根目录：每题一个 .md 文件（YAML frontmatter + 正文），文件夹即真实目录，
# 可直接用 Obsidian 打开这个目录当 vault 浏览/编辑。
BANK_DIR = BASE_DIR / "data" / "bank"

# 回收站：软删除的题目/文件夹移到这里（相对 BANK_DIR 的原路径记在 frontmatter 里，
# 供精确恢复），仿 Obsidian 自己的 .trash 目录约定。
TRASH_DIR = BANK_DIR / ".trash"

# 图片资产目录：扁平存放，正文用 Obsidian 双链嵌入语法 ![[<id>_N.ext]] 引用，
# 这样图片引用不受题目所在文件夹改名/移动影响。
ASSETS_DIR = BANK_DIR / "_assets"

# LLM 识别模型配置（cc-switch 风格，多套可切换）；API Key 用 crypto_utils 加密存储。
PROVIDERS_PATH = BASE_DIR / "data" / "providers.json"

# API key 加密密钥：不进 git、不进任何备份。删掉它=已存的 key 永久解不开。
ENC_KEY_PATH = BASE_DIR / "data" / ".enc_key"

# 导出产物目录
OUTPUT_DIR = BASE_DIR / "output"

# LaTeX 模板（复用自 project-alpha，pandoc 用）
TEX_TEMPLATE = BASE_DIR / "exam_template.tex"

# 外部工具路径：优先用 PATH 里能找到的，找不到时退回本机曾验证过的默认安装路径
# （其他机器上这两个默认路径大概率不存在，只是保留一个兜底提示）。
PANDOC = shutil.which("pandoc") or r"C:\Users\Lenovo\AppData\Local\Pandoc\pandoc.exe"
XELATEX = (shutil.which("xelatex")
           or r"C:\Users\Lenovo\AppData\Local\Programs\MiKTeX\miktex\bin\x64\xelatex.exe")

# project-alpha（PDF/图片 → MinerU OCR → DeepSeek 规范化）已整体 vendor 进本项目，
# 不再依赖外部路径；converter.py 以此为根注入 sys.path，并以此为 CWD 读取 .env。
PROJECT_ALPHA = str(BASE_DIR / "vendor" / "project_alpha")

# 上传文件暂存目录
UPLOAD_DIR = BASE_DIR / "data" / "uploads"

# 方式四「多组 PDF 批量导入」的上传子目录：与方式一分开存放，避免方式一
# 每次转换前的 _clean_uploads 误删方式四正在校对中的在途文件。
BATCH_UPLOAD_DIR = UPLOAD_DIR / "batch"

# 题目类型选项
QUESTION_TYPES = ["单选题", "多选题", "填空题", "解答题"]

# 难度选项：1-5（1 最易，5 最难）；空表示未设
DIFFICULTIES = ["1", "2", "3", "4", "5"]
