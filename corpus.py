"""识别语料留档：在中间产物被清掉之前，把切分逻辑的输入与账目另存一份。

为什么需要这一层
----------------
project-alpha 的 `_cleanup_temp` 的保留清单只有 `<stem>_normalized.md` 和
`validation_report.md`，也就是说 `<stem>_raw.md`（MinerU 原文，**切分的唯一
输入**）和 `<stem>_blocks.json`（逐块路径的切块/配对账目）每次转换完就被删掉。
真实产物目录里 393 个 stem 只剩 45 份 `_raw.md`，就是这么蒸发的。

代价是硬的：CLAUDE.md 里所有「在 14 / 60 份真实产物上标定」的阈值与权重
（`_DROP_NOTE_RATIO=0.25`、方言权重、`optcheck` 那两处反直觉的窄），语料没了
就再也复现不出来。而同一份 CLAUDE.md 又写着「单元测试替代不了真实产物的逐字节
回归」——v0.3.1 那次 `. .....4分` 静默吞掉一整道题，测试全绿，靠 61 份文件比对
才照出来。没有语料，那句规则就成了一句做不到的话。

所以这里只做一件事：`_cleanup_temp` 之前把该留的复制走。

三条不能破的约束
----------------
1. **留档失败绝不能搞掉转换**。用户是来转题的，不是来攒语料的。所有函数吞掉
   自己的异常，只记日志（同 `blockpipe._dump` 的态度：诊断产物不该反过来伤主
   流程）。
2. **不改 project-alpha**（按项目约定）。所以是「转换侧先复制」，不是「改那边
   的保留清单」。
3. **不进版本库**。语料是用户上传的真实卷子，落在 `data/corpus/` 下，跟着
   `.gitignore` 的 `data/` 一起被忽略。也**不要**加进 `cleanup_output.py`：那
   脚本按龄回收，而留档的价值随时间增长。容量由本模块自己的 `MAX_ARCHIVES`
   兜住（淘汰最旧的整份），不按天数过期。
"""

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# 一份留档 = 一次转换，通常只有 1 个 md（几十 KB ~ 几百 KB）加一个 json。
# 800 份在真实卷子体量下约 100~200MB，够覆盖「按方言/来源分层各挑十几份」的
# 标定需求；再多的边际收益很低，而生产是 2 核 1.6G 的轻量服务器。
# 淘汰按目录名（前缀是时间戳）字典序取最旧，不读 mtime——复制出来的文件 mtime
# 是复制时间，靠它排序在批量转换里会乱序。
MAX_ARCHIVES = 800

# 要留的中间产物。三种 raw 各自对应一条转换路径：
#   `<stem>_raw.md`           单文件（PDF/Word/图片）
#   `<stem>_combined_raw.md`  题干+解析两份合并后的原文（切块实际吃的是这份）
#   `<stem>_blocks.json`      逐块路径的切块与配对账目（whole 路径没有）
# `_normalized.md` 也一并留：判断一次改动是变好还是变坏，要能看到最终输出。
_KEEP_SUFFIXES = (
    "_raw.md",
    "_combined_raw.md",
    "_blocks.json",
    "_combined_blocks.json",
    "_doc2x.json",
    "_normalized.md",
)


def _archive_root() -> Path:
    return Path(config.CORPUS_DIR)


def _new_dir(stem: str) -> Path:
    """为一次留档开目录：`<时间戳>-<stem>`。

    带时间戳而不是直接用 stem：同一份卷子「重新转换」很常见（改题号模板、换
    引擎），后一次不该把前一次覆盖掉——两次的差异本身就是最有价值的语料。
    同一秒内的重名再挂 `-2`、`-3`（批量并发时会撞）。
    """
    ts = datetime.fromtimestamp(time.time()).strftime("%Y%m%d-%H%M%S")
    base = _archive_root() / f"{ts}-{stem}"
    if not base.exists():
        return base
    for n in range(2, 100):
        cand = _archive_root() / f"{ts}-{stem}-{n}"
        if not cand.exists():
            return cand
    return _archive_root() / f"{ts}-{stem}-{int(time.time() * 1000) % 100000}"


def _prune() -> None:
    """超过 MAX_ARCHIVES 时删掉最旧的若干份整目录。"""
    root = _archive_root()
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    for old in dirs[:max(0, len(dirs) - MAX_ARCHIVES)]:
        try:
            shutil.rmtree(old)
        except OSError:
            pass  # 被占用就留到下次，不值得为此报错


def archive(extract_dir, stem: str, *, meta: dict | None = None,
            texts: dict | None = None) -> Path | None:
    """把 extract_dir 里该留的中间产物复制到语料目录，返回留档目录（失败返回 None）。

    必须在 `_cleanup_temp(extract_dir, stem, ...)` **之前**调用——那之后
    `_raw.md` 已经不在了。

    texts 是「只在内存里、没落盘」的内容（`{文件名: 正文}`）。图片那条路径就是
    这样：`_convert_image` 从不写 `_raw.md`（它也不调 `_cleanup_temp`），原文只
    存在于变量里。不给这条路径开个口，纯图片上传的语料就永远收不到——而图片恰
    恰是 OCR 最容易出错、最需要标定的那一类输入。

    meta 里放的是「事后想复盘就一定得知道、但文件本身看不出来」的东西，目前
    最要紧的是 `mineru_model_version`：2026-08 那次 MinerU 把 `vlm` 从 3.4.0
    静默升到 3.4.4、开始整项丢掉行内公式选项（见 optcheck 模块头），当时是靠
    人肉比对两批卷子才定位到版本上。有这一行，同类事故直接按版本分组就能看出来。
    """
    src = Path(extract_dir)
    try:
        wanted = []
        if src.is_dir():
            wanted = [p for p in src.iterdir()
                      if p.is_file() and p.name.startswith(stem)
                      and p.name.endswith(_KEEP_SUFFIXES)]
        if not wanted and not texts:
            return None
        root = _archive_root()
        root.mkdir(parents=True, exist_ok=True)
        dest = _new_dir(stem)
        dest.mkdir(parents=True, exist_ok=True)
        for p in wanted:
            shutil.copy2(p, dest / p.name)
        for name, body in (texts or {}).items():
            # 落盘的那份优先：磁盘上的是真实产物，内存里的可能已被后续步骤改过
            if not (dest / name).exists():
                (dest / name).write_text(body or "", encoding="utf-8")
        payload = {
            "stem": stem,
            "archived_at": datetime.now().isoformat(timespec="seconds"),
            "files": sorted(p.name for p in dest.iterdir() if p.is_file()),
        }
        payload.update(meta or {})
        (dest / "meta.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _prune()
        logger.info("语料留档 %s（%d 个文件）", dest.name, len(payload["files"]))
        return dest
    except Exception as e:  # noqa: BLE001 —— 留档绝不能把转换带下去
        logger.warning("[WARN] 语料留档失败（不影响转换）: %s: %s",
                       type(e).__name__, e)
        return None


def iter_archives(root=None):
    """按时间顺序遍历留档目录，产出 (目录, meta dict)。供 tools/eval_split.py 用。

    meta 读不出来时给空 dict 而不是跳过：留档目录里的 md 本身仍然是有效语料，
    没有 meta 只是少了版本归因这一维。
    """
    base = Path(root) if root is not None else _archive_root()
    if not base.is_dir():
        return
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        meta = {}
        f = d / "meta.json"
        if f.is_file():
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        yield d, meta


def raw_md_of(archive_dir) -> Path | None:
    """留档目录里那份「切分实际吃的原文」。

    合并原文优先：题干+解析那条路径下，`blocksplit` 吃的是 `_combined_raw.md`，
    拿单份 `_raw.md` 去跑复现不出当时的行为（解析区边界都不一样）。
    """
    d = Path(archive_dir)
    for pat in ("*_combined_raw.md", "*_raw.md"):
        hits = sorted(d.glob(pat))
        if hits:
            return hits[0]
    return None
